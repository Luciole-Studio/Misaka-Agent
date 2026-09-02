"""OpenAI Responses provider adapter."""

from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncIterator
from typing import Any, Literal, TypedDict

try:
    from openai import AsyncOpenAI
except ImportError:  # optional extra: misaka[openai]
    AsyncOpenAI = None

from misaka.ai.env_api_keys import get_env_api_key
from misaka.ai.models import clamp_thinking_level
from misaka.ai.providers._common import (
    _await_maybe_with_signal,
    _await_with_signal,
    _close_stream,
    _empty_usage,
    _option,
    apply_service_tier_pricing,
    resolve_cache_retention,
)
from misaka.ai.providers.cloudflare import (
    is_cloudflare_provider,
    resolve_cloudflare_base_url,
)
from misaka.ai.providers.constrained_sampling import (
    create_grammar_tool_input_properties,
)
from misaka.ai.providers.github_copilot_headers import (
    build_copilot_dynamic_headers,
    has_copilot_vision_input,
)
from misaka.ai.providers.openai_prompt_cache import clamp_openai_prompt_cache_key
from misaka.ai.providers.openai_responses_shared import (
    convert_responses_messages,
    convert_responses_tools,
    process_responses_stream,
)
from misaka.ai.providers.sdk import require
from misaka.ai.providers.simple_options import build_base_options
from misaka.ai.types import (
    AssistantMessage,
    CacheRetention,
    Context,
    DoneEvent,
    ErrorEvent,
    Model,
    SimpleStreamOptions,
    StartEvent,
    StreamOptions,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream, spawn_stream_task
from misaka.ai.utils.headers import (
    apply_provider_headers,
    headers_to_record,
    provider_headers_to_record,
)
from misaka.ai.utils.provider_retry import retry_provider_request
from misaka.utils.values import maybe_await, read_field, signal_aborted

OPENAI_TOOL_CALL_PROVIDERS = {"openai", "openai-codex", "opencode"}


class OpenAIResponsesOptions(TypedDict, total=False):
    apiKey: str
    headers: dict[str, str]
    signal: Any
    sessionId: str
    cacheRetention: str
    onPayload: Any
    onResponse: Any
    timeoutMs: int
    maxRetries: int
    maxTokens: int
    temperature: float
    reasoningEffort: Literal["minimal", "low", "medium", "high", "xhigh", "max"]
    reasoningSummary: Literal["auto", "detailed", "concise"] | None
    serviceTier: Literal["auto", "default", "flex", "scale", "priority"]


def _detect_session_affinity_format(model: Model) -> str:
    base_url = getattr(model, "baseUrl", "") or ""
    return "openrouter" if model.provider == "openrouter" or "openrouter.ai" in base_url else "openai"


def get_compat(model: Model) -> dict[str, Any]:
    compat = model.compat if getattr(model, "compat", None) is not None else None
    return {
        "sessionAffinityFormat": read_field(
            compat, "sessionAffinityFormat", _detect_session_affinity_format(model)
        ),
        "supportsLongCacheRetention": read_field(compat, "supportsLongCacheRetention", True),
    }


def get_prompt_cache_retention(compat: dict[str, bool], cache_retention: CacheRetention) -> str | None:
    return "24h" if cache_retention == "long" and compat["supportsLongCacheRetention"] else None


def _error_message(error: Exception) -> str:
    message = getattr(error, "message", None)
    return message if isinstance(message, str) else str(error)


def format_openai_responses_error(error: Any) -> str:
    if isinstance(error, Exception):
        status = getattr(error, "status", None)
        if isinstance(status, int):
            return f"OpenAI API error ({status}): {_error_message(error)}"
        return _error_message(error)
    try:
        return json.dumps(error)
    except (TypeError, ValueError):
        return str(error)


def create_client(
    model: Model,
    context: Context,
    api_key: str | None = None,
    options_headers: dict[str, str] | None = None,
    session_id: str | None = None,
) -> AsyncOpenAI:
    if not api_key:
        env_key = os.environ.get("OPENAI_API_KEY")
        if not env_key:
            raise RuntimeError(
                "OpenAI API key is required. Set OPENAI_API_KEY environment variable or pass it as an argument."
            )
        api_key = env_key

    compat = get_compat(model)
    headers = dict(model.headers or {})
    if model.provider == "github-copilot":
        copilot_headers = build_copilot_dynamic_headers(
            messages=context.messages,
            hasImages=has_copilot_vision_input(context.messages),
        )
        headers.update(copilot_headers)

    if session_id:
        if compat["sessionAffinityFormat"] == "openrouter":
            headers["x-session-id"] = session_id
        else:
            if compat["sessionAffinityFormat"] == "openai":
                headers["session_id"] = session_id
            headers["x-client-request-id"] = session_id

    apply_provider_headers(headers, options_headers)

    if model.provider == "cloudflare-ai-gateway":
        # The gateway's own header replaces the provider's, and resolved auth marks the
        # displaced ones with `None`. httpx refuses a `None` header value outright, so the
        # merge is filtered rather than passed through -- upstream's
        # `providerHeadersToRecord` drops them for the same reason.
        default_headers = provider_headers_to_record(
            {
                **headers,
                "Authorization": headers.get("Authorization"),
                "cf-aig-authorization": f"Bearer {api_key}",
            }
        ) or {}
    else:
        default_headers = headers

    return require(AsyncOpenAI, "openai")(
        api_key=api_key,
        base_url=resolve_cloudflare_base_url(model) if is_cloudflare_provider(model.provider) else model.baseUrl,
        default_headers=default_headers,
    )


def build_params(model: Model, context: Context, options: Any = None) -> dict[str, Any]:
    messages = convert_responses_messages(
        model,
        context,
        OPENAI_TOOL_CALL_PROVIDERS,
        {"grammarToolInputProperties": create_grammar_tool_input_properties(
        context.tools,
        bool(getattr(getattr(model, "compat", None), "supportsOpenAIGrammarTools", None)),
    )},
    )

    cache_retention = resolve_cache_retention(_option(options, "cacheRetention"))
    compat = get_compat(model)
    params: dict[str, Any] = {
        "model": model.id,
        "input": messages,
        "stream": True,
        "prompt_cache_key": (
            None if cache_retention == "none" else clamp_openai_prompt_cache_key(_option(options, "sessionId"))
        ),
        "prompt_cache_retention": get_prompt_cache_retention(compat, cache_retention),
        "store": False,
    }

    if _option(options, "maxTokens"):
        params["max_output_tokens"] = _option(options, "maxTokens")
    if _option(options, "temperature") is not None:
        params["temperature"] = _option(options, "temperature")
    if _option(options, "serviceTier") is not None:
        params["service_tier"] = _option(options, "serviceTier")
    if context.tools:
        params["tools"] = convert_responses_tools(
            context.tools,
            {"supportsStrictMode": bool(getattr(getattr(model, "compat", None), "supportsStrictMode", None) if getattr(getattr(model, "compat", None), "supportsStrictMode", None) is not None else True),
             "supportsOpenAIGrammarTools": bool(getattr(getattr(model, "compat", None), "supportsOpenAIGrammarTools", None))},
        )

    reasoning_effort = _option(options, "reasoningEffort")
    reasoning_summary = _option(options, "reasoningSummary")
    if model.reasoning:
        if reasoning_effort or reasoning_summary:
            effort = (
                model.thinkingLevelMap.get(reasoning_effort, reasoning_effort)
                if reasoning_effort and model.thinkingLevelMap
                else reasoning_effort or "medium"
            )
            params["reasoning"] = {"effort": effort, "summary": reasoning_summary or "auto"}
            params["include"] = ["reasoning.encrypted_content"]
        elif model.provider != "github-copilot":
            has_off_override = False
            off_value: Any = None
            if model.thinkingLevelMap is not None:
                has_off_override = "off" in model.thinkingLevelMap
                off_value = model.thinkingLevelMap.get("off")
            if model.thinkingLevelMap is None or not has_off_override or off_value is not None:
                params["reasoning"] = {"effort": "none" if off_value is None else off_value}

    # Upstream applies samplingParams last with `Object.assign` (openai-responses.ts:342-343),
    # so custom keys override the named request fields above. openai_completions carries a
    # declared deviation in the other direction; this file follows upstream until the owner
    # unifies the two.
    sampling = _option(options, "samplingParams")
    if isinstance(sampling, dict) and sampling:
        params.update(sampling)

    return params


async def _iterate_stream(stream_obj: Any, signal: Any = None) -> AsyncIterator[dict[str, Any]]:
    iterator = stream_obj.__aiter__()
    while True:
        try:
            event = await _await_with_signal(iterator.__anext__(), signal, on_abort=lambda: _close_stream(stream_obj))
        except StopAsyncIteration:
            return

        if hasattr(event, "model_dump"):
            yield event.model_dump()
        elif isinstance(event, dict):
            yield event
        else:
            yield json.loads(json.dumps(event, default=lambda value: value.__dict__))


async def _create_responses_stream(client: Any, params: dict[str, Any], options: Any, model: Model) -> Any:
    signal = _option(options, "signal")
    request_client = client
    request_client_kwargs: dict[str, Any] = {}
    timeout_ms = _option(options, "timeoutMs")
    if timeout_ms is not None:
        request_client_kwargs["timeout"] = timeout_ms / 1000
    # Upstream disables the SDK's own retry and retries the request itself
    # (`maxRetries: 0` in its requestOptions), so the policy that decides *what* is
    # worth retrying is `utils/provider_retry`, not whichever heuristic the SDK ships.
    request_client_kwargs["max_retries"] = 0
    if request_client_kwargs and hasattr(client, "with_options"):
        request_client = client.with_options(**request_client_kwargs)

    responses = getattr(request_client, "responses", None)
    with_raw_response = getattr(responses, "with_raw_response", None)
    if with_raw_response is not None and hasattr(with_raw_response, "create"):
        raw_response = await _await_maybe_with_signal(with_raw_response.create(**params), signal)
        on_response = _option(options, "onResponse")
        if callable(on_response):
            await maybe_await(
                on_response(
                    {
                        "status": raw_response.http_response.status_code,
                        "headers": headers_to_record(raw_response.http_response.headers),
                    },
                    model,
                )
            )
        return await _await_maybe_with_signal(raw_response.parse(), signal)

    return await _await_maybe_with_signal(responses.create(**params), signal)


def stream_openai_responses(
    model: Model,
    context: Context,
    options: StreamOptions | dict[str, Any] | None = None,
) -> AssistantMessageEventStream:
    stream = AssistantMessageEventStream()

    async def run() -> None:
        output = AssistantMessage(
            content=[],
            api=model.api,
            provider=model.provider,
            model=model.id,
            usage=_empty_usage(),
            stopReason="stop",
            timestamp=time.time_ns() // 1_000_000,
        )

        try:
            api_key = _option(options, "apiKey") or get_env_api_key(model.provider) or ""
            cache_retention = resolve_cache_retention(_option(options, "cacheRetention"))
            cache_session_id = None if cache_retention == "none" else _option(options, "sessionId")
            client = create_client(
                model,
                context,
                api_key,
                _option(options, "headers"),
                cache_session_id,
            )
            params = build_params(model, context, options)
            on_payload = _option(options, "onPayload")
            if callable(on_payload):
                next_params = await maybe_await(on_payload(params, model))
                if next_params is not None:
                    params = next_params
            openai_stream = await retry_provider_request(
                lambda: _create_responses_stream(client, params, options, model),
                max_retries=_option(options, "maxRetries") or 0,
                max_retry_delay_ms=_option(options, "maxRetryDelayMs"),
                signal=_option(options, "signal"),
            )
            stream.push(StartEvent(partial=output))
            signal = _option(options, "signal")
            await process_responses_stream(
                _iterate_stream(openai_stream, signal),
                output,
                stream,
                model,
                {
                    "grammarToolInputProperties": create_grammar_tool_input_properties(
                        context.tools,
                        bool(getattr(getattr(model, "compat", None), "supportsOpenAIGrammarTools", None)),
                    ),
                    "serviceTier": _option(options, "serviceTier"),
                    "applyServiceTierPricing": lambda usage, tier: apply_service_tier_pricing(usage, tier, model),
                },
            )

            if signal_aborted(signal):
                raise RuntimeError("Request was aborted")
            if output.stopReason in {"aborted", "error"}:
                raise RuntimeError("An unknown error occurred")

            stream.push(DoneEvent(reason=output.stopReason, message=output))
        except Exception as error:  # noqa: BLE001
            for block in output.content:
                for attr in ("index", "partialJson"):
                    if hasattr(block, attr):
                        delattr(block, attr)
            signal = _option(options, "signal")
            output.stopReason = "aborted" if signal_aborted(signal) else "error"
            output.errorMessage = format_openai_responses_error(error)
            stream.push(ErrorEvent(reason=output.stopReason, error=output))
        finally:
            stream.end()

    spawn_stream_task(run())
    return stream


def stream_simple_openai_responses(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessageEventStream:
    api_key = _option(options, "apiKey") or get_env_api_key(model.provider)
    if not api_key:
        raise RuntimeError(f"No API key for provider: {model.provider}")

    base = build_base_options(model, context, options, api_key)
    clamped_reasoning = clamp_thinking_level(model, options.reasoning) if options and options.reasoning else None
    reasoning_effort = None if clamped_reasoning == "off" else clamped_reasoning

    return stream_openai_responses(
        model,
        context,
        {
            **base.model_dump(),
            "reasoningEffort": reasoning_effort,
        },
    )


streamOpenAIResponses = stream_openai_responses
streamSimpleOpenAIResponses = stream_simple_openai_responses
applyServiceTierPricing = apply_service_tier_pricing

__all__ = [
    "OpenAIResponsesOptions",
    "streamOpenAIResponses",
    "streamSimpleOpenAIResponses",
]

"""OpenAI Responses provider adapter."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Mapping
from typing import Any, Literal, TypedDict

try:
    from openai import AsyncOpenAI
except ImportError:  # optional extra: misaka[openai]
    AsyncOpenAI = None

import misaka.ai.providers.cloudflare as _cloudflare
import misaka.ai.providers.github_copilot_headers as _copilot_headers
from misaka.ai.env_api_keys import get_env_api_key
from misaka.ai.models import clamp_thinking_level
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
    Usage,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream
from misaka.ai.utils.headers import headers_to_record

OPENAI_TOOL_CALL_PROVIDERS = {"openai", "openai-codex", "opencode"}
is_cloudflare_provider = getattr(_cloudflare, "is_cloudflare_provider", lambda _provider: False)
resolve_cloudflare_base_url = getattr(_cloudflare, "resolve_cloudflare_base_url", lambda model: model.baseUrl)
build_copilot_dynamic_headers = getattr(_copilot_headers, "build_copilot_dynamic_headers", lambda **_: {})
has_copilot_vision_input = getattr(_copilot_headers, "has_copilot_vision_input", lambda _messages: False)


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
    reasoningEffort: Literal["minimal", "low", "medium", "high", "xhigh"]
    reasoningSummary: Literal["auto", "detailed", "concise"] | None
    serviceTier: Literal["auto", "default", "flex", "scale", "priority"]


def _option(options: Any, name: str, default: Any = None) -> Any:
    if options is None:
        return default
    if isinstance(options, dict):
        return options.get(name, default)
    return getattr(options, name, default)


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


def _compat_value(compat: Any, name: str, default: Any = None) -> Any:
    if compat is None:
        return default
    if isinstance(compat, Mapping):
        return compat.get(name, default)
    return getattr(compat, name, default)


def _is_aborted(signal: Any) -> bool:
    if signal is None:
        return False
    if getattr(signal, "aborted", False):
        return True
    return bool(getattr(signal, "is_set", lambda: False)())


def _create_abort_wait_task(signal: Any) -> asyncio.Task[None] | None:
    if signal is None or not hasattr(signal, "wait"):
        return None
    return asyncio.create_task(signal.wait())


async def _await_with_signal(awaitable: Any, signal: Any, *, on_abort: Any = None) -> Any:
    if _is_aborted(signal):
        if isinstance(awaitable, asyncio.Future):
            awaitable.cancel()
        else:
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
        if on_abort is not None:
            await _maybe_await(on_abort())
        raise RuntimeError("Request was aborted")

    task = asyncio.ensure_future(awaitable)
    abort_task = _create_abort_wait_task(signal)
    try:
        if abort_task is not None:
            done, _ = await asyncio.wait({task, abort_task}, return_when=asyncio.FIRST_COMPLETED)
            if abort_task in done and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                if on_abort is not None:
                    await _maybe_await(on_abort())
                raise RuntimeError("Request was aborted")
        return await task
    finally:
        if abort_task is not None:
            abort_task.cancel()
            await asyncio.gather(abort_task, return_exceptions=True)


async def _await_maybe_with_signal(value: Any, signal: Any, *, on_abort: Any = None) -> Any:
    if hasattr(value, "__await__"):
        return await _await_with_signal(value, signal, on_abort=on_abort)
    if _is_aborted(signal):
        if on_abort is not None:
            await _maybe_await(on_abort())
        raise RuntimeError("Request was aborted")
    return value


def resolve_cache_retention(cache_retention: CacheRetention | None = None) -> CacheRetention:
    if cache_retention:
        return cache_retention
    return "long" if os.environ.get("MISAKA_CACHE_RETENTION") == "long" else "short"


def get_compat(model: Model) -> dict[str, bool]:
    compat = model.compat if getattr(model, "compat", None) is not None else None
    return {
        "sendSessionIdHeader": _compat_value(compat, "sendSessionIdHeader", True),
        "supportsLongCacheRetention": _compat_value(compat, "supportsLongCacheRetention", True),
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
    except Exception:
        return str(error)


def _empty_usage() -> Usage:
    return AssistantMessage(
        content=[],
        api="openai-responses",
        provider="openai",
        model="",
        usage={"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0, "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}},
        stopReason="stop",
        timestamp=0,
    ).usage


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
        if compat["sendSessionIdHeader"]:
            headers["session_id"] = session_id
        headers["x-client-request-id"] = session_id

    if options_headers:
        headers.update(options_headers)

    default_headers = (
        {
            **headers,
            "Authorization": headers.get("Authorization"),
            "cf-aig-authorization": f"Bearer {api_key}",
        }
        if model.provider == "cloudflare-ai-gateway"
        else headers
    )

    return require(AsyncOpenAI, "openai")(
        api_key=api_key,
        base_url=resolve_cloudflare_base_url(model) if is_cloudflare_provider(model.provider) else model.baseUrl,
        default_headers=default_headers,
    )


def build_params(model: Model, context: Context, options: Any = None) -> dict[str, Any]:
    messages = convert_responses_messages(model, context, OPENAI_TOOL_CALL_PROVIDERS)

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
        params["tools"] = convert_responses_tools(context.tools)

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

    return params


def get_service_tier_cost_multiplier(model: Model, service_tier: str | None) -> float:
    if service_tier == "flex":
        return 0.5
    if service_tier == "priority":
        return 2.5 if model.id == "gpt-5.5" else 2.0
    return 1.0


def apply_service_tier_pricing(usage: Usage, service_tier: str | None, model: Model) -> None:
    multiplier = get_service_tier_cost_multiplier(model, service_tier)
    if multiplier == 1:
        return
    usage.cost.input *= multiplier
    usage.cost.output *= multiplier
    usage.cost.cacheRead *= multiplier
    usage.cost.cacheWrite *= multiplier
    usage.cost.total = usage.cost.input + usage.cost.output + usage.cost.cacheRead + usage.cost.cacheWrite


async def _close_stream(stream_obj: Any) -> None:
    close = getattr(stream_obj, "close", None)
    if callable(close):
        try:
            await _maybe_await(close())
        except Exception:  # noqa: BLE001
            return


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
    max_retries = _option(options, "maxRetries")
    if max_retries is not None:
        request_client_kwargs["max_retries"] = max_retries
    if request_client_kwargs and hasattr(client, "with_options"):
        request_client = client.with_options(**request_client_kwargs)

    responses = getattr(request_client, "responses", None)
    with_raw_response = getattr(responses, "with_raw_response", None)
    if with_raw_response is not None and hasattr(with_raw_response, "create"):
        raw_response = await _await_maybe_with_signal(with_raw_response.create(**params), signal)
        on_response = _option(options, "onResponse")
        if callable(on_response):
            await _maybe_await(
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
                next_params = await _maybe_await(on_payload(params, model))
                if next_params is not None:
                    params = next_params
            openai_stream = await _create_responses_stream(client, params, options, model)
            stream.push(StartEvent(partial=output))
            signal = _option(options, "signal")
            await process_responses_stream(
                _iterate_stream(openai_stream, signal),
                output,
                stream,
                model,
                {
                    "serviceTier": _option(options, "serviceTier"),
                    "applyServiceTierPricing": lambda usage, tier: apply_service_tier_pricing(usage, tier, model),
                },
            )

            if _is_aborted(signal):
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
            output.stopReason = "aborted" if _is_aborted(signal) else "error"
            output.errorMessage = format_openai_responses_error(error)
            stream.push(ErrorEvent(reason=output.stopReason, error=output))
        finally:
            stream.end()

    asyncio.create_task(run())
    return stream


def stream_simple_openai_responses(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessageEventStream:
    api_key = _option(options, "apiKey") or get_env_api_key(model.provider)
    if not api_key:
        raise RuntimeError(f"No API key for provider: {model.provider}")

    base = build_base_options(model, options, api_key)
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
resolveCacheRetention = resolve_cache_retention
getCompat = get_compat
createClient = create_client
buildParams = build_params
getServiceTierCostMultiplier = get_service_tier_cost_multiplier
applyServiceTierPricing = apply_service_tier_pricing

__all__ = [
    "OpenAIResponsesOptions",
    "streamOpenAIResponses",
    "streamSimpleOpenAIResponses",
]

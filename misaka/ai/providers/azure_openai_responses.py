"""Azure OpenAI Responses provider adapter."""

from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncIterable
from typing import Any, Literal, TypedDict
from urllib.parse import urlparse, urlunparse

try:
    from openai import AsyncAzureOpenAI
except ImportError:  # optional extra: misaka[openai]
    AsyncAzureOpenAI = None

from misaka.ai.env_api_keys import get_env_api_key
from misaka.ai.models import clamp_thinking_level
from misaka.ai.providers._common import (
    _await_with_signal,
    _close_stream,
    _empty_usage,
    _option,
)
from misaka.ai.providers.constrained_sampling import (
    create_grammar_tool_input_properties,
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
    Context,
    DoneEvent,
    ErrorEvent,
    Model,
    SimpleStreamOptions,
    StartEvent,
    StreamOptions,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream, spawn_stream_task
from misaka.ai.utils.headers import headers_to_record
from misaka.ai.utils.user_agent import get_misaka_user_agent
from misaka.utils.values import maybe_await, signal_aborted

DEFAULT_AZURE_API_VERSION = "v1"
AZURE_TOOL_CALL_PROVIDERS = {"openai", "openai-codex", "opencode", "azure-openai-responses"}


class AzureOpenAIResponsesOptions(TypedDict, total=False):
    apiKey: str
    headers: dict[str, str]
    signal: Any
    sessionId: str
    cacheRetention: str
    onPayload: Any
    onResponse: Any
    timeoutMs: int
    maxRetries: int
    reasoningEffort: Literal["minimal", "low", "medium", "high", "xhigh", "max"]
    reasoningSummary: Literal["auto", "detailed", "concise"] | None
    azureApiVersion: str
    azureResourceName: str
    azureBaseUrl: str
    azureDeploymentName: str


def parse_deployment_name_map(value: str | None) -> dict[str, str]:
    mapping: dict[str, str] = {}
    if not value:
        return mapping
    for entry in value.split(","):
        trimmed = entry.strip()
        if not trimmed:
            continue
        model_id, separator, deployment_name = trimmed.partition("=")
        if separator and model_id.strip() and deployment_name.strip():
            mapping[model_id.strip()] = deployment_name.strip()
    return mapping


def resolve_deployment_name(model: Model, options: Any = None) -> str:
    explicit = _option(options, "azureDeploymentName")
    if explicit:
        return explicit
    mapped = parse_deployment_name_map(os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME_MAP")).get(model.id)
    return mapped or model.id


def format_azure_openai_error(error: Any) -> str:
    if isinstance(error, Exception):
        status = getattr(error, "status", None)
        if isinstance(status, int):
            return f"Azure OpenAI API error ({status}): {error}"
        return str(error)
    try:
        return json.dumps(error)
    except (TypeError, ValueError):
        return str(error)


def normalize_azure_base_url(base_url: str) -> str:
    trimmed = base_url.strip().rstrip("/")
    parsed = urlparse(trimmed)
    try:
        hostname, port = parsed.hostname, parsed.port
    except ValueError as error:
        # `new URL()` refuses an out-of-range port outright; `urlparse` defers the
        # complaint to `.port`. Surfacing it here keeps the two implementations
        # rejecting the same inputs instead of letting a typo through to the SDK.
        raise RuntimeError(f"Invalid Azure OpenAI base URL: {base_url}") from error
    del port
    if not parsed.scheme or not parsed.netloc or hostname is None:
        raise RuntimeError(f"Invalid Azure OpenAI base URL: {base_url}")

    # `.ai.azure.com` is Azure AI Foundry: those endpoints need the same base path, or the
    # SDK appends /deployments/... to the bare host and every request 404s
    # (azure-openai-responses.ts:196-198).
    is_azure_host = hostname.endswith(
        (".openai.azure.com", ".ai.azure.com", ".cognitiveservices.azure.com")
    )
    normalized_path = parsed.path.rstrip("/")

    # `/openai/v1/responses` is what someone copies out of the portal's sample request;
    # upstream folds it back to the base path (azure-openai-responses.ts:203-210).
    if is_azure_host and normalized_path in {"", "/", "/openai", "/openai/v1/responses"}:
        parsed = parsed._replace(path="/openai/v1", query="")

    return urlunparse(parsed).rstrip("/")


def build_default_base_url(resource_name: str) -> str:
    return f"https://{resource_name}.openai.azure.com/openai/v1"


def resolve_azure_config(model: Model, options: Any = None) -> dict[str, str]:
    api_version = _option(options, "azureApiVersion") or os.environ.get("AZURE_OPENAI_API_VERSION") or DEFAULT_AZURE_API_VERSION
    base_url = (_option(options, "azureBaseUrl") or os.environ.get("AZURE_OPENAI_BASE_URL") or "").strip() or None
    resource_name = _option(options, "azureResourceName") or os.environ.get("AZURE_OPENAI_RESOURCE_NAME")

    resolved_base_url = base_url
    if not resolved_base_url and resource_name:
        resolved_base_url = build_default_base_url(resource_name)
    if not resolved_base_url and model.baseUrl:
        resolved_base_url = model.baseUrl
    if not resolved_base_url:
        raise RuntimeError(
            "Azure OpenAI base URL is required. Set AZURE_OPENAI_BASE_URL or AZURE_OPENAI_RESOURCE_NAME, or pass azureBaseUrl, azureResourceName, or model.baseUrl."
        )

    return {"baseUrl": normalize_azure_base_url(resolved_base_url), "apiVersion": api_version}


def create_client(model: Model, api_key: str, options: Any = None) -> AsyncAzureOpenAI:
    if not api_key:
        env_key = os.environ.get("AZURE_OPENAI_API_KEY")
        if not env_key:
            raise RuntimeError(
                "Azure OpenAI API key is required. Set AZURE_OPENAI_API_KEY environment variable or pass it as an argument."
            )
        api_key = env_key

    headers = {"User-Agent": get_misaka_user_agent(), **(model.headers or {})}
    if _option(options, "headers"):
        headers.update(_option(options, "headers"))

    config = resolve_azure_config(model, options)
    return require(AsyncAzureOpenAI, "openai")(
        api_key=api_key,
        api_version=config["apiVersion"],
        default_headers=headers,
        base_url=config["baseUrl"],
    )


def build_params(model: Model, context: Context, options: Any, deployment_name: str) -> dict[str, Any]:
    messages = convert_responses_messages(
        model,
        context,
        AZURE_TOOL_CALL_PROVIDERS,
        {"grammarToolInputProperties": create_grammar_tool_input_properties(
        context.tools,
        bool(getattr(getattr(model, "compat", None), "supportsOpenAIGrammarTools", None)),
    )},
    )
    params: dict[str, Any] = {
        "model": deployment_name,
        "input": messages,
        "stream": True,
        "prompt_cache_key": clamp_openai_prompt_cache_key(_option(options, "sessionId")),
    }

    if _option(options, "maxTokens"):
        params["max_output_tokens"] = _option(options, "maxTokens")
    if _option(options, "temperature") is not None:
        params["temperature"] = _option(options, "temperature")
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
            effort = (model.thinkingLevelMap or {}).get(reasoning_effort, reasoning_effort) if reasoning_effort else "medium"
            params["reasoning"] = {"effort": effort, "summary": reasoning_summary or "auto"}
            params["include"] = ["reasoning.encrypted_content"]
        else:
            thinking_level_map = model.thinkingLevelMap or {}
            off_is_explicitly_null = isinstance(thinking_level_map, dict) and "off" in thinking_level_map and thinking_level_map["off"] is None
            if not off_is_explicitly_null:
                params["reasoning"] = {"effort": thinking_level_map.get("off") or "none"}

    # Upstream applies samplingParams last with `Object.assign` (azure-openai-responses.ts:333-334);
    # custom keys override the named request fields above. Same note as openai_responses:
    # openai_completions deviates the other way, by declared choice.
    sampling = _option(options, "samplingParams")
    if isinstance(sampling, dict) and sampling:
        params.update(sampling)

    return params


async def _iterate_stream(stream_obj: Any, signal: Any = None) -> AsyncIterable[dict[str, Any]]:
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
    request_client_options: dict[str, Any] = {}
    request_call_options: dict[str, Any] = {}
    timeout_ms = _option(options, "timeoutMs")
    if timeout_ms is not None:
        request_client_options["timeout"] = timeout_ms / 1000
    max_retries = _option(options, "maxRetries")
    if max_retries is not None:
        request_client_options["max_retries"] = max_retries
    if request_client_options and hasattr(client, "with_options"):
        request_client = client.with_options(**request_client_options)
    else:
        request_call_options = request_client_options

    responses = getattr(request_client, "responses", None)
    with_raw_response = getattr(responses, "with_raw_response", None)

    if with_raw_response is not None and hasattr(with_raw_response, "create"):
        raw_response = await _await_with_signal(with_raw_response.create(**params, **request_call_options), signal)
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
        return await _await_with_signal(raw_response.parse(), signal)

    return await _await_with_signal(responses.create(**params, **request_call_options), signal)


def _delete_stream_scratch_field(block: Any, name: str) -> None:
    if isinstance(block, dict):
        block.pop(name, None)
        return
    if hasattr(block, name):
        try:
            delattr(block, name)
        except Exception:  # noqa: BLE001
            return


def stream_azure_openai_responses(
    model: Model,
    context: Context,
    options: StreamOptions | dict[str, Any] | None = None,
) -> AssistantMessageEventStream:
    stream = AssistantMessageEventStream()

    async def run() -> None:
        deployment_name = resolve_deployment_name(model, options)
        output = AssistantMessage(
            content=[],
            api="azure-openai-responses",
            provider=model.provider,
            model=model.id,
            usage=_empty_usage(),
            stopReason="stop",
            timestamp=time.time_ns() // 1_000_000,
        )

        try:
            api_key = _option(options, "apiKey") or get_env_api_key(model.provider) or ""
            client = create_client(model, api_key, options)
            params = build_params(model, context, options, deployment_name)
            on_payload = _option(options, "onPayload")
            if callable(on_payload):
                next_params = await maybe_await(on_payload(params, model))
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
                    "grammarToolInputProperties": create_grammar_tool_input_properties(
                        context.tools,
                        bool(getattr(getattr(model, "compat", None), "supportsOpenAIGrammarTools", None)),
                    ),
                },
            )

            if signal_aborted(signal):
                raise RuntimeError("Request was aborted")
            if output.stopReason in {"aborted", "error"}:
                raise RuntimeError("An unknown error occurred")
            stream.push(DoneEvent(reason=output.stopReason, message=output))
        except Exception as error:  # noqa: BLE001
            for block in output.content:
                _delete_stream_scratch_field(block, "index")
                _delete_stream_scratch_field(block, "partialJson")
            signal = _option(options, "signal")
            output.stopReason = "aborted" if signal_aborted(signal) else "error"
            output.errorMessage = format_azure_openai_error(error)
            stream.push(ErrorEvent(reason=output.stopReason, error=output))
        finally:
            stream.end()

    spawn_stream_task(run())
    return stream


def stream_simple_azure_openai_responses(
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
    return stream_azure_openai_responses(
        model,
        context,
        {**base.model_dump(), "reasoningEffort": reasoning_effort},
    )


streamAzureOpenAIResponses = stream_azure_openai_responses
streamSimpleAzureOpenAIResponses = stream_simple_azure_openai_responses
__all__ = [
    "AzureOpenAIResponsesOptions",
    "streamAzureOpenAIResponses",
    "streamSimpleAzureOpenAIResponses",
]

"""Google Vertex AI provider adapter."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping
from typing import Any, Literal, TypedDict
from urllib.parse import urlparse

try:
    from google.genai import Client as GoogleGenAI
except ImportError:  # optional extra: misaka[google]
    GoogleGenAI = None

from misaka.ai.models import calculate_cost, clamp_thinking_level
from misaka.ai.providers._common import (
    _close_stream,
    _iterate_async_iterable,
    _prepare_sdk_params,
)
from misaka.ai.providers.google import (
    _build_tool_call,
    _coalesce_attr,
    _empty_usage,
    _finish_current_block,
    _nested_option,
    _option,
    get_google_budget,
    is_gemini3_flash_model,
    is_gemini3_pro_model,
    maybe_await,
)
from misaka.ai.providers.google_shared import (
    GoogleThinkingLevel,
    convert_messages,
    convert_tools,
    is_thinking_part,
    map_stop_reason,
    resolve_google_function_calling_mode,
    resolve_google_thinking_level,
    retain_thought_signature,
    supports_google_strict_tool_sampling,
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
    TextContent,
    TextDeltaEvent,
    TextStartEvent,
    ThinkingContent,
    ThinkingDeltaEvent,
    ThinkingStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    Usage,
    UsageCost,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream, spawn_stream_task
from misaka.ai.utils.headers import provider_headers_to_record
from misaka.ai.utils.sanitize_unicode import sanitize_surrogates
from misaka.ai.utils.user_agent import get_misaka_user_agent
from misaka.utils.values import signal_aborted

ClampedThinkingLevel = Literal["minimal", "low", "medium", "high"]


class GoogleVertexOptions(TypedDict, total=False):
    apiKey: str
    headers: dict[str, str]
    signal: Any
    sessionId: str
    cacheRetention: str
    onPayload: Any
    onResponse: Any
    timeoutMs: int
    maxRetries: int
    toolChoice: Literal["auto", "none", "any"]
    thinking: dict[str, Any]
    project: str
    location: str

API_VERSION = "v1"
GCP_VERTEX_CREDENTIALS_MARKER = "gcp-vertex-credentials"
_API_VERSION_PATTERN = re.compile(r"^v\d+(?:beta\d*)?$")
_PLACEHOLDER_API_KEY_PATTERN = re.compile(r"^<[^>]+>$")


def _format_google_vertex_error(error: Any) -> str:
    return str(error) if isinstance(error, Exception) else json.dumps(error, default=str)


def stream_google_vertex(
    model: Model,
    context: Context,
    options: StreamOptions | Mapping[str, Any] | None = None,
) -> AssistantMessageEventStream:
    stream = AssistantMessageEventStream()

    async def run() -> None:
        output = AssistantMessage(
            content=[],
            api="google-vertex",
            provider=model.provider,
            model=model.id,
            usage=_empty_usage(),
            stopReason="stop",
            timestamp=time.time_ns() // 1_000_000,
        )
        signal = _option(options, "signal")
        client: Any = _option(options, "client")

        try:
            if client is None:
                api_key = resolve_api_key(options)
                if api_key:
                    client = create_client_with_api_key(
                        model,
                        api_key,
                        _option(options, "headers"),
                        _option(options, "timeoutMs"),
                    )
                else:
                    client = create_client(
                        model,
                        resolve_project(options),
                        resolve_location(options),
                        _option(options, "headers"),
                        _option(options, "timeoutMs"),
                    )

            params = build_params(model, context, options)
            on_payload = _option(options, "onPayload")
            if callable(on_payload):
                next_params = await maybe_await(on_payload(params, model))
                if next_params is not None:
                    params = next_params

            google_stream = await client.aio.models.generate_content_stream(
                **_prepare_sdk_params(params))
            stream.push(StartEvent(partial=output))

            current_block: TextContent | ThinkingContent | None = None
            async for chunk in _iterate_async_iterable(google_stream, signal, on_abort=lambda: _close_stream(google_stream)):
                if signal_aborted(signal):
                    raise RuntimeError("Request was aborted")

                output.responseId = output.responseId or _coalesce_attr(chunk, "response_id", "responseId")
                candidates = _coalesce_attr(chunk, "candidates") or []
                candidate = candidates[0] if candidates else None
                parts = _coalesce_attr(_coalesce_attr(candidate, "content"), "parts") or []

                for part in parts:
                    part_text = _coalesce_attr(part, "text")
                    if part_text is not None:
                        is_thinking = is_thinking_part(part)
                        should_start_new_block = (
                            current_block is None
                            or (is_thinking and current_block.type != "thinking")
                            or (not is_thinking and current_block.type != "text")
                        )
                        if should_start_new_block:
                            _finish_current_block(current_block, output, stream)
                            if is_thinking:
                                current_block = ThinkingContent(thinking="")
                                output.content.append(current_block)
                                stream.push(ThinkingStartEvent(contentIndex=len(output.content) - 1, partial=output))
                            else:
                                current_block = TextContent(text="")
                                output.content.append(current_block)
                                stream.push(TextStartEvent(contentIndex=len(output.content) - 1, partial=output))

                        if current_block.type == "thinking":
                            current_block.thinking += part_text
                            current_block.thinkingSignature = retain_thought_signature(
                                current_block.thinkingSignature,
                                _coalesce_attr(part, "thought_signature", "thoughtSignature"),
                            )
                            stream.push(
                                ThinkingDeltaEvent(
                                    contentIndex=len(output.content) - 1,
                                    delta=part_text,
                                    partial=output,
                                )
                            )
                        else:
                            current_block.text += part_text
                            current_block.textSignature = retain_thought_signature(
                                current_block.textSignature,
                                _coalesce_attr(part, "thought_signature", "thoughtSignature"),
                            )
                            stream.push(
                                TextDeltaEvent(
                                    contentIndex=len(output.content) - 1,
                                    delta=part_text,
                                    partial=output,
                                )
                            )

                    function_call = _coalesce_attr(part, "function_call", "functionCall")
                    if function_call is not None:
                        _finish_current_block(current_block, output, stream)
                        current_block = None

                        tool_call = _build_tool_call(function_call, part, output)
                        output.content.append(tool_call)
                        content_index = len(output.content) - 1
                        stream.push(ToolCallStartEvent(contentIndex=content_index, partial=output))
                        stream.push(
                            ToolCallDeltaEvent(
                                contentIndex=content_index,
                                delta=json.dumps(tool_call.arguments),
                                partial=output,
                            )
                        )
                        stream.push(
                            ToolCallEndEvent(
                                contentIndex=content_index,
                                toolCall=tool_call,
                                partial=output,
                            )
                        )

                finish_reason = _coalesce_attr(candidate, "finish_reason", "finishReason")
                if finish_reason is not None:
                    output.stopReason = map_stop_reason(finish_reason)
                    if any(block.type == "toolCall" for block in output.content):
                        output.stopReason = "toolUse"

                usage_metadata = _coalesce_attr(chunk, "usage_metadata", "usageMetadata")
                if usage_metadata is not None:
                    usage = Usage(
                        input=max(
                            0,
                            int(_coalesce_attr(usage_metadata, "prompt_token_count", "promptTokenCount") or 0)
                            - int(
                                _coalesce_attr(
                                    usage_metadata,
                                    "cached_content_token_count",
                                    "cachedContentTokenCount",
                                )
                                or 0
                            ),
                        ),
                        output=(
                            int(
                                _coalesce_attr(
                                    usage_metadata,
                                    "response_token_count",
                                    "responseTokenCount",
                                    "candidates_token_count",
                                    "candidatesTokenCount",
                                )
                                or 0
                            )
                            + int(_coalesce_attr(usage_metadata, "thoughts_token_count", "thoughtsTokenCount") or 0)
                        ),
                        cacheRead=int(
                            _coalesce_attr(
                                usage_metadata,
                                "cached_content_token_count",
                                "cachedContentTokenCount",
                            )
                            or 0
                        ),
                        cacheWrite=0,
                        totalTokens=int(_coalesce_attr(usage_metadata, "total_token_count", "totalTokenCount") or 0),
                        cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
                    )
                    calculate_cost(model, usage)
                    output.usage = usage

            _finish_current_block(current_block, output, stream)

            if signal_aborted(signal):
                raise RuntimeError("Request was aborted")
            if output.stopReason in {"aborted", "error"}:
                raise RuntimeError("An unknown error occurred")

            stream.push(DoneEvent(reason=output.stopReason, message=output))
        except Exception as error:  # noqa: BLE001
            output.stopReason = "aborted" if signal_aborted(signal) else "error"
            output.errorMessage = _format_google_vertex_error(error)
            stream.push(ErrorEvent(reason=output.stopReason, error=output), cause=error)
        finally:
            aio_client = getattr(client, "aio", None)
            close = getattr(aio_client, "aclose", None) if aio_client is not None else None
            if callable(close):
                try:
                    await maybe_await(close())
                except Exception:  # noqa: BLE001, S110 - closing the SDK client is best-effort after the stream ended
                    pass
            stream.end()

    spawn_stream_task(run(), stream=stream)
    return stream


def stream_simple_google_vertex(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessageEventStream:
    base = build_base_options(model, context, options, None)
    reasoning = _option(options, "reasoning")
    if not reasoning:
        return stream_google_vertex(
            model,
            context,
            {
                **base.model_dump(),
                "thinking": {"enabled": False},
            },
        )

    clamped_reasoning = clamp_thinking_level(model, reasoning)
    effort: ClampedThinkingLevel = resolve_google_thinking_level(model, clamped_reasoning)

    if is_gemini3_pro_model(model) or is_gemini3_flash_model(model):
        return stream_google_vertex(
            model,
            context,
            {
                **base.model_dump(),
                "thinking": {
                    "enabled": True,
                    "level": get_gemini3_thinking_level(effort, model),
                },
            },
        )

    return stream_google_vertex(
        model,
        context,
        {
            **base.model_dump(),
            "thinking": {
                "enabled": True,
                "budgetTokens": get_google_budget(model, effort, _option(options, "thinkingBudgets")),
            },
        },
    )


def create_client(
    model: Model,
    project: str,
    location: str,
    options_headers: Mapping[str, str] | None = None,
    timeout_ms: int | None = None,
) -> GoogleGenAI:
    http_options = build_http_options(model, options_headers, timeout_ms) or {}
    http_options.setdefault("apiVersion", API_VERSION)
    return require(GoogleGenAI, "google-genai")(
        vertexai=True,
        project=project,
        location=location,
        http_options=http_options,
    )


def create_client_with_api_key(
    model: Model,
    api_key: str,
    options_headers: Mapping[str, str] | None = None,
    timeout_ms: int | None = None,
) -> GoogleGenAI:
    http_options = build_http_options(model, options_headers, timeout_ms) or {}
    http_options.setdefault("apiVersion", API_VERSION)
    return require(GoogleGenAI, "google-genai")(
        vertexai=True,
        api_key=api_key,
        http_options=http_options,
    )


def build_http_options(
    model: Model,
    options_headers: Mapping[str, str] | None = None,
    timeout_ms: int | None = None,
) -> dict[str, Any] | None:
    # The outer retry_google_request owns retries; disable the SDK's five-attempt default.
    http_options: dict[str, Any] = {"retryOptions": {"attempts": 1}}
    base_url = resolve_custom_base_url(model.baseUrl)
    if base_url:
        http_options["baseUrl"] = base_url
        http_options["baseUrlResourceScope"] = "COLLECTION"
        if base_url_includes_api_version(base_url):
            http_options["apiVersion"] = ""

    # Unconditional, as upstream is: the client string goes out even when neither the
    # catalog entry nor the caller contributes a header (google-generative-ai.ts:348).
    http_options["headers"] = provider_headers_to_record(
        {
            "User-Agent": get_misaka_user_agent(),
            **(model.headers or {}),
            **dict(options_headers or {}),
        }
    )
    if timeout_ms is not None:
        http_options["timeout"] = timeout_ms

    return http_options or None


def resolve_custom_base_url(base_url: str) -> str | None:
    trimmed = base_url.strip()
    if not trimmed or "{location}" in trimmed:
        return None
    return trimmed


def base_url_includes_api_version(base_url: str) -> bool:
    try:
        parsed = urlparse(base_url)
        return any(_API_VERSION_PATTERN.fullmatch(part) for part in parsed.path.split("/") if part)
    except Exception:  # noqa: BLE001
        return bool(re.search(r"(?:^|/)v\d+(?:beta\d*)?(?:/|$)", base_url))


def resolve_api_key(options: StreamOptions | Mapping[str, Any] | None = None) -> str | None:
    api_key = (_option(options, "apiKey") or os.environ.get("GOOGLE_CLOUD_API_KEY") or "").strip()
    if not api_key or api_key == GCP_VERTEX_CREDENTIALS_MARKER or is_placeholder_api_key(api_key):
        return None
    return api_key


def is_placeholder_api_key(api_key: str) -> bool:
    return _PLACEHOLDER_API_KEY_PATTERN.fullmatch(api_key) is not None


def resolve_project(options: StreamOptions | Mapping[str, Any] | None = None) -> str:
    project = _option(options, "project") or os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCLOUD_PROJECT")
    if not project:
        raise RuntimeError(
            "Vertex AI requires a project ID. Set GOOGLE_CLOUD_PROJECT/GCLOUD_PROJECT or pass project in options."
        )
    return project


def resolve_location(options: StreamOptions | Mapping[str, Any] | None = None) -> str:
    location = _option(options, "location") or os.environ.get("GOOGLE_CLOUD_LOCATION")
    if not location:
        raise RuntimeError("Vertex AI requires a location. Set GOOGLE_CLOUD_LOCATION or pass location in options.")
    return location


def build_params(
    model: Model,
    context: Context,
    options: StreamOptions | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    signal = _option(options, "signal")
    if signal is not None and signal_aborted(signal):
        raise RuntimeError("Request aborted")

    contents = convert_messages(model, context)

    generation_config: dict[str, Any] = {}
    if _option(options, "temperature") is not None:
        generation_config["temperature"] = _option(options, "temperature")
    if _option(options, "maxTokens") is not None:
        generation_config["maxOutputTokens"] = _option(options, "maxTokens")

    config: dict[str, Any] = dict(generation_config)
    if context.systemPrompt:
        config["systemInstruction"] = sanitize_surrogates(context.systemPrompt)
    supports_strict_mode = supports_google_strict_tool_sampling(model.id)
    if context.tools:
        converted_tools = convert_tools(context.tools, supports_strict_mode=supports_strict_mode)
        if converted_tools is not None:
            config["tools"] = converted_tools

    # `VALIDATED` is Google's half of strict sampling: sending the constrained schema
    # without asking for the mode leaves the constraint unenforced.
    mode = resolve_google_function_calling_mode(
        list(context.tools or []), _option(options, "toolChoice"), supports_strict_mode
    )
    config["toolConfig"] = (
        {"functionCallingConfig": {"mode": mode}} if context.tools and mode else None
    )

    thinking = _option(options, "thinking")
    if _nested_option(thinking, "enabled") and model.reasoning:
        thinking_config: dict[str, Any] = {"includeThoughts": True}
        if _nested_option(thinking, "level") is not None:
            thinking_config["thinkingLevel"] = _nested_option(thinking, "level")
        elif _nested_option(thinking, "budgetTokens") is not None:
            thinking_config["thinkingBudget"] = _nested_option(thinking, "budgetTokens")
        config["thinkingConfig"] = thinking_config
    elif model.reasoning and thinking is not None and not _nested_option(thinking, "enabled", True):
        config["thinkingConfig"] = get_disabled_thinking_config(model)

    if signal is not None:
        config["abortSignal"] = signal

    return {
        "model": model.id,
        "contents": contents,
        "config": config,
    }


def get_disabled_thinking_config(model: Model) -> dict[str, Any]:
    if is_gemini3_pro_model(model):
        return {"thinkingLevel": "LOW"}
    if is_gemini3_flash_model(model):
        return {"thinkingLevel": "MINIMAL"}
    return {"thinkingBudget": 0}


def get_gemini3_thinking_level(effort: ClampedThinkingLevel, model: Model) -> GoogleThinkingLevel:
    if is_gemini3_pro_model(model):
        if effort in {"minimal", "low"}:
            return "LOW"
        return "HIGH"
    if effort == "minimal":
        return "MINIMAL"
    if effort == "low":
        return "LOW"
    if effort == "medium":
        return "MEDIUM"
    return "HIGH"


streamGoogleVertex = stream_google_vertex
streamSimpleGoogleVertex = stream_simple_google_vertex
__all__ = [
    "GoogleVertexOptions",
    "streamGoogleVertex",
    "streamSimpleGoogleVertex",
]

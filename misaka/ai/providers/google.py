"""Google Generative AI provider adapter."""

from __future__ import annotations

import itertools
import json
import re
import time
from collections.abc import Mapping
from typing import Any, Literal, TypedDict

try:
    from google.genai import Client as GoogleGenAI
except ImportError:  # optional extra: misaka[google]
    GoogleGenAI = None

from misaka.ai.env_api_keys import get_env_api_key
from misaka.ai.models import calculate_cost, clamp_thinking_level
from misaka.ai.providers._common import (
    _close_stream,
    _empty_usage,
    _is_aborted,
    _iterate_async_iterable,
    _option,
    _prepare_sdk_params,
)
from misaka.ai.providers.google_shared import (
    GoogleThinkingLevel,
    coerce_thought_signature,
    convert_messages,
    convert_tools,
    is_thinking_part,
    map_stop_reason,
    map_tool_choice,
    retain_thought_signature,
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
    TextEndEvent,
    TextStartEvent,
    ThinkingBudgets,
    ThinkingContent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ThinkingStartEvent,
    ToolCall,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    Usage,
    UsageCost,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream, spawn_stream_task
from misaka.ai.utils.sanitize_unicode import sanitize_surrogates
from misaka.utils.values import maybe_await

ClampedThinkingLevel = Literal["minimal", "low", "medium", "high"]


class GoogleOptions(TypedDict, total=False):
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

_TOOL_CALL_COUNTER = itertools.count(1)


def _nested_option(options: Any, name: str, default: Any = None) -> Any:
    if options is None:
        return default
    if isinstance(options, Mapping):
        return options.get(name, default)
    return getattr(options, name, default)


def _format_google_error(error: Any) -> str:
    return str(error) if isinstance(error, Exception) else json.dumps(error, default=str)


def _coalesce_attr(obj: Any, *names: str) -> Any:
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


def _finish_current_block(
    current_block: TextContent | ThinkingContent | None,
    output: AssistantMessage,
    stream: AssistantMessageEventStream,
) -> None:
    if current_block is None:
        return

    content_index = len(output.content) - 1
    if current_block.type == "text":
        stream.push(TextEndEvent(contentIndex=content_index, content=current_block.text, partial=output))
    else:
        stream.push(ThinkingEndEvent(contentIndex=content_index, content=current_block.thinking, partial=output))


def _build_tool_call(function_call: Any, part: Any, output: AssistantMessage) -> ToolCall:
    name = _coalesce_attr(function_call, "name") or ""
    raw_args = _coalesce_attr(function_call, "args") or {}
    if isinstance(raw_args, Mapping):
        arguments = dict(raw_args)
    elif hasattr(raw_args, "model_dump"):
        arguments = raw_args.model_dump()
    else:
        arguments = {}

    provided_id = _coalesce_attr(function_call, "id")
    is_duplicate = any(
        isinstance(block, ToolCall) and block.id == provided_id
        for block in output.content
    )
    if not provided_id or is_duplicate:
        provided_id = f"{name}_{int(time.time() * 1000)}_{next(_TOOL_CALL_COUNTER)}"

    thought_signature = coerce_thought_signature(
        _coalesce_attr(part, "thought_signature", "thoughtSignature")
    )
    return ToolCall(
        id=provided_id,
        name=name,
        arguments=arguments,
        thoughtSignature=thought_signature,
    )


def create_client(
    model: Model,
    api_key: str | None = None,
    options_headers: Mapping[str, str] | None = None,
) -> GoogleGenAI:
    http_options: dict[str, Any] = {}
    if model.baseUrl:
        http_options["baseUrl"] = model.baseUrl
        http_options["apiVersion"] = ""
    if model.headers or options_headers:
        http_options["headers"] = {**(model.headers or {}), **dict(options_headers or {})}

    return require(GoogleGenAI, "google-genai")(
        api_key=api_key,
        http_options=http_options or None,
    )


def build_params(
    model: Model,
    context: Context,
    options: StreamOptions | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    signal = _option(options, "signal")
    if signal is not None and _is_aborted(signal):
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
    if context.tools:
        converted_tools = convert_tools(context.tools)
        if converted_tools is not None:
            config["tools"] = converted_tools

    tool_choice = _option(options, "toolChoice")
    if context.tools and tool_choice:
        config["toolConfig"] = {
            "functionCallingConfig": {
                "mode": map_tool_choice(tool_choice),
            },
        }
    else:
        config["toolConfig"] = None

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


def stream_google(
    model: Model,
    context: Context,
    options: StreamOptions | Mapping[str, Any] | None = None,
) -> AssistantMessageEventStream:
    stream = AssistantMessageEventStream()

    async def run() -> None:
        output = AssistantMessage(
            content=[],
            api="google-generative-ai",
            provider=model.provider,
            model=model.id,
            usage=_empty_usage(),
            stopReason="stop",
            timestamp=time.time_ns() // 1_000_000,
        )
        signal = _option(options, "signal")
        client: Any = None

        try:
            api_key = _option(options, "apiKey") or get_env_api_key(model.provider) or ""
            client = _option(options, "client") or create_client(
                model,
                api_key,
                _option(options, "headers"),
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
                if _is_aborted(signal):
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

            if _is_aborted(signal):
                raise RuntimeError("Request was aborted")
            if output.stopReason in {"aborted", "error"}:
                raise RuntimeError("An unknown error occurred")

            stream.push(DoneEvent(reason=output.stopReason, message=output))
        except Exception as error:  # noqa: BLE001
            output.stopReason = "aborted" if _is_aborted(signal) else "error"
            output.errorMessage = _format_google_error(error)
            stream.push(ErrorEvent(reason=output.stopReason, error=output))
        finally:
            aio_client = getattr(client, "aio", None)
            close = getattr(aio_client, "aclose", None) if aio_client is not None else None
            if callable(close):
                try:
                    await maybe_await(close())
                except Exception:  # noqa: BLE001, S110 - closing the SDK client is best-effort after the stream ended
                    pass
            stream.end()

    spawn_stream_task(run())
    return stream


def stream_simple_google(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessageEventStream:
    api_key = _option(options, "apiKey") or get_env_api_key(model.provider)
    if not api_key:
        raise RuntimeError(f"No API key for provider: {model.provider}")

    base = build_base_options(model, options, api_key)
    reasoning = _option(options, "reasoning")
    if not reasoning:
        return stream_google(
            model,
            context,
            {
                **base.model_dump(),
                "thinking": {"enabled": False},
            },
        )

    clamped_reasoning = clamp_thinking_level(model, reasoning)
    effort: ClampedThinkingLevel = "high" if clamped_reasoning in {"off", "xhigh"} else clamped_reasoning

    if is_gemini3_pro_model(model) or is_gemini3_flash_model(model) or is_gemma4_model(model):
        return stream_google(
            model,
            context,
            {
                **base.model_dump(),
                "thinking": {
                    "enabled": True,
                    "level": get_thinking_level(effort, model),
                },
            },
        )

    return stream_google(
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


def is_gemma4_model(model: Model) -> bool:
    return re.search(r"gemma-?4", model.id.lower()) is not None


def is_gemini3_pro_model(model: Model) -> bool:
    return re.search(r"gemini-3(?:\.\d+)?-pro", model.id.lower()) is not None


def is_gemini3_flash_model(model: Model) -> bool:
    return re.search(r"gemini-3(?:\.\d+)?-flash", model.id.lower()) is not None


def get_disabled_thinking_config(model: Model) -> dict[str, Any]:
    if is_gemini3_pro_model(model):
        return {"thinkingLevel": "LOW"}
    if is_gemini3_flash_model(model) or is_gemma4_model(model):
        return {"thinkingLevel": "MINIMAL"}
    return {"thinkingBudget": 0}


def get_thinking_level(effort: ClampedThinkingLevel, model: Model) -> GoogleThinkingLevel:
    if is_gemini3_pro_model(model):
        if effort in {"minimal", "low"}:
            return "LOW"
        return "HIGH"
    if is_gemma4_model(model):
        if effort in {"minimal", "low"}:
            return "MINIMAL"
        return "HIGH"
    if effort == "minimal":
        return "MINIMAL"
    if effort == "low":
        return "LOW"
    if effort == "medium":
        return "MEDIUM"
    return "HIGH"


def _custom_budget(custom_budgets: ThinkingBudgets | Mapping[str, int | None] | None, effort: str) -> int | None:
    if custom_budgets is None:
        return None
    if isinstance(custom_budgets, Mapping):
        value = custom_budgets.get(effort)
        return int(value) if value is not None else None
    value = getattr(custom_budgets, effort, None)
    return int(value) if value is not None else None


def get_google_budget(
    model: Model,
    effort: ClampedThinkingLevel,
    custom_budgets: ThinkingBudgets | Mapping[str, int | None] | None = None,
) -> int:
    custom_budget = _custom_budget(custom_budgets, effort)
    if custom_budget is not None:
        return custom_budget

    model_id = model.id.lower()
    if "2.5-pro" in model_id:
        budgets = {"minimal": 128, "low": 2048, "medium": 8192, "high": 32768}
        return budgets[effort]
    if "2.5-flash-lite" in model_id:
        budgets = {"minimal": 512, "low": 2048, "medium": 8192, "high": 24576}
        return budgets[effort]
    if "2.5-flash" in model_id:
        budgets = {"minimal": 128, "low": 2048, "medium": 8192, "high": 24576}
        return budgets[effort]
    return -1


streamGoogle = stream_google
streamSimpleGoogle = stream_simple_google
getThinkingLevel = get_thinking_level

__all__ = [
    "GoogleOptions",
    "streamGoogle",
    "streamSimpleGoogle",
]

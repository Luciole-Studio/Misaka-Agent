"""Deterministic faux provider for tests and local development."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import random
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Literal, Protocol, TypeAlias, TypedDict

from misaka.ai.api_registry import ApiProvider, register_api_provider, unregister_api_providers
from misaka.ai.types import (
    AssistantMessage,
    Context,
    DoneEvent,
    ErrorEvent,
    ImageContent,
    MessageValue,
    Model,
    SimpleStreamOptions,
    StartEvent,
    StopReason,
    StreamOptions,
    TextContent,
    TextDeltaEvent,
    TextEndEvent,
    TextStartEvent,
    ThinkingContent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ThinkingStartEvent,
    ToolCall,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultMessage,
    Usage,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream, create_assistant_message_event_stream

DEFAULT_API = "faux"
DEFAULT_PROVIDER = "faux"
DEFAULT_MODEL_ID = "faux-1"
DEFAULT_MODEL_NAME = "Faux Model"
DEFAULT_BASE_URL = "http://localhost:0"
DEFAULT_MIN_TOKEN_SIZE = 3
DEFAULT_MAX_TOKEN_SIZE = 5

DEFAULT_USAGE = Usage(
    input=0,
    output=0,
    cacheRead=0,
    cacheWrite=0,
    totalTokens=0,
    cost={"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
)


def _option(options: Any, name: str, default: Any = None) -> Any:
    if options is None:
        return default
    if isinstance(options, dict):
        value = options.get(name, default)
    else:
        value = getattr(options, name, default)
    return default if value is None else value


class FauxResponseFactory(Protocol):
    def __call__(
        self,
        context: Context,
        options: StreamOptions | None,
        state: dict[str, int],
        model: Model,
    ) -> AssistantMessage | Awaitable[AssistantMessage]: ...


class _FauxModelCost(TypedDict):
    input: float
    output: float
    cacheRead: float
    cacheWrite: float


class _FauxTokenSizeOptions(TypedDict, total=False):
    min: int
    max: int


class RegisterFauxProviderOptions(TypedDict, total=False):
    api: str
    provider: str
    models: list[FauxModelDefinition]
    tokensPerSecond: int
    tokenSize: _FauxTokenSizeOptions


@dataclass(slots=True)
class FauxModelDefinition:
    id: str
    name: str | None = None
    reasoning: bool | None = None
    input: list[Literal["text", "image"]] | None = None
    cost: _FauxModelCost | None = None
    contextWindow: int | None = None
    maxTokens: int | None = None


FauxContentBlock: TypeAlias = TextContent | ThinkingContent | ToolCall
FauxResponseStep: TypeAlias = AssistantMessage | FauxResponseFactory


@dataclass(slots=True)
class FauxProviderRegistration:
    api: str
    models: list[Model]
    state: dict[str, int]
    _set_responses: Any
    _append_responses: Any
    _pending_count: Any
    _unregister: Any

    def getModel(self, modelId: str | None = None) -> Model | None:
        if not modelId:
            return self.models[0]
        return next((candidate for candidate in self.models if candidate.id == modelId), None)

    def get_model(self, model_id: str | None = None) -> Model | None:
        return self.getModel(model_id)

    def setResponses(self, responses: list[FauxResponseStep]) -> None:
        self._set_responses(responses)

    def set_responses(self, responses: list[FauxResponseStep]) -> None:
        self.setResponses(responses)

    def appendResponses(self, responses: list[FauxResponseStep]) -> None:
        self._append_responses(responses)

    def append_responses(self, responses: list[FauxResponseStep]) -> None:
        self.appendResponses(responses)

    def getPendingResponseCount(self) -> int:
        return self._pending_count()

    def get_pending_response_count(self) -> int:
        return self.getPendingResponseCount()

    def unregister(self) -> None:
        self._unregister()


def _copy_default_usage() -> Usage:
    return DEFAULT_USAGE.model_copy(deep=True)


def faux_text(text: str) -> TextContent:
    return TextContent(text=text)


def faux_thinking(thinking: str) -> ThinkingContent:
    return ThinkingContent(thinking=thinking)


def faux_tool_call(name: str, arguments_: dict[str, Any], options: dict[str, str] | None = None) -> ToolCall:
    options = options or {}
    tool_id = options["id"] if "id" in options and options["id"] is not None else _random_id("tool")
    return ToolCall(id=tool_id, name=name, arguments=arguments_)


def _normalize_faux_assistant_content(content: str | FauxContentBlock | list[FauxContentBlock]) -> list[FauxContentBlock]:
    if isinstance(content, str):
        return [faux_text(content)]
    return content if isinstance(content, list) else [content]


def faux_assistant_message(
    content: str | FauxContentBlock | list[FauxContentBlock],
    options: dict[str, Any] | None = None,
    **legacy_kwargs: Any,
) -> AssistantMessage:
    options_map = dict(options or {})
    legacy_to_ts = {
        "stop_reason": "stopReason",
        "stopReason": "stopReason",
        "error_message": "errorMessage",
        "errorMessage": "errorMessage",
        "response_id": "responseId",
        "responseId": "responseId",
        "timestamp": "timestamp",
    }
    for legacy_name, ts_name in legacy_to_ts.items():
        if legacy_name in legacy_kwargs and ts_name not in options_map:
            options_map[ts_name] = legacy_kwargs[legacy_name]

    stop_reason = options_map.get("stopReason")
    if stop_reason is None:
        stop_reason = "stop"
    timestamp = options_map.get("timestamp")
    if timestamp is None:
        timestamp = int(time.time() * 1000)
    return AssistantMessage(
        content=_normalize_faux_assistant_content(content),
        api=DEFAULT_API,
        provider=DEFAULT_PROVIDER,
        model=DEFAULT_MODEL_ID,
        usage=_copy_default_usage(),
        stopReason=stop_reason,
        errorMessage=options_map.get("errorMessage"),
        responseId=options_map.get("responseId"),
        timestamp=timestamp,
    )


def _estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


def _random_id(prefix: str) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    value = random.random()
    suffix = ""
    while len(suffix) < 11 and value > 0:
        value *= 36
        digit = int(value)
        suffix += digits[digit]
        value -= digit
    return f"{prefix}:{int(time.time() * 1000)}:{suffix or '0'}"


def _json_stringify(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def _content_to_text(content: str | list[TextContent | ImageContent]) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(
        block.text if block.type == "text" else f"[image:{block.mimeType}:{len(block.data)}]"
        for block in content
    )


def _assistant_content_to_text(content: list[FauxContentBlock]) -> str:
    parts: list[str] = []
    for block in content:
        if block.type == "text":
            parts.append(block.text)
        elif block.type == "thinking":
            parts.append(block.thinking)
        else:
            parts.append(f"{block.name}:{_json_stringify(block.arguments)}")
    return "\n".join(parts)


def _tool_result_to_text(message: ToolResultMessage) -> str:
    return "\n".join([message.toolName, *[_content_to_text([block]) for block in message.content]])


def _message_to_text(message: MessageValue) -> str:
    if message.role == "user":
        return _content_to_text(message.content)
    if message.role == "assistant":
        return _assistant_content_to_text(message.content)
    return _tool_result_to_text(message)


def _serialize_context(context: Context) -> str:
    parts: list[str] = []
    if context.systemPrompt:
        parts.append(f"system:{context.systemPrompt}")
    for message in context.messages:
        parts.append(f"{message.role}:{_message_to_text(message)}")
    if context.tools:
        parts.append(
            "tools:"
            + _json_stringify(
                [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters_json_schema(),
                    }
                    for tool in context.tools
                ]
            )
        )
    return "\n\n".join(parts)


def _common_prefix_length(a: str, b: str) -> int:
    length = min(len(a), len(b))
    index = 0
    while index < length and a[index] == b[index]:
        index += 1
    return index


def _with_usage_estimate(
    message: AssistantMessage,
    context: Context,
    options: StreamOptions | None,
    prompt_cache: dict[str, str],
) -> AssistantMessage:
    prompt_text = _serialize_context(context)
    prompt_tokens = _estimate_tokens(prompt_text)
    output_tokens = _estimate_tokens(_assistant_content_to_text(message.content))
    input_tokens = prompt_tokens
    cache_read = 0
    cache_write = 0
    session_id = _option(options, "sessionId")

    if session_id and _option(options, "cacheRetention") != "none":
        previous_prompt = prompt_cache.get(session_id)
        if previous_prompt is not None:
            cached_chars = _common_prefix_length(previous_prompt, prompt_text)
            cache_read = _estimate_tokens(previous_prompt[:cached_chars])
            cache_write = _estimate_tokens(prompt_text[cached_chars:])
            input_tokens = max(0, prompt_tokens - cache_read)
        else:
            cache_write = prompt_tokens
        prompt_cache[session_id] = prompt_text

    return message.model_copy(
        update={
            "usage": Usage(
                input=input_tokens,
                output=output_tokens,
                cacheRead=cache_read,
                cacheWrite=cache_write,
                totalTokens=input_tokens + output_tokens + cache_read + cache_write,
                cost={"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
            )
        }
    )


def _split_string_by_token_size(text: str, min_token_size: int, max_token_size: int) -> list[str]:
    chunks: list[str] = []
    index = 0
    while index < len(text):
        token_size = min_token_size + random.randint(0, max_token_size - min_token_size)
        char_size = max(1, token_size * 4)
        chunks.append(text[index : index + char_size])
        index += char_size
    return chunks or [""]


def _clone_message(message: AssistantMessage, api: str, provider: str, model_id: str) -> AssistantMessage:
    cloned = message.model_copy(deep=True)
    return cloned.model_copy(
        update={
            "api": api,
            "provider": provider,
            "model": model_id,
            "timestamp": cloned.timestamp if cloned.timestamp is not None else int(time.time() * 1000),
            "usage": cloned.usage if cloned.usage is not None else _copy_default_usage(),
        }
    )


def _create_error_message(error: Any, api: str, provider: str, model_id: str) -> AssistantMessage:
    return AssistantMessage(
        content=[],
        api=api,
        provider=provider,
        model=model_id,
        usage=_copy_default_usage(),
        stopReason="error",
        errorMessage=str(error),
        timestamp=int(time.time() * 1000),
    )


def _create_aborted_message(partial: AssistantMessage) -> AssistantMessage:
    return partial.model_copy(
        update={
            "stopReason": "aborted",
            "errorMessage": "Request was aborted",
            "timestamp": int(time.time() * 1000),
        }
    )


async def _schedule_chunk(chunk: str, tokens_per_second: int | None) -> None:
    if not tokens_per_second or tokens_per_second <= 0:
        await asyncio.sleep(0)
        return
    delay_ms = (_estimate_tokens(chunk) / tokens_per_second) * 1000
    await asyncio.sleep(delay_ms / 1000)


def _signal_aborted(signal: Any) -> bool:
    if signal is None:
        return False
    if hasattr(signal, "aborted"):
        return bool(signal.aborted)
    if hasattr(signal, "is_set"):
        return bool(signal.is_set())
    return False


async def _stream_with_deltas(
    stream: AssistantMessageEventStream,
    message: AssistantMessage,
    min_token_size: int,
    max_token_size: int,
    tokens_per_second: int | None,
    signal: Any,
) -> None:
    partial = message.model_copy(deep=True, update={"content": []})
    if _signal_aborted(signal):
        aborted = _create_aborted_message(partial)
        stream.push(ErrorEvent(reason="aborted", error=aborted))
        stream.end(aborted)
        return

    stream.push(StartEvent(partial=partial.model_copy(deep=True)))

    for index, block in enumerate(message.content):
        if _signal_aborted(signal):
            aborted = _create_aborted_message(partial)
            stream.push(ErrorEvent(reason="aborted", error=aborted))
            stream.end(aborted)
            return

        if block.type == "thinking":
            partial.content.append(ThinkingContent(thinking=""))
            stream.push(ThinkingStartEvent(contentIndex=index, partial=partial.model_copy(deep=True)))
            for chunk in _split_string_by_token_size(block.thinking, min_token_size, max_token_size):
                await _schedule_chunk(chunk, tokens_per_second)
                if _signal_aborted(signal):
                    aborted = _create_aborted_message(partial)
                    stream.push(ErrorEvent(reason="aborted", error=aborted))
                    stream.end(aborted)
                    return
                thinking_block = partial.content[index]
                assert isinstance(thinking_block, ThinkingContent)
                thinking_block.thinking += chunk
                stream.push(
                    ThinkingDeltaEvent(contentIndex=index, delta=chunk, partial=partial.model_copy(deep=True))
                )
            stream.push(
                ThinkingEndEvent(contentIndex=index, content=block.thinking, partial=partial.model_copy(deep=True))
            )
            continue

        if block.type == "text":
            partial.content.append(TextContent(text=""))
            stream.push(TextStartEvent(contentIndex=index, partial=partial.model_copy(deep=True)))
            for chunk in _split_string_by_token_size(block.text, min_token_size, max_token_size):
                await _schedule_chunk(chunk, tokens_per_second)
                if _signal_aborted(signal):
                    aborted = _create_aborted_message(partial)
                    stream.push(ErrorEvent(reason="aborted", error=aborted))
                    stream.end(aborted)
                    return
                text_block = partial.content[index]
                assert isinstance(text_block, TextContent)
                text_block.text += chunk
                stream.push(TextDeltaEvent(contentIndex=index, delta=chunk, partial=partial.model_copy(deep=True)))
            stream.push(TextEndEvent(contentIndex=index, content=block.text, partial=partial.model_copy(deep=True)))
            continue

        partial.content.append(ToolCall(id=block.id, name=block.name, arguments={}))
        stream.push(ToolCallStartEvent(contentIndex=index, partial=partial.model_copy(deep=True)))
        for chunk in _split_string_by_token_size(_json_stringify(block.arguments), min_token_size, max_token_size):
            await _schedule_chunk(chunk, tokens_per_second)
            if _signal_aborted(signal):
                aborted = _create_aborted_message(partial)
                stream.push(ErrorEvent(reason="aborted", error=aborted))
                stream.end(aborted)
                return
            stream.push(ToolCallDeltaEvent(contentIndex=index, delta=chunk, partial=partial.model_copy(deep=True)))
        tool_block = partial.content[index]
        assert isinstance(tool_block, ToolCall)
        tool_block.arguments = block.arguments
        stream.push(ToolCallEndEvent(contentIndex=index, toolCall=block, partial=partial.model_copy(deep=True)))

    if message.stopReason in {"error", "aborted"}:
        stream.push(ErrorEvent(reason=message.stopReason, error=message))
        stream.end(message)
        return

    stream.push(DoneEvent(reason=message.stopReason, message=message))
    stream.end(message)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def register_faux_provider(options: RegisterFauxProviderOptions | dict[str, Any] | None = None) -> FauxProviderRegistration:
    options = options or {}
    api = _option(options, "api", _random_id(DEFAULT_API))
    provider = _option(options, "provider", DEFAULT_PROVIDER)
    source_id = _random_id("faux-provider")
    token_size = _option(options, "tokenSize", {})
    min_token_value = token_size.get("min") if isinstance(token_size, dict) and token_size.get("min") is not None else DEFAULT_MIN_TOKEN_SIZE
    max_token_value = token_size.get("max") if isinstance(token_size, dict) and token_size.get("max") is not None else DEFAULT_MAX_TOKEN_SIZE
    min_token_size = max(1, min(min_token_value, max_token_value))
    max_token_size = max(min_token_size, max_token_value)
    tokens_per_second = _option(options, "tokensPerSecond")
    pending_responses: list[FauxResponseStep] = []
    state = {"callCount": 0}
    prompt_cache: dict[str, str] = {}

    model_definitions = _option(options, "models") or [
        FauxModelDefinition(
            id=DEFAULT_MODEL_ID,
            name=DEFAULT_MODEL_NAME,
            reasoning=False,
            input=["text", "image"],
            cost={"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            contextWindow=128000,
            maxTokens=16384,
        )
    ]
    normalized_definitions = [
        definition if isinstance(definition, FauxModelDefinition) else FauxModelDefinition(**definition)
        for definition in model_definitions
    ]
    models = [
        Model(
            id=definition.id,
            name=definition.name if definition.name is not None else definition.id,
            api=api,
            provider=provider,
            baseUrl=DEFAULT_BASE_URL,
            reasoning=definition.reasoning if definition.reasoning is not None else False,
            input=definition.input if definition.input is not None else ["text", "image"],
            cost=definition.cost if definition.cost is not None else {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            contextWindow=definition.contextWindow if definition.contextWindow is not None else 128000,
            maxTokens=definition.maxTokens if definition.maxTokens is not None else 16384,
        )
        for definition in normalized_definitions
    ]

    def stream(request_model: Model, context: Context, stream_options: StreamOptions | None = None) -> AssistantMessageEventStream:
        outer = create_assistant_message_event_stream()
        step = pending_responses.pop(0) if pending_responses else None
        state["callCount"] += 1

        async def run() -> None:
            try:
                on_response = _option(stream_options, "onResponse")
                if callable(on_response):
                    await _maybe_await(on_response({"status": 200, "headers": {}}, request_model))

                if step is None:
                    message = _create_error_message("No more faux responses queued", api, provider, request_model.id)
                    message = _with_usage_estimate(message, context, stream_options, prompt_cache)
                    outer.push(ErrorEvent(reason="error", error=message))
                    outer.end(message)
                    return

                resolved = (
                    await _maybe_await(step(context, stream_options, state, request_model))
                    if callable(step)
                    else step
                )
                message = _clone_message(resolved, api, provider, request_model.id)
                message = _with_usage_estimate(message, context, stream_options, prompt_cache)
                await _stream_with_deltas(
                    outer,
                    message,
                    min_token_size,
                    max_token_size,
                    tokens_per_second,
                    _option(stream_options, "signal"),
                )
            except Exception as error:  # noqa: BLE001
                message = _create_error_message(error, api, provider, request_model.id)
                outer.push(ErrorEvent(reason="error", error=message))
                outer.end(message)

        asyncio.create_task(run())
        return outer

    def stream_simple(
        request_model: Model,
        context: Context,
        stream_options: SimpleStreamOptions | None = None,
    ) -> AssistantMessageEventStream:
        return stream(request_model, context, stream_options)

    register_api_provider(ApiProvider(api=api, stream=stream, streamSimple=stream_simple), source_id)

    def set_responses(responses: list[FauxResponseStep]) -> None:
        nonlocal pending_responses
        pending_responses = [*responses]

    def append_responses(responses: list[FauxResponseStep]) -> None:
        pending_responses.extend(responses)

    def pending_count() -> int:
        return len(pending_responses)

    def unregister() -> None:
        unregister_api_providers(source_id)

    return FauxProviderRegistration(
        api=api,
        models=models,
        state=state,
        _set_responses=set_responses,
        _append_responses=append_responses,
        _pending_count=pending_count,
        _unregister=unregister,
    )


__all__ = [
    "FauxModelDefinition",
    "FauxContentBlock",
    "FauxResponseFactory",
    "FauxResponseStep",
    "RegisterFauxProviderOptions",
    "FauxProviderRegistration",
    ]

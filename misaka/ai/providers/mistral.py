"""Mistral Conversations provider adapter."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterable, Mapping
from typing import Any, Literal, TypedDict

from misaka.ai.env_api_keys import get_env_api_key
from misaka.ai.models import calculate_cost, clamp_thinking_level
from misaka.ai.providers.simple_options import build_base_options
from misaka.ai.providers.transform_messages import transform_messages
from misaka.ai.types import (
    AssistantMessage,
    Context,
    DoneEvent,
    ErrorEvent,
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
    Tool,
    ToolCall,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream, spawn_stream_task
from misaka.ai.utils.hash import short_hash
from misaka.ai.utils.json_parse import parse_streaming_json
from misaka.ai.utils.sanitize_unicode import sanitize_surrogates

try:
    from mistralai.client import Mistral as _MistralClient
except ImportError:  # optional extra: misaka[mistral]
    _MistralClient = None

from misaka.ai.providers._common import (
    _empty_usage,
    _is_aborted,
    _maybe_await,
    _option,
    safe_json_stringify,
)
from misaka.ai.providers.sdk import require

MISTRAL_TOOL_CALL_ID_LENGTH = 9
MAX_MISTRAL_ERROR_BODY_CHARS = 4000
MistralReasoningEffort = Literal["none", "high"]
_STREAM_END = object()


class MistralToolChoiceFunction(TypedDict):
    name: str


class MistralToolChoiceObject(TypedDict):
    type: Literal["function"]
    function: MistralToolChoiceFunction


class MistralOptions(TypedDict, total=False):
    apiKey: str
    headers: dict[str, str]
    signal: Any
    sessionId: str
    cacheRetention: str
    onPayload: Any
    onResponse: Any
    timeoutMs: int
    maxRetries: int
    toolChoice: Literal["auto", "none", "any", "required"] | MistralToolChoiceObject
    promptMode: Literal["reasoning"]
    reasoningEffort: MistralReasoningEffort


async def _await_with_abort(request_factory: Any, signal: Any) -> Any:
    if _is_aborted(signal):
        raise RuntimeError("Request was aborted")

    request = request_factory()
    wait = getattr(signal, "wait", None)
    if not callable(wait):
        return await request

    abort_waiter = wait()
    if not hasattr(abort_waiter, "__await__"):
        return await request

    request_task = asyncio.create_task(request)
    abort_task = asyncio.create_task(abort_waiter)

    try:
        done, _ = await asyncio.wait({request_task, abort_task}, return_when=asyncio.FIRST_COMPLETED)
        if request_task in done:
            return await request_task

        request_task.cancel()
        await asyncio.gather(request_task, return_exceptions=True)
        raise RuntimeError("Request was aborted")
    finally:
        abort_task.cancel()
        await asyncio.gather(abort_task, return_exceptions=True)


def _coalesce_attr(obj: Any, *names: str) -> Any:
    for name in names:
        if isinstance(obj, Mapping):
            value = obj.get(name)
            if value is not None:
                return value
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


async def _iterate_with_abort(iterable: AsyncIterable[Any], signal: Any) -> AsyncIterable[Any]:
    iterator = aiter(iterable)

    while True:
        if _is_aborted(signal):
            raise RuntimeError("Request was aborted")

        next_item = anext(iterator, _STREAM_END)
        wait = getattr(signal, "wait", None)
        if not callable(wait):
            item = await next_item
        else:
            abort_waiter = wait()
            if not hasattr(abort_waiter, "__await__"):
                item = await next_item
            else:
                next_task = asyncio.create_task(next_item)
                abort_task = asyncio.create_task(abort_waiter)
                try:
                    done, _ = await asyncio.wait({next_task, abort_task}, return_when=asyncio.FIRST_COMPLETED)
                    if next_task in done:
                        item = await next_task
                    else:
                        next_task.cancel()
                        await asyncio.gather(next_task, return_exceptions=True)
                        raise RuntimeError("Request was aborted")
                finally:
                    abort_task.cancel()
                    await asyncio.gather(abort_task, return_exceptions=True)

        if item is _STREAM_END:
            return
        yield item


def _get_mistral_client_class():
    require(_MistralClient, "mistralai")
    return _MistralClient


def stream_mistral(
    model: Model,
    context: Context,
    options: StreamOptions | Mapping[str, Any] | None = None,
) -> AssistantMessageEventStream:
    stream = AssistantMessageEventStream()

    async def run() -> None:
        output = create_output(model)

        try:
            api_key = _option(options, "apiKey") or get_env_api_key(model.provider)
            if not api_key:
                raise RuntimeError(f"No API key for provider: {model.provider}")

            mistral = _option(options, "client")
            if mistral is None:
                mistral = create_client(model, api_key)

            normalize_tool_call_id = create_mistral_tool_call_id_normalizer()
            transformed_messages = transform_messages(
                context.messages,
                model,
                lambda tool_call_id, _target_model, _source: normalize_tool_call_id(tool_call_id),
            )

            payload = build_chat_payload(model, context, transformed_messages, options)
            on_payload = _option(options, "onPayload")
            if callable(on_payload):
                next_payload = await _maybe_await(on_payload(payload, model))
                if next_payload is not None:
                    payload = next_payload

            request_options = build_request_kwargs(model, options)
            sdk_payload = _prepare_sdk_chat_payload(payload)
            sdk_request_kwargs = _prepare_sdk_request_kwargs(request_options)
            mistral_stream = await _await_with_abort(
                lambda: _maybe_await(mistral.chat.stream_async(**sdk_payload, **sdk_request_kwargs)),
                _option(options, "signal"),
            )
            stream.push(StartEvent(partial=output))
            await consume_chat_stream(model, output, stream, mistral_stream, _option(options, "signal"))

            if _is_aborted(_option(options, "signal")):
                raise RuntimeError("Request was aborted")
            if output.stopReason in {"aborted", "error"}:
                raise RuntimeError("An unknown error occurred")

            stream.push(DoneEvent(reason=output.stopReason, message=output))
        except Exception as error:  # noqa: BLE001
            output.stopReason = "aborted" if _is_aborted(_option(options, "signal")) else "error"
            output.errorMessage = format_mistral_error(error)
            stream.push(ErrorEvent(reason=output.stopReason, error=output))
        finally:
            stream.end()

    spawn_stream_task(run())
    return stream


def stream_simple_mistral(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessageEventStream:
    api_key = _option(options, "apiKey") or get_env_api_key(model.provider)
    if not api_key:
        raise RuntimeError(f"No API key for provider: {model.provider}")

    base = build_base_options(model, options, api_key)
    clamped_reasoning = clamp_thinking_level(model, options.reasoning) if options and options.reasoning else None
    reasoning = None if clamped_reasoning == "off" else clamped_reasoning
    should_use_reasoning = bool(model.reasoning and reasoning is not None)

    return stream_mistral(
        model,
        context,
        {
            **base.model_dump(),
            "promptMode": "reasoning" if should_use_reasoning and uses_prompt_mode_reasoning(model) else None,
            "reasoningEffort": map_reasoning_effort(model, reasoning) if should_use_reasoning and uses_reasoning_effort(model) else None,
        },
    )


def create_client(model: Model, api_key: str) -> Any:
    mistral_client = _get_mistral_client_class()
    return mistral_client(api_key=api_key, server_url=model.baseUrl)


def create_output(model: Model) -> AssistantMessage:
    return AssistantMessage(
        content=[],
        api=model.api,
        provider=model.provider,
        model=model.id,
        usage=_empty_usage(),
        stopReason="stop",
        timestamp=time.time_ns() // 1_000_000,
    )


def create_mistral_tool_call_id_normalizer() -> Any:
    id_map: dict[str, str] = {}
    reverse_map: dict[str, str] = {}

    def normalize(tool_call_id: str) -> str:
        existing = id_map.get(tool_call_id)
        if existing is not None:
            return existing

        attempt = 0
        while True:
            candidate = derive_mistral_tool_call_id(tool_call_id, attempt)
            owner = reverse_map.get(candidate)
            if owner is None or owner == tool_call_id:
                id_map[tool_call_id] = candidate
                reverse_map[candidate] = tool_call_id
                return candidate
            attempt += 1

    return normalize


def derive_mistral_tool_call_id(tool_call_id: str, attempt: int) -> str:
    normalized = "".join(char for char in tool_call_id if char.isalnum())
    if attempt == 0 and len(normalized) == MISTRAL_TOOL_CALL_ID_LENGTH:
        return normalized

    seed_base = normalized or tool_call_id
    seed = seed_base if attempt == 0 else f"{seed_base}:{attempt}"
    return "".join(char for char in short_hash(seed) if char.isalnum())[:MISTRAL_TOOL_CALL_ID_LENGTH]


def format_mistral_error(error: Any) -> str:
    if isinstance(error, Exception):
        status_code = _coalesce_attr(error, "status_code", "statusCode")
        body_text = _coalesce_attr(error, "body")
        if isinstance(status_code, int) and isinstance(body_text, str) and body_text.strip():
            return (
                f"Mistral API error ({status_code}): "
                f"{truncate_error_text(body_text.strip(), MAX_MISTRAL_ERROR_BODY_CHARS)}"
            )
        if isinstance(status_code, int):
            return f"Mistral API error ({status_code}): {error}"
        return str(error)
    return safe_json_stringify(error)


def truncate_error_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}... [truncated {len(text) - max_chars} chars]"


def build_request_kwargs(model: Model, options: StreamOptions | Mapping[str, Any] | None = None) -> dict[str, Any]:
    request_kwargs: dict[str, Any] = {
        "retries": {"strategy": "none"},
    }
    if _option(options, "signal") is not None:
        request_kwargs["signal"] = _option(options, "signal")
    headers: dict[str, str] = {}
    if model.headers:
        headers.update(model.headers)
    if _option(options, "headers"):
        headers.update(_option(options, "headers"))
    if _option(options, "sessionId") and "x-affinity" not in headers:
        headers["x-affinity"] = _option(options, "sessionId")
    if headers:
        request_kwargs["headers"] = headers
    return request_kwargs


def build_chat_payload(
    model: Model,
    context: Context,
    messages: list[MessageValue],
    options: StreamOptions | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model.id,
        "stream": True,
        "messages": to_chat_messages(messages, "image" in model.input),
    }

    if context.tools:
        payload["tools"] = to_function_tools(context.tools)
    if _option(options, "temperature") is not None:
        payload["temperature"] = _option(options, "temperature")
    if _option(options, "maxTokens") is not None:
        payload["maxTokens"] = _option(options, "maxTokens")
    tool_choice = map_tool_choice(_option(options, "toolChoice"))
    if tool_choice is not None:
        payload["toolChoice"] = tool_choice
    if _option(options, "promptMode") is not None:
        payload["promptMode"] = _option(options, "promptMode")
    if _option(options, "reasoningEffort") is not None:
        payload["reasoningEffort"] = _option(options, "reasoningEffort")

    if context.systemPrompt:
        payload["messages"].insert(
            0,
            {
                "role": "system",
                "content": sanitize_surrogates(context.systemPrompt),
            },
        )

    return payload


def _prepare_sdk_request_kwargs(request_options: Mapping[str, Any]) -> dict[str, Any]:
    sdk_request_kwargs: dict[str, Any] = {}
    headers = request_options.get("headers")
    if headers is not None:
        sdk_request_kwargs["http_headers"] = dict(headers)
    signal = request_options.get("signal")
    if signal is not None:
        sdk_request_kwargs["signal"] = signal
    retries = request_options.get("retries")
    if retries is not None:
        sdk_request_kwargs["retries"] = retries
    return sdk_request_kwargs


def _prepare_sdk_chat_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    sdk_payload: dict[str, Any] = {
        "model": payload["model"],
        "stream": payload["stream"],
        "messages": [_prepare_sdk_chat_message(message) for message in payload["messages"]],
    }
    if "tools" in payload:
        sdk_payload["tools"] = payload["tools"]
    if "temperature" in payload:
        sdk_payload["temperature"] = payload["temperature"]
    if "maxTokens" in payload:
        sdk_payload["max_tokens"] = payload["maxTokens"]
    if "toolChoice" in payload:
        sdk_payload["tool_choice"] = payload["toolChoice"]
    if "promptMode" in payload:
        sdk_payload["prompt_mode"] = payload["promptMode"]
    if "reasoningEffort" in payload:
        sdk_payload["reasoning_effort"] = payload["reasoningEffort"]
    return sdk_payload


def _prepare_sdk_chat_message(message: Mapping[str, Any]) -> dict[str, Any]:
    sdk_message: dict[str, Any] = {"role": message["role"]}
    if "content" in message:
        content = message["content"]
        if isinstance(content, list):
            sdk_message["content"] = [_prepare_sdk_content_chunk(item) for item in content]
        else:
            sdk_message["content"] = content
    if "toolCalls" in message:
        sdk_message["tool_calls"] = [
            {
                "id": tool_call["id"],
                "type": tool_call["type"],
                "function": dict(tool_call["function"]),
            }
            for tool_call in message["toolCalls"]
        ]
    if "toolCallId" in message:
        sdk_message["tool_call_id"] = message["toolCallId"]
    if "name" in message:
        sdk_message["name"] = message["name"]
    return sdk_message


def _prepare_sdk_content_chunk(item: Mapping[str, Any]) -> dict[str, Any]:
    if item["type"] == "image_url" and "imageUrl" in item:
        return {"type": "image_url", "image_url": item["imageUrl"]}
    return dict(item)


async def consume_chat_stream(
    model: Model,
    output: AssistantMessage,
    stream: AssistantMessageEventStream,
    mistral_stream: AsyncIterable[Any],
    signal: Any = None,
) -> None:
    current_block: TextContent | ThinkingContent | None = None
    tool_blocks_by_key: dict[str, int] = {}
    partial_args_by_index: dict[int, str] = {}

    def block_index() -> int:
        return len(output.content) - 1

    def finish_current_block(block: TextContent | ThinkingContent | None) -> None:
        if block is None:
            return
        if block.type == "text":
            stream.push(TextEndEvent(contentIndex=block_index(), content=block.text, partial=output))
        else:
            stream.push(ThinkingEndEvent(contentIndex=block_index(), content=block.thinking, partial=output))

    async for event in _iterate_with_abort(mistral_stream, signal):
        chunk = _coalesce_attr(event, "data") or event
        output.responseId = output.responseId or _coalesce_attr(chunk, "id")

        usage = _coalesce_attr(chunk, "usage")
        if usage is not None:
            output.usage.input = int(_coalesce_attr(usage, "prompt_tokens", "promptTokens") or 0)
            output.usage.output = int(_coalesce_attr(usage, "completion_tokens", "completionTokens") or 0)
            output.usage.cacheRead = 0
            output.usage.cacheWrite = 0
            output.usage.totalTokens = int(
                _coalesce_attr(usage, "total_tokens", "totalTokens") or (output.usage.input + output.usage.output)
            )
            calculate_cost(model, output.usage)

        choices = _coalesce_attr(chunk, "choices") or []
        choice = choices[0] if choices else None
        if choice is None:
            continue

        finish_reason = _coalesce_attr(choice, "finish_reason", "finishReason")
        if finish_reason is not None:
            output.stopReason = map_chat_stop_reason(finish_reason)

        delta = _coalesce_attr(choice, "delta")
        if delta is None:
            continue

        delta_content = _coalesce_attr(delta, "content")
        if delta_content is not None:
            content_items = [delta_content] if isinstance(delta_content, str) else list(delta_content)
            for item in content_items:
                if isinstance(item, str):
                    text_delta = sanitize_surrogates(item)
                    if current_block is None or current_block.type != "text":
                        finish_current_block(current_block)
                        current_block = TextContent(text="")
                        output.content.append(current_block)
                        stream.push(TextStartEvent(contentIndex=block_index(), partial=output))
                    current_block.text += text_delta
                    stream.push(TextDeltaEvent(contentIndex=block_index(), delta=text_delta, partial=output))
                    continue

                item_type = item.get("type") if isinstance(item, dict) else _coalesce_attr(item, "type")
                if item_type == "thinking":
                    raw_thinking = item.get("thinking") if isinstance(item, dict) else _coalesce_attr(item, "thinking") or []
                    thinking_delta = sanitize_surrogates(
                        "".join(
                            part.get("text", "") if isinstance(part, dict) else str(_coalesce_attr(part, "text") or "")
                            for part in raw_thinking
                        )
                    )
                    if not thinking_delta:
                        continue
                    if current_block is None or current_block.type != "thinking":
                        finish_current_block(current_block)
                        current_block = ThinkingContent(thinking="")
                        output.content.append(current_block)
                        stream.push(ThinkingStartEvent(contentIndex=block_index(), partial=output))
                    current_block.thinking += thinking_delta
                    stream.push(ThinkingDeltaEvent(contentIndex=block_index(), delta=thinking_delta, partial=output))
                    continue

                if item_type == "text":
                    text_value = item.get("text") if isinstance(item, dict) else _coalesce_attr(item, "text")
                    text_delta = sanitize_surrogates(str(text_value or ""))
                    if current_block is None or current_block.type != "text":
                        finish_current_block(current_block)
                        current_block = TextContent(text="")
                        output.content.append(current_block)
                        stream.push(TextStartEvent(contentIndex=block_index(), partial=output))
                    current_block.text += text_delta
                    stream.push(TextDeltaEvent(contentIndex=block_index(), delta=text_delta, partial=output))

        tool_calls = _coalesce_attr(delta, "tool_calls", "toolCalls") or []
        for tool_call in tool_calls:
            if current_block is not None:
                finish_current_block(current_block)
                current_block = None

            tool_call_index = int(_coalesce_attr(tool_call, "index") or 0)
            tool_call_id = _coalesce_attr(tool_call, "id")
            if not tool_call_id or tool_call_id == "null":
                tool_call_id = derive_mistral_tool_call_id(f"toolcall:{tool_call_index}", 0)
            key = f"{tool_call_id}:{tool_call_index}"
            existing_index = tool_blocks_by_key.get(key)

            block: ToolCall
            if existing_index is not None and output.content[existing_index].type == "toolCall":
                block = output.content[existing_index]
            else:
                function = _coalesce_attr(tool_call, "function") or {}
                block = ToolCall(
                    id=tool_call_id,
                    name=function.get("name") if isinstance(function, dict) else str(_coalesce_attr(function, "name") or ""),
                    arguments={},
                )
                output.content.append(block)
                existing_index = len(output.content) - 1
                tool_blocks_by_key[key] = existing_index
                stream.push(ToolCallStartEvent(contentIndex=existing_index, partial=output))

            function = _coalesce_attr(tool_call, "function") or {}
            raw_arguments = function.get("arguments") if isinstance(function, dict) else _coalesce_attr(function, "arguments")
            args_delta = raw_arguments if isinstance(raw_arguments, str) else json.dumps(raw_arguments or {})
            partial_args_by_index[existing_index] = partial_args_by_index.get(existing_index, "") + args_delta
            block.arguments = parse_streaming_json(partial_args_by_index[existing_index])
            stream.push(
                ToolCallDeltaEvent(
                    contentIndex=existing_index,
                    delta=args_delta,
                    partial=output,
                )
            )

    finish_current_block(current_block)
    for index in tool_blocks_by_key.values():
        block = output.content[index]
        if block.type != "toolCall":
            continue
        block.arguments = parse_streaming_json(partial_args_by_index.get(index, ""))
        stream.push(ToolCallEndEvent(contentIndex=index, toolCall=block, partial=output))


def to_function_tools(tools: list[Tool]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": strip_symbol_keys(tool.parameters_json_schema()),
                "strict": False,
            },
        }
        for tool in tools
    ]


def strip_symbol_keys(value: Any) -> Any:
    if isinstance(value, list):
        return [strip_symbol_keys(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): strip_symbol_keys(entry) for key, entry in value.items()}
    return value


def to_chat_messages(messages: list[MessageValue], supports_images: bool) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    for message in messages:
        if message.role == "user":
            if isinstance(message.content, str):
                result.append({"role": "user", "content": sanitize_surrogates(message.content)})
                continue

            had_images = any(item.type == "image" for item in message.content)
            content = []
            for item in message.content:
                if item.type == "text":
                    content.append({"type": "text", "text": sanitize_surrogates(item.text)})
                elif supports_images:
                    content.append({"type": "image_url", "imageUrl": f"data:{item.mimeType};base64,{item.data}"})
            if content:
                result.append({"role": "user", "content": content})
                continue
            if had_images and not supports_images:
                result.append({"role": "user", "content": "(image omitted: model does not support images)"})
            continue

        if message.role == "assistant":
            content_parts: list[dict[str, Any]] = []
            tool_calls: list[dict[str, Any]] = []

            for block in message.content:
                if block.type == "text" and block.text.strip():
                    content_parts.append({"type": "text", "text": sanitize_surrogates(block.text)})
                    continue
                if block.type == "thinking" and block.thinking.strip():
                    content_parts.append(
                        {
                            "type": "thinking",
                            "thinking": [{"type": "text", "text": sanitize_surrogates(block.thinking)}],
                        }
                    )
                    continue
                if block.type == "toolCall":
                    tool_calls.append(
                        {
                            "id": block.id,
                            "type": "function",
                            "function": {
                                "name": block.name,
                                "arguments": json.dumps(block.arguments or {}),
                            },
                        }
                    )

            assistant_message: dict[str, Any] = {"role": "assistant"}
            if content_parts:
                assistant_message["content"] = content_parts
            if tool_calls:
                assistant_message["toolCalls"] = tool_calls
            if content_parts or tool_calls:
                result.append(assistant_message)
            continue

        text_result = "\n".join(part.text for part in message.content if part.type == "text")
        has_images = any(part.type == "image" for part in message.content)
        tool_content = [
            {
                "type": "text",
                "text": build_tool_result_text(text_result, has_images, supports_images, message.isError),
            }
        ]
        for part in message.content:
            if supports_images and part.type == "image":
                tool_content.append({"type": "image_url", "imageUrl": f"data:{part.mimeType};base64,{part.data}"})
        result.append(
            {
                "role": "tool",
                "toolCallId": message.toolCallId,
                "name": message.toolName,
                "content": tool_content,
            }
        )

    return result


def build_tool_result_text(text: str, has_images: bool, supports_images: bool, is_error: bool) -> str:
    trimmed = text.strip()
    error_prefix = "[tool error] " if is_error else ""

    if trimmed:
        image_suffix = "\n[tool image omitted: model does not support images]" if has_images and not supports_images else ""
        return f"{error_prefix}{trimmed}{image_suffix}"
    if has_images:
        if supports_images:
            return "[tool error] (see attached image)" if is_error else "(see attached image)"
        return (
            "[tool error] (image omitted: model does not support images)"
            if is_error
            else "(image omitted: model does not support images)"
        )
    return "[tool error] (no tool output)" if is_error else "(no tool output)"


def uses_reasoning_effort(model: Model) -> bool:
    return model.id in {"mistral-small-2603", "mistral-small-latest", "mistral-medium-3.5"}


def uses_prompt_mode_reasoning(model: Model) -> bool:
    return bool(model.reasoning and not uses_reasoning_effort(model))


def map_reasoning_effort(model: Model, level: str | None) -> MistralReasoningEffort:
    if level is None:
        return "high"
    mapped = (model.thinkingLevelMap or {}).get(level)
    return "high" if mapped is None else mapped  # type: ignore[return-value]


def map_tool_choice(choice: Any) -> Any:
    if not choice:
        return None
    if choice in {"auto", "none", "any", "required"}:
        return choice
    function = choice.get("function") if isinstance(choice, dict) else getattr(choice, "function", None)
    function_name = function.get("name") if isinstance(function, dict) else getattr(function, "name", None)
    return {"type": "function", "function": {"name": function_name}}


def map_chat_stop_reason(reason: str | None) -> StopReason:
    if reason is None:
        return "stop"
    if reason == "stop":
        return "stop"
    if reason in {"length", "model_length"}:
        return "length"
    if reason == "tool_calls":
        return "toolUse"
    if reason == "error":
        return "error"
    return "stop"


streamMistral = stream_mistral
streamSimpleMistral = stream_simple_mistral
__all__ = [
    "MistralOptions",
    "streamMistral",
    "streamSimpleMistral",
]

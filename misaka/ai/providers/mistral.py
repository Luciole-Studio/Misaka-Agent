"""Mistral Conversations provider adapter."""

from __future__ import annotations

import inspect
import json
import math
import re
import time
from collections.abc import AsyncIterable, Mapping
from functools import lru_cache
from typing import Any, Literal, TypedDict, cast

from misaka.ai.env_api_keys import get_env_api_key
from misaka.ai.models import calculate_cost, clamp_thinking_level
from misaka.ai.providers.simple_options import build_base_options
from misaka.ai.providers.transform_messages import transform_messages
from misaka.ai.types import (
    AssistantMessage,
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
    TranscriptContext,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream, spawn_stream_task
from misaka.ai.utils.hash import short_hash
from misaka.ai.utils.json_parse import StreamingArgs
from misaka.ai.utils.sanitize_unicode import sanitize_surrogates
from misaka.ai.utils.text import get_system_message_text, render_system_message_update
from misaka.ai.utils.transcript import get_current_tools, resolve_transcript

try:
    from mistralai.client import Mistral as _MistralClient
except ImportError:  # optional extra: misaka[mistral]
    _MistralClient = None

from misaka.ai.providers._common import (
    _await_with_signal,
    _empty_usage,
    _iterate_async_iterable,
    _option,
    safe_json_stringify,
)
from misaka.ai.providers.constrained_sampling import (
    get_json_schema_tool_parameters,
    resolve_json_schema_strict_sampling,
)
from misaka.ai.providers.sdk import require
from misaka.ai.utils.headers import apply_provider_headers, headers_to_record
from misaka.ai.utils.user_agent import get_misaka_user_agent
from misaka.utils.values import maybe_await, signal_aborted

MISTRAL_TOOL_CALL_ID_LENGTH = 9
_MISTRAL_ID_DISALLOWED = re.compile(r"[^a-zA-Z0-9]")
MAX_MISTRAL_ERROR_BODY_CHARS = 4000
MistralReasoningEffort = Literal["none", "high"]


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


def _get_mistral_client_class():
    require(_MistralClient, "mistralai")
    return _MistralClient


def stream_mistral(
    model: Model,
    context: TranscriptContext,
    options: StreamOptions | Mapping[str, Any] | None = None,
) -> AssistantMessageEventStream:
    stream = AssistantMessageEventStream()
    normalized_context = resolve_transcript(
        context, getattr(getattr(model, "compat", None), "supportsMidConvoSystemMessages", None)
    )

    async def run() -> None:
        output = create_output(model)

        try:
            api_key = _option(options, "apiKey") or get_env_api_key(model.provider)
            if not api_key:
                raise RuntimeError(f"No API key for provider: {model.provider}")

            mistral = _option(options, "client")
            if mistral is None:
                mistral = create_client(model, api_key, _option(options, "timeoutMs"))

            normalize_tool_call_id = create_mistral_tool_call_id_normalizer()
            transformed_messages = transform_messages(
                normalized_context.messages,
                model,
                lambda tool_call_id, _target_model, _source: normalize_tool_call_id(tool_call_id),
            )

            payload = build_chat_payload(model, normalized_context, transformed_messages, options)
            on_payload = _option(options, "onPayload")
            if callable(on_payload):
                next_payload = await maybe_await(on_payload(payload, model))
                if next_payload is not None:
                    payload = next_payload

            request_options = build_request_kwargs(model, options)
            sdk_payload = _prepare_sdk_chat_payload(payload)
            sdk_request_kwargs = _prepare_sdk_request_kwargs(request_options)
            mistral_stream = await _await_with_signal(
                maybe_await(mistral.chat.stream_async(**sdk_payload, **sdk_request_kwargs)),
                _option(options, "signal"),
            )
            # The SDK's EventStreamAsync keeps the httpx.Response it was opened from;
            # that is where the status and the gateway's headers live.
            on_response = _option(options, "onResponse")
            http_response = getattr(mistral_stream, "response", None)
            if callable(on_response) and http_response is not None:
                status = getattr(http_response, "status_code", None)
                if status is not None:
                    await maybe_await(
                        on_response(
                            {"status": int(status), "headers": headers_to_record(getattr(http_response, "headers", None) or {})},
                            model,
                        )
                    )
            stream.push(StartEvent(partial=output))
            saw_finish_reason = await consume_chat_stream(
                model, output, stream, mistral_stream, _option(options, "signal")
            )

            if signal_aborted(_option(options, "signal")):
                raise RuntimeError("Request was aborted")
            # Upstream's "pending" initial stopReason, expressed as a flag: without it a
            # server-side close or a gateway truncation that raised no httpx error left
            # the constructor's "stop" in place and half a reply was pushed as a DoneEvent.
            if not saw_finish_reason:
                raise RuntimeError("Mistral stream ended without a finish reason")
            if output.stopReason in {"aborted", "error"}:
                raise RuntimeError(output.errorMessage or "An unknown error occurred")

            stream.push(DoneEvent(reason=output.stopReason, message=output))
        except Exception as error:  # noqa: BLE001
            output.stopReason = "aborted" if signal_aborted(_option(options, "signal")) else "error"
            output.errorMessage = format_mistral_error(error)
            stream.push(ErrorEvent(reason=output.stopReason, error=output), cause=error)
        finally:
            stream.end()

    spawn_stream_task(run(), stream=stream)
    return stream


def stream_simple_mistral(
    model: Model,
    context: TranscriptContext,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessageEventStream:
    api_key = _option(options, "apiKey") or get_env_api_key(model.provider)
    if not api_key:
        raise RuntimeError(f"No API key for provider: {model.provider}")

    base = build_base_options(model, context, options, api_key)
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


def create_client(model: Model, api_key: str, timeout_ms: int | None = None) -> Any:
    mistral_client = _get_mistral_client_class()
    client_options: dict[str, Any] = {
        "api_key": api_key,
        "server_url": model.baseUrl,
    }
    if timeout_ms is not None:
        client_options["timeout_ms"] = timeout_ms
    return mistral_client(**client_options)


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
    # Mistral's ids are `^[a-zA-Z0-9]{9}$`. `str.isalnum` is true for CJK and accented
    # letters too, so a 9-character id like "abc工12345" used to pass through whole and be
    # rejected by the API; pi strips with `[^a-zA-Z0-9]` and hashes what is left.
    normalized = _MISTRAL_ID_DISALLOWED.sub("", tool_call_id)
    if attempt == 0 and len(normalized) == MISTRAL_TOOL_CALL_ID_LENGTH:
        return normalized

    seed_base = normalized or tool_call_id
    seed = seed_base if attempt == 0 else f"{seed_base}:{attempt}"
    return _MISTRAL_ID_DISALLOWED.sub("", short_hash(seed))[:MISTRAL_TOOL_CALL_ID_LENGTH]


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


def _has_header_override(overrides: Mapping[str, Any] | None, target: str) -> bool:
    """Whether this layer named ``target`` at all -- setting it or deleting it."""
    return bool(overrides) and any(str(name).lower() == target for name in overrides)


def _should_use_prompt_caching(options: Any) -> bool:
    return _option(options, "cacheRetention") != "none" and bool(_option(options, "sessionId"))


def build_request_kwargs(model: Model, options: StreamOptions | Mapping[str, Any] | None = None) -> dict[str, Any]:
    request_kwargs: dict[str, Any] = {
        "retries": {"strategy": "none"},
    }
    headers: dict[str, str] = {"User-Agent": get_misaka_user_agent()}
    model_headers = model.headers
    option_headers = _option(options, "headers")
    apply_provider_headers(headers, model_headers)
    apply_provider_headers(headers, option_headers)

    # Affinity is only added when neither side spoke about it -- including a side that
    # *deleted* it. Testing the merged dict instead would re-add the header a caller had
    # just removed, because the deletion leaves no trace to find.
    has_explicit_affinity = _has_header_override(model_headers, "x-affinity") or _has_header_override(
        option_headers, "x-affinity"
    )
    if _should_use_prompt_caching(options) and not has_explicit_affinity:
        headers["x-affinity"] = _option(options, "sessionId")
    if headers:
        request_kwargs["headers"] = headers
    return request_kwargs


def build_chat_payload(
    model: Model,
    context: TranscriptContext,
    messages: list[MessageValue],
    options: StreamOptions | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model.id,
        "stream": True,
        "messages": to_chat_messages(messages, "image" in model.input),
    }

    current_tools = get_current_tools(context.messages)
    if len(current_tools) > 0:
        payload["tools"] = to_function_tools(current_tools)
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
    # The x-affinity header routes the request to the machine holding the cache;
    # promptCacheKey is what makes the cache exist at all. build_request_kwargs only set
    # the former, so caching was requested and never obtained.
    if _should_use_prompt_caching(options):
        payload["promptCacheKey"] = _option(options, "sessionId")

    return payload


def _prepare_sdk_request_kwargs(request_options: Mapping[str, Any]) -> dict[str, Any]:
    """Only what ``Chat.stream_async`` declares: it is generated code with a fixed keyword
    list and no ``**kwargs``, so anything extra -- an abort signal above all, which the
    agent loop always supplies -- is a TypeError raised before the request is sent.
    Abort is handled here by ``_common``'s ``_await_with_signal`` / ``_iterate_async_iterable``
    instead.
    """
    sdk_request_kwargs: dict[str, Any] = {}
    headers = request_options.get("headers")
    if headers is not None:
        sdk_request_kwargs["http_headers"] = dict(headers)
    retries = request_options.get("retries")
    if retries is not None:
        sdk_request_kwargs["retries"] = retries
    return sdk_request_kwargs


# The names the payload is built under, and what Mistral calls them on the wire. Upstream
# builds its payload in the SDK's camelCase shape and renames on the way out; this is the
# same table, applied at the same point.
_MISTRAL_WIRE_KEYS = (
    ("topP", "top_p"),
    ("maxTokens", "max_tokens"),
    ("randomSeed", "random_seed"),
    ("responseFormat", "response_format"),
    ("toolChoice", "tool_choice"),
    ("presencePenalty", "presence_penalty"),
    ("frequencyPenalty", "frequency_penalty"),
    ("parallelToolCalls", "parallel_tool_calls"),
    ("reasoningEffort", "reasoning_effort"),
    ("promptMode", "prompt_mode"),
    ("promptCacheKey", "prompt_cache_key"),
    ("safePrompt", "safe_prompt"),
)
_MISTRAL_WIRE_CHUNK_KEYS = (
    ("imageUrl", "image_url"),
    ("documentUrl", "document_url"),
    ("documentName", "document_name"),
    ("fileId", "file_id"),
    ("referenceIds", "reference_ids"),
    ("inputAudio", "input_audio"),
)
_MISTRAL_WIRE_MESSAGE_KEYS = (
    ("toolCalls", "tool_calls"),
    ("toolCallId", "tool_call_id"),
)


def get_mistral_cached_prompt_tokens(usage: Any, prompt_tokens: int) -> int:
    """How many of the prompt tokens Mistral served from its cache.

    Six spellings because the field has moved and the SDK and the raw wire disagree on
    case; upstream reads all six. Getting this wrong is not cosmetic -- ``cacheRead`` is
    what the cost calculation bills at the cached rate, so a hardcoded zero charges every
    cached prompt at full price. It cannot exceed the prompt itself.
    """
    for holder, field in (
        ("promptTokensDetails", "cachedTokens"),
        ("prompt_tokens_details", "cached_tokens"),
        ("promptTokenDetails", "cachedTokens"),
        ("prompt_token_details", "cached_tokens"),
    ):
        details = _coalesce_attr(usage, holder)
        if details is None:
            continue
        raw = _coalesce_attr(details, field)
        if raw is not None:
            break
    else:
        raw = _coalesce_attr(usage, "numCachedTokens", "num_cached_tokens")

    if not isinstance(raw, (int, float)) or isinstance(raw, bool) or not math.isfinite(raw):
        return 0
    return min(prompt_tokens, max(0, int(raw)))


@lru_cache(maxsize=1)
def _sdk_chat_stream_fields() -> frozenset[str]:
    """What the SDK's generated ``stream_async`` will accept as a keyword.

    Upstream POSTs the payload as JSON and every field reaches Mistral untouched. Here it
    goes through a generated method with a fixed keyword list, so a field it does not
    declare is a ``TypeError`` raised before the request is sent rather than something
    the server can ignore. Asking the SDK what it takes keeps the passthrough as wide as
    the SDK allows instead of as wide as a hand-written list happened to be.
    """
    try:
        # `chat` is bound on the instance, not the class, so the method has to come from
        # the module that defines it rather than from the client we would construct.
        from mistralai.client.chat import Chat

        return frozenset(inspect.signature(Chat.stream_async).parameters) - {"self"}
    except Exception:  # noqa: BLE001
        # No SDK, or a version that moved the method: fall back to sending everything and
        # let the SDK complain, which is what upstream's untyped POST does anyway.
        return frozenset()


def _remap_mistral_property(record: dict[str, Any], source: str, target: str) -> None:
    if source not in record:
        return
    record[target] = record.pop(source)


def _prepare_sdk_chat_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    # Everything the caller put on the payload, not a hand-picked six: `onPayload` exists
    # so a call site can set a field the builder does not, and an allowlist here silently
    # dropped every one of them.
    sdk_payload: dict[str, Any] = dict(payload)
    for source, target in _MISTRAL_WIRE_KEYS:
        _remap_mistral_property(sdk_payload, source, target)
    sdk_payload["messages"] = [_prepare_sdk_chat_message(message) for message in payload["messages"]]

    response_format = sdk_payload.get("response_format")
    if isinstance(response_format, Mapping):
        wire_response_format = dict(response_format)
        _remap_mistral_property(wire_response_format, "jsonSchema", "json_schema")
        json_schema = wire_response_format.get("json_schema")
        if isinstance(json_schema, Mapping):
            wire_json_schema = dict(json_schema)
            _remap_mistral_property(wire_json_schema, "schemaDefinition", "schema")
            wire_response_format["json_schema"] = wire_json_schema
        sdk_payload["response_format"] = wire_response_format

    accepted = _sdk_chat_stream_fields()
    if accepted:
        sdk_payload = {key: value for key, value in sdk_payload.items() if key in accepted}
    return sdk_payload


def _prepare_sdk_chat_message(message: Mapping[str, Any]) -> dict[str, Any]:
    sdk_message: dict[str, Any] = {"role": message["role"]}
    if "content" in message:
        content = message["content"]
        if isinstance(content, list):
            sdk_message["content"] = [_prepare_sdk_content_chunk(item) for item in content]
        else:
            sdk_message["content"] = content
    for key, value in message.items():
        if key not in ("role", "content"):
            sdk_message[key] = value
    for source, target in _MISTRAL_WIRE_MESSAGE_KEYS:
        _remap_mistral_property(sdk_message, source, target)
    if "tool_calls" in sdk_message:
        sdk_message["tool_calls"] = [
            {
                "id": tool_call["id"],
                "type": tool_call["type"],
                "function": dict(tool_call["function"]),
            }
            for tool_call in sdk_message["tool_calls"]
        ]
    return sdk_message


def _prepare_sdk_content_chunk(item: Mapping[str, Any]) -> dict[str, Any]:
    chunk = dict(item)
    for source, target in _MISTRAL_WIRE_CHUNK_KEYS:
        _remap_mistral_property(chunk, source, target)
    return chunk


async def consume_chat_stream(
    model: Model,
    output: AssistantMessage,
    stream: AssistantMessageEventStream,
    mistral_stream: AsyncIterable[Any],
    signal: Any = None,
) -> bool:
    """Drain the SDK stream into ``output``; returns whether a ``finish_reason`` arrived.

    The caller needs that answer: ``create_output`` starts at ``"stop"`` (misaka's
    StopReason has no ``"pending"``), so a stream that ends before any finish_reason is
    otherwise indistinguishable from a model that finished speaking.
    """
    current_block: TextContent | ThinkingContent | None = None
    tool_blocks_by_key: dict[str, int] = {}
    partial_args_by_index: dict[int, StreamingArgs] = {}
    saw_finish_reason = False

    def block_index() -> int:
        return len(output.content) - 1

    def finish_current_block(block: TextContent | ThinkingContent | None) -> None:
        if block is None:
            return
        if block.type == "text":
            stream.push(TextEndEvent(contentIndex=block_index(), content=block.text, partial=output))
        else:
            stream.push(ThinkingEndEvent(contentIndex=block_index(), content=block.thinking, partial=output))

    async for event in _iterate_async_iterable(mistral_stream, signal):
        chunk = _coalesce_attr(event, "data") or event
        output.responseId = output.responseId or _coalesce_attr(chunk, "id")

        usage = _coalesce_attr(chunk, "usage")
        if usage is not None:
            prompt_tokens = int(_coalesce_attr(usage, "prompt_tokens", "promptTokens") or 0)
            cached_prompt_tokens = get_mistral_cached_prompt_tokens(usage, prompt_tokens)
            output.usage.input = max(0, prompt_tokens - cached_prompt_tokens)
            output.usage.output = int(_coalesce_attr(usage, "completion_tokens", "completionTokens") or 0)
            output.usage.cacheRead = cached_prompt_tokens
            output.usage.cacheWrite = 0
            output.usage.totalTokens = int(
                _coalesce_attr(usage, "total_tokens", "totalTokens")
                or (output.usage.input + output.usage.output + output.usage.cacheRead + output.usage.cacheWrite)
            )
            calculate_cost(model, output.usage)

        choices = _coalesce_attr(chunk, "choices") or []
        choice = choices[0] if choices else None
        if choice is None:
            continue

        finish_reason = _coalesce_attr(choice, "finish_reason", "finishReason")
        if finish_reason is not None:
            saw_finish_reason = True
            stop_result = map_chat_stop_reason(finish_reason)
            output.stopReason = cast(StopReason, stop_result["stopReason"])
            if stop_result.get("errorMessage"):
                output.errorMessage = stop_result["errorMessage"]

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
            accumulated = partial_args_by_index.setdefault(existing_index, StreamingArgs())
            accumulated.append(args_delta)
            block.arguments = accumulated.arguments
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
        # Same guard as the anthropic adapter's content_block_stop (F4): only a buffer
        # that actually received something may overwrite what the tool call was born with.
        accumulated = partial_args_by_index.get(index)
        if accumulated is not None and accumulated.raw:
            accumulated.finish_into(block)
        stream.push(ToolCallEndEvent(contentIndex=index, toolCall=block, partial=output))

    return saw_finish_reason


def to_function_tools(tools: list[Tool]) -> list[dict[str, Any]]:
    def entry(tool: Tool) -> dict[str, Any]:
        # The `True` is upstream's second argument to the resolver -- "this API always
        # accepts a strict schema" -- not the wire field. The same answer has to reach
        # both: a strictified schema shipped with `strict: false` asks Mistral to ignore
        # exactly the constrained sampling the tool declared.
        strict = resolve_json_schema_strict_sampling(tool, True)
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": strip_symbol_keys(get_json_schema_tool_parameters(tool, strict)),
                "strict": strict is True,
            },
        }

    return [entry(tool) for tool in tools]


def strip_symbol_keys(value: Any) -> Any:
    if isinstance(value, list):
        return [strip_symbol_keys(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): strip_symbol_keys(entry) for key, entry in value.items()}
    return value


def to_chat_messages(messages: list[MessageValue], supports_images: bool) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    for index, message in enumerate(messages):
        if message.role == "system":
            text = get_system_message_text(message) if index == 0 else render_system_message_update(message)
            if len(text) > 0:
                result.append({"role": "system", "content": sanitize_surrogates(text)})
            continue
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

        # Sanitized like every other text on this payload: a lone surrogate anywhere in a
        # tool result makes pydantic's model_dump_json raise inside the SDK, which fails
        # not just this turn but every later turn that still carries it in history.
        text_result = "\n".join(sanitize_surrogates(part.text) for part in message.content if part.type == "text")
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
    return (
        model.id == "mistral-small-2603"
        or model.id == "mistral-small-latest"
        or model.id.startswith("mistral-medium-")
        or model.id == "zai-glm-5-2"
    )


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


def map_chat_stop_reason(reason: str | None) -> dict[str, str]:
    """Map Mistral's ``finish_reason``, keeping the raw value on unknowns.

    Same ``{"stopReason", "errorMessage"?}`` shape as openai-completions' map_stop_reason.
    A new or unrecognised finish reason used to become a plain ``"stop"``, so a filtered
    or aborted turn read to the agent loop as a model that had finished speaking.
    """
    if reason is None:
        return {"stopReason": "stop"}
    if reason == "stop":
        return {"stopReason": "stop"}
    if reason in {"length", "model_length"}:
        return {"stopReason": "length"}
    if reason == "tool_calls":
        return {"stopReason": "toolUse"}
    return {"stopReason": "error", "errorMessage": f"Provider stopped with: {reason}"}


streamMistral = stream_mistral
streamSimpleMistral = stream_simple_mistral
__all__ = [
    "MistralOptions",
    "streamMistral",
    "streamSimpleMistral",
]

"""Proxy stream function for apps that route model calls through a server.

This is the Python port of Pi's ``packages/agent/src/proxy.ts``.  The server owns
provider authentication and sends Pi assistant-message events with ``partial`` removed;
this module reconstructs the partial message locally.

Two Python runtime details are deliberate:

* ``httpTransport`` is the ``httpx`` equivalent of replacing the browser's global
  ``fetch`` in a test or host application.  It is local-only and never crosses the wire.
* Misaka's current ``StopReason`` has no transient ``"pending"`` member, so the partial
  starts at ``"stop"`` like its other providers and is overwritten by the terminal event.
"""

from __future__ import annotations

import codecs
import json
import logging
import time
from collections.abc import AsyncIterator, Mapping
from typing import Any, Literal, NotRequired, TypedDict

import httpx
from pydantic import BaseModel, ConfigDict

from misaka.ai.providers._common import (
    _await_with_signal,
    _empty_usage,
    _iterate_async_iterable,
)
from misaka.ai.types import (
    AssistantMessage,
    AssistantMessageEventValue,
    Context,
    DoneEvent,
    ErrorEvent,
    Model,
    SimpleStreamOptions,
    StartEvent,
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
    Usage,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream, spawn_stream_task
from misaka.ai.utils.json_parse import parse_streaming_json
from misaka.utils.values import signal_aborted

logger = logging.getLogger(__name__)


class ProxyMessageEventStream(AssistantMessageEventStream):
    """Assistant-message stream returned by :func:`stream_proxy`."""


class ProxyStartEvent(TypedDict):
    type: Literal["start"]


class ProxyTextStartEvent(TypedDict):
    type: Literal["text_start"]
    contentIndex: int


class ProxyTextDeltaEvent(TypedDict):
    type: Literal["text_delta"]
    contentIndex: int
    delta: str


class ProxyTextEndEvent(TypedDict):
    type: Literal["text_end"]
    contentIndex: int
    contentSignature: NotRequired[str]


class ProxyThinkingStartEvent(TypedDict):
    type: Literal["thinking_start"]
    contentIndex: int


class ProxyThinkingDeltaEvent(TypedDict):
    type: Literal["thinking_delta"]
    contentIndex: int
    delta: str


class ProxyThinkingEndEvent(TypedDict):
    type: Literal["thinking_end"]
    contentIndex: int
    contentSignature: NotRequired[str]


class ProxyToolCallStartEvent(TypedDict):
    type: Literal["toolcall_start"]
    contentIndex: int
    id: str
    toolName: str


class ProxyToolCallDeltaEvent(TypedDict):
    type: Literal["toolcall_delta"]
    contentIndex: int
    delta: str


class ProxyToolCallEndEvent(TypedDict):
    type: Literal["toolcall_end"]
    contentIndex: int
    toolCall: ToolCall


class ProxyDoneEvent(TypedDict):
    type: Literal["done"]
    reason: Literal["stop", "length", "toolUse"]
    usage: dict[str, Any]


class ProxyErrorEvent(TypedDict):
    type: Literal["error"]
    reason: Literal["aborted", "error"]
    errorMessage: NotRequired[str]
    usage: dict[str, Any]


type ProxyAssistantMessageEvent = (
    ProxyStartEvent
    | ProxyTextStartEvent
    | ProxyTextDeltaEvent
    | ProxyTextEndEvent
    | ProxyThinkingStartEvent
    | ProxyThinkingDeltaEvent
    | ProxyThinkingEndEvent
    | ProxyToolCallStartEvent
    | ProxyToolCallDeltaEvent
    | ProxyToolCallEndEvent
    | ProxyDoneEvent
    | ProxyErrorEvent
)


class ProxyStreamOptions(SimpleStreamOptions):
    """Pi's proxy-safe stream options plus the proxy endpoint credentials."""

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    authToken: str
    proxyUrl: str
    httpTransport: httpx.AsyncBaseTransport | None = None


def _build_proxy_request_options(options: ProxyStreamOptions) -> dict[str, Any]:
    thinking_budgets = options.thinkingBudgets
    return {
        "temperature": options.temperature,
        "samplingParams": options.samplingParams,
        "maxTokens": options.maxTokens,
        "reasoning": options.reasoning,
        "cacheRetention": options.cacheRetention,
        "sessionId": options.sessionId,
        "headers": dict(options.headers) if options.headers is not None else None,
        "metadata": dict(options.metadata) if options.metadata is not None else None,
        "transport": options.transport,
        "thinkingBudgets": (
            thinking_budgets.model_dump(mode="json", exclude_none=True)
            if isinstance(thinking_budgets, BaseModel)
            else thinking_budgets
        ),
        "maxRetryDelayMs": options.maxRetryDelayMs,
    }


def _as_options(
    options: ProxyStreamOptions | Mapping[str, Any] | Any,
) -> ProxyStreamOptions:
    if isinstance(options, ProxyStreamOptions):
        return options
    if isinstance(options, Mapping):
        payload = dict(options)
    elif hasattr(options, "keys") and hasattr(options, "__getitem__"):
        payload = {key: options[key] for key in options}
    elif isinstance(options, BaseModel):
        payload = options.model_dump()
    else:
        payload = vars(options)
    return ProxyStreamOptions.model_validate(payload)


def _as_context(context: Context | Mapping[str, Any] | Any) -> Context:
    if isinstance(context, Context):
        return context
    if isinstance(context, Mapping):
        return Context.model_validate(context)
    return Context(
        systemPrompt=getattr(context, "systemPrompt", None),
        messages=list(context.messages),
        tools=getattr(context, "tools", None),
    )


def _serialize_context(context: Context) -> dict[str, Any]:
    payload = context.model_dump(mode="json", exclude_none=True, exclude={"tools"})
    if context.tools is not None:
        payload["tools"] = []
        for tool in context.tools:
            serialized: dict[str, Any] = {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters_json_schema(),
            }
            if tool.constrainedSampling is not None:
                serialized["constrainedSampling"] = (
                    tool.constrainedSampling.model_dump(mode="json", exclude_none=True)
                    if isinstance(tool.constrainedSampling, BaseModel)
                    else tool.constrainedSampling
                )
            payload["tools"].append(serialized)
    return payload


def stream_proxy(
    model: Model,
    context: Context | Mapping[str, Any] | Any,
    options: ProxyStreamOptions | Mapping[str, Any] | Any,
) -> ProxyMessageEventStream:
    """Proxy one model stream through ``<proxyUrl>/api/stream``."""

    stream = ProxyMessageEventStream()
    partial = _create_partial_message(model)

    async def run() -> None:
        signal = _read_option(options, "signal")
        try:
            resolved_options = _as_options(options)
            await _consume_proxy_stream(
                stream,
                model,
                _as_context(context),
                resolved_options,
                partial,
            )
        except Exception as error:  # noqa: BLE001 - every request failure is an error event
            aborted = signal_aborted(signal)
            reason: Literal["aborted", "error"] = "aborted" if aborted else "error"
            partial.stopReason = reason
            partial.errorMessage = (
                "Request aborted by user" if aborted else _stringify_error(error)
            )
            stream.push(ErrorEvent(reason=reason, error=partial))
        finally:
            stream.end()

    spawn_stream_task(run())
    return stream


async def _consume_proxy_stream(
    stream: ProxyMessageEventStream,
    model: Model,
    context: Context,
    options: ProxyStreamOptions,
    partial: AssistantMessage,
) -> None:
    request_body = {
        "model": model.model_dump(mode="json", exclude_none=True),
        "context": _serialize_context(context),
        "options": {
            key: value
            for key, value in _build_proxy_request_options(options).items()
            if value is not None
        },
    }
    request_content = json.dumps(
        request_body,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    http_transport = options.httpTransport
    client = httpx.AsyncClient(
        transport=http_transport,
        timeout=httpx.Timeout(None),
        follow_redirects=True,
    )
    try:
        request = client.build_request(
            "POST",
            f"{options.proxyUrl}/api/stream",
            headers={
                "Authorization": f"Bearer {options.authToken}",
                "Content-Type": "application/json",
            },
            content=request_content,
        )
        response = await _await_with_signal(
            client.send(request, stream=True), options.signal
        )
        try:
            if not response.is_success:
                raise RuntimeError(
                    await _read_proxy_error_response(response, options.signal)
                )

            terminal_received = False
            tool_call_buffers: dict[int, str] = {}
            async for proxy_event in _read_proxy_events(response, options.signal):
                event = _process_proxy_event(proxy_event, partial, tool_call_buffers)
                if event is None:
                    continue
                stream.push(event)
                if event.type in {"done", "error"}:
                    terminal_received = True
                    break

            if signal_aborted(options.signal):
                raise RuntimeError("Request aborted by user")
            if not terminal_received:
                raise RuntimeError("Proxy stream ended without a terminal event")
        finally:
            await response.aclose()
    finally:
        # A caller-supplied transport may be a shared pool. Closing the client would close
        # that transport too; this matches the ownership rule used by Misaka's providers.
        if http_transport is None:
            await client.aclose()


async def _read_proxy_events(
    response: httpx.Response,
    signal: Any,
) -> AsyncIterator[Mapping[str, Any]]:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buffer = ""
    async for chunk in _iterate_async_iterable(
        response.aiter_bytes(),
        signal,
        on_abort=response.aclose,
    ):
        buffer += decoder.decode(chunk)
        lines = buffer.split("\n")
        buffer = lines.pop()
        for line in lines:
            if not line.startswith("data: "):
                continue
            data = line[6:].strip()
            if not data:
                continue
            parsed = json.loads(data)
            if not isinstance(parsed, Mapping):
                raise TypeError("Proxy event must be a JSON object")
            yield parsed


def _process_proxy_event(
    proxy_event: Mapping[str, Any],
    partial: AssistantMessage,
    tool_call_buffers: dict[int, str],
) -> AssistantMessageEventValue | None:
    event_type = proxy_event.get("type")

    if event_type == "start":
        return StartEvent(partial=partial)

    if event_type == "text_start":
        index = _content_index(proxy_event)
        _set_content(partial, index, TextContent(text=""))
        return TextStartEvent(contentIndex=index, partial=partial)

    if event_type == "text_delta":
        index = _content_index(proxy_event)
        content = _require_content(partial, index, "text", "text_delta")
        content.text += proxy_event["delta"]
        return TextDeltaEvent(
            contentIndex=index,
            delta=proxy_event["delta"],
            partial=partial,
        )

    if event_type == "text_end":
        index = _content_index(proxy_event)
        content = _require_content(partial, index, "text", "text_end")
        content.textSignature = proxy_event.get("contentSignature")
        return TextEndEvent(contentIndex=index, content=content.text, partial=partial)

    if event_type == "thinking_start":
        index = _content_index(proxy_event)
        _set_content(partial, index, ThinkingContent(thinking=""))
        return ThinkingStartEvent(contentIndex=index, partial=partial)

    if event_type == "thinking_delta":
        index = _content_index(proxy_event)
        content = _require_content(partial, index, "thinking", "thinking_delta")
        content.thinking += proxy_event["delta"]
        return ThinkingDeltaEvent(
            contentIndex=index,
            delta=proxy_event["delta"],
            partial=partial,
        )

    if event_type == "thinking_end":
        index = _content_index(proxy_event)
        content = _require_content(partial, index, "thinking", "thinking_end")
        content.thinkingSignature = proxy_event.get("contentSignature")
        return ThinkingEndEvent(
            contentIndex=index,
            content=content.thinking,
            partial=partial,
        )

    if event_type == "toolcall_start":
        index = _content_index(proxy_event)
        _set_content(
            partial,
            index,
            ToolCall(
                id=proxy_event["id"],
                name=proxy_event["toolName"],
                arguments={},
            ),
        )
        tool_call_buffers[index] = ""
        return ToolCallStartEvent(contentIndex=index, partial=partial)

    if event_type == "toolcall_delta":
        index = _content_index(proxy_event)
        content = _require_content(partial, index, "toolCall", "toolcall_delta")
        raw = f"{tool_call_buffers.get(index, '')}{proxy_event['delta']}"
        tool_call_buffers[index] = raw
        parsed = parse_streaming_json(raw)
        content.arguments = parsed if isinstance(parsed, dict) else {}
        return ToolCallDeltaEvent(
            contentIndex=index,
            delta=proxy_event["delta"],
            partial=partial,
        )

    if event_type == "toolcall_end":
        index = _content_index(proxy_event)
        if index >= len(partial.content):
            return None
        content = partial.content[index]
        if content.type != "toolCall":
            return None
        raw_tool_call = proxy_event["toolCall"]
        finalized = ToolCall.model_validate(raw_tool_call)
        raw_keys = (
            raw_tool_call.keys()
            if isinstance(raw_tool_call, Mapping)
            else ToolCall.model_fields
        )
        for key in raw_keys:
            if key in ToolCall.model_fields:
                setattr(content, key, getattr(finalized, key))
        tool_call_buffers.pop(index, None)
        return ToolCallEndEvent(contentIndex=index, toolCall=content, partial=partial)

    if event_type == "done":
        reason = proxy_event["reason"]
        partial.stopReason = reason
        partial.usage = Usage.model_validate(proxy_event["usage"])
        return DoneEvent(reason=reason, message=partial)

    if event_type == "error":
        reason = proxy_event["reason"]
        partial.stopReason = reason
        partial.errorMessage = proxy_event.get("errorMessage")
        partial.usage = Usage.model_validate(proxy_event["usage"])
        return ErrorEvent(reason=reason, error=partial)

    logger.warning("Unhandled proxy event type: %s", event_type)
    return None


def _create_partial_message(model: Model) -> AssistantMessage:
    return AssistantMessage(
        content=[],
        api=model.api,
        provider=model.provider,
        model=model.id,
        usage=_empty_usage(),
        # See the module docstring: Misaka has no transient "pending" StopReason yet.
        stopReason="stop",
        timestamp=int(time.time() * 1000),
    )


def _content_index(event: Mapping[str, Any]) -> int:
    index = event.get("contentIndex")
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        raise IndexError(f"Proxy event has an invalid contentIndex: {index!r}")
    return index


def _set_content(partial: AssistantMessage, index: int, content: Any) -> None:
    while len(partial.content) <= index:
        partial.content.append(TextContent(text=""))
    partial.content[index] = content


def _require_content(
    partial: AssistantMessage,
    index: int,
    expected_type: str,
    event_type: str,
) -> Any:
    if index >= len(partial.content):
        raise RuntimeError(f"Received {event_type} for non-{expected_type} content")
    content = partial.content[index]
    if content.type != expected_type:
        raise RuntimeError(f"Received {event_type} for non-{expected_type} content")
    return content


async def _read_proxy_error_response(response: httpx.Response, signal: Any) -> str:
    fallback = f"Proxy error: {response.status_code} {response.reason_phrase}"
    try:
        body = await _await_with_signal(
            response.aread(), signal, on_abort=response.aclose
        )
        payload = json.loads(body.decode("utf-8"))
    except Exception:  # noqa: BLE001 - Pi falls back when the error body is not JSON
        return fallback
    if isinstance(payload, Mapping) and payload.get("error"):
        return f"Proxy error: {payload['error']}"
    return fallback


def _read_option(options: Any, name: str) -> Any:
    if isinstance(options, Mapping):
        return options.get(name)
    return getattr(options, name, None)


def _stringify_error(error: Exception) -> str:
    return str(error) or error.__class__.__name__


streamProxy = stream_proxy

__all__ = [
    "ProxyAssistantMessageEvent",
    "ProxyStreamOptions",
    "streamProxy",
    "stream_proxy",
]

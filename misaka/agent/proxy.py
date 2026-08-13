"""Proxy stream bridge for server-routed model traffic."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from typing import Any, Literal, TypedDict

import httpx
from pydantic import ConfigDict
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
from misaka.ai.utils.event_stream import AssistantMessageEventStream
from misaka.ai.utils.json_parse import parse_streaming_json


class ProxyMessageEventStream(AssistantMessageEventStream):
    """Assistant-message stream returned by `stream_proxy()`."""


class ProxyStartEvent(TypedDict):
    type: Literal["start"]


class ProxyTextStartEvent(TypedDict):
    type: Literal["text_start"]
    contentIndex: int


class ProxyTextDeltaEvent(TypedDict):
    type: Literal["text_delta"]
    contentIndex: int
    delta: str


class ProxyTextEndEvent(TypedDict, total=False):
    type: Literal["text_end"]
    contentIndex: int
    contentSignature: str


class ProxyThinkingStartEvent(TypedDict):
    type: Literal["thinking_start"]
    contentIndex: int


class ProxyThinkingDeltaEvent(TypedDict):
    type: Literal["thinking_delta"]
    contentIndex: int
    delta: str


class ProxyThinkingEndEvent(TypedDict, total=False):
    type: Literal["thinking_end"]
    contentIndex: int
    contentSignature: str


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


class ProxyDoneEvent(TypedDict):
    type: Literal["done"]
    reason: Literal["stop", "length", "toolUse"]
    usage: dict[str, Any]


class ProxyErrorEvent(TypedDict, total=False):
    type: Literal["error"]
    reason: Literal["aborted", "error"]
    errorMessage: str
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
    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    authToken: str
    proxyUrl: str


def build_proxy_request_options(options: ProxyStreamOptions) -> dict[str, Any]:
    return {
        "temperature": options.temperature,
        "maxTokens": options.maxTokens,
        "reasoning": options.reasoning,
        "cacheRetention": options.cacheRetention,
        "sessionId": options.sessionId,
        "headers": dict(options.headers) if options.headers is not None else None,
        "metadata": dict(options.metadata) if options.metadata is not None else None,
        "transport": options.transport,
        "thinkingBudgets": options.thinkingBudgets.model_dump(exclude_none=True)
        if options.thinkingBudgets is not None
        else None,
        "maxRetryDelayMs": options.maxRetryDelayMs,
    }


def stream_proxy(
    model: Model,
    context: Context | dict[str, Any],
    options: ProxyStreamOptions | dict[str, Any],
) -> ProxyMessageEventStream:
    resolved_context = context if isinstance(context, Context) else Context.model_validate(context)
    resolved_options = (
        options
        if isinstance(options, ProxyStreamOptions)
        else ProxyStreamOptions.model_validate(options)
    )
    stream = ProxyMessageEventStream()
    partial = _create_partial_message(model)
    tool_call_buffers: dict[int, str] = {}

    async def run() -> None:
        consume_task = asyncio.create_task(
            _consume_proxy_stream(stream, model, resolved_context, resolved_options, partial, tool_call_buffers)
        )
        abort_task = (
            asyncio.create_task(_wait_for_abort(resolved_options.signal))
            if resolved_options.signal is not None
            else None
        )

        try:
            if abort_task is None:
                await consume_task
                return

            done, pending = await asyncio.wait(
                {consume_task, abort_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for pending_task in pending:
                pending_task.cancel()

            if abort_task in done and _signal_aborted(resolved_options.signal):
                consume_task.cancel()
                try:
                    await consume_task
                except asyncio.CancelledError:
                    pass
                raise RuntimeError("Request aborted by user")

            await consume_task
        except BaseException as error:  # noqa: BLE001
            reason = "aborted" if _signal_aborted(resolved_options.signal) else "error"
            error_message = "Request aborted by user" if reason == "aborted" else _stringify_error(error)
            partial.stopReason = reason
            partial.errorMessage = error_message
            stream.push(ErrorEvent(reason=reason, error=_clone_partial(partial)))
            stream.end()
        finally:
            if abort_task is not None:
                abort_task.cancel()

    asyncio.create_task(run())
    return stream


async def _consume_proxy_stream(
    stream: ProxyMessageEventStream,
    model: Model,
    context: Context,
    options: ProxyStreamOptions,
    partial: AssistantMessage,
    tool_call_buffers: dict[int, str],
) -> None:
    request_body = {
        "model": model.model_dump(mode="json", exclude_none=True),
        "context": context.model_dump(mode="json", exclude_none=True),
        "options": _drop_none(build_proxy_request_options(options)),
    }
    if options.onPayload is not None:
        await _maybe_await(options.onPayload(request_body, model))

    response: httpx.Response | None = None
    async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
        request = client.build_request(
            "POST",
            _resolve_proxy_stream_url(options.proxyUrl),
            headers={
                "Authorization": f"Bearer {options.authToken}",
                "Content-Type": "application/json",
            },
            json=request_body,
        )
        response = await client.send(request, stream=True)
        try:
            if options.onResponse is not None:
                await _maybe_await(
                    options.onResponse(
                        {"status": response.status_code, "headers": dict(response.headers)},
                        model,
                    )
                )

            if response.status_code >= 400:
                raise RuntimeError(await _read_proxy_error_response(response))

            async for raw_line in response.aiter_lines():
                if _signal_aborted(options.signal):
                    raise RuntimeError("Request aborted by user")
                line = raw_line.strip()
                if not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if not data or data == "[DONE]":
                    continue
                proxy_event = json.loads(data)
                event = process_proxy_event(proxy_event, partial, tool_call_buffers)
                if event is not None:
                    stream.push(event)

            if _signal_aborted(options.signal):
                raise RuntimeError("Request aborted by user")

            stream.end()
        finally:
            await response.aclose()


def process_proxy_event(
    proxy_event: ProxyAssistantMessageEvent | dict[str, Any],
    partial: AssistantMessage,
    tool_call_buffers: dict[int, str] | None = None,
) -> AssistantMessageEventValue | None:
    if tool_call_buffers is None:
        tool_call_buffers = {}
    event_type = proxy_event["type"]

    if event_type == "start":
        return StartEvent(partial=_clone_partial(partial))

    if event_type == "text_start":
        content_index = proxy_event["contentIndex"]
        _set_content_block(partial, content_index, TextContent(text=""))
        return TextStartEvent(contentIndex=content_index, partial=_clone_partial(partial))

    if event_type == "text_delta":
        content_index = proxy_event["contentIndex"]
        content = _require_content_type(partial, content_index, "text")
        assert isinstance(content, TextContent)
        content.text += proxy_event["delta"]
        return TextDeltaEvent(
            contentIndex=content_index,
            delta=proxy_event["delta"],
            partial=_clone_partial(partial),
        )

    if event_type == "text_end":
        content_index = proxy_event["contentIndex"]
        content = _require_content_type(partial, content_index, "text")
        assert isinstance(content, TextContent)
        content.textSignature = proxy_event.get("contentSignature")
        return TextEndEvent(contentIndex=content_index, content=content.text, partial=_clone_partial(partial))

    if event_type == "thinking_start":
        content_index = proxy_event["contentIndex"]
        _set_content_block(partial, content_index, ThinkingContent(thinking=""))
        return ThinkingStartEvent(contentIndex=content_index, partial=_clone_partial(partial))

    if event_type == "thinking_delta":
        content_index = proxy_event["contentIndex"]
        content = _require_content_type(partial, content_index, "thinking")
        assert isinstance(content, ThinkingContent)
        content.thinking += proxy_event["delta"]
        return ThinkingDeltaEvent(
            contentIndex=content_index,
            delta=proxy_event["delta"],
            partial=_clone_partial(partial),
        )

    if event_type == "thinking_end":
        content_index = proxy_event["contentIndex"]
        content = _require_content_type(partial, content_index, "thinking")
        assert isinstance(content, ThinkingContent)
        content.thinkingSignature = proxy_event.get("contentSignature")
        return ThinkingEndEvent(
            contentIndex=content_index,
            content=content.thinking,
            partial=_clone_partial(partial),
        )

    if event_type == "toolcall_start":
        content_index = proxy_event["contentIndex"]
        _set_content_block(
            partial,
            content_index,
            ToolCall(id=proxy_event["id"], name=proxy_event["toolName"], arguments={}),
        )
        tool_call_buffers[content_index] = ""
        return ToolCallStartEvent(contentIndex=content_index, partial=_clone_partial(partial))

    if event_type == "toolcall_delta":
        content_index = proxy_event["contentIndex"]
        content = _require_content_type(partial, content_index, "toolCall")
        assert isinstance(content, ToolCall)
        tool_call_buffers[content_index] = tool_call_buffers.get(content_index, "") + proxy_event["delta"]
        content.arguments = parse_streaming_json(tool_call_buffers[content_index]) or {}
        return ToolCallDeltaEvent(
            contentIndex=content_index,
            delta=proxy_event["delta"],
            partial=_clone_partial(partial),
        )

    if event_type == "toolcall_end":
        content_index = proxy_event["contentIndex"]
        try:
            content = partial.content[content_index]
        except IndexError:
            return None
        if content.type != "toolCall":
            return None
        tool_call_buffers.pop(content_index, None)
        assert isinstance(content, ToolCall)
        return ToolCallEndEvent(
            contentIndex=content_index,
            toolCall=content.model_copy(deep=True),
            partial=_clone_partial(partial),
        )

    if event_type == "done":
        partial.stopReason = proxy_event["reason"]
        partial.usage = Usage.model_validate(proxy_event["usage"])
        return DoneEvent(reason=proxy_event["reason"], message=_clone_partial(partial))

    if event_type == "error":
        partial.stopReason = proxy_event["reason"]
        partial.errorMessage = proxy_event.get("errorMessage")
        partial.usage = Usage.model_validate(proxy_event["usage"])
        return ErrorEvent(reason=proxy_event["reason"], error=_clone_partial(partial))

    return None


def _create_partial_message(model: Model) -> AssistantMessage:
    return AssistantMessage(
        role="assistant",
        content=[],
        api=model.api,
        provider=model.provider,
        model=model.id,
        usage=Usage(
            input=0,
            output=0,
            cacheRead=0,
            cacheWrite=0,
            totalTokens=0,
            cost={"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
        ),
        stopReason="stop",
        timestamp=int(time.time() * 1000),
    )


def _clone_partial(partial: AssistantMessage) -> AssistantMessage:
    return partial.model_copy(deep=True)


def _set_content_block(partial: AssistantMessage, index: int, block: Any) -> None:
    while len(partial.content) <= index:
        partial.content.append(TextContent(text=""))
    partial.content[index] = block


def _require_content_type(partial: AssistantMessage, index: int, expected_type: str) -> Any:
    try:
        content = partial.content[index]
    except IndexError as error:
        raise RuntimeError(f"Received {expected_type} event for missing content index {index}") from error
    if content.type != expected_type:
        raise RuntimeError(f"Received event for non-{expected_type} content")
    return content


async def _read_proxy_error_response(response: httpx.Response) -> str:
    try:
        body = await response.aread()
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return f"Proxy error: {response.status_code} {response.reason_phrase}"

    error_message = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error_message, str) and error_message:
        return f"Proxy error: {error_message}"
    return f"Proxy error: {response.status_code} {response.reason_phrase}"


def _resolve_proxy_stream_url(proxy_url: str) -> str:
    return proxy_url.rstrip("/") + "/api/stream"


def _signal_aborted(signal: Any | None) -> bool:
    if signal is None:
        return False
    if hasattr(signal, "aborted"):
        return bool(signal.aborted)
    if hasattr(signal, "is_set"):
        return bool(signal.is_set())
    return False


async def _wait_for_abort(signal: Any) -> None:
    if hasattr(signal, "wait") and callable(signal.wait):
        await signal.wait()
        return
    while not _signal_aborted(signal):
        await asyncio.sleep(0.01)


def _drop_none(value: dict[str, Any]) -> dict[str, Any]:
    return {key: nested for key, nested in value.items() if nested is not None}


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _stringify_error(error: BaseException) -> str:
    return error.args[0] if error.args else error.__class__.__name__


buildProxyRequestOptions = build_proxy_request_options
streamProxy = stream_proxy
processProxyEvent = process_proxy_event

__all__ = [
    "ProxyAssistantMessageEvent",
    "ProxyMessageEventStream",
    "ProxyStreamOptions",
    "buildProxyRequestOptions",
    "build_proxy_request_options",
    "processProxyEvent",
    "process_proxy_event",
    "streamProxy",
    "stream_proxy",
]

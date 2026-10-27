"""Translated from pi's ``packages/ai/src/api/pi-messages.ts`` (v1.1.0).

pi-messages API implementation.

Streams pi's own message protocol directly to a backend: the request is a
single POST of `{ model, context, options }` to `<baseUrl>/messages`, the
response is an SSE stream of serialized assistant-message events plus a
terminal `done`/`error` event. This is the wire protocol spoken by the
Radius gateway, but any backend implementing it can be used, e.g. via a
models.json custom provider with `"api": "pi-messages"`.

MISAKA fork, kept to what Python forces (each is marked where it happens): ``options.fetch`` is an httpx transport
(``httpTransport``); the request has the HTTP idle timeout upstream's undici dispatcher gives it; the context is
serialized from its pydantic models; the backend's events are folded into strict models (unknown usage keys and tool
call keys dropped, content indexes checked, an unknown event type dropped); a tool call's arguments stream through
``StreamingArgs``.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any, Literal, TypedDict

import httpx
from pydantic import BaseModel

from misaka.ai.api._common import _await_with_signal, _iterate_async_iterable, _option
from misaka.ai.types import (
    AssistantMessage,
    AssistantMessageEvent,
    CacheRetention,
    DoneEvent,
    ErrorEvent,
    Model,
    ProviderEnv,
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
    ThinkingLevel,
    ThinkingStartEvent,
    ToolCall,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    TranscriptContext,
    Usage,
    UsageCost,
)
from misaka.ai.utils._streaming_args import StreamingArgs
from misaka.ai.utils.diagnostics import (
    AssistantMessageDiagnostic,
    appendAssistantMessageDiagnostic,
    createAssistantMessageDiagnostic,
    formatThrownValue,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream, spawn_stream_task
from misaka.ai.utils.headers import headersToRecord, providerHeadersToRecord
from misaka.ai.utils.provider_env import getProviderEnvValue
from misaka.core.http_dispatcher import createHttpxIdleTimeout
from misaka.utils.js import (
    Utf8Decoder,
    jsLength,
    jsonParse,
    jsonStringify,
    jsSlice,
    jsString,
    jsTrim,
)
from misaka.utils.values import maybe_await, signal_aborted


class PiMessagesOptions(TypedDict, total=False):
    # StreamOptions members.
    temperature: float
    samplingParams: dict[str, Any]
    maxTokens: int
    signal: Any
    telemetryContext: Any
    apiKey: str
    transport: Any
    cacheRetention: CacheRetention
    sessionId: str
    headers: dict[str, str | None]
    onPayload: Any
    onResponse: Any
    onProviderStreamEvent: Any
    timeoutMs: int
    websocketConnectTimeoutMs: int
    maxRetries: int
    maxRetryDelayMs: int
    metadata: dict[str, Any]
    env: ProviderEnv
    reasoning: ThinkingLevel
    toolChoice: Literal["auto", "none", "required"] | dict[str, Any]
    # Ask the backend for debug metadata (e.g. routing response headers).
    debug: bool
    # MISAKA fork: the stand-in for upstream's `fetch` injection, handed to the httpx client.
    httpTransport: httpx.AsyncBaseTransport


_PiMessagesUsage = Usage
_PiMessagesStopReason = StopReason


class PiMessagesRewriteImpact(TypedDict):
    """Impact summary of a server-side message rewrite (e.g. a gateway policy)."""

    policyId: str
    policyVersion: int
    changed: bool
    tokenCountChange: int
    messageCountChange: int
    systemPromptChanged: bool


# Serialized assistant-message event as sent by a pi-messages backend: `{type: "start"}`,
# `text_start|text_delta|text_end`, `thinking_start|thinking_delta|thinking_end`,
# `toolcall_start|toolcall_delta|toolcall_end`, and the terminal `done`/`error` (with `usage`, `responseId?`,
# `providerThinkingLevel?`, `rewrite?`). MISAKA fork: the parsed JSON object as it arrives.
type PiMessagesEvent = dict[str, Any]

type _PiMessagesErrorBody = dict[str, Any]


class PiMessagesResponseError(Exception):
    def __init__(self, message: str, code: str | None, diagnosticDetails: dict[str, Any]) -> None:
        super().__init__(message)
        self.name = "PiMessagesResponseError"
        self.code = code
        self.diagnosticDetails = diagnosticDetails


def _parsePiMessagesErrorBody(body: str) -> _PiMessagesErrorBody | None:
    try:
        parsed = jsonParse(body)
        error = parsed.get("error") if isinstance(parsed, dict) else None
        return parsed if parsed and isinstance(error, dict) else None
    except ValueError:
        return None


def _truncateDiagnosticString(value: str) -> str:
    maxLength = 8192
    return f"{jsSlice(value, 0, maxLength)}…" if jsLength(value) > maxLength else value


def _formatPiMessagesResponseError(
    response: httpx.Response,
    body: str,
    errorBody: _PiMessagesErrorBody | None,
) -> str:
    error = (errorBody or {}).get("error") or {}
    message = error.get("message") if isinstance(error.get("message"), str) else None
    code = error.get("code") if isinstance(error.get("code"), str) else None
    suffix = message if message is not None else body
    codeSuffix = f" ({code})" if code else ""
    return f"{response.status_code} {response.reason_phrase}: {suffix}{codeSuffix}"


def _createPiMessagesResponseError(
    model: Model,
    url: str,
    response: httpx.Response,
    body: str,
) -> PiMessagesResponseError:
    errorBody = _parsePiMessagesErrorBody(body)
    error = (errorBody or {}).get("error")
    code = error.get("code") if isinstance(error, dict) and isinstance(error.get("code"), str) else None
    return PiMessagesResponseError(
        _formatPiMessagesResponseError(response, body, errorBody),
        code,
        {
            "version": 1,
            "provider": model.provider,
            "model": model.id,
            "url": url,
            "status": response.status_code,
            "statusText": response.reason_phrase,
            **({} if error is None else {"error": error}),
            **({} if errorBody else {"body": _truncateDiagnosticString(body)}),
            "timestampMs": time.time_ns() // 1_000_000,
        },
    )


def _createEmptyUsage() -> _PiMessagesUsage:
    return Usage(
        input=0,
        output=0,
        cacheRead=0,
        cacheWrite=0,
        totalTokens=0,
        cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
    )


def _appendRewriteDiagnostic(message: AssistantMessage, rewrite: PiMessagesRewriteImpact | None) -> None:
    if not rewrite:
        return
    appendAssistantMessageDiagnostic(
        message,
        AssistantMessageDiagnostic(type="pi_messages_rewrite", timestamp=time.time_ns() // 1_000_000, details={**rewrite}),
    )


def _coerceUsage(current: Usage, usage: Any) -> Usage:
    """MISAKA fork: upstream assigns the wire `usage` onto the message; ``Usage`` forbids extra keys, so an unmodelled
    counter (or price) a backend starts reporting is dropped instead of failing the whole response, and a missing or
    non-object `usage` keeps the zeroed one."""
    if not isinstance(usage, Mapping):
        return current
    merged = current.model_dump()
    for key, value in usage.items():
        if key not in Usage.model_fields:
            continue
        if key == "cost":
            if isinstance(value, Mapping):
                merged["cost"].update({k: v for k, v in value.items() if k in UsageCost.model_fields})
            continue
        merged[key] = value
    return Usage.model_validate(merged)


def _createEventConverter(model: Model) -> Callable[[PiMessagesEvent], AssistantMessageEvent | None]:
    partial = AssistantMessage(
        role="assistant",
        content=[],
        api=model.api,
        provider=model.provider,
        model=model.id,
        usage=_createEmptyUsage(),
        stopReason="pending",
        timestamp=time.time_ns() // 1_000_000,
    )
    # MISAKA fork: StreamingArgs rather than re-parsing the whole text on every delta; the final parse records a
    # malformed object as `argumentsError`.
    toolJson: dict[int, StreamingArgs] = {}

    def checkIndex(index: Any) -> int:
        """MISAKA fork: a list cannot be indexed by anything but a non-negative int."""
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise IndexError(f"pi-messages event has an invalid contentIndex: {jsString(index)}")
        return index

    def contentAt(index: Any) -> Any:
        position = checkIndex(index)
        if position >= len(partial.content):
            raise IndexError(f"pi-messages event references missing content index {position}")
        return partial.content[position]

    def setContent(index: Any, block: Any) -> None:
        """MISAKA fork: a JS array grows with holes when written past its end; an empty text block stands in."""
        position = checkIndex(index)
        while len(partial.content) <= position:
            partial.content.append(TextContent(type="text", text=""))
        partial.content[position] = block

    def assignTerminal(event: PiMessagesEvent) -> None:
        partial.stopReason = event["reason"]
        partial.usage = _coerceUsage(partial.usage, event.get("usage"))
        partial.responseId = event.get("responseId")

    def convertEvent(event: PiMessagesEvent) -> AssistantMessageEvent | None:
        match event.get("type"):
            case "done":
                # MISAKA fork: buffers a backend never closed get their exact parse before dispatch.
                for index, args in toolJson.items():
                    if args.raw:
                        args.finish_into(contentAt(index))
                toolJson.clear()
                assignTerminal(event)
                if event.get("providerThinkingLevel") is not None:
                    partial.providerThinkingLevel = event["providerThinkingLevel"]
                _appendRewriteDiagnostic(partial, event.get("rewrite"))
                return DoneEvent(reason=event["reason"], message=partial)
            case "error":
                assignTerminal(event)
                partial.errorMessage = event.get("errorMessage")
                if event.get("providerThinkingLevel") is not None:
                    partial.providerThinkingLevel = event["providerThinkingLevel"]
                _appendRewriteDiagnostic(partial, event.get("rewrite"))
                return ErrorEvent(reason=event["reason"], error=partial)
            case "start":
                return StartEvent(partial=partial)
            case "text_start":
                setContent(event["contentIndex"], TextContent(type="text", text=""))
                return TextStartEvent(contentIndex=event["contentIndex"], partial=partial)
            case "text_delta":
                contentAt(event["contentIndex"]).text += event["delta"]
                return TextDeltaEvent(contentIndex=event["contentIndex"], delta=event["delta"], partial=partial)
            case "text_end":
                block = contentAt(event["contentIndex"])
                block.text = event["content"]
                block.textSignature = event.get("contentSignature")
                return TextEndEvent(contentIndex=event["contentIndex"], content=event["content"], partial=partial)
            case "thinking_start":
                setContent(event["contentIndex"], ThinkingContent(type="thinking", thinking=""))
                return ThinkingStartEvent(contentIndex=event["contentIndex"], partial=partial)
            case "thinking_delta":
                contentAt(event["contentIndex"]).thinking += event["delta"]
                return ThinkingDeltaEvent(contentIndex=event["contentIndex"], delta=event["delta"], partial=partial)
            case "thinking_end":
                block = contentAt(event["contentIndex"])
                block.thinking = event["content"]
                block.thinkingSignature = event.get("contentSignature")
                block.redacted = event.get("redacted")
                return ThinkingEndEvent(contentIndex=event["contentIndex"], content=event["content"], partial=partial)
            case "toolcall_start":
                setContent(
                    event["contentIndex"],
                    ToolCall(type="toolCall", id=event["id"], name=event["toolName"], arguments={}),
                )
                toolJson[checkIndex(event["contentIndex"])] = StreamingArgs()
                return ToolCallStartEvent(contentIndex=event["contentIndex"], partial=partial)
            case "toolcall_delta":
                json = toolJson.setdefault(checkIndex(event["contentIndex"]), StreamingArgs())
                json.append(event["delta"])
                contentAt(event["contentIndex"]).arguments = json.arguments
                return ToolCallDeltaEvent(contentIndex=event["contentIndex"], delta=event["delta"], partial=partial)
            case "toolcall_end":
                block = contentAt(event["contentIndex"])
                # MISAKA fork: `Object.assign` copies every own property; the strict model takes the ones it declares.
                for key, value in dict(event["toolCall"]).items():
                    if key in ToolCall.model_fields:
                        setattr(block, key, value)
                args = toolJson.pop(event["contentIndex"], None)
                # MISAKA fork: the backend's final arguments win; only an empty object is checked against the
                # streamed text, so a malformed stream still surfaces as `argumentsError`.
                if args is not None and args.raw and block.argumentsError is None and not block.arguments:
                    args.finish_into(block)
                return ToolCallEndEvent(contentIndex=event["contentIndex"], toolCall=block, partial=partial)
        # MISAKA fork: upstream forwards an unknown event type as is; the typed stream cannot carry it.
        return None

    return convertEvent


async def _bodyChunks(response: httpx.Response) -> AsyncIterator[bytes]:
    """MISAKA fork: undici's body timeout ends a fetch body with "terminated"."""
    try:
        async for chunk in response.aiter_bytes():
            yield chunk
    except httpx.ReadTimeout as error:
        raise RuntimeError("terminated") from error


async def _readPiMessagesEvents(stream: httpx.Response, signal: Any = None) -> AsyncIterator[PiMessagesEvent]:
    """MISAKA fork: ``stream`` is the httpx response; a read races the abort signal."""
    decoder = Utf8Decoder()
    buffer = ""

    async def handOver() -> None:
        # MISAKA fork: JavaScript's `for await` hands control to the consumer at every event, so it sees the
        # partial as each event left it; a chunk's events would otherwise all be applied before it runs.
        await asyncio.sleep(0)

    def drain() -> list[PiMessagesEvent]:
        nonlocal buffer
        events: list[PiMessagesEvent] = []
        split = buffer.find("\n\n")
        while split != -1:
            event = _parsePiMessagesEvent(buffer[:split])
            if event:
                events.append(event)
            buffer = buffer[split + 2 :]
            split = buffer.find("\n\n")
        return events

    async for value in _iterate_async_iterable(_bodyChunks(stream), signal, on_abort=stream.aclose):
        buffer = (buffer + decoder.decode(value)).replace("\r\n", "\n")
        for event in drain():
            await handOver()
            yield event

    buffer = (buffer + decoder.decode(b"", final=True)).replace("\r\n", "\n")
    for event in drain():
        yield event

    if jsTrim(buffer):
        event = _parsePiMessagesEvent(buffer)
        if event:
            await handOver()
            yield event


def _parsePiMessagesEvent(raw: str) -> PiMessagesEvent | None:
    line = next((line for line in raw.split("\n") if line.startswith("data:")), None)
    data = jsTrim(line[5:]) if line is not None else None

    if not data or data == "[DONE]":
        return None
    parsed = jsonParse(data)
    # MISAKA fork: anything but an object is no event.
    return parsed if isinstance(parsed, dict) else None


def _createErrorEvent(model: Model, error: Any, aborted: bool) -> ErrorEvent:
    reason: Literal["aborted", "error"] = "aborted" if aborted else "error"
    assistantMessage = AssistantMessage(
        role="assistant",
        content=[],
        api=model.api,
        provider=model.provider,
        model=model.id,
        usage=_createEmptyUsage(),
        stopReason=reason,
        errorMessage=formatThrownValue(error) if isinstance(error, Exception) else jsString(error),
        timestamp=time.time_ns() // 1_000_000,
    )

    if not aborted and isinstance(error, PiMessagesResponseError):
        appendAssistantMessageDiagnostic(
            assistantMessage,
            createAssistantMessageDiagnostic("pi_messages_response_failure", error, error.diagnosticDetails),
        )

    return ErrorEvent(reason=reason, error=assistantMessage)


def _resolveCacheRetention(cacheRetention: CacheRetention | None = None, env: ProviderEnv | None = None) -> CacheRetention | None:
    if cacheRetention:
        return cacheRetention
    # Backend defaults apply when unset; only the legacy env opt-in is mapped.
    return "long" if getProviderEnvValue("PI_CACHE_RETENTION", env) == "long" else None


def _serializeContext(context: TranscriptContext | Mapping[str, Any]) -> Any:
    """MISAKA fork: the context as ``JSON.stringify`` would write the upstream objects -- absent fields left out and a
    tool's pydantic ``parameters`` class as its JSON schema. A tool call's ``argumentsError`` travels like any field,
    as upstream sends ``context`` whole."""
    if not isinstance(context, BaseModel):
        return context
    payload = context.model_dump(mode="json", exclude_none=True, exclude={"messages": {"__all__": {"toolsAdded": True}}})
    for message, dumped in zip(context.messages, payload["messages"], strict=True):
        tools = getattr(message, "toolsAdded", None)
        # `is not None`: `JSON.stringify` writes `"toolsAdded":[]` for an empty list.
        if tools is not None:
            dumped["toolsAdded"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters_json_schema(),
                    **(
                        {}
                        if tool.constrainedSampling is None
                        else {
                            "constrainedSampling": tool.constrainedSampling
                            if isinstance(tool.constrainedSampling, bool)
                            else tool.constrainedSampling.model_dump(exclude_none=True)
                        }
                    ),
                }
                for tool in tools
            ]
    return payload


def stream(
    model: Model,
    context: TranscriptContext,
    options: PiMessagesOptions | StreamOptions | Mapping[str, Any] | None = None,
) -> AssistantMessageEventStream:
    eventStream = AssistantMessageEventStream()
    convertEvent = _createEventConverter(model)

    async def run() -> None:
        signal = _option(options, "signal")
        try:
            apiKey = _option(options, "apiKey")
            if not apiKey:
                raise RuntimeError(f'No API key provided for provider "{model.provider}"')

            url = f"{re.sub(r'/+$', '', model.baseUrl)}/messages"
            params = {"debug": "1"} if _option(options, "debug") else None

            # MISAKA fork: `JSON.stringify` drops undefined options; the None ones are left out here.
            requestOptions = {
                "temperature": _option(options, "temperature"),
                "maxTokens": _option(options, "maxTokens"),
                "reasoning": _option(options, "reasoning"),
                "cacheRetention": _resolveCacheRetention(_option(options, "cacheRetention"), _option(options, "env")),
                "sessionId": _option(options, "sessionId"),
                "toolChoice": _option(options, "toolChoice"),
            }
            payload: Any = {
                "model": model.id,
                "context": _serializeContext(context),
                "options": {key: value for key, value in requestOptions.items() if value is not None},
            }
            onPayload = _option(options, "onPayload")
            nextPayload = await maybe_await(onPayload(payload, model)) if onPayload else None
            if nextPayload is not None:
                payload = nextPayload

            # MISAKA fork: upstream's fetch takes its idle timeout from the global undici dispatcher; here the client
            # takes it from `timeoutMs`, which the SDK fills from the same HTTP idle setting. A transport handed in
            # stands for `options.fetch` and is not closed with the client.
            httpTransport = _option(options, "httpTransport")
            client = httpx.AsyncClient(
                transport=httpTransport, timeout=createHttpxIdleTimeout(_option(options, "timeoutMs")), follow_redirects=True
            )
            try:
                request = client.build_request(
                    "POST",
                    url,
                    params=params,
                    headers={
                        "authorization": f"Bearer {apiKey}",
                        "accept": "text/event-stream",
                        "content-type": "application/json",
                        **(providerHeadersToRecord(_option(options, "headers")) or {}),
                    },
                    content=jsonStringify(payload).encode("utf-8"),
                )
                try:
                    response = await _await_with_signal(client.send(request, stream=True), signal)
                except httpx.ReadTimeout as error:
                    # MISAKA fork: undici's headers timeout rejects the fetch with "fetch failed".
                    raise RuntimeError("fetch failed") from error
                try:
                    onResponse = _option(options, "onResponse")
                    if onResponse:
                        await maybe_await(
                            onResponse({"status": response.status_code, "headers": headersToRecord(response.headers)}, model)
                        )

                    if not response.is_success:
                        body = (await response.aread()).decode("utf-8", errors="replace")
                        raise _createPiMessagesResponseError(model, str(request.url), response, body)

                    onProviderStreamEvent = _option(options, "onProviderStreamEvent")
                    async for piEvent in _readPiMessagesEvents(response, signal):
                        if onProviderStreamEvent:
                            await maybe_await(onProviderStreamEvent(piEvent, model))
                        event = convertEvent(piEvent)
                        if event is None:
                            continue
                        eventStream.push(event)
                        if event.type == "done" or event.type == "error":
                            return

                    raise RuntimeError(f"{model.provider} stream ended without a terminal event")
                finally:
                    await response.aclose()
            finally:
                if httpTransport is None:
                    await client.aclose()
        except Exception as error:  # noqa: BLE001 - every failure leaves as an error event
            eventStream.push(_createErrorEvent(model, error, signal_aborted(signal)), cause=error)
        finally:
            # MISAKA fork: release a consumer still waiting (the terminal event already settled the result).
            eventStream.end()

    spawn_stream_task(run(), stream=eventStream)
    return eventStream


def streamSimple(
    model: Model,
    context: TranscriptContext,
    options: SimpleStreamOptions | Mapping[str, Any] | None = None,
) -> AssistantMessageEventStream:
    extra = options
    base: dict[str, Any] = dict(options) if options is not None else {}
    return stream(
        model,
        context,
        {
            **base,
            "reasoning": _option(options, "reasoning"),
            "toolChoice": _option(options, "toolChoice"),
            "debug": _option(extra, "debug"),
        },
    )


__all__ = [
    "PiMessagesEvent",
    "PiMessagesOptions",
    "PiMessagesResponseError",
    "PiMessagesRewriteImpact",
    "stream",
    "streamSimple",
]

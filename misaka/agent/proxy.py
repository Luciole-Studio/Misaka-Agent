"""Proxy stream function for apps that route LLM calls through a server.
The server manages auth and proxies requests to LLM providers.

Translated from pi's ``packages/agent/src/proxy.ts`` (v1.1.0). MISAKA fork: ``fetch`` is the WHATWG shim
(``misaka.utils.whatwg_fetch``), so tests replace this module's ``fetch`` where upstream stubs the global one.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any, Literal, NotRequired, TypedDict

from misaka.ai.api.pi_messages import _serializeContext
from misaka.ai.types import (
    AssistantMessage,
    AssistantMessageEventValue,
    DoneEvent,
    ErrorEvent,
    Model,
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
    TranscriptContext,
    Usage,
)
from misaka.ai.utils._streaming_args import StreamingArgs
from misaka.ai.utils.event_stream import (
    AssistantMessageEventStream,
    createAssistantMessageEventStream,
    spawn_stream_task,
)
from misaka.ai.utils.json_parse import parseStreamingJson
from misaka.utils.js import (
    Utf8Decoder,
    addAbortListener,
    errorMessage,
    jsonParse,
    jsonStringify,
    jsTrim,
)
from misaka.utils.values import read_field, signal_aborted
from misaka.utils.whatwg_fetch import fetch

logger = logging.getLogger(__name__)


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
    toolCall: Any


class ProxyDoneEvent(TypedDict):
    type: Literal["done"]
    reason: Literal["stop", "length", "toolUse"]
    usage: Any
    providerThinkingLevel: NotRequired[str]


class ProxyErrorEvent(TypedDict):
    type: Literal["error"]
    reason: Literal["aborted", "error"]
    errorMessage: NotRequired[str]
    usage: Any
    providerThinkingLevel: NotRequired[str]


# Proxy event types - server sends these with partial field stripped to reduce bandwidth.
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


class ProxySerializableStreamOptions(TypedDict, total=False):
    temperature: float
    samplingParams: Any
    maxTokens: int
    reasoning: Any
    cacheRetention: Any
    sessionId: str
    headers: dict[str, str]
    metadata: dict[str, Any]
    transport: Any
    thinkingBudgets: Any
    maxRetryDelayMs: int


class ProxyStreamOptions(ProxySerializableStreamOptions, total=False):
    # Local abort signal for the proxy request
    signal: Any
    # Auth token for the proxy server
    authToken: str
    # Proxy server URL (e.g., "https://genai.example.com")
    proxyUrl: str


def _buildProxyRequestOptions(options: Any) -> ProxySerializableStreamOptions:
    """Stream function that proxies through a server instead of calling LLM providers directly.
    The server strips the partial field from delta events to reduce bandwidth.
    We reconstruct the partial message client-side.

    Use this as the `streamFn` option when creating an Agent that needs to go through a proxy.

    MISAKA fork: the options are read as an object or a mapping (the agent loop hands over an options namespace).
    """
    return {  # type: ignore[return-value]
        "temperature": read_field(options, "temperature"),
        "samplingParams": read_field(options, "samplingParams"),
        "maxTokens": read_field(options, "maxTokens"),
        "reasoning": read_field(options, "reasoning"),
        "cacheRetention": read_field(options, "cacheRetention"),
        "sessionId": read_field(options, "sessionId"),
        "headers": read_field(options, "headers"),
        "metadata": read_field(options, "metadata"),
        "transport": read_field(options, "transport"),
        "thinkingBudgets": read_field(options, "thinkingBudgets"),
        "maxRetryDelayMs": read_field(options, "maxRetryDelayMs"),
    }


def _jsonValue(value: Any) -> Any:
    """MISAKA fork: a pydantic option (thinking budgets, sampling parameters) as ``JSON.stringify`` writes it."""
    dump = getattr(value, "model_dump", None)
    return dump(mode="json", exclude_none=True) if callable(dump) else value


def streamProxy(model: Model, context: TranscriptContext, options: ProxyStreamOptions | Any) -> AssistantMessageEventStream:
    stream = createAssistantMessageEventStream()

    async def run() -> None:
        # Initialize the partial message that we'll build up from events
        partial = AssistantMessage(
            stopReason="pending",
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
            timestamp=_now(),
        )

        reader: Any = None
        signal = read_field(options, "signal")

        def abortHandler() -> None:
            if reader is not None:
                spawn_stream_task(reader.cancel())

        removeAbortListener = addAbortListener(signal, abortHandler) if signal is not None else None

        try:
            requestOptions = {
                key: _jsonValue(value)
                for key, value in _buildProxyRequestOptions(options).items()
                if value is not None
            }
            response = await fetch(
                f"{read_field(options, 'proxyUrl')}/api/stream",
                {
                    "method": "POST",
                    "headers": {
                        "Authorization": f"Bearer {read_field(options, 'authToken')}",
                        "Content-Type": "application/json",
                    },
                    "body": jsonStringify(
                        {
                            "model": model.model_dump(mode="json", exclude_none=True),
                            "context": _serializeContext(context),
                            "options": requestOptions,
                        }
                    ),
                    "signal": signal,
                },
            )

            if not response.ok:
                failure = f"Proxy error: {response.status} {response.statusText}"
                try:
                    errorData = await response.json()
                    if isinstance(errorData, Mapping) and errorData.get("error"):
                        failure = f"Proxy error: {errorData['error']}"
                except Exception:  # noqa: BLE001, S110 - Couldn't parse error response
                    pass
                raise RuntimeError(failure)

            reader = response.body
            decoder = Utf8Decoder()
            buffer = ""
            sawTerminalEvent = False
            # MISAKA fork: upstream keeps `partialJson` on the tool-call block; a ToolCall model has no such field,
            # so the raw argument text lives here, keyed by content index.
            partialJson: dict[int, str] = {}

            def processLine(line: str) -> None:
                nonlocal sawTerminalEvent
                if not line.startswith("data: "):
                    return
                data = jsTrim(line[6:])
                if not data:
                    return
                proxyEvent = jsonParse(data)
                event = _processProxyEvent(proxyEvent, partial, partialJson)
                if event is not None:
                    if event.type in ("done", "error"):
                        sawTerminalEvent = True
                    stream.push(event)

            if reader is not None:
                async for value in reader:
                    if signal_aborted(signal):
                        raise RuntimeError("Request aborted by user")

                    buffer += decoder.decode(value)
                    lines = buffer.split("\n")
                    buffer = lines.pop() or ""

                    for line in lines:
                        processLine(line)

            if signal_aborted(signal):
                raise RuntimeError("Request aborted by user")

            # The final event may not be newline-terminated; flush the decoder and
            # process whatever is left in the buffer.
            buffer += decoder.decode(b"", final=True)
            if buffer:
                processLine(buffer)

            if not sawTerminalEvent:
                # A clean EOF without a done/error event means the server dropped the
                # response mid-stream. Surface it as an error instead of leaving
                # consumers waiting on a result that never arrives.
                partial.stopReason = "error"
                partial.errorMessage = "Connection closed by proxy server before the response completed"
                stream.push(ErrorEvent(reason="error", error=partial))

            stream.end()
        except Exception as error:  # noqa: BLE001 - every request failure is an error event
            reason: Literal["aborted", "error"] = "aborted" if signal_aborted(signal) else "error"
            partial.stopReason = reason
            partial.errorMessage = errorMessage(error)
            stream.push(ErrorEvent(reason=reason, error=partial), cause=error)
            stream.end()
        finally:
            if removeAbortListener is not None:
                removeAbortListener()

    spawn_stream_task(run(), stream=stream)
    return stream


def _now() -> int:
    return int(time.time() * 1000)


def _contentAt(partial: AssistantMessage, index: int) -> Any:
    """MISAKA fork: ``partial.content[index]``, undefined past the end."""
    return partial.content[index] if 0 <= index < len(partial.content) else None


def _setContent(partial: AssistantMessage, index: int, content: Any) -> None:
    """MISAKA fork: ``partial.content[index] = content``; a JavaScript array grows to take the index."""
    while len(partial.content) <= index:
        partial.content.append(TextContent(text=""))
    partial.content[index] = content


def _processProxyEvent(
    proxyEvent: Mapping[str, Any],
    partial: AssistantMessage,
    partialJson: dict[int, str],
) -> AssistantMessageEventValue | None:
    """Process a proxy event and update the partial message."""
    match proxyEvent.get("type"):
        case "start":
            return StartEvent(partial=partial)

        case "text_start":
            _setContent(partial, proxyEvent["contentIndex"], TextContent(text=""))
            return TextStartEvent(contentIndex=proxyEvent["contentIndex"], partial=partial)

        case "text_delta":
            content = _contentAt(partial, proxyEvent["contentIndex"])
            if content is not None and content.type == "text":
                content.text += proxyEvent["delta"]
                return TextDeltaEvent(
                    contentIndex=proxyEvent["contentIndex"],
                    delta=proxyEvent["delta"],
                    partial=partial,
                )
            raise RuntimeError("Received text_delta for non-text content")

        case "text_end":
            content = _contentAt(partial, proxyEvent["contentIndex"])
            if content is not None and content.type == "text":
                content.textSignature = proxyEvent.get("contentSignature")
                return TextEndEvent(
                    contentIndex=proxyEvent["contentIndex"],
                    content=content.text,
                    partial=partial,
                )
            raise RuntimeError("Received text_end for non-text content")

        case "thinking_start":
            _setContent(partial, proxyEvent["contentIndex"], ThinkingContent(thinking=""))
            return ThinkingStartEvent(contentIndex=proxyEvent["contentIndex"], partial=partial)

        case "thinking_delta":
            content = _contentAt(partial, proxyEvent["contentIndex"])
            if content is not None and content.type == "thinking":
                content.thinking += proxyEvent["delta"]
                return ThinkingDeltaEvent(
                    contentIndex=proxyEvent["contentIndex"],
                    delta=proxyEvent["delta"],
                    partial=partial,
                )
            raise RuntimeError("Received thinking_delta for non-thinking content")

        case "thinking_end":
            content = _contentAt(partial, proxyEvent["contentIndex"])
            if content is not None and content.type == "thinking":
                content.thinkingSignature = proxyEvent.get("contentSignature")
                return ThinkingEndEvent(
                    contentIndex=proxyEvent["contentIndex"],
                    content=content.thinking,
                    partial=partial,
                )
            raise RuntimeError("Received thinking_end for non-thinking content")

        case "toolcall_start":
            _setContent(
                partial,
                proxyEvent["contentIndex"],
                ToolCall(id=proxyEvent["id"], name=proxyEvent["toolName"], arguments={}),
            )
            partialJson[proxyEvent["contentIndex"]] = ""
            return ToolCallStartEvent(contentIndex=proxyEvent["contentIndex"], partial=partial)

        case "toolcall_delta":
            content = _contentAt(partial, proxyEvent["contentIndex"])
            if content is not None and content.type == "toolCall":
                partialJson[proxyEvent["contentIndex"]] = partialJson.get(proxyEvent["contentIndex"], "") + proxyEvent["delta"]
                parsed = parseStreamingJson(partialJson[proxyEvent["contentIndex"]])
                content.arguments = parsed if isinstance(parsed, dict) else {}
                partial.content[proxyEvent["contentIndex"]] = content.model_copy()  # Trigger reactivity
                return ToolCallDeltaEvent(
                    contentIndex=proxyEvent["contentIndex"],
                    delta=proxyEvent["delta"],
                    partial=partial,
                )
            raise RuntimeError("Received toolcall_delta for non-toolCall content")

        case "toolcall_end":
            content = _contentAt(partial, proxyEvent["contentIndex"])
            if content is not None and content.type == "toolCall":
                # `Object.assign(content, proxyEvent.toolCall)`: the fields the server sent replace the block's.
                finalized = ToolCall.model_validate(proxyEvent["toolCall"])
                for key in proxyEvent["toolCall"]:
                    if key in ToolCall.model_fields:
                        setattr(content, key, getattr(finalized, key))
                raw = partialJson.pop(proxyEvent["contentIndex"], None)
                # MISAKA fork: the server's final arguments win; only an empty object is checked against the streamed
                # text, so a malformed stream still surfaces as `argumentsError` (as `pi_messages` does).
                if raw and content.argumentsError is None and not content.arguments:
                    StreamingArgs(raw).finish_into(content)
                return ToolCallEndEvent(
                    contentIndex=proxyEvent["contentIndex"],
                    toolCall=content,
                    partial=partial,
                )
            return None

        case "done":
            # MISAKA fork: a tool call the server never ended keeps a streaming preview, which is not an executable
            # argument set; it gets the strict final parse (and an error receipt when the JSON is incomplete).
            for index, raw in partialJson.items():
                content = _contentAt(partial, index)
                if raw and content is not None and content.type == "toolCall":
                    StreamingArgs(raw).finish_into(content)
            partialJson.clear()
            partial.stopReason = proxyEvent["reason"]
            partial.usage = Usage.model_validate(proxyEvent["usage"])
            if proxyEvent.get("providerThinkingLevel") is not None:
                partial.providerThinkingLevel = proxyEvent["providerThinkingLevel"]
            return DoneEvent(reason=proxyEvent["reason"], message=partial)

        case "error":
            partial.stopReason = proxyEvent["reason"]
            partial.errorMessage = proxyEvent.get("errorMessage")
            partial.usage = Usage.model_validate(proxyEvent["usage"])
            if proxyEvent.get("providerThinkingLevel") is not None:
                partial.providerThinkingLevel = proxyEvent["providerThinkingLevel"]
            return ErrorEvent(reason=proxyEvent["reason"], error=partial)

        case _:
            logger.warning("Unhandled proxy event type: %s", proxyEvent.get("type"))
            return None


__all__ = ["ProxyAssistantMessageEvent", "ProxyStreamOptions", "streamProxy"]

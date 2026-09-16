"""pi-messages API implementation, translated from pi's ``api/pi-messages.ts``.

Streams pi's own message protocol directly to a backend: the request is a single POST of
``{model, context, options}`` to ``<baseUrl>/messages``, the response is an SSE stream of
serialized assistant-message events plus a terminal ``done``/``error`` event. This is the
wire protocol spoken by the Radius gateway, but any backend implementing it can be used,
e.g. via a models.json custom provider with ``"api": "pi-messages"``.

Three things could not be copied across verbatim:

* Upstream injects a ``fetch`` implementation through ``options.fetch``. There is no
  ``fetch`` here; the equivalent seam is an ``httpx`` transport, read off the options as
  ``httpTransport`` and handed to the client. A caller (or a test) that passes one serves
  the request without a socket.
* ``JSON.stringify`` drops keys whose value is ``undefined`` and emits no whitespace.
  Python keeps ``None`` keys and ``json.dumps`` pads with spaces, so ``None`` entries are
  dropped explicitly and the separators are pinned; otherwise the backend would read an
  explicit ``null`` where upstream sends nothing.
* Upstream's in-flight message carries ``stopReason: "pending"``. misaka's ``StopReason``
  has no such member, so the placeholder is ``"stop"``; either way the value is
  overwritten by the terminal event before anything reads it as final.
"""

from __future__ import annotations

import codecs
import json
import time
from collections.abc import AsyncIterator, Mapping
from typing import Any, Literal, TypedDict

import httpx
from pydantic import BaseModel

from misaka.ai.providers._common import (
    _await_with_signal,
    _empty_usage,
    _iterate_async_iterable,
    _option,
)
from misaka.ai.providers.simple_options import build_base_options
from misaka.ai.types import (
    AssistantMessage,
    AssistantMessageEvent,
    CacheRetention,
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
from misaka.ai.utils.diagnostics import (
    AssistantMessageDiagnostic,
    append_assistant_message_diagnostic,
    create_assistant_message_diagnostic,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream, spawn_stream_task
from misaka.ai.utils.headers import headers_to_record
from misaka.ai.utils.json_parse import StreamingArgs, parse_streaming_json
from misaka.ai.utils.provider_env import get_provider_env_value
from misaka.core.http_dispatcher import createHttpxIdleTimeout
from misaka.utils.values import maybe_await, signal_aborted

DIAGNOSTIC_BODY_MAX_LENGTH = 8192
"""Characters of a failed response body kept in a diagnostic before it is elided.

Compared against ``len(str)``, so the unit is code points here and UTF-16 code units in
upstream's ``value.length`` -- neither is bytes.
"""


class PiMessagesOptions(TypedDict, total=False):
    """The options a pi-messages request reads. Upstream's ``PiMessagesOptions``.

    A ``StreamOptions`` model is accepted just as well; every read goes through
    ``_option``, which handles both. Listed are the keys this module actually reads --
    upstream's, plus ``httpTransport``, which stands in for upstream's ``fetch``.
    """

    apiKey: str
    signal: Any
    headers: dict[str, str | None]
    env: dict[str, str]
    onPayload: Any
    onResponse: Any
    temperature: float
    maxTokens: int
    cacheRetention: CacheRetention
    sessionId: str
    reasoning: str
    timeoutMs: int
    toolChoice: str | dict[str, Any]
    debug: bool
    """Ask the backend for debug metadata (e.g. routing response headers)."""
    httpTransport: httpx.AsyncBaseTransport
    """misaka's stand-in for upstream's ``fetch`` injection; see the module docstring."""


class PiMessagesRewriteImpact(TypedDict):
    """Impact summary of a server-side message rewrite (e.g. a gateway policy)."""

    policyId: str
    policyVersion: int
    changed: bool
    tokenCountChange: int
    messageCountChange: int
    systemPromptChanged: bool


class PiMessagesResponseError(RuntimeError):
    """A non-2xx response from a pi-messages backend, with the body kept for diagnostics."""

    def __init__(self, message: str, code: str | None, diagnostic_details: dict[str, Any]) -> None:
        super().__init__(message)
        # ``extract_diagnostic_error`` reads ``name`` and ``code`` off the exception the way
        # upstream's reads them off an ``Error``; setting both keeps the recorded diagnostic
        # identical to upstream's, which relies on ``this.name = "PiMessagesResponseError"``.
        self.name = "PiMessagesResponseError"
        self.code = code
        self.diagnosticDetails = diagnostic_details


def _now_ms() -> int:
    return int(time.time() * 1000)


def _provider_headers_to_record(headers: Any) -> dict[str, str] | None:
    """Upstream's ``providerHeadersToRecord``: drop null-valued entries, keep nothing empty."""
    if not headers:
        return None
    record = {str(key): str(value) for key, value in dict(headers).items() if value is not None}
    return record or None


def _parse_pi_messages_error_body(body: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    # Upstream requires ``error`` to be a non-null, non-array object before it trusts the
    # body; a dict is exactly that set in Python.
    return parsed if isinstance(parsed.get("error"), dict) else None


def _truncate_diagnostic_string(value: str) -> str:
    if len(value) > DIAGNOSTIC_BODY_MAX_LENGTH:
        return f"{value[:DIAGNOSTIC_BODY_MAX_LENGTH]}…"
    return value


def _format_pi_messages_response_error(
    status: int,
    status_text: str,
    body: str,
    error_body: dict[str, Any] | None,
) -> str:
    error = error_body.get("error", {}) if error_body else {}
    message = error.get("message") if isinstance(error.get("message"), str) else None
    code = error.get("code") if isinstance(error.get("code"), str) else None
    # ``??`` in upstream, not ``||``: an empty structured message still wins over the body.
    suffix = body if message is None else message
    code_suffix = f" ({code})" if code else ""
    return f"{status} {status_text}: {suffix}{code_suffix}"


def _create_pi_messages_response_error(
    model: Model,
    url: str,
    status: int,
    status_text: str,
    body: str,
) -> PiMessagesResponseError:
    error_body = _parse_pi_messages_error_body(body)
    error = error_body.get("error") if error_body else None
    code = error.get("code") if isinstance(error, dict) and isinstance(error.get("code"), str) else None
    details: dict[str, Any] = {
        "version": 1,
        "provider": model.provider,
        "model": model.id,
        "url": url,
        "status": status,
        "statusText": status_text,
        "error": error,
        # Upstream keeps the raw body only when it could not be parsed into ``error``.
        "body": None if error_body else _truncate_diagnostic_string(body),
        "timestampMs": _now_ms(),
    }
    # Upstream sets both keys and lets ``JSON.stringify`` drop the ``undefined`` one; dropping
    # them here is what makes the serialized diagnostic come out the same.
    return PiMessagesResponseError(
        _format_pi_messages_response_error(status, status_text, body, error_body),
        code,
        {key: value for key, value in details.items() if value is not None},
    )


def _append_rewrite_diagnostic(message: AssistantMessage, rewrite: PiMessagesRewriteImpact | None) -> None:
    if not rewrite:
        return
    append_assistant_message_diagnostic(
        message,
        AssistantMessageDiagnostic(
            type="pi_messages_rewrite",
            timestamp=_now_ms(),
            details=dict(rewrite),
        ),
    )


def _create_error_event(model: Model, error: Any, aborted: bool) -> ErrorEvent:
    reason: Literal["aborted", "error"] = "aborted" if aborted else "error"
    assistant_message = AssistantMessage(
        content=[],
        api=model.api,
        provider=model.provider,
        model=model.id,
        usage=_empty_usage(),
        stopReason=reason,
        errorMessage=str(error),
        timestamp=_now_ms(),
    )
    if not aborted and isinstance(error, PiMessagesResponseError):
        append_assistant_message_diagnostic(
            assistant_message,
            create_assistant_message_diagnostic(
                "pi_messages_response_failure",
                error,
                error.diagnosticDetails,
            ),
        )
    return ErrorEvent(reason=reason, error=assistant_message)


def _coerce_usage(current: Usage, usage: Mapping[str, Any]) -> Usage:
    """A wire ``usage`` object folded onto the current one, unmodelled keys discarded.

    Upstream (``api/pi-messages.ts:191-198`` and ``:199-206``) simply assigns
    ``event.usage`` onto the message, so a backend that starts reporting an extra
    counter costs it nothing. ``Usage`` is ``extra="forbid"``, so validating the wire
    object directly raises on that extra counter, the ValidationError escapes the
    converter into the outer ``except Exception``, and a response whose text had already
    streamed in full is replaced wholesale by a synthetic error event -- the real
    ``stopReason`` and every content block lost to one unknown field. Keeping only the
    modelled keys, and leaving the zeroed default in place for a key the backend omits,
    keeps the response. It is the same rule ``_EventConverter.convert`` applies to an
    unmodelled ToolCall key in its ``toolcall_end`` branch.
    """
    merged = current.model_dump()
    for key, value in usage.items():
        if key not in Usage.model_fields:
            continue
        if key == "cost":
            # ``cost`` is a nested extra="forbid" model, so a new per-counter price would
            # blow up in exactly the same way; filter it by the same rule.
            if isinstance(value, Mapping):
                merged["cost"].update({k: v for k, v in value.items() if k in UsageCost.model_fields})
            continue
        merged[key] = value
    return Usage.model_validate(merged)


def _resolve_cache_retention(cache_retention: Any, env: Any) -> CacheRetention | None:
    if cache_retention:
        return cache_retention
    # Only ``PI_CACHE_RETENTION=long`` is mapped; any other value leaves this unset.
    return "long" if get_provider_env_value("PI_CACHE_RETENTION", env) == "long" else None


def _serialize_context(context: Context | Mapping[str, Any]) -> Any:
    """The wire form of a ``Context``.

    ``exclude_none`` is what reproduces ``JSON.stringify``'s treatment of ``undefined``
    fields. Tools are rebuilt rather than dumped because ``Tool.parameters`` may hold a
    pydantic model *class*, which has no JSON form until ``parameters_json_schema()``
    turns it into the schema the wire actually carries.
    """
    if not isinstance(context, BaseModel):
        return context
    # Keep native parse diagnostics in history, not in the upstream wire protocol.
    payload = context.model_dump(mode="json", exclude_none=True, exclude={
        "tools": True,
        "messages": {"__all__": {"content": {"__all__": {"argumentsError"}}}},
    })
    tools = getattr(context, "tools", None)
    # ``is not None``, not truthiness: upstream hands the whole context to JSON.stringify,
    # which writes ``"tools":[]`` for an empty list and omits the key only for undefined.
    # Dropping the key for an empty list tells the backend "no opinion" where the caller
    # said "no tools" -- a backend that falls back to a default tool set would then get
    # the opposite of what was asked.
    if tools is not None:
        payload["tools"] = [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters_json_schema(),
            }
            for tool in tools
        ]
    return payload


def _parse_pi_messages_event(raw: str) -> dict[str, Any] | None:
    """One SSE block to one protocol event, or ``None`` when the block carries no event."""
    data: str | None = None
    for line in raw.split("\n"):
        if line.startswith("data:"):
            # Upstream reads the *first* ``data:`` line only
            # (``.split("\n").find(line => line.startsWith("data:"))``, pi-messages.ts:304-308).
            data = line[5:].strip()
            break
    if not data or data == "[DONE]":
        return None
    parsed = json.loads(data)
    return parsed if isinstance(parsed, dict) else None


async def _read_pi_messages_events(response: httpx.Response, signal: Any) -> AsyncIterator[dict[str, Any]]:
    # Upstream decodes with a ``TextDecoder``, which is UTF-8 and holds a partial multi-byte
    # character back across chunk boundaries; an incremental codec is the same machine.
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buffer = ""

    def drain(buffer: str) -> tuple[list[dict[str, Any]], str]:
        events: list[dict[str, Any]] = []
        split = buffer.find("\n\n")
        while split != -1:
            event = _parse_pi_messages_event(buffer[:split])
            if event is not None:
                events.append(event)
            buffer = buffer[split + 2 :]
            split = buffer.find("\n\n")
        return events, buffer

    async for chunk in _iterate_async_iterable(response.aiter_bytes(), signal, on_abort=response.aclose):
        buffer = (buffer + decoder.decode(chunk)).replace("\r\n", "\n")
        events, buffer = drain(buffer)
        for event in events:
            yield event

    buffer = (buffer + decoder.decode(b"", final=True)).replace("\r\n", "\n")
    events, buffer = drain(buffer)
    for event in events:
        yield event

    if buffer.strip():
        event = _parse_pi_messages_event(buffer)
        if event is not None:
            yield event


class _EventConverter:
    """Serialized backend events to misaka events, folding each into one partial message."""

    def __init__(self, model: Model) -> None:
        self.partial = AssistantMessage(
            content=[],
            api=model.api,
            provider=model.provider,
            model=model.id,
            usage=_empty_usage(),
            # Upstream's "pending"; see the module docstring for why it cannot be used.
            stopReason="stop",
            timestamp=_now_ms(),
        )
        self._tool_json: dict[int, str] = {}

    def convert(self, event: Mapping[str, Any]) -> AssistantMessageEvent | None:
        event_type = event.get("type")

        if event_type == "done":
            for index, raw in self._tool_json.items():
                if raw:
                    StreamingArgs(raw).finish_into(self._content_at(index))
            self._tool_json.clear()
            self._apply_terminal(event)
            return DoneEvent(reason=event["reason"], message=self.partial)
        if event_type == "error":
            self._apply_terminal(event)
            self.partial.errorMessage = event.get("errorMessage")
            return ErrorEvent(reason=event["reason"], error=self.partial)
        if event_type == "start":
            return StartEvent(partial=self.partial)

        index = event.get("contentIndex")

        if event_type == "text_start":
            self._set_content(index, TextContent(text=""))
            return TextStartEvent(contentIndex=index, partial=self.partial)
        if event_type == "text_delta":
            self._content_at(index).text += event["delta"]
            return TextDeltaEvent(contentIndex=index, delta=event["delta"], partial=self.partial)
        if event_type == "text_end":
            block = self._content_at(index)
            block.text = event["content"]
            block.textSignature = event.get("contentSignature")
            # The signature rides on the content block, not on the event: misaka's
            # TextEndEvent has no ``contentSignature`` field, and neither does upstream's
            # -- it only survives the spread because TS erases the extra key.
            return TextEndEvent(contentIndex=index, content=event["content"], partial=self.partial)
        if event_type == "thinking_start":
            self._set_content(index, ThinkingContent(thinking=""))
            return ThinkingStartEvent(contentIndex=index, partial=self.partial)
        if event_type == "thinking_delta":
            self._content_at(index).thinking += event["delta"]
            return ThinkingDeltaEvent(contentIndex=index, delta=event["delta"], partial=self.partial)
        if event_type == "thinking_end":
            block = self._content_at(index)
            block.thinking = event["content"]
            block.thinkingSignature = event.get("contentSignature")
            block.redacted = event.get("redacted")
            return ThinkingEndEvent(contentIndex=index, content=event["content"], partial=self.partial)
        if event_type == "toolcall_start":
            self._set_content(index, ToolCall(id=event["id"], name=event["toolName"], arguments={}))
            self._tool_json[index] = ""
            return ToolCallStartEvent(contentIndex=index, partial=self.partial)
        if event_type == "toolcall_delta":
            raw = f"{self._tool_json.get(index, '')}{event['delta']}"
            self._tool_json[index] = raw
            self._content_at(index).arguments = parse_streaming_json(raw)
            return ToolCallDeltaEvent(contentIndex=index, delta=event["delta"], partial=self.partial)
        if event_type == "toolcall_end":
            block = self._content_at(index)
            # Upstream's ``Object.assign`` copies every own property; a pydantic model
            # rejects unknown attributes, so an unmodelled key is dropped rather than
            # turning the whole response into an error.
            for key, value in dict(event["toolCall"]).items():
                if key in ToolCall.model_fields:
                    setattr(block, key, value)
            raw = self._tool_json.pop(index, None)
            if raw and block.argumentsError is None:
                StreamingArgs(raw).finish_into(block)
            return ToolCallEndEvent(contentIndex=index, toolCall=block, partial=self.partial)

        # Upstream's union is closed, so an unknown type is a protocol violation. TS still
        # forwards it; misaka's stream is typed and cannot carry it, so it is dropped.
        return None

    def _apply_terminal(self, event: Mapping[str, Any]) -> None:
        self.partial.stopReason = event["reason"]
        usage = event.get("usage")
        if isinstance(usage, Mapping):
            # ``AssistantMessage.usage`` is not optional here, where upstream would happily
            # assign ``undefined``; a backend that omits usage leaves the zeroed one, and
            # so does one that sends something that is not an object at all.
            self.partial.usage = _coerce_usage(self.partial.usage, usage)
        self.partial.responseId = event.get("responseId")
        _append_rewrite_diagnostic(self.partial, event.get("rewrite"))

    def _check_index(self, index: Any) -> int:
        # Anything that is not a non-negative int is rejected here, so it never reaches the
        # list indexing in ``_content_at`` / ``_set_content`` below.
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise IndexError(f"pi-messages event has an invalid contentIndex: {index!r}")
        return index

    def _content_at(self, index: Any) -> Any:
        position = self._check_index(index)
        if position >= len(self.partial.content):
            raise IndexError(f"pi-messages event references missing content index {position}")
        return self.partial.content[position]

    def _set_content(self, index: Any, block: Any) -> None:
        position = self._check_index(index)
        content = self.partial.content
        # A TS array grows with holes when written past its end; a list raises. Indices
        # arrive in order in practice, so the padding only shows up on a malformed stream,
        # where an empty text block is the closest representable stand-in for a hole.
        while len(content) <= position:
            content.append(TextContent(text=""))
        content[position] = block


def stream_pi_messages(
    model: Model,
    context: Context,
    options: StreamOptions | PiMessagesOptions | None = None,
) -> AssistantMessageEventStream:
    event_stream = AssistantMessageEventStream()
    converter = _EventConverter(model)

    async def run() -> None:
        signal = _option(options, "signal")
        try:
            api_key = _option(options, "apiKey")
            if not api_key:
                raise RuntimeError(f'No API key provided for provider "{model.provider}"')

            url = f"{model.baseUrl.rstrip('/')}/messages"
            params = {"debug": "1"} if _option(options, "debug") else None

            request_options = {
                "temperature": _option(options, "temperature"),
                "maxTokens": _option(options, "maxTokens"),
                "reasoning": _option(options, "reasoning"),
                "cacheRetention": _resolve_cache_retention(
                    _option(options, "cacheRetention"), _option(options, "env")
                ),
                "sessionId": _option(options, "sessionId"),
                "toolChoice": _option(options, "toolChoice"),
            }
            payload: Any = {
                "model": model.id,
                "context": _serialize_context(context),
                "options": {key: value for key, value in request_options.items() if value is not None},
            }
            on_payload = _option(options, "onPayload")
            if callable(on_payload):
                next_payload = await maybe_await(on_payload(payload, model))
                if next_payload is not None:
                    payload = next_payload

            request_headers = {
                "authorization": f"Bearer {api_key}",
                "accept": "text/event-stream",
                "content-type": "application/json",
            }
            # Caller headers land last, so they override the defaults, as upstream's spread does.
            request_headers.update(_provider_headers_to_record(_option(options, "headers")) or {})

            transport = _option(options, "httpTransport")
            client = httpx.AsyncClient(
                transport=transport,
                timeout=createHttpxIdleTimeout(_option(options, "timeoutMs")),
                follow_redirects=True,
            )
            try:
                request = client.build_request(
                    "POST",
                    url,
                    params=params,
                    headers=request_headers,
                    # Pinned separators and ``ensure_ascii=False``: ``json.dumps`` pads with
                    # spaces and escapes non-ASCII to ``\uXXXX``, ``JSON.stringify`` does
                    # neither, and the two bodies must be byte-identical. Escaping would also
                    # roughly double the request size for a CJK conversation.
                    content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                )
                # Upstream hands the signal to ``fetch``; httpx has no such parameter, so the
                # send is raced against the signal instead.
                response = await _await_with_signal(client.send(request, stream=True), signal)
                try:
                    on_response = _option(options, "onResponse")
                    if callable(on_response):
                        await maybe_await(
                            on_response(
                                {"status": response.status_code, "headers": headers_to_record(response.headers)},
                                model,
                            )
                        )

                    if not response.is_success:
                        body = (await response.aread()).decode("utf-8", errors="replace")
                        raise _create_pi_messages_response_error(
                            model,
                            str(request.url),
                            response.status_code,
                            response.reason_phrase,
                            body,
                        )

                    terminated = False
                    async for pi_event in _read_pi_messages_events(response, signal):
                        event = converter.convert(pi_event)
                        if event is None:
                            continue
                        event_stream.push(event)
                        if event.type in {"done", "error"}:
                            terminated = True
                            break

                    if not terminated:
                        raise RuntimeError(f"{model.provider} stream ended without a terminal event")
                finally:
                    await response.aclose()
            finally:
                # Only a client that owns its transport is closed. ``AsyncClient.aclose``
                # closes the transport it was handed, and upstream never disposes of the
                # ``fetch`` the caller injected -- a caller that reuses one transport across
                # calls (a connection pool, a test's MockTransport) would find it shut after
                # the first request.
                if transport is None:
                    await client.aclose()
        except Exception as error:  # noqa: BLE001 - every failure leaves as an error event
            event_stream.push(_create_error_event(model, error, signal_aborted(signal)), cause=error)
        finally:
            # The terminal event already resolved the stream's result; this only releases a
            # consumer that is still waiting on the queue.
            event_stream.end()

    spawn_stream_task(run(), stream=event_stream)
    return event_stream


def stream_simple_pi_messages(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | PiMessagesOptions | None = None,
) -> AssistantMessageEventStream:
    # Upstream spreads the caller's options wholesale and then re-states the three it
    # narrows. A Mapping is already that spread; a typed options model goes through
    # ``build_base_options``, which is misaka's version of the same copy.
    if isinstance(options, Mapping):
        base: dict[str, Any] = dict(options)
    else:
        base = build_base_options(model, context, options, _option(options, "apiKey")).model_dump()
        # Neither field is part of StreamOptions, so the copy above cannot carry them.
        base["env"] = _option(options, "env")
        base["httpTransport"] = _option(options, "httpTransport")

    return stream_pi_messages(
        model,
        context,
        {
            **base,
            "reasoning": _option(options, "reasoning"),
            "toolChoice": _option(options, "toolChoice"),
            "debug": _option(options, "debug"),
        },
    )


__all__ = [
    "PiMessagesOptions",
    "PiMessagesResponseError",
    "PiMessagesRewriteImpact",
    "stream_pi_messages",
    "stream_simple_pi_messages",
]

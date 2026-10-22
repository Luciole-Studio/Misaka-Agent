"""The Streamable HTTP transport: JSON-RPC over POST with JSON or SSE responses, the optional
server-to-client GET stream, session and protocol-version headers, SSE resumption, and bearer auth
that hands a 401 to the auth provider once.

Translated from pi's ``packages/mcp/src/transports/streamable-http.ts``.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Literal, NotRequired, Required, TypedDict

import httpx

from misaka.mcp.auth_provider import AuthProvider, McpFetch, UnauthorizedContext
from misaka.mcp.protocol.jsonrpc import (
    JSON_RPC_ERROR_CODES,
    JsonRpcId,
    JsonRpcMessage,
    McpAbortError,
    McpConnectionClosedError,
    isJsonRpcRequest,
    isJsonRpcResponse,
    parseJsonRpcMessage,
    toError,
)
from misaka.mcp.transports.transport import (
    DEFAULT_MAX_MESSAGE_BYTES,
    McpTransportSendOptions,
    TransportEvents,
)
from misaka.utils import whatwg_fetch as _fetch
from misaka.utils.js import (
    AbortController,
    AbortSignal,
    Utf8Decoder,
    addAbortListener,
    isAborted,
    jsLength,
    jsNumber,
    jsonParse,
    jsonStringify,
    jsSlice,
    jsTrim,
    spawn,
    throwIfAborted,
)
from misaka.utils.whatwg_url import URL

MAX_ERROR_BODY_BYTES: Final = 8 * 1024
ERROR_MESSAGE_BODY_CHARS: Final = 500
DEFAULT_RECONNECT_INITIAL_DELAY_MS: Final = 1_000
DEFAULT_RECONNECT_MAX_DELAY_MS: Final = 30_000
DEFAULT_RECONNECT_MAX_RETRIES: Final = 5


class SseEvent(TypedDict):
    event: NotRequired[str]
    data: str
    id: NotRequired[str]


class ConsumeSseOptions(TypedDict, total=False):
    maxEventBytes: int
    onEvent: Required[Callable[[SseEvent], None]]
    # Called for every `id` field, including events without data (for example resumption priming events).
    onId: Callable[[str], None]
    # Called for every valid `retry` field, in milliseconds.
    onRetry: Callable[[float], None]


async def consumeSseStream(stream: AsyncIterator[bytes], options: ConsumeSseOptions) -> None:
    decoder = Utf8Decoder()
    buffered = ""
    eventName: str | None = None
    eventId: str | None = None
    dataLines: list[str] = []
    # Bytes of the pending event's data, including the "\n" joins, so events streamed as many
    # short `data:` lines without a terminating blank line cannot grow without bound.
    dataBytes = 0
    maxEventBytes = options.get("maxEventBytes")
    if maxEventBytes is None:
        maxEventBytes = DEFAULT_MAX_MESSAGE_BYTES

    def dispatch() -> None:
        nonlocal eventName, eventId, dataLines, dataBytes
        if len(dataLines) == 0:
            eventName = None
            eventId = None
            return
        data = "\n".join(dataLines)
        event: SseEvent = {**({"event": eventName} if eventName else {}), "data": data}  # type: ignore[typeddict-item]
        if eventId:
            event["id"] = eventId
        options["onEvent"](event)
        eventName = None
        eventId = None
        dataLines = []
        dataBytes = 0

    def processLine(rawLine: str) -> None:
        nonlocal eventName, eventId, dataBytes
        line = rawLine.removesuffix("\r")
        if line == "":
            dispatch()
            return
        if line.startswith(":"):
            return
        colon = line.find(":")
        field = line if colon < 0 else line[:colon]
        value = "" if colon < 0 else line[colon + 1 :]
        value = value.removeprefix(" ")
        if field == "data":
            dataBytes += len(value.encode("utf-8")) + (1 if len(dataLines) > 0 else 0)
            if dataBytes > maxEventBytes:
                raise RuntimeError(f"MCP SSE event exceeds {maxEventBytes} bytes")
            dataLines.append(value)
        elif field == "event":
            eventName = value
        elif field == "id" and "\0" not in value:
            eventId = value
            onId = options.get("onId")
            if onId is not None:
                onId(value)
        elif field == "retry" and re.fullmatch(r"[0-9]+", value):
            onRetry = options.get("onRetry")
            if onRetry is not None:
                onRetry(jsNumber(value))

    async for value in stream:
        buffered += decoder.decode(value)
        newline = buffered.find("\n")
        while newline >= 0:
            processLine(buffered[:newline])
            buffered = buffered[newline + 1 :]
            newline = buffered.find("\n")
        if len(buffered.encode("utf-8")) > maxEventBytes:
            raise RuntimeError(f"MCP SSE event exceeds {maxEventBytes} bytes")
    buffered += decoder.decode(b"", final=True)
    if buffered:
        processLine(buffered)
    dispatch()
    # `reader.releaseLock()`: the stream stays the caller's.


class StreamableHttpReconnectOptions(TypedDict, total=False):
    # Delay before the first reconnection attempt, unless the server sent a `retry` field. Default: 1000.
    initialDelayMs: float
    # Upper bound for the exponential backoff. Default: 30000.
    maxDelayMs: float
    # Consecutive failed attempts before giving up on a stream. Default: 5.
    maxRetries: int


class StreamableHttpTransportOptions(TypedDict, total=False):
    url: Required[str | URL]
    headers: Mapping[str, str] | None
    fetch: McpFetch
    # Open the server-to-client GET stream after initialization. Default: true.
    openGetStream: bool
    maxMessageBytes: int
    authProvider: AuthProvider
    # Reconnection of dropped SSE streams (the GET stream, and response streams that carry event IDs).
    reconnect: StreamableHttpReconnectOptions


class McpHttpError(Exception):
    def __init__(self, status: int, message: str, body: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.name = "McpHttpError"
        self.status = status
        self.body = body


class McpAuthRequiredError(McpHttpError):
    def __init__(self, response: _fetch.Response, body: str = "") -> None:
        super().__init__(401, "MCP server requires authentication", body)
        self.name = "McpAuthRequiredError"
        self.wwwAuthenticate: str | None = response.headers.get("www-authenticate")


class McpSessionExpiredError(McpHttpError):
    def __init__(self, body: str = "") -> None:
        super().__init__(404, "MCP session expired", body)
        self.name = "McpSessionExpiredError"


def contentType(response: _fetch.Response) -> str | None:
    value = response.headers.get("content-type")
    return jsTrim(value.split(";", 1)[0]).lower() if value is not None else None


def needsAuthorization(response: _fetch.Response) -> bool:
    """401, or 403 with an ``insufficient_scope`` bearer challenge (step-up authorization)."""
    if response.status == 401:
        return True
    if response.status != 403:
        return False
    return (
        re.search(
            r'(?:^|[\s,])error="?insufficient_scope"?', response.headers.get("www-authenticate") or "", re.IGNORECASE
        )
        is not None
    )


def isTransientStatus(status: int) -> bool:
    """Statuses worth retrying when a stream fails to (re)open."""
    return status == 408 or status == 429 or status >= 500


async def discard(response: _fetch.Response) -> None:
    if response.body is not None:
        with contextlib.suppress(Exception):
            await response.body.cancel()


def describeHttpFailure(status: int, body: str) -> str:
    text = jsTrim(body)
    snippet = f"{jsSlice(text, 0, ERROR_MESSAGE_BODY_CHARS - 3)}..." if jsLength(text) > ERROR_MESSAGE_BODY_CHARS else text
    return f"MCP HTTP request failed with status {status}{f': {snippet}' if snippet else ''}"


@dataclass(slots=True)
class StreamCursor:
    lastEventId: str | None
    retryMs: float | None
    # Whether the stream delivered any event since it was (re)opened.
    received: bool


def _headerValue(value: str) -> str:
    """A header value as WHATWG ``Headers`` stores it: leading and trailing HTTP whitespace (tab, LF, CR,
    space) removed."""
    return value.strip("\t\n\r ")


class StreamableHttpTransport(TransportEvents):
    def __init__(self, options: StreamableHttpTransportOptions) -> None:
        super().__init__()
        headers = options.get("headers")
        self.options: Mapping[str, Any] = MappingProxyType({
            **options,
            "headers": dict(headers) if headers is not None else None,
        })
        self.url = URL(options["url"])
        fetch = options.get("fetch")
        # MISAKA fork: without an injected fetch the transport keeps a connection pool of its own, as
        # undici's dispatcher does for `globalThis.fetch`, and closes it in `close()`.
        self._client: httpx.AsyncClient | None = None if fetch is not None else _fetch.createClient()
        if fetch is None:
            client = self._client

            async def defaultFetch(input: str | URL, init: _fetch.RequestInit | None = None) -> _fetch.Response:
                assert client is not None
                throwIfAborted((init or {}).get("signal"))
                if client.is_closed:
                    # A fetch after `close()` (an auth refresh still running) fails the way fetch fails on a
                    # destroyed dispatcher, which the OAuth flow and the retry logic key on.
                    raise TypeError("fetch failed")
                return await _fetch.fetchWith(client, input, init)

            fetch = defaultFetch
        self._fetch: McpFetch = fetch
        # MISAKA fork: the auth provider's refresh may still run after `close()` has shut the pool, so
        # it gets a fetch that opens its own connection, as undici's global fetch is not tied to this
        # transport. An injected fetch is shared as upstream shares it.
        self._authFetch: McpFetch = _fetch.fetch if options.get("fetch") is None else fetch
        self._controller = AbortController()
        self._started = False
        self._closed = False
        self._sessionIdValue: str | None = None
        self._protocolVersion: str | None = None
        self._getStreamStarted = False
        # Access token of the latest request, which `close()` reuses instead of asking the auth provider.
        self._lastToken: str | None = None
        # MISAKA fork: asyncio holds tasks weakly; the stream readers live here until they finish.
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def sessionId(self) -> str | None:
        return self._sessionIdValue

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("MCP Streamable HTTP transport already started")
        if self._closed:
            raise McpConnectionClosedError()
        self._started = True

    def setProtocolVersion(self, version: str) -> None:
        self._protocolVersion = version

    async def send(self, message: JsonRpcMessage, options: McpTransportSendOptions | None = None) -> None:
        signal = (options or {}).get("signal")
        if not self._started or self._closed:
            raise McpConnectionClosedError()
        response = await self._authorizedFetch(
            "POST",
            {
                "headers": {"accept": "application/json, text/event-stream", "content-type": "application/json"},
                "body": jsonStringify(message),
            },
            signal,
        )
        await self._checkResponse(response)
        self._captureSession(response)

        if not isJsonRpcRequest(message):
            # Notifications and responses are acknowledged with 202 and carry no reply; ignore any body.
            await discard(response)
            # The server-to-client stream may only open once the session is initialized.
            if "method" in message and message["method"] == "notifications/initialized":  # type: ignore[typeddict-item]
                self._startGetStream()
            return
        if response.status == 202 or response.status == 204:
            # MISAKA fork: the unread body is closed, so its connection returns to the pool.
            await discard(response)
            raise McpHttpError(response.status, f"MCP server accepted request {message['method']} without a response")
        type = contentType(response)
        if type == "application/json":
            body: Any = await response.json()
            for item in body if isinstance(body, list) else [body]:
                self._emitMessage(parseJsonRpcMessage(item))
            return
        if type == "text/event-stream" and response.body is not None:
            spawn(self._consumeResponseStream(response.body, message["id"]), self._tasks)
            return
        await discard(response)
        raise McpHttpError(response.status, f"Unsupported MCP response content type: {type if type is not None else 'missing'}")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._controller.abort()
        if self._started and self._sessionIdValue:
            # Best effort: the session expires on the server anyway. The auth provider may refresh tokens
            # over the network, so it is not asked here and closing never waits for a refresh.
            try:
                response = await self._fetch(
                    self.url,
                    {
                        "method": "DELETE",
                        "headers": self._buildHeaders({}, self._lastToken),
                        "signal": AbortSignal.timeout(1_000),
                    },
                )
                await discard(response)
            except Exception:  # noqa: BLE001, S110 - upstream `catch {}`
                pass
        self._emitClose()
        # MISAKA fork: then the transport's connection pool closes, after its stream readers. Those the abort
        # did not end (one waiting on the auth provider) are cancelled: closing never waits for a refresh, and
        # a refresh is shielded inside the provider, so it still saves its tokens.
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        if self._client is not None:
            await self._client.aclose()

    async def _authorizedFetch(
        self, method: Literal["GET", "POST"], init: dict[str, Any], signal: Any = None
    ) -> _fetch.Response:
        """Fetch with auth headers. A 401 (or a 403 asking for more scope) is handed to the auth provider
        once, and the request is retried with whatever credentials it left behind.

        ``signal`` only stops dispatch: getting a token or the authorization recovery can take a while, and a
        request its sender gave up on in the meantime must not reach the server. It does not abort the shared
        recovery, which other requests may wait for."""
        authProvider = self.options.get("authProvider")
        onUnauthorized = getattr(authProvider, "onUnauthorized", None) if authProvider is not None else None
        attempt = 0
        while True:
            headers, token = await self._headers(init["headers"])
            if isAborted(signal):
                raise McpAbortError()
            response = await self._fetch(
                self.url,
                {"method": method, "headers": headers, "body": init.get("body"), "signal": self._controller.signal},
            )
            if attempt > 0 or onUnauthorized is None or not needsAuthorization(response):
                return response
            try:
                context: UnauthorizedContext = {"response": response, "serverUrl": self.url, "fetch": self._authFetch}
                if token is not None:
                    context["token"] = token
                await onUnauthorized(context)
            finally:
                await discard(response)
            attempt += 1

    async def _headers(self, extra: Mapping[str, str] | None = None) -> tuple[httpx.Headers, str | None]:
        authProvider = self.options.get("authProvider")
        token = await authProvider.token() if authProvider is not None else None
        self._lastToken = token
        return self._buildHeaders(extra if extra is not None else {}, token), (token if token else None)

    def _buildHeaders(self, extra: Mapping[str, str], token: str | None) -> httpx.Headers:
        headers = httpx.Headers({name: _headerValue(value) for name, value in (self.options.get("headers") or {}).items()})
        for name, value in extra.items():
            headers[name] = _headerValue(value)
        if self._sessionIdValue:
            headers["Mcp-Session-Id"] = _headerValue(self._sessionIdValue)
        if self._protocolVersion:
            headers["MCP-Protocol-Version"] = _headerValue(self._protocolVersion)
        if token:
            headers["Authorization"] = _headerValue(f"Bearer {token}")
        return headers

    def _captureSession(self, response: _fetch.Response) -> None:
        sessionId = response.headers.get("mcp-session-id")
        if sessionId:
            self._sessionIdValue = sessionId

    async def _checkResponse(self, response: _fetch.Response) -> None:
        if response.ok:
            return
        try:
            body = jsSlice(await response.text(), 0, MAX_ERROR_BODY_BYTES)
        except Exception:  # noqa: BLE001 - upstream `.catch(() => "")`
            body = ""
        if response.status == 401:
            raise McpAuthRequiredError(response, body)
        if response.status == 404 and self._sessionIdValue:
            raise McpSessionExpiredError(body)
        raise McpHttpError(response.status, describeHttpFailure(response.status, body), body)

    async def _consumeSse(
        self,
        stream: AsyncIterator[bytes],
        cursor: StreamCursor,
        onMessage: Callable[[JsonRpcMessage], None] | None = None,
    ) -> None:
        def onId(id: str) -> None:
            cursor.lastEventId = id

        def onRetry(delayMs: float) -> None:
            cursor.retryMs = delayMs

        def onEvent(event: SseEvent) -> None:
            cursor.received = True
            # Events without data prime resumption; other event types are not JSON-RPC.
            if not jsTrim(event["data"]) or ("event" in event and event["event"] != "message"):
                return
            try:
                message = parseJsonRpcMessage(jsonParse(event["data"]))
            except Exception as error:  # noqa: BLE001 - reported, as upstream
                self._emitError(error)
                return
            if onMessage is not None:
                onMessage(message)
            self._emitMessage(message)

        maxMessageBytes = self.options.get("maxMessageBytes")
        try:
            await consumeSseStream(
                stream,
                {
                    "maxEventBytes": maxMessageBytes if maxMessageBytes is not None else DEFAULT_MAX_MESSAGE_BYTES,
                    "onId": onId,
                    "onRetry": onRetry,
                    "onEvent": onEvent,
                },
            )
        finally:
            # MISAKA fork: an httpx body holds a pooled connection until it is closed, where a released
            # web stream is left to the garbage collector.
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                await aclose()

    async def _consumeResponseStream(self, body: AsyncIterator[bytes], requestId: JsonRpcId) -> None:
        """Read the SSE stream answering one request. When the stream ends or breaks before the response
        arrives and the server assigned event IDs, resume it with GET and ``Last-Event-ID``, as the
        server may close response streams at will. Otherwise only this request fails."""
        cursor = StreamCursor(lastEventId=None, retryMs=None, received=False)
        answered = False

        def onMessage(message: JsonRpcMessage) -> None:
            nonlocal answered
            if isJsonRpcResponse(message) and message["id"] == requestId:
                answered = True

        stream: AsyncIterator[bytes] | None = body
        failure: BaseException | None = None
        attempt = 0
        while True:
            if stream is not None:
                try:
                    await self._consumeSse(stream, cursor, onMessage)
                    failure = None
                except Exception as error:  # noqa: BLE001 - upstream `catch (error) { failure = error }`
                    failure = error
            if answered or self._closed:
                return
            if failure is not None and not self._isRetryable(failure):
                break
            if cursor.lastEventId is None or attempt >= self._maxRetries():
                break
            if cursor.received:
                attempt = 0
            cursor.received = False
            delay = self._reconnectDelay(attempt, cursor.retryMs)
            attempt += 1
            if not await self._sleep(delay):
                return
            try:
                stream = await self._openSseStream(cursor.lastEventId)
            except Exception as error:  # noqa: BLE001 - upstream `catch (error)`
                failure = error
                if not self._isRetryable(error):
                    break
                stream = None
        if self._closed:
            return
        reason = "stream ended without a response" if failure is None else str(toError(failure))
        self._emitMessage({
            "jsonrpc": "2.0",
            "id": requestId,
            "error": {"code": JSON_RPC_ERROR_CODES.internalError, "message": f"MCP response stream failed: {reason}"},
        })

    def _startGetStream(self) -> None:
        if self.options.get("openGetStream") is False or self._getStreamStarted or self._closed:
            return
        self._getStreamStarted = True
        spawn(self._runGetStream(), self._tasks)

    async def _runGetStream(self) -> None:
        """Keep the server-to-client stream open, reconnecting with backoff when it drops."""
        cursor = StreamCursor(lastEventId=None, retryMs=None, received=False)
        attempt = 0
        while not self._closed:
            try:
                stream = await self._openSseStream(cursor.lastEventId)
                # The server does not offer a GET stream.
                if stream is None:
                    return
                openedAt = time.time() * 1000
                await self._consumeSse(stream, cursor)
                # A stream that stayed up for a while counts as healthy, even if it was idle.
                if cursor.received or time.time() * 1000 - openedAt > self._maxDelay():
                    attempt = 0
            except Exception as error:  # noqa: BLE001 - upstream `catch (error)`
                if self._closed:
                    return
                if not self._isRetryable(error):
                    self._emitError(error)
                    return
            cursor.received = False
            if attempt >= self._maxRetries():
                self._emitError(RuntimeError("MCP server-to-client stream dropped and could not be reopened"))
                return
            delay = self._reconnectDelay(attempt, cursor.retryMs)
            attempt += 1
            if not await self._sleep(delay):
                return

    async def _openSseStream(self, lastEventId: str | None) -> AsyncIterator[bytes] | None:
        """Open a GET SSE stream. Resolves to ``None`` when the server answers 405 (no GET stream)."""
        headers: dict[str, str] = {"accept": "text/event-stream"}
        if lastEventId is not None:
            headers["last-event-id"] = lastEventId
        response = await self._authorizedFetch("GET", {"headers": headers})
        if response.status == 405:
            await discard(response)
            return None
        await self._checkResponse(response)
        self._captureSession(response)
        type = contentType(response)
        if type != "text/event-stream" or response.body is None:
            await discard(response)
            raise McpHttpError(
                response.status, f"Unsupported MCP GET response content type: {type if type is not None else 'missing'}"
            )
        return response.body

    def _isRetryable(self, error: Any) -> bool:
        """Network failures and transient statuses are retried; auth, session, and protocol errors are not.
        ``fetch`` reports network failures, including a connection dropped mid-body, as ``TypeError``."""
        if isinstance(error, McpHttpError):
            return isTransientStatus(error.status)
        if isinstance(error, TypeError):
            return True
        code = getattr(error, "code", None)
        return isinstance(code, str) and code.startswith(("E", "UND_ERR"))

    def _reconnectDelay(self, attempt: int, serverDelayMs: float | None) -> float:
        if serverDelayMs is not None:
            return serverDelayMs
        reconnect = self.options.get("reconnect")
        initial = reconnect.get("initialDelayMs") if reconnect is not None else None
        if initial is None:
            initial = DEFAULT_RECONNECT_INITIAL_DELAY_MS
        try:
            return min(initial * 2**attempt, self._maxDelay())
        except OverflowError:
            # MISAKA fork: JavaScript's product is `Infinity` here, and the minimum the maximum delay.
            return self._maxDelay()

    def _maxDelay(self) -> float:
        reconnect = self.options.get("reconnect")
        maxDelayMs = reconnect.get("maxDelayMs") if reconnect is not None else None
        return maxDelayMs if maxDelayMs is not None else DEFAULT_RECONNECT_MAX_DELAY_MS

    def _maxRetries(self) -> int:
        reconnect = self.options.get("reconnect")
        maxRetries = reconnect.get("maxRetries") if reconnect is not None else None
        return maxRetries if maxRetries is not None else DEFAULT_RECONNECT_MAX_RETRIES

    def _sleep(self, ms: float) -> asyncio.Future[bool]:
        """Resolves false when the transport closed while waiting."""
        signal = self._controller.signal
        loop = asyncio.get_running_loop()
        settled: asyncio.Future[bool] = loop.create_future()
        if signal.aborted:
            settled.set_result(False)
            return settled
        removeAbortListener: Callable[[], None] = lambda: None

        def onAbort() -> None:
            timer.cancel()
            if not settled.done():
                settled.set_result(False)

        def onTimer() -> None:
            removeAbortListener()
            if not settled.done():
                settled.set_result(True)

        # `setTimeout` runs a delay below 1 ms, or outside its 32-bit range, after 1 ms.
        timer = loop.call_later((ms if 1 <= ms <= 2_147_483_647 else 1) / 1000, onTimer)
        # A reconnect wait alone does not keep the process alive. MISAKA fork: asyncio timers never keep
        # the process alive, so there is no `unref`.
        removeAbortListener = addAbortListener(signal, onAbort)
        return settled


__all__ = [
    "ConsumeSseOptions",
    "McpAuthRequiredError",
    "McpHttpError",
    "McpSessionExpiredError",
    "SseEvent",
    "StreamableHttpReconnectOptions",
    "StreamableHttpTransport",
    "StreamableHttpTransportOptions",
    "consumeSseStream",
]

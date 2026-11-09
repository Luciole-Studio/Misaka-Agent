"""MISAKA fork: the WHATWG ``fetch`` and ``Response`` pi's MCP package and pi-ai's OAuth flows call, over httpx.

pi's MCP client, its OAuth flow, the MCP extension and pi-ai's sign-in flows run on Node's ``fetch``: redirects are
followed, a network failure or an idle timeout rejects with ``TypeError`` (which OAuth discovery and the transport's
retry logic key on), ``text()`` decodes UTF-8 and strips the byte order mark whatever the charset header says,
``json()`` parses as ``JSON.parse`` does, a missing header reads as ``null``, and ``body.cancel()`` drops an unread
body. httpx does each of these differently, so the translated modules call this shim and stay line for line with
upstream. The idle timeout is the one pi gives undici (``core/http_dispatcher.py``); proxies come from the environment,
as httpx reads it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any, Literal, TypedDict

import httpx

from misaka.core.http_dispatcher import dispatcherIdleTimeout
from misaka.utils.js import jsonParse, raceAbort
from misaka.utils.whatwg_url import URL, URLSearchParams

# Statuses whose response has no body: the Fetch Standard's "null body status".
_NULL_BODY_STATUSES = frozenset({101, 103, 204, 205, 304})


class RequestInit(TypedDict, total=False):
    method: str
    headers: Mapping[str, str] | httpx.Headers | None
    body: str | bytes | URLSearchParams | None
    signal: Any
    redirect: Literal["follow", "error", "manual"]


async def _next(chunks: AsyncIterator[bytes]) -> bytes | None:
    try:
        return await anext(chunks)
    except StopAsyncIteration:
        return None


async def _chunksOf(data: bytes) -> AsyncIterator[bytes]:
    if data:
        yield data


class Body:
    """A response body: the byte chunks as they arrive, and ``cancel()`` to drop the rest."""

    def __init__(
        self,
        chunks: AsyncIterator[bytes],
        close: Callable[[], Awaitable[None]] | None = None,
        signal: Any = None,
    ) -> None:
        self._chunks = chunks
        self._close = close
        self._signal = signal
        self._done = False

    def __aiter__(self) -> Body:
        return self

    async def __anext__(self) -> bytes:
        if self._done:
            raise StopAsyncIteration
        try:
            reading = _next(self._chunks)
            chunk = await (reading if self._signal is None else raceAbort(reading, self._signal))
        except httpx.HTTPError as error:
            # A connection dropped mid-body, or a body idle timeout, is a `TypeError` for fetch too.
            await self.cancel()
            raise TypeError("terminated") from error
        except BaseException:
            await self.cancel()
            raise
        if chunk is None:
            await self.cancel()
            raise StopAsyncIteration
        return chunk

    async def cancel(self) -> None:
        if self._done:
            return
        self._done = True
        if self._close is not None:
            await self._close()

    # An async generator's name for the same thing, so a stream of either kind closes alike.
    aclose = cancel


class Response:
    """What ``fetch`` resolves with: ``status``, ``statusText``, ``ok``, ``headers``, ``body``, ``text()`` and
    ``json()``. Constructible as ``Response(None, status=401, headers={...})``, as ``new Response``."""

    def __init__(
        self,
        body: Body | bytes | str | None = None,
        *,
        status: int = 200,
        headers: Mapping[str, str] | httpx.Headers | None = None,
        url: str = "",
        statusText: str = "",
    ) -> None:
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.status = status
        self.statusText = statusText
        self.headers = httpx.Headers(headers)
        self.body: Body | None = Body(_chunksOf(body)) if isinstance(body, bytes) else body
        self.url = url
        self._content: bytes | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status <= 299

    async def text(self) -> str:
        return (await self._read()).decode("utf-8-sig", "replace")

    async def json(self) -> Any:
        return jsonParse(await self.text())

    async def _read(self) -> bytes:
        if self._content is None:
            content = bytearray()
            if self.body is not None:
                async for chunk in self.body:
                    content += chunk
            self._content = bytes(content)
        return self._content


# undici's default connect timeout, which pi's dispatcher keeps.
_CONNECT_TIMEOUT_S = 10.0


def createClient() -> httpx.AsyncClient:
    """The pool one ``fetch`` owner keeps: pi's idle timeout on headers and body, undici's connect
    timeout, and no total one. Redirects are decided per request."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=_CONNECT_TIMEOUT_S, read=dispatcherIdleTimeout(), write=None, pool=None),
        trust_env=True,
    )


def _content(body: Any, headers: httpx.Headers) -> bytes | None:
    if body is None:
        return None
    if isinstance(body, URLSearchParams):
        headers.setdefault("content-type", "application/x-www-form-urlencoded;charset=UTF-8")
        return str(body).encode("utf-8")
    if isinstance(body, str):
        headers.setdefault("content-type", "text/plain;charset=UTF-8")
        return body.encode("utf-8")
    if isinstance(body, bytes | bytearray | memoryview):
        return bytes(body)
    raise TypeError("Unsupported fetch body")


async def fetch(input: str | URL, init: RequestInit | None = None) -> Response:
    """``globalThis.fetch``: a connection of its own, released with the response body."""
    client = createClient()
    try:
        return await fetchWith(client, input, init, owned=True)
    except BaseException:
        await client.aclose()
        raise


async def fetchWith(
    client: httpx.AsyncClient, input: str | URL, init: RequestInit | None = None, *, owned: bool = False
) -> Response:
    """``fetch`` over ``client``; with ``owned`` the client closes along with the response body."""
    options: RequestInit = init if init is not None else {}
    url = URL(input)  # `TypeError("Invalid URL")`, as the Request constructor throws
    if url.username or url.password:
        raise TypeError("Request cannot be constructed from a URL that includes credentials.")
    if url.protocol not in ("http:", "https:"):
        raise TypeError("fetch failed")
    method = (options.get("method") or "GET").upper()
    headers = httpx.Headers(options.get("headers"))
    content = _content(options.get("body"), headers)
    signal = options.get("signal")
    redirect = options.get("redirect") or "follow"
    request = client.build_request(method, url.href, headers=headers, content=content)
    sending = client.send(request, stream=True, follow_redirects=redirect == "follow")
    try:
        response = await (sending if signal is None else raceAbort(sending, signal))
    except httpx.HTTPError as error:
        # Network failures, connection and idle timeouts and redirect loops all reject with `TypeError`.
        raise TypeError("fetch failed") from error
    if redirect == "error" and response.has_redirect_location:
        await response.aclose()
        raise TypeError("fetch failed")

    async def close() -> None:
        await response.aclose()
        if owned:
            await client.aclose()

    if method == "HEAD" or response.status_code in _NULL_BODY_STATUSES:
        await close()
        return Response(
            None,
            status=response.status_code,
            headers=response.headers,
            url=str(response.url),
            statusText=response.reason_phrase,
        )
    return Response(
        Body(response.aiter_bytes(), close, signal),
        status=response.status_code,
        headers=response.headers,
        url=str(response.url),
        statusText=response.reason_phrase,
    )


__all__ = ["Body", "RequestInit", "Response", "createClient", "fetch", "fetchWith"]

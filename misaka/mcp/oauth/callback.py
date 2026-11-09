"""The loopback HTTP server that receives OAuth authorization redirects.

Translated from pi's ``packages/mcp/src/oauth/callback.ts``. node:http's server becomes an asyncio
stream server with the plumbing of ``misaka/ai/auth/oauth/callback_server.py``: the request line
and headers are read and dropped, a peer that sends nothing gets no answer, and ``close()`` aborts
idle preconnects so it returns.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from typing import Literal, NotRequired, TypedDict

from misaka.utils.whatwg_url import URL


class OAuthCallback(TypedDict):
    code: str
    state: str
    iss: NotRequired[str]


class OAuthCallbackPageOk(TypedDict):
    ok: Literal[True]


class OAuthCallbackPageFailed(TypedDict):
    ok: Literal[False]
    message: str
    details: NotRequired[str]


# Outcome shown on the browser page after the redirect.
type OAuthCallbackPage = OAuthCallbackPageOk | OAuthCallbackPageFailed


class OAuthCallbackServerOptions(TypedDict, total=False):
    # Address to listen on. Default: `127.0.0.1`.
    host: str
    # Host name in `redirectUrl`, for example `localhost` for a client registered with it while
    # listening on `127.0.0.1`. Default: `host`.
    redirectHost: str
    port: int
    path: str
    # More paths that receive the callback, for example a server-specific path of a redirect URI.
    extraPaths: list[str]
    timeoutMs: float
    # Render the browser page as HTML. Default: a plain-text message.
    renderPage: Callable[[OAuthCallbackPage], str]


def plainText(page: OAuthCallbackPage) -> str:
    if page["ok"]:
        return "Authorization complete. You may close this window."
    return f"{page['message']}\n\n{page['details']}" if page.get("details") else page["message"]


@dataclass(slots=True)
class _Pending:
    future: asyncio.Future[OAuthCallback]
    timer: asyncio.TimerHandle
    path: str | None


class OAuthCallbackServer:
    def __init__(
        self,
        server: asyncio.Server,
        redirectUrl: str,
        paths: list[str],
        timeoutMs: float,
        renderPage: Callable[[OAuthCallbackPage], str] | None,
    ) -> None:
        self._server = server
        self.redirectUrl = redirectUrl
        self._paths = paths
        self._timeoutMs = timeoutMs
        self._renderPage = renderPage
        self._pending: dict[str, _Pending] = {}
        # MISAKA fork: connections that have not sent a request yet; `close()` aborts them.
        self._idle: set[asyncio.StreamWriter] = set()

    @classmethod
    async def listen(cls, options: OAuthCallbackServerOptions | None = None) -> OAuthCallbackServer:
        options = options if options is not None else {}
        host = options.get("host")
        if host is None:
            host = "127.0.0.1"
        redirectHost = options.get("redirectHost")
        if redirectHost is None:
            redirectHost = host
        path = options.get("path")
        if path is None:
            path = "/callback"
        port = options.get("port")
        if port is None:
            port = 0
        instance: OAuthCallbackServer | None = None

        async def serve(request: asyncio.StreamReader, response: asyncio.StreamWriter) -> None:
            if instance is not None:
                await instance._serve(request, response)

        loop = asyncio.get_running_loop()
        # MISAKA fork: bind only the first address the host resolves to, as `listen(port, host)` does.
        addresses = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
        family, _, _, _, sockaddr = addresses[0]
        server = await asyncio.start_server(serve, str(sockaddr[0]), port, family=family)
        address = server.sockets[0].getsockname() if server.sockets else None
        if not address:
            raise RuntimeError("OAuth callback server did not bind to TCP")
        timeoutMs = options.get("timeoutMs")
        instance = cls(
            server,
            f"http://{f'[{redirectHost}]' if ':' in redirectHost else redirectHost}:{address[1]}{path}",
            [path, *(options.get("extraPaths") or [])],
            timeoutMs if timeoutMs is not None else 5 * 60_000,
            options.get("renderPage"),
        )
        return instance

    def waitForCallback(self, state: str, path: str | None = None) -> asyncio.Future[OAuthCallback]:
        """Wait for the authorization response with `state`. With `path`, a response on another path fails, so
        a server-specific redirect URI can tell authorization servers apart (RFC 9700 section 4.4.2.2).
        """
        if state in self._pending:
            raise RuntimeError("OAuth state is already pending")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[OAuthCallback] = loop.create_future()
        # MISAKA fork: a rejection nobody awaits (upstream's test does `denied.catch(() => undefined)`)
        # and a future its awaiter cancelled must not surface as "never retrieved".
        future.add_done_callback(lambda done: done.cancelled() or done.exception())

        def timeout() -> None:
            self._pending.pop(state, None)
            if not future.done():
                future.set_exception(RuntimeError("OAuth callback timed out"))

        timer = loop.call_later(self._timeoutMs / 1000, timeout)
        self._pending[state] = _Pending(future, timer, path)
        return future

    async def close(self) -> None:
        for pending in list(self._pending.values()):
            pending.timer.cancel()
            if not pending.future.done():
                pending.future.set_exception(RuntimeError("OAuth callback server closed"))
        self._pending.clear()
        if not self._server.is_serving():
            # `server.close(callback)` on a server that is not listening.
            notRunning = RuntimeError("Server is not running.")
            notRunning.code = "ERR_SERVER_NOT_RUNNING"  # type: ignore[attr-defined]
            raise notRunning
        self._server.close()
        # MISAKA fork: `wait_closed()` (3.12.1+) waits for every connection, browser preconnects
        # included; Node's `server.close()` closes idle sockets itself.
        for connection in list(self._idle):
            connection.transport.abort()
        self._idle.clear()
        await self._server.wait_closed()

    def _reply(self, response: asyncio.StreamWriter, status: int, page: OAuthCallbackPage, head: bool) -> None:
        if self._renderPage is not None:
            body = self._renderPage(page).encode("utf-8")
            headers = "content-type: text/html; charset=utf-8\r\ncache-control: no-store\r\n"
        else:
            body = plainText(page).encode("utf-8")
            headers = "content-type: text/plain; charset=utf-8\r\n"
        start = (
            f"HTTP/1.1 {status} {HTTPStatus(status).phrase}\r\n"
            f"{headers}content-length: {len(body)}\r\nconnection: close\r\n\r\n"
        )
        # MISAKA fork: the peer may be gone already; node's `response.end` does not throw for it. A
        # HEAD request gets the head alone, as node:http sends it.
        with contextlib.suppress(OSError, RuntimeError):
            response.write(start.encode("latin-1") + (b"" if head else body))
            response.close()

    def _handle(self, rawUrl: str, response: asyncio.StreamWriter, *, head: bool = False) -> None:
        url = URL(rawUrl, self.redirectUrl)
        if url.pathname not in self._paths:
            self._reply(response, 404, {"ok": False, "message": "Not found"}, head)
            return
        state = url.searchParams.get("state")
        pending = self._pending.get(state) if state else None
        if not state or pending is None:
            self._reply(response, 400, {"ok": False, "message": "Invalid or expired OAuth state"}, head)
            return
        pending.timer.cancel()
        del self._pending[state]
        if pending.path is not None and url.pathname != pending.path:
            if not pending.future.done():
                pending.future.set_exception(RuntimeError("The authorization response arrived on another redirect URI"))
            self._reply(response, 400, {"ok": False, "message": "Unexpected redirect URI"}, head)
            return
        error = url.searchParams.get("error")
        if error:
            description = url.searchParams.get("error_description")
            if description is None:
                description = error
            if not pending.future.done():
                pending.future.set_exception(RuntimeError(description))
            self._reply(
                response,
                200,
                {"ok": False, "message": "Authorization failed. You may close this window.", "details": description},
                head,
            )
            return
        code = url.searchParams.get("code")
        if not code:
            if not pending.future.done():
                pending.future.set_exception(RuntimeError("OAuth callback did not include an authorization code"))
            self._reply(response, 400, {"ok": False, "message": "Missing authorization code"}, head)
            return
        iss = url.searchParams.get("iss")
        callback: OAuthCallback = {"code": code, "state": state}
        if iss:
            callback["iss"] = iss
        if not pending.future.done():
            pending.future.set_result(callback)
        self._reply(response, 200, {"ok": True}, head)

    async def _serve(self, request: asyncio.StreamReader, response: asyncio.StreamWriter) -> None:
        # MISAKA fork: node:http parses the request; here the request line is read and the headers dropped.
        self._idle.add(response)
        try:
            requestLine = await request.readline()
            while requestLine.strip():
                header = await request.readline()
                if not header or header in (b"\r\n", b"\n"):
                    break
        except (OSError, ValueError, asyncio.IncompleteReadError):
            requestLine = b""
        finally:
            self._idle.discard(response)
        parts = requestLine.decode("latin-1").split()
        if len(parts) < 2:
            # A peer that sent no request line (a preconnect, a port scan, or one of `close()`'s own
            # aborts) gets no answer.
            response.transport.abort()
            return
        self._handle(parts[1], response, head=parts[0] == "HEAD")


__all__ = [
    "OAuthCallback",
    "OAuthCallbackPage",
    "OAuthCallbackPageFailed",
    "OAuthCallbackPageOk",
    "OAuthCallbackServer",
    "OAuthCallbackServerOptions",
]

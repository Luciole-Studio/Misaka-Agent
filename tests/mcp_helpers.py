"""pi's ``packages/mcp/test/helpers.ts``: a loopback HTTP/1.1 server with node:http's request and
response shapes, for the Streamable HTTP transport, OAuth and MCP extension tests."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from http import HTTPStatus

from misaka.utils.js import jsString


class _Socket:
    """node's `request.socket`, as far as tests read it."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter | None) -> None:
        self._reader = reader
        self._writer = writer

    @property
    def destroyed(self) -> bool:
        """Whether the connection ended: the peer closed or reset it, or this side closed it."""
        closing = self._writer is not None and self._writer.transport.is_closing()
        return closing or getattr(self._reader, "_eof", False) or self._reader.exception() is not None


class IncomingMessage:
    def __init__(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter | None = None,
    ) -> None:
        self.method = method
        self.url = url
        # Lower-cased names; repeated headers joined with ", ", as node's `request.headers`.
        self.headers = headers
        self._reader = reader
        self.socket = _Socket(reader, writer)

    async def read(self) -> bytes:
        length = int(self.headers.get("content-length") or 0)
        return await self._reader.readexactly(length) if length else b""


class ServerResponse:
    def __init__(self, writer: asyncio.StreamWriter, onFinish: Callable[[], None]) -> None:
        self._writer = writer
        self._onFinish = onFinish
        self.statusCode = 200
        self._headers: dict[str, str] = {}
        self.headersSent = False
        self.finished = False
        self._chunked = False

    def setHeader(self, name: str, value: str) -> None:
        self._headers[name.lower()] = value

    def writeHead(self, status: int, headers: dict[str, str] | None = None) -> ServerResponse:
        self.statusCode = status
        for name, value in (headers or {}).items():
            self.setHeader(name, value)
        return self

    def _sendHead(self, contentLength: int | None) -> None:
        if self.headersSent:
            return
        self.headersSent = True
        headers = dict(self._headers)
        if contentLength is None:
            headers.setdefault("transfer-encoding", "chunked")
            self._chunked = True
        else:
            headers.setdefault("content-length", str(contentLength))
        headers["connection"] = "close"
        try:
            phrase = HTTPStatus(self.statusCode).phrase
        except ValueError:
            phrase = ""
        head = f"HTTP/1.1 {self.statusCode} {phrase}\r\n" + "".join(f"{n}: {v}\r\n" for n, v in headers.items()) + "\r\n"
        self._writer.write(head.encode("latin-1"))

    def write(self, chunk: str | bytes) -> None:
        """Streams a chunk; the head goes out first, with chunked transfer encoding."""
        data = chunk.encode("utf-8") if isinstance(chunk, str) else chunk
        self._sendHead(None)
        if self._chunked:
            self._writer.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        else:
            self._writer.write(data)

    def end(self, body: str | bytes | None = None) -> None:
        if self.finished:
            return
        self.finished = True
        data = body.encode("utf-8") if isinstance(body, str) else (body or b"")
        if not self.headersSent:
            self._sendHead(len(data))
            if data:
                self._writer.write(data)
        else:
            if data:
                self.write(data)
            if self._chunked:
                self._writer.write(b"0\r\n\r\n")
        with contextlib.suppress(OSError, RuntimeError):
            self._writer.close()
        self._onFinish()


type HttpHandler = Callable[[IncomingMessage, ServerResponse, str], Awaitable[None]]


class _Server:
    def __init__(self, server: asyncio.Server, origin: str) -> None:
        self.server = server
        self.origin = origin
        self.connections: set[asyncio.StreamWriter] = set()


servers: list[_Server] = []


async def readBody(request: IncomingMessage) -> str:
    return (await request.read()).decode("utf-8")


async def listen(handler: HttpHandler) -> str:
    """Starts a loopback HTTP server and returns its origin. Call ``closeServers`` from the teardown."""
    origin = ""
    connections: set[asyncio.StreamWriter] = set()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connections.add(writer)
        try:
            requestLine = await reader.readline()
            parts = requestLine.decode("latin-1").split()
            if len(parts) < 2:
                connections.discard(writer)
                writer.transport.abort()
                return
            headers: dict[str, str] = {}
            while True:
                line = await reader.readline()
                if not line or line in (b"\r\n", b"\n"):
                    break
                name, _, value = line.decode("latin-1").partition(":")
                name = name.strip().lower()
                value = value.strip()
                headers[name] = f"{headers[name]}, {value}" if name in headers else value
        except (OSError, asyncio.IncompleteReadError):
            connections.discard(writer)
            return
        request = IncomingMessage(parts[0], parts[1], headers, reader, writer)
        response = ServerResponse(writer, lambda: connections.discard(writer))
        try:
            await handler(request, response, origin)
        except Exception as error:  # noqa: BLE001 - upstream `.catch`: answered with 500
            response.statusCode = 500
            response.end(jsString(error))

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    address = server.sockets[0].getsockname()
    origin = f"http://127.0.0.1:{address[1]}"
    entry = _Server(server, origin)
    entry.connections = connections
    servers.append(entry)
    return origin


async def _close(entry: _Server) -> None:
    for writer in list(entry.connections):
        writer.transport.abort()
    entry.connections.clear()
    entry.server.close()
    await entry.server.wait_closed()


async def closeServer(origin: str) -> None:
    """Close one server started by ``listen``, as a fixture's own ``close`` does."""
    for entry in servers[:]:
        if entry.origin == origin:
            servers.remove(entry)
            await _close(entry)


async def closeServers() -> None:
    closing = servers[:]
    del servers[:]
    for entry in closing:
        await _close(entry)


__all__ = [
    "HttpHandler",
    "IncomingMessage",
    "ServerResponse",
    "closeServer",
    "closeServers",
    "listen",
    "readBody",
    "servers",
]

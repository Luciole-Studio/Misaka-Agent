"""pi chord's ``test/websocket.test.ts``.

MISAKA adaptations: upstream runs a real ``ws`` ``WebSocketServer`` and Node's global ``WebSocket``. Chord takes any
WHATWG-shaped socket (``WebSocketLike``) and any server with ``on("connection", ...)`` (``WebSocketServerLike``);
misaka has no WHATWG WebSocket, so this file adapts real sockets of the ``websockets`` library (asyncio API) to those
shapes: ``ClientSocket`` (a ``new WebSocket(url)``: connecting, then ``open``/``message``/``close`` events) and
``Server`` (``on("connection", listener)`` handing each accepted connection, already open, and its request). The
request's URL is its ``path``. ``AbortSignal.timeout`` is ``misaka.utils.js.AbortSignal.timeout``. The test is skipped
without ``websockets``.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from misaka.chord import (
    ChordError,
    acceptWebSockets,
    createHost,
    hostNamed,
    inflateMessage,
    isChordError,
    method,
    remote,
    webSocketLink,
)
from misaka.chord._typebox import Type
from misaka.utils.js import AbortSignal

websockets = pytest.importorskip("websockets")
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

CONNECTING, OPEN, CLOSING, CLOSED = 0, 1, 2, 3


class _Socket:
    """The WHATWG WebSocket surface over a ``websockets`` connection: ``readyState``, ``send``, ``close`` and
    ``addEventListener`` for ``open``, ``message`` and ``close``."""

    def __init__(self) -> None:
        self.readyState = CONNECTING
        self.binaryType = "blob"
        self._listeners: dict[str, list[Any]] = {"open": [], "message": [], "close": [], "error": []}
        self._connection: Any = None
        self._outbox: asyncio.Queue[Any] = asyncio.Queue()
        self._tasks: set[asyncio.Task[Any]] = set()

    def addEventListener(self, type: str, listener: Any) -> None:
        self._listeners.setdefault(type, []).append(listener)

    def _emit(self, type: str, event: Any = None) -> None:
        for listener in list(self._listeners.get(type, [])):
            if type == "message":
                listener(event)
            else:
                listener()

    def send(self, data: str | bytes) -> None:
        if self.readyState != OPEN:
            raise RuntimeError("InvalidStateError: the socket is not open")
        self._outbox.put_nowait(("send", data))

    def close(self, code: int | None = None, reason: str | None = None) -> None:
        if self.readyState >= CLOSING:
            return
        self.readyState = CLOSING
        self._outbox.put_nowait(("close", code or 1000))

    def _start(self, connection: Any) -> None:
        self._connection = connection
        self.readyState = OPEN
        for coroutine in (self._write(), self._read()):
            task = asyncio.ensure_future(coroutine)
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _write(self) -> None:
        while True:
            kind, data = await self._outbox.get()
            try:
                if kind == "send":
                    await self._connection.send(data)
                else:
                    await self._connection.close(data)
                    return
            except Exception:  # noqa: BLE001 - a socket that went away ends here; `_read` reports the close
                return

    async def _read(self) -> None:
        try:
            async for message in self._connection:
                self._emit("message", SimpleNamespace(data=message))
        except Exception:  # noqa: BLE001, S110 - any end of the connection is its close
            pass
        self.readyState = CLOSED
        self._emit("close")

    async def closed(self) -> None:
        """Waits until the connection's reader ended (the server handler's lifetime)."""
        while self.readyState != CLOSED:
            await asyncio.sleep(0.005)


class ClientSocket(_Socket):
    """``new WebSocket(url)``: connects in the background, then fires ``open``."""

    def __init__(self, url: str) -> None:
        super().__init__()

        async def opening() -> None:
            try:
                connection = await connect(url)
            except Exception:  # noqa: BLE001 - a refused connection is a close
                self.readyState = CLOSED
                self._emit("close")
                return
            self._start(connection)
            self._emit("open")

        task = asyncio.ensure_future(opening())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


class Server:
    """A ``websockets`` server as a ``WebSocketServerLike``: ``on("connection", (socket, request) => ...)``."""

    def __init__(self) -> None:
        self._listeners: list[Any] = []
        self._server: Any = None
        self.port = 0

    def on(self, event: str, listener: Any) -> Server:
        if event == "connection":
            self._listeners.append(listener)
        return self

    async def start(self) -> None:
        async def handler(connection: Any) -> None:
            socket = _Socket()
            socket._start(connection)
            request = SimpleNamespace(url=connection.request.path)
            for listener in list(self._listeners):
                listener(socket, request)
            await socket.closed()

        self._server = await serve(handler, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        self._server.close()
        await self._server.wait_closed()


Board = remote(
    "t.board",
    {
        "version": 1,
        "input": Type.Object({}),
        "host": hostNamed("server"),
        "variants": {
            "main": {
                "state": {"notes": Type.Array(Type.String())},
                "methods": {"add": method(Type.String()), "who": method(None, Type.Unknown())},
            },
        },
    },
)


async def test_serves_a_browser_style_client_over_a_real_socket_stamped_by_the_authenticator():
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})

    def factory(_input: Any, principal: Any, self: Any) -> dict[str, Any]:
        notes = self.state("notes", Type.Array(Type.String()), [])

        def add(text: str, ctx: Any) -> None:
            def push(draft: Any) -> None:
                draft.push(text)

            notes.change(ctx, push)

        return {"variant": "main", "notes": notes, "add": add, "who": lambda ctx: principal}

    server.serve(Board, factory)
    wss = Server()
    await wss.start()

    def principal(request: Any) -> Any:
        token = parse_qs(urlparse(request.url or "/").query).get("token", [None])[0]
        return {"guest": "web"} if token == "ok" else None

    acceptWebSockets(wss, server, {"principal": principal})
    port = wss.port

    def open_(token: str) -> Any:
        return createHost(
            {
                "self": {"kind": "host"},
                "name": "browser",
                "kind": "client",
                "upstream": {
                    "link": webSocketLink(ClientSocket(f"ws://127.0.0.1:{port}/?token={token}")),
                    "principal": {"kind": "server"},
                },
                "onError": lambda _error: None,
            }
        )

    try:
        browser = open_("ok")
        board = browser.hold(Board, {})
        seen: list[Any] = []
        board.notes.subscribe(seen.append)
        await board.add("hello")
        assert await board.who() == {"guest": "web"}
        for _ in range(50):
            try:
                if len(board.notes.value) == 1:
                    break
            except ChordError:
                pass
            await asyncio.sleep(0.005)
        assert board.notes.value == ["hello"]
        assert seen[-1] == ["hello"]
        await browser.close()

        # WebSocket peers are always clients: one announcing itself as a host is cut off.
        impostor = createHost(
            {
                "self": {"kind": "host"},
                "name": "session:S1",
                "upstream": {
                    "link": webSocketLink(ClientSocket(f"ws://127.0.0.1:{port}/?token=ok")),
                    "principal": {"kind": "server"},
                },
                "onError": lambda _error: None,
            }
        )
        with pytest.raises(ChordError) as info:
            await impostor.root(
                lambda ctx: impostor.hold(Board, {}).add("x", ctx), {"signal": AbortSignal.timeout(150)}
            )
        assert isChordError(info.value, "aborted")
        await impostor.close()

        refused = open_("bad")
        denied = refused.hold(Board, {})
        # The server closes unauthenticated sockets; calls wait for a reconnect until their ctx gives up.
        with pytest.raises(ChordError) as info:
            await refused.root(lambda ctx: denied.add("x", ctx), {"signal": AbortSignal.timeout(100)})
        assert isChordError(info.value, "aborted")
        await refused.close()
    finally:
        await wss.close()


async def test_large_snapshots_and_results_reach_the_client_as_binary_messages_clients_may_not_send_them():
    notes = [f"{i}:{'x' * 4000}" for i in range(40)]
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})

    def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
        state = self.state("notes", Type.Array(Type.String()), notes)
        return {"variant": "main", "notes": state, "add": lambda text, ctx: None, "who": lambda ctx: notes}

    server.serve(Board, factory)
    wss = Server()
    await wss.start()
    acceptWebSockets(wss, server, {"principal": lambda _request: {"guest": "web"}})
    url = f"ws://127.0.0.1:{wss.port}/"
    try:
        binary: list[bytes] = []
        socket = ClientSocket(url)
        socket.addEventListener(
            "message", lambda event: binary.append(bytes(event.data)) if not isinstance(event.data, str) else None
        )
        browser = createHost(
            {
                "self": {"kind": "host"},
                "name": "browser",
                "kind": "client",
                "upstream": {"link": webSocketLink(socket), "principal": {"kind": "server"}},
                "onError": lambda _error: None,
            }
        )
        board = browser.hold(Board, {})
        assert await board.who() == notes
        board.notes.subscribe(lambda _value: None)
        for _ in range(100):
            try:
                board.notes.value  # noqa: B018 - readable once hydrated
                break
            except ChordError:
                await asyncio.sleep(0.005)
        assert board.notes.value == notes
        assert len(binary) >= 2, "the result and the snapshot came as binary messages"
        # A text-only transport (the TUI launcher) turns a packed message back into its text frame.
        frames = [json.loads(inflateMessage(message)) for message in binary]
        assert any(frame["t"] == "result" and frame.get("value") == notes for frame in frames)
        assert any(frame["t"] == "state" and frame.get("value") == notes for frame in frames)
        await browser.close()

        # A client's binary message is a protocol error: the server closes the socket.
        raw = await connect(url)
        await raw.send(bytes([0, 0, 0, 2, 123, 125]))
        with pytest.raises(websockets.ConnectionClosed):
            for _ in range(100):
                await asyncio.wait_for(raw.recv(), 5)
    finally:
        await wss.close()

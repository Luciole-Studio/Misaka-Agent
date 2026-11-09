"""Regression tests for the link findings of the chord 1.3.0 verification (VERIFY-V4 B23-B29).

Each expected value was recorded by running the same scenario under upstream chord v1.3.0 (``src/link.ts``,
``src/websocket.ts``) with Node. MISAKA adaptations: a JavaScript microtask is one ``loop.call_soon`` callback;
``queueMicrotask`` is ``loop.call_soon``; Node's unhandled rejection is the loop's exception handler.
"""

from __future__ import annotations

import asyncio
import errno
import struct
import zlib
from typing import Any

import misaka.chord.link as linkModule
from misaka.chord import createHost, hostNamed, local, memoryLinkPair, method, remote
from misaka.chord._typebox import Type
from misaka.chord.link import inflateMessage, ipcLink, unpack, webSocketLink
from misaka.chord.websocket import acceptWebSockets
from misaka.utils.js import AbortController


async def turns(count: int = 10) -> None:
    for _ in range(count):
        await asyncio.sleep(0)


def deflate(data: bytes) -> bytes:
    compressor = zlib.compressobj(1, zlib.DEFLATED, -15)
    return compressor.compress(data) + compressor.flush()


def binaryMessage(envelope: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(envelope)) + envelope + payload


class FakeSocket:
    readyState = 1

    def __init__(self) -> None:
        self.listeners: dict[str, list[Any]] = {}
        self.sent: list[Any] = []
        self.closedWith: list[Any] = []

    def send(self, data: Any) -> None:
        self.sent.append(data)

    def close(self, code: int | None = None, reason: str | None = None) -> None:
        self.closedWith.append(code)

    def addEventListener(self, type: str, listener: Any) -> None:
        self.listeners.setdefault(type, []).append(listener)

    def fire(self, type: str, event: Any = None) -> None:
        for listener in list(self.listeners.get(type, [])):
            listener(event)


class MessageEvent:
    def __init__(self, data: Any) -> None:
        self.data = data


class FakeChannel:
    connected = True

    def __init__(self, sendError: BaseException | None = None) -> None:
        self.listeners: dict[str, list[Any]] = {}
        self.sendError = sendError

    def on(self, event: str, listener: Any) -> None:
        self.listeners.setdefault(event, []).append(listener)

    def off(self, event: str, listener: Any) -> None:
        self.listeners[event].remove(listener)

    def send(self, message: Any, callback: Any = None) -> bool:
        if callback is not None:
            asyncio.get_running_loop().call_soon(callback, self.sendError)
        return True

    def emit(self, event: str, *args: Any) -> None:
        for listener in list(self.listeners.get(event, [])):
            listener(*args)


# ─── B23: MemoryLink delivery is one promise chain per direction ─────────────


async def test_b23_a_memory_link_delivers_one_frame_per_step_after_what_the_previous_delivery_scheduled():
    loop = asyncio.get_running_loop()
    log: list[str] = []
    a, b = memoryLinkPair()

    def onFrame(frame: Any) -> None:
        log.append(f"got {frame['n']}")
        loop.call_soon(log.append, f"after {frame['n']}")  # queueMicrotask

    b.receive(onFrame)
    for n in (1, 2, 3):
        a.send({"t": "ping", "n": n})
    await turns()
    # Upstream: ["got 1","after 1","got 2","after 2","got 3","after 3"].
    assert log == ["got 1", "after 1", "got 2", "after 2", "got 3", "after 3"]


async def test_b23_a_delivery_that_throws_rejects_the_chain_later_frames_are_dropped_and_the_error_reported():
    loop = asyncio.get_running_loop()
    reported: list[str] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reported.append(str(context.get("exception"))))
    try:
        a, b = memoryLinkPair()
        got: list[int] = []

        def onFrame(frame: Any) -> None:
            got.append(frame["n"])
            if frame["n"] == 1:
                raise RuntimeError("boom")

        b.receive(onFrame)
        a.send({"t": "ping", "n": 1})
        a.send({"t": "ping", "n": 2})
        await turns()
        a.send({"t": "ping", "n": 3})
        await turns()
    finally:
        loop.set_exception_handler(previous)
    # Upstream: got [1], b.closed false, an unhandled rejection "boom".
    assert got == [1]
    assert not b.closed
    assert reported == ["boom"]


def test_b23_closing_a_memory_link_outside_a_running_loop_tells_the_peer():
    a, b = memoryLinkPair()
    reasons: list[str] = []
    b.onClose(reasons.append)
    a.close("sync")
    assert a.closed
    assert b.closed
    assert reasons == ["sync"]


async def test_b23_cross_host_interleaving_matches_upstream():
    """VERIFY-V4 ``diff/s1``: a browser's calls through a server, with state pushes, as upstream orders them."""
    log: list[str] = []
    Todo = Type.Object({"text": Type.String(), "done": Type.Boolean()})
    Rows = Type.Array(Todo)
    TodoStore = local(
        "t.todos",
        {
            "version": 1,
            "input": Type.Object({"list": Type.String()}),
            "members": {
                "rows": Rows,
                "count": Type.Number(),
                "add": method(Type.Object({"text": Type.String()}), Type.Number()),
            },
        },
    )
    Todos = remote(
        "t.view",
        {
            "version": 1,
            "input": Type.Object({"list": Type.String()}),
            "host": hostNamed("server"),
            "variants": {
                "editor": {
                    "state": {"rows": Rows, "count": Type.Number()},
                    "methods": {"add": method(Type.Object({"text": Type.String()}), Type.Number())},
                }
            },
        },
    )
    server = createHost({"self": "server", "name": "server"})

    def store(_input: Any, _principal: Any, self: Any) -> Any:
        rows = self.state("rows", Rows, [])
        count = self.derive([rows], lambda r: len(r))

        def add(args: Any, ctx: Any) -> int:
            rows.change(ctx, lambda draft: draft.push({"text": args["text"], "done": False}) and None)
            log.append(f"server add {args['text']}")
            return len(rows.value)

        return {"rows": rows, "count": count, "add": add}

    server.serve(TodoStore, store)

    def view(input: Any, _principal: Any, self: Any) -> Any:
        todos = self.get(TodoStore, {"list": input["list"]})
        return {"variant": "editor", "rows": todos.rows, "count": todos.count, "add": lambda a, ctx: todos.add(a, ctx)}

    server.serve(Todos, view)
    down, up = memoryLinkPair()
    server.accept(down, {"principal": "owner", "kind": "client"})
    browser = createHost(
        {"self": "browser", "name": "browser", "kind": "client", "upstream": {"link": up, "principal": "server"}}
    )
    h1 = browser.hold(Todos, {"list": "f"})
    log.append(f"h1 status {h1.status}")
    h1.onStatus(lambda status, *_: log.append(f"h1 onStatus {status}"))
    r1 = await browser.get(Todos, {"list": "f"})
    log.append(f"r1 got status {r1.status} h1 {h1.status}")
    stop1 = r1.rows.subscribe(lambda rows: log.append("r1 rows " + ",".join(row["text"] for row in rows)))
    stop2 = h1.count.subscribe(lambda count: log.append(f"h1 count {count}"))
    calls = asyncio.gather(r1.add({"text": "a"}), r1.add({"text": "b"}), h1.add({"text": "c"}))
    log.append("calls sent")
    results = await calls
    log.append("results " + ",".join(str(result) for result in results))
    await asyncio.sleep(0.02)
    log.append(f"values {','.join(row['text'] for row in r1.rows.value)} {h1.count.value}")
    stop1()
    r1.release()
    log.append(f"after r1 release h1 {h1.status} r1 {r1.status}")
    await asyncio.sleep(0.02)
    await h1.add({"text": "d"})
    await asyncio.sleep(0.02)
    stop2()
    h1.release()
    await asyncio.sleep(0.02)
    controller = AbortController()
    controller.abort()
    try:
        await browser.get(Todos, {"list": "f"}, {"signal": controller.signal})
    except Exception as error:  # noqa: BLE001 - the expected abort
        log.append(f"aborted get: {getattr(error, 'code', type(error).__name__)} {getattr(error, 'reason', None) or ''}")
    await browser.close()
    log.append("closed")
    await server.close()
    # Recorded under upstream chord v1.3.0 (`diff/s1.ts`).
    assert log == [
        "h1 status loading",
        "h1 onStatus live",
        "r1 got status live h1 live",
        "r1 rows ",
        "h1 count 0",
        "calls sent",
        "server add a",
        "r1 rows a",
        "server add b",
        "h1 count 1",
        "server add c",
        "r1 rows a,b",
        "h1 count 2",
        "r1 rows a,b,c",
        "h1 count 3",
        "results 1,2,3",
        "values a,b,c 3",
        "after r1 release h1 live r1 released",
        "server add d",
        "h1 count 4",
        "h1 onStatus released",
        "aborted get: aborted ",
        "closed",
    ]


# ─── B24: IPC messages ───────────────────────────────────────────────────────


async def test_b24_an_ipc_chord2_null_is_a_protocol_error_other_values_reach_the_receiver():
    channel = FakeChannel()
    link = ipcLink(channel)
    got: list[Any] = []
    reasons: list[str] = []
    link.receive(got.append)
    link.onClose(reasons.append)
    for message in [{"other": 1}, "str", 7, None, [1], {"chord2": 5}, {"chord2": "x"}, {"chord2": [1]}]:
        channel.emit("message", message)
    # Upstream: control messages pass by; non-frame values reach the receiver (the connection rejects them).
    assert got == [5, "x", [1]]
    assert reasons == []
    channel.emit("message", {"chord2": None})
    # Upstream: `deliver(null)` throws, and the link closes as a protocol error.
    assert link.closed
    assert reasons == ["protocol error"]


# ─── B25: bytes are decoded as JavaScript decodes them ───────────────────────


async def test_b25_an_envelope_with_an_invalid_byte_or_a_byte_order_mark_is_decoded_not_refused():
    socket = FakeSocket()
    link = webSocketLink(socket)
    got: list[Any] = []
    reasons: list[str] = []
    link.receive(got.append)
    link.onClose(reasons.append)
    socket.fire(
        "message", MessageEvent(binaryMessage(b'{"t":"result","id":1,"z":"value","x":"\xff"}', deflate(b"1")))
    )
    socket.fire(
        "message", MessageEvent(binaryMessage(b'\xef\xbb\xbf{"t":"result","id":2,"z":"value"}', deflate(b'"e"')))
    )
    # Upstream (TextDecoder): U+FFFD for the bad byte, the byte order mark dropped; both frames delivered.
    assert reasons == []
    assert [frame["id"] for frame in got] == [1, 2]
    assert got[0]["x"] == "�"
    assert unpack(got[0]["value"]) == 1


async def test_b25_an_inflated_payload_with_an_invalid_byte_is_decoded_with_a_replacement_character():
    message = binaryMessage(b'{"t":"result","id":2,"z":"value"}', deflate(b'"\xff"'))
    # Upstream (Buffer.toString("utf8")): '{"t":"result","id":2,"value":"�"}'.
    assert inflateMessage(message) == '{"t":"result","id":2,"value":"�"}'
    socket = FakeSocket()
    link = webSocketLink(socket)
    got: list[Any] = []
    link.receive(got.append)
    socket.fire("message", MessageEvent(message))
    assert unpack(got[0]["value"]) == "�"


# ─── B26: an IPC write failure is named by its code ──────────────────────────


async def test_b26_an_ipc_send_failure_reports_the_error_code_as_node_does():
    cases = [
        # The platform's own numbers: 54 is ECONNRESET on macOS only (EXFULL on Linux).
        (ConnectionResetError(errno.ECONNRESET, "Connection reset by peer"), "ipc send failed: ECONNRESET"),
        (BrokenPipeError(errno.EPIPE, "Broken pipe"), "ipc send failed: EPIPE"),
        (Exception("boom"), "ipc send failed: boom"),
    ]
    coded = Exception("ignored")
    coded.code = "ERR_IPC_CHANNEL_CLOSED"  # type: ignore[attr-defined]
    cases.append((coded, "ipc send failed: ERR_IPC_CHANNEL_CLOSED"))
    for error, expected in cases:
        link = ipcLink(FakeChannel(error))
        reasons: list[str] = []
        link.onClose(reasons.append)
        link.send({"t": "ping", "n": 1})
        await turns(2)
        assert reasons == [expected]


# ─── B27: the browser path never delivers inline ─────────────────────────────


async def test_b27_without_zlib_a_packed_frame_is_delivered_later_and_in_order():
    saved = linkModule._zlib
    linkModule._zlib = None
    try:
        socket = FakeSocket()
        link = webSocketLink(socket)
        got: list[Any] = []
        link.receive(got.append)
        socket.fire("message", MessageEvent(binaryMessage(b'{"t":"result","id":1,"z":"value"}', deflate(b'"v"'))))
        socket.fire("message", MessageEvent('{"t":"result","id":2,"value":"w"}'))
        # Upstream: the inflate promise chain delivers after the message event, never inside it.
        assert got == []
        await turns()
        assert got == [{"t": "result", "id": 1, "value": "v"}, {"t": "result", "id": 2, "value": "w"}]
    finally:
        linkModule._zlib = saved


# ─── B28: any bytes-like view ────────────────────────────────────────────────


def test_b28_inflate_message_accepts_a_memoryview():
    message = binaryMessage(b'{"t":"result","id":2,"z":"value"}', deflate(b'"\xff"'))
    view = memoryview(bytearray(b"xx" + message))[2:]
    # Upstream: inflateMessage of a Uint8Array view (33 characters).
    assert inflateMessage(view) == '{"t":"result","id":2,"value":"�"}'


# ─── B29: acceptWebSockets ───────────────────────────────────────────────────


class FakeServer:
    def __init__(self) -> None:
        self.listener: Any = None

    def on(self, event: str, listener: Any) -> None:
        assert event == "connection"
        self.listener = listener


class FakeHost:
    def __init__(self, error: BaseException | None = None) -> None:
        self.accepted: list[Any] = []
        self.error = error

    def accept(self, link: Any, options: Any) -> None:
        if self.error is not None:
            raise self.error
        self.accepted.append(options["principal"])


async def test_b29_an_accept_that_throws_after_authentication_is_reported_not_swallowed():
    loop = asyncio.get_running_loop()
    reported: list[BaseException | None] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reported.append(context.get("exception")))
    try:
        server = FakeServer()
        failure = RuntimeError("accept failed")
        acceptWebSockets(server, FakeHost(failure), {"principal": lambda _request: {"user": "u"}})  # type: ignore[arg-type]
        server.listener(FakeSocket(), {})
        await turns()
    finally:
        loop.set_exception_handler(previous)
    assert reported == [failure]


async def test_b29_the_principal_is_taken_on_a_later_turn_and_none_refuses_the_client():
    server = FakeServer()
    host = FakeHost()
    principals = iter([{"user": "u"}, None])
    acceptWebSockets(server, host, {"principal": lambda _request: next(principals)})  # type: ignore[arg-type]
    socket = FakeSocket()
    server.listener(socket, {})
    # Upstream: `Promise.resolve(principal).then(...)` accepts on a later turn.
    assert host.accepted == []
    await turns()
    assert host.accepted == [{"user": "u"}]
    refused = FakeSocket()
    server.listener(refused, {})
    await turns()
    assert host.accepted == [{"user": "u"}]
    assert refused.sent == ['{"t":"bye","reason":"unauthorized"}']


async def test_b29_a_cancelled_authentication_refuses_the_client():
    server = FakeServer()
    host = FakeHost()
    pending: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    acceptWebSockets(server, host, {"principal": lambda _request: pending})  # type: ignore[arg-type]
    socket = FakeSocket()
    server.listener(socket, {})
    pending.cancel()
    await turns()
    # Upstream: any rejection of the authenticator refuses the client.
    assert socket.sent == ['{"t":"bye","reason":"authentication failed"}']
    assert host.accepted == []

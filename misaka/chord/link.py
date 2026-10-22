"""Links: ordered (FIFO), bidirectional frame channels between two hosts, over memory, an IPC channel or a WebSocket.

Translated from pi's ``packages/chord/src/link.ts``. ``JSON.stringify``/``JSON.parse`` are
``misaka.utils.js.jsonStringify``/``jsonParse`` (JavaScript's text), a JSON text's length is counted in UTF-16 code
units (``jsLength``), and Node's ``zlib`` is Python's (raw deflate, ``wbits=-15``). Python always has zlib, so the
browser path (``DecompressionStream`` before delivery) runs only when ``_zlib`` is set to ``None``. Bytes are decoded as
JavaScript does: ``TextDecoder`` (and ``Response.text()``) and ``Buffer.toString("utf8")`` replace an invalid sequence with
U+FFFD (``Utf8Decoder``); only the first drops a leading byte order mark. A promise chain step (one microtask) is one
``call_soon`` callback.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import errno
import struct
import threading
import zlib
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Awaitable, Callable, Iterator
from typing import Any, Final, Protocol, TypedDict

from misaka.chord._promise import background, resolved, start
from misaka.chord.wire import Frame
from misaka.utils.js import (
    UNDEFINED,
    Utf8Decoder,
    errorMessage,
    jsLength,
    jsonParse,
    jsonStringify,
)

# Node's zlib, or `None` for a host without it (a browser).
_zlib: Any = zlib


class Link(Protocol):
    """An ordered (FIFO), bidirectional frame channel between two hosts."""

    @property
    def closed(self) -> bool: ...

    def send(self, frame: Frame) -> int | None:
        """Returns the size of the encoded frame as sent (JSON text length, or bytes of a packed message) when the
        transport knows it. The connection's flow control counts these sizes; frames without one count as zero, so a
        link that never reports sizes is not windowed."""
        ...

    def receive(self, handler: Callable[[Frame], None]) -> None:
        """Sets the single receiver. Frames arriving earlier are queued."""
        ...

    def onClose(self, handler: Callable[[str], None]) -> None: ...

    def close(self, reason: str = "closed") -> None:
        """Closes both ends: the peer gets a ``bye``."""
        ...


class BaseLink(ABC):
    """Shared plumbing: receive queue, close handlers, ``bye``. Transports implement ``transmit`` and ``shutdown``."""

    def __init__(self) -> None:
        self._handler: Callable[[Frame], None] | None = None
        self._queue: list[Frame] = []
        self._closeHandlers: dict[Callable[[str], None], None] = {}
        self._closed = False
        # The tail of frames being decoded asynchronously; None when none is.
        self._decoding: asyncio.Future[None] | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    @abstractmethod
    def transmit(self, frame: Frame) -> int | None: ...

    @abstractmethod
    def shutdown(self) -> None: ...

    def send(self, frame: Frame) -> int | None:
        if self._closed:
            return None
        return self.transmit(frame)

    def receive(self, handler: Callable[[Frame], None]) -> None:
        self._handler = handler
        queued, self._queue = self._queue, []
        for frame in queued:
            handler(frame)

    def onClose(self, handler: Callable[[str], None]) -> None:
        if self._closed:
            handler("closed")
        else:
            self._closeHandlers[handler] = None

    def close(self, reason: str = "closed") -> None:
        if self._closed:
            return
        try:
            self.transmit({"t": "bye", "reason": reason})
        except Exception:  # noqa: BLE001, S110 - the transport may already be gone
            pass
        self.ended(reason)

    def deliver(self, frame: Frame) -> None:
        """Delivers a parsed frame from the transport, after any frame still being decoded (``deliverLater``)."""
        if self._decoding is not None:
            self.deliverLater(resolved(frame))
        else:
            self._deliverNow(frame)

    def deliverLater(self, frame: Awaitable[Frame]) -> None:
        """Delivers a frame that is still being decoded (inflated in a browser); later frames wait for it, in order."""
        previous = self._decoding
        tail: list[asyncio.Future[None]] = []

        async def next() -> None:
            try:
                # `(decoding ?? Promise.resolve()).then(...)`: the chain runs its steps on later turns, never inline,
                # even when the frame is already decoded.
                await asyncio.sleep(0)
                if previous is not None:
                    await previous
                decoded = await frame
            except Exception:  # noqa: BLE001 - an undecodable frame is a protocol error
                self.close("protocol error")
            else:
                self._deliverNow(decoded)
            finally:
                if self._decoding is tail[0]:
                    self._decoding = None

        current = background(next())
        tail.append(current)
        self._decoding = current

    def _deliverNow(self, frame: Frame) -> None:
        if self._closed:
            return
        # `frame.t` throws for null (a transport's caller reports it as a protocol error); any other value without
        # `t: "bye"` goes to the receiver, which rejects a malformed frame itself.
        if frame is None or frame is UNDEFINED:
            raise TypeError("Cannot read properties of null (reading 't')")
        if isinstance(frame, dict) and frame.get("t") == "bye":
            reason = frame.get("reason")
            self.ended("peer closed" if reason is None else reason)
            return
        if self._handler is None:
            self._queue.append(frame)
        else:
            self._handler(frame)

    def ended(self, reason: str) -> None:
        """Marks the link closed (transport ended) and notifies once."""
        if self._closed:
            return
        self._closed = True
        self._queue = []
        try:
            self.shutdown()
        finally:
            handlers = list(self._closeHandlers)
            self._closeHandlers.clear()
            for handler in handlers:
                handler(reason)


# ─── payload packing ─────────────────────────────────────────────────────────
#
# A transport optimization, invisible above the links. A frame's payload (call args, a result value, a state snapshot)
# whose JSON exceeds `PACK_THRESHOLD` characters is sent deflated. A host that only relays the frame forwards the
# deflated bytes unread (`Packed`); the host that uses the payload inflates it (`unpack`). Only hosts with zlib deflate
# (synchronously, so frames keep their order); a browser inflates with `DecompressionStream` before delivery.

# Payloads whose JSON is longer than this many characters are deflated.
PACK_THRESHOLD: Final = 64 * 1024


def _deflateRaw(text: str) -> bytes:
    compressor = zlib.compressobj(1, zlib.DEFLATED, -15)
    return compressor.compress(text.encode("utf-8")) + compressor.flush()


def _inflateRaw(data: bytes) -> str:
    """``zlib.inflateRawSync(data).toString("utf8")``: an invalid sequence becomes U+FFFD, a byte order mark stays."""
    decompressor = zlib.decompressobj(-15)
    return Utf8Decoder(ignoreBOM=True).decode(decompressor.decompress(data) + decompressor.flush(), True)


class Packed:
    """A deflated JSON payload, kept as received until a host uses it."""

    __slots__ = ("bytes",)

    def __init__(self, data: bytes) -> None:
        self.bytes = data


def unpack(value: Any) -> Any:
    """A frame payload as its user needs it: inflated and parsed if it arrived packed."""
    if not isinstance(value, Packed):
        return value
    if _zlib is None:
        raise TypeError("packed payload without zlib")
    return jsonParse(_inflateRaw(value.bytes))


PAYLOAD_FIELDS: Final = frozenset({"args", "value"})


def payloadField(frame: Frame) -> str | None:
    if frame["t"] == "call":
        return None if frame.get("args", UNDEFINED) is UNDEFINED else "args"
    if frame["t"] == "result" or frame["t"] == "state":
        return None if frame.get("value", UNDEFINED) is UNDEFINED else "value"
    return None


def envelopeOf(frame: Frame, field: str) -> dict[str, Any]:
    """The frame without its payload field (the envelope)."""
    envelope = dict(frame)
    envelope.pop(field, None)
    return envelope


def packedBytes(value: Any, compress: bool, json: str | None = None) -> bytes | None:
    """The payload's deflated bytes: as received, or deflated now if large. None: send it as JSON."""
    if isinstance(value, Packed):
        return value.bytes
    if not compress or _zlib is None or json is None or jsLength(json) <= PACK_THRESHOLD:
        return None
    return _deflateRaw(json)


def packObject(frame: Frame, compress: bool) -> Any:
    """For object transports (IPC, memory): a packed payload travels as ``z: [field, base64]``."""
    field = payloadField(frame)
    if field is None:
        return frame
    value = frame[field]
    json = jsonStringify(value) if compress and _zlib is not None and not isinstance(value, Packed) else None
    bytes_ = packedBytes(value, compress, json)
    if bytes_ is None:
        return frame
    return {**envelopeOf(frame, field), "z": [field, base64.b64encode(bytes_).decode("ascii")]}


def unpackObject(message: Any) -> Any:
    if not isinstance(message, dict) or "z" not in message:
        return message
    rest = dict(message)
    z = rest.pop("z")
    if not isinstance(z, list) or len(z) < 2 or z[0] not in PAYLOAD_FIELDS or not isinstance(z[1], str):
        raise TypeError("bad packed payload")
    return {**rest, z[0]: Packed(base64.b64decode(z[1]))}


def packMessage(frame: Frame, compress: bool) -> str | bytes:
    """For WebSockets: a frame with a packed payload is a binary message, ``[envelope length: u32 BE][envelope JSON,
    with z: field][deflated payload]``; any other frame is text. The payload's JSON is spliced in, not encoded
    twice."""
    field = payloadField(frame)
    if field is None:
        return jsonStringify(frame)
    value = frame[field]
    json = None if isinstance(value, Packed) else jsonStringify(value)
    bytes_ = packedBytes(value, compress, json)
    envelope = envelopeOf(frame, field)
    if bytes_ is None:
        head = jsonStringify(envelope)
        return f"{head[:-1]},{jsonStringify(field)}:{json}}}"
    head_ = jsonStringify({**envelope, "z": field}).encode("utf-8")
    return struct.pack(">I", len(head_)) + head_ + bytes_


def splitMessage(data: bytes | bytearray | memoryview) -> tuple[dict[str, Any], str, bytes]:
    """The envelope of a binary message and its payload bytes (any bytes-like view, as upstream takes any
    ``Uint8Array``)."""
    data = bytes(data)
    (length,) = struct.unpack(">I", data[:4])
    # `new TextDecoder().decode(...)`.
    envelope = jsonParse(Utf8Decoder().decode(data[4 : 4 + length], True))
    if not isinstance(envelope, dict):
        raise TypeError("bad envelope")
    rest = dict(envelope)
    field = rest.pop("z", None)
    if not isinstance(field, str) or field not in PAYLOAD_FIELDS:
        raise TypeError("bad packed payload")
    return rest, field, bytes(data[4 + length :])


def inflateMessage(data: bytes | bytearray | memoryview) -> str:
    """The text frame of a binary (packed) WebSocket message, inflated: for a transport that relays text frames only
    (the TUI launcher feeding its sandbox). Needs zlib."""
    if _zlib is None:
        raise TypeError("inflateMessage needs zlib")
    envelope, field, payload = splitMessage(data)
    head = jsonStringify(envelope)
    return f"{head[:-1]},{jsonStringify(field)}:{_inflateRaw(payload)}}}"


async def inflate(data: bytes) -> Any:
    """Inflates without zlib (browsers): ``DecompressionStream("deflate-raw")`` read by ``Response.text()``."""
    decompressor = zlib.decompressobj(-15)
    return jsonParse(Utf8Decoder().decode(decompressor.decompress(data) + decompressor.flush(), True))


def parseFrame(data: str) -> Frame:
    """Parses a JSON frame; anything else is a protocol error."""
    frame = unpackObject(jsonParse(data))
    if not isinstance(frame, dict) or not isinstance(frame.get("t"), str):
        raise TypeError("not a frame")
    return frame


# ─── memory ──────────────────────────────────────────────────────────────────


class MemoryLink(BaseLink):
    def __init__(self) -> None:
        super().__init__()
        self.peer: MemoryLink | None = None
        self._severed = False
        # `#pending`, the promise chain of this direction: the steps not run yet, run one per turn in order; a step
        # starts one turn after the previous one finished, so what a delivery schedules runs before the next delivery.
        self._pending: deque[Callable[[], None]] = deque()
        self._step: asyncio.Handle | None = None
        # A step threw: the chain is rejected, and every later step is skipped.
        self._rejected = False

    def sever(self) -> None:
        """Simulates a dead network: frames are dropped silently in both directions and no close is signalled."""
        self._severed = True
        if self.peer is not None:
            self.peer._severed = True

    def transmit(self, frame: Frame) -> int | None:
        if self._severed:
            return None
        # A JSON round trip detaches the frame (and rejects what JSON would mangle silently).
        data = jsonStringify(packObject(frame, True))
        peer = self.peer

        def deliver() -> None:
            if not self._severed and peer is not None:
                peer.deliver(parseFrame(data))

        # One promise chain per direction keeps delivery FIFO and asynchronous, like a real transport.
        self._then(deliver)
        return jsLength(data)

    def shutdown(self) -> None:
        peer = self.peer

        def end() -> None:
            if peer is not None:
                peer.ended("peer closed")

        self._then(end)

    def send(self, frame: Frame) -> int | None:
        with _operation():
            return super().send(frame)

    def close(self, reason: str = "closed") -> None:
        with _operation():
            super().close(reason)

    def _then(self, step: Callable[[], None]) -> None:
        """``this.#pending = this.#pending.then(step)``."""
        if self._rejected:
            return
        self._pending.append(step)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # MISAKA fork: JavaScript always has a microtask queue; Python has no loop outside `asyncio.run`, so the
            # steps of a link sent on or closed from plain code wait in one queue (`_microtasks`, the microtask queue's
            # stand-in) and run, in order, once the outermost `send`/`close` returned: the closing side ends with its
            # own reason before the peer's `peer closed` reaches it, and the peer is told.
            if self._step is not None:
                self._step.cancel()
                self._step = None
            while self._pending:
                _microtasks.queue.append((self, self._pending.popleft()))
            if _microtasks.depth == 0:
                _drainMicrotasks()
            return
        if self._step is None:
            self._step = loop.call_soon(self._next)

    def _next(self) -> None:
        # `_step` stays set while the step runs: a frame it sends on this link waits for the step to finish.
        try:
            if self._pending and not self._rejected:
                self._run(self._pending.popleft())
        finally:
            if self._pending and not self._rejected:
                self._step = asyncio.get_running_loop().call_soon(self._next)
            else:
                self._step = None

    def _run(self, step: Callable[[], None]) -> None:
        try:
            step()
        except BaseException:
            # A rejected chain skips every later step; the rejection nobody handles is reported (the loop's handler,
            # as Node reports an unhandled rejection).
            self._rejected = True
            self._pending.clear()
            raise


class _Microtasks(threading.local):
    """The memory links' steps run without an event loop (one queue per thread, as one microtask queue per realm)."""

    def __init__(self) -> None:
        self.queue: deque[tuple[MemoryLink, Callable[[], None]]] = deque()
        # Operations (`send`, `close`) running now; the queue drains when the outermost returns.
        self.depth = 0


_microtasks = _Microtasks()


@contextlib.contextmanager
def _operation() -> Iterator[None]:
    """A memory link's `send`/`close`: without a running loop, the steps it chained run after it returned."""
    _microtasks.depth += 1
    try:
        yield
    finally:
        _microtasks.depth -= 1
        if _microtasks.depth == 0 and _microtasks.queue:
            _drainMicrotasks()


def _drainMicrotasks() -> None:
    """Runs the queued steps in order (a step may queue more); a step that throws rejects its link's chain, the others
    still run, and the first error is raised at the end."""
    queue = _microtasks.queue
    _microtasks.depth += 1
    failure: BaseException | None = None
    try:
        while queue:
            link, step = queue.popleft()
            if link._rejected:
                continue
            try:
                link._run(step)
            except Exception as error:  # noqa: BLE001 - the other steps still run; the first error is raised after them
                failure = failure or error
    finally:
        _microtasks.depth -= 1
    if failure is not None:
        raise failure


def memoryLinkPair() -> tuple[MemoryLink, MemoryLink]:
    """Two connected in-memory links (tests)."""
    a = MemoryLink()
    b = MemoryLink()
    a.peer = b
    b.peer = a
    return a, b


# ─── IPC ─────────────────────────────────────────────────────────────────────


class IpcChannel(Protocol):
    """``process`` in a forked child, or the parent's child process (structural; see ``manager.py`` for misaka's
    channels)."""

    @property
    def connected(self) -> bool: ...

    def send(self, message: Any, callback: Callable[[BaseException | None], None] | None = None) -> bool:
        """With a callback, a failed write (the peer process exited and the pipe is broken: ``EPIPE``) is reported to
        it instead of raised."""
        ...

    def on(self, event: str, listener: Callable[..., None]) -> Any: ...

    def off(self, event: str, listener: Callable[..., None]) -> Any: ...


class IpcLink(BaseLink):
    """Chord frames on an IPC channel are wrapped as ``{"chord2": frame}``; other messages (control) pass by. Both ends
    are processes of one application, so large payloads are packed both ways. The channel serializes the messages
    itself, so the link reports no sizes and is not windowed: both ends are local processes."""

    def __init__(self, channel: IpcChannel) -> None:
        super().__init__()
        self._channel = channel
        channel.on("message", self._onMessage)
        channel.on("disconnect", self._onDisconnect)

    def _onMessage(self, message: Any) -> None:
        # `message?.chord2 === undefined`: a message without the key is control; `{"chord2": null}` is a bad frame.
        if not isinstance(message, dict) or "chord2" not in message:
            return
        frame = message["chord2"]
        try:
            self.deliver(unpackObject(frame))
        except Exception:  # noqa: BLE001 - a malformed frame is a protocol error
            self.close("protocol error")

    def _onDisconnect(self, *_: Any) -> None:
        self.ended("ipc disconnected")

    def _onSent(self, error: BaseException | None) -> None:
        """A write can fail after the peer exited and before the channel reports ``disconnect`` (an ``ack`` or a
        ``bye`` sent then). The peer is gone either way: the link ends, as on ``disconnect``."""
        if error is not None:
            self.ended(f"ipc send failed: {_errorCode(error)}")

    def transmit(self, frame: Frame) -> None:
        if self._channel.connected:
            self._channel.send({"chord2": packObject(frame, True)}, self._onSent)

    def shutdown(self) -> None:
        self._channel.off("message", self._onMessage)
        self._channel.off("disconnect", self._onDisconnect)


def _errorCode(error: BaseException) -> str:
    """``error.code ?? error.message``: a system error's code is the name of its errno (``EPIPE``, ``ECONNRESET``), as
    Node gives it."""
    code = getattr(error, "code", None)
    if code is not None:
        return str(code)
    if isinstance(error, OSError) and error.errno in errno.errorcode:
        return errno.errorcode[error.errno]
    return errorMessage(error)


def ipcLink(channel: IpcChannel) -> IpcLink:
    return IpcLink(channel)


# ─── WebSocket ───────────────────────────────────────────────────────────────


class WebSocketLike(Protocol):
    """The WHATWG WebSocket surface (``readyState``, ``send``, ``close``, ``addEventListener``). misaka has no WHATWG
    WebSocket; ``websocket.py`` adapts a ``websockets`` connection to it."""

    @property
    def readyState(self) -> int: ...

    def send(self, data: str | bytes) -> None: ...

    def close(self, code: int | None = None, reason: str | None = None) -> None: ...

    def addEventListener(self, type: str, listener: Callable[..., None]) -> None: ...


OPEN: Final = 1


class WebSocketLinkOptions(TypedDict, total=False):
    # Deflate large payloads of outgoing frames (default false; the server's side of client sockets).
    compress: bool
    # Accept packed (binary) frames from the peer (default true; false for a server's untrusted clients).
    acceptPacked: bool


class WebSocketLink(BaseLink):
    def __init__(self, socket: WebSocketLike, options: WebSocketLinkOptions | None = None) -> None:
        super().__init__()
        options = options or {}
        self._socket = socket
        self._compress = options.get("compress", False)
        acceptPacked = options.get("acceptPacked", True)
        self._outbox: list[str | bytes] = []
        # Binary messages as bytes (browsers default to Blob).
        if hasattr(socket, "binaryType"):
            socket.binaryType = "arraybuffer"  # type: ignore[attr-defined]

        def onOpen(*_: Any) -> None:
            outbox, self._outbox = self._outbox, []
            for data in outbox:
                socket.send(data)

        def onMessage(event: Any) -> None:
            try:
                data = event.data if hasattr(event, "data") else event["data"]
                if isinstance(data, str):
                    self.deliver(parseFrame(data))
                    return
                if not acceptPacked:
                    raise TypeError("packed frames are not accepted")
                if not isinstance(data, bytes | bytearray | memoryview):
                    raise TypeError("unknown message type")
                envelope, field, payload = splitMessage(bytes(data))
                if _zlib is not None:
                    self.deliver({**envelope, field: Packed(payload)})
                else:

                    async def decoded() -> Frame:
                        return {**envelope, field: await inflate(payload)}

                    self.deliverLater(start(decoded()))
            except Exception:  # noqa: BLE001 - a malformed message is a protocol error
                self.close("protocol error")

        socket.addEventListener("open", onOpen)
        socket.addEventListener("message", onMessage)
        socket.addEventListener("close", lambda *_: self.ended("socket closed"))

    def transmit(self, frame: Frame) -> int:
        data = packMessage(frame, self._compress)
        if self._socket.readyState == OPEN:
            self._socket.send(data)
        else:
            self._outbox.append(data)
        return jsLength(data) if isinstance(data, str) else len(data)

    def shutdown(self) -> None:
        self._outbox = []
        if self._socket.readyState <= OPEN:
            self._socket.close(1000)


def webSocketLink(socket: WebSocketLike, options: WebSocketLinkOptions | None = None) -> WebSocketLink:
    return WebSocketLink(socket, options)


__all__ = [
    "PACK_THRESHOLD",
    "BaseLink",
    "IpcChannel",
    "IpcLink",
    "Link",
    "MemoryLink",
    "Packed",
    "WebSocketLike",
    "WebSocketLink",
    "WebSocketLinkOptions",
    "inflateMessage",
    "ipcLink",
    "memoryLinkPair",
    "unpack",
    "webSocketLink",
]

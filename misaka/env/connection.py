"""One connection to a pi-env daemon over its stdin and stdout.

Translated from pi's ``packages/env/src/connection.ts`` (v1.3.0). The daemon is a child process driven through asyncio;
a request returns a future that starts at the call, as a JavaScript promise does.
"""

from __future__ import annotations

import asyncio
import inspect
import secrets
import struct
import subprocess
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, NotRequired, TypedDict

from misaka.utils.js import (
    Interval,
    clearInterval,
    clearTimeout,
    eager,
    errorMessage,
    formatNumber,
    jsonParse,
    jsonStringify,
    jsString,
    jsTrim,
    rejectedPromise,
    setInterval,
    setTimeout,
    toWellFormed,
)

# Frame types (docs/protocol.md).
REQUEST = 1
RESULT = 2
ERROR = 3
EVENT = 4
CANCEL = 5
PING = 6

MAX_FRAME = 16 * 1024 * 1024
PING_INTERVAL_MS = 5000
# The daemon pings every five seconds; this much silence means the connection is gone.
SILENCE_LIMIT_MS = 30_000
# How long starting the daemon and its `hello` may take.
START_TIMEOUT_MS = 60_000

type Json = dict[str, Any]


class RemoteError(Exception):
    """A failure reported by the daemon, with its Node-style code."""

    def __init__(self, fields: Json) -> None:
        message = fields["message"] if isinstance(fields.get("message"), str) else "Remote operation failed"
        super().__init__(message)
        self.name = "RemoteError"
        self.message = message
        self.code: str = fields["code"] if isinstance(fields.get("code"), str) else "unknown"
        self.path: str | None = fields["path"] if isinstance(fields.get("path"), str) else None
        self.fields = fields


class RemoteInfo(TypedDict):
    """What ``hello`` reports about the remote machine. ``separator``: ``/``, or ``\\`` on Windows. ``cwd``: the daemon's
    working directory, where Node's ``path.resolve`` would fall back to ``process.cwd()``. ``driveCwds``: Windows'
    per-drive working directories (``C:`` → ``C:\\work``) of the ``=C:`` variables. ``features``: operations added to
    protocol 1 that this daemon supports, such as ``listFiles``; absent from older daemons."""

    protocol: int
    version: str
    os: str
    arch: str
    home: str
    tmpdir: str
    separator: str
    cwd: str
    driveCwds: dict[str, str]
    pid: int
    features: NotRequired[list[str]]


def nodePlatform(info: dict[str, Any]) -> dict[str, str]:
    """Node's names for the daemon's system and architecture, which ``hello`` reports in Rust's spelling: ``windows`` is
    ``win32``, ``macos`` is ``darwin``, ``x86_64`` is ``x64``, ``aarch64`` is ``arm64``, so they compare with
    ``process.platform`` and ``process.arch``. Other names are kept."""
    platforms = {"windows": "win32", "macos": "darwin"}
    archs = {"x86_64": "x64", "aarch64": "arm64", "x86": "ia32"}
    return {"platform": platforms.get(info["os"], info["os"]), "arch": archs.get(info["arch"], info["arch"])}


class ConnectionOptions(TypedDict):
    """``command``: the command that starts the daemon, before its ``serve --token <hex>`` arguments, e.g. ``["ssh",
    "-T", "--", "host", "~/.pi/mobile/tools/pi-env"]``. A function computes it at each start (detecting and deploying
    first, for example); its failure fails that start, and the next request tries again. ``onLog`` receives the daemon's
    and the transport's diagnostic output."""

    command: Sequence[str] | Callable[[], Awaitable[Sequence[str]]]
    onLog: NotRequired[Callable[[str], None]]


class Reply(TypedDict):
    """``session``: the daemon session that answered; handles from it are only valid while it lives."""

    json: Json
    payload: bytes
    session: int


class _Pending:
    __slots__ = ("onEvent", "reject", "resolve")

    def __init__(
        self,
        resolve: Callable[[Reply], None],
        reject: Callable[[BaseException], None],
        onEvent: Callable[[Json, bytes], None] | None,
    ) -> None:
        self.resolve = resolve
        self.reject = reject
        self.onEvent = onEvent


class RequestOptions(TypedDict, total=False):
    """``signal``: aborting sends ``cancel``; the request still settles with the daemon's result or error. ``onEvent``:
    progress events of the request, such as ``exec`` output. ``onStart``: the request's id and session, for ``kill``.
    ``session``: only run in this daemon session, for requests on handles it opened. After the connection was lost and
    started again, such requests fail instead of reaching a daemon that never opened the handle."""

    payload: bytes
    signal: Any
    onEvent: Callable[[Json, bytes], None]
    onStart: Callable[[int, int], None]
    session: int


def wellFormed(value: Any) -> Any:
    """Lone surrogates become U+FFFD, as Node encodes strings for system calls (the replacer of ``JSON.stringify``, which
    sees values, not keys)."""
    if isinstance(value, str):
        return toWellFormed(value)
    if isinstance(value, dict):
        return {key: wellFormed(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [wellFormed(item) for item in value]
    return value


def frame(type: int, id: int, json: Json, payload: bytes = b"") -> bytes:
    body = jsonStringify(wellFormed(json)).encode("utf-8")
    header = struct.pack(">IBII", 9 + len(body) + len(payload), type, id, len(body))
    return header + body + bytes(payload)


def lost(message: str) -> RemoteError:
    """The connection failed or ended; a request may succeed once it is started again."""
    return RemoteError({"code": "unknown", "message": message, "lost": True})


def isConnectionLost(error: Any) -> bool:
    """Whether an error means the connection was lost, not that the daemon refused the request."""
    return isinstance(error, RemoteError) and error.fields.get("lost") is True


def closed() -> RemoteError:
    return RemoteError({"code": "unknown", "message": "Connection closed"})


def _nowMs() -> float:
    return time.time() * 1000


class _DaemonProtocol(asyncio.SubprocessProtocol):
    """The ``data``, ``exit`` and ``error`` events of Node's ``ChildProcess``."""

    def __init__(self) -> None:
        self.transport: asyncio.SubprocessTransport | None = None
        self.onStdout: Callable[[bytes], None] = lambda _chunk: None
        self.onStderr: Callable[[bytes], None] = lambda _chunk: None
        self.onExit: list[Callable[[int | None], None]] = []
        self.exited = False

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def pipe_data_received(self, fd: int, data: bytes) -> None:
        if fd == 1:
            self.onStdout(data)
        elif fd == 2:
            self.onStderr(data)

    def process_exited(self) -> None:
        self.exited = True
        transport = self.transport
        returncode = transport.get_returncode() if transport is not None else None
        # Node reports a process killed by a signal with code `null`.
        code = returncode if returncode is not None and returncode >= 0 else None
        for listener in list(self.onExit):
            listener(code)
        if transport is not None:
            transport.close()


class _Child:
    """The parts of Node's ``ChildProcessWithoutNullStreams`` the connection uses."""

    def __init__(self, protocol: _DaemonProtocol) -> None:
        self.protocol = protocol

    def write(self, data: bytes) -> None:
        transport = self.protocol.transport
        if transport is None:
            return
        pipe: Any = transport.get_pipe_transport(0)
        if pipe is not None and not pipe.is_closing():
            pipe.write(data)

    def end(self) -> None:
        transport = self.protocol.transport
        if transport is None:
            return
        pipe = transport.get_pipe_transport(0)
        if pipe is not None:
            pipe.close()

    def kill(self) -> None:
        """``child.kill()``: SIGTERM."""
        transport = self.protocol.transport
        if transport is None:
            return
        if not self.protocol.exited:
            try:
                transport.terminate()
            except ProcessLookupError:
                pass
        # MISAKA fork: asyncio needs its transport closed; a process still running then gets killed outright, which
        # ends the daemon as SIGTERM would (it has no handler for either).
        transport.close()


class Session:
    """One running daemon."""

    def __init__(self, id: int, child: _Child, token: str) -> None:
        self.id = id
        self.child = child
        self.token = token
        self.pending: dict[int, _Pending] = {}
        # Received bytes not yet parsed, without concatenating on every chunk.
        self.chunks: list[bytes] = []
        self.buffered = 0
        self.synced = False
        self.lastSeen = _nowMs()
        self.timer: Interval | None = None
        self.live = True

    def peek(self, length: int) -> bytes:
        """The first ``length`` buffered bytes as one buffer, without consuming them."""
        if len(self.chunks[0]) < length:
            self.chunks = [b"".join(self.chunks)]
        return self.chunks[0][:length]

    def consume(self, length: int) -> None:
        self.buffered -= length
        while length > 0:
            first = self.chunks[0]
            if len(first) <= length:
                self.chunks.pop(0)
                length -= len(first)
            else:
                self.chunks[0] = first[length:]
                length = 0


class _Ready(TypedDict):
    info: RemoteInfo
    session: Session


def _observe(future: asyncio.Future[Any]) -> None:
    if not future.cancelled():
        future.exception()


class Connection:
    """One connection to a pi-env daemon: started lazily on the first request and started again after it is lost.
    Requests in flight when it is lost fail with code ``unknown``; for mutations their outcome is then unknown."""

    def __init__(self, options: ConnectionOptions) -> None:
        self._options = options
        self._session: Session | None = None
        self._ready: asyncio.Future[_Ready] | None = None
        self._sessions = 0
        self._nextId = 1
        self._closed = False

    async def info(self) -> RemoteInfo:
        """Connect if needed and return what the daemon reported about its machine."""
        return (await asyncio.shield(self._connect()))["info"]

    async def session(self) -> int:
        """Connect if needed and return the live session's id."""
        return (await asyncio.shield(self._connect()))["session"].id

    @eager
    async def request(self, op: str, json: Json, options: RequestOptions | None = None) -> Reply:
        options = {} if options is None else options
        if options.get("session") is not None:
            current = self._session
            if current is None or current.id != options["session"] or not current.live:
                raise lost("pi-env connection lost")
            return await self._send(current, op, json, options)
        session = (await asyncio.shield(self._connect()))["session"]
        return await self._send(session, op, json, options)

    def close(self) -> None:
        """Stop the daemon; it kills everything it started."""
        self._closed = True
        if self._session is not None:
            self._teardown(self._session, closed())

    def kill(self, id: int, session: int) -> None:
        """Kill one ``exec`` without aborting it."""
        current = self._session
        if current is not None and current.id == session and current.live:
            self._write(current, frame(CANCEL, id, {"mode": "kill"}))

    def _connect(self) -> asyncio.Future[_Ready]:
        if self._closed:
            return rejectedPromise(closed())
        if self._ready is None:
            # Started on the next turn, as the async function's failure is seen only after `catch` is attached.
            ready: asyncio.Future[_Ready] = asyncio.ensure_future(self._start())
            self._ready = ready

            # A failed start is not remembered: the next request tries again.
            def forget(future: asyncio.Future[_Ready]) -> None:
                if (future.cancelled() or future.exception() is not None) and self._ready is ready:
                    self._ready = None

            ready.add_done_callback(forget)
        return self._ready

    def _send(self, session: Session, op: str, json: Json, options: RequestOptions) -> asyncio.Future[Reply]:
        if not session.live:
            return rejectedPromise(lost("pi-env connection lost"))
        id = self._nextId
        self._nextId += 1
        future: asyncio.Future[Reply] = asyncio.get_running_loop().create_future()
        signal = options.get("signal")

        def onAbort() -> None:
            self._write(session, frame(CANCEL, id, {}))

        def done() -> None:
            if signal is not None:
                signal.removeEventListener("abort", onAbort)

        def resolve(value: Reply) -> None:
            done()
            if not future.done():
                future.set_result(value)

        def reject(error: BaseException) -> None:
            done()
            if not future.done():
                future.set_exception(error)

        session.pending[id] = _Pending(resolve, reject, options.get("onEvent"))
        if signal is not None:
            signal.addEventListener("abort", onAbort, once=True)
        onStart = options.get("onStart")
        if onStart is not None:
            onStart(id, session.id)
        self._write(session, frame(REQUEST, id, {**json, "op": op}, options.get("payload", b"")))
        if signal is not None and signal.aborted:
            onAbort()
        return future

    def _write(self, session: Session, data: bytes) -> None:
        if session.live:
            session.child.write(data)

    async def _start(self) -> _Ready:
        try:
            option = self._options["command"]
            command = option() if callable(option) else option
            if inspect.isawaitable(command):
                command = await command
        except Exception as error:  # noqa: BLE001 - the start fails with it
            raise RemoteError({"code": "spawn_error", "message": errorMessage(error), "lost": True}) from None
        if self._closed:
            raise closed()
        command = list(command)
        if not command:
            raise RemoteError({"code": "spawn_error", "message": "No daemon command"})
        program, *args = command
        token = secrets.token_hex(16)
        protocol = _DaemonProtocol()
        self._sessions += 1
        session = Session(self._sessions, _Child(protocol), token)
        self._session = session
        loop = asyncio.get_running_loop()
        failed: asyncio.Future[Any] = loop.create_future()
        failed.add_done_callback(_observe)

        def fail(error: BaseException) -> None:
            if not failed.done():
                failed.set_exception(error)

        startTimer = setTimeout(
            lambda: fail(lost(f"pi-env did not answer within {formatNumber(START_TIMEOUT_MS / 1000)} s")),
            START_TIMEOUT_MS,
        )
        protocol.onExit.append(lambda code: fail(lost(f"pi-env exited with code {jsString(code)} before it was ready")))
        protocol.onStdout = lambda chunk: self._onData(session, chunk)
        onLog = self._options.get("onLog")
        protocol.onStderr = lambda chunk: onLog(chunk.decode("utf-8", "replace")) if onLog is not None else None
        protocol.onExit.append(lambda _code: self._teardown(session, lost("pi-env connection lost")))
        try:
            await loop.subprocess_exec(
                lambda: protocol,
                program,
                *args,
                "serve",
                "--token",
                token,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except (OSError, ValueError) as error:
            fail(RemoteError({"code": "spawn_error", "message": str(error), "lost": True}))

        def ping() -> None:
            self._write(session, frame(PING, 0, {}))
            if _nowMs() - session.lastSeen > SILENCE_LIMIT_MS:
                self._teardown(session, lost("pi-env connection timed out"))

        session.timer = setInterval(ping, PING_INTERVAL_MS)
        try:
            hello = self._send(session, "hello", {"protocol": 1}, {})
            hello.add_done_callback(_observe)
            # `Promise.race`: whichever settles first.
            race: asyncio.Future[Reply] = loop.create_future()

            def settle(future: asyncio.Future[Any]) -> None:
                if race.done() or future.cancelled():
                    return
                error = future.exception()
                if error is not None:
                    race.set_exception(error)
                else:
                    race.set_result(future.result())

            hello.add_done_callback(settle)
            failed.add_done_callback(settle)
            json = (await race)["json"]
            if json.get("protocol") != 1:
                raise lost(f"Unsupported protocol {jsString(json.get('protocol'))}")
            return {"info": json, "session": session}  # type: ignore[typeddict-item]
        except BaseException as error:
            self._teardown(session, error if isinstance(error, Exception) else lost(str(error)))
            raise
        finally:
            clearTimeout(startTimer)

    def _teardown(self, session: Session, error: BaseException) -> None:
        if not session.live:
            return
        session.live = False
        clearInterval(session.timer)
        if self._session is session:
            self._session = None
            self._ready = None
        session.child.end()
        session.child.kill()
        pending = list(session.pending.values())
        session.pending.clear()
        for request in pending:
            request.reject(error)

    def _onData(self, session: Session, chunk: bytes) -> None:
        if not session.live:
            return
        session.lastSeen = _nowMs()
        session.chunks.append(chunk)
        session.buffered += len(chunk)
        if not session.synced:
            # Shell startup files may print before the daemon runs; skip everything before its sync line.
            marker = f"PI-ENV {session.token}\n".encode()
            everything = session.peek(session.buffered)
            index = everything.find(marker)
            if index == -1:
                if session.buffered > 1024 * 1024:
                    session.consume(session.buffered - len(marker))
                return
            noise = jsTrim(everything[:index].decode("utf-8", "replace"))
            onLog = self._options.get("onLog")
            if noise != "" and onLog is not None:
                onLog(noise)
            session.consume(index + len(marker))
            session.synced = True
        while session.live and session.buffered >= 4:
            length = int.from_bytes(session.peek(4), "big")
            if length < 9 or length > MAX_FRAME:
                self._teardown(session, lost("Corrupt frame from pi-env"))
                return
            if session.buffered < 4 + length:
                return
            body = session.peek(4 + length)[4:]
            type = body[0]
            id = int.from_bytes(body[1:5], "big")
            jsonLength = int.from_bytes(body[5:9], "big")
            try:
                if 9 + jsonLength > length:
                    raise ValueError("JSON length out of range")
                parsed = jsonParse(body[9 : 9 + jsonLength].decode("utf-8", "replace"))
                if not isinstance(parsed, dict):
                    raise TypeError("not an object")
                json: Json = parsed
            except (ValueError, TypeError):
                self._teardown(session, lost("Corrupt frame from pi-env"))
                return
            payload = bytes(body[9 + jsonLength :])
            session.consume(4 + length)
            self._dispatch(session, type, id, json, payload)

    def _dispatch(self, session: Session, type: int, id: int, json: Json, payload: bytes) -> None:
        pending = session.pending.get(id)
        if type == EVENT:
            if pending is not None and pending.onEvent is not None:
                pending.onEvent(json, payload)
            return
        if type != RESULT and type != ERROR:
            return
        if pending is None:
            return
        del session.pending[id]
        if type == RESULT:
            pending.resolve({"json": json, "payload": payload, "session": session.id})
        else:
            pending.reject(RemoteError(json))


__all__ = [
    "Connection",
    "ConnectionOptions",
    "Json",
    "RemoteError",
    "RemoteInfo",
    "Reply",
    "RequestOptions",
    "isConnectionLost",
    "nodePlatform",
]

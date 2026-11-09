"""Host manager: runs a child host process and keeps at most one instance of it alive.

Translated from pi's ``packages/chord/src/manager.ts``. MISAKA forks, forced by the platform:

- Node's ``child_process.fork`` with its IPC channel is a Python child process (``sys.executable``, the interpreter
  flags in ``execArgv``, the entry file, ``args``) with one end of a socket pair: its descriptor is passed in the
  environment variable ``CHORD2_IPC_FD`` (Node's ``NODE_CHANNEL_FD``), and messages are newline-delimited JSON, as Node's
  default IPC serialization is JSON. ``processChannel()`` is the child's side (Node's ``process`` with its ``send`` and
  ``"message"``/``"disconnect"`` events); ``ChildChannel`` is the parent's (the ``ChildProcess``). Descriptor passing
  needs a POSIX system. Node removes ``NODE_CHANNEL_FD`` from the child's environment at startup, so its own children
  do not inherit it; Python has no startup hook, so ``processChannel()`` removes ``CHORD2_IPC_FD`` when it adopts the
  descriptor (a child that spawns processes before that passes the variable on).
- Node learns of a child's exit from libuv's ``SIGCHLD`` handler; here a daemon thread per child waits for it (as
  asyncio's ``ThreadedChildWatcher`` does), so no executor thread is held for a child's lifetime.
- ``detached: true`` is a new session (``start_new_session``): the child leads a process group, so the whole tree can
  be killed.
- The process lock is an abstract socket on Linux and Android, a named pipe on Windows (``multiprocessing``'s
  ``AF_PIPE`` listener), and a socket file elsewhere; ``process.once("exit", ...)`` is ``atexit``.
"""

from __future__ import annotations

import asyncio
import atexit
import codecs
import errno
import hashlib
import inspect
import os
import signal as signals
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol, TypedDict

from misaka.chord._promise import (
    Deferred,
    awaited,
    background,
    deferred,
    quiet,
    race,
    start,
)
from misaka.chord.errors import ChordError
from misaka.chord.link import IpcLink, ipcLink
from misaka.utils.js import (
    UNDEFINED,
    clearInterval,
    clearTimeout,
    errorMessage,
    jsonParse,
    jsonStringify,
    nullishCoalesce,
    setInterval,
    setTimeout,
)

# Control messages share the IPC channel with chord frames: `{ chord2control: message }`.
# `{ type: "init", value } | { type: "ready" } | { type: "fatal", message, reason? } | { type: "shutdown" }
#  | { type: "status", activeCalls?, viewers?, durableTasks? }`.
type ControlMessage = dict[str, Any]


class Liveness(TypedDict):
    # Started but not ready yet.
    booting: bool
    activeCalls: int
    # Viewers the child reports (it counts heartbeating subscribers).
    viewers: int
    durableTasks: int
    # Milliseconds since the child last reported work (calls, viewers or tasks), or since it became ready.
    idleFor: float


class Exit(TypedDict):
    code: int | None
    signal: str | None


class Instance(Protocol):
    """A running child: its link (for ``host.accept``) and its exit."""

    pid: int
    link: IpcLink
    exited: asyncio.Future[Exit]

    def send(self, message: ControlMessage) -> None: ...

    def killTree(self) -> None:
        """Kills the child and everything it spawned."""
        ...

    def onControl(self, listener: Callable[[ControlMessage], None]) -> None: ...


class Driver(Protocol):
    def spawn(self) -> Instance: ...


class ForkDriverOptions(TypedDict, total=False):
    # The child's entry file.
    entry: str
    args: Sequence[str]
    env: Mapping[str, str]
    # Interpreter flags for the child, e.g. `["-X", "dev"]` (default: none).
    execArgv: Sequence[str]
    # Sent to the child as its first control message (`childControl().init`).
    init: Any
    # A lock name: one live process per name, across managers and processes. The child acquires it as a kernel lock
    # (`childControl().lock()`, before opening its storage); a second child fails to start with `unavailable/locked`.
    lock: str
    # Optional stdout/stderr tap. Streams are still mirrored to the parent's standard output.
    onOutput: Callable[[Literal["stdout", "stderr"], bytes], None]
    # Optional application metadata tap; control messages and chord frames keep their normal handling.
    onMessage: Callable[[Any], None]


# ─── IPC channels ────────────────────────────────────────────────────────────

IPC_ENV: Final = "CHORD2_IPC_FD"


class _SocketChannel:
    """An IPC channel over a connected stream socket: newline-delimited JSON, ``"message"`` and ``"disconnect"``
    events."""

    def __init__(self, sock: socket.socket) -> None:
        sock.setblocking(False)
        self._socket: socket.socket | None = sock
        self._loop = asyncio.get_running_loop()
        self._listeners: dict[str, list[Callable[..., None]]] = {"message": [], "disconnect": []}
        self._buffer = b""
        self._outbox = bytearray()
        self._callbacks: list[tuple[int, Callable[[BaseException | None], None] | None]] = []
        self._written = 0
        self._queued = 0
        self._loop.add_reader(sock.fileno(), self._read)

    @property
    def connected(self) -> bool:
        return self._socket is not None

    def on(self, event: str, listener: Callable[..., None]) -> _SocketChannel:
        self._listeners.setdefault(event, []).append(listener)
        return self

    def off(self, event: str, listener: Callable[..., None]) -> _SocketChannel:
        listeners = self._listeners.get(event, [])
        if listener in listeners:
            listeners.remove(listener)
        return self

    def send(self, message: Any, callback: Callable[[BaseException | None], None] | None = None) -> bool:
        if self._socket is None:
            error = BrokenPipeError(errno.EPIPE, "channel closed")
            if callback is None:
                raise error
            self._loop.call_soon(callback, error)
            return False
        data = (jsonStringify(message) + "\n").encode("utf-8")
        self._outbox += data
        self._queued += len(data)
        self._callbacks.append((self._queued, callback))
        self._flush()
        return True

    def _flush(self) -> None:
        sock = self._socket
        if sock is None:
            return
        try:
            while self._outbox:
                sent = sock.send(self._outbox)
                del self._outbox[:sent]
                self._written += sent
        except BlockingIOError:
            self._loop.add_writer(sock.fileno(), self._writable)
        except OSError as error:
            self._fail(error)
            return
        else:
            self._loop.remove_writer(sock.fileno())
        done = [callback for end, callback in self._callbacks if end <= self._written]
        self._callbacks = [(end, callback) for end, callback in self._callbacks if end > self._written]
        for callback in done:
            if callback is not None:
                callback(None)

    def _writable(self) -> None:
        self._flush()

    def _fail(self, error: BaseException) -> None:
        callbacks, self._callbacks = self._callbacks, []
        for _, callback in callbacks:
            if callback is not None:
                callback(error)
        self.disconnect()

    def _read(self) -> None:
        sock = self._socket
        if sock is None:
            return
        try:
            chunk = sock.recv(65536)
        except BlockingIOError:
            return
        except OSError:
            chunk = b""
        if chunk == b"":
            self.disconnect()
            return
        self._buffer += chunk
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            if not line:
                continue
            message = jsonParse(line.decode("utf-8"))
            for listener in list(self._listeners.get("message", [])):
                listener(message)

    def disconnect(self) -> None:
        sock = self._socket
        if sock is None:
            return
        self._socket = None
        self._loop.remove_reader(sock.fileno())
        self._loop.remove_writer(sock.fileno())
        sock.close()
        for listener in list(self._listeners.get("disconnect", [])):
            listener()


class ChildChannel(_SocketChannel):
    """The parent's side of a child's IPC channel."""


_processChannel: _SocketChannel | None = None


def processChannel() -> _SocketChannel | None:
    """The child's side of its IPC channel (Node's ``process``), or None in a process not forked with one."""
    global _processChannel
    if _processChannel is None:
        # Make sure it's not accidentally inherited by child processes (Node deletes NODE_CHANNEL_FD).
        descriptor = os.environ.pop(IPC_ENV, None)
        if descriptor is None:
            return None
        _processChannel = _SocketChannel(socket.socket(fileno=int(descriptor)))
    return _processChannel


# ─── process lock ────────────────────────────────────────────────────────────


async def processLock(name: str) -> Callable[[], Awaitable[None]]:
    """A process lock owned by the kernel for the holder's lifetime: an abstract socket (Linux, Android) or a named
    pipe (Windows) vanishes with its process, so there is no stale state to take over and no race between two
    would-be owners. Other platforms need a socket file, removed on exit; a leftover one nobody accepts on is taken
    over. The child acquires it (before opening storage), so the lock follows the process that owns the data."""
    key = hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]
    platform = sys.platform
    if platform == "win32":
        return _pipeLock(name, f"\\\\.\\pipe\\chord2-{key}")
    file = not (platform.startswith("linux") or hasattr(sys, "getandroidapilevel"))
    address = await lockSocketPath(f"chord2-{key}.sock") if file else f"\0chord2-{key}"
    while True:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(address)
            server.listen(16)
            break
        except OSError as error:
            server.close()
            if error.errno != errno.EADDRINUSE:
                raise
            if not file or await accepting(address):
                raise ChordError("unavailable", f"lock {name} is held by another process", {"reason": "locked"}) from None
            try:
                os.unlink(address)
            except FileNotFoundError:
                pass
    server.setblocking(False)
    loop = asyncio.get_running_loop()

    def acceptOne() -> None:
        try:
            connection, _ = server.accept()
            connection.close()
        except OSError:
            pass

    loop.add_reader(server.fileno(), acceptOne)

    def removeFile() -> None:
        try:
            os.unlink(address)
        except OSError:
            pass  # Already gone.

    if file:
        atexit.register(removeFile)

    async def release() -> None:
        if file:
            atexit.unregister(removeFile)
        loop.remove_reader(server.fileno())
        # `server.close()`: libuv unlinks the socket file before it closes the descriptor; the other way around, a
        # process that found the file unanswered and bound its own socket there would lose it to our unlink.
        if file:
            removeFile()
        server.close()

    return release


def _pipeLock(name: str, address: str) -> Callable[[], Awaitable[None]]:
    from multiprocessing.connection import Listener

    try:
        listener = Listener(address, family="AF_PIPE")
    except OSError:
        raise ChordError("unavailable", f"lock {name} is held by another process", {"reason": "locked"}) from None

    async def release() -> None:
        listener.close()

    return release


# sun_path holds 104 bytes on macOS and the BSDs, including the terminating NUL.
MAX_SOCKET_PATH_BYTES: Final = 103


def tmpdir() -> str:
    """Node's ``os.tmpdir()``, read at every call. MISAKA fork: ``tempfile.gettempdir()`` caches its first answer, so
    a changed ``TMPDIR`` would not count; on Windows it is Python's answer."""
    if sys.platform == "win32":
        return tempfile.gettempdir()
    path = os.environ.get("TMPDIR") or os.environ.get("TMP") or os.environ.get("TEMP") or "/tmp"
    return path[:-1] if len(path) > 1 and path.endswith("/") else path


async def lockSocketPath(file: str) -> str:
    """The lock socket's path: in the temporary directory, or, when that path does not fit in sun_path (a deep TMPDIR,
    such as a test runner's below macOS's per-user one), in a private per-user directory under /tmp. Other users must
    not be able to create or remove sockets there, so the directory has to be ours and closed to everyone else."""
    preferred = os.path.join(tmpdir(), file)
    if len(preferred.encode("utf-8")) <= MAX_SOCKET_PATH_BYTES:
        return preferred
    uid = os.getuid() if hasattr(os, "getuid") else 0
    directory = f"/tmp/chord2-{uid}"
    os.makedirs(directory, mode=0o700, exist_ok=True)
    stats = os.lstat(directory)
    if not stat.S_ISDIR(stats.st_mode) or stats.st_uid != uid or (stats.st_mode & 0o077) != 0:
        raise ChordError("unavailable", f"lock directory {directory} must be a directory only its owner can access")
    return os.path.join(directory, file)


async def accepting(address: str) -> bool:
    try:
        _, writer = await asyncio.open_unix_connection(address)
    except OSError as error:
        return error.errno not in (errno.ECONNREFUSED, errno.ENOENT)
    writer.close()
    return True


LOCK_ENV: Final = "CHORD2_LOCK"


def ignoreSendError(_error: BaseException | None) -> None:
    """A control message to a peer process that is gone: dropped (its exit or ``disconnect`` is reported
    separately)."""


@dataclass
class _ForkedInstance:
    pid: int
    link: IpcLink
    exited: asyncio.Future[Exit]
    send: Callable[[ControlMessage], None]
    killTree: Callable[[], None]
    onControl: Callable[[Callable[[ControlMessage], None]], None]


class _ForkDriver:
    def __init__(self, options: ForkDriverOptions) -> None:
        self._options = options

    def spawn(self) -> _ForkedInstance:
        options = self._options
        # A lock this process inherited (e.g. as a child host itself) is not the child's: it gets only its own.
        env = dict(os.environ)
        env.pop(LOCK_ENV, None)
        env.update(options.get("env") or {})
        if options.get("lock") is not None:
            env[LOCK_ENV] = options["lock"]
        parent, child = socket.socketpair()
        env[IPC_ENV] = str(child.fileno())
        onOutput = options.get("onOutput")
        # Detached: the child leads a process group, so the whole tree can be killed.
        process = subprocess.Popen(
            [sys.executable, *(options.get("execArgv") or []), options["entry"], *(options.get("args") or [])],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if onOutput else None,
            stderr=subprocess.PIPE if onOutput else None,
            pass_fds=(child.fileno(),),
            start_new_session=True,
        )
        child.close()
        loop = asyncio.get_running_loop()
        if onOutput:
            for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                if stream is not None:
                    _tap(loop, name, stream, onOutput)
        pid = process.pid
        channel = ChildChannel(parent)

        def killGroup() -> None:
            try:
                _killGroup(pid)
            except OSError:
                pass  # No group left.

        exited: asyncio.Future[Exit] = loop.create_future()

        def onExit(code: int) -> None:
            # However the leader ended (graceful, crash, kill), nothing it spawned outlives it.
            killGroup()
            if not exited.done():
                exited.set_result(_exitOf(code))

        def wait() -> None:
            code = process.wait()
            try:
                loop.call_soon_threadsafe(onExit, code)
            except RuntimeError:
                pass  # The loop is closed: nobody is left to tell.

        threading.Thread(target=wait, name=f"chord2-child-{pid}", daemon=True).start()
        listeners: list[Callable[[ControlMessage], None]] = []

        def onMessage(message: Any) -> None:
            tap = options.get("onMessage")
            if tap is not None:
                tap(message)
            control = message.get("chord2control") if isinstance(message, dict) else None
            if control is not None:
                for listener in listeners:
                    listener(control)

        channel.on("message", onMessage)

        def send(message: ControlMessage) -> None:
            # A child that exited before the channel reported it fails the write (EPIPE): its exit is what counts.
            if channel.connected:
                channel.send({"chord2control": message}, ignoreSendError)

        if options.get("init", UNDEFINED) is not UNDEFINED:
            send({"type": "init", "value": options["init"]})

        def killTree() -> None:
            try:
                _killGroup(pid)
            except OSError:
                process.kill()

        return _ForkedInstance(
            pid=pid,
            link=ipcLink(channel),
            exited=exited,
            send=send,
            killTree=killTree,
            onControl=listeners.append,
        )


def _tap(
    loop: asyncio.AbstractEventLoop,
    name: Literal["stdout", "stderr"],
    stream: Any,
    onOutput: Callable[[Literal["stdout", "stderr"], bytes], None],
) -> None:
    """``child[stream].on("data", ...)``: each chunk goes to ``onOutput``, then to this process's own stream; the pipe
    closes at its end, as a Node stream does."""
    descriptor = stream.fileno()
    # MISAKA fork: a text stream without a byte buffer (one a program put in place of `sys.stdout`) gets the bytes as
    # UTF-8 text, as Node's streams take any chunk.
    decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def readable() -> None:
        try:
            chunk = os.read(descriptor, 65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            chunk = b""
        if not chunk:
            loop.remove_reader(descriptor)
            stream.close()
            return
        onOutput(name, chunk)
        # `process[stream].write(chunk)`: the stream in place at this write.
        mirror = getattr(sys, name)
        if mirror is None:
            return
        buffer = getattr(mirror, "buffer", None)
        if buffer is not None:
            buffer.write(chunk)
            buffer.flush()
        else:
            mirror.write(decoder.decode(chunk))
            mirror.flush()

    os.set_blocking(descriptor, False)
    loop.add_reader(descriptor, readable)


def _exitOf(returncode: int) -> Exit:
    """The ``"exit"`` event's ``(code, signal)`` for a ``Popen.returncode``. Node names a signal from its own table
    (``signo_string``) and reports one it has no name for, such as a Linux real-time signal, as no signal: the exit code
    (libuv's, 0 for a signalled process) stands."""
    if returncode >= 0:
        return {"code": returncode, "signal": None}
    try:
        return {"code": None, "signal": signals.Signals(-returncode).name}
    except ValueError:
        return {"code": 0, "signal": None}


def _killGroup(pid: int) -> None:
    """``process.kill(-pid, "SIGKILL")``: the child's whole process group (it leads one, see ``start_new_session``)."""
    if hasattr(os, "killpg"):
        os.killpg(pid, getattr(signals, "SIGKILL", signals.SIGTERM))  # windows-footgun: ok - gated by hasattr
        return
    # Windows has no process groups to signal: the tree psutil can still see.
    import psutil

    try:
        root = psutil.Process(pid)
        for child in root.children(recursive=True):
            child.kill()
        root.kill()
    except psutil.NoSuchProcess as error:
        raise ProcessLookupError(pid) from error


def forkDriver(options: ForkDriverOptions) -> Driver:
    return _ForkDriver(options)


class HostManagerOptions(TypedDict, total=False):
    driver: Driver
    # Evaluated every `checkInterval` while running; `false` stops the child.
    keepAlive: Callable[[Liveness], bool]
    checkInterval: float
    # Milliseconds a child may take to shut down before its process tree is killed (default 5000).
    grace: float
    # Milliseconds to wait for `ready` (default 30000).
    readyTimeout: float
    # The child exited on its own (crash, fatal) or after a stop.
    onExit: Callable[[Exit], None]


def _now() -> float:
    return time.time() * 1000


class HostManager:
    """Keeps at most one child host alive: ``start`` (or the running one) once it reported ready, ``stop`` (asks the
    child to shut down, then kills its process tree after the grace period), ``replace`` (stop, wait for the exit,
    start). Serialized with each other."""

    def __init__(self, options: HostManagerOptions) -> None:
        self._options = options
        self._status: Literal["stopped", "starting", "running", "stopping"] = "stopped"
        self._instance: Any = None
        self._queue: asyncio.Future[Any] | None = None
        self._report = {"activeCalls": 0, "viewers": 0, "durableTasks": 0}
        # When the child last became idle; None while it reports work.
        self._idleSince: float | None = None
        self._booting = False
        self._timer: Any = None

    @property
    def status(self) -> str:
        return self._status

    @property
    def instance(self) -> Any:
        return self._instance

    @property
    def liveness(self) -> Liveness:
        return {
            "booting": self._booting,
            "activeCalls": self._report["activeCalls"],
            "viewers": self._report["viewers"],
            "durableTasks": self._report["durableTasks"],
            "idleFor": 0 if self._idleSince is None else _now() - self._idleSince,
        }

    def _serialized(self, step: Callable[[], Awaitable[Any]]) -> asyncio.Future[Any]:
        previous = self._queue

        async def next() -> Any:
            # `queue.then(step, step)`: the step runs a turn after the previous one settled, however it settled.
            if previous is None:
                await asyncio.sleep(0)
            else:
                await asyncio.wait([previous])
            return await step()

        # `queue = next.catch(() => {})`: the chain cannot fail, and nobody has to handle a step's failure.
        future = quiet(start(next()))
        self._queue = future
        # A promise cannot be cancelled: a caller that stops waiting (`asyncio.wait_for`) leaves the step running and
        # the steps after it queued behind it.
        return quiet(asyncio.shield(future))

    async def _doStop(self) -> None:
        current = self._instance
        if current is None:
            self._status = "stopped"
            self._booting = False
            return
        self._status = "stopping"
        if self._timer is not None:
            clearInterval(self._timer)
        current.send({"type": "shutdown"})
        late: Deferred[bool] = deferred()
        graceTimer = setTimeout(lambda: late.resolve(False), nullishCoalesce(self._options.get("grace"), 5000))

        async def exited() -> bool:
            # The child's exit is shared (`onExit`, the ready check): waiting for it never cancels it.
            await awaited(current.exited)
            return True

        graceful = await race([start(exited()), late.promise])
        clearTimeout(graceTimer)
        if not graceful:
            current.killTree()
        await awaited(current.exited)
        if self._instance is current:
            self._instance = None
        self._status = "stopped"

    async def _doStart(self) -> Any:
        if self._instance is not None and self._status == "running":
            return self._instance
        self._status = "starting"
        self._booting = True
        self._report = {"activeCalls": 0, "viewers": 0, "durableTasks": 0}
        try:
            current = self._options["driver"].spawn()
        except BaseException:
            self._status = "stopped"
            self._booting = False
            raise
        self._instance = current

        def onExited(done: asyncio.Future[Exit]) -> None:
            if self._instance is current:
                self._instance = None
                self._status = "stopped"
                if self._timer is not None:
                    clearInterval(self._timer)
            onExit = self._options.get("onExit")
            if onExit is not None and not done.cancelled() and done.exception() is None:
                onExit(done.result())

        current.exited.add_done_callback(onExited)
        ready: Deferred[None] = deferred()
        timeout = setTimeout(
            lambda: ready.reject(ChordError("unavailable", "child not ready in time")),
            nullishCoalesce(self._options.get("readyTimeout"), 30_000),
        )

        def settle(fn: Callable[[], None]) -> None:
            clearTimeout(timeout)
            fn()

        def exitedBeforeReady(done: asyncio.Future[Exit]) -> None:
            exit = done.result()
            what = exit["code"] if exit["code"] is not None else exit["signal"]
            settle(lambda: ready.reject(ChordError("unavailable", f"child exited before ready ({what})")))

        current.exited.add_done_callback(exitedBeforeReady)

        def onControl(message: ControlMessage) -> None:
            kind = message.get("type")
            if kind == "ready":
                settle(ready.resolve)
            elif kind == "fatal":
                reason = message.get("reason")
                settle(
                    lambda: ready.reject(
                        ChordError("unavailable", f"child failed: {message.get('message')}", {"reason": reason})
                    )
                )
            elif kind == "status":
                self._report = {
                    "activeCalls": nullishCoalesce(message.get("activeCalls"), 0),
                    "viewers": nullishCoalesce(message.get("viewers"), 0),
                    "durableTasks": nullishCoalesce(message.get("durableTasks"), 0),
                }
                # The idle clock starts at the busy → idle transition, not at the last busy report.
                if sum(self._report.values()) > 0:
                    self._idleSince = None
                elif self._idleSince is None:
                    self._idleSince = _now()

        current.onControl(onControl)
        try:
            await ready.promise
        except GeneratorExit:
            raise
        except BaseException:
            current.killTree()
            await awaited(current.exited)
            raise
        self._booting = False
        if sum(self._report.values()) == 0 and self._idleSince is None:
            self._idleSince = _now()
        self._status = "running"
        keepAlive = self._options.get("keepAlive")
        if keepAlive is not None:

            def check() -> None:
                if self._status == "running" and not keepAlive(self.liveness):
                    self._serialized(self._doStop)

            self._timer = setInterval(check, nullishCoalesce(self._options.get("checkInterval"), 1000))
        return current

    def start(self) -> asyncio.Future[Any]:
        """Starts the child (or returns the running one) once it reported ready. Serialized with stop/replace."""
        return self._serialized(self._doStart)

    def stop(self) -> asyncio.Future[None]:
        """Asks the child to shut down, then kills its process tree after the grace period."""
        return self._serialized(self._doStop)

    def replace(self) -> asyncio.Future[Any]:
        """Stop, wait for the exit, start."""

        async def replace() -> Any:
            await self._doStop()
            return await self._doStart()

        return self._serialized(replace)


def createHostManager(options: HostManagerOptions) -> HostManager:
    return HostManager(options)


@dataclass
class ChildControl:
    """The child side of the control channel (call ``childControl()`` in the child's entry)."""

    init: asyncio.Future[Any]
    # Acquires the manager's lock (if one was configured) as a kernel lock held for this process's lifetime. Call it
    # before opening storage. Fails with `unavailable/locked`, also reported to the manager as a fatal start error.
    lock: Callable[[], asyncio.Future[None]]
    ready: Callable[[], None]
    fatal: Callable[[Any], None]
    report: Callable[[Mapping[str, int]], None]
    onShutdown: Callable[[Callable[[], Any]], None]


def _unhandledShutdownRejection(error: BaseException) -> None:
    """MISAKA fork: Node reports a promise rejection nobody handles (and exits on it by default); asyncio's counterpart
    for a task failure nobody awaits is the loop's exception handler, which gets it here at once."""
    asyncio.get_running_loop().call_exception_handler(
        {"message": "chord shutdown handler failed (unhandled rejection)", "exception": error}
    )


# The process lock this child holds (released when the process exits).
_held: list[Callable[[], Awaitable[None]]] = []


def childControl(channel: Any = None) -> ChildControl:
    """The child side of the control channel."""
    channel = processChannel() if channel is None else channel

    def send(message: ControlMessage) -> None:
        # A parent that is gone fails the write (EPIPE); the `disconnect` that follows is how a child notices.
        if channel is not None and channel.connected:
            channel.send({"chord2control": message}, ignoreSendError)

    init: Deferred[Any] = deferred()
    # A set: a handler added twice runs once.
    shutdown: dict[Callable[[], Any], None] = {}

    def onMessage(message: Any) -> None:
        control = message.get("chord2control") if isinstance(message, dict) else None
        kind = None if not isinstance(control, dict) else control.get("type")
        if kind == "init":
            init.resolve(control.get("value"))  # type: ignore[union-attr]
        if kind == "shutdown":
            # `void handler()`: a handler that throws throws out of this listener; one that rejects is unhandled.
            for handler in list(shutdown):
                result = handler()
                if inspect.isawaitable(result):
                    background(result, _unhandledShutdownRejection)

    def onShutdown(handler: Callable[[], Any]) -> None:
        shutdown[handler] = None

    if channel is not None:
        channel.on("message", onMessage)

    def fatal(error: Any) -> None:
        message: ControlMessage = {"type": "fatal", "message": errorMessage(error)}
        if isinstance(error, ChordError) and error.reason is not None:
            message["reason"] = error.reason
        send(message)

    async def lock() -> None:
        name = os.environ.get(LOCK_ENV)
        if name is None:
            return
        try:
            _held.append(await processLock(name))
        except Exception as error:
            fatal(error)
            raise

    def report(status: Mapping[str, int]) -> None:
        send({"type": "status", **status})

    return ChildControl(
        init=init.promise,
        lock=lambda: start(lock()),
        ready=lambda: send({"type": "ready"}),
        fatal=fatal,
        report=report,
        onShutdown=onShutdown,
    )


__all__ = [
    "ChildChannel",
    "ControlMessage",
    "Driver",
    "Exit",
    "ForkDriverOptions",
    "HostManager",
    "HostManagerOptions",
    "Instance",
    "Liveness",
    "childControl",
    "createHostManager",
    "forkDriver",
    "processChannel",
    "processLock",
]

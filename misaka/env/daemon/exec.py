"""``exec``: Durable's ``NodeExecutionEnv.exec`` semantics (docs/semantics.md), output streamed as ``output`` events."""

from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
from typing import Any

from . import system
from .decode import StreamDecoder
from .errors import Failure
from .frame import EVENT, Frame, as_bool, as_f64, as_str, as_u64, get
from .output import Counter, Output
from .window import Pending, Window

EXIT_STDIO_GRACE = 0.1
# How often a delivery waiting for the link to drain looks again.
UNSENT_POLL = 0.02

# Messages to a running command: `(DATA, stream, bytes)`, `(EOF, stream)`, `(EXIT, returncode)`, `(WAIT_FAILED, text)`,
# `(CANCEL,)` (abort: the command settles as `aborted`), and `(KILL,)` (kill without aborting, as `cleanup()` does: the
# command settles with the killed process's status).
DATA = "data"
EOF = "eof"
EXIT = "exit"
WAIT_FAILED = "wait_failed"
CANCEL = "cancel"
KILL = "kill"


class Groups:
    """Commands this daemon started and has not reaped; killed when the connection ends."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pids: set[int] = set()

    def insert(self, pid: int) -> None:
        with self._lock:
            self._pids.add(pid)

    def remove(self, pid: int) -> None:
        with self._lock:
            self._pids.discard(pid)

    def pids(self) -> list[int]:
        with self._lock:
            return list(self._pids)


class Control:
    """Cancellation of one request, registered before the request runs so a cancel that arrives first is not lost."""

    def __init__(self) -> None:
        self.aborted = threading.Event()
        self.killed = threading.Event()
        self._lock = threading.Lock()
        self._sender: queue.SimpleQueue[tuple[Any, ...]] | None = None

    def cancel(self, kill: bool) -> None:
        with self._lock:
            if kill:
                self.killed.set()
            else:
                self.aborted.set()
            if self._sender is not None:
                self._sender.put((KILL,) if kill else (CANCEL,))

    def attach(self, sender: queue.SimpleQueue[tuple[Any, ...]]) -> None:
        with self._lock:
            self._sender = sender


class ExecRequest:
    def __init__(self, json: Any) -> None:
        argv = get(json, "argv")
        self.command = as_str(get(json, "command"))
        self.argv: list[str] | None = (
            list(argv) if isinstance(argv, list) and all(isinstance(item, str) for item in argv) else None
        )
        cwd = as_str(get(json, "cwd"))
        if cwd is None:
            raise Failure("EINVAL", "exec needs cwd")
        self.cwd = cwd
        env = get(json, "env")
        self.env: dict[str, Any] = dict(env) if isinstance(env, dict) else {}
        inherit_env = as_bool(get(json, "inheritEnv"))
        self.inherit_env = True if inherit_env is None else inherit_env
        self.shell_path = as_str(get(json, "shellPath"))
        timeout_ms = as_f64(get(json, "timeoutMs"))
        # Seconds.
        self.timeout = None if timeout_ms is None else timeout_ms / 1000
        spill = get(json, "spill")
        after_bytes = as_u64(get(spill, "afterBytes"))
        after_lines = as_u64(get(spill, "afterLines"))
        self.spill = None if after_bytes is None or after_lines is None else (after_bytes, after_lines)
        self.window = Window.from_json(get(json, "window"))

    @staticmethod
    def from_json(json: Any) -> ExecRequest:
        return ExecRequest(json)


def kill_group(pid: int) -> None:
    system.kill_tree(pid)


def random_uuid() -> str:
    data = bytearray(system.random_bytes())
    data[6] = (data[6] & 0x0F) | 0x40
    data[8] = (data[8] & 0x3F) | 0x80
    hex = data.hex()
    return f"{hex[0:8]}-{hex[8:12]}-{hex[12:16]}-{hex[16:20]}-{hex[20:32]}"


class Spill:
    """Spill of the complete raw output, started once it crosses either threshold, like Node's."""

    def __init__(self, after_bytes: int, after_lines: int) -> None:
        self.after_bytes = after_bytes
        self.after_lines = after_lines
        self.seen_bytes = 0
        self.seen_newlines = 0
        self.prefix: list[bytes] = []
        self.file: system.File | None = None
        self.path: str | None = None
        self.failed: str | None = None

    def push(self, chunk: bytes, tmpdir: str) -> None:
        if self.failed is not None or not chunk:
            return
        if self.file is None:
            self.seen_bytes += len(chunk)
            self.seen_newlines += chunk.count(b"\n")
            lines = self.seen_newlines + (1 if chunk[-1:] != b"\n" else 0)
            if self.seen_bytes <= self.after_bytes and lines <= self.after_lines:
                self.prefix.append(chunk)
                return
            error = self.start(tmpdir)
            if error is not None:
                self.failed = error
                return
        if self.file is not None:
            try:
                self.file.write_all(chunk)
            except OSError as error:
                self.failed = str(error)

    def start(self, tmpdir: str) -> str | None:
        """``createTempFile({ prefix: "pi-output-", suffix: ".log" })``: a fresh ``tmp-`` directory holding the file.
        Returns the error, if starting failed."""
        try:
            directory = system.mkdtemp(os.path.join(tmpdir, "tmp-"))
            path = os.path.join(directory, f"pi-output-{random_uuid()}.log")
            file = system.open(path, system.APPEND)
            for chunk in self.prefix:
                file.write_all(chunk)
        except (OSError, ValueError) as error:
            return str(error)
        self.prefix = []
        self.file = file
        self.path = path
        return None


def spawn(request: ExecRequest, program: str, args: list[str], piped_stdin: bool) -> subprocess.Popen[bytes]:
    child = subprocess.Popen(
        **system.configure(program, args, request.env, request.inherit_env),
        cwd=system.os_path(request.cwd),
        stdin=subprocess.PIPE if piped_stdin else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    system.after_spawn(child)
    return child


def spawn_reader(
    stream: int,
    pipe: Any,
    sender: queue.SimpleQueue[tuple[Any, ...]],
    stop: threading.Event,
    output: Output | None,
) -> threading.Thread:
    """Read one pipe on its own thread. Without an output window, reading waits while too much output is unsent."""

    def on_data(data: bytes) -> bool:
        sender.put((DATA, stream, data))
        if output is not None:
            output.wait_for_room(stop)
        return True

    def read() -> None:
        system.read_pipe(pipe, stop, on_data)
        sender.put((EOF, stream))

    thread = threading.Thread(target=read, name=f"pi-env-reader-{stream}", daemon=True)
    thread.start()
    return thread


def output_frame(id: int, stream: str, text: bytes, skipped: dict[str, Any] | None) -> Frame:
    json: dict[str, Any] = {"kind": "output", "stream": stream}
    if skipped is not None:
        json["skipped"] = skipped
    return Frame(EVENT, id, json, text)


def run(id: int, request: ExecRequest, tmpdir: str, output: Output, control: Control, groups: Groups) -> dict[str, Any]:
    """Run one command, sending output events for request ``id``, until it settles."""
    sender: queue.SimpleQueue[tuple[Any, ...]] = queue.SimpleQueue()
    control.attach(sender)
    if control.aborted.is_set():
        raise Failure("aborted", "aborted")
    # A string runs through the shell (as its last argument, or on stdin for legacy WSL bash); argv runs directly.
    stdin_command: str | None = None
    if request.command is not None:
        shell = system.shell_config(request.shell_path)
        program, args = shell.program, list(shell.args)
        if shell.command_on_stdin:
            stdin_command = request.command
        else:
            args.append(request.command)
    elif request.argv is not None:
        if not request.argv:
            raise Failure("spawn_error", "Empty argv: no program to run")
        program = system.resolve_program(request.argv[0], request.cwd, request.env)
        args = request.argv[1:]
    else:
        raise Failure("EINVAL", "exec needs command or argv")
    if not os.path.exists(system.os_path(request.cwd)):
        raise Failure(
            "spawn_error",
            f"Working directory does not exist: {request.cwd}\nCannot execute bash commands.",
        )
    try:
        child = spawn(request, program, args, stdin_command is not None)
    except (OSError, ValueError) as error:
        raise Failure("spawn_error", str(error)) from None
    if stdin_command is not None and child.stdin is not None:
        try:
            child.stdin.write(stdin_command.encode("utf-8"))
        except OSError:
            pass
        try:
            child.stdin.close()
        except OSError:
            pass
    pid = child.pid
    groups.insert(pid)
    deadline = None if request.timeout is None else time.monotonic() + request.timeout
    stop = threading.Event()
    # Without a window, output is not coalesced; reading then waits for the link.
    gate = output if request.window is None else None
    readers = [
        spawn_reader(0, child.stdout, sender, stop, gate),
        spawn_reader(1, child.stderr, sender, stop, gate),
    ]

    def wait() -> None:
        try:
            sender.put((EXIT, child.wait()))
        except Exception as error:  # noqa: BLE001 - the command settles with the failure
            sender.put((WAIT_FAILED, str(error)))

    threading.Thread(target=wait, name="pi-env-waiter", daemon=True).start()

    decoders = [StreamDecoder(), StreamDecoder()]
    spill = None if request.spill is None else Spill(*request.spill)
    pending = None if request.window is None else Pending(request.window)
    unsent = Counter()
    names = ("stdout", "stderr")

    def emit(stream: int, text: str) -> None:
        if not text:
            return
        data = text.encode("utf-8")
        if pending is not None:
            pending.push(names[stream], data)
        else:
            output.bulk(output_frame(id, names[stream], data, None), None)

    def deliver(pending: Pending) -> None:
        for event in pending.take():
            output.bulk(output_frame(id, event.stream, event.text, event.skipped), unsent)

    ended = [False, False]
    status: tuple[str, Any] | None = None
    idle_until: float | None = None
    timed_out = False
    aborted = False
    killed = False

    def kill() -> None:
        nonlocal killed
        if not killed:
            killed = True
            kill_group(pid)

    if control.killed.is_set():
        kill()
    try:
        while True:
            now = time.monotonic()
            # Node's timer stays armed until the command settles, including the grace period after exit.
            if not timed_out and deadline is not None and now >= deadline:
                timed_out = True
                kill()
            if status is not None and ended[0] and ended[1]:
                break
            if idle_until is not None and now >= idle_until:
                # A descendant may hold the pipes open after the process exited; stop waiting for it.
                break
            if pending is not None and not pending.is_empty() and now >= pending.next_send() and unsent.load() == 0:
                deliver(pending)
            wait_for = 3600.0
            if deadline is not None and not timed_out:
                wait_for = min(wait_for, max(deadline - now, 0.0))
            if idle_until is not None:
                wait_for = min(wait_for, max(idle_until - now, 0.0))
            if pending is not None and not pending.is_empty():
                wait_for = min(
                    wait_for, UNSENT_POLL if unsent.load() > 0 else max(pending.next_send() - now, 0.0)
                )
            try:
                message = sender.get(timeout=wait_for)
            except queue.Empty:
                continue
            kind = message[0]
            if kind == DATA:
                stream, data = message[1], message[2]
                emit(stream, decoders[stream].decode(data, False))
                if spill is not None:
                    spill.push(data, tmpdir)
                    if spill.failed is not None:
                        kill()
                if status is not None:
                    idle_until = time.monotonic() + EXIT_STDIO_GRACE
            elif kind == EOF:
                ended[message[1]] = True
            elif kind == EXIT:
                status = ("ok", message[1])
                idle_until = time.monotonic() + EXIT_STDIO_GRACE
            elif kind == WAIT_FAILED:
                status = ("error", message[1])
                idle_until = time.monotonic()
            elif kind == CANCEL:
                aborted = True
                kill()
            elif kind == KILL:
                kill()
        # Like Node destroying its streams: stop reading, so the pipes close even if a descendant still holds them.
        stop.set()
        for reader in readers:
            system.interrupt_reader(reader)
        groups.remove(pid)
        emit(0, decoders[0].decode(b"", True))
        emit(1, decoders[1].decode(b"", True))
        if pending is not None and not pending.is_empty():
            deliver(pending)
        spill_path = None if spill is None else spill.path

        def with_spill(failure: Failure) -> Failure:
            return failure.extra({"spillPath": spill_path}) if spill_path is not None else failure

        if timed_out:
            raise with_spill(Failure("timeout", "timeout"))
        if aborted:
            raise with_spill(Failure("aborted", "aborted"))
        if spill is not None and spill.failed is not None:
            raise Failure("unknown", f"Failed to preserve complete shell output: {spill.failed}")
        if status is None:
            raise Failure("spawn_error", "process did not report its exit")
        if status[0] == "error":
            raise Failure("spawn_error", status[1])
        result: dict[str, Any] = {"exitCode": system.exit_code(status[1])}
        if spill_path is not None:
            result["spillPath"] = spill_path
        return result
    finally:
        if spill is not None and spill.file is not None:
            spill.file.close()

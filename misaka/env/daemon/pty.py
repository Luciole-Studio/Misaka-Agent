"""``pty``: a program in a pseudo-terminal, for interactive terminals. Unlike ``exec``, output is the terminal's raw bytes,
and the program reads input: ``ptyWrite``, ``ptyResize`` and ``ptyForeground`` address the running ``pty`` request by
its id.

Ported from pi's ``packages/env/daemon/src/pty.rs`` (v1.3.0). Unix only, as upstream; elsewhere ``pty`` fails with
``ENOTSUP``. MISAKA fork: Rust's ``pre_exec`` makes the program a session leader whose controlling terminal is the pty
(``setsid``, ``TIOCSCTTY``) and resets its signals between ``fork`` and ``exec``. Python cannot run code there safely in a
process with threads, so the program starts through a short launcher, this interpreter running ``LAUNCHER``, which does
the same and then ``exec``s the program: the program keeps the launcher's process id, as it keeps the forked child's
upstream. The rest of Rust's ``Command::spawn`` is kept: the launcher ``exec``s with exactly the environment the daemon
computed (sent over a pipe, since the launcher's own interpreter adds to its environment: PEP 538's ``LC_CTYPE``, and
``__CF_USER_TEXT_ENCODING`` on macOS), searches ``PATH`` as ``execvp`` does, and reports a failure before the program runs
over a close-on-exec pipe, so the request fails with ``spawn_error`` before ``started``, as upstream.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
from typing import Any

from . import system
from .errors import Failure
from .exec import (
    CANCEL,
    DATA,
    EOF,
    EXIT,
    KILL,
    WAIT_FAILED,
    Control,
    Groups,
    kill_group,
)
from .frame import EVENT, Frame, as_bool, as_str, as_u64, get
from .output import Output

EXIT_GRACE = 0.1
"""After the program exits, output still arriving is collected until this much silence (seconds)."""
MAX_CELLS = 4096
"""Largest terminal size accepted, in cells."""
READ_CHUNK = 64 * 1024

LAUNCHER = """\
import errno, fcntl, os, signal, sys, termios
status = int(sys.argv[1])
os.set_inheritable(status, False)
try:
    source = int(sys.argv[2])
    chunks = []
    while True:
        chunk = os.read(source, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    os.close(source)
    env = {}
    for entry in b"".join(chunks).split(b"\\0")[:-1]:
        key, _, value = entry.partition(b"=")
        if key:
            env[key] = value
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    for number in range(1, 32):
        if number not in (signal.SIGKILL, signal.SIGSTOP):
            try:
                signal.signal(number, signal.SIG_DFL)
            except (OSError, ValueError, RuntimeError):
                pass
    signal.pthread_sigmask(signal.SIG_SETMASK, [])
    program = os.fsencode(sys.argv[3])
    argv = [os.fsencode(argument) for argument in sys.argv[4:]]

    def attempt(path):
        try:
            os.execve(path, argv, env)
        except OSError as error:
            if error.errno != errno.ENOEXEC:
                raise
        os.execve(b"/bin/sh", [b"sh" if sys.platform == "darwin" else b"/bin/sh", path] + argv[1:], env)

    if not program:
        raise OSError(errno.ENOENT, "")
    if b"/" in program:
        attempt(program)
    skipped = {errno.ENOENT, errno.ENOTDIR, errno.ESTALE, errno.ENODEV, errno.ETIMEDOUT}
    if sys.platform == "darwin":
        skipped |= {errno.ELOOP, errno.ENAMETOOLONG}
    found = errno.ENOENT
    denied = False
    path = env.get(b"PATH", b"/usr/bin:/bin" if sys.platform == "darwin" else b"/bin:/usr/bin")
    for directory in path.split(b":"):
        try:
            if directory:
                attempt(directory + b"/" + program)
            else:
                attempt(b"./" + program if sys.platform == "darwin" else program)
        except OSError as error:
            if error.errno == errno.EACCES:
                denied = True
            elif error.errno in skipped:
                if sys.platform != "darwin":
                    found = error.errno
            else:
                raise
    raise OSError(errno.EACCES if denied else found, "")
except BaseException as error:
    code = error.errno if isinstance(error, OSError) and error.errno else errno.EINVAL
    os.write(status, code.to_bytes(4, "big") + b"NOEX")
    os._exit(1)
"""
"""The launcher's source: ``pre_exec`` and ``exec`` in a fresh, single-threaded interpreter (see the module note). Its
arguments are the status pipe's write end, the environment pipe's read end, the program to run and the program's own
``argv``, ``argv[0]`` included. It reads the environment (``key=value`` entries, each ending in a NUL byte) to its end,
then makes the session, resets the signals and ``exec``s the program as Rust's ``execvp`` does (glibc's or macOS's: a
name without ``/`` is searched on the environment's ``PATH``, else the system's default path, an empty entry naming the
working directory; a file the system cannot run is run by ``/bin/sh``; a denied candidate makes the error ``EACCES``). On failure it writes the error number and ``NOEX`` to the status pipe, as
Rust's child does, and exits; a successful ``exec`` closes that pipe. MISAKA fork: an entry with an empty name, which
Python's ``execve`` cannot pass, is left out."""


class Terminal:
    """What ``ptyWrite``, ``ptyResize`` and ``ptyForeground`` reach: the terminal of one running ``pty`` request.
    ``pid``: the program's process id, which is also its process group's (it leads the terminal's session).

    The master descriptor is shared as Rust shares its ``Arc<File>``: ``Terminals.get`` holds the terminal (Rust clones
    the ``Arc``), ``release`` lets it go, and the descriptor closes with the last holder, so a resize or a foreground
    query that found the terminal just before it ended still works on its own descriptor, never on a closed or reused
    one. The ``pty`` request holds it from the start."""

    def __init__(self, input: queue.SimpleQueue[bytes | None], master: int, pid: int) -> None:
        self.input = input
        self.master = master
        self.pid = pid
        self.ended = False
        self._lock = threading.Lock()
        self._holders = 1

    def hold(self) -> None:
        with self._lock:
            self._holders += 1

    def release(self) -> None:
        with self._lock:
            self._holders -= 1
            last = self._holders == 0
        if last:
            try:
                os.close(self.master)
            except OSError:
                pass


class Terminals:
    """Running terminals by the id of their ``pty`` request."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._terminals: dict[int, Terminal] = {}

    def insert(self, id: int, terminal: Terminal) -> None:
        with self._lock:
            self._terminals[id] = terminal

    def remove(self, id: int) -> Terminal | None:
        with self._lock:
            return self._terminals.pop(id, None)

    def get(self, id: int) -> Terminal | None:
        """The terminal, held (see ``Terminal``); the caller releases it."""
        with self._lock:
            terminal = self._terminals.get(id)
            if terminal is not None:
                terminal.hold()
            return terminal


def size(json: Any, key: str) -> int:
    value = as_u64(get(json, key))
    if value is None or not 1 <= value <= MAX_CELLS:
        raise Failure("EINVAL", f"{key} must be between 1 and {MAX_CELLS}")
    return value


class PtyRequest:
    def __init__(self, json: Any) -> None:
        argv = get(json, "argv")
        if argv is None:
            self.argv: list[str] | None = None
        elif isinstance(argv, list) and len(argv) > 0 and all(isinstance(item, str) for item in argv):
            self.argv = list(argv)
        else:
            raise Failure("EINVAL", "argv must be a non-empty array of strings")
        cwd = as_str(get(json, "cwd"))
        if cwd is None:
            raise Failure("EINVAL", "pty needs cwd")
        self.cwd = cwd
        env = get(json, "env")
        self.env: dict[str, Any] = dict(env) if isinstance(env, dict) else {}
        inherit_env = as_bool(get(json, "inheritEnv"))
        self.inherit_env = True if inherit_env is None else inherit_env
        self.cols = size(json, "cols")
        self.rows = size(json, "rows")

    @staticmethod
    def from_json(json: Any) -> PtyRequest:
        return PtyRequest(json)


def find(terminals: Terminals, json: Any) -> Terminal:
    """The addressed terminal, held: the caller releases it."""
    target = as_u64(get(json, "target"))
    if target is None:
        raise Failure("EINVAL", "missing field target")
    terminal = terminals.get(target)
    if terminal is None:
        raise Failure("EBADF", "no running terminal with this id")
    return terminal


def write(terminals: Terminals, json: Any, payload: bytes) -> dict[str, Any]:
    """Queue input for the terminal; it is written in the order it arrives, without holding up other requests."""
    terminal = find(terminals, json)
    try:
        if terminal.ended:
            raise Failure("EBADF", "the terminal has ended")
        terminal.input.put(payload)
        return {}
    finally:
        terminal.release()


def resize(terminals: Terminals, json: Any) -> dict[str, Any]:
    """Change the terminal's size; the program receives ``SIGWINCH``."""
    terminal = find(terminals, json)
    try:
        cols, rows = size(json, "cols"), size(json, "rows")
        if not system.WINDOWS:
            try:
                set_size(terminal.master, cols, rows)
            except OSError as error:
                raise Failure.io(error, "ioctl", "pty") from None
        return {}
    finally:
        terminal.release()


def foreground(terminals: Terminals, json: Any) -> dict[str, Any]:
    """The process group in the terminal's foreground when it is not the program's own: a job a shell started, such as
    an editor. ``{"foreground": None}`` while the program itself is in front (a shell at its prompt)."""
    terminal = find(terminals, json)
    try:
        if system.WINDOWS:
            return {"foreground": None}
        try:
            group = os.tcgetpgrp(terminal.master)
        except OSError as error:
            raise Failure.io(error, "tcgetpgrp", "pty") from None
        if group <= 0 or group == terminal.pid:
            return {"foreground": None}
        return {"foreground": {"pid": group, "name": process_name(group)}}
    finally:
        terminal.release()


def spawn_error(error: BaseException) -> Failure:
    """``spawn_error`` with the text Rust's ``io::Error`` displays: the system's description and ``(os error N)``."""
    if isinstance(error, OSError) and error.errno:
        return Failure("spawn_error", f"{os.strerror(error.errno)} (os error {error.errno})")
    return Failure("spawn_error", str(error))


def run(
    id: int, request: PtyRequest, output: Output, control: Control, groups: Groups, terminals: Terminals
) -> dict[str, Any]:
    """Run the program in a new terminal, sending a ``started`` event and then its output, until it exits."""
    if system.WINDOWS:
        raise Failure("ENOTSUP", "Terminals are not supported on this system yet")
    sender: queue.SimpleQueue[tuple[Any, ...]] = queue.SimpleQueue()
    control.attach(sender)
    if control.aborted.is_set():
        raise Failure("aborted", "aborted")
    if not os.path.exists(system.os_path(request.cwd)):
        raise Failure("spawn_error", f"Working directory does not exist: {request.cwd}")
    try:
        master, child = spawn(request)
    except (OSError, ValueError) as error:
        raise spawn_error(error) from None
    pid = child.pid
    groups.insert(pid)
    inputs: queue.SimpleQueue[bytes | None] = queue.SimpleQueue()
    terminal = Terminal(inputs, master, pid)
    terminals.insert(id, terminal)
    output.control(Frame(EVENT, id, {"kind": "started", "pid": pid}))

    stop = threading.Event()

    def on_data(data: bytes) -> bool:
        sender.put((DATA, 0, data))
        # Like a full pipe: a program that writes faster than the link waits.
        output.wait_for_room(stop)
        return True

    def read_terminal() -> None:
        read(master, stop, on_data)
        sender.put((EOF, 0))

    reader = threading.Thread(target=read_terminal, name="pi-env-pty-reader", daemon=True)
    reader.start()

    def write_terminal() -> None:
        while True:
            data = inputs.get()
            if data is None or not write_all(master, data, stop):
                return

    writer = threading.Thread(target=write_terminal, name="pi-env-pty-writer", daemon=True)
    writer.start()

    def wait() -> None:
        try:
            sender.put((EXIT, child.wait()))
        except Exception as error:  # noqa: BLE001 - the terminal settles with the failure
            sender.put((WAIT_FAILED, str(error)))

    threading.Thread(target=wait, name="pi-env-pty-waiter", daemon=True).start()

    status: tuple[str, Any] | None = None
    ended = False
    idle_until: float | None = None
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
            if status is not None and ended:
                break
            if idle_until is not None:
                now = time.monotonic()
                if now >= idle_until:
                    # A background job may keep the terminal open after the program exited.
                    break
                wait_for = idle_until - now
            else:
                wait_for = 3600.0
            try:
                message = sender.get(timeout=wait_for)
            except queue.Empty:
                continue
            kind = message[0]
            if kind == DATA:
                output.bulk(Frame(EVENT, id, {"kind": "output"}, message[2]), None)
                if status is not None:
                    idle_until = time.monotonic() + EXIT_GRACE
            elif kind == EOF:
                ended = True
            elif kind == EXIT:
                status = ("ok", message[1])
                idle_until = time.monotonic() + EXIT_GRACE
            elif kind == WAIT_FAILED:
                status = ("error", message[1])
                idle_until = time.monotonic()
            elif kind == CANCEL:
                aborted = True
                kill()
            elif kind == KILL:
                kill()
    finally:
        stop.set()
        terminal.ended = True
        # Ending the input queue ends the writer thread.
        inputs.put(None)
        terminals.remove(id)
        groups.remove(pid)
        reader.join()
        writer.join()
        # The descriptor closes once a resize or foreground query underway lets the terminal go.
        terminal.release()
    if aborted:
        raise Failure("aborted", "aborted")
    if status is None:
        raise Failure("spawn_error", "process did not report its exit")
    if status[0] == "error":
        raise Failure("spawn_error", status[1])
    return {"exitCode": system.exit_code(status[1])}


# --- Unix ------------------------------------------------------------------------------------------------------------


def set_size(master: int, cols: int, rows: int) -> None:
    import fcntl
    import struct
    import termios

    # TIOCSWINSZ reads one winsize: rows, columns, and two unused pixel sizes.
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def process_name(pid: int) -> str | None:
    """A process's name, if it still runs and the system tells."""
    if sys.platform == "darwin":
        try:
            import ctypes
            import ctypes.util

            path = ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib"
            library = ctypes.CDLL(path)
            buffer = ctypes.create_string_buffer(256)
            length = library.proc_name(pid, buffer, len(buffer))
        except (ImportError, OSError, AttributeError):
            # A Python built without ``ctypes`` cannot ask; the name is unknown.
            return None
        return buffer.raw[:length].decode("utf-8", "replace") if length > 0 else None
    if sys.platform.startswith("linux") or hasattr(sys, "getandroidapilevel"):
        try:
            with open(f"/proc/{pid}/comm", "rb") as file:
                name = file.read().decode("utf-8", "replace").rstrip()
        except OSError:
            return None
        return name or None
    return None


def command(request: PtyRequest) -> list[str]:
    """The program to run and its ``argv``: ``argv``, or the user's shell as a login shell (``-`` before its name, as
    ``login`` does)."""
    if request.argv is not None:
        return [request.argv[0], *request.argv]
    shell = request.env.get("SHELL")
    if not isinstance(shell, str):
        shell = os.environ.get("SHELL")
    if not shell or not system.path_exists(shell):
        shell = "/bin/sh"
    name = shell.rsplit("/", 1)[-1]
    return [shell, f"-{name}"]


def environment(request: PtyRequest) -> dict[bytes, bytes]:
    """The program's environment, as Rust's ``Command`` builds it: the daemon's own (unless ``inheritEnv`` is false),
    then the request's string values. Unchanged, it keeps the daemon's order; otherwise it is ordered by name, as
    Rust's ``BTreeMap`` of the variables is."""
    child_env: dict[bytes, bytes] = dict(os.environb) if request.inherit_env else {}
    changed = not request.inherit_env
    for key, value in request.env.items():
        if isinstance(value, str):
            child_env[key.encode("utf-8")] = value.encode("utf-8")
            changed = True
    return dict(sorted(child_env.items())) if changed else child_env


def spawn(request: PtyRequest) -> tuple[int, Any]:
    """Open a pseudo-terminal of the request's size and start the program in it, through the launcher. Like Rust's
    ``Command::spawn``, it fails if the program cannot be started (it does not exist, or may not be run): the launcher
    reports the error over the status pipe before anything runs."""
    import fcntl
    import subprocess

    program = command(request)
    child_env = environment(request)
    # Rust refuses these before it forks.
    if any("\0" in argument for argument in program) or any(
        b"\0" in key or b"\0" in value for key, value in child_env.items()
    ):
        raise ValueError("nul byte found in provided data")
    master, slave = os.openpty()
    status_read, status_write = os.pipe()
    env_read, env_write = os.pipe()
    try:
        os.set_inheritable(master, False)
        set_size(master, request.cols, request.rows)
        args = [sys.executable, "-I", "-c", LAUNCHER, str(status_write), str(env_read), *program]
        child = subprocess.Popen(
            [system.os_path(argument) for argument in args],
            cwd=system.os_path(request.cwd),
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
            pass_fds=(status_write, env_read),
            restore_signals=True,
        )
        os.close(status_write)
        status_write = -1
        os.close(env_read)
        env_read = -1
        try:
            _write_all_blocking(env_write, b"".join(key + b"=" + value + b"\0" for key, value in child_env.items()))
        except BrokenPipeError:
            pass
        os.close(env_write)
        env_write = -1
        report = _read_to_end(status_read)
        if report:
            child.wait()
            code = int.from_bytes(report[:4], "big")
            raise OSError(code, os.strerror(code))
    except BaseException:
        os.close(master)
        os.close(slave)
        for fd in (status_write, env_read, env_write):
            if fd >= 0:
                os.close(fd)
        os.close(status_read)
        raise
    os.close(status_read)
    # Only the program holds the terminal now, so reads end once it and its descendants close it.
    os.close(slave)
    # Reads and writes poll instead of blocking.
    flags = fcntl.fcntl(master, fcntl.F_GETFL)
    fcntl.fcntl(master, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    return master, child


def _write_all_blocking(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while len(view) > 0:
        view = view[os.write(fd, view) :]


def _read_to_end(fd: int) -> bytes:
    chunks = []
    while True:
        chunk = os.read(fd, 64)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _poll(fd: int, writable: bool) -> bool:
    """Whether ``fd`` is ready within 100 ms. MISAKA fork: ``select`` where Python has no ``poll``; it raises
    ``ValueError`` for a descriptor of ``FD_SETSIZE`` or more, which ends the terminal's reads or writes as a ``poll``
    failure does upstream."""
    import select

    try:
        if hasattr(select, "poll"):
            poller = select.poll()
            poller.register(fd, select.POLLOUT if writable else select.POLLIN)
            return len(poller.poll(100)) > 0
        if writable:
            return len(select.select([], [fd], [], 0.1)[1]) > 0
        return len(select.select([fd], [], [], 0.1)[0]) > 0
    except InterruptedError:
        return False


def read(master: int, stop: threading.Event, on_data: Any) -> None:
    """Read until the terminal closes (Linux reports ``EIO`` once no program holds it), ``on_data`` returns false, or
    ``stop`` is set."""
    while not stop.is_set():
        try:
            if not _poll(master, False):
                continue
        except (OSError, ValueError):
            return
        try:
            data = os.read(master, READ_CHUNK)
        except (BlockingIOError, InterruptedError):
            continue
        except OSError:
            return
        if not data:
            return
        if not on_data(data):
            return


def write_all(master: int, data: bytes, stop: threading.Event) -> bool:
    """Write all of ``data``, waiting while the program does not read its input; false once the terminal is gone."""
    view = memoryview(data)
    while len(view) > 0:
        if stop.is_set():
            return False
        try:
            written = os.write(master, view)
        except BlockingIOError:
            try:
                _poll(master, True)
            except (OSError, ValueError):
                return False
            continue
        except InterruptedError:
            continue
        except OSError:
            return False
        if written == 0:
            return False
        view = view[written:]
    return True


__all__ = [
    "LAUNCHER",
    "MAX_CELLS",
    "PtyRequest",
    "Terminal",
    "Terminals",
    "foreground",
    "resize",
    "run",
    "write",
]

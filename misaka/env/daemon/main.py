"""pi-env: a small daemon that gives a Pi Durable host an execution environment on this machine over stdin/stdout
(docs/protocol.md). Started as ``pi-env serve --token <hex>``, usually through ``ssh``.

Ported from pi's ``packages/env/daemon/src/main.rs`` (v1.3.0)."""

from __future__ import annotations

import collections
import io
import os
import platform
import queue
import signal
import struct
import sys
import threading
import time
from collections.abc import Callable
from typing import Any, BinaryIO

from . import exec, fs, pty, scan, system, watch

# MISAKA fork: Rust's `list` module, imported under another name so that it does not shadow Python's `list`.
from . import list as listing
from .errors import Failure
from .exec import Control
from .frame import (
    CANCEL,
    ERROR,
    MAX_PAYLOAD,
    PING,
    REQUEST,
    RESULT,
    Frame,
    as_bool,
    as_str,
    as_u64,
    get,
    read_frame,
)
from .output import Output

PROTOCOL = 1
# Operations added after protocol 1's first release; clients check `hello.features` before using them.
FEATURES = ["listFiles"]
# MISAKA fork: pi's build.rs bakes in the version of the npm package the daemon ships with; this is the pi-env release
# the port follows.
VERSION = "1.3.0"
PING_INTERVAL = 5.0
SILENCE_LIMIT_MS = 30_000
SCAN_CHUNK = 64 * 1024
# Threads for file operations; commands run on their own threads.
WORKERS = 16
# Open handles per connection, beyond which opening fails with `EMFILE`.
MAX_HANDLES = 4096
# Replies with larger payloads queue behind command output instead of going first.
CONTROL_PAYLOAD = 64 * 1024

Job = Callable[[], None]


class Serial:
    def __init__(self) -> None:
        self.jobs: collections.deque[Job] = collections.deque()
        self.running = False


class FileHandle:
    def __init__(self, file: system.File, path: str) -> None:
        self.file = file
        self.path = path
        # A failed chunk write poisons a write handle, so later chunks cannot leave a gap.
        self.failed = threading.Event()
        # Chunk writes run one at a time in arrival order, though the client sends several at once.
        self.serial_lock = threading.Lock()
        self.serial = Serial()


class DirHandle:
    def __init__(self, entries: Any, path: str) -> None:
        self.entries = entries
        self.path = path
        self.lock = threading.Lock()


def now_ms() -> int:
    return int(time.time() * 1000)


def field(json: Any, key: str) -> str:
    value = as_str(get(json, key))
    if value is None:
        raise Failure("EINVAL", f"missing field {key}")
    return value


def number(json: Any, key: str) -> int:
    value = as_u64(get(json, key))
    if value is None:
        raise Failure("EINVAL", f"missing or invalid field {key}")
    return value


def flag(json: Any, key: str) -> bool:
    return as_bool(get(json, key)) or False


def _os_name() -> str:
    """Rust's ``std::env::consts::OS``."""
    if hasattr(sys, "getandroidapilevel"):
        return "android"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    for name in ("freebsd", "openbsd", "netbsd", "dragonfly"):
        if sys.platform.startswith(name):
            return name
    return sys.platform


def _arch_name() -> str:
    """Rust's ``std::env::consts::ARCH`` for this interpreter."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64", "x64"):
        return "x86_64"
    if machine in ("aarch64", "arm64", "armv8l"):
        return "aarch64"
    if machine in ("i386", "i486", "i586", "i686", "x86"):
        return "x86"
    if machine.startswith("arm"):
        return "arm"
    return machine


class Server:
    def __init__(self, output: Output, tmpdir: str) -> None:
        self.output = output
        self.tmpdir = tmpdir
        self.handles_lock = threading.Lock()
        self.handles: dict[int, FileHandle | DirHandle] = {}
        self.next_handle = 1
        self.controls_lock = threading.Lock()
        self.controls: dict[int, Control] = {}
        self.groups = exec.Groups()
        self.terminals = pty.Terminals()

    def file(self, json: Any) -> FileHandle:
        key = number(json, "handle")
        with self.handles_lock:
            handle = self.handles.get(key)
        if isinstance(handle, FileHandle):
            return handle
        if isinstance(handle, DirHandle):
            raise Failure("EBADF", "not a file handle")
        raise Failure("EBADF", "unknown handle")

    def insert(self, handle: FileHandle | DirHandle) -> int:
        with self.handles_lock:
            if len(self.handles) >= MAX_HANDLES:
                raise Failure("EMFILE", "EMFILE: too many open files")
            key = self.next_handle
            self.next_handle += 1
            self.handles[key] = handle
            return key

    def insert_file(self, file: system.File, path: str) -> int:
        return self.insert(FileHandle(file, path))

    def dispatch(self, id: int, json: Any, payload: bytes, control: Control) -> tuple[dict[str, Any], bytes]:
        if json is None:
            raise Failure("EINVAL", "invalid JSON")
        op = as_str(get(json, "op")) or ""
        if op == "hello":
            return {
                "protocol": PROTOCOL,
                "version": VERSION,
                "os": _os_name(),
                "arch": _arch_name(),
                "home": system.home(),
                "separator": system.PATH_SEPARATOR,
                "tmpdir": self.tmpdir,
                "cwd": system.cwd(),
                "driveCwds": system.drive_cwds(),
                "pid": os.getpid(),
                "features": FEATURES,
            }, b""
        if op == "lstat":
            return fs.lstat(field(json, "path")), b""
        if op == "realpath":
            return fs.realpath(field(json, "path")), b""
        if op == "write":
            path = field(json, "path")
            parents = as_bool(get(json, "parents"))
            file = fs.write(path, flag(json, "append"), True if parents is None else parents, payload)
            if not flag(json, "keep"):
                file.close()
                return {}, b""
            return {"handle": self.insert_file(file, path)}, b""
        if op == "writeChunk":
            handle = self.file(json)
            if handle.failed.is_set():
                raise Failure("EBADF", "an earlier write failed")
            try:
                handle.file.write_all(payload)
            except OSError as error:
                handle.failed.set()
                raise Failure.io(error, "write", handle.path) from None
            return {}, b""
        if op == "truncate":
            return fs.truncate(field(json, "path"), number(json, "size")), b""
        if op == "fsync":
            return fs.fsync(field(json, "path")), b""
        if op == "rename":
            return fs.rename(field(json, "path"), field(json, "to")), b""
        if op == "mkdir":
            return fs.mkdir(field(json, "path"), flag(json, "recursive")), b""
        if op == "rm":
            return fs.rm(field(json, "path"), flag(json, "recursive"), flag(json, "force")), b""
        if op == "mkdtemp":
            return fs.mkdtemp(field(json, "path")), b""
        if op == "open":
            path = field(json, "path")
            if as_str(get(json, "mode")) == "read":
                # Node's `open(path, "r")`: any kind of file, blocking; reads report what the file is.
                try:
                    file = system.open(path, system.READ)
                except (OSError, ValueError) as error:
                    raise Failure.io(error, "open", path) from None
                try:
                    result: dict[str, Any] = {"stat": fs.info(path, file.metadata())}
                except OSError as error:
                    result = {"statError": Failure.io(error, "fstat", path).to_json()}
                result["handle"] = self.insert_file(file, path)
                return result, b""
            file, info = fs.open_reader(path, flag(json, "noFollow"))
            return {"handle": self.insert_file(file, path), "info": info}, b""
        if op == "pread":
            handle = self.file(json)
            length = min(number(json, "length"), MAX_PAYLOAD)
            offset = as_u64(get(json, "offset"))
            if offset is not None:
                data = fs.pread(handle.file, handle.path, offset, length)
            else:
                data = fs.read(handle.file, handle.path, length)
            return {}, data
        if op == "fstat":
            handle = self.file(json)
            try:
                metadata = handle.file.metadata()
            except OSError as error:
                raise Failure.io(error, "fstat", handle.path) from None
            return fs.info(handle.path, metadata), b""
        if op == "scanLines":
            handle = self.file(json)
            return scan_lines(handle.file, handle.path, json, control), b""
        if op == "opendir":
            path = field(json, "path")
            try:
                entries = system.read_dir(path)
            except (OSError, ValueError) as error:
                raise Failure.io(error, "opendir", path) from None
            return {"handle": self.insert(DirHandle(entries, path))}, b""
        if op == "readdir":
            return self.read_dir(json), b""
        if op == "listFiles":
            return listing.run(json, control.aborted.is_set)
        if op == "close":
            key = number(json, "handle")
            with self.handles_lock:
                removed = self.handles.pop(key, None)
            if isinstance(removed, DirHandle):
                # A listing in progress keeps the directory open until it finishes, as Rust's `Arc` does.
                with removed.lock:
                    removed.entries.close()
            return {}, b""
        if op == "exec":
            return exec.run(id, exec.ExecRequest.from_json(json), self.tmpdir, self.output, control, self.groups), b""
        if op == "watch":
            return watch.run(id, json, self.output, control), b""
        if op == "pty":
            return pty.run(id, pty.PtyRequest.from_json(json), self.output, control, self.groups, self.terminals), b""
        raise Failure("EINVAL", f"unknown operation {op}")

    def read_dir(self, json: Any) -> dict[str, Any]:
        maximum = max(number(json, "max"), 1)
        # Lock only this directory, so other requests are not held up by a slow listing.
        key = number(json, "handle")
        with self.handles_lock:
            handle = self.handles.get(key)
        if not isinstance(handle, DirHandle):
            raise Failure("EBADF", "not a directory handle")
        with handle.lock:
            entries: list[dict[str, Any]] = []
            done = False
            while len(entries) < maximum:
                try:
                    entry = next(handle.entries)
                except StopIteration:
                    done = True
                    break
                except OSError as error:
                    raise Failure.io(error, "readdir", handle.path) from None
                entries.append(fs.dir_entry(handle.path, entry.name))
        return {"entries": entries, "done": done}


def scan_lines(file: system.File, path: str, json: Any, control: Control) -> dict[str, Any]:
    start_line = number(json, "startLine")
    end_line = as_u64(get(json, "endLine"))
    if end_line is not None and end_line <= start_line:
        raise Failure("EINVAL", "Invalid line range")
    scanner = scan.LineScanner(start_line, end_line)
    position = 0
    while True:
        if control.aborted.is_set():
            raise Failure("aborted", "aborted")
        try:
            data = system.read_at(file, SCAN_CHUNK, position)
        except OSError as error:
            raise Failure.io(error, "read", path) from None
        if not data:
            return scanner.finish()
        scanner.push(data)
        position += len(data)


def kill_all(groups: exec.Groups) -> None:
    for pid in groups.pids():
        exec.kill_group(pid)


class LastSeen:
    def __init__(self) -> None:
        self.value = now_ms()


class Seen(io.RawIOBase):
    """Stdin that records when bytes last arrived, so a large frame arriving slowly counts as a live client."""

    def __init__(self, inner: Any, last_seen: LastSeen) -> None:
        super().__init__()
        self.inner = inner
        self.last_seen = last_seen

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        read = self.inner.readinto(buffer)
        if read:
            self.last_seen.value = now_ms()
        return read or 0


def start_workers() -> queue.SimpleQueue[Job]:
    jobs: queue.SimpleQueue[Job] = queue.SimpleQueue()

    def work() -> None:
        while True:
            jobs.get()()

    for _ in range(WORKERS):
        threading.Thread(target=work, name="pi-env-worker", daemon=True).start()
    return jobs


def respond(server: Server, id: int, request: Frame, control: Control) -> None:
    is_exec = as_str(get(request.json, "op")) in ("exec", "pty")
    try:
        json, payload = server.dispatch(id, request.json, request.payload, control)
        reply = Frame(RESULT, id, json, payload)
    except Failure as failure:
        reply = Frame(ERROR, id, failure.to_json())
    except Exception as error:  # noqa: BLE001 - MISAKA fork: a Rust panic would leave the request unanswered
        reply = Frame(ERROR, id, Failure("unknown", f"pi-env: {error}").to_json())
    with server.controls_lock:
        server.controls.pop(id, None)
    # A command's result follows its output; large payloads do not hold up pings and small replies.
    if is_exec or len(reply.payload) > CONTROL_PAYLOAD:
        server.output.bulk(reply, None)
    else:
        server.output.control(reply)


def run_in_order(jobs: queue.SimpleQueue[Job], handle: FileHandle, job: Job) -> None:
    """Queue ``job`` behind the handle's earlier chunk writes; one pool job drains the queue."""
    with handle.serial_lock:
        handle.serial.jobs.append(job)
        if handle.serial.running:
            return
        handle.serial.running = True

    def drain() -> None:
        while True:
            with handle.serial_lock:
                if not handle.serial.jobs:
                    handle.serial.running = False
                    return
                next_job = handle.serial.jobs.popleft()
            next_job()

    jobs.put(drain)


def serve_frames(server: Server, input: BinaryIO) -> None:
    jobs = start_workers()
    while True:
        request = read_frame(input)
        if request is None:
            return
        op = as_str(get(request.json, "op")) if request.kind == REQUEST else None
        if op in ("ptyWrite", "ptyResize", "ptyForeground"):
            # Answered here, in arrival order: through the worker pool, two writes could swap.
            try:
                if op == "ptyWrite":
                    answer = pty.write(server.terminals, request.json, request.payload)
                elif op == "ptyResize":
                    answer = pty.resize(server.terminals, request.json)
                else:
                    answer = pty.foreground(server.terminals, request.json)
                reply = Frame(RESULT, request.id, answer)
            except Failure as failure:
                reply = Frame(ERROR, request.id, failure.to_json())
            except Exception as error:  # noqa: BLE001 - MISAKA fork: as in `respond`, where Rust cannot fail
                reply = Frame(ERROR, request.id, Failure("unknown", f"pi-env: {error}").to_json())
            server.output.control(reply)
        elif request.kind == REQUEST:
            id = request.id
            # Registered before the request runs, so a cancel right behind it is not lost.
            control = Control()
            with server.controls_lock:
                server.controls[id] = control
            if op in ("exec", "watch", "pty"):
                # Commands and watchers last; they get their own threads (watchers recurse through trees).
                thread = threading.Thread(
                    target=respond, args=(server, id, request, control), name=f"pi-env-{op}", daemon=True
                )
                try:
                    thread.start()
                except RuntimeError as error:
                    with server.controls_lock:
                        server.controls.pop(id, None)
                    failure = Failure("spawn_error", str(error))
                    server.output.bulk(Frame(ERROR, id, failure.to_json()), None)
            else:
                serial = None
                if op == "writeChunk":
                    try:
                        serial = server.file(request.json)
                    except Failure:
                        serial = None

                def job(request: Frame = request, id: int = id, control: Control = control) -> None:
                    respond(server, id, request, control)

                if serial is not None:
                    run_in_order(jobs, serial, job)
                else:
                    jobs.put(job)
        elif request.kind == CANCEL:
            with server.controls_lock:
                found = server.controls.get(request.id)
            if found is not None:
                found.cancel(as_str(get(request.json, "mode")) == "kill")


def serve(token: str) -> None:
    # MISAKA fork: Rust's watcher threads get 16 MiB stacks for their recursion through trees; Python sets one size for
    # every thread it starts afterwards, and a recursion limit to match.
    threading.stack_size(16 * 1024 * 1024)
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 10_000))
    system.prepare_children()
    stdout = sys.stdout.buffer
    stdout.write(f"PI-ENV {token}\n".encode())
    stdout.flush()
    output = Output()
    writer = threading.Thread(target=output.run, args=(stdout,), name="pi-env-writer", daemon=True)
    writer.start()
    server = Server(output, system.tmpdir())
    last_seen = LastSeen()

    def ping() -> None:
        while True:
            time.sleep(PING_INTERVAL)
            output.control(Frame(PING, 0, {}))
            # A client that went silent (a phone that lost its network) leaves nothing running.
            if now_ms() - last_seen.value > SILENCE_LIMIT_MS:
                kill_all(server.groups)
                os._exit(0)

    threading.Thread(target=ping, name="pi-env-ping", daemon=True).start()
    stdin: Any = sys.stdin.buffer
    input = io.BufferedReader(Seen(stdin.raw, last_seen), buffer_size=1 << 20)
    failure: BaseException | None = None
    try:
        serve_frames(server, input)
    except Exception as error:  # noqa: BLE001 - reported once everything is stopped
        failure = error
    # However input ended, the client is gone: stop everything it started.
    kill_all(server.groups)
    output.close()
    writer.join()
    if failure is not None:
        raise failure


def startup_environment() -> dict[bytes, bytes] | None:
    """The environment this process was started with, as the system keeps it: ``/proc/self/environ`` on Linux and
    Android, ``KERN_PROCARGS2`` on macOS; ``None`` where it cannot be read. Read as CPython reads ``environ``: each entry
    up to its first ``=``, the first of two equal names kept."""
    if sys.platform.startswith("linux") or hasattr(sys, "getandroidapilevel"):
        try:
            with open("/proc/self/environ", "rb") as file:
                entries = file.read().split(b"\0")
        except OSError:
            return None
    elif sys.platform == "darwin":
        try:
            import ctypes

            libc = ctypes.CDLL(None, use_errno=True)
            # CTL_KERN, KERN_PROCARGS2: argc, the executable's path, NUL padding, the arguments, the environment, an
            # empty string, then the system's own strings.
            mib = (ctypes.c_int * 3)(1, 49, os.getpid())
            length = ctypes.c_size_t(0)
            if libc.sysctl(mib, 3, None, ctypes.byref(length), None, 0) != 0:
                return None
            buffer = ctypes.create_string_buffer(length.value)
            if libc.sysctl(mib, 3, buffer, ctypes.byref(length), None, 0) != 0:
                return None
            raw = buffer.raw[: length.value]
            argc = struct.unpack_from("i", raw)[0]
        except (ImportError, OSError, AttributeError, ValueError, struct.error):
            return None
        rest = raw[4:]
        parts = rest[rest.find(b"\0") :].lstrip(b"\0").split(b"\0")[argc:]
        end = parts.index(b"") if b"" in parts else len(parts)
        entries = parts[:end]
    else:
        return None
    environment: dict[bytes, bytes] = {}
    for entry in entries:
        key, equals, value = entry.partition(b"=")
        if equals:
            environment.setdefault(key, value)
    return environment


def restore_environment() -> None:
    """MISAKA fork: give the daemon the environment it was started with, which Rust's ``std::env`` reads and every
    command inherits. Python's startup adds to its own: PEP 538 locale coercion sets ``LC_CTYPE=C.UTF-8`` when no
    locale is set (a daemon started over ``ssh`` without ``LANG``), and on macOS ``__CF_USER_TEXT_ENCODING`` appears.
    Without this, every ``exec`` and ``pty`` program would inherit them, which upstream's never do."""
    original = startup_environment()
    if original is None:
        return
    for key in [key for key in os.environb if key not in original]:
        try:
            del os.environb[key]
        except (KeyError, ValueError, OSError):
            pass
    for key, value in original.items():
        if os.environb.get(key) != value:
            try:
                os.environb[key] = value
            except (ValueError, OSError):
                pass


def _usage() -> None:
    sys.stderr.write("usage: pi-env serve --token <hex>\n")
    sys.stderr.flush()
    os._exit(2)


def main(argv: list[str] | None = None) -> None:
    args = sys.argv if argv is None else argv
    # As a Rust program, the daemon ends on SIGINT instead of raising KeyboardInterrupt.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    command = args[1] if len(args) > 1 else None
    if command == "serve":
        token = None
        if "--token" in args:
            index = args.index("--token")
            token = args[index + 1] if index + 1 < len(args) else None
        if token is None:
            _usage()
            return
        restore_environment()
        try:
            serve(token)
        except Exception as error:  # noqa: BLE001 - the daemon's exit status reports it
            sys.stderr.write(f"pi-env: {error}\n")
            sys.stderr.flush()
            os._exit(1)
        # Request threads may still be blocked (a FIFO open); nothing is left to answer them.
        os._exit(0)
    if command == "--version":
        print(f"pi-env {VERSION}")
        return
    _usage()

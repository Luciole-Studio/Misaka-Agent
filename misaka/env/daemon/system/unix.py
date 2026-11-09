"""Linux, macOS and Android."""

from __future__ import annotations

import errno
import os
import select
import signal
import stat
import subprocess
import sys
import threading
from collections.abc import Callable
from typing import Any

from ..errors import Failure
from . import APPEND, READ, READ_WRITE, WRITE, File, ShellConfig, path_exists

_ERROR_NAMES: dict[int, str] = {}
for _name in (
    "EPERM",
    "ENOENT",
    "EIO",
    "EBADF",
    "EAGAIN",
    "ENOMEM",
    "EACCES",
    "EBUSY",
    "EEXIST",
    "EXDEV",
    "ENOTDIR",
    "EISDIR",
    "EINVAL",
    "ENFILE",
    "EMFILE",
    "ETXTBSY",
    "EFBIG",
    "ENOSPC",
    "EROFS",
    "EMLINK",
    "ENAMETOOLONG",
    "ENOTEMPTY",
    "ELOOP",
    "ENOSYS",
    "ENOTSUP",
):
    if hasattr(errno, _name):
        _ERROR_NAMES.setdefault(getattr(errno, _name), _name)


def os_error_name(error: BaseException) -> str:
    """libuv's name for an OS error."""
    number = error.errno if isinstance(error, OSError) else None
    if number is None:
        return "UNKNOWN"
    return _ERROR_NAMES.get(number, "UNKNOWN")


PATH_SEPARATOR = "/"


def os_path(path: str) -> str | bytes:
    """A path as the system takes it: UTF-8 bytes."""
    return path.encode("utf-8")


def to_string(value: bytes | str) -> str:
    """What the system returned, decoded as UTF-8 with replacement characters (``to_string_lossy``)."""
    if isinstance(value, str):
        value = os.fsencode(value)
    return value.decode("utf-8", "replace")


def read_at(file: File, length: int, offset: int) -> bytes:
    return os.pread(file.fd, length, offset)


def read(file: File, length: int) -> bytes:
    return os.read(file.fd, length)


def write_all(file: File, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(file.fd, view)
        view = view[written:]


def set_len(file: File, size: int) -> None:
    os.ftruncate(file.fd, size)


def sync_all(file: File) -> None:
    os.fsync(file.fd)


def mtime(metadata: os.stat_result) -> tuple[int, int]:
    """Modification time as seconds and nanoseconds since the epoch."""
    seconds, nanoseconds = divmod(metadata.st_mtime_ns, 1_000_000_000)
    return seconds, nanoseconds


def identity(metadata: os.stat_result) -> tuple[int, int]:
    """Device and inode, which identify a file across renames."""
    return metadata.st_dev, metadata.st_ino


def file_type(metadata: os.stat_result) -> str:
    """``file``, ``directory``, ``symlink`` or ``other``, as Rust's ``FileType`` tells them apart."""
    mode = metadata.st_mode
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


def _variable(key: str) -> str | None:
    """``std::env::var``: ``None`` when unset or not UTF-8."""
    value = os.environb.get(key.encode())
    if value is None:
        return None
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return None


def tmpdir() -> str:
    """Node's ``os.tmpdir()``; Termux's Node falls back to ``$PREFIX/tmp``."""
    path = None
    for key in ("TMPDIR", "TMP", "TEMP"):
        value = _variable(key)
        if value:
            path = value
            break
    if path is None:
        if hasattr(sys, "getandroidapilevel"):
            path = f"{_variable('PREFIX') or '/data/data/com.termux/files/usr'}/tmp"
        else:
            path = "/tmp"
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path


def home() -> str:
    return _variable("HOME") or ""


_TEMPLATE_CHARACTERS = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


def mkdtemp(prefix: str) -> str:
    """``mkdtemp(3)`` of ``prefix``, as libuv calls it: six random characters from ``[A-Za-z0-9]``, mode 0700, tried again
    while the name exists."""
    base = prefix.encode("utf-8")
    for _ in range(62**3):
        suffix = bytes(_TEMPLATE_CHARACTERS[byte % 62] for byte in os.urandom(6))
        path = base + suffix
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            continue
        return to_string(path)
    raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), prefix)


def open_reader(path: str, no_follow: bool) -> File:
    """Open a regular file for positional reads, without blocking on FIFOs and, with ``no_follow``, without following a
    final symbolic link."""
    flags = os.O_RDONLY | os.O_NONBLOCK
    if no_follow:
        flags |= os.O_NOFOLLOW
    try:
        return File(os.open(os_path(path), flags))
    except (OSError, ValueError) as error:
        if no_follow and isinstance(error, OSError) and error.errno in (errno.ELOOP, errno.EMLINK):
            raise Failure("SYMLINK", "Refusing to follow a symbolic link").path(path) from None
        raise Failure.io(error, "open", path) from None


def open(path: str, mode: str) -> File:
    """Open a file in one of Node's modes."""
    if mode == READ:
        flags = os.O_RDONLY
    elif mode == WRITE:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    elif mode == APPEND:
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    elif mode == READ_WRITE:
        flags = os.O_RDWR
    else:
        raise ValueError(f"unknown open mode {mode}")
    return File(os.open(os_path(path), flags, 0o666))


_libc: Any = None


def _c_library() -> Any:
    global _libc
    if _libc is None:
        import ctypes

        library = ctypes.CDLL(None, use_errno=True)
        library.realpath.restype = ctypes.c_void_p
        library.realpath.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
        library.free.argtypes = [ctypes.c_void_p]
        _libc = library
    return _libc


def realpath(path: str) -> str:
    """``realpath(3)``, as Rust's ``fs::canonicalize`` calls it."""
    try:
        import ctypes

        library = _c_library()
    except (ImportError, OSError, AttributeError):
        # A Python without ctypes: resolve, then fail as the system would for a path that does not exist.
        resolved = os.path.realpath(os_path(path))
        os.stat(resolved)
        return to_string(resolved)
    pointer = library.realpath(os_path(path), None)
    if not pointer:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), path)
    try:
        return to_string(ctypes.string_at(pointer))
    finally:
        library.free(pointer)


def rename(source: str, destination: str) -> None:
    os.rename(os_path(source), os_path(destination))


def remove_file(path: str) -> None:
    os.unlink(os_path(path))


def read_pipe(pipe: Any, stop: threading.Event, on_data: Callable[[bytes], bool]) -> None:
    """Read ``pipe`` until it ends, ``on_data`` returns false, or ``stop`` is set; the pipe closes when this returns."""
    try:
        fd = pipe.fileno()
        os.set_blocking(fd, False)
        poller = select.poll()
        poller.register(fd, select.POLLIN)
        while not stop.is_set():
            try:
                ready = poller.poll(100)
            except OSError:
                return
            if not ready:
                continue
            try:
                data = os.read(fd, 64 * 1024)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                return
            if not data:
                return
            if not on_data(data):
                return
    finally:
        pipe.close()


def interrupt_reader(reader: threading.Thread) -> None:
    """Readers poll ``stop`` every 100 ms."""


def resolve_program(program: str, cwd: str, env: dict[str, Any]) -> str:
    """The program an argv command runs; it is searched on ``PATH`` like ``execvp``, as libuv does."""
    return program


def which_bash() -> str | None:
    try:
        output = subprocess.run(
            ["which", "bash"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return None
    if output.returncode != 0:
        return None
    lines = to_string(output.stdout).strip().split("\n")
    first = lines[0].rstrip("\r") if lines else ""
    if not first:
        return None
    return first if path_exists(first) else None


def shell_config(shell_path: str | None) -> ShellConfig:
    """Node's shell resolution: a configured shell must exist; otherwise ``/bin/bash``, ``which bash``, then ``sh``."""
    if shell_path is not None:
        if path_exists(shell_path):
            return ShellConfig(shell_path, ["-c"], False)
        raise Failure("shell_unavailable", f"Custom shell path not found: {shell_path}")
    if path_exists("/bin/bash"):
        return ShellConfig("/bin/bash", ["-c"], False)
    return ShellConfig(which_bash() or "sh", ["-c"], False)


def prepare_children() -> None:
    """MISAKA fork: libuv gives each child default signal handling and an empty signal mask between ``fork`` and
    ``exec``. Python runs no code there safely in a process with threads, so the daemon sets both up for itself before
    it starts any: an ignored signal stays ignored across ``exec``, a handled one does not, and the mask is inherited.
    SIGPIPE and SIGXFSZ stay ignored in the daemon, and ``subprocess`` restores them for each child."""
    signal.pthread_sigmask(signal.SIG_SETMASK, [])
    keep = {signal.SIGKILL, signal.SIGSTOP, signal.SIGPIPE, getattr(signal, "SIGXFSZ", signal.SIGPIPE)}  # windows-footgun: ok - POSIX-only module
    for number in range(1, 32):
        if number in keep:
            continue
        try:
            if signal.getsignal(number) == signal.SIG_IGN:
                signal.signal(number, signal.SIG_DFL)
        except (OSError, ValueError, RuntimeError):
            pass


def configure(program: str, args: list[str], env: dict[str, Any], inherit_env: bool) -> dict[str, Any]:
    """Arguments, environment and process setup as libuv applies them: the ``subprocess.Popen`` arguments. A new
    session, so a kill reaches every descendant."""
    child_env: dict[bytes, bytes] = dict(os.environb) if inherit_env else {}
    for key, value in env.items():
        if isinstance(value, str):
            child_env[key.encode("utf-8")] = value.encode("utf-8")
    return {
        "args": [os_path(program), *(os_path(argument) for argument in args)],
        "env": child_env,
        "start_new_session": True,
        "restore_signals": True,
    }


def after_spawn(child: subprocess.Popen[bytes]) -> None:
    pass


def kill_tree(pid: int) -> None:
    """Kill the command's process group, or the process if the group is gone."""
    try:
        os.killpg(pid, signal.SIGKILL)  # windows-footgun: ok - POSIX-only module
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)  # windows-footgun: ok - POSIX-only module
        except OSError:
            pass


def exit_code(returncode: int) -> int:
    """A process killed by a signal has no exit code; report 128 + the signal, as the shell does."""
    return returncode if returncode >= 0 else 128 - returncode


def raw_name(name: bytes | str) -> list[int] | None:
    """The bytes of a file name, which libuv sorts directory listings by."""
    return list(name if isinstance(name, bytes) else os.fsencode(name))


def drive_cwds() -> dict[str, str]:
    """Windows only: the per-drive working directories of the ``=C:`` variables."""
    return {}


def read_dir(path: str) -> Any:
    return os.scandir(os_path(path))


def path_identity(path: str, metadata: os.stat_result, follow: bool) -> tuple[int, int]:
    """Device and inode of ``path``; the metadata already has them."""
    return identity(metadata)

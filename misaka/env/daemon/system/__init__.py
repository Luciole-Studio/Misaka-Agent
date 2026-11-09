"""What differs between operating systems: error names, positional reads, temporary directories, opening readers, and
starting and killing commands. Each implementation follows what Node (libuv) does on that system.

MISAKA fork: Rust's ``sys`` module, named ``system`` so that it does not shadow Python's ``sys``. Paths are ``str`` here;
on POSIX each implementation passes them to the system as UTF-8 bytes and decodes what the system returns as UTF-8 with
replacement characters, as Rust and Node do, whatever the locale's file system encoding.
"""

from __future__ import annotations

import os
import sys

WINDOWS = sys.platform == "win32"


class ShellConfig:
    """How a string command runs: the shell, its arguments before the command, and whether the command goes to its stdin
    instead of its arguments (legacy WSL ``bash.exe``)."""

    __slots__ = ("args", "command_on_stdin", "program")

    def __init__(self, program: str, args: list[str], command_on_stdin: bool) -> None:
        self.program = program
        self.args = args
        self.command_on_stdin = command_on_stdin


# How Node opens files: `r`, `writeFile` (`w`), `appendFile` (`a`), and `r+` for truncate and fsync. `READ` is Node's
# `open(path, "r")`: any kind of file, blocking reads.
READ = "read"
WRITE = "write"
APPEND = "append"
READ_WRITE = "read_write"


class Synthetic(OSError):
    """An error code libuv reports where the system call itself reported another one, such as ``ENOTDIR`` from a
    recursive ``mkdir`` through a file, or ``EISDIR`` when Windows creates a file where a directory exists."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code

    def __str__(self) -> str:
        return self.code


def synthetic(code: str) -> OSError:
    return Synthetic(code)


class File:
    """An open file descriptor, closed when the last reference to it goes, as Rust's ``File`` closes on drop: a request
    still reading a handle keeps it open after ``close`` removed it."""

    def __init__(self, fd: int) -> None:
        self.fd = fd

    def close(self) -> None:
        fd, self.fd = self.fd, -1
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass

    def __del__(self) -> None:
        self.close()

    def metadata(self) -> os.stat_result:
        return os.fstat(self.fd)

    def read(self, length: int) -> bytes:
        """One read at the current position."""
        return read(self, length)

    def write_all(self, data: bytes) -> None:
        write_all(self, data)

    def set_len(self, size: int) -> None:
        set_len(self, size)

    def sync_all(self) -> None:
        sync_all(self)


# Defined before the platform module is imported, which uses them; the names they call resolve when they run.


def error_name(error: BaseException) -> str:
    """The libuv name of an error: a synthetic code, or the system's code translated per platform."""
    if isinstance(error, Synthetic):
        return error.code
    return os_error_name(error)


def path_exists(path: str) -> bool:
    """Whether a path exists, following symbolic links, like Node's ``access(path, F_OK)``."""
    try:
        os.stat(os_path(path))
    except (OSError, ValueError):
        return False
    return True


def random_bytes() -> bytes:
    """Sixteen random bytes for temporary names (not for secrets)."""
    return os.urandom(16)


def cwd() -> str:
    """The daemon's working directory, the ``process.cwd()`` Node's ``path.resolve`` falls back to."""
    try:
        return to_string(os.getcwdb() if not WINDOWS else os.getcwd())
    except OSError:
        return ""


if WINDOWS:
    from .windows import (
        PATH_SEPARATOR,
        after_spawn,
        configure,
        drive_cwds,
        exit_code,
        file_type,
        home,
        identity,
        interrupt_reader,
        kill_tree,
        mkdtemp,
        mtime,
        open,
        open_reader,
        os_error_name,
        os_path,
        path_identity,
        prepare_children,
        raw_name,
        read,
        read_at,
        read_dir,
        read_pipe,
        realpath,
        remove_file,
        rename,
        resolve_program,
        set_len,
        shell_config,
        sync_all,
        tmpdir,
        to_string,
        write_all,
    )
else:
    from .unix import (
        PATH_SEPARATOR,
        after_spawn,
        configure,
        drive_cwds,
        exit_code,
        file_type,
        home,
        identity,
        interrupt_reader,
        kill_tree,
        mkdtemp,
        mtime,
        open,
        open_reader,
        os_error_name,
        os_path,
        path_identity,
        prepare_children,
        raw_name,
        read,
        read_at,
        read_dir,
        read_pipe,
        realpath,
        remove_file,
        rename,
        resolve_program,
        set_len,
        shell_config,
        sync_all,
        tmpdir,
        to_string,
        write_all,
    )


__all__ = [
    "APPEND",
    "PATH_SEPARATOR",
    "READ",
    "READ_WRITE",
    "WINDOWS",
    "WRITE",
    "File",
    "ShellConfig",
    "Synthetic",
    "after_spawn",
    "configure",
    "cwd",
    "drive_cwds",
    "error_name",
    "exit_code",
    "file_type",
    "home",
    "identity",
    "interrupt_reader",
    "kill_tree",
    "mkdtemp",
    "mtime",
    "open",
    "open_reader",
    "os_error_name",
    "os_path",
    "path_exists",
    "path_identity",
    "prepare_children",
    "random_bytes",
    "raw_name",
    "read",
    "read_at",
    "read_dir",
    "read_pipe",
    "realpath",
    "remove_file",
    "rename",
    "resolve_program",
    "set_len",
    "shell_config",
    "sync_all",
    "synthetic",
    "tmpdir",
    "to_string",
    "write_all",
]

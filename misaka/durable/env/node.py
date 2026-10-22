"""The local execution environment: file system and shell on this machine.

Translated from pi's ``packages/durable/src/env/node.ts``. File operations are blocking calls run in a thread
(``asyncio.to_thread``); the shell runs through ``misaka.utils.child_process`` (Node's ``spawn`` over asyncio). An
operation returns a ``Result`` instead of raising: ``{"ok": True, "value": ...}`` or ``{"ok": False, "error": ...}``.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import math
import os
import re
import shutil
import stat as statModule
import subprocess
import sys
import tempfile
import threading
import uuid
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import unquote, urlparse

from misaka.chord.context import Context
from misaka.utils import node_path
from misaka.utils.child_process import ChildProcess, spawn_child_process
from misaka.utils.js import (
    DOMException,
    formatNumber,
    jsIsInteger,
    jsIsSafeInteger,
    pathResolve,
    toWellFormed,
)
from misaka.utils.node_errors import uvMessage, withSyscall
from misaka.utils.shell import killProcessTree as killShellProcessTree

from . import (
    ExecutionError,
    FileError,
    Result,
    err,
    ok,
    toError,
)
from .decode import StreamDecoder
from .line_scan import LineScanner
from .node_watch import NodeFileWatcher, NodeWatchOptions

MAX_TIMEOUT_MS = 2_147_483_647
MAX_TIMEOUT_SECONDS = MAX_TIMEOUT_MS / 1000
# MISAKA fork: no `EXIT_STDIO_GRACE_MS`; upstream's `waitForChildProcess` is misaka.utils.child_process, which applies
# the 100 ms post-exit stdio grace itself (`EXIT_STDIO_GRACE_MS`, re-armed while `spillIsDraining`).
SPILL_HIGH_WATER_MARK = 1024 * 1024
# Largest single read of a `BinaryReader`, so a huge `length` allocates only as much as the file yields.
BINARY_READ_CHUNK = 1024 * 1024


def resolveTimeoutMs(timeout: float | None) -> Result:
    if timeout is None:
        return ok(None)
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or not math.isfinite(timeout) or timeout <= 0:
        return err(ExecutionError("timeout", "Invalid timeout: must be a finite number of seconds"))

    timeoutMs = timeout * 1000
    if timeoutMs > MAX_TIMEOUT_MS:
        return err(ExecutionError("timeout", f"Invalid timeout: maximum is {formatSeconds(MAX_TIMEOUT_SECONDS)} seconds"))
    return ok(timeoutMs)


def formatSeconds(value: float) -> str:
    return str(int(value)) if value == int(value) else str(value)


def fileUrlToPath(url: str, windows: bool | None = None) -> str | None:
    """``fileURLToPath(url, { windows })``, or ``None`` where it throws: the string then stays an ordinary path.
    ``windows`` picks the path rules (default: this system's)."""
    if windows is None:
        windows = sys.platform == "win32"
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    path, host = parsed.path, parsed.netloc
    # The URL parser spells a drive letter `C|` as `C:`.
    path = re.sub(r"^/([A-Za-z])\|(?=/|$)", r"/\1:", path)
    # An encoded separator, and a percent escape that is not one, are what Node refuses.
    if re.search(r"%(?![0-9A-Fa-f]{2})", path) or re.search(r"%2f", path, re.IGNORECASE):
        return None
    if windows and re.search(r"%5c", path, re.IGNORECASE):
        return None
    try:
        local = unquote(path, errors="strict")
    except UnicodeDecodeError:
        return None
    if windows:
        if host and host != "localhost":
            return "\\\\" + host + local.replace("/", "\\")
        if not re.match(r"/[A-Za-z]:", local):
            return None
        return local[1:].replace("/", "\\")
    if host and host != "localhost":
        return None
    return local


def resolvePath(cwd: str, path: str) -> str:
    normalized = path
    if normalized == "~":
        normalized = str(Path.home())
    elif normalized.startswith("~/") or (sys.platform == "win32" and normalized.startswith("~\\")):
        normalized = os.path.join(str(Path.home()), normalized[2:])
    elif normalized.startswith("file://"):
        converted = fileUrlToPath(normalized)
        # Keep what cannot be converted as an ordinary path so filesystem methods preserve their non-raising contract.
        if converted is not None:
            normalized = converted
    return pathResolve(normalized) if os.path.isabs(normalized) else pathResolve(os.path.join(cwd, normalized))


def fileKindFromStats(stats: os.stat_result) -> str | None:
    if statModule.S_ISREG(stats.st_mode):
        return "file"
    if statModule.S_ISDIR(stats.st_mode):
        return "directory"
    if statModule.S_ISLNK(stats.st_mode):
        return "symlink"
    return None


def fileInfoFromStats(path: str, stats: os.stat_result) -> Result:
    kind = fileKindFromStats(stats)
    size = stats.st_size
    if (
        kind == "directory"
        and sys.platform == "win32"
        and getattr(stats, "st_reparse_tag", 0) == statModule.IO_REPARSE_TAG_MOUNT_POINT  # type: ignore[attr-defined]
    ):
        # libuv reports a junction as a symbolic link whose size is the length of its target.
        kind = "symlink"
        try:
            size = len(os.fsencode(os.readlink(path)))
        except OSError:
            size = 0
    if kind is None:
        return err(FileError("invalid", "Unsupported file type", path))
    return ok({"name": os.path.basename(path), "path": path, "kind": kind, "size": size, "mtimeMs": stats.st_mtime_ns / 1e6})


_WINDOWS_INVALID_NAME_CHARACTERS = frozenset('<>:"|?*')


def _hasInvalidWindowsName(path: str) -> bool:
    if path.startswith("\\\\?\\"):
        return False
    return any(character in _WINDOWS_INVALID_NAME_CHARACTERS for character in os.path.splitdrive(path)[1])


# MISAKA fork: `openingFile` -- Windows refuses to open a directory with a permission error, which is `is_directory`
# only when the call was opening a file.
def toFileError(error: Any, fallbackPath: str | None = None, openingFile: bool = False) -> FileError:
    if isinstance(error, FileError):
        return error
    cause = toError(error)
    path = getattr(error, "filename", None) if isinstance(getattr(error, "filename", None), str) else fallbackPath
    # Node's `CODE: description, syscall 'path'` where the call recorded its syscall (`withSyscall`).
    message = getattr(error, "uvMessage", None) or str(cause)
    if isinstance(error, OSError):
        number = error.errno
        winerror = getattr(error, "winerror", None)
        if sys.platform == "win32":
            # libuv maps ERROR_INVALID_NAME (a name Windows does not allow) to ENOENT, and a sharing violation to EBUSY.
            if winerror in (123, 161) or (number == errno.EINVAL and path is not None and _hasInvalidWindowsName(path)):
                return FileError("not_found", message, path, cause)
            if winerror in (32, 33):
                return FileError("unknown", message, path, cause)
        if isinstance(error, FileNotFoundError) or number == errno.ENOENT:
            return FileError("not_found", message, path, cause)
        if isinstance(error, PermissionError) or number in (errno.EACCES, errno.EPERM):
            # Windows refuses to open a directory as a file with a permission error; Node reports EISDIR.
            if sys.platform == "win32" and openingFile and path is not None and os.path.isdir(path):
                return FileError("is_directory", message, path, cause)
            return FileError("permission_denied", message, path, cause)
        if isinstance(error, NotADirectoryError) or number == errno.ENOTDIR:
            return FileError("not_directory", message, path, cause)
        if isinstance(error, IsADirectoryError) or number == errno.EISDIR:
            return FileError("is_directory", message, path, cause)
        if number == errno.EINVAL:
            return FileError("invalid", message, path, cause)
    if getattr(error, "name", None) == "AbortError":
        return FileError("aborted", message, path, cause)
    return FileError("unknown", message, path, cause)


def abortResult(signal: Any, path: str | None = None) -> Result | None:
    return err(FileError("aborted", "aborted", path)) if signal is not None and signal.aborted else None


async def pathExists(path: str) -> bool:
    return await asyncio.to_thread(os.path.exists, path)


async def runCommand(command: str, args: list[str], timeoutMs: float) -> dict[str, Any]:
    try:
        process = await asyncio.create_subprocess_exec(
            command,
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except (OSError, ValueError):
        return {"stdout": "", "status": None}
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeoutMs / 1000)
    except TimeoutError:
        if process.pid:
            killProcessTree(process.pid)
        await process.wait()
        return {"stdout": "", "status": None}
    return {"stdout": stdout.decode("utf-8", errors="replace"), "status": process.returncode}


async def findBashOnPath() -> str | None:
    result = (
        await runCommand("where", ["bash.exe"], 5000)
        if sys.platform == "win32"
        else await runCommand("which", ["bash"], 5000)
    )
    if result["status"] != 0 or not result["stdout"]:
        return None
    lines = result["stdout"].strip().splitlines()
    firstMatch = lines[0] if lines else None
    return firstMatch if firstMatch and await pathExists(firstMatch) else None


class ShellConfig:
    def __init__(self, shell: str, args: list[str], commandTransport: str | None = None) -> None:
        self.shell = shell
        self.args = args
        self.commandTransport = commandTransport


def isLegacyWslBashPath(path: str) -> bool:
    import re

    normalized = path.replace("/", "\\").lower()
    return re.fullmatch(r"[a-z]:\\windows\\(?:system32|sysnative)\\bash\.exe", normalized) is not None


def getBashShellConfig(shell: str) -> ShellConfig:
    return ShellConfig(shell, ["-s"], "stdin") if isLegacyWslBashPath(shell) else ShellConfig(shell, ["-c"])


async def getShellConfig(customShellPath: str | None = None) -> Result:
    if customShellPath:
        if await pathExists(customShellPath):
            return ok(getBashShellConfig(customShellPath))
        return err(ExecutionError("shell_unavailable", f"Custom shell path not found: {customShellPath}"))
    if sys.platform == "win32":
        candidates: list[str] = []
        programFiles = os.environ.get("ProgramFiles")
        if programFiles:
            candidates.append(f"{programFiles}\\Git\\bin\\bash.exe")
        programFilesX86 = os.environ.get("ProgramFiles(x86)")
        if programFilesX86:
            candidates.append(f"{programFilesX86}\\Git\\bin\\bash.exe")
        for candidate in candidates:
            if await pathExists(candidate):
                return ok(getBashShellConfig(candidate))
        bashOnPath = await findBashOnPath()
        if bashOnPath:
            return ok(getBashShellConfig(bashOnPath))
        return err(
            ExecutionError(
                "shell_unavailable",
                "No bash shell found. Options:\n"
                "  1. Install Git for Windows: https://git-scm.com/download/win\n"
                "  2. Add your bash to PATH (Cygwin, MSYS2, etc.)\n"
                "  3. Configure an explicit shellPath\n\n"
                "Searched Git Bash in:\n" + "\n".join(f"  {path}" for path in candidates),
            )
        )

    if await pathExists("/bin/bash"):
        return ok(getBashShellConfig("/bin/bash"))
    bashOnPath = await findBashOnPath()
    if bashOnPath:
        return ok(getBashShellConfig(bashOnPath))
    return ok(ShellConfig("sh", ["-c"]))


def getShellEnv(baseEnv: dict[str, str] | None = None, extraEnv: dict[str, str] | None = None, inheritEnv: bool = True) -> dict[str, str]:
    if not inheritEnv:
        return {**(extraEnv or {})}
    # MISAKA fork: MISAKA 0.18.9 -- a release archive's Python runs with PYTHONSAFEPATH/PYTHONNOUSERSITE, which a user's
    # `python script.py` must not inherit (misaka.utils.shell.getShellEnv strips them the same way).
    from misaka.config.engine import bundle_root
    from misaka.utils.shell import _ARCHIVE_PYTHON_FLAGS

    inherited = dict(os.environ)
    if bundle_root() is not None:
        for name in _ARCHIVE_PYTHON_FLAGS:
            inherited.pop(name, None)
    return {**inherited, **(baseEnv or {}), **(extraEnv or {})}


def killProcessTree(pid: int) -> None:
    if sys.platform != "win32":
        killShellProcessTree(pid)
        return
    try:
        child = subprocess.Popen(
            [
                os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe"),
                "/F",
                "/T",
                "/PID",
                str(pid),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except OSError:
        return  # A failed spawn is ignored, as Node ignores the asynchronous "error" of its taskkill child.
    # Reap the child in the background so it never outlives its handle as a zombie.
    threading.Thread(target=child.wait, daemon=True).start()


def _withMessage(error: OSError, syscall: str, path: str | None = None, dest: str | None = None) -> OSError:
    """``error`` with Node's message for ``syscall``."""
    if getattr(error, "uvMessage", None) is None and (error.errno is not None or getattr(error, "winerror", None)):
        error.uvMessage = uvMessage(error, syscall, path, dest)  # type: ignore[attr-defined]
    return error


def _directoryError(syscall: str, path: str | None = None) -> OSError:
    """EISDIR as Node reports it for ``syscall``. Python refuses to open a directory where Node opens it (always on
    Windows, through libuv's FILE_FLAG_BACKUP_SEMANTICS, and for reading on POSIX) and fails at the next call."""
    return _withMessage(IsADirectoryError(errno.EISDIR, "illegal operation on a directory", path), syscall, path)


def _readBytes(path: str) -> bytes:
    """``readFile``: Node opens a directory and fails when it reads it."""
    try:
        file = open(path, "rb")  # noqa: SIM115 - the `with` below closes it
    except OSError as error:
        if os.path.isdir(path):
            raise _directoryError("read") from error
        raise _withMessage(error, "open", path) from None
    with file:
        return withSyscall("read", file.read)


def _mkdirRecursive(path: str) -> None:
    """Node's ``mkdir(path, { recursive: true })``: an existing directory is fine, an existing file is EEXIST, and a
    file in the way of a parent is ENOTDIR (Windows reports a missing path there, which ``os.makedirs`` passes on)."""
    try:
        os.mkdir(path)
        return
    except FileExistsError as error:
        if os.path.isdir(path):
            return
        raise _withMessage(error, "mkdir", path) from None
    except FileNotFoundError as error:
        parent = os.path.dirname(path)
        if not parent or parent == path:
            raise _withMessage(error, "mkdir", path) from None
    except OSError as error:
        raise _withMessage(error, "mkdir", path) from None
    try:
        _mkdirRecursive(parent)
    except FileExistsError:
        raise _withMessage(NotADirectoryError(errno.ENOTDIR, "not a directory", parent), "mkdir", parent) from None
    try:
        os.mkdir(path)
    except FileExistsError as error:
        if os.path.isdir(path):
            return
        raise _withMessage(error, "mkdir", path) from None
    except OSError as error:
        raise _withMessage(error, "mkdir", path) from None


WRITE_CHUNK_BYTES = 512 * 1024


def _writeBytes(path: str, content: bytes, append: bool, isAborted: Callable[[], bool] | None = None) -> None:
    if sys.platform == "win32" and os.path.isdir(path):
        # libuv opens a directory to append to it, and the first write fails; it refuses to create a file over one.
        if not append:
            raise _directoryError("open", path)
        if content:
            raise _directoryError("write")
        return
    with withSyscall("open", open, path, "ab" if append else "wb", path=path) as file:
        # ``fs.writeFile(path, data, {signal})`` checks the signal between chunks.
        for offset in range(0, max(len(content), 1), WRITE_CHUNK_BYTES):
            if isAborted is not None and isAborted():
                raise DOMException("The operation was aborted", "AbortError")
            withSyscall("write", file.write, content[offset : offset + WRITE_CHUNK_BYTES])


def _encodeText(content: str | bytes) -> bytes:
    return toWellFormed(content).encode("utf-8") if isinstance(content, str) else bytes(content)


def _truncate(path: str, size: int) -> None:
    # Node opens a directory for writing on Windows (libuv's FILE_FLAG_BACKUP_SEMANTICS) and the truncation fails with
    # EINVAL; Python cannot open one there.
    if sys.platform == "win32" and os.path.isdir(path):
        raise _withMessage(OSError(errno.EINVAL, "invalid argument"), "ftruncate")
    with withSyscall("open", open, path, "r+b", path=path) as file:
        withSyscall("ftruncate", file.truncate, size)


def _sync(path: str) -> bool:
    """Whether ``path`` was a directory; otherwise its contents are flushed."""
    with withSyscall("open", open, path, "r+b", path=path) as file:
        # POSIX refuses to open a directory for writing; Windows opens it, so check explicitly.
        if statModule.S_ISDIR(withSyscall("fstat", os.fstat, file.fileno()).st_mode):
            return True
        file.flush()
        withSyscall("fsync", os.fsync, file.fileno())
    return False


class NodeTextLineReader:
    """Strict LF reader; it reports whether the final line was newline-terminated."""

    def __init__(self, file: BinaryIO | None, path: str) -> None:
        self._file = file
        self._path = path
        self._decoder = StreamDecoder()
        self._chunkSize = 64 * 1024
        self._byteOffset = 0
        self._buffered = ""
        self._ended = False
        self._closed = False

    def _readAt(self, offset: int) -> bytes:
        if self._file is None:
            # A directory opens and fails at its first read, as ``fs.open`` and ``read`` do.
            raise _directoryError("read")
        self._file.seek(offset)
        return withSyscall("read", self._file.read, self._chunkSize)

    async def readLine(self, context: Context) -> Result:
        aborted = abortResult(context.abortSignal, self._path)
        if aborted:
            return aborted
        if self._closed:
            return err(FileError("invalid", "Text line reader is closed", self._path))

        try:
            while True:
                newline = self._buffered.find("\n")
                if newline != -1:
                    text = self._buffered[:newline]
                    self._buffered = self._buffered[newline + 1 :]
                    return ok({"text": text, "terminated": True})
                if self._ended:
                    if len(self._buffered) == 0:
                        return ok(None)
                    text = self._buffered
                    self._buffered = ""
                    return ok({"text": text, "terminated": False})

                # Explicit positions allow an aborted read to be retried without skipping bytes.
                data = await asyncio.to_thread(self._readAt, self._byteOffset)
                afterReadAbort = abortResult(context.abortSignal, self._path)
                if afterReadAbort:
                    return afterReadAbort
                self._byteOffset += len(data)
                if len(data) == 0:
                    self._buffered += self._decoder.decode()
                    self._ended = True
                else:
                    self._buffered += self._decoder.decode(data)
        except Exception as error:  # noqa: BLE001
            return err(toFileError(error, self._path))

    async def close(self, _context: Context) -> None:
        if self._closed:
            return
        self._closed = True
        self._buffered = ""
        try:
            if self._file is not None:
                await asyncio.to_thread(self._file.close)
        except Exception:  # noqa: BLE001, S110
            pass  # Closing is best-effort, including after cancellation or an earlier I/O failure.


def closedResult(what: str, path: str) -> Result:
    return err(FileError("invalid", f"{what} is closed", path))


def _openReadOnly(path: str, noFollow: bool) -> int:
    """``open(path, O_RDONLY | O_NONBLOCK [| O_NOFOLLOW])`` as a file descriptor.

    MISAKA fork: on Windows the file is opened through ``CreateFileW`` with every share mode, as libuv opens it, so the
    opened file can still be renamed (and appended to) while it is read, and a directory opens and then fails its
    ``stat`` check as upstream's does. Python's ``open`` has no FILE_SHARE_DELETE.
    """
    if sys.platform == "win32":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
            wintypes.HANDLE,
        ]
        GENERIC_READ, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS = 0x80000000, 3, 0x02000000
        FILE_SHARE_READ_WRITE_DELETE = 0x1 | 0x2 | 0x4
        handle = kernel32.CreateFileW(
            path, GENERIC_READ, FILE_SHARE_READ_WRITE_DELETE, None, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None
        )
        if handle is None or handle == wintypes.HANDLE(-1).value:
            code = ctypes.get_last_error()
            raise OSError(None, ctypes.FormatError(code).strip(), path, code)
        return msvcrt.open_osfhandle(handle, os.O_RDONLY)
    flags = os.O_RDONLY
    # Nonblocking, so opening a FIFO does not wait for a writer.
    flags |= os.O_NONBLOCK
    if noFollow:
        flags |= os.O_NOFOLLOW
    return os.open(path, flags)


class NodeBinaryReader:
    def __init__(self, file: int, path: str) -> None:
        self._file = file
        self._path = path
        self._closed = False
        # MISAKA fork: Windows has no ``os.pread``; a seek and a read are one step under this lock.
        self._lock = threading.Lock()

    def _readAt(self, position: int, length: int) -> bytes:
        if hasattr(os, "pread"):
            return withSyscall("read", os.pread, self._file, length, position)
        with self._lock:
            os.lseek(self._file, position, os.SEEK_SET)
            return withSyscall("read", os.read, self._file, length)

    async def info(self, context: Context) -> Result:
        aborted = abortResult(context.abortSignal, self._path)
        if aborted:
            return aborted
        if self._closed:
            return closedResult("Binary reader", self._path)
        try:
            return fileInfoFromStats(self._path, await asyncio.to_thread(withSyscall, "fstat", os.fstat, self._file))
        except Exception as error:  # noqa: BLE001
            return err(toFileError(error, self._path))

    async def read(self, offset: float, length: float, context: Context) -> Result:
        aborted = abortResult(context.abortSignal, self._path)
        if aborted:
            return aborted
        if self._closed:
            return closedResult("Binary reader", self._path)
        if not jsIsSafeInteger(offset) or offset < 0 or not jsIsSafeInteger(length) or length < 0:
            return err(FileError("invalid", "Offset and length must be non-negative safe integers", self._path))
        start, wanted = int(offset), int(length)
        chunks: list[bytes] = []
        total = 0
        try:
            while total < wanted:
                bytesRead = await asyncio.to_thread(self._readAt, start + total, min(wanted - total, BINARY_READ_CHUNK))
                afterReadAbort = abortResult(context.abortSignal, self._path)
                if afterReadAbort:
                    return afterReadAbort
                if len(bytesRead) == 0:
                    break
                chunks.append(bytesRead)
                total += len(bytesRead)
        except Exception as error:  # noqa: BLE001
            return err(toFileError(error, self._path))
        if len(chunks) == 1:
            return ok(chunks[0])
        return ok(b"".join(chunks))

    async def scanLines(self, options: dict[str, Any], context: Context) -> Result:
        aborted = abortResult(context.abortSignal, self._path)
        if aborted:
            return aborted
        if self._closed:
            return closedResult("Binary reader", self._path)
        try:
            endLine = options.get("endLine")
            scanner = LineScanner(options.get("startLine"), math.inf if endLine is None else endLine)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            return err(FileError("invalid", "Invalid line range", self._path))
        try:
            position = 0
            while True:
                bytesRead = await asyncio.to_thread(self._readAt, position, 64 * 1024)
                afterReadAbort = abortResult(context.abortSignal, self._path)
                if afterReadAbort:
                    return afterReadAbort
                if len(bytesRead) == 0:
                    return ok(scanner.finish())
                scanner.push(bytesRead)
                position += len(bytesRead)
        except Exception as error:  # noqa: BLE001
            return err(toFileError(error, self._path))

    async def close(self, _context: Context) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            await asyncio.to_thread(os.close, self._file)


class NodeDirReader:
    def __init__(self, dir: Any, path: str) -> None:
        self._dir = dir
        self._path = path
        self._done = False
        self._closed = False

    async def next(self, maxEntries: float, context: Context) -> Result:
        aborted = abortResult(context.abortSignal, self._path)
        if aborted:
            return aborted
        if self._closed:
            return closedResult("Directory reader", self._path)
        if not jsIsSafeInteger(maxEntries) or maxEntries <= 0:
            return err(FileError("invalid", "maxEntries must be a positive safe integer", self._path))
        entries: list[dict[str, Any]] = []
        try:
            while not self._done and len(entries) < maxEntries:
                entry = await asyncio.to_thread(next, self._dir, None)
                if entry is None:
                    self._done = True
                    break
                loopAbort = abortResult(context.abortSignal, self._path)
                if loopAbort:
                    return loopAbort
                name = entry.name.decode("utf-8", "replace") if isinstance(entry.name, bytes) else entry.name
                entryPath = pathResolve(os.path.join(self._path, name))
                try:
                    stats = await asyncio.to_thread(withSyscall, "lstat", os.lstat, entryPath, path=entryPath)
                except Exception as error:  # noqa: BLE001
                    # Removed between enumeration and lstat: not part of the listing any more.
                    if isinstance(error, FileNotFoundError):
                        continue
                    return err(toFileError(error, entryPath))
                info = fileInfoFromStats(entryPath, stats)
                if info["ok"]:
                    entries.append(info["value"])
            return ok({"entries": entries, "done": self._done})
        except Exception as error:  # noqa: BLE001
            return err(toFileError(error, self._path))

    async def close(self, _context: Context) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self._dir.close)


def symlinkRefused(path: str, cause: BaseException | None = None) -> FileError:
    return FileError("invalid", "Refusing to follow a symbolic link", path, cause)


def _writeSpillChunk(file: BinaryIO, chunk: bytes) -> None:
    file.write(chunk)
    file.flush()


class _SpillStream:
    """The slice of a Node ``WriteStream`` the shell uses: ``write`` returns whether more may be written without waiting
    for ``drain``; writes happen in a thread, in order."""

    def __init__(self, file: BinaryIO, onError: Callable[[BaseException], None]) -> None:
        self._file = file
        self._onError = onError
        self._buffer: deque[bytes] = deque()
        self._buffered = 0
        self._needsDrain = False
        self._drainCallbacks: list[Callable[[], None]] = []
        self._writer: asyncio.Task[None] | None = None
        self.destroyed = False
        self._finished: asyncio.Future[None] | None = None

    def write(self, chunk: bytes) -> bool:
        self._buffer.append(chunk)
        self._buffered += len(chunk)
        if self._writer is None or self._writer.done():
            self._writer = asyncio.ensure_future(self._run())
        accepted = self._buffered < SPILL_HIGH_WATER_MARK
        if not accepted:
            self._needsDrain = True
        return accepted

    def onceDrain(self, callback: Callable[[], None]) -> None:
        self._drainCallbacks.append(callback)

    async def _run(self) -> None:
        try:
            while self._buffer:
                chunk = self._buffer.popleft()
                await asyncio.to_thread(_writeSpillChunk, self._file, chunk)
                self._buffered -= len(chunk)
                if self._needsDrain and self._buffered < SPILL_HIGH_WATER_MARK:
                    self._needsDrain = False
                    callbacks, self._drainCallbacks = self._drainCallbacks, []
                    for callback in callbacks:
                        callback()
        except BaseException as error:  # noqa: BLE001
            self.destroyed = True
            self._buffer.clear()
            self._onError(error)

    async def end(self) -> None:
        writer = self._writer
        if writer is not None:
            await asyncio.shield(writer)
        try:
            await asyncio.to_thread(self._file.close)
        except Exception as error:  # noqa: BLE001
            self.destroyed = True
            self._onError(error)


class _ExecRun:
    """One ``exec`` call: the child, its output pipeline, the optional spill, and the single settlement."""

    def __init__(
        self,
        env: NodeExecutionEnv,
        program: str,
        args: list[str],
        stdinCommand: str | None,
        options: dict[str, Any],
        context: Context,
        cwd: str,
        timeoutMs: float | None,
    ) -> None:
        self.env = env
        self.program = program
        self.args = args
        self.stdinCommand = stdinCommand
        self.options = options
        self.context = context
        self.cwd = cwd
        self.timeoutMs = timeoutMs
        self.signal = context.abortSignal
        self.loop = asyncio.get_running_loop()
        self.result: asyncio.Future[Result] = self.loop.create_future()
        self.settled = False
        self.timedOut = False
        self.callbackError: ExecutionError | None = None
        self.spillError: ExecutionError | None = None
        self.child: ChildProcess | None = None
        self.timeoutHandle: asyncio.TimerHandle | None = None
        self.spillPrefix: list[bytes] = []
        self.spillPath: str | None = None
        self.spillQueue: list[bytes] = []
        self.spillStart: asyncio.Future[None] | None = None
        self.spillStream: _SpillStream | None = None
        self.spillBackpressured = False
        # One decoder per stream, so a character split across chunks of one stream survives interleaving.
        self.decoders = {
            1: StreamDecoder(),
            2: StreamDecoder(),
        }
        # Output seen before the spill starts: counted against the thresholds and kept for the spill's prefix.
        self.seenBytes = 0
        self.seenNewlines = 0
        self.pendingTasks: set[asyncio.Task[Any]] = set()

    def onAbort(self) -> None:
        if self.child is not None and self.child.pid:
            killProcessTree(self.child.pid)

    def failCallback(self, error: BaseException) -> None:
        if self.callbackError is not None:
            return
        cause = toError(error)
        self.callbackError = ExecutionError("callback_error", str(cause), cause)
        self.onAbort()

    def emit(self, text: str, stream: str) -> None:
        # No output reaches the caller after exec() settled, for example from a descendant holding stdio open.
        onOutput = self.options.get("onOutput")
        if self.settled or text == "" or onOutput is None or self.callbackError is not None:
            return
        try:
            onOutput(text, self.context, {"stream": stream})
        except Exception as error:  # noqa: BLE001
            self.failCallback(error)

    def settle(self, result: Result) -> None:
        if self.settled:
            return
        self.settled = True
        if self.timeoutHandle is not None:
            self.timeoutHandle.cancel()
        if self.signal is not None:
            self.signal.removeEventListener("abort", self.onAbort)
        if self.child is not None and self.child.pid:
            self.env._activeChildPids.discard(self.child.pid)
        if not self.result.done():
            self.result.set_result(result)

    def pauseOutput(self) -> None:
        if self.child is not None:
            self.child.pause_output()

    def resumeOutput(self) -> None:
        if (
            self.callbackError is not None
            or self.spillError is not None
            or self.timedOut
            or (self.signal is not None and self.signal.aborted)
            or self.spillBackpressured
        ):
            return
        if self.child is not None:
            self.child.resume_output()

    def failSpill(self, error: BaseException) -> None:
        if self.spillError is not None:
            return
        cause = toError(error)
        self.spillError = ExecutionError("unknown", f"Failed to preserve complete shell output: {cause}", cause)
        self.spillBackpressured = False
        self.onAbort()

    def writeSpill(self, chunk: bytes) -> None:
        if self.spillStream is None or len(chunk) == 0:
            return
        if self.spillStream.write(chunk) or self.spillBackpressured:
            return
        self.spillBackpressured = True
        self.pauseOutput()

        def drained() -> None:
            self.spillBackpressured = False
            self.resumeOutput()

        self.spillStream.onceDrain(drained)

    def startSpill(self, chunk: bytes) -> None:
        if self.spillStream is not None:
            self.writeSpill(chunk)
            return
        self.spillQueue.append(chunk)
        if self.spillStart is not None:
            return
        self.pauseOutput()
        self.spillStart = asyncio.ensure_future(self._openSpill())

    async def _openSpill(self) -> None:
        try:
            created = await self.env.createTempFile({"prefix": "pi-output-", "suffix": ".log"}, self.context)
            if not created["ok"]:
                raise created["error"]
            self.spillPath = created["value"]
            file = await asyncio.to_thread(open, self.spillPath, "ab")
            self.spillStream = _SpillStream(file, self.failSpill)
            for queued in self.spillQueue:
                self.writeSpill(queued)
            self.spillQueue.clear()
        except Exception as error:  # noqa: BLE001
            self.failSpill(error)
        finally:
            self.resumeOutput()

    async def finishSpill(self) -> None:
        if self.spillStart is not None:
            await self.spillStart
        stream = self.spillStream
        if stream is None or self.spillError is not None or stream.destroyed:
            return
        await stream.end()

    def feed(self, fd: int, chunk: bytes) -> None:
        self.emit(self.decoders[fd].decode(chunk), "stdout" if fd == 1 else "stderr")
        spill = self.options.get("spill")
        if spill is None or len(chunk) == 0:
            return
        if self.spillStart is not None:
            self.startSpill(chunk)
            return
        self.seenBytes += len(chunk)
        self.seenNewlines += chunk.count(b"\n")
        lines = self.seenNewlines + (0 if chunk[-1] == 0x0A else 1)
        if self.seenBytes <= spill["afterBytes"] and lines <= spill["afterLines"]:
            self.spillPrefix.append(chunk)
            return
        for prefix in self.spillPrefix:
            self.startSpill(prefix)
        self.spillPrefix.clear()
        self.startSpill(chunk)

    def spillIsDraining(self) -> bool:
        return (
            self.spillError is None
            and self.spillStart is not None
            and (self.spillStream is None or self.spillBackpressured)
        )

    def _spawned(self, child: ChildProcess) -> None:
        self.child = child

    async def run(self) -> Result:
        try:
            # MISAKA fork: `spawn` plus `waitForChildProcess` are misaka.utils.child_process, an asyncio subprocess
            # protocol with the same exit-then-stdio settling. As upstream wires `spillIsDraining` and the child before
            # any event of it can fire, the check goes in at construction and the child is ours at `on_spawn`, before
            # its first chunk (a spill pauses that one already): set later, the exit could have armed the 100 ms
            # post-exit grace first, and a loop that took that long to resume closed the pipes on a backpressured
            # spill, losing what a descendant wrote afterwards.
            self.child = await spawn_child_process(
                self.program,
                *self.args,
                cwd=self.cwd,
                env=getShellEnv(self.env.shellEnv, self.options.get("env"), self.options.get("inheritEnv", True)),
                stdin=subprocess.DEVNULL if self.stdinCommand is None else subprocess.PIPE,
                start_new_session=sys.platform != "win32",
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                on_data=self.feed,
                is_draining=self.spillIsDraining,
                on_spawn=self._spawned,
            )
            if self.child.pid:
                self.env._activeChildPids.add(self.child.pid)
            if self.stdinCommand is not None:
                self.child.write_stdin(toWellFormed(self.stdinCommand).encode("utf-8"))
                self.child.close_stdin()
        except Exception as error:  # noqa: BLE001
            cause = toError(error)
            return err(ExecutionError("spawn_error", str(cause), cause))

        if self.timeoutMs is not None:

            def onTimeout() -> None:
                self.timedOut = True
                self.onAbort()

            self.timeoutHandle = self.loop.call_later(self.timeoutMs / 1000, onTimeout)

        if self.signal is not None:
            if self.signal.aborted:
                self.onAbort()
            else:
                self.signal.addEventListener("abort", self.onAbort, once=True)

        try:
            code = await self.child.wait()
        except Exception as error:  # noqa: BLE001
            cause = toError(error)
            self.settle(err(ExecutionError("spawn_error", str(cause), cause)))
            return await self.result
        await self.finishSpill()
        self.emit(self.decoders[1].decode(), "stdout")
        self.emit(self.decoders[2].decode(), "stderr")
        if self.callbackError is not None:
            self.settle(err(self.callbackError))
            return await self.result
        interrupted: ExecutionError | None = None
        if self.timedOut:
            interrupted = ExecutionError("timeout", f"timeout:{formatTimeout(self.options.get('timeout'))}")
        elif self.signal is not None and self.signal.aborted:
            interrupted = ExecutionError("aborted", "aborted")
        if interrupted is not None:
            if self.spillPath is not None:
                interrupted.spillPath = self.spillPath
            self.settle(err(interrupted))
            return await self.result
        if self.spillError is not None:
            self.settle(err(self.spillError))
            return await self.result
        # A process killed by a signal (e.g. OOM killer) has no exit code; map it to the conventional 128 + signal number
        # so callers do not mistake it for a successful exit.
        if code is None:
            exitCode = 1
        elif code < 0:
            exitCode = 128 + (-code)
        else:
            exitCode = code
        self.settle(ok({"exitCode": exitCode, **({} if self.spillPath is None else {"spillPath": self.spillPath})}))
        return await self.result


def formatTimeout(timeout: Any) -> str:
    if timeout is None:
        return "undefined"
    return formatNumber(timeout) if isinstance(timeout, int | float) and not isinstance(timeout, bool) else str(timeout)


class NodeExecutionEnv:
    """The local environment: ``cwd`` resolves relative paths, ``shellPath`` and ``shellEnv`` configure the shell,
    ``watch`` (``NodeWatchOptions``) how ``watch()`` watches."""

    def __init__(self, options: dict[str, Any]) -> None:
        self.id = "node:local"  # Every local environment sees the same files.
        self.cwd: str = options["cwd"]
        self.shellPath: str | None = options.get("shellPath")
        self.shellEnv: dict[str, str] | None = options.get("shellEnv")
        watch = options.get("watch")
        self._watchOptions: NodeWatchOptions = {} if watch is None else watch
        self._activeChildPids: set[int] = set()

    async def watch(self, targets: list[Any], onChange: Callable[[Any], None], context: Context) -> Result:
        aborted = abortResult(context.abortSignal)
        if aborted:
            return aborted
        try:
            watcher = await NodeFileWatcher.open(
                targets,
                lambda path: resolvePath(self.cwd, path),
                onChange,
                self._watchOptions,
            )
            afterOpenAbort = abortResult(context.abortSignal)
            if afterOpenAbort:
                await watcher.close(context)
                return afterOpenAbort
            return ok(watcher)
        except Exception as error:  # noqa: BLE001
            return err(toFileError(error))

    async def absolutePath(self, path: str, _context: Context) -> Result:
        return ok(resolvePath(self.cwd, path))

    async def joinPath(self, parts: list[str], _context: Context) -> Result:
        return ok(node_path.join(*parts))

    async def exec(self, command: str | list[str], options: dict[str, Any] | None, context: Context) -> Result:
        options = options or {}
        signal = context.abortSignal
        if signal is not None and signal.aborted:
            return err(ExecutionError("aborted", "aborted"))
        timeoutMsResult = resolveTimeoutMs(options.get("timeout"))
        if not timeoutMsResult["ok"]:
            return err(timeoutMsResult["error"])
        timeoutMs = timeoutMsResult["value"]

        cwd = resolvePath(self.cwd, options["cwd"]) if options.get("cwd") else self.cwd
        # A string runs through the shell; an argv array runs its program directly, without shell parsing.
        stdinCommand: str | None = None
        if isinstance(command, str):
            shellConfig = await getShellConfig(self.shellPath)
            if not shellConfig["ok"]:
                return shellConfig
            commandFromStdin = shellConfig["value"].commandTransport == "stdin"
            program = shellConfig["value"].shell
            args = shellConfig["value"].args if commandFromStdin else [*shellConfig["value"].args, command]
            stdinCommand = command if commandFromStdin else None
        else:
            if len(command) == 0:
                return err(ExecutionError("spawn_error", "Empty argv: no program to run"))
            program, args = command[0], list(command[1:])
        if not await asyncio.to_thread(os.access, cwd, os.F_OK):
            cause = FileNotFoundError(errno.ENOENT, "no such file or directory, access", cwd)
            return err(
                ExecutionError(
                    "spawn_error", f"Working directory does not exist: {cwd}\nCannot execute bash commands.", cause
                )
            )
        return await _ExecRun(self, program, args, stdinCommand, options, context, cwd, timeoutMs).run()

    async def openTextLineReader(self, path: str, context: Context) -> Result:
        resolved = resolvePath(self.cwd, path)
        aborted = abortResult(context.abortSignal, resolved)
        if aborted:
            return aborted
        try:
            if await asyncio.to_thread(os.path.isdir, resolved):
                return ok(NodeTextLineReader(None, resolved))
            # The reader closes it.
            file = await asyncio.to_thread(withSyscall, "open", lambda: open(resolved, "rb"), path=resolved)  # noqa: SIM115
            afterOpenAbort = abortResult(context.abortSignal, resolved)
            if afterOpenAbort:
                try:
                    await asyncio.to_thread(file.close)
                except Exception:  # noqa: BLE001, S110
                    pass
                return afterOpenAbort
            return ok(NodeTextLineReader(file, resolved))
        except Exception as error:  # noqa: BLE001
            return err(toFileError(error, resolved, True))

    async def readTextFile(self, path: str, context: Context) -> Result:
        resolved = resolvePath(self.cwd, path)
        signal = context.abortSignal
        aborted = abortResult(signal, resolved)
        if aborted:
            return aborted
        try:
            data = await asyncio.to_thread(_readBytes, resolved)
            return abortResult(signal, resolved) or ok(data.decode("utf-8", errors="replace"))
        except Exception as error:  # noqa: BLE001
            return err(toFileError(error, resolved, True))

    async def readTextLines(self, path: str, options: dict[str, Any] | None, context: Context) -> Result:
        maxLines = None if options is None else options.get("maxLines")
        if maxLines is not None and maxLines <= 0:
            return ok([])
        opened = await self.openTextLineReader(path, context)
        if not opened["ok"]:
            return opened
        lines: list[str] = []
        try:
            while maxLines is None or len(lines) < maxLines:
                line = await opened["value"].readLine(context)
                if not line["ok"]:
                    return line
                if line["value"] is None:
                    break
                lines.append(line["value"]["text"])
            return ok(lines)
        finally:
            await opened["value"].close(context)

    async def readBinaryFile(self, path: str, context: Context) -> Result:
        resolved = resolvePath(self.cwd, path)
        signal = context.abortSignal
        aborted = abortResult(signal, resolved)
        if aborted:
            return aborted
        try:
            data = await asyncio.to_thread(_readBytes, resolved)
            return abortResult(signal, resolved) or ok(data)
        except Exception as error:  # noqa: BLE001
            return err(toFileError(error, resolved, True))

    async def openBinaryReader(self, path: str, options: dict[str, Any] | None, context: Context) -> Result:
        resolved = resolvePath(self.cwd, path)
        aborted = abortResult(context.abortSignal, resolved)
        if aborted:
            return aborted
        noFollow = options is not None and options.get("noFollow") is True
        file: int | None = None
        try:
            if sys.platform == "win32":
                # Windows has no O_NOFOLLOW; check the final component before opening (not race-free).
                if noFollow:
                    linkStats = await asyncio.to_thread(os.lstat, resolved)
                    linkInfo = fileInfoFromStats(resolved, linkStats)
                    if linkInfo["ok"] and linkInfo["value"]["kind"] == "symlink":
                        return err(symlinkRefused(resolved))
            file = await asyncio.to_thread(withSyscall, "open", _openReadOnly, resolved, noFollow, path=resolved)
            stats = await asyncio.to_thread(withSyscall, "fstat", os.fstat, file)
            if not statModule.S_ISREG(stats.st_mode):
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(os.close, file)
                return err(
                    FileError("is_directory", "EISDIR: illegal operation on a directory, read", resolved)
                    if statModule.S_ISDIR(stats.st_mode)
                    else FileError("invalid", "Not a regular file", resolved)
                )
            afterOpenAbort = abortResult(context.abortSignal, resolved)
            if afterOpenAbort:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(os.close, file)
                return afterOpenAbort
            return ok(NodeBinaryReader(file, resolved))
        except Exception as error:  # noqa: BLE001
            if file is not None:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(os.close, file)
            # O_NOFOLLOW reports a final-component symlink as ELOOP (EMLINK on some BSDs).
            if noFollow and isinstance(error, OSError) and error.errno in (errno.ELOOP, errno.EMLINK):
                return err(symlinkRefused(resolved, toError(error)))
            return err(toFileError(error, resolved))

    async def writeFile(self, path: str, content: str | bytes, context: Context) -> Result:
        return await self._write(path, content, context, False)

    async def appendFile(self, path: str, content: str | bytes, context: Context) -> Result:
        return await self._write(path, content, context, True)

    async def _write(self, path: str, content: str | bytes, context: Context, append: bool) -> Result:
        resolved = resolvePath(self.cwd, path)
        signal = context.abortSignal
        aborted = abortResult(signal, resolved)
        if aborted:
            return aborted
        try:
            await asyncio.to_thread(_mkdirRecursive, os.path.dirname(resolved))
            afterMkdirAbort = abortResult(signal, resolved)
            if afterMkdirAbort:
                return afterMkdirAbort
            await asyncio.to_thread(
                _writeBytes, resolved, _encodeText(content), append, lambda: signal is not None and signal.aborted
            )
            return ok(None)
        except Exception as error:  # noqa: BLE001
            return err(toFileError(error, resolved, True))

    async def truncateFile(self, path: str, size: int, context: Context) -> Result:
        resolved = resolvePath(self.cwd, path)
        signal = context.abortSignal
        aborted = abortResult(signal, resolved)
        if aborted:
            return aborted
        if not jsIsInteger(size) or size < 0 or size > 2**53 - 1:
            return err(FileError("invalid", "File size must be a non-negative safe integer", resolved))
        size = int(size)
        try:
            await asyncio.to_thread(_truncate, resolved, size)
            return abortResult(signal, resolved) or ok(None)
        except Exception as error:  # noqa: BLE001
            return err(toFileError(error, resolved, True))

    async def flushFile(self, path: str, context: Context) -> Result:
        resolved = resolvePath(self.cwd, path)
        signal = context.abortSignal
        aborted = abortResult(signal, resolved)
        if aborted:
            return aborted
        try:
            if await asyncio.to_thread(_sync, resolved):
                return err(FileError("is_directory", "Is a directory", resolved))
            return abortResult(signal, resolved) or ok(None)
        except Exception as error:  # noqa: BLE001
            return err(toFileError(error, resolved, True))

    async def renameFile(self, sourcePath: str, destinationPath: str, context: Context) -> Result:
        source = resolvePath(self.cwd, sourcePath)
        destination = resolvePath(self.cwd, destinationPath)
        aborted = abortResult(context.abortSignal, destination)
        if aborted:
            return aborted
        try:
            await asyncio.to_thread(withSyscall, "rename", os.replace, source, destination, path=source, dest=destination)
            return ok(None)
        except Exception as error:  # noqa: BLE001
            return err(toFileError(error, source))

    async def fileInfo(self, path: str, context: Context) -> Result:
        resolved = resolvePath(self.cwd, path)
        aborted = abortResult(context.abortSignal, resolved)
        if aborted:
            return aborted
        try:
            return fileInfoFromStats(resolved, await asyncio.to_thread(withSyscall, "lstat", os.lstat, resolved, path=resolved))
        except Exception as error:  # noqa: BLE001
            return err(toFileError(error, resolved))

    async def listDir(self, path: str, context: Context) -> Result:
        resolved = resolvePath(self.cwd, path)
        signal = context.abortSignal
        aborted = abortResult(signal, resolved)
        if aborted:
            return aborted
        try:
            names = await asyncio.to_thread(_listNames, resolved)
            infos: list[dict[str, Any]] = []
            for name in names:
                loopAbort = abortResult(signal, resolved)
                if loopAbort:
                    return loopAbort
                entryPath = os.path.abspath(os.path.join(resolved, name))
                try:
                    info = fileInfoFromStats(
                        entryPath, await asyncio.to_thread(withSyscall, "lstat", os.lstat, entryPath, path=entryPath)
                    )
                    if info["ok"]:
                        infos.append(info["value"])
                except Exception as error:  # noqa: BLE001
                    return err(toFileError(error, entryPath))
            return ok(infos)
        except Exception as error:  # noqa: BLE001
            return err(toFileError(error, resolved))

    async def openDirReader(self, path: str, context: Context) -> Result:
        resolved = resolvePath(self.cwd, path)
        aborted = abortResult(context.abortSignal, resolved)
        if aborted:
            return aborted
        try:
            # Names as Node decodes them: UTF-8 with replacement characters.
            target: str | bytes = resolved if sys.platform == "win32" else os.fsencode(resolved)
            dir = await asyncio.to_thread(withSyscall, "opendir", os.scandir, target, path=resolved)
            afterOpenAbort = abortResult(context.abortSignal, resolved)
            if afterOpenAbort:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(dir.close)
                return afterOpenAbort
            return ok(NodeDirReader(dir, resolved))
        except Exception as error:  # noqa: BLE001
            return err(toFileError(error, resolved))

    async def canonicalPath(self, path: str, context: Context) -> Result:
        resolved = resolvePath(self.cwd, path)
        aborted = abortResult(context.abortSignal, resolved)
        if aborted:
            return aborted
        try:
            return ok(
                await asyncio.to_thread(
                    withSyscall, "realpath", lambda: os.path.realpath(resolved, strict=True), path=resolved
                )
            )
        except Exception as error:  # noqa: BLE001
            return err(toFileError(error, resolved))

    async def exists(self, path: str, context: Context) -> Result:
        result = await self.fileInfo(path, context)
        if result["ok"]:
            return ok(True)
        if result["error"].code == "not_found":
            return ok(False)
        return err(result["error"])

    async def createDir(self, path: str, options: dict[str, Any] | None, context: Context) -> Result:
        resolved = resolvePath(self.cwd, path)
        aborted = abortResult(context.abortSignal, resolved)
        if aborted:
            return aborted
        recursive = True if options is None or options.get("recursive") is None else options["recursive"]
        try:
            if recursive:
                await asyncio.to_thread(_mkdirRecursive, resolved)
            else:
                await asyncio.to_thread(withSyscall, "mkdir", os.mkdir, resolved, path=resolved)
            return ok(None)
        except Exception as error:  # noqa: BLE001
            return err(toFileError(error, resolved))

    async def remove(self, path: str, options: dict[str, Any] | None, context: Context) -> Result:
        resolved = resolvePath(self.cwd, path)
        aborted = abortResult(context.abortSignal, resolved)
        if aborted:
            return aborted
        options = options or {}
        try:
            await asyncio.to_thread(_remove, resolved, bool(options.get("recursive")), bool(options.get("force")))
            return ok(None)
        except Exception as error:  # noqa: BLE001
            return err(toFileError(error, resolved))

    async def createTempDir(self, prefix: str | None, context: Context) -> Result:
        aborted = abortResult(context.abortSignal)
        if aborted:
            return aborted
        try:
            name = "tmp-" if prefix is None else prefix
            return ok(
                await asyncio.to_thread(
                    withSyscall,
                    "mkdtemp",
                    lambda: tempfile.mkdtemp(prefix=name),
                    path=f"{os.path.join(tempfile.gettempdir(), name)}XXXXXX",
                )
            )
        except Exception as error:  # noqa: BLE001
            return err(toFileError(error))

    async def createTempFile(self, options: dict[str, Any] | None, context: Context) -> Result:
        directory = await self.createTempDir("tmp-", context)
        if not directory["ok"]:
            return directory
        options = options or {}
        filePath = os.path.join(
            directory["value"], f"{options.get('prefix') or ''}{uuid.uuid4()}{options.get('suffix') or ''}"
        )
        try:
            await asyncio.to_thread(_writeBytes, filePath, b"", False)
            return ok(filePath)
        except Exception as error:  # noqa: BLE001
            return err(toFileError(error, filePath))

    async def cleanup(self, _context: Context) -> None:
        for pid in list(self._activeChildPids):
            killProcessTree(pid)
        self._activeChildPids.clear()


def _listNames(path: str) -> list[str]:
    """``readdir``: names decoded as UTF-8 with replacement characters, which libuv sorts by their bytes on POSIX (on
    Windows it keeps the file system's order)."""
    if sys.platform == "win32":
        return withSyscall("scandir", os.listdir, path, path=path)
    names = withSyscall("scandir", os.listdir, os.fsencode(path), path=path)
    return [name.decode("utf-8", "replace") for name in sorted(names)]


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except PermissionError:
        if sys.platform != "win32":
            raise
        # Node's ``rm`` clears the read-only attribute (chmod 0o666) and tries again on Windows.
        os.chmod(path, statModule.S_IWRITE)
        os.unlink(path)


def _onRemoveError(function: Callable[[str], Any], path: str, error: BaseException) -> None:
    if sys.platform == "win32" and isinstance(error, PermissionError):
        os.chmod(path, statModule.S_IWRITE)
        function(path)
        return
    raise error


def _remove(path: str, recursive: bool, force: bool) -> None:
    try:
        info = withSyscall("lstat", os.lstat, path, path=path)
    except FileNotFoundError:
        if force:
            return
        raise
    if sys.platform == "win32" and os.path.isjunction(path):
        # A junction is a link to ``rm``: it is unlinked and its target is left alone.
        withSyscall("rmdir", os.rmdir, path, path=path)
    elif statModule.S_ISDIR(info.st_mode):
        if not recursive:
            raise RuntimeError(f"Path is a directory: rm returned EISDIR (is a directory) {path}")
        withSyscall("rm", lambda: shutil.rmtree(path, onexc=_onRemoveError), path=path)
    else:
        withSyscall("unlink", _unlink, path, path=path)


__all__ = ["NodeBinaryReader", "NodeDirReader", "NodeExecutionEnv", "NodeTextLineReader", "NodeWatchOptions"]


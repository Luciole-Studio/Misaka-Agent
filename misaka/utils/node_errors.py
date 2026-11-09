"""The messages of Node's file system errors (``lib/internal/errors.js``'s ``uvException``):
``CODE: description, syscall 'path' -> 'dest'``, with libuv's error names and descriptions.

``UV_ERROR_MESSAGES`` is ``util.getSystemErrorMap()`` of Node 24 by name. A Python ``OSError`` carries an errno (and on
Windows the system error, which libuv translates by its own table), not the syscall; ``withSyscall`` records the syscall
Node would name, and ``uvMessage`` builds the message from both.
"""

from __future__ import annotations

import errno
import sys
from collections.abc import Callable
from typing import Any

UV_ERROR_MESSAGES = {
    "E2BIG": "argument list too long",
    "EACCES": "permission denied",
    "EADDRINUSE": "address already in use",
    "EADDRNOTAVAIL": "address not available",
    "EAFNOSUPPORT": "address family not supported",
    "EAGAIN": "resource temporarily unavailable",
    "EAI_ADDRFAMILY": "address family not supported",
    "EAI_AGAIN": "temporary failure",
    "EAI_BADFLAGS": "bad ai_flags value",
    "EAI_BADHINTS": "invalid value for hints",
    "EAI_CANCELED": "request canceled",
    "EAI_FAIL": "permanent failure",
    "EAI_FAMILY": "ai_family not supported",
    "EAI_MEMORY": "out of memory",
    "EAI_NODATA": "no address",
    "EAI_NONAME": "unknown node or service",
    "EAI_OVERFLOW": "argument buffer overflow",
    "EAI_PROTOCOL": "resolved protocol is unknown",
    "EAI_SERVICE": "service not available for socket type",
    "EAI_SOCKTYPE": "socket type not supported",
    "EALREADY": "connection already in progress",
    "EBADF": "bad file descriptor",
    "EBUSY": "resource busy or locked",
    "ECANCELED": "operation canceled",
    "ECHARSET": "invalid Unicode character",
    "ECONNABORTED": "software caused connection abort",
    "ECONNREFUSED": "connection refused",
    "ECONNRESET": "connection reset by peer",
    "EDESTADDRREQ": "destination address required",
    "EEXIST": "file already exists",
    "EFAULT": "bad address in system call argument",
    "EFBIG": "file too large",
    "EFTYPE": "inappropriate file type or format",
    "EHOSTDOWN": "host is down",
    "EHOSTUNREACH": "host is unreachable",
    "EILSEQ": "illegal byte sequence",
    "EINTR": "interrupted system call",
    "EINVAL": "invalid argument",
    "EIO": "i/o error",
    "EISCONN": "socket is already connected",
    "EISDIR": "illegal operation on a directory",
    "ELOOP": "too many symbolic links encountered",
    "EMFILE": "too many open files",
    "EMLINK": "too many links",
    "EMSGSIZE": "message too long",
    "ENAMETOOLONG": "name too long",
    "ENETDOWN": "network is down",
    "ENETUNREACH": "network is unreachable",
    "ENFILE": "file table overflow",
    "ENOBUFS": "no buffer space available",
    "ENODATA": "no data available",
    "ENODEV": "no such device",
    "ENOENT": "no such file or directory",
    "ENOEXEC": "exec format error",
    "ENOMEM": "not enough memory",
    "ENONET": "machine is not on the network",
    "ENOPROTOOPT": "protocol not available",
    "ENOSPC": "no space left on device",
    "ENOSYS": "function not implemented",
    "ENOTCONN": "socket is not connected",
    "ENOTDIR": "not a directory",
    "ENOTEMPTY": "directory not empty",
    "ENOTSOCK": "socket operation on non-socket",
    "ENOTSUP": "operation not supported on socket",
    "ENOTTY": "inappropriate ioctl for device",
    "ENXIO": "no such device or address",
    "EOF": "end of file",
    "EOVERFLOW": "value too large for defined data type",
    "EPERM": "operation not permitted",
    "EPIPE": "broken pipe",
    "EPROTO": "protocol error",
    "EPROTONOSUPPORT": "protocol not supported",
    "EPROTOTYPE": "protocol wrong type for socket",
    "ERANGE": "result too large",
    "EREMOTEIO": "remote I/O error",
    "EROFS": "read-only file system",
    "ESHUTDOWN": "cannot send after transport endpoint shutdown",
    "ESOCKTNOSUPPORT": "socket type not supported",
    "ESPIPE": "invalid seek",
    "ESRCH": "no such process",
    "ETIMEDOUT": "connection timed out",
    "ETXTBSY": "text file is busy",
    "EUNATCH": "protocol driver not attached",
    "EXDEV": "cross-device link not permitted",
    "UNKNOWN": "unknown error",
}

# libuv's `uv_translate_sys_error` for the Windows errors file operations produce.
UV_WINDOWS_ERROR_NAMES = {
    1: "EISDIR",  # ERROR_INVALID_FUNCTION
    2: "ENOENT",  # ERROR_FILE_NOT_FOUND
    3: "ENOENT",  # ERROR_PATH_NOT_FOUND
    4: "EMFILE",  # ERROR_TOO_MANY_OPEN_FILES
    5: "EPERM",  # ERROR_ACCESS_DENIED
    6: "EBADF",  # ERROR_INVALID_HANDLE
    8: "ENOMEM",  # ERROR_NOT_ENOUGH_MEMORY
    13: "EINVAL",  # ERROR_INVALID_DATA
    14: "ENOMEM",  # ERROR_OUTOFMEMORY
    15: "ENOENT",  # ERROR_INVALID_DRIVE
    17: "EXDEV",  # ERROR_NOT_SAME_DEVICE
    19: "EROFS",  # ERROR_WRITE_PROTECT
    32: "EBUSY",  # ERROR_SHARING_VIOLATION
    33: "EBUSY",  # ERROR_LOCK_VIOLATION
    39: "ENOSPC",  # ERROR_HANDLE_DISK_FULL
    50: "ENOTSUP",  # ERROR_NOT_SUPPORTED
    80: "EEXIST",  # ERROR_FILE_EXISTS
    82: "ENOSPC",  # ERROR_CANNOT_MAKE
    87: "EINVAL",  # ERROR_INVALID_PARAMETER
    112: "ENOSPC",  # ERROR_DISK_FULL
    122: "EINVAL",  # ERROR_INSUFFICIENT_BUFFER
    123: "ENOENT",  # ERROR_INVALID_NAME
    126: "ENOENT",  # ERROR_MOD_NOT_FOUND
    131: "EINVAL",  # ERROR_NEGATIVE_SEEK
    145: "ENOTEMPTY",  # ERROR_DIR_NOT_EMPTY
    161: "ENOENT",  # ERROR_BAD_PATHNAME
    183: "EEXIST",  # ERROR_ALREADY_EXISTS
    206: "ENAMETOOLONG",  # ERROR_FILENAME_EXCED_RANGE
    267: "ENOENT",  # ERROR_DIRECTORY
    740: "EACCES",  # ERROR_ELEVATION_REQUIRED
    998: "EFAULT",  # ERROR_NOACCESS
    1314: "EPERM",  # ERROR_PRIVILEGE_NOT_HELD
    1464: "EINVAL",  # ERROR_SYMLINK_NOT_SUPPORTED
    1920: "EACCES",  # ERROR_CANT_ACCESS_FILE
    1921: "ELOOP",  # ERROR_CANT_RESOLVE_FILENAME
    4392: "ENOENT",  # ERROR_INVALID_REPARSE_DATA
}

# Linux spells EOPNOTSUPP and ENOTSUP with one number; libuv names it ENOTSUP, as do EWOULDBLOCK and EAGAIN.
_PREFERRED_NAMES = ("ENOTSUP", "EAGAIN")


def uvErrorName(error: OSError) -> str:
    """The libuv name of an ``OSError``: the Windows error as libuv translates it, else the errno's name."""
    winerror = getattr(error, "winerror", None)
    if sys.platform == "win32" and winerror is not None:
        return UV_WINDOWS_ERROR_NAMES.get(winerror, "UNKNOWN")
    if error.errno is None:
        return "UNKNOWN"
    for name in _PREFERRED_NAMES:
        if getattr(errno, name, None) == error.errno:
            return name
    return errno.errorcode.get(error.errno, "UNKNOWN")


def uvMessage(error: OSError, syscall: str, path: str | None = None, dest: str | None = None) -> str:
    """Node's ``uvException`` message: ``CODE: description, syscall 'path' -> 'dest'``."""
    name = uvErrorName(error)
    message = f"{name}: {UV_ERROR_MESSAGES.get(name, 'unknown error')}, {syscall}"
    if path:
        message += f" '{path}'"
    if dest:
        message += f" -> '{dest}'"
    return message


def withSyscall[T](syscall: str, function: Callable[..., T], *args: Any, path: str | None = None, dest: str | None = None) -> T:
    """Call ``function``; an ``OSError`` it raises carries Node's message for ``syscall`` (``error.uvMessage``)."""
    try:
        return function(*args)
    except OSError as error:
        if getattr(error, "uvMessage", None) is None and _isSystemError(error):
            error.uvMessage = uvMessage(error, syscall, path, dest)  # type: ignore[attr-defined]
        raise


def _isSystemError(error: OSError) -> bool:
    """Whether the system reported ``error`` (it has an errno or a Windows error); another one keeps its own message."""
    return error.errno is not None or getattr(error, "winerror", None) is not None


__all__ = ["UV_ERROR_MESSAGES", "UV_WINDOWS_ERROR_NAMES", "uvErrorName", "uvMessage", "withSyscall"]

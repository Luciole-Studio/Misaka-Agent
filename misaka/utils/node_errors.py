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

UV_WINDOWS_ERROR_NAMES = {
    1: "EISDIR",
    2: "ENOENT",
    3: "ENOENT",
    4: "EMFILE",
    5: "EPERM",
    6: "EBADF",
    8: "ENOMEM",
    13: "EINVAL",
    14: "ENOMEM",
    15: "ENOENT",
    17: "EXDEV",
    19: "EROFS",
    32: "EBUSY",
    33: "EBUSY",
    39: "ENOSPC",
    50: "ENOTSUP",
    80: "EEXIST",
    82: "ENOSPC",
    87: "EINVAL",
    112: "ENOSPC",
    122: "EINVAL",
    123: "ENOENT",
    126: "ENOENT",
    131: "EINVAL",
    145: "ENOTEMPTY",
    161: "ENOENT",
    183: "EEXIST",
    206: "ENAMETOOLONG",
    267: "ENOENT",
    740: "EACCES",
    998: "EFAULT",
    1314: "EPERM",
    1464: "EINVAL",
    1920: "EACCES",
    1921: "ELOOP",
    4392: "ENOENT",
}

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

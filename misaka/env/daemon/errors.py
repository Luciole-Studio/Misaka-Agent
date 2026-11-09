"""Errors as Node reports them: a code such as ``ENOENT`` plus a diagnostic message."""

from __future__ import annotations

from typing import Any


class _Details:
    __slots__ = ("extra", "path", "syscall")

    def __init__(self) -> None:
        self.syscall: str | None = None
        self.path: str | None = None
        # Extra fields for the client, such as `spillPath`.
        self.extra: dict[str, Any] | None = None


class Failure(Exception):
    """A failure with a Node-style code; messages are diagnostic only. MISAKA fork: Rust returns it as the error of a
    ``Result``; here it is raised."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self._details: _Details | None = None

    def details(self) -> _Details:
        if self._details is None:
            self._details = _Details()
        return self._details

    def extra(self, extra: dict[str, Any]) -> Failure:
        """Add fields to the error JSON, such as ``spillPath``."""
        self.details().extra = extra
        return self

    @staticmethod
    def io(error: BaseException, syscall: str, path: str) -> Failure:
        """An I/O error of ``syscall`` on ``path``, with libuv's error name as code."""
        return Failure.io_between(error, syscall, path, None)

    @staticmethod
    def io_between(error: BaseException, syscall: str, path: str, destination: str | None) -> Failure:
        """Node's message for an error of a two-path call: ``CODE: description, syscall 'from' -> 'to'``."""
        from . import system

        code = system.error_name(error)
        description = describe(code)
        if description is None:
            # Unknown to libuv's table: the system's text.
            text = error.strerror if isinstance(error, OSError) and error.strerror else str(error)
            description = text.lower()
        target = f"'{path}' -> '{destination}'" if destination is not None else f"'{path}'"
        failure = Failure(code, f"{code}: {description}, {syscall} {target}")
        failure.details().syscall = syscall
        return failure.path(path)

    def path(self, path: str) -> Failure:
        self.details().path = path
        return self

    def to_json(self) -> dict[str, Any]:
        value: dict[str, Any] = {"code": self.code, "message": self.message}
        details = self._details
        if details is None:
            return value
        if details.syscall is not None:
            value["syscall"] = details.syscall
        if details.path is not None:
            value["path"] = details.path
        if isinstance(details.extra, dict):
            for key, field in details.extra.items():
                value[key] = field
        return value


# libuv's `uv_strerror` texts, which Node uses on every system.
_DESCRIPTIONS = {
    "EACCES": "permission denied",
    "EAGAIN": "resource temporarily unavailable",
    "EBADF": "bad file descriptor",
    "EBUSY": "resource busy or locked",
    "EEXIST": "file already exists",
    "EFBIG": "file too large",
    "EINVAL": "invalid argument",
    "EIO": "i/o error",
    "EISDIR": "illegal operation on a directory",
    "ELOOP": "too many symbolic links encountered",
    "EMFILE": "too many open files",
    "EMLINK": "too many links",
    "ENAMETOOLONG": "name too long",
    "ENFILE": "file table overflow",
    "ENOENT": "no such file or directory",
    "ENOMEM": "not enough memory",
    "ENOSPC": "no space left on device",
    "ENOSYS": "function not implemented",
    "ENOTDIR": "not a directory",
    "ENOTEMPTY": "directory not empty",
    "ENOTSUP": "operation not supported on socket",
    "EPERM": "operation not permitted",
    "EROFS": "read-only file system",
    "ETXTBSY": "text file is busy",
    "EXDEV": "cross-device link not permitted",
}


def describe(code: str) -> str | None:
    """libuv's ``uv_strerror`` texts, which Node uses on every system."""
    return _DESCRIPTIONS.get(code)

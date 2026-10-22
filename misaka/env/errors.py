"""Translated from pi's ``packages/env/src/errors.ts`` (v1.3.0)."""

from __future__ import annotations

from typing import Any

from misaka.durable.env import FileError, Result, err
from misaka.utils.js import errorMessage, jsString

from .connection import RemoteError


def abortResult(signal: Any, path: str | None = None) -> Result | None:
    return err(FileError("aborted", "aborted", path)) if signal is not None and signal.aborted else None


def toFileError(error: Any, fallbackPath: str | None = None) -> FileError:
    """``NodeExecutionEnv``'s mapping of Node error codes to ``FileError`` codes."""
    if isinstance(error, FileError):
        return error
    if not isinstance(error, RemoteError):
        cause = error if isinstance(error, Exception) else Exception(jsString(error))
        return FileError("unknown", errorMessage(cause), fallbackPath, cause)
    path = error.path if error.path is not None else fallbackPath
    code = error.code
    if code == "aborted":
        return FileError("aborted", error.message, path, error)
    if code == "ENOENT":
        return FileError("not_found", error.message, path, error)
    if code in ("EACCES", "EPERM"):
        return FileError("permission_denied", error.message, path, error)
    if code == "ENOTDIR":
        return FileError("not_directory", error.message, path, error)
    if code == "EISDIR":
        return FileError("is_directory", error.message, path, error)
    if code in ("EINVAL", "SYMLINK", "NOT_REGULAR"):
        return FileError("invalid", error.message, path, error)
    return FileError("unknown", error.message, path, error)


__all__ = ["abortResult", "toFileError"]

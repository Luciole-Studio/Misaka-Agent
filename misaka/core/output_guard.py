"""Stdout takeover helpers for print and RPC modes."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, Callable


type _WriteCallback = Callable[[Exception | None], None]
type _WriteMethod = Callable[..., Any]


@dataclass(slots=True)
class _StdoutTakeoverState:
    rawStdoutWrite: _WriteMethod
    rawStderrWrite: _WriteMethod
    originalStdoutWrite: _WriteMethod


_stdoutTakeoverState: _StdoutTakeoverState | None = None


def _invoke_write(write: _WriteMethod, chunk: Any, callback: _WriteCallback | None = None) -> Any:
    try:
        result = write(str(chunk))
    except Exception as error:  # noqa: BLE001
        if callback is not None:
            callback(error if isinstance(error, Exception) else Exception(str(error)))
        raise
    if callback is not None:
        callback(None)
    return result


def takeOverStdout() -> None:
    global _stdoutTakeoverState
    if _stdoutTakeoverState is not None:
        return

    rawStdoutWrite = sys.stdout.write
    rawStderrWrite = sys.stderr.write
    originalStdoutWrite = sys.stdout.write

    def redirected_write(
        chunk: str | bytes,
        encodingOrCallback: Any = None,
        callback: _WriteCallback | None = None,
    ) -> Any:
        if callable(encodingOrCallback):
            return _invoke_write(rawStderrWrite, chunk, encodingOrCallback)
        return _invoke_write(rawStderrWrite, chunk, callback)

    sys.stdout.write = redirected_write  # type: ignore[method-assign]

    _stdoutTakeoverState = _StdoutTakeoverState(
        rawStdoutWrite=rawStdoutWrite,
        rawStderrWrite=rawStderrWrite,
        originalStdoutWrite=originalStdoutWrite,
    )


def restoreStdout() -> None:
    global _stdoutTakeoverState
    if _stdoutTakeoverState is None:
        return

    sys.stdout.write = _stdoutTakeoverState.originalStdoutWrite  # type: ignore[method-assign]
    _stdoutTakeoverState = None


def isStdoutTakenOver() -> bool:
    return _stdoutTakeoverState is not None


def writeRawStdout(text: str) -> None:
    if _stdoutTakeoverState is not None:
        _stdoutTakeoverState.rawStdoutWrite(text)
        return
    sys.stdout.write(text)


async def flushRawStdout() -> None:
    write = _stdoutTakeoverState.rawStdoutWrite if _stdoutTakeoverState is not None else sys.stdout.write
    write("")
    stream = getattr(write, "__self__", None)
    flush = getattr(stream, "flush", None)
    if callable(flush):
        flush()


__all__ = [
    "flushRawStdout",
    "isStdoutTakenOver",
    "restoreStdout",
    "takeOverStdout",
    "writeRawStdout",
]

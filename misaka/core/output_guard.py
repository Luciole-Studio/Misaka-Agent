"""Stdout takeover helpers for print and RPC modes."""

from __future__ import annotations

import errno
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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
    except Exception as error:
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


# pi output-guard.ts:9-43 — chunks are written strictly in order, each one acknowledged before the
# next, and a transient kernel-buffer refusal is retried every 10ms instead of failing the run.
RAW_STDOUT_RETRY_DELAY_S = 0.01
_RETRYABLE_ERRNOS = {errno.EAGAIN, errno.EWOULDBLOCK, errno.ENOBUFS}


def _rawStdoutWrite() -> _WriteMethod:
    return _stdoutTakeoverState.rawStdoutWrite if _stdoutTakeoverState is not None else sys.stdout.write


def _retry_transient(operation: Callable[[], Any]) -> None:
    while True:
        try:
            operation()
            return
        except OSError as error:
            if error.errno not in _RETRYABLE_ERRNOS:
                raise
            time.sleep(RAW_STDOUT_RETRY_DELAY_S)


def _write_raw_stdout_chunk(text: str) -> None:
    write = _rawStdoutWrite()
    stream = getattr(write, "__self__", None)
    buffer = getattr(stream, "buffer", None)
    if buffer is None:                        # StringIO-style stand-ins: no binary layer, so no EAGAIN either
        write(text)
        if callable(getattr(stream, "flush", None)):
            stream.flush()
        return
    pending = [text.encode(getattr(stream, "encoding", None) or "utf-8", getattr(stream, "errors", None) or "strict")]

    def write_pending() -> None:
        while pending[0]:
            try:
                written = buffer.write(pending[0])
            except BlockingIOError as error:  # the part that went through must not be sent twice
                pending[0] = pending[0][error.characters_written:]
                raise
            if written is None:               # unbuffered FileIO signals EAGAIN with None, not an exception
                raise BlockingIOError(errno.EAGAIN, "raw stdout would block")
            pending[0] = pending[0][written:]  # a BufferedWriter takes all; a raw FileIO may take part

    _retry_transient(stream.flush)            # text the TextIO still holds goes out first, so order is kept
    _retry_transient(write_pending)
    _retry_transient(buffer.flush)


def writeRawStdout(text: str) -> None:
    if not text:
        return
    _write_raw_stdout_chunk(text)


async def waitForRawStdoutBackpressure() -> None:
    """Every chunk is pushed through synchronously, so the queue is always drained by the time a
    caller awaits this; print mode still awaits it between agent events (pi print-mode.ts:113-118)."""
    return


async def flushRawStdout() -> None:
    await waitForRawStdoutBackpressure()
    _write_raw_stdout_chunk("")


__all__ = [
    "flushRawStdout",
    "isStdoutTakenOver",
    "restoreStdout",
    "takeOverStdout",
    "waitForRawStdoutBackpressure",
    "writeRawStdout",
]

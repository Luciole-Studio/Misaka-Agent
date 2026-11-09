"""Translated from pi's ``packages/coding-agent/src/core/output-guard.ts`` (v1.1.0).

MISAKA fork: a chunk is written synchronously, retried in place on a transient refusal, so there is no promise chain
(``rawStdoutWriteTail``) to wait on; a write that fails for good raises to the caller, which ``misaka.__main__`` turns
into a quiet exit, where pi calls ``process.exit(1)``.
"""

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
class StdoutTakeoverState:
    rawStdoutWrite: _WriteMethod
    rawStderrWrite: _WriteMethod
    originalStdoutWrite: _WriteMethod


stdoutTakeoverState: StdoutTakeoverState | None = None


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


RAW_STDOUT_RETRY_DELAY_MS = 10
_RETRYABLE_ERRNOS = {errno.EAGAIN, errno.EWOULDBLOCK, errno.ENOBUFS}


def getRawStdoutWrite() -> _WriteMethod:
    if stdoutTakeoverState is not None:
        return stdoutTakeoverState.rawStdoutWrite
    return sys.stdout.write


def _retry_transient(operation: Callable[[], Any]) -> None:
    while True:
        try:
            operation()
            return
        except OSError as error:
            if error.errno not in _RETRYABLE_ERRNOS:
                raise
            time.sleep(RAW_STDOUT_RETRY_DELAY_MS / 1000)


def writeRawStdoutChunk(text: str) -> None:
    write = getRawStdoutWrite()
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


def takeOverStdout() -> None:
    global stdoutTakeoverState
    if stdoutTakeoverState is not None:
        return

    rawStdoutWrite = sys.stdout.write
    rawStderrWrite = sys.stderr.write
    originalStdoutWrite = sys.stdout.write

    def redirectedWrite(
        chunk: str | bytes,
        encodingOrCallback: Any = None,
        callback: _WriteCallback | None = None,
    ) -> Any:
        if callable(encodingOrCallback):
            return _invoke_write(rawStderrWrite, chunk, encodingOrCallback)
        return _invoke_write(rawStderrWrite, chunk, callback)

    sys.stdout.write = redirectedWrite  # type: ignore[method-assign]

    stdoutTakeoverState = StdoutTakeoverState(
        rawStdoutWrite=rawStdoutWrite,
        rawStderrWrite=rawStderrWrite,
        originalStdoutWrite=originalStdoutWrite,
    )


def restoreStdout() -> None:
    global stdoutTakeoverState
    if stdoutTakeoverState is None:
        return

    sys.stdout.write = stdoutTakeoverState.originalStdoutWrite  # type: ignore[method-assign]
    stdoutTakeoverState = None


def isStdoutTakenOver() -> bool:
    return stdoutTakeoverState is not None


def writeRawStdout(text: str) -> None:
    if not text:
        return
    writeRawStdoutChunk(text)


async def waitForRawStdoutBackpressure() -> None:
    # MISAKA fork: every chunk is pushed through synchronously, so nothing is pending by the time a caller awaits this.
    return


async def flushRawStdout() -> None:
    await waitForRawStdoutBackpressure()
    writeRawStdoutChunk("")


__all__ = [
    "flushRawStdout",
    "isStdoutTakenOver",
    "restoreStdout",
    "takeOverStdout",
    "waitForRawStdoutBackpressure",
    "writeRawStdout",
]

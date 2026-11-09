"""Bash command execution with streaming support and cancellation.

This module provides a unified bash execution implementation used by:
- AgentSession.executeBash() for interactive mode
- Direct calls from modes that need bash execution

Translated from pi's ``packages/coding-agent/src/core/bash-executor.ts`` (v1.1.0).
"""

from __future__ import annotations

import codecs
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, BinaryIO, TypedDict

from misaka.core.tools.bash import BashOperations
from misaka.core.tools.truncate import DEFAULT_MAX_BYTES, truncateTail
from misaka.utils.ansi import splitIncompleteAnsiSuffix, stripAnsi
from misaka.utils.js import jsLength
from misaka.utils.output_files import createOutputFileStream
from misaka.utils.shell import sanitizeBinaryOutput
from misaka.utils.values import read_field, signal_aborted

# ============================================================================
# Types
# ============================================================================


class BashExecutorOptions(TypedDict, total=False):
    # Callback for streaming output chunks (already sanitized)
    onChunk: Callable[[str], None]
    # AbortSignal for cancellation
    signal: Any


@dataclass(slots=True)
class BashResult:
    # Combined stdout + stderr output (sanitized, possibly truncated)
    output: str
    # Process exit code (None if killed/cancelled)
    exitCode: int | None
    # Whether the command was cancelled via signal
    cancelled: bool
    # Whether the output was truncated
    truncated: bool
    # Path to temp file containing full output (if output exceeded truncation threshold)
    fullOutputPath: str | None = None


# ============================================================================
# Implementation
# ============================================================================


async def executeBashWithOperations(
    command: str,
    cwd: str,
    operations: BashOperations,
    options: BashExecutorOptions | Mapping[str, Any] | None = None,
) -> BashResult:
    """Execute a bash command using custom BashOperations.
    Used for remote execution (SSH, containers, etc.).
    """
    outputChunks: list[str] = []
    outputBytes = 0
    maxOutputBytes = DEFAULT_MAX_BYTES * 2

    tempFilePath: str | None = None
    tempFileStream: BinaryIO | None = None
    totalBytes = 0
    signal = read_field(options, "signal")

    def ensureTempFile() -> None:
        nonlocal tempFilePath, tempFileStream
        if tempFilePath:
            return
        # MISAKA fork: output files are named after misaka (upstream: `pi-bash`); the stream is binary, so text is
        # written as UTF-8.
        tempFilePath, tempFileStream = createOutputFileStream("misaka-bash", ".log")
        for chunk in outputChunks:
            tempFileStream.write(chunk.encode("utf-8"))

    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    # Unfinished escape sequence at the end of the previous chunk, completed by the next chunk.
    pendingAnsi = ""

    def appendText(rawText: str) -> None:
        nonlocal outputBytes
        # Sanitize: strip ANSI, replace binary garbage, normalize newlines
        text = sanitizeBinaryOutput(stripAnsi(rawText)).replace("\r", "")
        if not text:
            return

        # Start writing to temp file if exceeds threshold
        if totalBytes > DEFAULT_MAX_BYTES:
            ensureTempFile()

        if tempFileStream is not None:
            tempFileStream.write(text.encode("utf-8"))

        # Keep rolling buffer
        outputChunks.append(text)
        outputBytes += jsLength(text)
        while outputBytes > maxOutputBytes and len(outputChunks) > 1:
            removed = outputChunks.pop(0)
            outputBytes -= jsLength(removed)

        # Stream to callback
        onChunk = read_field(options, "onChunk")
        if onChunk:
            onChunk(text)

    def onData(data: bytes) -> None:
        nonlocal totalBytes, pendingAnsi
        totalBytes += len(data)
        complete, pending = splitIncompleteAnsiSuffix(pendingAnsi + decoder.decode(data, final=False))
        pendingAnsi = pending
        appendText(complete)

    def flushOutput() -> None:
        nonlocal pendingAnsi
        rest = pendingAnsi + decoder.decode(b"", final=True)
        pendingAnsi = ""
        appendText(rest)

    def endTempFile() -> None:
        if tempFileStream is not None and not tempFileStream.closed:
            tempFileStream.close()

    exitCode: int | None = None
    try:
        try:
            exitCode = read_field(await operations.exec(command, cwd, {"onData": onData, "signal": signal}), "exitCode")
        except Exception:
            # An aborted command still returns the output it produced so far
            if not signal_aborted(signal):
                endTempFile()
                raise

        flushOutput()
        fullOutput = "".join(outputChunks)
        truncationResult = truncateTail(fullOutput)
        if truncationResult.truncated:
            ensureTempFile()
        endTempFile()
        cancelled = signal_aborted(signal)

        return BashResult(
            output=truncationResult.content if truncationResult.truncated else fullOutput,
            exitCode=None if cancelled else exitCode,
            cancelled=cancelled,
            truncated=truncationResult.truncated,
            fullOutputPath=tempFilePath,
        )
    finally:
        # MISAKA fork: the stream is also closed when the task is cancelled (a Node stream would stay open).
        endTempFile()


__all__ = ["BashExecutorOptions", "BashResult", "executeBashWithOperations"]

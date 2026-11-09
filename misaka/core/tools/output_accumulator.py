"""Incremental output accumulation with bounded memory usage."""

from __future__ import annotations

import asyncio
import codecs
import os
from dataclasses import dataclass

from misaka.core.tools.truncate import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_LINES,
    TruncationOptions,
    TruncationResult,
    truncateTail,
)
from misaka.utils.output_files import createOutputFileStream


@dataclass(slots=True)
class OutputAccumulatorOptions:
    maxLines: int | None = None
    maxBytes: int | None = None
    tempFilePrefix: str | None = None


@dataclass(slots=True)
class OutputSnapshot:
    content: str
    truncation: TruncationResult
    fullOutputPath: str | None = None


def byteLength(text: str) -> int:
    return len(text.encode("utf-8"))


@dataclass(slots=True)
class FullOutput:
    content: str
    # Whether `content` omits part of the output.
    truncated: bool


class OutputAccumulator:
    def __init__(self, options: OutputAccumulatorOptions | None = None) -> None:
        resolved = options or OutputAccumulatorOptions()
        self.maxLines = resolved.maxLines if resolved.maxLines is not None else DEFAULT_MAX_LINES
        self.maxBytes = resolved.maxBytes if resolved.maxBytes is not None else DEFAULT_MAX_BYTES
        self.maxRollingBytes = max(self.maxBytes * 2, 1)
        # MISAKA fork: output files are named after misaka (upstream: `pi-output`).
        self.tempFilePrefix = resolved.tempFilePrefix or "misaka-output"
        # errors="replace" like core/bash_executor.py: a command that emits non-UTF-8 bytes
        # (grep on a binary, cat a PNG) must cost the offending bytes, not the whole output —
        # a strict decoder raises inside the forwarding task and every captured byte is lost.
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

        self.rawChunks: list[bytes] = []
        self.tailText = ""
        self.tailBytes = 0
        self.tailStartsAtLineBoundary = True
        self.totalRawBytes = 0
        self.totalDecodedBytes = 0
        self.completedLines = 0
        self.totalLines = 0
        self.currentLineBytes = 0
        self.hasOpenLine = False
        self.finished = False

        self.tempFilePath: str | None = None
        self.tempFileHandle: object | None = None

    def append(self, data: bytes) -> None:
        if self.finished:
            raise RuntimeError("Cannot append to a finished output accumulator")

        self.totalRawBytes += len(data)
        self.appendDecodedText(self.decoder.decode(data, final=False))

        if self.tempFileHandle or self.shouldUseTempFile():
            self.ensureTempFile()
            assert self.tempFileHandle is not None
            self.tempFileHandle.write(data)
            self.tempFileHandle.flush()
        elif data:
            self.rawChunks.append(bytes(data))

    def finish(self) -> None:
        if self.finished:
            return
        self.finished = True
        self.appendDecodedText(self.decoder.decode(b"", final=True))
        if self.shouldUseTempFile():
            self.ensureTempFile()

    def snapshot(self, *, persistIfTruncated: bool = False) -> OutputSnapshot:
        tail_truncation = truncateTail(
            self.getSnapshotText(),
            TruncationOptions(maxLines=self.maxLines, maxBytes=self.maxBytes),
        )
        truncated = self.totalLines > self.maxLines or self.totalDecodedBytes > self.maxBytes
        truncated_by = tail_truncation.truncatedBy if truncated else None
        if truncated and truncated_by is None:
            truncated_by = "bytes" if self.totalDecodedBytes > self.maxBytes else "lines"
        truncation = TruncationResult(
            content=tail_truncation.content,
            truncated=truncated,
            truncatedBy=truncated_by,
            totalLines=self.totalLines,
            totalBytes=self.totalDecodedBytes,
            outputLines=tail_truncation.outputLines,
            outputBytes=tail_truncation.outputBytes,
            lastLinePartial=tail_truncation.lastLinePartial,
            firstLineExceedsLimit=tail_truncation.firstLineExceedsLimit,
            maxLines=self.maxLines,
            maxBytes=self.maxBytes,
        )

        if persistIfTruncated and truncation.truncated:
            self.ensureTempFile()

        return OutputSnapshot(content=truncation.content, truncation=truncation, fullOutputPath=self.tempFilePath)

    async def closeTempFile(self) -> None:
        if self.tempFileHandle is None:
            return
        handle = self.tempFileHandle
        self.tempFileHandle = None
        await asyncio.to_thread(handle.close)

    # Kept for misaka/core/subagent, which calls this name.
    close_temp_file = closeTempFile

    async def readFullOutput(self, max_bytes: int) -> FullOutput:
        """The complete output, for callers that can take more than the display snapshot. Call
        after ``finish()`` and ``closeTempFile()``. Output longer than ``max_bytes`` raw bytes
        keeps its first and last ``max_bytes / 2`` bytes around an omission marker."""
        if self.tempFilePath is None:
            return FullOutput(content=b"".join(self.rawChunks).decode("utf-8", errors="replace"), truncated=False)
        return await asyncio.to_thread(self._readFullOutputFile, self.tempFilePath, max_bytes)

    @staticmethod
    def _readFullOutputFile(path: str, max_bytes: int) -> FullOutput:
        with open(path, "rb") as file:
            size = os.fstat(file.fileno()).st_size
            if size <= max_bytes:
                return FullOutput(content=file.read().decode("utf-8", errors="replace"), truncated=False)
            head_bytes = max_bytes // 2
            tail_bytes = max_bytes - head_bytes
            head = file.read(head_bytes)
            file.seek(size - tail_bytes)
            tail = file.read(tail_bytes)
        # Cut at character boundaries: streaming decode holds back an incomplete trailing sequence,
        # and the tail skips leading continuation bytes.
        head_text = codecs.getincrementaldecoder("utf-8")(errors="replace").decode(head, final=False)
        tail_start = 0
        while tail_start < len(tail) and (tail[tail_start] & 0xC0) == 0x80:
            tail_start += 1
        tail_text = tail[tail_start:].decode("utf-8", errors="replace")
        omitted = size - head_bytes - tail_bytes
        return FullOutput(content=f"{head_text}\n\n[... {omitted} bytes omitted ...]\n\n{tail_text}", truncated=True)

    def getLastLineBytes(self) -> int:
        return self.currentLineBytes

    def appendDecodedText(self, text: str) -> None:
        if not text:
            return

        bytes_count = byteLength(text)
        self.totalDecodedBytes += bytes_count
        self.tailText += text
        self.tailBytes += bytes_count
        if self.tailBytes > self.maxRollingBytes * 2:
            self.trimTail()

        newlines = text.count("\n")
        if newlines == 0:
            self.currentLineBytes += bytes_count
            self.hasOpenLine = True
        else:
            self.completedLines += newlines
            tail = text.split("\n")[-1]
            self.currentLineBytes = byteLength(tail)
            self.hasOpenLine = bool(tail)
        self.totalLines = self.completedLines + (1 if self.hasOpenLine else 0)

    def trimTail(self) -> None:
        buffer = self.tailText.encode("utf-8")
        if len(buffer) <= self.maxRollingBytes:
            self.tailBytes = len(buffer)
            return

        start = len(buffer) - self.maxRollingBytes
        while start < len(buffer) and (buffer[start] & 0xC0) == 0x80:
            start += 1

        # start is at least 1 here: the whole-buffer case returned above.
        self.tailStartsAtLineBoundary = buffer[start - 1] == 0x0A
        self.tailText = buffer[start:].decode("utf-8")
        self.tailBytes = byteLength(self.tailText)

    def getSnapshotText(self) -> str:
        if self.tailStartsAtLineBoundary:
            return self.tailText
        first_newline = self.tailText.find("\n")
        return self.tailText if first_newline == -1 else self.tailText[first_newline + 1 :]

    def shouldUseTempFile(self) -> bool:
        return (
            self.totalRawBytes > self.maxBytes
            or self.totalDecodedBytes > self.maxBytes
            or self.totalLines > self.maxLines
        )

    def ensureTempFile(self) -> None:
        if self.tempFilePath is not None:
            return
        self.tempFilePath, self.tempFileHandle = createOutputFileStream(self.tempFilePrefix, ".log")
        for chunk in self.rawChunks:
            self.tempFileHandle.write(chunk)
        self.tempFileHandle.flush()
        self.rawChunks = []

    # Kept for misaka/core/subagent, which calls this name.
    ensure_temp_file = ensureTempFile


__all__ = [
    "FullOutput",
    "OutputAccumulator",
    "OutputAccumulatorOptions",
    "OutputSnapshot",
]

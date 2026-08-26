"""Incremental output accumulation with bounded memory usage."""

from __future__ import annotations

import asyncio
import codecs
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path

from misaka.core.tools.truncate import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_LINES,
    TruncationOptions,
    TruncationResult,
    truncate_tail,
)


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


def default_temp_file_path(prefix: str) -> str:
    return str(Path(tempfile.gettempdir()) / f"{prefix}-{secrets.token_hex(8)}.log")


def byte_length(text: str) -> int:
    return len(text.encode("utf-8"))


class OutputAccumulator:
    def __init__(self, options: OutputAccumulatorOptions | None = None) -> None:
        resolved = options or OutputAccumulatorOptions()
        self.maxLines = resolved.maxLines if resolved.maxLines is not None else DEFAULT_MAX_LINES
        self.maxBytes = resolved.maxBytes if resolved.maxBytes is not None else DEFAULT_MAX_BYTES
        self.maxRollingBytes = max(self.maxBytes * 2, 1)
        self.tempFilePrefix = resolved.tempFilePrefix or "misaka-output"
        self.decoder = codecs.getincrementaldecoder("utf-8")()

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
        self.append_decoded_text(self.decoder.decode(data, final=False))

        if self.tempFileHandle or self.should_use_temp_file():
            self.ensure_temp_file()
            assert self.tempFileHandle is not None
            self.tempFileHandle.write(data)
            self.tempFileHandle.flush()
        elif data:
            self.rawChunks.append(bytes(data))

    def finish(self) -> None:
        if self.finished:
            return
        self.finished = True
        self.append_decoded_text(self.decoder.decode(b"", final=True))
        if self.should_use_temp_file():
            self.ensure_temp_file()

    def snapshot(self, *, persistIfTruncated: bool = False) -> OutputSnapshot:
        tail_truncation = truncate_tail(
            self.get_snapshot_text(),
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
            self.ensure_temp_file()

        return OutputSnapshot(content=truncation.content, truncation=truncation, fullOutputPath=self.tempFilePath)

    async def close_temp_file(self) -> None:
        if self.tempFileHandle is None:
            return
        handle = self.tempFileHandle
        self.tempFileHandle = None
        await asyncio.to_thread(handle.close)

    def get_last_line_bytes(self) -> int:
        return self.currentLineBytes

    def append_decoded_text(self, text: str) -> None:
        if not text:
            return

        bytes_count = byte_length(text)
        self.totalDecodedBytes += bytes_count
        self.tailText += text
        self.tailBytes += bytes_count
        if self.tailBytes > self.maxRollingBytes * 2:
            self.trim_tail()

        newlines = text.count("\n")
        if newlines == 0:
            self.currentLineBytes += bytes_count
            self.hasOpenLine = True
        else:
            self.completedLines += newlines
            tail = text.split("\n")[-1]
            self.currentLineBytes = byte_length(tail)
            self.hasOpenLine = bool(tail)
        self.totalLines = self.completedLines + (1 if self.hasOpenLine else 0)

    def trim_tail(self) -> None:
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
        self.tailBytes = byte_length(self.tailText)

    def get_snapshot_text(self) -> str:
        if self.tailStartsAtLineBoundary:
            return self.tailText
        first_newline = self.tailText.find("\n")
        return self.tailText if first_newline == -1 else self.tailText[first_newline + 1 :]

    def should_use_temp_file(self) -> bool:
        return (
            self.totalRawBytes > self.maxBytes
            or self.totalDecodedBytes > self.maxBytes
            or self.totalLines > self.maxLines
        )

    def ensure_temp_file(self) -> None:
        if self.tempFilePath is not None:
            return
        self.tempFilePath = default_temp_file_path(self.tempFilePrefix)
        self.tempFileHandle = open(self.tempFilePath, "wb")  # noqa: SIM115 - the accumulator owns it and closes it in finalize
        for chunk in self.rawChunks:
            self.tempFileHandle.write(chunk)
        self.tempFileHandle.flush()
        self.rawChunks = []


defaultTempFilePath = default_temp_file_path
byteLength = byte_length

__all__ = [
    "OutputAccumulator",
    "OutputAccumulatorOptions",
    "OutputSnapshot",
    "byteLength",
    "defaultTempFilePath",
]

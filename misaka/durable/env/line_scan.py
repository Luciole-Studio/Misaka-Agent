"""Ported from: pi packages/durable/src/env/line-scan.ts @ v1.3.0"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from misaka.utils.js import RangeError, Utf8Decoder, jsIsSafeInteger

from .decode import rangeDecoder, startsWithBom

if TYPE_CHECKING:
    from . import LineScan

NEWLINE = 0x0A


def _decodedBytes(text: str) -> int:
    """UTF-8 byte length of decoded text."""
    return 0 if text == "" else len(text.encode("utf-8"))


class LineScanner:
    """Computes a ``LineScan`` from a file's bytes fed in order, so an environment can scan any size in bounded memory.
    Decoded sizes use the same streaming WHATWG decoder as decoding the whole file at once."""

    def __init__(self, startLine: float, endLine: float = math.inf) -> None:
        """``startLine`` and ``endLine`` must be non-negative integers with ``endLine > startLine``; ``endLine`` absent:
        to the end."""
        if not jsIsSafeInteger(startLine) or startLine < 0 or not (endLine > startLine):
            raise RangeError("Invalid line range")
        if endLine != math.inf and not jsIsSafeInteger(endLine):
            raise RangeError("Invalid line range")
        self._startLine = startLine
        self._endLine = endLine
        self._position = 0
        self._newlines = 0
        self._lineStart = 0
        self._start: int | None = None
        self._end: int | None = None
        self._firstLineEnd: int | None = None
        self._lastLineStart: int | None = None
        self._selectedBytes = 0
        self._firstLineBytes = 0
        self._selection: Utf8Decoder | None = None
        self._firstLine: Utf8Decoder | None = None
        # The first bytes, held until it is known whether they are a byte-order mark.
        self._head: bytearray | None = bytearray()
        self._bom = False
        if startLine == 0:
            self._begin(0)

    def push(self, chunk: bytes) -> None:
        if self._head is not None:
            take = min(3 - len(self._head), len(chunk))
            self._head += chunk[:take]
            if len(self._head) < 3:
                return
            self._releaseHead()
            chunk = chunk[take:]
        self._process(chunk)

    def _releaseHead(self) -> None:
        head = bytes(self._head or b"")
        self._head = None
        self._bom = startsWithBom(head)
        self._process(head)

    def _process(self, chunk: bytes) -> None:
        base = self._position
        from_ = 0
        index = chunk.find(NEWLINE)
        while index != -1:
            # The newline ends line `self._newlines`. It belongs to the selection between selected lines only.
            self._feed(chunk, base, from_, index)
            line = self._newlines
            position = base + index
            if line == self._startLine:
                self._endFirstLine(position)
            if line == self._endLine - 1:
                self._endSelection(position)
            self._feed(chunk, base, index, index + 1)
            from_ = index + 1
            self._newlines += 1
            self._lineStart = position + 1
            if self._newlines == self._startLine:
                self._begin(self._lineStart)
            if self._newlines == self._endLine - 1:
                self._lastLineStart = self._lineStart
            index = chunk.find(NEWLINE, index + 1)
        self._feed(chunk, base, from_, len(chunk))
        self._position += len(chunk)

    def finish(self) -> LineScan:
        if self._head is not None:
            self._releaseHead()
        size = self._position
        if self._start is None:
            return {
                "newlines": self._newlines,
                "start": size,
                "end": size,
                "firstLineEnd": size,
                "lastLineStart": size,
                "selectedBytes": 0,
                "firstLineBytes": 0,
            }
        if self._firstLineEnd is None:
            self._endFirstLine(size)
        if self._end is None:
            self._endSelection(size)
        return {
            "newlines": self._newlines,
            "start": self._start,
            "end": self._end if self._end is not None else size,
            "firstLineEnd": self._firstLineEnd if self._firstLineEnd is not None else size,
            # A selection that reaches past the last line ends with the last line.
            "lastLineStart": self._lastLineStart if self._lastLineStart is not None else self._lineStart,
            "selectedBytes": self._selectedBytes,
            "firstLineBytes": self._firstLineBytes,
        }

    def _begin(self, start: int) -> None:
        self._start = start
        if self._startLine == self._endLine - 1:
            self._lastLineStart = start
        self._selection = rangeDecoder()
        self._firstLine = rangeDecoder()

    def _endFirstLine(self, position: int) -> None:
        self._firstLineEnd = position
        if self._firstLine is not None:
            self._firstLineBytes += _decodedBytes(self._firstLine.decode(b"", final=True))
        self._firstLine = None

    def _endSelection(self, position: int) -> None:
        self._end = position
        if self._selection is not None:
            self._selectedBytes += _decodedBytes(self._selection.decode(b"", final=True))
        self._selection = None

    def _feed(self, chunk: bytes, base: int, from_: int, to: int) -> None:
        """Feed ``chunk[from_, to)``, which starts at file offset ``base``, to the decoders of the ranges still open."""
        # Decoding the whole file drops a leading byte-order mark.
        if self._bom and base + from_ < 3:
            from_ = min(to, 3 - base)
        if to <= from_:
            return
        bytes_ = chunk[from_:to]
        if self._selection is not None:
            self._selectedBytes += _decodedBytes(self._selection.decode(bytes_))
        if self._firstLine is not None:
            self._firstLineBytes += _decodedBytes(self._firstLine.decode(bytes_))


__all__ = ["NEWLINE", "LineScanner"]

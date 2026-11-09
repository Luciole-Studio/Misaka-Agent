"""Port of Durable's ``LineScanner`` (``env/line-scan.ts``): locates lines ``[start_line, end_line)`` of a file fed in order
and measures their decoded UTF-8 size as ``new TextDecoder().decode(file)`` would produce it."""

from __future__ import annotations

from typing import Any

from .decode import Decoder, decode_into, range_decoder, starts_with_bom

NEWLINE = 0x0A


def _utf8_length(text: str) -> int:
    return len(text.encode("utf-8"))


class LineScanner:
    def __init__(self, start_line: int, end_line: int | None) -> None:
        """``end_line``, when given, must be greater than ``start_line``."""
        self.start_line = start_line
        self.end_line = end_line
        self.position = 0
        self.newlines = 0
        self.line_start = 0
        self.start: int | None = None
        self.end: int | None = None
        self.first_line_end: int | None = None
        self.last_line_start: int | None = None
        self.selected_bytes = 0
        self.first_line_bytes = 0
        self.selection: Decoder | None = None
        self.first_line: Decoder | None = None
        self.head: bytearray | None = bytearray()
        self.bom = False
        if start_line == 0:
            self.begin(0)

    def is_last_line(self, line: int) -> bool:
        return self.end_line is not None and line + 1 == self.end_line

    def push(self, chunk: bytes) -> None:
        if self.head is not None:
            take = min(3 - len(self.head), len(chunk))
            self.head += chunk[:take]
            if len(self.head) < 3:
                return
            chunk = chunk[take:]
            self.release_head()
        self.process(chunk)

    def release_head(self) -> None:
        head = bytes(self.head or b"")
        self.head = None
        self.bom = starts_with_bom(head)
        self.process(head)

    def process(self, chunk: bytes) -> None:
        base = self.position
        start = 0
        newline = chunk.find(NEWLINE)
        while newline != -1:
            # The newline ends line `self.newlines`; it belongs to the selection between selected lines only.
            self.feed(chunk, base, start, newline)
            line = self.newlines
            position = base + newline
            if line == self.start_line:
                self.end_first_line(position)
            if self.is_last_line(line):
                self.end_selection(position)
            self.feed(chunk, base, newline, newline + 1)
            start = newline + 1
            self.newlines += 1
            self.line_start = position + 1
            if self.newlines == self.start_line:
                self.begin(self.line_start)
            if self.is_last_line(self.newlines):
                self.last_line_start = self.line_start
            newline = chunk.find(NEWLINE, newline + 1)
        self.feed(chunk, base, start, len(chunk))
        self.position += len(chunk)

    def finish(self) -> dict[str, Any]:
        if self.head is not None:
            self.release_head()
        size = self.position
        start = self.start
        if start is None:
            return {
                "newlines": self.newlines,
                "start": size,
                "end": size,
                "firstLineEnd": size,
                "lastLineStart": size,
                "selectedBytes": 0,
                "firstLineBytes": 0,
            }
        if self.first_line_end is None:
            self.end_first_line(size)
        if self.end is None:
            self.end_selection(size)
        return {
            "newlines": self.newlines,
            "start": start,
            "end": self.end if self.end is not None else size,
            "firstLineEnd": self.first_line_end if self.first_line_end is not None else size,
            # A selection that reaches past the last line ends with the last line.
            "lastLineStart": self.last_line_start if self.last_line_start is not None else self.line_start,
            "selectedBytes": self.selected_bytes,
            "firstLineBytes": self.first_line_bytes,
        }

    def begin(self, start: int) -> None:
        self.start = start
        if self.is_last_line(self.start_line):
            self.last_line_start = start
        self.selection = range_decoder()
        self.first_line = range_decoder()

    def end_first_line(self, position: int) -> None:
        self.first_line_end = position
        decoder, self.first_line = self.first_line, None
        if decoder is not None:
            self.first_line_bytes += _utf8_length(decode_into(decoder, b"", True))

    def end_selection(self, position: int) -> None:
        self.end = position
        decoder, self.selection = self.selection, None
        if decoder is not None:
            self.selected_bytes += _utf8_length(decode_into(decoder, b"", True))

    def feed(self, chunk: bytes, base: int, start: int, to: int) -> None:
        # Decoding the whole file drops a leading byte-order mark.
        if self.bom and base + start < 3:
            start = min(to, 3 - base)
        if to <= start:
            return
        data = chunk[start:to]
        if self.selection is not None:
            self.selected_bytes += _utf8_length(decode_into(self.selection, data, False))
        if self.first_line is not None:
            self.first_line_bytes += _utf8_length(decode_into(self.first_line, data, False))

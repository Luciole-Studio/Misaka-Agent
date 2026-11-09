"""Durable's output window (``ShellExecOptions.window``, ``ShellOutputInfo.skipped``): undelivered command output is held
and coalesced; text followed by more than the window (by a byte or a line) can never be in the caller's tail and is
replaced by counts. Delivery follows the caller's commit pace, and waits while an earlier frame is still unsent.

Text is held as UTF-8 bytes, so lengths are byte lengths as in Rust."""

from __future__ import annotations

import time
from typing import Any

from .frame import as_f64, as_u64, get


class Window:
    __slots__ = ("bytes_per_second", "max_bytes", "max_lines", "min_interval")

    def __init__(self, max_bytes: int, max_lines: int, min_interval: float, bytes_per_second: float) -> None:
        self.max_bytes = max_bytes
        self.max_lines = max_lines
        # Seconds.
        self.min_interval = min_interval
        self.bytes_per_second = bytes_per_second

    @staticmethod
    def from_json(json: Any) -> Window | None:
        if not isinstance(json, dict):
            return None
        max_bytes = as_u64(get(json, "maxBytes"))
        max_lines = as_u64(get(json, "maxLines"))
        min_interval_ms = as_f64(get(json, "minIntervalMs"))
        bytes_per_second = as_f64(get(json, "bytesPerSecond"))
        if max_bytes is None or max_lines is None or min_interval_ms is None or bytes_per_second is None:
            return None
        return Window(max_bytes, max_lines, max(min_interval_ms, 0.0) / 1000, max(bytes_per_second, 1.0))


class Skipped:
    __slots__ = ("bytes", "ends_with_newline", "newlines")

    def __init__(self) -> None:
        self.bytes = 0
        self.newlines = 0
        self.ends_with_newline = False


class Event:
    """One output event: its stream, text, and the output omitted right before it."""

    __slots__ = ("skipped", "stream", "text")

    def __init__(self, stream: str, text: bytes, skipped: dict[str, Any] | None) -> None:
        self.stream = stream
        self.text = text
        self.skipped = skipped


def newline_count(text: bytes | bytearray) -> int:
    return text.count(b"\n")


def _is_char_boundary(text: bytes | bytearray, index: int) -> bool:
    return index == 0 or index >= len(text) or (text[index] & 0xC0) != 0x80


class Pending:
    def __init__(self, window: Window) -> None:
        self.window = window
        # Undelivered text in arrival order, by stream.
        self.segments: list[tuple[str, bytearray]] = []
        self.bytes = 0
        self.newlines = 0
        self.skipped: Skipped | None = None
        self._next_send = time.monotonic()

    def is_empty(self) -> bool:
        return not self.segments

    def next_send(self) -> float:
        """When the next delivery may happen (``time.monotonic()``)."""
        return self._next_send

    def push(self, stream: str, text: bytes) -> None:
        if not text:
            return
        self.bytes += len(text)
        self.newlines += newline_count(text)
        if self.segments and self.segments[-1][0] == stream:
            self.segments[-1][1].extend(text)
        else:
            self.segments.append((stream, bytearray(text)))
        # Trimming costs a pass over the held text; do it once a few windows have accumulated.
        if self.bytes > 4 * (self.window.max_bytes + 1) or self.newlines > 4 * (self.window.max_lines + 1):
            self.trim()

    def proves(self, bytes: int, newlines: int) -> bool:
        """Whether the text after a cut still proves the cut text is outside the window."""
        return bytes > self.window.max_bytes or newlines > self.window.max_lines

    def trim(self) -> None:
        """Replace the longest prefix whose remainder still proves exclusion by counts."""
        removed = bytearray()
        # Whole segments first.
        while len(self.segments) > 1:
            first = self.segments[0][1]
            length, newlines = len(first), newline_count(first)
            if not self.proves(self.bytes - length, self.newlines - newlines):
                break
            self.segments.pop(0)
            self.bytes -= length
            self.newlines -= newlines
            removed += first
        # Then a prefix of the first remaining segment, at a character boundary.
        first = self.segments[0][1]
        rest_bytes = self.bytes - len(first)
        rest_newlines = self.newlines - newline_count(first)
        cut = 0
        # By bytes: keep more than `max_bytes` overall.
        keep_bytes = max(self.window.max_bytes + 1 - rest_bytes, 0)
        if len(first) > keep_bytes:
            index = len(first) - keep_bytes
            while index > 0 and not _is_char_boundary(first, index):
                index -= 1
            cut = max(cut, index)
        # By lines: keep more than `max_lines` newlines overall, starting at a newline of this segment.
        keep_newlines = max(self.window.max_lines + 1 - rest_newlines, 0)
        if keep_newlines > 0:
            position = len(first)
            for _ in range(keep_newlines):
                position = first.rfind(b"\n", 0, position)
                if position == -1:
                    break
            if position != -1:
                cut = max(cut, position)
        else:
            cut = len(first)
        if cut > 0:
            prefix = bytes(first[:cut])
            del first[:cut]
            self.bytes -= len(prefix)
            self.newlines -= newline_count(prefix)
            removed += prefix
            if not first:
                self.segments.pop(0)
        if removed:
            if self.skipped is None:
                self.skipped = Skipped()
            self.skipped.bytes += len(removed)
            self.skipped.newlines += newline_count(removed)
            self.skipped.ends_with_newline = removed.endswith(b"\n")

    def take(self) -> list[Event]:
        """Deliver everything held: one event carrying the skip and all text after it, or one event per segment."""
        segments, self.segments = self.segments, []
        delivered = self.bytes
        self.bytes = 0
        self.newlines = 0
        # The caller writes at most the window per sample, and paces itself by what it wrote.
        pace = min(delivered, self.window.max_bytes) / self.window.bytes_per_second
        self._next_send = time.monotonic() + max(self.window.min_interval, pace)
        skipped, self.skipped = self.skipped, None
        if skipped is not None:
            stream = segments[-1][0] if segments else "stdout"
            text = b"".join(bytes(text) for _, text in segments)
            return [
                Event(
                    stream,
                    text,
                    {
                        "bytes": skipped.bytes,
                        "newlines": skipped.newlines,
                        "endsWithNewline": skipped.ends_with_newline,
                    },
                )
            ]
        return [Event(stream, bytes(text), None) for stream, text in segments]

"""Shared truncation utilities for tool outputs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from misaka.utils.js import formatNumber

DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 50 * 1024
GREP_MAX_LINE_LENGTH = 500


@dataclass(slots=True)
class TruncationResult:
    content: str
    truncated: bool
    truncatedBy: str | None
    totalLines: int
    totalBytes: int
    outputLines: int
    outputBytes: int
    lastLinePartial: bool
    firstLineExceedsLimit: bool
    maxLines: int
    maxBytes: int


@dataclass(slots=True)
class TruncationOptions:
    maxLines: int | None = None
    maxBytes: int | None = None


def splitLinesForCounting(content: str) -> list[str]:
    if not content:
        return []
    lines = content.split("\n")
    if content.endswith("\n"):
        lines.pop()
    return lines


def formatSize(bytes_count: int) -> str:
    if bytes_count < 1024:
        return f"{formatNumber(bytes_count)}B"
    if bytes_count < 1024 * 1024:
        return f"{_to_fixed_1(bytes_count / 1024)}KB"
    return f"{_to_fixed_1(bytes_count / (1024 * 1024))}MB"


def _to_fixed_1(value: float) -> str:
    """``value.toFixed(1)``: on an exact tie the larger digit wins, where ``format`` rounds to even."""
    return str(Decimal(value).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def truncateHead(content: str, options: TruncationOptions | None = None) -> TruncationResult:
    resolved = options or TruncationOptions()
    max_lines = resolved.maxLines if resolved.maxLines is not None else DEFAULT_MAX_LINES
    max_bytes = resolved.maxBytes if resolved.maxBytes is not None else DEFAULT_MAX_BYTES

    total_bytes = len(content.encode("utf-8"))
    lines = splitLinesForCounting(content)
    total_lines = len(lines)

    if total_lines <= max_lines and total_bytes <= max_bytes:
        return TruncationResult(
            content=content,
            truncated=False,
            truncatedBy=None,
            totalLines=total_lines,
            totalBytes=total_bytes,
            outputLines=total_lines,
            outputBytes=total_bytes,
            lastLinePartial=False,
            firstLineExceedsLimit=False,
            maxLines=max_lines,
            maxBytes=max_bytes,
        )

    first_line = lines[0] if lines else ""
    if len(first_line.encode("utf-8")) > max_bytes:
        return TruncationResult(
            content="",
            truncated=True,
            truncatedBy="bytes",
            totalLines=total_lines,
            totalBytes=total_bytes,
            outputLines=0,
            outputBytes=0,
            lastLinePartial=False,
            firstLineExceedsLimit=True,
            maxLines=max_lines,
            maxBytes=max_bytes,
        )

    output_lines: list[str] = []
    output_bytes = 0
    truncated_by = "lines"

    for index, line in enumerate(lines[:max_lines]):
        line_bytes = len(line.encode("utf-8")) + (1 if index > 0 else 0)
        if output_bytes + line_bytes > max_bytes:
            truncated_by = "bytes"
            break
        output_lines.append(line)
        output_bytes += line_bytes

    if len(output_lines) >= max_lines and output_bytes <= max_bytes:
        truncated_by = "lines"

    output_content = "\n".join(output_lines)
    return TruncationResult(
        content=output_content,
        truncated=True,
        truncatedBy=truncated_by,
        totalLines=total_lines,
        totalBytes=total_bytes,
        outputLines=len(output_lines),
        outputBytes=len(output_content.encode("utf-8")),
        lastLinePartial=False,
        firstLineExceedsLimit=False,
        maxLines=max_lines,
        maxBytes=max_bytes,
    )


def truncateTail(content: str, options: TruncationOptions | None = None) -> TruncationResult:
    resolved = options or TruncationOptions()
    max_lines = resolved.maxLines if resolved.maxLines is not None else DEFAULT_MAX_LINES
    max_bytes = resolved.maxBytes if resolved.maxBytes is not None else DEFAULT_MAX_BYTES

    total_bytes = len(content.encode("utf-8"))
    lines = splitLinesForCounting(content)
    total_lines = len(lines)

    if total_lines <= max_lines and total_bytes <= max_bytes:
        return TruncationResult(
            content=content,
            truncated=False,
            truncatedBy=None,
            totalLines=total_lines,
            totalBytes=total_bytes,
            outputLines=total_lines,
            outputBytes=total_bytes,
            lastLinePartial=False,
            firstLineExceedsLimit=False,
            maxLines=max_lines,
            maxBytes=max_bytes,
        )

    output_lines: list[str] = []
    output_bytes = 0
    truncated_by = "lines"
    last_line_partial = False

    for line in reversed(lines):
        if len(output_lines) >= max_lines:
            break
        line_bytes = len(line.encode("utf-8")) + (1 if output_lines else 0)
        if output_bytes + line_bytes > max_bytes:
            truncated_by = "bytes"
            if not output_lines:
                truncated_line = truncateStringToBytesFromEnd(line, max_bytes)
                output_lines.insert(0, truncated_line)
                output_bytes = len(truncated_line.encode("utf-8"))
                last_line_partial = True
            break
        output_lines.insert(0, line)
        output_bytes += line_bytes

    if len(output_lines) >= max_lines and output_bytes <= max_bytes:
        truncated_by = "lines"

    output_content = "\n".join(output_lines)
    return TruncationResult(
        content=output_content,
        truncated=True,
        truncatedBy=truncated_by,
        totalLines=total_lines,
        totalBytes=total_bytes,
        outputLines=len(output_lines),
        outputBytes=len(output_content.encode("utf-8")),
        lastLinePartial=last_line_partial,
        firstLineExceedsLimit=False,
        maxLines=max_lines,
        maxBytes=max_bytes,
    )


def truncateStringToBytesFromEnd(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value

    start = len(encoded) - max_bytes
    while start < len(encoded) and (encoded[start] & 0xC0) == 0x80:
        start += 1
    return encoded[start:].decode("utf-8")


def truncateLine(line: str, max_chars: int = GREP_MAX_LINE_LENGTH) -> dict[str, str | bool]:
    if len(line) <= max_chars:
        return {"text": line, "wasTruncated": False}
    return {"text": f"{line[:max_chars]}... [truncated]", "wasTruncated": True}


@dataclass(slots=True)
class MiddleTruncationResult:
    # The start and end of the content with a `…N chars truncated…` marker between them.
    content: str
    truncated: bool
    # Characters left out.
    removedChars: int
    totalBytes: int
    totalLines: int


# `Buffer.from(content, "utf-8")` writes a lone surrogate as U+FFFD, where `str.encode` would raise.
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def truncateMiddle(content: str, max_bytes: int) -> MiddleTruncationResult:
    """Keep the start and the end of ``content``, half of ``max_bytes`` each, and replace the middle
    with a ``…N chars truncated…`` marker, like Codex does for tool output. Cuts only at character
    boundaries."""
    buf = _LONE_SURROGATE.sub("�", content).encode("utf-8")
    total_lines = len(splitLinesForCounting(content))
    if len(buf) <= max_bytes:
        return MiddleTruncationResult(
            content=content, truncated=False, removedChars=0, totalBytes=len(buf), totalLines=total_lines
        )

    # Continuation bytes (10xxxxxx) are not character starts.
    def is_boundary(index: int) -> bool:
        return index >= len(buf) or (buf[index] & 0xC0) != 0x80

    head_end = max_bytes // 2
    while head_end > 0 and not is_boundary(head_end):
        head_end -= 1
    tail_start = len(buf) - (max_bytes - max_bytes // 2)
    while tail_start < len(buf) and not is_boundary(tail_start):
        tail_start += 1
    head = buf[:head_end].decode("utf-8")
    tail = buf[tail_start:].decode("utf-8")
    removed_chars = len(buf[head_end:tail_start].decode("utf-8"))
    return MiddleTruncationResult(
        content=f"{head}…{removed_chars} chars truncated…{tail}",
        truncated=True,
        removedChars=removed_chars,
        totalBytes=len(buf),
        totalLines=total_lines,
    )



__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_LINES",
    "GREP_MAX_LINE_LENGTH",
    "MiddleTruncationResult",
    "TruncationOptions",
    "TruncationResult",
    "formatSize",
    "truncateHead",
    "truncateLine",
    "truncateMiddle",
    "truncateTail",
]

# Kept for misaka/core/subagent, which imports these names.
truncate_head = truncateHead

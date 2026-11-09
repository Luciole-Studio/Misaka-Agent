"""Shared truncation utilities for tool outputs.

Translated from pi's ``packages/durable/src/truncate.ts``. Truncation is based on two independent limits, and whichever
is hit first wins: a line limit (default 2000 lines) and a byte limit (default 50KB). It never returns partial lines.
Tool output streams are bounded by ``OutputBuffer`` from ``misaka.chord.output`` instead. A result is a ``dict`` with the
upstream keys.
"""

from __future__ import annotations

from typing import Literal, TypedDict

# One implementation, shared with the output buffer.
from misaka.chord.output import utf8ByteLength
from misaka.utils.js import toFixed

DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 50 * 1024  # 50KB


class TruncationResult(TypedDict):
    content: str  # The truncated content.
    truncated: bool  # Whether truncation occurred.
    truncatedBy: Literal["lines", "bytes"] | None  # Which limit was hit, or None if not truncated.
    totalLines: int  # Total number of lines in the original content.
    totalBytes: int  # Total number of bytes in the original content.
    outputLines: int  # Number of complete lines in the truncated output.
    outputBytes: int  # Number of bytes in the truncated output.
    lastLinePartial: bool  # Whether the last line was partially truncated (only for tail truncation edge case).
    firstLineExceedsLimit: bool  # Whether the first line exceeded the byte limit (for head truncation).
    maxLines: int  # The max lines limit that was applied.
    maxBytes: int  # The max bytes limit that was applied.


class TruncationOptions(TypedDict, total=False):
    maxLines: int  # Maximum number of lines (default: 2000).
    maxBytes: int  # Maximum number of bytes (default: 50KB).


def splitLinesForCounting(content: str) -> list[str]:
    if len(content) == 0:
        return []
    lines = content.split("\n")
    if content.endswith("\n"):
        lines.pop()
    return lines


def formatSize(bytes: int) -> str:
    """Format bytes as human-readable size."""
    if bytes < 1024:
        return f"{bytes}B"
    if bytes < 1024 * 1024:
        return f"{toFixed(bytes / 1024, 1)}KB"
    return f"{toFixed(bytes / (1024 * 1024), 1)}MB"


def truncateHead(content: str, options: TruncationOptions | None = None) -> TruncationResult:
    """Truncate content from the head (keep first N lines/bytes). Suitable for file reads where you want to see the
    beginning.

    Never returns partial lines. If the first line exceeds the byte limit, returns empty content with
    ``firstLineExceedsLimit`` set."""
    return truncateHeadOf(content, {"lines": len(splitLinesForCounting(content)), "bytes": utf8ByteLength(content)}, options)


def truncateHeadOf(prefix: str, totals: dict[str, int], options: TruncationOptions | None = None) -> TruncationResult:
    """``truncateHead`` of a text known by a prefix and its totals (``lines`` counted like ``truncateHead``, ignoring a
    trailing newline). The prefix must be the whole text, or longer than ``maxBytes + 1`` UTF-8 bytes, or hold at least
    ``maxLines`` newlines; then the result equals ``truncateHead`` of the whole text."""
    if options is None:
        options = {}
    maxLines = options.get("maxLines")
    if maxLines is None:
        maxLines = DEFAULT_MAX_LINES
    maxBytes = options.get("maxBytes")
    if maxBytes is None:
        maxBytes = DEFAULT_MAX_BYTES

    totalBytes = totals["bytes"]
    lines = splitLinesForCounting(prefix)
    totalLines = totals["lines"]

    # Check if no truncation needed
    if totalLines <= maxLines and totalBytes <= maxBytes:
        return {
            "content": prefix,
            "truncated": False,
            "truncatedBy": None,
            "totalLines": totalLines,
            "totalBytes": totalBytes,
            "outputLines": totalLines,
            "outputBytes": totalBytes,
            "lastLinePartial": False,
            "firstLineExceedsLimit": False,
            "maxLines": maxLines,
            "maxBytes": maxBytes,
        }

    # Check if first line alone exceeds byte limit
    firstLineBytes = utf8ByteLength(lines[0])
    if firstLineBytes > maxBytes:
        return {
            "content": "",
            "truncated": True,
            "truncatedBy": "bytes",
            "totalLines": totalLines,
            "totalBytes": totalBytes,
            "outputLines": 0,
            "outputBytes": 0,
            "lastLinePartial": False,
            "firstLineExceedsLimit": True,
            "maxLines": maxLines,
            "maxBytes": maxBytes,
        }

    # Collect complete lines that fit
    outputLinesArr: list[str] = []
    outputBytesCount = 0
    truncatedBy: Literal["lines", "bytes"] = "lines"

    i = 0
    while i < len(lines) and i < maxLines:
        line = lines[i]
        lineBytes = utf8ByteLength(line) + (1 if i > 0 else 0)  # +1 for newline

        if outputBytesCount + lineBytes > maxBytes:
            truncatedBy = "bytes"
            break

        outputLinesArr.append(line)
        outputBytesCount += lineBytes
        i += 1

    # Without a byte break, only omitted lines prove the line limit was reached; otherwise a trailing newline exceeded bytes.
    if truncatedBy != "bytes":
        truncatedBy = "lines" if len(outputLinesArr) < totalLines else "bytes"

    outputContent = "\n".join(outputLinesArr)
    finalOutputBytes = utf8ByteLength(outputContent)

    return {
        "content": outputContent,
        "truncated": True,
        "truncatedBy": truncatedBy,
        "totalLines": totalLines,
        "totalBytes": totalBytes,
        "outputLines": len(outputLinesArr),
        "outputBytes": finalOutputBytes,
        "lastLinePartial": False,
        "firstLineExceedsLimit": False,
        "maxLines": maxLines,
        "maxBytes": maxBytes,
    }


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_LINES",
    "TruncationOptions",
    "TruncationResult",
    "formatSize",
    "truncateHead",
    "truncateHeadOf",
    "utf8ByteLength",
]

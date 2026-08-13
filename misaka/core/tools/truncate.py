"""Shared truncation utilities for tool outputs."""

from __future__ import annotations

from dataclasses import dataclass

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


def _split_lines_for_counting(content: str) -> list[str]:
    if not content:
        return []
    lines = content.split("\n")
    if content.endswith("\n"):
        lines.pop()
    return lines


def format_size(bytes_count: int) -> str:
    if bytes_count < 1024:
        return f"{bytes_count}B"
    if bytes_count < 1024 * 1024:
        return f"{bytes_count / 1024:.1f}KB"
    return f"{bytes_count / (1024 * 1024):.1f}MB"


def truncate_head(content: str, options: TruncationOptions | None = None) -> TruncationResult:
    resolved = options or TruncationOptions()
    max_lines = resolved.maxLines if resolved.maxLines is not None else DEFAULT_MAX_LINES
    max_bytes = resolved.maxBytes if resolved.maxBytes is not None else DEFAULT_MAX_BYTES

    total_bytes = len(content.encode("utf-8"))
    lines = _split_lines_for_counting(content)
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


def truncate_tail(content: str, options: TruncationOptions | None = None) -> TruncationResult:
    resolved = options or TruncationOptions()
    max_lines = resolved.maxLines if resolved.maxLines is not None else DEFAULT_MAX_LINES
    max_bytes = resolved.maxBytes if resolved.maxBytes is not None else DEFAULT_MAX_BYTES

    total_bytes = len(content.encode("utf-8"))
    lines = _split_lines_for_counting(content)
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
                truncated_line = _truncate_string_to_bytes_from_end(line, max_bytes)
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


def _truncate_string_to_bytes_from_end(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value

    start = len(encoded) - max_bytes
    while start < len(encoded) and (encoded[start] & 0xC0) == 0x80:
        start += 1
    return encoded[start:].decode("utf-8")


def truncate_line(line: str, max_chars: int = GREP_MAX_LINE_LENGTH) -> dict[str, str | bool]:
    if len(line) <= max_chars:
        return {"text": line, "wasTruncated": False}
    return {"text": f"{line[:max_chars]}... [truncated]", "wasTruncated": True}


formatSize = format_size
truncateHead = truncate_head
truncateTail = truncate_tail
truncateLine = truncate_line

__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_LINES",
    "GREP_MAX_LINE_LENGTH",
    "TruncationOptions",
    "TruncationResult",
    "formatSize",
    "truncateHead",
    "truncateLine",
    "truncateTail",
]

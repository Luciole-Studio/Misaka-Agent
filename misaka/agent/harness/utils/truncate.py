"""Shared truncation utilities for tool outputs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 50 * 1024
GREP_MAX_LINE_LENGTH = 500


@dataclass(slots=True)
class TruncationResult:
    content: str
    truncated: bool
    truncatedBy: Literal["lines", "bytes"] | None
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


def _encode_node_utf8(content: str) -> bytes:
    output = bytearray()
    i = 0
    while i < len(content):
        code = ord(content[i])
        if 0xD800 <= code <= 0xDBFF:
            if i + 1 < len(content):
                next_code = ord(content[i + 1])
                if 0xDC00 <= next_code <= 0xDFFF:
                    combined = 0x10000 + ((code - 0xD800) << 10) + (next_code - 0xDC00)
                    output.extend(chr(combined).encode("utf-8"))
                    i += 2
                    continue
            output.extend("\uFFFD".encode("utf-8"))
            i += 1
            continue
        if 0xDC00 <= code <= 0xDFFF:
            output.extend("\uFFFD".encode("utf-8"))
            i += 1
            continue
        output.extend(content[i].encode("utf-8"))
        i += 1
    return bytes(output)


def _utf8_byte_length(content: str) -> int:
    return len(_encode_node_utf8(content))


def _replace_unpaired_surrogates(content: str) -> str:
    output: list[str] = []
    i = 0
    while i < len(content):
        code = ord(content[i])
        if 0xD800 <= code <= 0xDBFF:
            if i + 1 < len(content):
                next_code = ord(content[i + 1])
                if 0xDC00 <= next_code <= 0xDFFF:
                    output.append(content[i])
                    output.append(content[i + 1])
                    i += 2
                    continue
            output.append("\uFFFD")
        elif 0xDC00 <= code <= 0xDFFF:
            output.append("\uFFFD")
        else:
            output.append(content[i])
        i += 1
    return "".join(output)


def _collapse_surrogate_pairs(content: str) -> str:
    output: list[str] = []
    i = 0
    while i < len(content):
        code = ord(content[i])
        if 0xD800 <= code <= 0xDBFF and i + 1 < len(content):
            next_code = ord(content[i + 1])
            if 0xDC00 <= next_code <= 0xDFFF:
                combined = 0x10000 + ((code - 0xD800) << 10) + (next_code - 0xDC00)
                output.append(chr(combined))
                i += 2
                continue
        output.append(content[i])
        i += 1
    return "".join(output)


def format_size(bytes_count: int) -> str:
    if bytes_count < 1024:
        return f"{bytes_count}B"
    if bytes_count < 1024 * 1024:
        return f"{bytes_count / 1024:.1f}KB"
    return f"{bytes_count / (1024 * 1024):.1f}MB"


def truncate_head(content: str, options: TruncationOptions | dict[str, int] | None = None) -> TruncationResult:
    max_lines, max_bytes = _resolve_limits(options)
    total_bytes = _utf8_byte_length(content)
    lines = content.split("\n")
    total_lines = len(lines)

    if total_lines <= max_lines and total_bytes <= max_bytes:
        output_content = _collapse_surrogate_pairs(content)
        return TruncationResult(
            content=output_content,
            truncated=False,
            truncatedBy=None,
            totalLines=total_lines,
            totalBytes=total_bytes,
            outputLines=total_lines,
            outputBytes=_utf8_byte_length(output_content),
            lastLinePartial=False,
            firstLineExceedsLimit=False,
            maxLines=max_lines,
            maxBytes=max_bytes,
        )

    first_line_bytes = _utf8_byte_length(lines[0] if lines else "")
    if first_line_bytes > max_bytes:
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
    output_bytes_count = 0
    truncated_by: Literal["lines", "bytes"] = "lines"
    for index, line in enumerate(lines[:max_lines]):
        line_bytes = _utf8_byte_length(line) + (1 if index > 0 else 0)
        if output_bytes_count + line_bytes > max_bytes:
            truncated_by = "bytes"
            break
        output_lines.append(line)
        output_bytes_count += line_bytes

    if len(output_lines) >= max_lines and output_bytes_count <= max_bytes:
        truncated_by = "lines"

    output_content = _collapse_surrogate_pairs("\n".join(output_lines))
    return TruncationResult(
        content=output_content,
        truncated=True,
        truncatedBy=truncated_by,
        totalLines=total_lines,
        totalBytes=total_bytes,
        outputLines=len(output_lines),
        outputBytes=_utf8_byte_length(output_content),
        lastLinePartial=False,
        firstLineExceedsLimit=False,
        maxLines=max_lines,
        maxBytes=max_bytes,
    )


def truncate_tail(content: str, options: TruncationOptions | dict[str, int] | None = None) -> TruncationResult:
    max_lines, max_bytes = _resolve_limits(options)
    total_bytes = _utf8_byte_length(content)
    lines = content.split("\n")
    if len(lines) > 1 and lines[-1] == "":
        lines.pop()
    total_lines = len(lines)

    if total_lines <= max_lines and total_bytes <= max_bytes:
        output_content = _collapse_surrogate_pairs(content)
        return TruncationResult(
            content=output_content,
            truncated=False,
            truncatedBy=None,
            totalLines=total_lines,
            totalBytes=total_bytes,
            outputLines=total_lines,
            outputBytes=_utf8_byte_length(output_content),
            lastLinePartial=False,
            firstLineExceedsLimit=False,
            maxLines=max_lines,
            maxBytes=max_bytes,
        )

    output_lines: list[str] = []
    output_bytes_count = 0
    truncated_by: Literal["lines", "bytes"] = "lines"
    last_line_partial = False
    for line in reversed(lines):
        line_bytes = _utf8_byte_length(line) + (1 if output_lines else 0)
        if output_bytes_count + line_bytes > max_bytes:
            truncated_by = "bytes"
            if not output_lines:
                truncated_line = _truncate_string_to_bytes_from_end(line, max_bytes)
                output_lines.insert(0, truncated_line)
                output_bytes_count = _utf8_byte_length(truncated_line)
                last_line_partial = True
            break
        output_lines.insert(0, line)
        output_bytes_count += line_bytes
        if len(output_lines) >= max_lines:
            break

    if len(output_lines) >= max_lines and output_bytes_count <= max_bytes:
        truncated_by = "lines"

    output_content = _collapse_surrogate_pairs("\n".join(output_lines))
    return TruncationResult(
        content=output_content,
        truncated=True,
        truncatedBy=truncated_by,
        totalLines=total_lines,
        totalBytes=total_bytes,
        outputLines=len(output_lines),
        outputBytes=_utf8_byte_length(output_content),
        lastLinePartial=last_line_partial,
        firstLineExceedsLimit=False,
        maxLines=max_lines,
        maxBytes=max_bytes,
    )


def _truncate_string_to_bytes_from_end(content: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    encoded = _encode_node_utf8(content)
    if len(encoded) <= max_bytes:
        return _replace_unpaired_surrogates(content)
    start = len(encoded) - max_bytes
    while start < len(encoded) and (encoded[start] & 0xC0) == 0x80:
        start += 1
    return encoded[start:].decode("utf-8", errors="replace")


def truncate_line(line: str, max_chars: int = GREP_MAX_LINE_LENGTH) -> dict[str, str | bool]:
    if len(line) <= max_chars:
        return {"text": line, "wasTruncated": False}
    return {"text": f"{line[:max_chars]}... [truncated]", "wasTruncated": True}


def _resolve_limits(options: TruncationOptions | dict[str, int] | None) -> tuple[int, int]:
    if isinstance(options, TruncationOptions):
        max_lines = options.maxLines
        max_bytes = options.maxBytes
    elif isinstance(options, dict):
        max_lines = options.get("maxLines")
        max_bytes = options.get("maxBytes")
    else:
        max_lines = None
        max_bytes = None
    return (
        DEFAULT_MAX_LINES if max_lines is None else max_lines,
        DEFAULT_MAX_BYTES if max_bytes is None else max_bytes,
    )


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
    "format_size",
    "truncateHead",
    "truncateLine",
    "truncateTail",
    "truncate_head",
    "truncate_line",
    "truncate_tail",
]

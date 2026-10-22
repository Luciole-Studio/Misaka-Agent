"""pi durable's ``test/tools-read-differential.test.ts``."""

from __future__ import annotations

import base64
import math
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.chord.output import characterEnd
from misaka.durable.env.node import NodeExecutionEnv
from misaka.durable.tools.image import detectSupportedImageMimeType
from misaka.durable.tools.read import createReadTool
from misaka.durable.truncate import DEFAULT_MAX_BYTES, formatSize, truncateHead
from misaka.utils.js import Utf8Decoder, formatNumber
from tests.test_durable_env_line_scan import random


def _jsSliceIndex(value: float, length: int) -> int:
    if math.isnan(value):
        value = 0
    elif not math.isinf(value):
        value = math.trunc(value)
    if value < 0:
        return int(max(length + value, 0))
    return int(min(value, length))


def _jsSlice(items: list[str], start: float, end: float | None = None) -> list[str]:
    """``items.slice(start, end)``."""
    return items[_jsSliceIndex(start, len(items)) : len(items) if end is None else _jsSliceIndex(end, len(items))]


def _jsMin(a: float, b: float) -> float:
    return math.nan if math.isnan(a) or math.isnan(b) else min(a, b)


def _truthy(value: float | None) -> bool:
    return value is not None and value != 0 and not math.isnan(value)


def _decode(data: bytes) -> str:
    """``new TextDecoder().decode(data)``."""
    return Utf8Decoder().decode(data, final=True)


def referenceRead(data: bytes, path: str, offset: float | None, limit: float | None) -> dict[str, Any]:
    """The ``read`` tool before bounded reads, kept as the reference: it decodes the whole file, splits it into lines,
    and truncates the selection. The tool must give the same result while reading only a bounded part of the file."""
    mimeType = detectSupportedImageMimeType(data)
    if mimeType:
        return {
            "output": [{"type": "image", "data": base64.b64encode(data).decode("ascii"), "mimeType": mimeType}],
            "diagnostics": [{"severity": "info", "code": "image", "message": f"Read image file [{mimeType}]."}],
        }
    textContent = _decode(data)
    allLines = textContent.split("\n")
    totalFileLines = len(allLines)
    startLine: float = max(0, offset - 1) if _truthy(offset) else 0  # type: ignore[operator]
    startLineDisplay = startLine + 1
    if startLine >= len(allLines):
        raise RuntimeError(f"Offset {formatNumber(offset)} is beyond end of file ({len(allLines)} lines total)")
    userLimitedLines: float | None = None
    if limit is not None:
        endLine = _jsMin(startLine + limit, len(allLines))
        selectedContent = "\n".join(_jsSlice(allLines, startLine, endLine))
        userLimitedLines = endLine - startLine
    else:
        selectedContent = "\n".join(_jsSlice(allLines, startLine))
    truncation: dict[str, Any] = dict(truncateHead(selectedContent))  # type: ignore[arg-type]
    headText = truncation.pop("content")
    diagnostics: list[dict[str, Any]] = []
    outputText = headText
    details: dict[str, Any] | None = None
    if truncation["firstLineExceedsLimit"]:
        # `allLines[startLine]` of a fractional index is undefined, and `encode(undefined)` encodes "".
        line = allLines[int(startLine)] if startLine == int(startLine) else ""
        lineBytes = line.encode("utf-8")
        end = characterEnd(lineBytes, DEFAULT_MAX_BYTES)
        outputText = _decode(lineBytes[:end])
        diagnostics.append(
            {
                "severity": "warn",
                "code": "truncated",
                "message": f"Line {formatNumber(startLineDisplay)} is {formatSize(len(lineBytes))}, exceeds the {formatSize(DEFAULT_MAX_BYTES)} limit; showing its first {formatSize(end)}. Use bash: sed -n '{formatNumber(startLineDisplay)}p' {path} | tail -c +{end + 1}",
            }
        )
        details = {"truncation": {**truncation, "outputBytes": end, "outputLines": 1}}
    elif truncation["truncated"]:
        endLineDisplay = startLineDisplay + truncation["outputLines"] - 1
        nextOffset = endLineDisplay + 1
        limitText = "" if truncation["truncatedBy"] == "lines" else f" ({formatSize(DEFAULT_MAX_BYTES)} limit)"
        diagnostics.append(
            {
                "severity": "info",
                "code": "truncated",
                "message": f"Showing lines {formatNumber(startLineDisplay)}-{formatNumber(endLineDisplay)} of {totalFileLines}{limitText}. Use offset={formatNumber(nextOffset)} to continue.",
            }
        )
        details = {"truncation": truncation}
    elif userLimitedLines is not None and startLine + userLimitedLines < len(allLines):
        remaining = len(allLines) - (startLine + userLimitedLines)
        nextOffset = startLine + userLimitedLines + 1
        diagnostics.append(
            {
                "severity": "info",
                "message": f"{formatNumber(remaining)} more lines in file. Use offset={formatNumber(nextOffset)} to continue.",
            }
        )
    return {
        "output": [] if outputText == "" else [{"type": "text", "text": outputText}],
        **({} if details is None else {"details": details}),
        "diagnostics": diagnostics,
    }


PIECES = [
    [0x0A],
    [0x0A],
    [0x61],
    [0x62, 0x63, 0x64, 0x65],
    [0xEF, 0xBB, 0xBF],
    [0xC3, 0xA9],
    [0xE2, 0x82, 0xAC],
    [0xF0, 0x9F, 0x98, 0x80],
    [0xE2, 0x82],
    [0xFF],
    [0x0D, 0x0A],
]


def randomFile(next_) -> bytes:
    """Mostly small files, some over the line limit, some over the byte limit, some with one huge line."""
    kind = next_()
    if kind < 0.1:
        return bytes([0xEF, 0xBB, 0xBF]) + ("x\n" * 2100).encode()
    if kind < 0.2:
        line = bytearray(b"a" * (DEFAULT_MAX_BYTES + 200 + int(next_() * 400)))
        line[DEFAULT_MAX_BYTES - 1 + int(next_() * 3)] = 0xC3
        return bytes(line) + bytes([0x0A, 0x62])
    if kind < 0.3:
        return (f"{'0123456789' * 30}\n" * 200).encode()
    out: list[int] = []
    pieces = int(next_() * 80)
    for _index in range(pieces):
        out.extend(PIECES[int(next_() * len(PIECES))])
    return bytes(out)


OFFSETS = [None, 0, 1, 2, 3, 2.5, -4, 50, 2001, 3000, 1e20, math.nan]
LIMITS = [None, 0, 1, 2, -3, -1e20, 1.5, 7, 2000, 2500, 1e20, math.nan]


def apiFor(env: NodeExecutionEnv) -> Any:
    async def details(*_args: Any) -> None:
        return None

    async def agent(*_args: Any) -> dict[str, Any]:
        return {}

    return SimpleNamespace(
        env=env, output=lambda *_args: None, outputWindow=None, diagnostic=lambda *_args: None, details=details, agent=agent
    )


async def outcome(run) -> Any:
    try:
        value = run()
        if hasattr(value, "__await__"):
            value = await value
        return value
    except Exception as error:  # noqa: BLE001
        return {"error": str(error)}


def _same(actual: Any, expected: Any) -> bool:
    """``toEqual``, with NaN equal to NaN (numbers in the diagnostics are formatted, so only details can hold one)."""
    if isinstance(actual, float) and isinstance(expected, float) and math.isnan(actual) and math.isnan(expected):
        return True
    if isinstance(actual, dict) and isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(_same(actual[key], expected[key]) for key in actual)
    if isinstance(actual, list) and isinstance(expected, list):
        return len(actual) == len(expected) and all(_same(a, b) for a, b in zip(actual, expected, strict=True))
    return actual == expected


class TestReadTool:
    async def test_returns_exactly_what_reading_the_whole_file_returned(self):
        cwd = tempfile.mkdtemp(prefix="pi-durable-read-")
        try:
            env = NodeExecutionEnv({"cwd": cwd})
            tool = createReadTool()
            for seed in range(1, 401):
                next_ = random(seed)
                file = randomFile(next_)
                Path(cwd, "f.txt").write_bytes(file)
                for _trial in range(4):
                    offset = OFFSETS[int(next_() * len(OFFSETS))]
                    limit = LIMITS[int(next_() * len(LIMITS))]
                    args = {
                        "path": "f.txt",
                        **({} if offset is None else {"offset": offset}),
                        **({} if limit is None else {"limit": limit}),
                    }
                    actual = await outcome(lambda args=args: tool["execute"](args, apiFor(env), BACKGROUND_CONTEXT))
                    expected = await outcome(lambda file=file, offset=offset, limit=limit: referenceRead(file, "f.txt", offset, limit))
                    assert _same(actual, expected), f"seed {seed} offset {offset} limit {limit}: {actual!r} != {expected!r}"
        finally:
            shutil.rmtree(cwd, ignore_errors=True)

    async def test_detects_animated_pngs_whose_actl_chunk_lies_far_beyond_the_header(self):
        cwd = tempfile.mkdtemp(prefix="pi-durable-read-")
        try:

            def chunk(type_: str, length: int) -> bytes:
                return length.to_bytes(4, "big") + type_.encode() + bytes(length + 4)

            png = (
                bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
                + chunk("IHDR", 13)
                + chunk("iCCP", 200_000)
                + chunk("acTL", 8)
                + chunk("IDAT", 10)
            )
            Path(cwd, "a.png").write_bytes(png)
            env = NodeExecutionEnv({"cwd": cwd})
            actual = await outcome(lambda: createReadTool()["execute"]({"path": "a.png"}, apiFor(env), BACKGROUND_CONTEXT))
            assert actual == await outcome(lambda: referenceRead(png, "a.png", None, None))
        finally:
            shutil.rmtree(cwd, ignore_errors=True)

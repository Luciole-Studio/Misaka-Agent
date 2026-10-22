"""The ``read`` tool.

Translated from pi's ``packages/durable/src/tools/read.ts`` (v1.3.0). Reads text files and images; remarks about
truncation, continuation, and image processing are diagnostics, and the content is only file text, or one image.
``createReadTool(options)`` takes an optional ``dict`` (``ReadToolOptions``) with ``images``, an ``ImageProcessor``.
"""

from __future__ import annotations

import math
from typing import Any, TypedDict

from misaka.chord.context import Context
from misaka.chord.output import characterEnd
from misaka.utils.js import (
    Utf8Decoder,
    formatNumber,
    jsIsInteger,
    jsIsSafeInteger,
    toFixed,
)

from ..env import getOrThrow, rangeDecoder, startsWithBom
from ..harness.define import defineTool
from ..truncate import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_LINES,
    formatSize,
    truncateHeadOf,
    utf8ByteLength,
)
from .env import requireEnv
from .image import detectSupportedImageMimeTypeOf, imageDimensions
from .image_processor import (
    DEFAULT_IMAGE_LIMITS,
    INLINE_IMAGE_TYPES,
    ImageProcessor,
    base64Length,
    toBase64,
)
from .path_utils import resolveReadToolPath

readSchema: dict[str, Any] = {
    "type": "object",
    "required": ["path"],
    "properties": {
        "path": {"type": "string", "description": "Path to the file to read (relative or absolute)"},
        "offset": {"type": "number", "description": "Line number to start reading from (1-indexed)"},
        "limit": {"type": "number", "description": "Maximum number of lines to read"},
    },
}

READ_CHUNK = 64 * 1024


def _sliceIndex(value: float) -> float:
    """``Array.prototype.slice``'s conversion of an index: NaN is 0, other values truncate toward zero."""
    if math.isnan(value):
        return 0
    return value if math.isinf(value) else math.trunc(value)


def _jsMin(a: float, b: float) -> float:
    """``Math.min(a, b)``: NaN wins."""
    return math.nan if math.isnan(a) or math.isnan(b) else min(a, b)


def _jsMax(a: float, b: float) -> float:
    """``Math.max(a, b)``: NaN wins."""
    return math.nan if math.isnan(a) or math.isnan(b) else max(a, b)


def _truthy(value: float | None) -> bool:
    """A JavaScript number in a condition: ``undefined``, ``0`` and ``NaN`` are false."""
    return value is not None and value != 0 and not math.isnan(value)


async def _readHead(reader: Any, start: int, end: int, skipBom: bool, context: Context) -> str:
    """The decoded start of bytes ``[start, end)`` of the file, decoded as part of the whole file: all of it, or enough
    for ``truncateHeadOf`` (more than ``DEFAULT_MAX_BYTES + 1`` bytes, or ``DEFAULT_MAX_LINES`` newlines)."""
    decoder = rangeDecoder()
    text = ""
    newlines = 0
    position = 3 if skipBom and start == 0 else start
    while position < end:
        data = getOrThrow(await reader.read(position, min(READ_CHUNK, end - position), context))
        if len(data) == 0:
            break
        position += len(data)
        decoded = decoder.decode(data)
        text += decoded
        newlines += decoded.count("\n")
        if newlines >= DEFAULT_MAX_LINES or utf8ByteLength(text) > DEFAULT_MAX_BYTES + 1:
            return text
    return text + decoder.decode(b"", final=True)


class ReadToolOptions(TypedDict, total=False):
    images: ImageProcessor
    """Resizes and converts images; without one, images within ``DEFAULT_IMAGE_LIMITS["maxBytes"]`` pass through as they
    are."""


def createReadTool(options: ReadToolOptions | None = None) -> dict[str, Any]:
    """Reads text files and images. Remarks about truncation, continuation, and image processing are diagnostics; the
    content is only file text, or one image, which programs get as its ``ImageContent``."""
    if options is None:
        options = {}
    images = options.get("images")
    formats = "jpg, png, gif, webp" if images is None else "jpg, png, gif, webp, bmp"

    async def execute(args: dict[str, Any], api: Any, context: Context) -> dict[str, Any]:
        path = args["path"]
        offset = args.get("offset")
        limit = args.get("limit")
        env = requireEnv(api)
        absolutePath = await resolveReadToolPath(env, path, context)
        reader = getOrThrow(await env.openBinaryReader(absolutePath, None, context))
        try:
            # A concurrent writer can change the file between the scan and the reads. Appending (a growing log) leaves
            # the scanned text as it was; an image must be whole. Otherwise the file is read again once.
            attempt = 0
            while True:
                before = getOrThrow(await reader.info(context))
                mimeType = await detectSupportedImageMimeTypeOf(_ReaderSource(reader, before["size"], context))
                if mimeType is None:
                    result = await _readText(reader, path, offset, limit, context)
                else:
                    result = await _readImage(
                        reader, before, path, mimeType, images, await _imageModel(api, context), context
                    )
                after = getOrThrow(await reader.info(context))
                unchanged = after["size"] == before["size"] and after["mtimeMs"] == before["mtimeMs"]
                if unchanged or (mimeType is None and after["size"] > before["size"]):
                    return result
                if attempt == 1:
                    raise RuntimeError(f"{path} changed while it was read")
                attempt += 1
        finally:
            await reader.close(context)

    return defineTool(
        {
            "name": "read",
            "description": f"Read the contents of a file. Supports text files and images ({formats}). Images are sent as attachments. For text files, output is truncated to {DEFAULT_MAX_LINES} lines or {DEFAULT_MAX_BYTES // 1024}KB (whichever is hit first). Use offset/limit for large files. When you need the full file, continue with offset until complete.",
            "parameters": readSchema,
            "execute": execute,
        }
    )


async def _readImage(
    reader: Any,
    info: dict[str, Any],
    path: str,
    mimeType: str,
    processor: ImageProcessor | None,
    model: dict[str, Any],
    context: Context,
) -> dict[str, Any]:
    """One image block, prepared by ``processor`` or, without one, as it is when its format is inline and it fits the
    limits (its size is checked before reading it, its dimensions from its header); otherwise an error result saying
    why. What happened to the image is an ``info`` diagnostic, so programs get the bare ``ImageContent``. A model without
    image input sees pi-ai's placeholder instead; a diagnostic says so. ``model`` is ``{"vision", "limits"}``."""
    vision = model["vision"]
    limits = model["limits"]
    notes: list[str] = []
    if processor is not None:
        data = getOrThrow(await reader.read(0, info["size"], context))
        prepared = await processor.prepare(data, mimeType, limits)
        if prepared is None:
            return _imageError(f"{path} is an image ({mimeType}) that cannot be prepared for the model")
        image = {"data": prepared["data"], "mimeType": prepared["mimeType"]}
        if prepared.get("convertedFrom") is not None:
            notes.append(f"Converted from {prepared['convertedFrom']} to {prepared['mimeType']}.")
        if prepared.get("resized") is not None:
            source = prepared["resized"]["from"]
            target = prepared["resized"]["to"]
            scale = toFixed(source["width"] / target["width"], 2)
            notes.append(
                f"Resized from {source['width']}x{source['height']} to {target['width']}x{target['height']}. Multiply coordinates by {scale} to map them to the original."
            )
    else:
        if mimeType not in INLINE_IMAGE_TYPES:
            return _imageError(f"{path} is an image ({mimeType}) that needs converting, and no image processor is configured")
        unshrinkable = "and no image processor is configured to shrink it"
        if base64Length(info["size"]) > limits["maxBytes"]:
            return _imageError(
                f"{path} is an image ({mimeType}) of {formatSize(info['size'])}, too large to send, {unshrinkable}"
            )
        data = getOrThrow(await reader.read(0, info["size"], context))
        size = imageDimensions(data, mimeType)
        if size is None:
            return _imageError(f"{path} is an image ({mimeType}) whose size cannot be read")
        if size["width"] > limits["maxWidth"] or size["height"] > limits["maxHeight"]:
            return _imageError(
                f"{path} is an image ({mimeType}) of {size['width']}x{size['height']}, larger than {limits['maxWidth']}x{limits['maxHeight']}, {unshrinkable}"
            )
        image = {"data": toBase64(data), "mimeType": mimeType}
    if not vision:
        notes.append("The current model does not support images; it sees a placeholder instead.")
    diagnostics = [
        {"severity": "info", "code": "image", "message": " ".join([f"Read image file [{image['mimeType']}].", *notes])}
    ]
    return {"output": [{"type": "image", "data": image["data"], "mimeType": image["mimeType"]}], "diagnostics": diagnostics}


async def _imageModel(api: Any, context: Context) -> dict[str, Any]:
    """What the conversation's model takes (``ImageModel``): whether images (``vision``), and within which ``limits``:
    ``DEFAULT_IMAGE_LIMITS`` overridden by its ``inputLimits.images.resize``. Without a resolvable model, it counts as
    one that takes images within the defaults."""
    ref = (await api.agent(context)).get("model")
    model = None if ref is None else api.models.getModel(ref["provider"], ref["modelId"])
    inputLimits = None if model is None else model.inputLimits
    images = None if inputLimits is None else inputLimits.images
    resize = None if images is None else images.resize

    def limit(name: str) -> int:
        value = None if resize is None else getattr(resize, name)
        return DEFAULT_IMAGE_LIMITS[name] if value is None else value  # type: ignore[literal-required]

    return {
        "vision": True if model is None else "image" in model.input,
        "limits": {"maxWidth": limit("maxWidth"), "maxHeight": limit("maxHeight"), "maxBytes": limit("maxBytes")},
    }


def _imageError(message: str) -> dict[str, Any]:
    return {"output": [], "isError": True, "diagnostics": [{"severity": "error", "code": "unsupported_image", "message": message}]}


class _ReaderSource:
    def __init__(self, reader: Any, size: int, context: Context) -> None:
        self.size = size
        self._reader = reader
        self._context = context

    async def read(self, position: int, length: int) -> bytes:
        return getOrThrow(await self._reader.read(position, length, self._context))


async def _readText(reader: Any, path: str, offset: float | None, limit: float | None, context: Context) -> dict[str, Any]:
    """The read result for the opened file. It equals decoding the whole file with ``TextDecoder``, splitting it on
    ``\\n``, and bounding the selected lines with ``truncateHead``, while reading only one scan's worth of the file plus
    the head."""
    startLine: float = max(0, offset - 1) if _truthy(offset) else 0  # type: ignore[operator]
    startLineDisplay = startLine + 1
    # Lines are selected like `allLines.slice(startLine, endLine)`, which truncates fractional indices.
    sliceStart = _sliceIndex(startLine)
    # One pass finds the line count and the selection; a selection past the last line ends with it, as `slice` does,
    # and an empty one (a zero or negative limit) is scanned as one line and then ignored.
    # A start beyond any file is scanned from 0 only to count lines; the offset check below then fails as before.
    scanStart = int(sliceStart) if jsIsSafeInteger(sliceStart) else 0
    requestedEnd = None if limit is None else _jsMax(scanStart + 1, _sliceIndex(startLine + limit))
    scanEnd = int(requestedEnd) if requestedEnd is not None and jsIsSafeInteger(requestedEnd) else None

    async def scanOf(endLine: int | None) -> dict[str, Any]:
        return getOrThrow(
            await reader.scanLines({"startLine": scanStart, **({} if endLine is None else {"endLine": endLine})}, context)
        )

    scan = await scanOf(scanEnd)
    totalFileLines = scan["newlines"] + 1
    if startLine >= totalFileLines:
        raise RuntimeError(
            f"Offset {'undefined' if offset is None else formatNumber(offset)} is beyond end of file ({totalFileLines} lines total)"
        )

    userLimitedLines: float | None = None
    selectedLineCount = totalFileLines - sliceStart
    if limit is not None:
        endLine = _jsMin(startLine + limit, totalFileLines)
        userLimitedLines = endLine - startLine
        # `slice` counts a negative end from the end of the lines, which only the line count tells; scan again for it.
        relativeEnd = _sliceIndex(endLine)
        sliceEnd = max(totalFileLines + relativeEnd, 0) if relativeEnd < 0 else relativeEnd
        selectedLineCount = max(0, sliceEnd - sliceStart)
        if selectedLineCount > 0 and relativeEnd < 0:
            scan = await scanOf(int(sliceEnd))
    empty = selectedLineCount == 0
    # Counted like `truncateHead`: a trailing newline adds no line, and empty text has none.
    endsWithNewline = not empty and scan["lastLineStart"] == scan["end"] and scan["lastLineStart"] > scan["start"]
    totals = {
        "lines": 0 if empty or scan["selectedBytes"] == 0 else selectedLineCount - (1 if endsWithNewline else 0),
        "bytes": 0 if empty else scan["selectedBytes"],
    }
    firstBytes = getOrThrow(await reader.read(0, 3, context))
    head = "" if empty else await _readHead(reader, scan["start"], scan["end"], startsWithBom(firstBytes), context)

    truncation: dict[str, Any] = dict(truncateHeadOf(head, totals))  # type: ignore[arg-type]
    headText = truncation.pop("content")
    diagnostics: list[dict[str, Any]] = []
    outputText = headText
    details: dict[str, Any] | None = None
    if truncation["firstLineExceedsLimit"]:
        # Show the start of the line, cut at the byte limit on a character boundary. Like `allLines[startLine]`, a
        # fractional start line names no line.
        integral = jsIsInteger(startLine)
        lineBytes = (head.split("\n")[0] if integral else "").encode("utf-8")
        lineSize = scan["firstLineBytes"] if integral else 0
        end = characterEnd(lineBytes, DEFAULT_MAX_BYTES)
        outputText = Utf8Decoder().decode(lineBytes[:end], final=True)
        diagnostics.append(
            {
                "severity": "warn",
                "code": "truncated",
                "message": f"Line {formatNumber(startLineDisplay)} is {formatSize(lineSize)}, exceeds the {formatSize(DEFAULT_MAX_BYTES)} limit; showing its first {formatSize(end)}. Use bash: sed -n '{formatNumber(startLineDisplay)}p' {path} | tail -c +{end + 1}",
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
    elif userLimitedLines is not None and startLine + userLimitedLines < totalFileLines:
        remaining = totalFileLines - (startLine + userLimitedLines)
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


__all__ = ["ReadToolOptions", "createReadTool"]

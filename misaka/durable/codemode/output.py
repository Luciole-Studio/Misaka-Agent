"""A script's result as the model reads it, copied from the coding agent's code mode: a "Script completed/failed"
header with the wall time, the script's text items (``==> text N/M <==`` when there are several), its ``console`` lines
in one ``<console_output>`` block, the returned value, and on failure the error with the calls already made. Text past
the token budget is cut to its start and end, with the complete text in a temp file; images are saved to temp files
too, with their path next to them, because a script has no other way to hand their bytes on.

Translated from pi's ``packages/durable/src/codemode/output.ts`` (v1.3.0). Items are ``dict`` content blocks; lengths
and slices count UTF-16 code units, as JavaScript's do.
"""

from __future__ import annotations

import asyncio
import base64
import math
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from misaka.utils.js import (
    UNDEFINED,
    jsLength,
    jsonStringify,
    jsSlice,
    jsString,
    nullishCoalesce,
    toFixed,
)

type OutputFile = Callable[[str, str | bytes], Awaitable[dict[str, str]]]
"""Writes a file of the result under a fixed name: ``{"path"}``, or why it could not be written, ``{"error"}``."""

LINE_MARGIN = 100
"""Lines of the result besides the script's text: the header, markers, notes."""

DEFAULT_MAX_OUTPUT_TOKENS = 10_000
"""Default token budget for script output."""
CHARS_PER_TOKEN = 4
"""Characters per token when estimating."""

IMAGE_EXTENSIONS: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
"""File extensions of the image types ``image()`` accepts."""


def valueText(value: Any) -> str:
    """Like the script's ``text()``: strings as is, other values as compact JSON."""
    if isinstance(value, str):
        return value
    if value is UNDEFINED:
        return jsString(value)
    try:
        return jsonStringify(value)
    except (TypeError, ValueError):
        return jsString(value)


def formatItems(output: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Lay out the script's output so the model can tell items apart: with more than one text item, each starts with a
    ``==> text N/M <==`` line; ``console`` lines follow all other output in one ``<console_output>`` block."""
    total = len([item for item in output if item["type"] == "text" and not item.get("console")])
    items: list[dict[str, Any]] = []
    consoleLines: list[str] = []
    index = 0
    for item in output:
        if item["type"] == "image":
            items.append({"type": "image", "data": item["data"], "mimeType": item["mimeType"]})
        elif item.get("console"):
            consoleLines.append(item["text"])
        else:
            index += 1
            items.append({"type": "text", "text": f"==> text {index}/{total} <==\n{item['text']}" if total > 1 else item["text"]})
    if len(consoleLines) > 0:
        joined = "\n".join(consoleLines)
        items.append({"type": "text", "text": f"<console_output>\n{joined}\n</console_output>"})
    return items


def joinAdjacentText(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join adjacent text items into one, each part starting on its own line. The separator check looks at the last
    non-empty part, not the joined text: reading the end of a growing concatenation is quadratic."""
    joined: list[dict[str, Any]] = []
    parts: list[str] = []
    lastPart = ""

    def flush() -> None:
        nonlocal parts, lastPart
        if len(parts) > 0:
            joined.append({"type": "text", "text": "".join(parts)})
        parts = []
        lastPart = ""

    for item in items:
        if item["type"] != "text":
            flush()
            joined.append(item)
            continue
        if lastPart != "" and not lastPart.endswith("\n"):
            parts.append("\n")
            lastPart = "\n"
        parts.append(item["text"])
        if item["text"] != "":
            lastPart = item["text"]
    flush()
    return joined


async def truncate(items: Sequence[dict[str, Any]], maxTokens: int, limits: dict[str, int], write: OutputFile) -> dict[str, Any]:
    """Apply the token budget: when the combined text exceeds it, the text items become one item that keeps the start
    and end of the text, and images follow it. The complete text is written to a temp file. ``{"items",
    "fullOutputPath"?}``."""
    texts = [item["text"] for item in items if item["type"] == "text"]
    combined = "\n".join(texts)
    # Within the limits too: a UTF-16 unit is at most 3 UTF-8 bytes, and the rest of the result is a few short lines.
    budget = min(maxTokens * CHARS_PER_TOKEN, limits["maxBytes"] // 4)
    lineBudget = (limits["maxLines"] - LINE_MARGIN) // 2
    length = jsLength(combined)
    lines = len(combined.split("\n"))
    if len(texts) == 0 or (length <= budget and lines <= 2 * lineBudget):
        return {"items": list(items)}
    headChars = budget // 2
    tailChars = budget - headChars
    head = "\n".join(jsSlice(combined, 0, headChars).split("\n")[:lineBudget])
    tail = "\n".join(jsSlice(combined, -tailChars).split("\n")[-lineBudget:]) if tailChars > 0 else ""
    removed = length - jsLength(head) - jsLength(tail)
    text = f"Warning: truncated output (original token count: {math.ceil(length / CHARS_PER_TOKEN)})\nTotal output lines: {lines}\n\n{head}…{math.ceil(removed / CHARS_PER_TOKEN)} tokens truncated…{tail}"
    spilled = await write("output.txt", combined)
    text += (
        f"\n\n[Full output: {spilled['path']} (read with offset/limit)]"
        if "path" in spilled
        else f"\n\n[Could not save the full output: {spilled['error']}]"
    )
    return {
        "items": [{"type": "text", "text": text}, *[item for item in items if item["type"] == "image"]],
        **({"fullOutputPath": spilled["path"]} if "path" in spilled else {}),
    }


async def saveImages(items: Sequence[dict[str, Any]], write: OutputFile) -> list[dict[str, Any]]:
    """Save each image to a file and put a text item with its path before it; an image shown twice is saved once."""
    labels: dict[str, asyncio.Future[str]] = {}

    async def label(image: dict[str, Any], index: int) -> str:
        data = base64.b64decode(image["data"])
        kind = f"{image['mimeType']}, {len(data)} bytes"
        # A failed write must not discard the result of a script whose calls already ran: it becomes part of the label.
        saved = await write(f"image-{index}{IMAGE_EXTENSIONS.get(image['mimeType'], '.bin')}", data)
        return f"[Image saved to {saved['path']} ({kind})]" if "path" in saved else f"[Image ({kind}) could not be saved: {saved['error']}]"

    async def expand(item: dict[str, Any]) -> list[dict[str, Any]]:
        if item["type"] != "image":
            return [item]
        pending = labels.get(item["data"])
        if pending is None:
            pending = asyncio.ensure_future(label(item, len(labels) + 1))
            labels[item["data"]] = pending
        return [{"type": "text", "text": await asyncio.shield(pending)}, item]

    result = await asyncio.gather(*(expand(item) for item in items))
    return [part for parts in result for part in parts]


def formatError(result: dict[str, Any], calls: Sequence[dict[str, Any]]) -> str:
    error = result["error"]
    kind = error["kind"]
    if kind == "script":
        head = nullishCoalesce(error.get("stack"), f"{nullishCoalesce(error.get('name'), 'Error')}: {error['message']}")
    elif kind == "timeout":
        head = f"Script timed out: {error['message']}"
    elif kind == "aborted":
        head = f"Script aborted: {error['message']}"
    else:
        head = f"Script sandbox failed: {error['message']}"
    summary = (
        "No tool calls were made."
        if len(calls) == 0
        else f"Tool calls made before the failure (they are not undone): {', '.join(f'{call['name']} ({call['status']})' for call in calls)}"
    )
    return f"{head}\n\n{summary}"


async def formatResult(result: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    """The tool result's content for a script's result, and the path of the complete text when it was cut:
    ``{"content", "fullOutputPath"?}``. ``options``: ``generatedImages`` (images ``models.generateImages()`` returned;
    when the script showed none, the result says so), ``wallMs``, ``maxOutputTokens``, ``calls``, ``limits``
    (``{"maxBytes", "maxLines"}``), ``write`` (an ``OutputFile``)."""
    output = list(result["output"])
    if result["ok"] and result.get("value", UNDEFINED) is not UNDEFINED:
        output.append({"type": "text", "text": valueText(result["value"])})
    items = formatItems(output)
    if not result["ok"]:
        items.append({"type": "text", "text": f"Script error:\n{formatError(result, options['calls'])}"})
    generated = options["generatedImages"]
    if generated > 0 and not any(item["type"] == "image" for item in items):
        items.append(
            {
                "type": "text",
                "text": f"Note: models.generateImages() returned {generated} image{'' if generated == 1 else 's'} that the script did not show. Show each image block of result.output with image(block).",
            }
        )
    truncated = await truncate(joinAdjacentText(items), options["maxOutputTokens"], options["limits"], options["write"])
    # After truncation, which joins the text items and moves images after them, so each path stays next to its image.
    content = joinAdjacentText(await saveImages(truncated["items"], options["write"]))
    header = f"{'Script completed' if result['ok'] else 'Script failed'}\nWall time {toFixed(options['wallMs'] / 1000, 1)} seconds\nOutput:\n"
    return {
        "content": [{"type": "text", "text": header}, *content],
        **({} if truncated.get("fullOutputPath") is None else {"fullOutputPath": truncated["fullOutputPath"]}),
    }


__all__ = [
    "CHARS_PER_TOKEN",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "IMAGE_EXTENSIONS",
    "LINE_MARGIN",
    "OutputFile",
    "formatError",
    "formatItems",
    "formatResult",
    "joinAdjacentText",
    "saveImages",
    "truncate",
    "valueText",
]

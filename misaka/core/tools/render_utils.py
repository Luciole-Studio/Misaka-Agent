"""Translated from pi's ``core/tools/render-utils.ts`` (v1.3.0).

MISAKA fork: renderer state is the dict ``ToolExecutionComponent`` keeps (``state["startedAt"]`` for
``state.startedAt``).
"""

from __future__ import annotations

import builtins
import math
import os
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypedDict

from misaka.ui.tui.terminal_image import (
    getCapabilities,
    getImageDimensions,
    hyperlink,
    imageFallback,
    pathToFileURL,
)
from misaka.utils.ansi import stripAnsi
from misaka.utils.js import (
    UNDEFINED,
    Interval,
    clearInterval,
    isNumber,
    jsLength,
    jsNumber,
    jsonStringify,
    jsSlice,
    jsString,
    jsTrim,
    setInterval,
    toFixed,
)
from misaka.utils.paths import resolvePath
from misaka.utils.shell import sanitizeBinaryOutput
from misaka.utils.values import read_field

if TYPE_CHECKING:
    from misaka.core.extensions.types import ToolRenderContext


def shortenPath(path: object) -> builtins.str:
    if not isinstance(path, builtins.str):
        return ""
    home = os.path.expanduser("~")
    if path.startswith(home):
        return f"~{path[len(home) :]}"
    return path


def linkPath(styledText: builtins.str, rawPath: builtins.str, cwd: builtins.str) -> builtins.str:
    if not getCapabilities().hyperlinks:
        return styledText
    absolutePath = resolvePath(rawPath, cwd)
    return hyperlink(styledText, pathToFileURL(absolutePath))


def str(value: object) -> builtins.str | None:
    if isinstance(value, builtins.str):
        return value
    if value is None or value is UNDEFINED:
        return ""
    return None


def _parseLineNumber(value: object) -> int | float | None:
    """pi's ``parseLineNumber``. A Python bool is not a number here, as a JavaScript boolean is not."""
    if isinstance(value, builtins.str) and jsTrim(value) != "":
        value = jsNumber(value)
    # `jsNumber` also turns an integer beyond the double range into JavaScript's Infinity.
    return value if isNumber(value) and math.isfinite(jsNumber(value)) else None


def formatReadLineRange(offset: object, limit: object) -> builtins.str:
    """Format finite numbers or numeric strings as a read line range, omitting malformed ranges."""
    # Strict schemas represent omitted optional arguments as null.
    if (offset is None or offset is UNDEFINED) and (limit is None or limit is UNDEFINED):
        return ""
    start = 1 if offset is None or offset is UNDEFINED else _parseLineNumber(offset)
    if start is None:
        return ""
    if limit is None or limit is UNDEFINED:
        return f":{jsString(start)}"
    count = _parseLineNumber(limit)
    if count is None:
        return ""
    end = start + count - 1
    return f":{jsString(start)}{f'-{jsString(end)}' if end else ''}"


def argOf(args: object, key: builtins.str) -> Any:
    """MISAKA fork: ``args?.<key>`` on tool arguments, which arrive as dicts (or models from Python callers):
    ``UNDEFINED`` when ``args`` is missing or lacks the key, ``None`` for a JSON null."""
    if args is None or args is UNDEFINED:
        return UNDEFINED
    if isinstance(args, Mapping):
        return args.get(key, UNDEFINED)
    return getattr(args, key, UNDEFINED)


def replaceTabs(text: builtins.str) -> builtins.str:
    return text.replace("\t", "   ")


def normalizeDisplayText(text: builtins.str) -> builtins.str:
    return text.replace("\r", "")


def getTextOutput(result: Any | None, showImages: bool) -> builtins.str:
    """MISAKA fork: ``result`` and its blocks are read with ``read_field`` (models or dicts)."""
    if not result:
        return ""

    content = read_field(result, "content") or []
    textBlocks = [c for c in content if read_field(c, "type") == "text"]
    imageBlocks = [c for c in content if read_field(c, "type") == "image"]

    output = "\n".join(
        sanitizeBinaryOutput(stripAnsi(read_field(c, "text") or "")).replace("\r", "") for c in textBlocks
    )

    caps = getCapabilities()
    if len(imageBlocks) > 0 and (not caps.images or not showImages):

        def indicator(img: Any) -> builtins.str:
            mimeType = read_field(img, "mimeType") or "image/unknown"
            data = read_field(img, "data")
            dims = getImageDimensions(data, read_field(img, "mimeType")) if data and read_field(img, "mimeType") else None
            return imageFallback(mimeType, dims)

        imageIndicators = "\n".join(indicator(img) for img in imageBlocks)
        output = f"{output}\n{imageIndicators}" if output else imageIndicators

    return output


# MISAKA: misaka/core/subagent imports this name, and that package is not edited.
get_text_output = getTextOutput


class ToolRenderResultLike[TDetails](TypedDict):
    content: list[Any]
    details: TDetails


COLLAPSED_ARGS_CHARS = 100


def _stringifyOrString(value: object, indent: int | None = None) -> builtins.str:
    """``JSON.stringify(value, null, indent) ?? String(value)``."""
    try:
        return jsonStringify(value, indent)
    except (TypeError, ValueError):
        return jsString(value)


def formatToolCallWithArgs(title: builtins.str, args: object, theme: Any, expanded: bool) -> builtins.str:
    """Generic tool call header: the title followed by the arguments. Collapsed, they are `key=value`
    pairs on the title line, cut to `COLLAPSED_ARGS_CHARS`. Expanded, each is a `key: value`
    line below the title, with strings shown raw and continuation lines indented.
    """
    header = theme.fg("toolTitle", theme.bold(title))
    if args is None:
        return header
    entries: list[tuple[builtins.str, object]] = (
        [(builtins.str(key), value) for key, value in args.items()] if isinstance(args, Mapping) else [("args", args)]
    )
    if len(entries) == 0:
        return header
    if expanded:
        lines: list[builtins.str] = []
        for key, value in entries:
            text = value if isinstance(value, builtins.str) else _stringifyOrString(value, 2)
            lines.append(f"  {key}: {'\n    '.join(replaceTabs(text).replace('\r', '').split('\n'))}")
        joined = "\n".join(lines)
        return f"{header}\n{theme.fg('muted', joined)}"
    pairs = " ".join(f"{key}={_stringifyOrString(value)}" for key, value in entries)
    preview = f"{jsSlice(pairs, 0, COLLAPSED_ARGS_CHARS - 3)}..." if jsLength(pairs) > COLLAPSED_ARGS_CHARS else pairs
    return f"{header} {theme.fg('muted', preview)}"


def formatToolDuration(ms: float) -> builtins.str:
    """Duration as `4.2s` below a minute, then `1m 30s` and `2h 3m 4s`."""
    seconds = ms / 1000
    if seconds < 60:
        return f"{toFixed(seconds, 1)}s"

    totalSeconds = math.floor(seconds)
    minutes = math.floor(totalSeconds / 60)
    remainder = totalSeconds % 60
    if minutes < 60:
        return f"{minutes}m {remainder}s"

    return f"{math.floor(minutes / 60)}h {minutes % 60}m {remainder}s"


class ToolTimerState(TypedDict, total=False):
    """Renderer state of `startToolTimer` and `toolTimerText`, kept in `ToolRenderContext.state`."""

    startedAt: int | None
    endedAt: int | None
    interval: Interval | None


def _now() -> int:
    """``Date.now()``"""
    return int(time.time() * 1000)


def startToolTimer(context: ToolRenderContext) -> None:
    """Start the row's clock once execution starts. Call from `renderCall`."""
    state: ToolTimerState = context.state
    if context.executionStarted and state.get("startedAt") is None:
        state["startedAt"] = _now()
        state["endedAt"] = None


def toolTimerText(context: ToolRenderContext, isPartial: bool) -> builtins.str | None:
    """The row's timing line, from `renderResult`: `Elapsed 4.2s` while the result is partial, redrawn
    every second, and `Took 4.2s` once it is final. None when the row has no start time and no
    recorded duration."""
    state: ToolTimerState = context.state
    if state.get("startedAt") is not None and isPartial and not state.get("interval"):
        state["interval"] = setInterval(lambda: context.invalidate(), 1000)
    if not isPartial or context.isError:
        if state.get("endedAt") is None:
            state["endedAt"] = _now()
        if state.get("interval"):
            clearInterval(state["interval"])
            state["interval"] = None
    # A final result's recorded duration wins: it is monotonic and survives reloads. The renderer's own clock is the
    # fallback for live progress and for results stored without one.
    if not isPartial and context.durationMs is not None:
        return f"Took {formatToolDuration(context.durationMs)}"
    startedAt = state.get("startedAt")
    if startedAt is None:
        return None
    endedAt = state.get("endedAt")
    endTime = endedAt if endedAt is not None else _now()
    return f"{'Elapsed' if isPartial else 'Took'} {formatToolDuration(endTime - startedAt)}"


def invalidArgText(theme: Any) -> builtins.str:
    return theme.fg("error", "[invalid arg]")


class RenderToolPathOptions(TypedDict, total=False):
    """MISAKA fork: a name for the inline type of ``renderToolPath``'s ``options``."""

    emptyFallback: builtins.str


def renderToolPath(
    rawPath: builtins.str | None,
    theme: Any,
    cwd: builtins.str,
    options: RenderToolPathOptions | None = None,
) -> builtins.str:
    if rawPath is None:
        return invalidArgText(theme)
    value = rawPath or (options.get("emptyFallback") if options is not None else None)
    if not value:
        return theme.fg("toolOutput", "...")
    return linkPath(theme.fg("accent", shortenPath(value)), value, cwd)

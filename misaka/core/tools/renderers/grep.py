"""Translated from pi's ``core/tools/renderers/grep.ts`` (v1.1.0).

Presentation for the grep tool.

Renderers live apart from the implementation so a process that only displays tool output does not
load the execution path or its typebox parameter schema. `grep.ts` spreads these into its
definition, so the tool's public shape is unchanged.
"""

from __future__ import annotations

from typing import Any, cast

from misaka.core.extensions.types import (
    ToolRenderContext,
    ToolRenderers,
    ToolRenderResultOptions,
)
from misaka.core.tools import render_utils
from misaka.core.tools.render_utils import (
    argOf,
    getTextOutput,
    invalidArgText,
    shortenPath,
)
from misaka.core.tools.truncate import DEFAULT_MAX_BYTES, formatSize
from misaka.modes.interactive.components.keybinding_hints import keyHint
from misaka.ui.tui import Text
from misaka.utils.js import UNDEFINED, jsString, jsTrim
from misaka.utils.values import read_field


def formatGrepCall(args: Any, theme: Any) -> str:
    pattern = render_utils.str(argOf(args, "pattern"))
    rawPath = render_utils.str(argOf(args, "path"))
    path = shortenPath(rawPath or ".") if rawPath is not None else None
    glob = render_utils.str(argOf(args, "glob"))
    limit = argOf(args, "limit")
    invalidArg = invalidArgText(theme)
    text = (
        theme.fg("toolTitle", theme.bold("grep"))
        + " "
        + (invalidArg if pattern is None else theme.fg("accent", f"/{pattern or ''}/"))
        + theme.fg("toolOutput", f" in {invalidArg if path is None else path}")
    )
    if glob:
        text += theme.fg("toolOutput", f" ({glob})")
    if limit is not UNDEFINED:
        text += theme.fg("toolOutput", f" limit {jsString(limit)}")
    return text


def formatGrepResult(result: Any, options: ToolRenderResultOptions, theme: Any, showImages: bool) -> str:
    output = jsTrim(getTextOutput(result, showImages))
    text = ""
    if output:
        lines = output.split("\n")
        maxLines = len(lines) if options["expanded"] else 15
        displayLines = lines[0:maxLines]
        remaining = len(lines) - maxLines
        text += "\n" + "\n".join(theme.fg("toolOutput", line) for line in displayLines)
        if remaining > 0:
            text += (
                f"{theme.fg('muted', f'\n... ({remaining} more lines,')} "
                f"{keyHint('app.tools.expand', 'to expand')}{theme.fg('muted', ')')}"
            )

    details = read_field(result, "details")
    matchLimit = read_field(details, "matchLimitReached")
    truncation = read_field(details, "truncation")
    linesTruncated = read_field(details, "linesTruncated")
    if matchLimit or read_field(truncation, "truncated") or linesTruncated:
        warnings: list[str] = []
        if matchLimit:
            warnings.append(f"{jsString(matchLimit)} matches limit")
        if read_field(truncation, "truncated"):
            warnings.append(f"{formatSize(read_field(truncation, 'maxBytes', DEFAULT_MAX_BYTES))} limit")
        if linesTruncated:
            warnings.append("some lines truncated")
        text += "\n" + theme.fg("warning", f"[Truncated: {', '.join(warnings)}]")
    return text


def renderCall(args: Any, theme: Any, context: ToolRenderContext) -> Text:
    text = cast(Text, context.lastComponent) if context.lastComponent is not None else Text("", 0, 0)
    text.setText(formatGrepCall(args, theme))
    return text


def renderResult(result: Any, options: ToolRenderResultOptions, theme: Any, context: ToolRenderContext) -> Text:
    text = cast(Text, context.lastComponent) if context.lastComponent is not None else Text("", 0, 0)
    text.setText(formatGrepResult(result, options, theme, context.showImages))
    return text


grepRenderers: ToolRenderers = {"renderCall": renderCall, "renderResult": renderResult}

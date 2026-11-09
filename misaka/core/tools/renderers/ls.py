"""Translated from pi's ``core/tools/renderers/ls.ts`` (v1.1.0).

Presentation for the ls tool.

Renderers live apart from the implementation so a process that only displays tool output does not
load the execution path or its typebox parameter schema. `ls.ts` spreads these into its
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
from misaka.core.tools.render_utils import argOf, getTextOutput, renderToolPath
from misaka.core.tools.truncate import DEFAULT_MAX_BYTES, formatSize
from misaka.modes.interactive.components.keybinding_hints import keyHint
from misaka.ui.tui import Text
from misaka.utils.js import UNDEFINED, jsString, jsTrim
from misaka.utils.values import read_field


def formatLsCall(args: Any, theme: Any, cwd: str) -> str:
    limit = argOf(args, "limit")
    pathDisplay = renderToolPath(render_utils.str(argOf(args, "path")), theme, cwd, {"emptyFallback": "."})
    text = f"{theme.fg('toolTitle', theme.bold('ls'))} {pathDisplay}"
    if limit is not UNDEFINED:
        text += theme.fg("toolOutput", f" (limit {jsString(limit)})")
    return text


def formatLsResult(result: Any, options: ToolRenderResultOptions, theme: Any, showImages: bool) -> str:
    output = jsTrim(getTextOutput(result, showImages))
    text = ""
    if output:
        lines = output.split("\n")
        maxLines = len(lines) if options["expanded"] else 20
        displayLines = lines[0:maxLines]
        remaining = len(lines) - maxLines
        text += "\n" + "\n".join(theme.fg("toolOutput", line) for line in displayLines)
        if remaining > 0:
            text += (
                f"{theme.fg('muted', f'\n... ({remaining} more lines,')} "
                f"{keyHint('app.tools.expand', 'to expand')}{theme.fg('muted', ')')}"
            )

    details = read_field(result, "details")
    entryLimit = read_field(details, "entryLimitReached")
    truncation = read_field(details, "truncation")
    if entryLimit or read_field(truncation, "truncated"):
        warnings: list[str] = []
        if entryLimit:
            warnings.append(f"{jsString(entryLimit)} entries limit")
        if read_field(truncation, "truncated"):
            warnings.append(f"{formatSize(read_field(truncation, 'maxBytes', DEFAULT_MAX_BYTES))} limit")
        text += "\n" + theme.fg("warning", f"[Truncated: {', '.join(warnings)}]")
    return text


def renderCall(args: Any, theme: Any, context: ToolRenderContext) -> Text:
    text = cast(Text, context.lastComponent) if context.lastComponent is not None else Text("", 0, 0)
    text.setText(formatLsCall(args, theme, context.cwd))
    return text


def renderResult(result: Any, options: ToolRenderResultOptions, theme: Any, context: ToolRenderContext) -> Text:
    text = cast(Text, context.lastComponent) if context.lastComponent is not None else Text("", 0, 0)
    text.setText(formatLsResult(result, options, theme, context.showImages))
    return text


lsRenderers: ToolRenderers = {"renderCall": renderCall, "renderResult": renderResult}

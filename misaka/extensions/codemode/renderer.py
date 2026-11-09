"""Presentation for the codemode tool.

The call shows the script; the result lists the nested tool calls with their status as they run and
the cost of its model calls, followed by the script output without the "Script completed" header,
and the time the script has been running (``Elapsed``) or took (``Took``), like bash rows. Nested
calls are not separate tool rows because they never reach the model as tool calls.

Translated from pi's ``coding-agent/src/extensions/codemode/renderer.ts``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from misaka.core.tools import render_utils
from misaka.core.tools.render_utils import (
    getTextOutput,
    replaceTabs,
    startToolTimer,
    toolTimerText,
)
from misaka.ui.tui import Container, Spacer, Text
from misaka.utils.js import (
    UNDEFINED,
    jsLength,
    jsRound,
    jsSlice,
    jsTrim,
    jsTrimEnd,
    toFixed,
    toPrecision,
)
from misaka.utils.values import read_field

CODE_PREVIEW_LINES = 10
CALL_PREVIEW_COUNT = 8
OUTPUT_PREVIEW_LINES = 5
COLLAPSED_ARGS_CHARS = 80
SCRIPT_HEADER = re.compile(r"^Script (completed|failed)\nWall time [0-9.]+ seconds\nOutput:\n\Z")


def expandHint(theme: Any, hidden: int, noun: str) -> str:
    from misaka.modes.interactive.components.keybinding_hints import keyHint

    return f"{theme.fg('muted', f'... ({hidden} more {noun},')} {keyHint('app.tools.expand', 'to expand')}{theme.fg('muted', ')')}"


def formatDuration(ms: float | None) -> str:
    if ms is None:
        return ""
    return f"{jsRound(ms)}ms" if ms < 1000 else f"{toFixed(ms / 1000, 1)}s"


def formatCost(cost: float) -> str:
    """Cents for larger amounts, two significant digits for the fractions of a cent classifier calls
    cost."""
    return f"${toFixed(cost, 2)}" if cost >= 0.01 else f"${toPrecision(cost, 2)}"


def statusIcon(call: Any, theme: Any) -> str:
    match read_field(call, "status"):
        case "running":
            return theme.fg("warning", "…")
        case "ok":
            return theme.fg("success", "✓")
        case "error":
            return theme.fg("error", "✗")
        case "cancelled":
            return theme.fg("muted", "⊘")
    return UNDEFINED  # type: ignore[return-value]


def formatCall(call: Any, theme: Any, expanded: bool) -> str:
    callArgs = read_field(call, "args") or ""
    args = (
        f"{jsSlice(callArgs, 0, COLLAPSED_ARGS_CHARS - 3)}..."
        if not expanded and jsLength(callArgs) > COLLAPSED_ARGS_CHARS
        else callArgs
    )
    duration = formatDuration(read_field(call, "durationMs"))
    line = f"{statusIcon(call, theme)} {theme.fg('toolTitle', read_field(call, 'name'))}"
    if args:
        line += f" {theme.fg('muted', args)}"
    if duration:
        line += f" {theme.fg('dim', duration)}"
    cost = read_field(call, "cost")
    if cost:
        line += f" {theme.fg('dim', formatCost(cost))}"
    error = read_field(call, "error")
    if expanded and error:
        line += "\n    " + theme.fg("error", "\n    ".join(error.split("\n")))
    return line


def renderCall(args: Any, theme: Any, context: Any) -> Any:
    from misaka.modes.interactive.components.visual_truncate import (
        VisualLinePreview,
        VisualLinePreviewOptions,
    )
    from misaka.modes.interactive.theme.theme import highlightCode

    startToolTimer(context)
    # The code includes the `// @options:` line, so options show as part of the script.
    code = render_utils.str(read_field(args, "code")) if args is not None else render_utils.str(None)
    title = theme.fg("toolTitle", theme.bold("codemode"))
    component = context.lastComponent if context.lastComponent is not None else Container()
    component.clear()
    if code is None:
        component.addChild(Text(f"{title} {theme.fg('error', '[invalid arg]')}", 0, 0))  # type: ignore[abstract]
        return component
    component.addChild(Text(title, 0, 0))  # type: ignore[abstract]
    if code:
        source = replaceTabs(jsTrimEnd(code.replace("\r", "")))
        highlighted = "\n".join(highlightCode(source, "javascript"))
        component.addChild(  # type: ignore[arg-type]
            Text(highlighted, 0, 0)  # type: ignore[abstract, arg-type]
            if context.expanded
            else VisualLinePreview(
                VisualLinePreviewOptions(
                    text=highlighted,
                    maxVisualLines=CODE_PREVIEW_LINES,
                    keep="start",
                    formatHint=lambda hidden: expandHint(theme, hidden, "lines"),
                )
            )
        )
    return component


def renderResult(result: Any, options: Any, theme: Any, context: Any) -> Any:
    from misaka.modes.interactive.components.keybinding_hints import keyHint
    from misaka.modes.interactive.components.visual_truncate import (
        VisualLinePreview,
        VisualLinePreviewOptions,
    )

    expanded = bool(read_field(options, "expanded"))
    timerText = toolTimerText(context, bool(read_field(options, "isPartial")))
    component = context.lastComponent if context.lastComponent is not None else Container()
    component.clear()
    details = read_field(result, "details")
    calls: Sequence[Any] = (read_field(details, "calls") if details is not None else None) or []
    if len(calls) > 0:
        shown = calls if expanded else calls[-CALL_PREVIEW_COUNT:]
        lines = [formatCall(call, theme, expanded) for call in shown]
        if len(shown) < len(calls):
            lines.insert(
                0,
                f"{theme.fg('muted', f'... ({len(calls) - len(shown)} earlier calls,')} "
                f"{keyHint('app.tools.expand', 'to expand')}{theme.fg('muted', ')')}",
            )
        # Collapsed rows hide earlier calls, so the total covers every call.
        priced = [call for call in calls if read_field(call, "cost")]
        if len(priced) > 1:
            # Added left to right, as `reduce` does; `sum()` compensates its rounding.
            total = 0
            for call in priced:
                total += read_field(call, "cost") or 0
            lines.append(theme.fg("muted", f"Model calls: {formatCost(total)}"))
        component.addChild(Spacer(1))  # type: ignore[abstract]
        component.addChild(Text("\n".join(lines), 0, 0))  # type: ignore[abstract]

    # Drop the "Script completed\nWall time ...\nOutput:\n" header. Rejected input (invalid options)
    # has no header.
    content = list(read_field(result, "content") or [])
    first = content[0] if content else None
    hasHeader = first is not None and read_field(first, "type") == "text" and SCRIPT_HEADER.match(read_field(first, "text")) is not None
    output = (
        ""
        if read_field(options, "isPartial")
        else jsTrim(getTextOutput({"content": content[1:] if hasHeader else content}, context.showImages))
    )
    if output:
        color = "error" if context.isError else "toolOutput"
        styled = "\n".join(theme.fg(color, line) for line in replaceTabs(output).split("\n"))
        component.addChild(Spacer(1))  # type: ignore[abstract]
        if expanded:
            component.addChild(Text(styled, 0, 0))  # type: ignore[abstract]
        else:
            # Limit wrapped lines, not logical ones: script output is often one long JSON line.
            component.addChild(  # type: ignore[arg-type]
                VisualLinePreview(  # type: ignore[arg-type]
                    VisualLinePreviewOptions(
                        text=styled,
                        maxVisualLines=OUTPUT_PREVIEW_LINES,
                        keep="start",
                        formatHint=lambda hidden: expandHint(theme, hidden, "lines"),
                    )
                )
            )
            # The collapsed preview hides the truncation notice at the end, so name the file here.
            fullOutputPath = read_field(details, "fullOutputPath") if details is not None else None
            if fullOutputPath:
                component.addChild(Text(theme.fg("muted", f"Full output: {fullOutputPath}"), 0, 0))  # type: ignore[abstract]
    if timerText:
        component.addChild(Spacer(1))  # type: ignore[abstract]
        component.addChild(Text(theme.fg("muted", timerText), 0, 0))  # type: ignore[abstract]
    return component


codemodeRenderers: dict[str, Any] = {"renderCall": renderCall, "renderResult": renderResult}


__all__ = ["codemodeRenderers"]

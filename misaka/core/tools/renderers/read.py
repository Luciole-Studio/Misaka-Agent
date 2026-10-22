"""Translated from pi's ``core/tools/renderers/read.ts`` (v1.3.0).

Presentation for the read tool.

Renderers live apart from the implementation so a process that only displays tool output does not
load the execution path or its typebox parameter schema. `read.ts` spreads these into its
definition, so the tool's public shape is unchanged.

MISAKA fork: reads of pi's own documentation (``getPiDocsClassification``: the package README, docs/ and examples/) are
not compacted; MISAKA's installed package ships no such documentation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal, cast

from misaka.core.extensions.types import (
    ToolRenderContext,
    ToolRenderers,
    ToolRenderResultOptions,
)
from misaka.core.tools import render_utils
from misaka.core.tools.path_utils import resolveToCwd
from misaka.core.tools.render_utils import (
    argOf,
    formatReadLineRange,
    getTextOutput,
    renderToolPath,
    replaceTabs,
)
from misaka.core.tools.truncate import DEFAULT_MAX_BYTES, DEFAULT_MAX_LINES, formatSize
from misaka.modes.interactive.components.keybinding_hints import keyHint, keyText
from misaka.modes.interactive.theme.theme import getLanguageFromPath, highlightCode
from misaka.ui.tui import Text
from misaka.utils.js import UNDEFINED, jsString
from misaka.utils.paths import formatPathRelativeToCwdOrAbsolute
from misaka.utils.values import read_field


@dataclass(slots=True)
class CompactReadClassification:
    kind: Literal["docs", "resource", "skill"]
    label: str


COMPACT_RESOURCE_FILE_NAMES = frozenset({"AGENTS.override.md", "AGENTS.md", "AGENTS.MD", "CLAUDE.md", "CLAUDE.MD"})


def _rawPathOf(args: Any) -> str | None:
    """``str(args?.file_path ?? args?.path)``"""
    filePath = argOf(args, "file_path")
    return render_utils.str(filePath if filePath is not None and filePath is not UNDEFINED else argOf(args, "path"))


def styledReadLineRange(args: Any, theme: Any) -> str:
    range = formatReadLineRange(argOf(args, "offset"), argOf(args, "limit"))
    return theme.fg("warning", range) if range else ""


def formatReadCall(args: Any, theme: Any, cwd: str) -> str:
    pathDisplay = renderToolPath(_rawPathOf(args), theme, cwd)
    return f"{theme.fg('toolTitle', theme.bold('read'))} {pathDisplay}{styledReadLineRange(args, theme)}"


def trimTrailingEmptyLines(lines: list[str]) -> list[str]:
    end = len(lines)
    while end > 0 and lines[end - 1] == "":
        end -= 1
    return lines[0:end]


def getCompactReadClassification(args: Any, cwd: str) -> CompactReadClassification | None:
    rawPath = _rawPathOf(args)
    if not rawPath:
        return None

    absolutePath = resolveToCwd(rawPath, cwd)
    fileName = os.path.basename(absolutePath)
    if fileName == "SKILL.md":
        return CompactReadClassification(kind="skill", label=os.path.basename(os.path.dirname(absolutePath)) or fileName)

    # MISAKA fork: no pi documentation classification (see the module docstring).

    if fileName in COMPACT_RESOURCE_FILE_NAMES:
        return CompactReadClassification(
            kind="resource", label=formatPathRelativeToCwdOrAbsolute(absolutePath, cwd)
        )

    return None


def formatCompactReadCall(classification: CompactReadClassification, args: Any, theme: Any) -> str:
    expandHint = theme.fg("dim", f" ({keyText('app.tools.expand')} to expand)")
    if classification.kind == "skill":
        return (
            theme.fg("customMessageLabel", "\x1b[1m[skill]\x1b[22m ")
            + theme.fg("customMessageText", classification.label)
            + styledReadLineRange(args, theme)
            + expandHint
        )

    return (
        theme.fg("toolTitle", theme.bold(f"read {classification.kind}"))
        + " "
        + theme.fg("accent", classification.label)
        + styledReadLineRange(args, theme)
        + expandHint
    )


def formatReadResult(
    args: Any,
    result: Any,
    options: ToolRenderResultOptions,
    theme: Any,
    showImages: bool,
    _cwd: str,
    isError: bool,
) -> str:
    if not options["expanded"] and not isError:
        return ""

    rawPath = _rawPathOf(args)
    output = getTextOutput(result, showImages)
    lang = getLanguageFromPath(rawPath) if not isError and rawPath else None
    renderedLines = highlightCode(replaceTabs(output), lang) if lang else output.split("\n")
    lines = trimTrailingEmptyLines(renderedLines)
    maxLines = len(lines) if options["expanded"] else 10
    displayLines = lines[0:maxLines]
    remaining = len(lines) - maxLines
    text = "\n" + "\n".join(
        replaceTabs(line) if lang else theme.fg("toolOutput", replaceTabs(line)) for line in displayLines
    )
    if remaining > 0:
        text += (
            f"{theme.fg('muted', f'\n... ({remaining} more lines,')} "
            f"{keyHint('app.tools.expand', 'to expand')}{theme.fg('muted', ')')}"
        )

    truncation = read_field(read_field(result, "details"), "truncation")
    if read_field(truncation, "truncated"):
        if read_field(truncation, "firstLineExceedsLimit"):
            text += "\n" + theme.fg(
                "warning",
                f"[First line exceeds {formatSize(read_field(truncation, 'maxBytes', DEFAULT_MAX_BYTES))} limit]",
            )
        elif read_field(truncation, "truncatedBy") == "lines":
            text += "\n" + theme.fg(
                "warning",
                f"[Truncated: showing {jsString(read_field(truncation, 'outputLines'))} of "
                f"{jsString(read_field(truncation, 'totalLines'))} lines "
                f"({jsString(read_field(truncation, 'maxLines', DEFAULT_MAX_LINES))} line limit)]",
            )
        else:
            text += "\n" + theme.fg(
                "warning",
                f"[Truncated: {jsString(read_field(truncation, 'outputLines'))} lines shown "
                f"({formatSize(read_field(truncation, 'maxBytes', DEFAULT_MAX_BYTES))} limit)]",
            )
    return text


def renderCall(rawArgs: Any, theme: Any, context: ToolRenderContext) -> Text:
    args = rawArgs
    text = cast(Text, context.lastComponent) if context.lastComponent is not None else Text("", 0, 0)
    classification = getCompactReadClassification(args, context.cwd) if not context.expanded else None
    text.setText(
        formatCompactReadCall(classification, args, theme) if classification else formatReadCall(args, theme, context.cwd)
    )
    return text


def renderResult(result: Any, options: ToolRenderResultOptions, theme: Any, context: ToolRenderContext) -> Text:
    text = cast(Text, context.lastComponent) if context.lastComponent is not None else Text("", 0, 0)
    text.setText(
        formatReadResult(
            context.args,
            result,
            options,
            theme,
            context.showImages,
            context.cwd,
            context.isError,
        )
    )
    return text


readRenderers: ToolRenderers = {"renderCall": renderCall, "renderResult": renderResult}

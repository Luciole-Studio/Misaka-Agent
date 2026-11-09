"""Translated from pi's ``core/tools/renderers/write.ts`` (v1.1.0).

Presentation for the write tool.

Renderers live apart from the implementation so a process that only displays tool output does not
load the execution path or its typebox parameter schema. `write.ts` spreads these into its
definition, so the tool's public shape is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from misaka.core.extensions.types import (
    ToolRenderContext,
    ToolRenderers,
    ToolRenderResultOptions,
)
from misaka.core.tools import render_utils
from misaka.core.tools.render_utils import (
    argOf,
    normalizeDisplayText,
    renderToolPath,
    replaceTabs,
)
from misaka.modes.interactive.components.keybinding_hints import keyHint
from misaka.modes.interactive.theme.theme import getLanguageFromPath, highlightCode
from misaka.ui.tui import Container, Text
from misaka.utils.js import UNDEFINED
from misaka.utils.values import read_field


@dataclass(slots=True)
class WriteHighlightCache:
    rawPath: str | None
    lang: str
    rawContent: str
    normalizedLines: list[str]
    highlightedLines: list[str]


class WriteCallRenderComponent(Text):
    def __init__(self) -> None:
        super().__init__("", 0, 0)
        self.cache: WriteHighlightCache | None = None


WRITE_PARTIAL_FULL_HIGHLIGHT_LINES = 50


def highlightSingleLine(line: str, lang: str) -> str:
    highlighted = highlightCode(line, lang)
    return highlighted[0] if len(highlighted) > 0 else ""


def refreshWriteHighlightPrefix(cache: WriteHighlightCache) -> None:
    prefixCount = min(WRITE_PARTIAL_FULL_HIGHLIGHT_LINES, len(cache.normalizedLines))
    if prefixCount == 0:
        return
    prefixSource = "\n".join(cache.normalizedLines[0:prefixCount])
    prefixHighlighted = highlightCode(prefixSource, cache.lang)
    for i in range(prefixCount):
        cache.highlightedLines[i] = (
            prefixHighlighted[i]
            if i < len(prefixHighlighted)
            else highlightSingleLine(cache.normalizedLines[i] if i < len(cache.normalizedLines) else "", cache.lang)
        )


def rebuildWriteHighlightCacheFull(rawPath: str | None, fileContent: str) -> WriteHighlightCache | None:
    lang = getLanguageFromPath(rawPath) if rawPath else None
    if not lang:
        return None
    displayContent = normalizeDisplayText(fileContent)
    normalized = replaceTabs(displayContent)
    return WriteHighlightCache(
        rawPath=rawPath,
        lang=lang,
        rawContent=fileContent,
        normalizedLines=normalized.split("\n"),
        highlightedLines=highlightCode(normalized, lang),
    )


def updateWriteHighlightCacheIncremental(
    cache: WriteHighlightCache | None,
    rawPath: str | None,
    fileContent: str,
) -> WriteHighlightCache | None:
    lang = getLanguageFromPath(rawPath) if rawPath else None
    if not lang:
        return None
    if not cache:
        return rebuildWriteHighlightCacheFull(rawPath, fileContent)
    if cache.lang != lang or cache.rawPath != rawPath:
        return rebuildWriteHighlightCacheFull(rawPath, fileContent)
    if not fileContent.startswith(cache.rawContent):
        return rebuildWriteHighlightCacheFull(rawPath, fileContent)
    if len(fileContent) == len(cache.rawContent):
        return cache

    deltaRaw = fileContent[len(cache.rawContent) :]
    deltaDisplay = normalizeDisplayText(deltaRaw)
    deltaNormalized = replaceTabs(deltaDisplay)
    cache.rawContent = fileContent
    if len(cache.normalizedLines) == 0:
        cache.normalizedLines.append("")
        cache.highlightedLines.append("")

    segments = deltaNormalized.split("\n")
    lastIndex = len(cache.normalizedLines) - 1
    cache.normalizedLines[lastIndex] += segments[0]
    cache.highlightedLines[lastIndex] = highlightSingleLine(cache.normalizedLines[lastIndex], cache.lang)
    for i in range(1, len(segments)):
        cache.normalizedLines.append(segments[i])
        cache.highlightedLines.append(highlightSingleLine(segments[i], cache.lang))
    refreshWriteHighlightPrefix(cache)
    return cache


def trimTrailingEmptyLines(lines: list[str]) -> list[str]:
    end = len(lines)
    while end > 0 and lines[end - 1] == "":
        end -= 1
    return lines[0:end]


def _rawPathOf(args: Any) -> str | None:
    """``str(args?.file_path ?? args?.path)``"""
    filePath = argOf(args, "file_path")
    return render_utils.str(filePath if filePath is not None and filePath is not UNDEFINED else argOf(args, "path"))


def formatWriteCall(
    args: Any,
    options: ToolRenderResultOptions,
    theme: Any,
    cache: WriteHighlightCache | None,
    cwd: str,
) -> str:
    rawPath = _rawPathOf(args)
    fileContent = render_utils.str(argOf(args, "content"))
    pathDisplay = renderToolPath(rawPath, theme, cwd)
    text = f"{theme.fg('toolTitle', theme.bold('write'))} {pathDisplay}"

    if fileContent is None:
        text += f"\n\n{theme.fg('error', '[invalid content arg - expected string]')}"
    elif fileContent:
        lang = getLanguageFromPath(rawPath) if rawPath else None
        renderedLines = (
            (
                cache.highlightedLines
                if cache is not None
                else highlightCode(replaceTabs(normalizeDisplayText(fileContent)), lang)
            )
            if lang
            else normalizeDisplayText(fileContent).split("\n")
        )
        lines = trimTrailingEmptyLines(renderedLines)
        totalLines = len(lines)
        maxLines = len(lines) if options["expanded"] else 10
        displayLines = lines[0:maxLines]
        remaining = len(lines) - maxLines
        text += "\n\n" + "\n".join(line if lang else theme.fg("toolOutput", replaceTabs(line)) for line in displayLines)
        if remaining > 0:
            text += (
                f"{theme.fg('muted', f'\n... ({remaining} more lines, {totalLines} total,')} "
                f"{keyHint('app.tools.expand', 'to expand')}{theme.fg('muted', ')')}"
            )

    return text


def formatWriteResult(result: Any, isError: bool, theme: Any) -> str | None:
    """MISAKA fork: ``isError`` is a parameter (upstream spreads it into ``result``)."""
    if not isError:
        return None
    output = "\n".join(
        read_field(c, "text") or "" for c in read_field(result, "content") or [] if read_field(c, "type") == "text"
    )
    if not output:
        return None
    return f"\n{theme.fg('error', output)}"


def renderCall(args: Any, theme: Any, context: ToolRenderContext) -> Text:
    renderArgs = args
    rawPath = _rawPathOf(renderArgs)
    fileContent = render_utils.str(argOf(renderArgs, "content"))
    component = (
        cast(WriteCallRenderComponent, context.lastComponent)
        if context.lastComponent is not None
        else WriteCallRenderComponent()
    )
    if fileContent is not None:
        component.cache = (
            rebuildWriteHighlightCacheFull(rawPath, fileContent)
            if context.argsComplete
            else updateWriteHighlightCacheIncremental(component.cache, rawPath, fileContent)
        )
    else:
        component.cache = None
    component.setText(
        formatWriteCall(
            renderArgs,
            {"expanded": context.expanded, "isPartial": context.isPartial},
            theme,
            component.cache,
            context.cwd,
        )
    )
    return component


def renderResult(result: Any, _options: ToolRenderResultOptions, theme: Any, context: ToolRenderContext) -> Any:
    output = formatWriteResult(result, context.isError, theme)
    if not output:
        component = cast(Container, context.lastComponent) if context.lastComponent is not None else Container()
        component.clear()
        return component
    text = cast(Text, context.lastComponent) if context.lastComponent is not None else Text("", 0, 0)
    text.setText(output)
    return text


writeRenderers: ToolRenderers = {"renderCall": renderCall, "renderResult": renderResult}

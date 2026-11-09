"""Translated from pi's ``modes/interactive/components/mermaid.ts`` (v1.1.0)."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from misaka.core.extensions.types import MarkdownTransformContext, MarkdownTransformer
from misaka.core.settings_manager import MermaidRenderingMode
from misaka.modes.interactive.theme.theme import Theme
from misaka.ui.tui import Marked
from misaka.ui.tui.vendor.grok_mermaid import MermaidArt, Span, render
from misaka.utils.js import jsSplitWhitespace, jsTrim

markdownParser = Marked()


@dataclass
class MermaidTransformerOptions:
    getMode: Callable[[], MermaidRenderingMode]
    theme: Theme | None = None


def isMermaid(token: Any) -> bool:
    return token.type == "code" and token.lang is not None and jsSplitWhitespace(jsTrim(token.lang), 1)[0].lower() == "mermaid"


def codeSpan(line: str) -> str:
    # Encode each diagram row as inline code (` ... `) so Markdown preserves its spacing and
    # box-drawing characters. Use a non-breaking space for blank rows because an
    # empty code span has no visible height.
    content = line or " "
    # CommonMark code spans use matching backtick delimiters, so choose one
    # longer than any backtick run in the content (``hel`lo`` -> <code>hel`lo</code>).
    # If the content starts or ends with a backtick, separating it from the
    # delimiter with a space keeps that backtick as content; CommonMark removes
    # the padding when rendering (`` `edge` `` -> <code>`edge`</code>).
    # Mermaid labels can preserve backticks, for example:
    #   `┌──────────────┐    ┌──────────────┐`
    # ```│ plain ` tick ├───▶│ two `` ticks │```
    #   `└──────────────┘    └──────────────┘`
    longestBacktickRun = max([0, *(len(match[0]) for match in re.finditer(r"`+", content))])
    fence = "`" * (longestBacktickRun + 1)
    padding = " " if content.startswith("`") or content.endswith("`") else ""
    return f"{fence}{padding}{content}{padding}{fence}"


def styleSpan(span: Span, theme: Theme) -> str:
    match span.cls:
        case "border":
            return theme.fg("borderMuted", span.text)
        case "text":
            return theme.fg("text", span.text)
        case "edge":
            return theme.fg("accent", span.text)
        case "edgeLabel":
            return theme.fg("muted", span.text)
        case "title":
            return theme.fg("accent", theme.bold(span.text))
        case "none":
            return span.text


def themedLines(art: MermaidArt, theme: Theme) -> list[str]:
    return ["".join(styleSpan(span, theme) for span in row) for row in art.styled]


def createMermaidMarkdownTransformer(options: MermaidTransformerOptions) -> MarkdownTransformer:
    """Create a transformer that replaces top-level Mermaid code blocks with Unicode terminal diagrams."""

    def transform(markdown: str, context: MarkdownTransformContext) -> str:
        mode = options.getMode()
        if (
            mode == "off"
            or context["messageType"] == "assistant-thinking"
            or (context["isStreaming"] and mode != "streaming")
        ):
            return markdown

        def replace(token: Any) -> str:
            if not isMermaid(token):
                return token.raw
            art = render(token.text)
            if not art or art.width > context["availableWidth"]:
                return token.raw
            if not context["isStreaming"] and len(art.warnings) > 0:
                suffix = f" (+{len(art.warnings) - 1} more)" if len(art.warnings) > 1 else ""
                warning = f"Mermaid diagram not rendered: {art.warnings[0]}{suffix}"
                styledWarning = options.theme.fg("warning", warning) if options.theme else warning
                return f"{token.raw}\n{codeSpan(styledWarning)}  \n"
            lines = themedLines(art, options.theme) if options.theme else art.plain
            # Markdown hard breaks keep every diagram row on its own line.
            return "  \n".join(map(codeSpan, lines)) + "\n"

        return "".join(replace(token) for token in markdownParser.lexer(markdown))

    return transform


__all__ = ["MermaidTransformerOptions", "createMermaidMarkdownTransformer"]

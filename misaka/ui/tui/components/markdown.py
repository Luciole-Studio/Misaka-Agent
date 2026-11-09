"""Translated from pi-tui's ``components/markdown.ts`` (v1.1.0), on the vendored marked lexer.

MISAKA forks, each marked where it happens: C0/C1 control characters are dropped before lexing; the token cache is a
strong reference (``WeakRef`` would be collected as soon as ``render`` returns under CPython's reference counting).
"""

from __future__ import annotations

import copy
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypedDict

from misaka.ui.tui.latex import RenderLatexOptions, renderLatex
from misaka.ui.tui.terminal_image import getCapabilities, hyperlink, isImageLine
from misaka.ui.tui.tui import Component
from misaka.ui.tui.utils import (
    applyBackgroundToLine,
    flattenLines,
    visibleWidth,
    wrapTextWithAnsi,
)
from misaka.ui.tui.vendor.marked import Marked, Token, Tokenizer
from misaka.utils import js_regexp
from misaka.utils.js import jsIndex, jsTrim
from misaka.utils.js_regexp import RegExp

type StyleFn = Callable[[str], str]

STRICT_STRIKETHROUGH_REGEX = RegExp(r"^(~~)(?=[^\s~])((?:\\.|[^\\])*?(?:\\.|[^\s~\\]))\1(?=[^~]|$)")


class StrictStrikethroughTokenizer(Tokenizer):
    def del_(self, src: str, *_args: Any) -> Token | None:
        match = STRICT_STRIKETHROUGH_REGEX.exec(src)
        if not match:
            return None

        text = match[2]
        return Token(
            type="del",
            raw=match[0],
            text=text,
            tokens=self.lexer.inlineTokens(text),
        )


def isEscaped(source: str, index: int) -> bool:
    backslashes = 0
    position = index - 1
    while position >= 0 and source[position] == "\\":
        backslashes += 1
        position -= 1
    return backslashes % 2 == 1


def findClosingDelimiter(source: str, closing: str, start: int) -> int:
    index = source.find(closing, start)
    while index >= 0 and isEscaped(source, index):
        index = source.find(closing, index + len(closing))
    return index


def looksLikePendingDollarMath(source: str) -> bool:
    return RegExp(r"\\[A-Za-z]+|[_^=+*/<>()[\]|±≤≥≠≈∈→⇒∞∫∑√-]").test(source)


def tokenizeInlineLatex(source: str) -> Token | None:
    opening = ""
    closing = ""
    if source.startswith("$$"):
        opening = "$$"
        closing = "$$"
    elif source.startswith("\\("):
        opening = "\\("
        closing = "\\)"
    elif source.startswith("\\["):
        opening = "\\["
        closing = "\\]"
    elif source.startswith("$") and not RegExp(r"^\$\s").test(source):
        opening = "$"
        closing = "$"
    else:
        return None

    closingIndex = findClosingDelimiter(source, closing, len(opening))
    if (
        closingIndex >= 0
        and opening == "$"
        and (
            RegExp(r"\s$").test(source[len(opening) : closingIndex])
            or RegExp(r"^\d").test(source[closingIndex + 1 :])
            or (
                RegExp(r"^[A-Z_][A-Z0-9_]*(?:[^A-Za-z0-9_\s])?$").test(source[len(opening) : closingIndex])
                and RegExp(r"^[A-Za-z_][A-Za-z0-9_]*").test(source[closingIndex + 1 :])
            )
            or "`" in source[len(opening) : closingIndex]
        )
    ):
        return None

    if closingIndex < 0:
        pendingSource = source[len(opening) :]
        if opening.startswith("\\") or looksLikePendingDollarMath(pendingSource):
            return Token(type="latex", raw=source, text=pendingSource, pending=True)
        return None

    text = source[len(opening) : closingIndex]
    if not text or "\n" in text:
        return None

    raw = source[: closingIndex + len(closing)]
    return Token(type="latex", raw=raw, text=text)


def tokenizeBlockLatex(source: str) -> Token | None:
    dollarMatch = RegExp(r"^ {0,3}\$\$[ \t]*(?:\n)?([\s\S]*?)\$\$[ \t]*(?:\n|$)").exec(source)
    if dollarMatch and dollarMatch[1]:
        return Token(type="latexBlock", raw=dollarMatch[0], text=jsTrim(dollarMatch[1]))

    bracketMatch = RegExp(r"^ {0,3}\\\[[ \t]*(?:\n)?([\s\S]*?)\\\][ \t]*(?:\n|$)").exec(source)
    if bracketMatch and bracketMatch[1]:
        return Token(type="latexBlock", raw=bracketMatch[0], text=jsTrim(bracketMatch[1]))

    pendingBracket = RegExp(r"^ {0,3}\\\[[ \t]*(?:\n)?([\s\S]*)$").exec(source)
    if pendingBracket:
        return Token(type="latexBlock", raw=pendingBracket[0], text=pendingBracket[1], pending=True)
    pendingDollar = RegExp(r"^ {0,3}\$\$[ \t]*(?:\n)?([\s\S]*)$").exec(source)
    if pendingDollar and pendingDollar[1] and looksLikePendingDollarMath(pendingDollar[1]):
        return Token(type="latexBlock", raw=pendingDollar[0], text=pendingDollar[1], pending=True)
    return None


def _latexBlockStart(source: str) -> int | None:
    match = RegExp(r"(?:^|\n) {0,3}(?:\$\$|\\\[)").exec(source)
    return match.index + (1 if match[0].startswith("\n") else 0) if match else None


def _latexInlineStart(source: str) -> int | None:
    indices = [index for index in [source.find("$"), source.find("\\("), source.find("\\[")] if index >= 0]
    return min(indices) if len(indices) > 0 else None


LATEX_MARKDOWN_EXTENSIONS: list[dict[str, Any]] = [
    {
        "name": "latexBlock",
        "level": "block",
        "start": _latexBlockStart,
        "tokenizer": tokenizeBlockLatex,
    },
    {
        "name": "latex",
        "level": "inline",
        "start": _latexInlineStart,
        "tokenizer": tokenizeInlineLatex,
    },
]


def trimPartialClosingFences(tokens: list[Any]) -> None:
    token = jsIndex(tokens, len(tokens) - 1)
    if token is not None and token.type == "list":
        lastItem = jsIndex(token.items, len(token.items) - 1)
        trimPartialClosingFences((lastItem.tokens if lastItem is not None else None) or [])
        return
    if token is not None and token.type == "blockquote":
        trimPartialClosingFences(token.tokens or [])
        return
    if token is None or token.type != "code":
        return

    # Trim streamed partial closing fences so code blocks do not shrink/flicker
    # when the final fence character arrives. See https://github.com/earendil-works/pi/issues/5825.
    markerMatch = RegExp(r"^(`{3,}|~{3,})").exec(token.raw)
    marker = markerMatch[1] if markerMatch else None
    lastLine = token.raw.split("\n")[-1]
    if not marker or not lastLine or len(lastLine) >= len(marker) or lastLine != marker[0] * len(lastLine):
        return

    token.text = js_regexp.replace(token.text[: -len(lastLine)], RegExp(r"\n$"), "")


markdownParser = Marked()
markdownParser.setOptions({"tokenizer": StrictStrikethroughTokenizer()})
markdownParser.use({"extensions": [*LATEX_MARKDOWN_EXTENSIONS]})


@dataclass(slots=True)
class DefaultTextStyle:
    """Default text styling for markdown content.
    Applied to all text unless overridden by markdown formatting.
    """

    color: StyleFn | None = None
    """Foreground color function"""
    bgColor: StyleFn | None = None
    """Background color function"""
    bold: bool = False
    """Bold text"""
    italic: bool = False
    """Italic text"""
    strikethrough: bool = False
    """Strikethrough text"""
    underline: bool = False
    """Underline text"""


@dataclass(slots=True)
class MarkdownTheme:
    """Theme functions for markdown elements.
    Each function takes text and returns styled text with ANSI codes.
    """

    heading: StyleFn
    link: StyleFn
    linkUrl: StyleFn
    code: StyleFn
    codeBlock: StyleFn
    codeBlockBorder: StyleFn
    quote: StyleFn
    quoteBorder: StyleFn
    hr: StyleFn
    listBullet: StyleFn
    bold: StyleFn
    italic: StyleFn
    strikethrough: StyleFn
    underline: StyleFn
    highlightCode: Callable[[str, str | None], list[str]] | None = None
    codeBlockIndent: str | None = None
    """Prefix applied to each rendered code block line (default: "  ")"""


@dataclass(slots=True)
class MarkdownOptions:
    preserveOrderedListMarkers: bool | None = None
    """Preserve source list markers instead of normalizing them."""
    preserveBackslashEscapes: bool | None = None
    """Preserve source backslash escapes instead of normalizing escaped punctuation."""
    transform: Callable[[str, int], str] | None = None
    """Transform source Markdown before parsing, with the exact width available for content."""
    renderLatex: bool | None = None
    """Render supported LaTeX math expressions as Unicode text (default: true)."""


@dataclass(slots=True)
class InlineStyleContext:
    applyText: StyleFn
    stylePrefix: str


class _CachedTokens(TypedDict):
    source: str
    tokens: list[Any]


# MISAKA fork: model and user prose reaches the terminal through this renderer, and a C0 control character in it is
# written verbatim (an `\x1b[10A` in a reply moves the cursor and breaks the renderer's line accounting). C1 goes with
# them: a UTF-8 terminal still reads U+009B as CSI and U+009C as ST. Tab, newline and carriage return stay.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


class Markdown(Component):
    def __init__(
        self,
        text: str,
        paddingX: int,
        paddingY: int,
        theme: MarkdownTheme,
        defaultTextStyle: DefaultTextStyle | None = None,
        options: MarkdownOptions | None = None,
    ) -> None:
        self.defaultStylePrefix: str | None = None

        # Cache for rendered output
        self.cachedText: str | None = None
        self.cachedWidth: int | None = None
        self.cachedLines: list[str] | None = None
        # Parsed tokens depend only on the source, so they survive theme and width invalidation.
        # MISAKA fork: held strongly (see the module docstring).
        self.cachedTokens: _CachedTokens | None = None

        self.text = text
        self.paddingX = paddingX  # Left/right padding
        self.paddingY = paddingY  # Top/bottom padding
        self.theme = theme
        self.defaultTextStyle = defaultTextStyle
        self.options = copy.copy(options) if options else MarkdownOptions()

    def setText(self, text: str) -> None:
        self.text = text
        self.invalidate()

    def invalidate(self) -> None:
        self.cachedText = None
        self.cachedWidth = None
        self.cachedLines = None

    def render(self, width: int) -> list[str]:
        # Check cache
        if self.cachedLines is not None and self.cachedText == self.text and self.cachedWidth == width:
            return self.cachedLines

        # Calculate available width for content (subtract horizontal padding)
        contentWidth = max(1, width - self.paddingX * 2)
        transformed = self.options.transform(self.text, contentWidth) if self.options.transform else None
        text = transformed if transformed is not None else self.text

        # Don't render anything if there's no actual text
        if not text or jsTrim(text) == "":
            result: list[str] = []
            # Update cache
            self.cachedText = self.text
            self.cachedWidth = width
            self.cachedLines = result
            return result

        # Replace tabs with 3 spaces for consistent rendering
        normalizedText = _CONTROL_CHARACTERS.sub("", text.replace("\t", "   "))

        # Parse markdown to HTML-like tokens
        cached = self.cachedTokens
        tokens = cached["tokens"] if cached is not None and cached["source"] == normalizedText else None
        if not tokens:
            tokens = markdownParser.lexer(normalizedText)
            trimPartialClosingFences(tokens)
            self.cachedTokens = {"source": normalizedText, "tokens": tokens}

        # Convert tokens to styled terminal output
        renderedLines: list[str] = []

        for i in range(len(tokens)):
            token = tokens[i]
            nextToken = jsIndex(tokens, i + 1)
            tokenLines = self.renderToken(token, contentWidth, nextToken.type if nextToken is not None else None)
            for tokenLine in tokenLines:
                renderedLines.append(tokenLine)  # noqa: PERF402 - upstream loop

        # Wrap lines (NO padding, NO background yet)
        wrappedLines: list[str] = []
        for line in renderedLines:
            if isImageLine(line):
                wrappedLines.append(line)
            else:
                for wrappedLine in wrapTextWithAnsi(line, contentWidth):
                    wrappedLines.append(wrappedLine)  # noqa: PERF402 - upstream loop

        # Add margins and background to each wrapped line
        leftMargin = " " * self.paddingX
        rightMargin = " " * self.paddingX
        bgFn = self.defaultTextStyle.bgColor if self.defaultTextStyle else None
        contentLines: list[str] = []

        for line in wrappedLines:
            if isImageLine(line):
                contentLines.append(line)
                continue

            lineWithMargins = leftMargin + line + rightMargin

            if bgFn:
                contentLines.append(applyBackgroundToLine(lineWithMargins, width, bgFn))
            else:
                # No background - just pad to width
                visibleLen = visibleWidth(lineWithMargins)
                paddingNeeded = max(0, width - visibleLen)
                contentLines.append(lineWithMargins + " " * paddingNeeded)

        # Add top/bottom padding (empty lines)
        emptyLine = " " * width
        emptyLines: list[str] = []
        for _i in range(self.paddingY):
            line = applyBackgroundToLine(emptyLine, width, bgFn) if bgFn else emptyLine
            emptyLines.append(line)

        # Combine top padding, content, and bottom padding
        result = [*emptyLines, *contentLines, *emptyLines]
        flattenLines(result)

        # Update cache
        self.cachedText = self.text
        self.cachedWidth = width
        self.cachedLines = result

        return result if len(result) > 0 else [""]

    def applyDefaultStyle(self, text: str) -> str:
        """Apply default text style to a string.
        This is the base styling applied to all text content.
        NOTE: Background color is NOT applied here - it's applied at the padding stage
        to ensure it extends to the full line width.
        """
        if not self.defaultTextStyle:
            return text

        styled = text

        # Apply foreground color (NOT background - that's applied at padding stage)
        if self.defaultTextStyle.color:
            styled = self.defaultTextStyle.color(styled)

        # Apply text decorations using this.theme
        if self.defaultTextStyle.bold:
            styled = self.theme.bold(styled)
        if self.defaultTextStyle.italic:
            styled = self.theme.italic(styled)
        if self.defaultTextStyle.strikethrough:
            styled = self.theme.strikethrough(styled)
        if self.defaultTextStyle.underline:
            styled = self.theme.underline(styled)

        return styled

    def getDefaultStylePrefix(self) -> str:
        if not self.defaultTextStyle:
            return ""

        if self.defaultStylePrefix is not None:
            return self.defaultStylePrefix

        sentinel = "\u0000"
        styled = sentinel

        if self.defaultTextStyle.color:
            styled = self.defaultTextStyle.color(styled)

        if self.defaultTextStyle.bold:
            styled = self.theme.bold(styled)
        if self.defaultTextStyle.italic:
            styled = self.theme.italic(styled)
        if self.defaultTextStyle.strikethrough:
            styled = self.theme.strikethrough(styled)
        if self.defaultTextStyle.underline:
            styled = self.theme.underline(styled)

        sentinelIndex = styled.find(sentinel)
        self.defaultStylePrefix = styled[:sentinelIndex] if sentinelIndex >= 0 else ""
        return self.defaultStylePrefix

    def getStylePrefix(self, styleFn: StyleFn) -> str:
        sentinel = "\u0000"
        styled = styleFn(sentinel)
        sentinelIndex = styled.find(sentinel)
        return styled[:sentinelIndex] if sentinelIndex >= 0 else ""

    def getDefaultInlineStyleContext(self) -> InlineStyleContext:
        return InlineStyleContext(applyText=lambda text: self.applyDefaultStyle(text), stylePrefix=self.getDefaultStylePrefix())

    def renderToken(
        self,
        token: Any,
        width: int,
        nextTokenType: str | None = None,
        styleContext: InlineStyleContext | None = None,
    ) -> list[str]:
        lines: list[str] = []

        match token.type:
            case "heading":
                headingLevel = token.depth
                headingPrefix = f"{'#' * headingLevel} "

                # Build a heading-specific style context so inline tokens (codespan, bold, etc.)
                # restore heading styling after their own ANSI resets instead of falling back to
                # the default text style.
                headingStyleFn: StyleFn
                if headingLevel == 1:
                    headingStyleFn = lambda text: self.theme.heading(self.theme.bold(self.theme.underline(text)))
                else:
                    headingStyleFn = lambda text: self.theme.heading(self.theme.bold(text))

                headingStyleContext = InlineStyleContext(
                    applyText=headingStyleFn,
                    stylePrefix=self.getStylePrefix(headingStyleFn),
                )

                headingText = self.renderInlineTokens(token.tokens or [], headingStyleContext)
                styledHeading = headingStyleFn(headingPrefix) + headingText if headingLevel >= 3 else headingText
                lines.append(styledHeading)
                if nextTokenType and nextTokenType != "space":
                    lines.append("")  # Add spacing after headings (unless space token follows)

            case "paragraph":
                paragraphText = self.renderInlineTokens(token.tokens or [], styleContext)
                lines.append(paragraphText)
                # Don't add spacing if next token is space or list
                if nextTokenType and nextTokenType != "list" and nextTokenType != "space":
                    lines.append("")

            case "text":
                lines.append(self.renderInlineTokens([token], styleContext))

            case "latexBlock":
                latexToken = token
                displayed = (
                    renderLatex(latexToken.text, RenderLatexOptions(display=True))
                    if not latexToken.pending and self.options.renderLatex is not False
                    else None
                )
                rendered = displayed if displayed is not None else jsTrim(latexToken.raw)
                for line in rendered.split("\n"):
                    lines.append(self.applyDefaultStyle(line))
                if nextTokenType and nextTokenType != "space":
                    lines.append("")

            case "code":
                indent = self.theme.codeBlockIndent if self.theme.codeBlockIndent is not None else "  "
                lines.append(self.theme.codeBlockBorder(f"```{token.lang or ''}"))
                if self.theme.highlightCode:
                    highlightedLines = self.theme.highlightCode(token.text, token.lang)
                    for hlLine in highlightedLines:
                        lines.append(f"{indent}{hlLine}")
                else:
                    # Split code by newlines and style each line
                    codeLines = token.text.split("\n")
                    for codeLine in codeLines:
                        lines.append(f"{indent}{self.theme.codeBlock(codeLine)}")
                lines.append(self.theme.codeBlockBorder("```"))
                if nextTokenType and nextTokenType != "space":
                    lines.append("")  # Add spacing after code blocks (unless space token follows)

            case "list":
                listLines = self.renderList(token, 0, width, styleContext)
                lines.extend(listLines)
                # Don't add spacing after lists if a space token follows
                # (the space token will handle it)

            case "table":
                tableLines = self.renderTable(token, width, nextTokenType, styleContext)
                lines.extend(tableLines)

            case "blockquote":

                def quoteStyle(text: str) -> str:
                    return self.theme.quote(self.theme.italic(text))

                quoteStylePrefix = self.getStylePrefix(quoteStyle)

                def applyQuoteStyle(line: str) -> str:
                    if not quoteStylePrefix:
                        return quoteStyle(line)
                    lineWithReappliedStyle = line.replace("\x1b[0m", f"\x1b[0m{quoteStylePrefix}")
                    return quoteStyle(lineWithReappliedStyle)

                # Calculate available width for quote content (subtract border "│ " = 2 chars)
                quoteContentWidth = max(1, width - 2)

                # Blockquotes contain block-level tokens (paragraph, list, code, etc.), so render
                # children with renderToken() instead of renderInlineTokens().
                # Default message style should not apply inside blockquotes.
                quoteInlineStyleContext = InlineStyleContext(applyText=lambda text: text, stylePrefix=quoteStylePrefix)
                quoteTokens = token.tokens or []
                renderedQuoteLines: list[str] = []
                for i in range(len(quoteTokens)):
                    quoteToken = quoteTokens[i]
                    nextQuoteToken = jsIndex(quoteTokens, i + 1)
                    renderedQuoteLines.extend(
                        self.renderToken(
                            quoteToken,
                            quoteContentWidth,
                            nextQuoteToken.type if nextQuoteToken is not None else None,
                            quoteInlineStyleContext,
                        )
                    )

                # Avoid rendering an extra empty quote line before the outer blockquote spacing.
                while len(renderedQuoteLines) > 0 and renderedQuoteLines[-1] == "":
                    renderedQuoteLines.pop()

                for quoteLine in renderedQuoteLines:
                    styledLine = applyQuoteStyle(quoteLine)
                    wrappedLines = wrapTextWithAnsi(styledLine, quoteContentWidth)
                    for wrappedLine in wrappedLines:
                        lines.append(self.theme.quoteBorder("│ ") + wrappedLine)
                if nextTokenType and nextTokenType != "space":
                    lines.append("")  # Add spacing after blockquotes (unless space token follows)

            case "hr":
                lines.append(self.theme.hr("─" * min(width, 80)))
                if nextTokenType and nextTokenType != "space":
                    lines.append("")  # Add spacing after horizontal rules (unless space token follows)

            case "html":
                # Render HTML as plain text (escaped for terminal)
                if "raw" in token and isinstance(token.raw, str):
                    lines.append(self.applyDefaultStyle(jsTrim(token.raw)))

            case "space":
                # Space tokens represent blank lines in markdown
                lines.append("")

            case _:
                # Handle any other token types as plain text
                if "text" in token and isinstance(token.text, str):
                    lines.append(token.text)

        return lines

    def renderInlineTokens(self, tokens: list[Any], styleContext: InlineStyleContext | None = None) -> str:
        result = ""
        resolvedStyleContext = styleContext if styleContext is not None else self.getDefaultInlineStyleContext()
        applyText, stylePrefix = resolvedStyleContext.applyText, resolvedStyleContext.stylePrefix

        def applyTextWithNewlines(text: str) -> str:
            segments = text.split("\n")
            return "\n".join(applyText(segment) for segment in segments)

        for token in tokens:
            match token.type:
                case "latex":
                    latexToken = token
                    inline = (
                        renderLatex(latexToken.text)
                        if not latexToken.pending and self.options.renderLatex is not False
                        else None
                    )
                    rendered = inline if inline is not None else latexToken.raw
                    result += applyTextWithNewlines(rendered)

                case "escape":
                    result += applyTextWithNewlines(token.raw if self.options.preserveBackslashEscapes else token.text)

                case "text":
                    # Text tokens in list items can have nested tokens for inline formatting
                    if token.tokens and len(token.tokens) > 0:
                        result += self.renderInlineTokens(token.tokens, resolvedStyleContext)
                    else:
                        result += applyTextWithNewlines(token.text)

                case "paragraph":
                    # Paragraph tokens contain nested inline tokens
                    result += self.renderInlineTokens(token.tokens or [], resolvedStyleContext)

                case "strong":
                    boldContent = self.renderInlineTokens(token.tokens or [], resolvedStyleContext)
                    result += self.theme.bold(boldContent) + stylePrefix

                case "em":
                    italicContent = self.renderInlineTokens(token.tokens or [], resolvedStyleContext)
                    result += self.theme.italic(italicContent) + stylePrefix

                case "codespan":
                    result += self.theme.code(token.text) + stylePrefix

                case "link":
                    linkText = self.renderInlineTokens(token.tokens or [], resolvedStyleContext)
                    styledLink = self.theme.link(self.theme.underline(linkText))
                    if getCapabilities().hyperlinks:
                        # OSC 8: render as a clickable hyperlink. The URL is not printed inline,
                        # so we always show only the link text regardless of whether it matches href.
                        result += hyperlink(styledLink, token.href) + stylePrefix
                    else:
                        # Fallback: print URL in parentheses when text differs from href.
                        # Compare raw token.text (not styled) against href for the equality check.
                        # For mailto: links strip the prefix (autolinked emails use text="foo@bar.com"
                        # but href="mailto:foo@bar.com").
                        hrefForComparison = token.href.removeprefix("mailto:")
                        if token.text == token.href or token.text == hrefForComparison:
                            result += styledLink + stylePrefix
                        else:
                            result += styledLink + self.theme.linkUrl(f" ({token.href})") + stylePrefix

                case "br":
                    result += "\n"

                case "del":
                    delContent = self.renderInlineTokens(token.tokens or [], resolvedStyleContext)
                    result += self.theme.strikethrough(delContent) + stylePrefix

                case "html":
                    # Render inline HTML as plain text
                    if "raw" in token and isinstance(token.raw, str):
                        result += applyTextWithNewlines(token.raw)

                case _:
                    # Handle any other inline token types as plain text
                    if "text" in token and isinstance(token.text, str):
                        result += applyTextWithNewlines(token.text)

        while stylePrefix and result.endswith(stylePrefix):
            result = result[: -len(stylePrefix)]

        return result

    def getOrderedListMarker(self, item: Any) -> str | None:
        match = RegExp(r"^(?: {0,3})(\d{1,9}[.)])[ \t]+").exec(item.raw)
        return f"{match[1]} " if match else None

    def getUnorderedListMarker(self, item: Any) -> str | None:
        match = RegExp(r"^(?: {0,3})([-+*])(?:[ \t]+|(?=\r?\n|$))").exec(item.raw)
        return f"{match[1]} " if match else None

    def renderList(self, token: Any, depth: int, width: int, styleContext: InlineStyleContext | None = None) -> list[str]:
        """Render a list with proper nesting support"""
        lines: list[str] = []
        indent = "    " * depth
        # Use the list's start property (defaults to 1 for ordered lists)
        startNumber = token.start if isinstance(token.start, int | float) and not isinstance(token.start, bool) else 1

        for i in range(len(token.items)):
            item = token.items[i]
            isLastItem = i == len(token.items) - 1
            orderedMarker = self.getOrderedListMarker(item) if token.ordered and self.options.preserveOrderedListMarkers else None
            unorderedMarker = (
                self.getUnorderedListMarker(item) if not token.ordered and self.options.preserveOrderedListMarkers else None
            )
            bullet = (
                (orderedMarker if orderedMarker is not None else f"{startNumber + i}. ")
                if token.ordered
                else (unorderedMarker if unorderedMarker is not None else "- ")
            )
            taskMarker = f"[{'x' if item.checked else ' '}] " if item.task else ""
            marker = bullet + taskMarker
            firstPrefix = indent + self.theme.listBullet(marker)
            continuationPrefix = indent + " " * visibleWidth(marker)
            itemWidth = max(1, width - visibleWidth(firstPrefix))
            renderedAnyLine = False

            for itemToken in item.tokens:
                if itemToken.type == "list":
                    lines.extend(self.renderList(itemToken, depth + 1, width, styleContext))
                    renderedAnyLine = True
                    continue

                itemLines = self.renderToken(itemToken, itemWidth, None, styleContext)
                for line in itemLines:
                    for wrappedLine in wrapTextWithAnsi(line, itemWidth):
                        linePrefix = continuationPrefix if renderedAnyLine else firstPrefix
                        lines.append(linePrefix + wrappedLine)
                        renderedAnyLine = True

            if not renderedAnyLine:
                lines.append(firstPrefix)

            if token.loose and not isLastItem:
                lines.append("")

        return lines

    def getLongestWordWidth(self, text: str, maxWidth: int | None = None) -> int:
        """Get the visible width of the longest word in a string."""
        words = [word for word in js_regexp.split(text, RegExp(r"\s+")) if len(word) > 0]
        longest = 0
        for word in words:
            longest = max(longest, visibleWidth(word))
        if maxWidth is None:
            return longest
        return min(longest, maxWidth)

    def wrapCellText(self, text: str, maxWidth: int, stylePrefix: str = "") -> list[str]:
        """Wrap a table cell to fit into a column.

        Delegates to wrapTextWithAnsi() so ANSI codes + long tokens are handled
        consistently with the rest of the renderer.
        """
        lines = wrapTextWithAnsi(text, max(1, maxWidth))
        wrapped: list[str] = []
        for index, line in enumerate(lines):
            # Reset text styles after each non-final fragment, then restore the surrounding style before padding and borders.
            styleReset = "\x1b[22;23;24;25;27;28;29;39m" if index < len(lines) - 1 else ""
            wrapped.append(f"{line}{styleReset}{stylePrefix}")
        return wrapped

    def renderTable(
        self,
        token: Any,
        availableWidth: int,
        nextTokenType: str | None = None,
        styleContext: InlineStyleContext | None = None,
    ) -> list[str]:
        """Render a table with width-aware cell wrapping.
        Cells that don't fit are wrapped to multiple lines.
        """
        lines: list[str] = []
        numCols = len(token.header)

        if numCols == 0:
            return lines

        # Calculate border overhead: "│ " + (n-1) * " │ " + " │"
        # = 2 + (n-1) * 3 + 2 = 3n + 1
        borderOverhead = 3 * numCols + 1
        availableForCells = availableWidth - borderOverhead
        if availableForCells < numCols:
            # Too narrow to render a stable table. Fall back to raw markdown.
            fallbackLines = wrapTextWithAnsi(token.raw, availableWidth) if token.raw else []
            if nextTokenType and nextTokenType != "space":
                fallbackLines.append("")
            return fallbackLines

        maxUnbrokenWordWidth = 30

        # Calculate natural column widths (what each column needs without constraints)
        naturalWidths: list[int] = [0] * numCols
        minWordWidths: list[int] = [0] * numCols
        for i in range(numCols):
            headerText = self.renderInlineTokens(token.header[i].tokens or [], styleContext)
            naturalWidths[i] = visibleWidth(headerText)
            minWordWidths[i] = max(1, self.getLongestWordWidth(headerText, maxUnbrokenWordWidth))
        for row in token.rows:
            for i in range(len(row)):
                cellText = self.renderInlineTokens(row[i].tokens or [], styleContext)
                naturalWidths[i] = max(naturalWidths[i] or 0, visibleWidth(cellText))
                minWordWidths[i] = max(minWordWidths[i] or 1, self.getLongestWordWidth(cellText, maxUnbrokenWordWidth))

        minColumnWidths = minWordWidths
        minCellsWidth = sum(minColumnWidths)

        if minCellsWidth > availableForCells:
            minColumnWidths = [1] * numCols
            remaining = availableForCells - numCols

            if remaining > 0:
                totalWeight = sum(max(0, width - 1) for width in minWordWidths)
                growth = [
                    math.floor((max(0, width - 1) / totalWeight) * remaining) if totalWeight > 0 else 0
                    for width in minWordWidths
                ]

                for i in range(numCols):
                    minColumnWidths[i] += jsIndex(growth, i) or 0

                allocated = sum(growth)
                leftover = remaining - allocated
                i = 0
                while leftover > 0 and i < numCols:
                    minColumnWidths[i] += 1
                    leftover -= 1
                    i += 1

            minCellsWidth = sum(minColumnWidths)

        # Calculate column widths that fit within available width
        totalNaturalWidth = sum(naturalWidths) + borderOverhead
        columnWidths: list[int]

        if totalNaturalWidth <= availableWidth:
            # Everything fits naturally
            columnWidths = [max(width, minColumnWidths[index]) for index, width in enumerate(naturalWidths)]
        else:
            # Need to shrink columns to fit
            totalGrowPotential = sum(max(0, width - minColumnWidths[index]) for index, width in enumerate(naturalWidths))
            extraWidth = max(0, availableForCells - minCellsWidth)
            columnWidths = []
            for index, minWidth in enumerate(minColumnWidths):
                naturalWidth = naturalWidths[index]
                minWidthDelta = max(0, naturalWidth - minWidth)
                grow = 0
                if totalGrowPotential > 0:
                    grow = math.floor((minWidthDelta / totalGrowPotential) * extraWidth)
                columnWidths.append(minWidth + grow)

            # Adjust for rounding errors - distribute remaining space
            allocated = sum(columnWidths)
            remaining = availableForCells - allocated
            while remaining > 0:
                grew = False
                i = 0
                while i < numCols and remaining > 0:
                    if columnWidths[i] < naturalWidths[i]:
                        columnWidths[i] += 1
                        remaining -= 1
                        grew = True
                    i += 1
                if not grew:
                    break

        # Render top border
        topBorderCells = ["─" * w for w in columnWidths]
        lines.append(f"┌─{'─┬─'.join(topBorderCells)}─┐")

        # Render header with wrapping
        stylePrefix = styleContext.stylePrefix if styleContext is not None else ""
        headerCellLines: list[list[str]] = [
            self.wrapCellText(self.renderInlineTokens(cell.tokens or [], styleContext), columnWidths[i], stylePrefix)
            for i, cell in enumerate(token.header)
        ]
        headerLineCount = max(len(c) for c in headerCellLines)

        for lineIdx in range(headerLineCount):
            rowParts = []
            for colIdx, cellLines in enumerate(headerCellLines):
                text = jsIndex(cellLines, lineIdx) or ""
                padded = text + " " * max(0, columnWidths[colIdx] - visibleWidth(text))
                rowParts.append(self.theme.bold(padded))
            lines.append(f"│ {' │ '.join(rowParts)} │")

        # Render separator
        separatorCells = ["─" * w for w in columnWidths]
        separatorLine = f"├─{'─┼─'.join(separatorCells)}─┤"
        lines.append(separatorLine)

        # Render rows with wrapping
        for rowIndex in range(len(token.rows)):
            row = token.rows[rowIndex]
            rowCellLines: list[list[str]] = [
                self.wrapCellText(self.renderInlineTokens(cell.tokens or [], styleContext), columnWidths[i], stylePrefix)
                for i, cell in enumerate(row)
            ]
            rowLineCount = max(len(c) for c in rowCellLines)

            for lineIdx in range(rowLineCount):
                rowParts = []
                for colIdx, cellLines in enumerate(rowCellLines):
                    text = jsIndex(cellLines, lineIdx) or ""
                    rowParts.append(text + " " * max(0, columnWidths[colIdx] - visibleWidth(text)))
                lines.append(f"│ {' │ '.join(rowParts)} │")

            if rowIndex < len(token.rows) - 1:
                lines.append(separatorLine)

        # Render bottom border
        bottomBorderCells = ["─" * w for w in columnWidths]
        lines.append(f"└─{'─┴─'.join(bottomBorderCells)}─┘")

        if nextTokenType and nextTokenType != "space":
            lines.append("")  # Add spacing after table
        return lines


__all__ = ["DefaultTextStyle", "InlineStyleContext", "Markdown", "MarkdownOptions", "MarkdownTheme"]

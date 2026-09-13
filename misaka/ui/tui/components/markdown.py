"""ANSI-aware Markdown renderer for terminal output."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode


def _render_latex_or_none(source: str):
    from misaka.ui.tui.latex import render_latex

    return render_latex(source)

from misaka.ui.tui.terminal_image import getCapabilities, hyperlink, isImageLine
from misaka.ui.tui.tui import Component
from misaka.ui.tui.utils import applyBackgroundToLine, visibleWidth, wrapTextWithAnsi

type StyleFn = Callable[[str], str]
type HighlightCodeFn = Callable[[str, str | None], list[str]]
type MarkdownTransform = Callable[[str, int], str]

# Everything this renderer is handed is model or user prose. A C0 control character in it
# reaches the terminal verbatim — nothing downstream filters it (`normalize_terminal_output`
# only touches tabs and Thai/Lao vowels), so an `\x1b[10A` in an assistant reply really does
# move the cursor and scramble the diff renderer's line accounting. Newline and carriage
# return stay; the tab is already expanded before this runs.
# C1 (U+0080-U+009F) goes with them: in a UTF-8 terminal U+009B is still read as CSI and
# U+009C as ST by xterm and friends, so a lone U+009C inside an autolinked URL closes the
# OSC 8 hyperlink early and the rest of the sequence lands on screen as text
# (audit 2026-09-02, ui-tui-core-09).
_C0_CONTROLS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

_BARE_URL_RE = r"(?:https?://|www\.)[^\s<>\x00-\x1f\x7f-\x9f]+"
_EMAIL_RE = r"(?<![A-Za-z0-9.+-])[A-Za-z0-9.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9.-])"
_AUTOLINK_RE = re.compile(f"(?P<url>{_BARE_URL_RE})|(?P<email>{_EMAIL_RE})")


def _trim_bare_url(url: str) -> str:
    """GFM-style trailing trim: strip trailing punctuation; strip a closing paren only when unbalanced (so wiki links ending in (x) survive)."""
    while url:
        ch = url[-1]
        if ch in ".,!?:;\"'*_~]" or ch == ")" and url.count("(") < url.count(")"):
            url = url[:-1]
        else:
            break
    return url

try:  # LaTeX math detection (parity with pi 05e89b4; uses the official markdown-it plugin instead of a hand-written tokenizer)
    from mdit_py_plugins.dollarmath import dollarmath_plugin

    _MARKDOWN_PARSER = (
        MarkdownIt("commonmark").enable("table").enable("strikethrough")
        .use(dollarmath_plugin, allow_space=False, double_inline=True)
    )
except ImportError:  # plugin missing: show math as source text rather than breaking markdown
    _MARKDOWN_PARSER = MarkdownIt("commonmark").enable("table").enable("strikethrough")

_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_PARTIAL_FENCE_RE = re.compile(r"^ {0,3}(`{1,2}|~{1,2})\s*$")


def _trim_partial_closing_fence(text: str) -> str:
    """Anti-flicker for streamed output (pi issue #5825): if the last line is a half-typed
    closing fence (1-2 backticks) inside an unclosed code block, cut it off first; otherwise
    it renders as code content and vanishes on the next frame."""
    if not text.endswith(("`", "~")):
        return text
    lines = text.split("\n")
    if not _PARTIAL_FENCE_RE.match(lines[-1]):
        return text
    open_fence = None
    for line in lines[:-1]:
        m = _FENCE_OPEN_RE.match(line)
        if not m:
            continue
        marker = m.group(1)
        if open_fence is None:
            open_fence = marker
        elif marker[0] == open_fence[0] and len(marker) >= len(open_fence):
            open_fence = None
    if open_fence is None:
        return text
    return "\n".join(lines[:-1]).rstrip("\n")


@dataclass(slots=True)
class DefaultTextStyle:
    color: StyleFn | None = None
    bgColor: StyleFn | None = None
    bold: bool = False
    italic: bool = False
    strikethrough: bool = False
    underline: bool = False


@dataclass(slots=True)
class MarkdownTheme:
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
    highlightCode: HighlightCodeFn | None = None
    codeBlockIndent: str | None = None


@dataclass(slots=True)
class InlineStyleContext:
    applyText: StyleFn
    stylePrefix: str


class Markdown(Component):
    def __init__(
        self,
        text: str,
        paddingX: int,
        paddingY: int,
        theme: MarkdownTheme,
        defaultTextStyle: DefaultTextStyle | None = None,
        transform: MarkdownTransform | None = None,
    ) -> None:
        self.text = text
        self.paddingX = paddingX
        self.paddingY = paddingY
        self.theme = theme
        self.defaultTextStyle = defaultTextStyle
        self.transform = transform
        self.defaultStylePrefix: str | None = None
        self.cachedText: str | None = None
        self.cachedWidth: int | None = None
        self.cachedLines: list[str] | None = None
        self._sourceLines: list[str] = []
        self._parsed: tuple[str, SyntaxTreeNode] | None = None

    def setText(self, text: str) -> None:
        self.text = text
        self.invalidate()

    def invalidate(self) -> None:
        self.cachedText = None
        self.cachedWidth = None
        self.cachedLines = None
        self.defaultStylePrefix = None

    def render(self, width: int) -> list[str]:
        if self.cachedLines is not None and self.cachedText == self.text and self.cachedWidth == width:
            return self.cachedLines

        content_width = max(1, width - self.paddingX * 2)
        text = self.transform(self.text, content_width) if self.transform is not None else self.text
        if not text or text.strip() == "":
            result: list[str] = []
            self.cachedText = self.text
            self.cachedWidth = width
            self.cachedLines = result
            return result

        normalized_text = _C0_CONTROLS_RE.sub("", text.replace("\t", "   "))
        normalized_text = _trim_partial_closing_fence(normalized_text)
        self._sourceLines = normalized_text.split("\n")
        if self._parsed is None or self._parsed[0] != normalized_text:
            # PORT-NOTE: pi lexes on every render (marked is fast). A width change re-renders
            # every message of a transcript, and markdown-it in Python was most of that time,
            # so the tree is kept per text. Rendering only reads it; the one place that wrote
            # to it (_takeTaskMarker) remembers what it took.
            self._parsed = (normalized_text, SyntaxTreeNode(_MARKDOWN_PARSER.parse(normalized_text)))
        root = self._parsed[1]

        rendered_lines: list[str] = []
        # markdown-it swallows a leading blank line (upstream renders the space token as an empty line); put it back
        if re.match(r"[ \t]*\r?\n", normalized_text):
            rendered_lines.append("")
        for index, node in enumerate(root.children or []):
            next_type = root.children[index + 1].type if index + 1 < len(root.children) else None
            rendered_lines.extend(self.renderBlock(node, content_width, next_type))

        wrapped_lines: list[str] = []
        for line in rendered_lines:
            if isImageLine(line):
                wrapped_lines.append(line)
            else:
                wrapped_lines.extend(wrapTextWithAnsi(line, content_width))

        left_margin = " " * self.paddingX
        right_margin = " " * self.paddingX
        bg_fn = self.defaultTextStyle.bgColor if self.defaultTextStyle else None
        content_lines: list[str] = []
        for line in wrapped_lines:
            if isImageLine(line):
                content_lines.append(line)
                continue

            line_with_margins = left_margin + line + right_margin
            if bg_fn is not None:
                content_lines.append(applyBackgroundToLine(line_with_margins, width, bg_fn))
            else:
                visible_len = visibleWidth(line_with_margins)
                content_lines.append(line_with_margins + (" " * max(0, width - visible_len)))

        empty_line = " " * width
        empty_lines: list[str] = []
        for _ in range(self.paddingY):
            empty_lines.append(applyBackgroundToLine(empty_line, width, bg_fn) if bg_fn is not None else empty_line)

        result = [*empty_lines, *content_lines, *empty_lines]
        self.cachedText = self.text
        self.cachedWidth = width
        self.cachedLines = result
        return result if result else [""]

    def applyDefaultStyle(self, text: str) -> str:
        if self.defaultTextStyle is None:
            return text

        styled = text
        if self.defaultTextStyle.color is not None:
            styled = self.defaultTextStyle.color(styled)
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
        if self.defaultTextStyle is None:
            return ""
        if self.defaultStylePrefix is not None:
            return self.defaultStylePrefix

        sentinel = "\x00"
        styled = sentinel
        if self.defaultTextStyle.color is not None:
            styled = self.defaultTextStyle.color(styled)
        if self.defaultTextStyle.bold:
            styled = self.theme.bold(styled)
        if self.defaultTextStyle.italic:
            styled = self.theme.italic(styled)
        if self.defaultTextStyle.strikethrough:
            styled = self.theme.strikethrough(styled)
        if self.defaultTextStyle.underline:
            styled = self.theme.underline(styled)

        sentinel_index = styled.find(sentinel)
        self.defaultStylePrefix = styled[:sentinel_index] if sentinel_index >= 0 else ""
        return self.defaultStylePrefix

    def getStylePrefix(self, styleFn: StyleFn) -> str:
        sentinel = "\x00"
        styled = styleFn(sentinel)
        sentinel_index = styled.find(sentinel)
        return styled[:sentinel_index] if sentinel_index >= 0 else ""

    def getDefaultInlineStyleContext(self) -> InlineStyleContext:
        return InlineStyleContext(applyText=self.applyDefaultStyle, stylePrefix=self.getDefaultStylePrefix())

    def renderBlock(
        self,
        node: SyntaxTreeNode,
        width: int,
        nextType: str | None = None,
        styleContext: InlineStyleContext | None = None,
    ) -> list[str]:
        lines: list[str] = []

        match node.type:
            case "math_block":
                rendered = _render_latex_or_none(node.content)
                body = rendered if rendered is not None else node.content.strip()
                resolved_ctx = styleContext or self.getDefaultInlineStyleContext()
                for seg in body.split("\n"):
                    lines.append("  " + resolved_ctx.applyText(seg))
                return lines

            case "heading":
                heading_level = int(node.tag[1:]) if node.tag.startswith("h") else 1
                heading_prefix = f"{'#' * heading_level} "

                if heading_level == 1:
                    def heading_style_fn(text: str) -> str:
                        return self.theme.heading(self.theme.bold(self.theme.underline(text)))
                else:
                    def heading_style_fn(text: str) -> str:
                        return self.theme.heading(self.theme.bold(text))

                heading_style_context = InlineStyleContext(
                    applyText=heading_style_fn,
                    stylePrefix=self.getStylePrefix(heading_style_fn),
                )
                heading_text = self.renderInlineNodes(node.children or [], heading_style_context)
                styled_heading = (
                    f"{heading_style_fn(heading_prefix)}{heading_text}" if heading_level >= 3 else heading_text
                )
                lines.append(styled_heading)
                if nextType is not None:
                    lines.append("")

            case "paragraph":
                lines.append(self.renderInlineNodes(node.children or [], styleContext))
                if nextType not in {None, "bullet_list", "ordered_list"}:
                    lines.append("")

            case "text":
                lines.append(self.renderInlineNodes([node], styleContext))

            case "fence" | "code_block":
                indent = self.theme.codeBlockIndent or "  "
                lines.append(self.theme.codeBlockBorder(f"```{node.info or ''}"))
                code_content = node.content.removesuffix("\n")
                if self.theme.highlightCode is not None:
                    highlighted_lines = self.theme.highlightCode(code_content, node.info or None)
                    for highlighted in highlighted_lines:
                        lines.append(f"{indent}{highlighted}")
                else:
                    code_lines = code_content.split("\n") if code_content else [""]
                    for code_line in code_lines:
                        lines.append(f"{indent}{self.theme.codeBlock(code_line)}")
                lines.append(self.theme.codeBlockBorder("```"))
                if nextType is not None:
                    lines.append("")

            case "bullet_list" | "ordered_list":
                lines.extend(self.renderList(node, 0, width, styleContext))
                if nextType is not None:
                    lines.append("")

            case "table":
                lines.extend(self.renderTable(node, width, nextType, styleContext))

            case "blockquote":
                def quote_style(text: str) -> str:
                    return self.theme.quote(self.theme.italic(text))

                quote_style_prefix = self.getStylePrefix(quote_style)
                quote_content_width = max(1, width - 2)
                quote_style_context = InlineStyleContext(
                    applyText=lambda text: text,
                    stylePrefix=quote_style_prefix,
                )

                rendered_quote_lines: list[str] = []
                for index, child in enumerate(node.children or []):
                    next_child_type = node.children[index + 1].type if index + 1 < len(node.children) else None
                    rendered_quote_lines.extend(
                        self.renderBlock(child, quote_content_width, next_child_type, quote_style_context)
                    )

                while rendered_quote_lines and rendered_quote_lines[-1] == "":
                    rendered_quote_lines.pop()

                for quote_line in rendered_quote_lines:
                    styled_line = self.applyQuoteStyle(quote_line, quote_style, quote_style_prefix)
                    for wrapped_line in wrapTextWithAnsi(styled_line, quote_content_width):
                        lines.append(self.theme.quoteBorder("│ ") + wrapped_line)

                if nextType is not None:
                    lines.append("")

            case "hr":
                lines.append(self.theme.hr("─" * min(width, 80)))
                if nextType is not None:
                    lines.append("")

            case "html_block":
                if node.content.strip():
                    lines.append(self.applyDefaultStyle(node.content.strip()))

            case _:
                if node.content:
                    lines.append(node.content)

        return lines

    def applyQuoteStyle(self, line: str, quoteStyle: StyleFn, quoteStylePrefix: str) -> str:
        if not quoteStylePrefix:
            return quoteStyle(line)
        line_with_reapplied_style = line.replace("\x1b[0m", f"\x1b[0m{quoteStylePrefix}")
        return quoteStyle(line_with_reapplied_style)

    def renderInlineNodes(
        self,
        nodes: list[SyntaxTreeNode],
        styleContext: InlineStyleContext | None = None,
    ) -> str:
        result = ""
        resolved = styleContext or self.getDefaultInlineStyleContext()
        apply_text = resolved.applyText
        style_prefix = resolved.stylePrefix

        def apply_text_with_newlines(text: str) -> str:
            return "\n".join(apply_text(segment) for segment in text.split("\n"))

        for node in nodes:
            match node.type:
                case "text":
                    result += self.renderAutolinkText(node.content, resolved)

                case "inline":
                    result += self.renderInlineNodes(node.children or [], resolved)

                case "paragraph":
                    result += self.renderInlineNodes(node.children or [], resolved)

                case "strong":
                    result += self.theme.bold(self.renderInlineNodes(node.children or [], resolved)) + style_prefix

                case "em":
                    result += self.theme.italic(self.renderInlineNodes(node.children or [], resolved)) + style_prefix

                case "code_inline":
                    result += self.theme.code(node.content) + style_prefix

                case "math_inline" | "math_inline_double":
                    rendered = _render_latex_or_none(node.content)
                    src = f"${node.content}$" if node.type == "math_inline" else f"$${node.content}$$"
                    result += apply_text_with_newlines(rendered if rendered is not None else src)

                case "link":
                    self._inLink = True
                    try:
                        link_text = self.renderInlineNodes(node.children or [], resolved)
                    finally:
                        self._inLink = False
                    link_text_plain = self.inlinePlainText(node.children or [])
                    href = node.attrs.get("href", "")
                    result += self.renderLink(link_text, link_text_plain, href, style_prefix)

                case "s":
                    result += (
                        self.theme.strikethrough(self.renderInlineNodes(node.children or [], resolved)) + style_prefix
                    )

                case "html_inline":
                    result += apply_text_with_newlines(node.content)

                case "softbreak" | "hardbreak":
                    result += "\n"

                case _:
                    if node.children:
                        result += self.renderInlineNodes(node.children, resolved)
                    elif node.content:
                        result += apply_text_with_newlines(node.content)

        while style_prefix and result.endswith(style_prefix):
            result = result[: -len(style_prefix)]
        return result

    def renderAutolinkText(self, text: str, styleContext: InlineStyleContext) -> str:
        if getattr(self, "_inLink", False):
            return styleContext.applyText(text)  # no autolinking inside a link label: nested OSC 8 would close the outer link early
        if "\n" in text:
            return "\n".join(self.renderAutolinkText(part, styleContext) for part in text.split("\n"))

        result = ""
        last_end = 0
        for match in _AUTOLINK_RE.finditer(text):
            start = match.start()
            result += styleContext.applyText(text[last_end:start])
            url = match.group("url")
            email = match.group("email")
            raw_value = url or email or ""
            value = _trim_bare_url(raw_value) if url is not None else raw_value
            if not value:
                last_end = start + len(raw_value)
                continue
            href = (value if value.startswith("http") else f"http://{value}") if url is not None \
                else f"mailto:{value}"
            styled_link = self.theme.link(self.theme.underline(value))
            result += self.renderLink(styled_link, value, href, styleContext.stylePrefix, alreadyStyled=True)
            last_end = start + len(value)
        result += styleContext.applyText(text[last_end:])
        return result

    def renderLink(
        self,
        linkText: str,
        linkTextPlain: str,
        href: str,
        stylePrefix: str,
        *,
        alreadyStyled: bool = False,
    ) -> str:
        styled_link = linkText if alreadyStyled else self.theme.link(self.theme.underline(linkText))
        # A control character inside the OSC 8 parameter terminates the sequence early (BEL and
        # ST are both legal OSC terminators), so such an href never becomes a hyperlink: it
        # falls through to the plain "label (url)" form below.
        if getCapabilities().hyperlinks and not _C0_CONTROLS_RE.search(href):
            return hyperlink(styled_link, href) + stylePrefix

        href_for_comparison = href.removeprefix("mailto:")
        candidates = {href, href_for_comparison}
        try:  # markdown-it percent-encodes href, so decode before comparing with the plain-text label
            from urllib.parse import unquote
            candidates |= {unquote(href), unquote(href_for_comparison)}
        except Exception:  # noqa: BLE001, S110 - an href that cannot be decoded is compared as-is
            pass
        if linkTextPlain in candidates:
            return styled_link + stylePrefix
        return styled_link + self.theme.linkUrl(f" ({href})") + stylePrefix

    def inlinePlainText(self, nodes: list[SyntaxTreeNode]) -> str:
        parts: list[str] = []
        for node in nodes:
            match node.type:
                case "text" | "code_inline" | "html_inline":
                    parts.append(node.content)
                case "softbreak" | "hardbreak":
                    parts.append("\n")
                case _:
                    if node.children:
                        parts.append(self.inlinePlainText(node.children))
                    elif node.content:
                        parts.append(node.content)
        return "".join(parts)

    def renderList(
        self,
        node: SyntaxTreeNode,
        depth: int,
        width: int,
        styleContext: InlineStyleContext | None = None,
    ) -> list[str]:
        lines: list[str] = []
        indent = "    " * depth
        start_number = int(node.attrs.get("start", 1)) if node.type == "ordered_list" else 1
        items = node.children or []
        # loose list (blank lines between items): markdown-it signals it by leaving the paragraph un-hidden
        loose = any(child.type == "paragraph" and not child.hidden
                    for item in items for child in item.children or [])

        for index, item in enumerate(items):
            bullet = f"{start_number + index}. " if node.type == "ordered_list" else "- "
            task_marker = self._takeTaskMarker(item)
            bullet += task_marker
            first_prefix = indent + self.theme.listBullet(bullet)
            continuation_prefix = indent + (" " * visibleWidth(bullet))
            item_width = max(1, width - visibleWidth(first_prefix))
            rendered_any_line = False

            for child in item.children or []:
                if child.type in {"bullet_list", "ordered_list"}:
                    lines.extend(self.renderList(child, depth + 1, width, styleContext))
                    rendered_any_line = True
                    continue

                child_lines = self.renderBlock(child, item_width, None, styleContext)
                for line in child_lines:
                    for wrapped_line in wrapTextWithAnsi(line, item_width):
                        line_prefix = continuation_prefix if rendered_any_line else first_prefix
                        lines.append(line_prefix + wrapped_line)
                        rendered_any_line = True

            if not rendered_any_line:
                lines.append(first_prefix)
            if loose and index < len(items) - 1:
                lines.append("")

        return lines

    def _takeTaskMarker(self, item: SyntaxTreeNode) -> str:
        """GFM task lists: consume a leading [ ]/[x] from the item's first text node and fold it into the bullet (the commonmark preset has no tasklists)."""
        for child in item.children or []:
            if child.type != "paragraph":
                continue
            for inline in child.children or []:
                if inline.type != "inline" or not inline.children:
                    break
                first = inline.children[0]
                if first.type != "text":
                    break
                token = getattr(first, "token", None)
                if token is None:
                    break
                taken = token.meta.get("taskMarker")
                if taken is not None:             # this tree rendered before: the marker is already out
                    return taken
                m = re.match(r"^\[([ xX])\] ", first.content)
                if not m:
                    break
                token.content = first.content[m.end():]
                token.meta["taskMarker"] = f"[{'x' if m.group(1).lower() == 'x' else ' '}] "
                return token.meta["taskMarker"]
            break
        return ""

    def getLongestWordWidth(self, text: str, maxWidth: int | None = None) -> int:
        longest = 0
        for word in (segment for segment in text.split() if segment):
            longest = max(longest, visibleWidth(word))
        return min(longest, maxWidth) if maxWidth is not None else longest

    def wrapCellText(self, text: str, maxWidth: int) -> list[str]:
        return wrapTextWithAnsi(text, max(1, maxWidth))

    def renderTable(
        self,
        node: SyntaxTreeNode,
        availableWidth: int,
        nextType: str | None = None,
        styleContext: InlineStyleContext | None = None,
    ) -> list[str]:
        lines: list[str] = []
        if not node.children:
            return lines

        header_rows = node.children[0].children if node.children and node.children[0].type == "thead" else []
        body_section = node.children[1] if len(node.children) > 1 and node.children[1].type == "tbody" else None
        body_rows = body_section.children if body_section is not None else []
        if not header_rows:
            return lines

        header_row = header_rows[0]
        num_cols = len(header_row.children or [])
        if num_cols == 0:
            return lines

        border_overhead = 3 * num_cols + 1
        available_for_cells = availableWidth - border_overhead
        if available_for_cells < num_cols:
            fallback_lines = wrapTextWithAnsi(self.rawSourceForNode(node), availableWidth)
            if nextType is not None:
                fallback_lines.append("")
            return fallback_lines

        max_unbroken_word_width = 30
        natural_widths = [0] * num_cols
        min_word_widths = [1] * num_cols

        for index, cell in enumerate(header_row.children or []):
            text = self.renderInlineNodes(cell.children or [], styleContext)
            natural_widths[index] = visibleWidth(text)
            min_word_widths[index] = max(1, self.getLongestWordWidth(text, max_unbroken_word_width))

        for row in body_rows:
            for index, cell in enumerate(row.children or []):
                text = self.renderInlineNodes(cell.children or [], styleContext)
                natural_widths[index] = max(natural_widths[index], visibleWidth(text))
                min_word_widths[index] = max(
                    min_word_widths[index],
                    self.getLongestWordWidth(text, max_unbroken_word_width),
                )

        min_column_widths = list(min_word_widths)
        min_cells_width = sum(min_column_widths)
        if min_cells_width > available_for_cells:
            min_column_widths = [1] * num_cols
            remaining = available_for_cells - num_cols
            if remaining > 0:
                total_weight = sum(max(0, width - 1) for width in min_word_widths)
                growth = [
                    int((max(0, width - 1) / total_weight) * remaining) if total_weight > 0 else 0
                    for width in min_word_widths
                ]
                for index, width_value in enumerate(growth):
                    min_column_widths[index] += width_value

                allocated = sum(growth)
                leftover = remaining - allocated
                for index in range(num_cols):
                    if leftover <= 0:
                        break
                    min_column_widths[index] += 1
                    leftover -= 1

            min_cells_width = sum(min_column_widths)

        total_natural_width = sum(natural_widths) + border_overhead
        if total_natural_width <= availableWidth:
            column_widths = [
                max(width_value, min_column_widths[index]) for index, width_value in enumerate(natural_widths)
            ]
        else:
            total_grow_potential = sum(
                max(0, width_value - min_column_widths[index]) for index, width_value in enumerate(natural_widths)
            )
            extra_width = max(0, available_for_cells - min_cells_width)
            column_widths = []
            for index, min_width in enumerate(min_column_widths):
                natural_width = natural_widths[index]
                width_delta = max(0, natural_width - min_width)
                grow = int((width_delta / total_grow_potential) * extra_width) if total_grow_potential > 0 else 0
                column_widths.append(min_width + grow)

            allocated = sum(column_widths)
            remaining = available_for_cells - allocated
            while remaining > 0:
                grew = False
                for index in range(num_cols):
                    if remaining <= 0:
                        break
                    if column_widths[index] < natural_widths[index]:
                        column_widths[index] += 1
                        remaining -= 1
                        grew = True
                if not grew:
                    break

        lines.append(f"┌─{'─┬─'.join('─' * width_value for width_value in column_widths)}─┐")

        header_cell_lines = [
            self.wrapCellText(self.renderInlineNodes(cell.children or [], styleContext), column_widths[index])
            for index, cell in enumerate(header_row.children or [])
        ]
        header_line_count = max((len(cell_lines) for cell_lines in header_cell_lines), default=0)
        for line_index in range(header_line_count):
            row_parts: list[str] = []
            for column_index, cell_lines in enumerate(header_cell_lines):
                text = cell_lines[line_index] if line_index < len(cell_lines) else ""
                padded = text + (" " * max(0, column_widths[column_index] - visibleWidth(text)))
                row_parts.append(self.theme.bold(padded))
            lines.append(f"│ {' │ '.join(row_parts)} │")

        separator_line = f"├─{'─┼─'.join('─' * width_value for width_value in column_widths)}─┤"
        lines.append(separator_line)

        for row_index, row in enumerate(body_rows):
            row_cell_lines = [
                self.wrapCellText(self.renderInlineNodes(cell.children or [], styleContext), column_widths[index])
                for index, cell in enumerate(row.children or [])
            ]
            row_line_count = max((len(cell_lines) for cell_lines in row_cell_lines), default=0)
            for line_index in range(row_line_count):
                row_parts = []
                for column_index, cell_lines in enumerate(row_cell_lines):
                    text = cell_lines[line_index] if line_index < len(cell_lines) else ""
                    row_parts.append(text + (" " * max(0, column_widths[column_index] - visibleWidth(text))))
                lines.append(f"│ {' │ '.join(row_parts)} │")
            if row_index < len(body_rows) - 1:
                lines.append(separator_line)

        lines.append(f"└─{'─┴─'.join('─' * width_value for width_value in column_widths)}─┘")
        if nextType is not None:
            lines.append("")
        return lines

    def rawSourceForNode(self, node: SyntaxTreeNode) -> str:
        if node.map is None:
            return node.content
        start, end = node.map
        return "\n".join(self._sourceLines[start:end])


__all__ = ["DefaultTextStyle", "Markdown", "MarkdownTheme", "MarkdownTransform"]

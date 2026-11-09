"""Translated from pi's ``modes/interactive/components/diff.ts`` (v1.1.0)."""

from __future__ import annotations

from dataclasses import dataclass

from misaka.modes.interactive.theme.theme import theme
from misaka.utils import js_regexp, jsdiff
from misaka.utils.js_regexp import RegExp

_DIFF_LINE = RegExp(r"^([+-\s])(\s*\d*)\s(.*)$")
_LEADING_WHITESPACE = RegExp(r"^(\s*)")


@dataclass(slots=True)
class _ParsedDiffLine:
    """MISAKA fork: a name for the inline result type of ``parseDiffLine``."""

    prefix: str
    lineNum: str
    content: str


def parseDiffLine(line: str) -> _ParsedDiffLine | None:
    """Parse diff line to extract prefix, line number, and content.
    Format: "+123 content" or "-123 content" or " 123 content" or "     ..."
    """
    match = js_regexp.match(line, _DIFF_LINE)
    if not match:
        return None
    return _ParsedDiffLine(prefix=match[1], lineNum=match[2], content=match[3])


def replaceTabs(text: str) -> str:
    """Replace tabs with spaces for consistent rendering."""
    return text.replace("\t", "   ")


def renderIntraLineDiff(oldContent: str, newContent: str) -> tuple[str, str]:
    """Compute word-level diff and render with inverse on changed parts.
    Uses diffWords which groups whitespace with adjacent words for cleaner highlighting.
    Strips leading whitespace from inverse to avoid highlighting indentation.

    MISAKA fork: returns ``(removedLine, addedLine)`` for ``{ removedLine, addedLine }``.
    """
    wordDiff = jsdiff.diffWords(oldContent, newContent)

    removedLine = ""
    addedLine = ""
    isFirstRemoved = True
    isFirstAdded = True

    for part in wordDiff:
        if part["removed"]:
            value = part["value"]
            # Strip leading whitespace from the first removed part
            if isFirstRemoved:
                match = js_regexp.match(value, _LEADING_WHITESPACE)
                leadingWs = (match[1] if match else None) or ""
                value = value[len(leadingWs) :]
                removedLine += leadingWs
                isFirstRemoved = False
            if value:
                removedLine += theme.inverse(value)
        elif part["added"]:
            value = part["value"]
            # Strip leading whitespace from the first added part
            if isFirstAdded:
                match = js_regexp.match(value, _LEADING_WHITESPACE)
                leadingWs = (match[1] if match else None) or ""
                value = value[len(leadingWs) :]
                addedLine += leadingWs
                isFirstAdded = False
            if value:
                addedLine += theme.inverse(value)
        else:
            removedLine += part["value"]
            addedLine += part["value"]

    return removedLine, addedLine


@dataclass(slots=True)
class RenderDiffOptions:
    # File path (unused, kept for API compatibility)
    filePath: str | None = None


def renderDiff(diffText: str, _options: RenderDiffOptions | None = None) -> str:
    """Render a diff string with colored lines and intra-line change highlighting.
    - Context lines: dim/gray
    - Removed lines: red, with inverse on changed tokens
    - Added lines: green, with inverse on changed tokens
    """
    lines = diffText.split("\n")
    result: list[str] = []

    i = 0
    while i < len(lines):
        line = lines[i]
        parsed = parseDiffLine(line)

        if not parsed:
            result.append(theme.fg("toolDiffContext", line))
            i += 1
            continue

        if parsed.prefix == "-":
            # Collect consecutive removed lines
            removedLines: list[_ParsedDiffLine] = []
            while i < len(lines):
                p = parseDiffLine(lines[i])
                if not p or p.prefix != "-":
                    break
                removedLines.append(p)
                i += 1

            # Collect consecutive added lines
            addedLines: list[_ParsedDiffLine] = []
            while i < len(lines):
                p = parseDiffLine(lines[i])
                if not p or p.prefix != "+":
                    break
                addedLines.append(p)
                i += 1

            # Only do intra-line diffing when there's exactly one removed and one added line
            # (indicating a single line modification). Otherwise, show lines as-is.
            if len(removedLines) == 1 and len(addedLines) == 1:
                removed = removedLines[0]
                added = addedLines[0]

                removedLine, addedLine = renderIntraLineDiff(
                    replaceTabs(removed.content),
                    replaceTabs(added.content),
                )

                result.append(theme.fg("toolDiffRemoved", f"-{removed.lineNum} {removedLine}"))
                result.append(theme.fg("toolDiffAdded", f"+{added.lineNum} {addedLine}"))
            else:
                # Show all removed lines first, then all added lines
                for removed in removedLines:
                    result.append(theme.fg("toolDiffRemoved", f"-{removed.lineNum} {replaceTabs(removed.content)}"))
                for added in addedLines:
                    result.append(theme.fg("toolDiffAdded", f"+{added.lineNum} {replaceTabs(added.content)}"))
        elif parsed.prefix == "+":
            # Standalone added line
            result.append(theme.fg("toolDiffAdded", f"+{parsed.lineNum} {replaceTabs(parsed.content)}"))
            i += 1
        else:
            # Context line
            result.append(theme.fg("toolDiffContext", f" {parsed.lineNum} {replaceTabs(parsed.content)}"))
            i += 1

    return "\n".join(result)

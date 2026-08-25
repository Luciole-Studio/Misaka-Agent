"""ANSI-styled diff rendering with intra-line highlighting."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from misaka.modes.interactive.theme.theme import theme

_DIFF_LINE_RE = re.compile(r"^([+\-\s])(\s*\d*)\s(.*)$")
_TOKEN_RE = re.compile(r"\S+\s*|\s+")


@dataclass(slots=True)
class RenderDiffOptions:
    filePath: str | None = None


@dataclass(slots=True)
class ParsedDiffLine:
    prefix: str
    lineNum: str
    content: str


def parse_diff_line(line: str) -> ParsedDiffLine | None:
    match = _DIFF_LINE_RE.match(line)
    if match is None:
        return None
    return ParsedDiffLine(prefix=match.group(1), lineNum=match.group(2), content=match.group(3))


def replace_tabs(text: str) -> str:
    return text.replace("\t", "   ")


def _tokenize_words(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def render_intra_line_diff(oldContent: str, newContent: str) -> tuple[str, str]:
    matcher = difflib.SequenceMatcher(a=_tokenize_words(oldContent), b=_tokenize_words(newContent))
    removed_line = ""
    added_line = ""
    is_first_removed = True
    is_first_added = True

    for opcode, a0, a1, b0, b1 in matcher.get_opcodes():
        old_part = "".join(matcher.a[a0:a1])
        new_part = "".join(matcher.b[b0:b1])
        if opcode in {"replace", "delete"}:
            value = old_part
            if is_first_removed:
                leading_ws = re.match(r"^(\s*)", value).group(1) if value else ""
                value = value[len(leading_ws) :]
                removed_line += leading_ws
                is_first_removed = False
            if value:
                removed_line += theme.inverse(value)
        if opcode in {"replace", "insert"}:
            value = new_part
            if is_first_added:
                leading_ws = re.match(r"^(\s*)", value).group(1) if value else ""
                value = value[len(leading_ws) :]
                added_line += leading_ws
                is_first_added = False
            if value:
                added_line += theme.inverse(value)
        if opcode == "equal":
            removed_line += old_part
            added_line += new_part

    return removed_line, added_line


def render_diff(diffText: str, _options: RenderDiffOptions | None = None) -> str:
    del _options
    lines = diffText.split("\n")
    result: list[str] = []
    index = 0

    while index < len(lines):
        parsed = parse_diff_line(lines[index])
        if parsed is None:
            result.append(theme.fg("toolDiffContext", lines[index]))
            index += 1
            continue

        if parsed.prefix == "-":
            removed_lines: list[ParsedDiffLine] = []
            while index < len(lines):
                candidate = parse_diff_line(lines[index])
                if candidate is None or candidate.prefix != "-":
                    break
                removed_lines.append(candidate)
                index += 1

            added_lines: list[ParsedDiffLine] = []
            while index < len(lines):
                candidate = parse_diff_line(lines[index])
                if candidate is None or candidate.prefix != "+":
                    break
                added_lines.append(candidate)
                index += 1

            if len(removed_lines) == 1 and len(added_lines) == 1:
                removed = removed_lines[0]
                added = added_lines[0]
                removed_line, added_line = render_intra_line_diff(
                    replace_tabs(removed.content),
                    replace_tabs(added.content),
                )
                result.append(theme.fg("toolDiffRemoved", f"-{removed.lineNum} {removed_line}"))
                result.append(theme.fg("toolDiffAdded", f"+{added.lineNum} {added_line}"))
                continue

            for removed in removed_lines:
                result.append(theme.fg("toolDiffRemoved", f"-{removed.lineNum} {replace_tabs(removed.content)}"))
            for added in added_lines:
                result.append(theme.fg("toolDiffAdded", f"+{added.lineNum} {replace_tabs(added.content)}"))
            continue

        if parsed.prefix == "+":
            result.append(theme.fg("toolDiffAdded", f"+{parsed.lineNum} {replace_tabs(parsed.content)}"))
            index += 1
            continue

        result.append(theme.fg("toolDiffContext", f" {parsed.lineNum} {replace_tabs(parsed.content)}"))
        index += 1

    return "\n".join(result)


parseDiffLine = parse_diff_line
renderDiff = render_diff
renderIntraLineDiff = render_intra_line_diff
replaceTabs = replace_tabs

__all__ = [
    "RenderDiffOptions",
    "renderDiff",
]

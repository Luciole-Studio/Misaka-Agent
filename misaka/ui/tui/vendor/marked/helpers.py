"""Translated from marked's ``src/helpers.ts`` (marked 18.0.11, MIT)."""

from __future__ import annotations

from urllib.parse import quote

from misaka.utils import js_regexp
from misaka.utils.js import jsTrim

from .rules import other

# Helpers
escapeReplacements: dict[str, str] = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
}


def getEscapeReplacement(ch: str, *_args: object) -> str:
    return escapeReplacements[ch]


def escapeHtmlEntities(html: str, encode: bool | None = None) -> str:
    if encode:
        if other.escapeTest.test(html):
            return js_regexp.replace(html, other.escapeReplace, getEscapeReplacement)
    else:
        if other.escapeTestNoEncode.test(html):
            return js_regexp.replace(html, other.escapeReplaceNoEncode, getEscapeReplacement)

    return html


# MISAKA fork: `encodeURI`'s reserved and unescaped characters, which it leaves as they are.
_ENCODE_URI_SAFE = ";,/?:@&=+$-_.!~*'()#"


def cleanUrl(href: str) -> str | None:
    try:
        # MISAKA fork: `encodeURI`, which throws on a lone surrogate.
        href.encode("utf-8")
        href = js_regexp.replace(quote(href, safe=_ENCODE_URI_SAFE), other.percentDecode, "%")
    except UnicodeEncodeError:
        return None
    return href


def splitCells(tableRow: str, count: int | None = None) -> list[str]:
    # ensure that every cell-delimiting pipe has a space
    # before it to distinguish it from an escaped pipe
    def replacePipe(match: str, offset: int, string: str) -> str:
        escaped = False
        curr = offset
        curr -= 1
        while curr >= 0 and string[curr] == "\\":
            escaped = not escaped
            curr -= 1
        if escaped:
            # odd number of slashes means | is escaped
            # so we leave it alone
            return "|"
        else:
            # add space before unescaped |
            return " |"

    row = js_regexp.replace(tableRow, other.findPipe, replacePipe)
    cells = js_regexp.split(row, other.splitPipe)
    i = 0

    # First/last cell in a row cannot be empty if it has no leading/trailing pipe
    if not jsTrim(cells[0]):
        cells.pop(0)
    if len(cells) > 0 and not jsTrim(cells[-1]):
        cells.pop()

    if count:
        if len(cells) > count:
            del cells[count:]
        else:
            while len(cells) < count:
                cells.append("")

    while i < len(cells):
        # leading or trailing whitespace is ignored per the gfm spec
        cells[i] = js_regexp.replace(jsTrim(cells[i]), other.slashPipe, "|")
        i += 1
    return cells


def rtrim(str_: str, c: str, invert: bool | None = None) -> str:
    """Remove trailing 'c's. Equivalent to str.replace(/c*$/, '').
    /c*$/ is vulnerable to REDOS.

    :param str_: the string
    :param c: the character
    :param invert: Remove suffix of non-c chars instead. Default falsey.
    """
    l = len(str_)
    if l == 0:
        return ""

    # Length of suffix matching the invert condition.
    suffLen = 0

    # Step left until we fail to match the invert condition.
    while suffLen < l:
        currChar = str_[l - suffLen - 1]
        if currChar == c and not invert or currChar != c and invert:
            suffLen += 1
        else:
            break

    return str_[: l - suffLen]


def trimTrailingBlankLines(str_: str) -> str:
    lines = str_.split("\n")
    end = len(lines) - 1
    while end >= 0 and other.blankLine.test(lines[end]):
        end -= 1
    if len(lines) - end <= 2:
        # we want to keep single trailing blank lines
        return str_

    return "\n".join(lines[: end + 1])


def findClosingBracket(str_: str, b: str) -> int:
    if b[1] not in str_:
        return -1

    level = 0
    i = 0
    while i < len(str_):
        if str_[i] == "\\":
            i += 1
        elif str_[i] == b[0]:
            level += 1
        elif str_[i] == b[1]:
            level -= 1
            if level < 0:
                return i
        i += 1
    if level > 0:
        return -2

    return -1


def expandTabs(line: str, indent: int = 0) -> str:
    col = indent
    expanded = ""
    for char in line:
        if char == "\t":
            added = 4 - (col % 4)
            expanded += " " * added
            col += added
        else:
            expanded += char
            col += 1

    return expanded


__all__ = [
    "cleanUrl",
    "escapeHtmlEntities",
    "expandTabs",
    "findClosingBracket",
    "rtrim",
    "splitCells",
    "trimTrailingBlankLines",
]

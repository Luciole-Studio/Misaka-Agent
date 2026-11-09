"""Translated from grok-mermaid 0.2.3's ``src/canvas.ts``.

MISAKA fork: a cell index below zero is skipped, as writing it to a JavaScript array sets a property no cell reads,
where a Python list would wrap around to its end.
"""

from __future__ import annotations

from typing import Final

from .types import Cls, Span
from .width import measured

# Sentinel occupying the trailing column of a wide glyph. Never emitted: the
# line builder skips it so a CJK character claims two cells of layout but
# contributes one character of output.
CONT: Final = chr(0)

# Connection direction bits, combined into a box-drawing glyph by `maskChar`.
U: Final = 1
D: Final = 2
L: Final = 4
R: Final = 8

# Line styles, tracked per cell so crossing edges keep their own stroke.
STY_DOT: Final = 1
STY_THICK: Final = 2
STY_SOLID: Final = 4


class Canvas:
    """A grid of cells. Edges accumulate as direction bits rather than glyphs so
    that crossings and junctions resolve correctly whatever order they are drawn
    in; ``finalizeMask`` turns the accumulated bits into characters at the end.

    ``occupied`` marks cells claimed by a box, which edge bits must not overwrite.
    """

    def __init__(self, w: int, h: int) -> None:
        n = w * h
        self.w = w
        self.h = h
        self.ch: list[str] = [" "] * n
        self.cls: list[Cls] = ["none"] * n
        self.mask: list[int] = [0] * n
        self.style: list[int] = [0] * n
        self.occupied: list[int] = [0] * n
        self.curStyle: int = STY_SOLID

    def idx(self, x: int, y: int) -> int:
        return y * self.w + x

    def set(self, x: int, y: int, c: str, cls: Cls) -> None:
        if x >= self.w or y >= self.h:
            return
        i = self.idx(x, y)
        if i < 0:
            return
        self.ch[i] = c
        self.cls[i] = cls

    def addBits(self, x: int, y: int, bits: int, cls: Cls = "edge") -> None:
        """Accumulate direction bits on a free cell.

        ``cls`` is the class to claim the cell for; ``border`` cells are never
        reclassified, so a connector meeting a box keeps the box's styling.
        """
        if x >= self.w or y >= self.h:
            return
        i = self.idx(x, y)
        if i < 0 or self.occupied[i]:
            return
        self.mask[i] = (self.mask[i] | bits) & 0xFF
        self.style[i] = (self.style[i] | self.curStyle) & 0xFF
        if self.cls[i] != "border":
            self.cls[i] = cls

    def blit(self, sub: Canvas, ox: int, oy: int) -> None:
        """Stamp a finished sub-canvas (a subgraph frame's contents) at an offset."""
        for sy in range(sub.h):
            for sx in range(sub.w):
                x = ox + sx
                y = oy + sy
                if x >= self.w or y >= self.h:
                    continue
                si = sub.idx(sx, sy)
                di = self.idx(x, y)
                if di < 0:
                    continue
                self.ch[di] = sub.ch[si]
                self.cls[di] = sub.cls[si]
                self.style[di] = sub.style[si]
                self.occupied[di] = 1

    def junction(self, x: int, y: int, bits: int) -> None:
        """Add direction bits even to an occupied cell, so an edge can meet a border."""
        if x >= self.w or y >= self.h:
            return
        i = self.idx(x, y)
        if i < 0:
            return
        self.mask[i] = (self.mask[i] | bits) & 0xFF
        if self.cls[i] != "border":
            self.cls[i] = "edge"

    def segV(self, x: int, y0: int, y1: int) -> None:
        a = min(y0, y1)
        b = max(y0, y1)
        for y in range(a, b + 1):
            bits = 0
            if y > a:
                bits |= U
            if y < b:
                bits |= D
            self.addBits(x, y, bits)

    def segH(self, y: int, x0: int, x1: int) -> None:
        a = min(x0, x1)
        b = max(x0, x1)
        for x in range(a, b + 1):
            bits = 0
            if x > a:
                bits |= L
            if x < b:
                bits |= R
            self.addBits(x, y, bits)

    def finalizeMask(self) -> None:
        """Resolve accumulated direction bits into glyphs, honouring line style."""
        for i in range(len(self.ch)):
            if self.mask[i] != 0 and self.ch[i] == " ":
                c = maskChar(self.mask[i])
                self.ch[i] = (
                    dottedChar(c) if self.style[i] == STY_DOT else thickChar(c) if self.style[i] == STY_THICK else c
                )

    def flipVertical(self) -> None:
        """Mirror top-to-bottom for ``BT``. Rows reorder but within-row text does not,
        so labels stay readable; box-drawing glyphs flip to match."""
        for y in range(self.h // 2):
            y2 = self.h - 1 - y
            for x in range(self.w):
                i = self.idx(x, y)
                j = self.idx(x, y2)
                self.ch[i], self.ch[j] = self.ch[j], self.ch[i]
                self.cls[i], self.cls[j] = self.cls[j], self.cls[i]
        for i in range(len(self.ch)):
            self.ch[i] = flipGlyphV(self.ch[i])

    def flipHorizontal(self) -> None:
        """Mirror left-to-right for ``RL``. Mirroring reverses each row, so after
        flipping glyphs each text/label run is reversed back to reading order."""
        for y in range(self.h):
            for x in range(self.w // 2):
                x2 = self.w - 1 - x
                i = self.idx(x, y)
                j = self.idx(x2, y)
                self.ch[i], self.ch[j] = self.ch[j], self.ch[i]
                self.cls[i], self.cls[j] = self.cls[j], self.cls[i]
        for i in range(len(self.ch)):
            self.ch[i] = flipGlyphH(self.ch[i])
        for y in range(self.h):
            x = 0
            while x < self.w:
                cls = self.cls[self.idx(x, y)]
                if cls in ("text", "edgeLabel"):
                    start = self.idx(x, y)
                    while x < self.w and self.cls[self.idx(x, y)] == cls:
                        x += 1
                    end = self.idx(x, y)
                    reverseSlice(self.ch, start, end)
                else:
                    x += 1

    def toLines(self) -> tuple[list[str], list[list[Span]], int]:
        """Group each row into runs of one class, dropping wide-glyph continuations."""
        plain: list[str] = []
        styled: list[list[Span]] = []
        width = 0
        for y in range(self.h):
            # A trailing CONT counts as painted: it is the second cell of a wide
            # glyph, so the row really does reach that column.
            last = 0
            for x in range(self.w - 1, -1, -1):
                if self.ch[self.idx(x, y)] != " ":
                    last = x + 1
                    break
            width = max(width, last)
            spans: list[Span] = []
            plainRow = ""
            run = ""
            runCls: Cls = "none"
            for x in range(last):
                i = self.idx(x, y)
                c = self.ch[i]
                if c == CONT:
                    continue
                cls = self.cls[i]
                plainRow += c
                if cls != runCls and run != "":
                    spans.append(Span(text=run, cls=runCls))
                    run = ""
                runCls = cls
                run += c
            if run != "":
                spans.append(Span(text=run, cls=runCls))
            styled.append(spans)
            # Only ASCII spaces, which is all a blank cell ever holds. Trimming `\s`
            # would eat a trailing NBSP that `styled` keeps, desyncing the two.
            plain.append(plainRow.rstrip(" "))
        first = 0
        while first < len(plain) and plain[first] == "":
            first += 1
        end = len(plain)
        while end > first and plain[end - 1] == "":
            end -= 1
        return plain[first:end], styled[first:end], width


def reverseSlice(arr: list[str], start: int, end: int) -> None:
    i, j = start, end - 1
    while i < j:
        arr[i], arr[j] = arr[j], arr[i]
        i += 1
        j -= 1


def drawText(canvas: Canvas, text: str, x: int, y: int, cls: Cls) -> None:
    """Paint ``text`` at ``x, y``, one grapheme cluster per cell.

    A wide cluster claims a second cell, marked with ``CONT`` so the line builder
    emits one character for it rather than a stray space.
    """
    cur = x
    for cluster, cw in measured(text):
        if cw == 0:
            continue
        canvas.set(cur, y, cluster, cls)
        for k in range(1, cw):
            canvas.set(cur + k, y, CONT, cls)
        cur += cw


def drawTextOverEdges(canvas: Canvas, text: str, x: int, y: int, cls: Cls) -> None:
    """Paint ``text`` at ``x, y``, clearing any edge bits underneath first.

    Used where text sits on top of a drawn line (sequence messages, dividers,
    compartment rows) and must win over it.
    """
    cur = x
    for cluster, cw in measured(text):
        if cw == 0:
            continue
        for k in range(cw):
            if cur + k < canvas.w and y < canvas.h and canvas.idx(cur + k, y) >= 0:
                canvas.mask[canvas.idx(cur + k, y)] = 0
            canvas.set(cur + k, y, cluster if k == 0 else CONT, cls)
        cur += cw


def maskChar(mask: int) -> str:
    match mask:
        case 0:
            return " "
        case 1 | 2 | 3:  # U, D, U | D
            return "│"
        case 4 | 8 | 12:  # L, R, L | R
            return "─"
        case 10:  # D | R
            return "┌"
        case 6:  # D | L
            return "┐"
        case 9:  # U | R
            return "└"
        case 5:  # U | L
            return "┘"
        case 11:  # U | D | R
            return "├"
        case 7:  # U | D | L
            return "┤"
        case 14:  # D | L | R
            return "┬"
        case 13:  # U | L | R
            return "┴"
        case _:
            return "┼"


DOTTED: Final = {"─": "╌", "│": "╎"}

THICK: Final = {
    "─": "━", "│": "┃", "┌": "┏", "┐": "┓", "└": "┗", "┘": "┛", "├": "┣", "┤": "┫", "┬": "┳", "┴": "┻", "┼": "╋",
}

FLIP_V: Final = {
    "┌": "└", "└": "┌", "┐": "┘", "┘": "┐", "┏": "┗", "┗": "┏", "┓": "┛", "┛": "┓", "╭": "╰", "╰": "╭",
    "╮": "╯", "╯": "╮", "┬": "┴", "┴": "┬", "┳": "┻", "┻": "┳", "▼": "▲", "▲": "▼", "▽": "△", "△": "▽",
}

FLIP_H: Final = {
    "┌": "┐", "┐": "┌", "└": "┘", "┘": "└", "┏": "┓", "┓": "┏", "┗": "┛", "┛": "┗", "╭": "╮", "╮": "╭",
    "╰": "╯", "╯": "╰", "├": "┤", "┤": "├", "┣": "┫", "┫": "┣", "▶": "◄", "◄": "▶", "▷": "◁", "◁": "▷",
}


def dottedChar(c: str) -> str:
    return DOTTED.get(c, c)


def thickChar(c: str) -> str:
    return THICK.get(c, c)


def flipGlyphV(c: str) -> str:
    return FLIP_V.get(c, c)


def flipGlyphH(c: str) -> str:
    return FLIP_H.get(c, c)


__all__ = [
    "CONT",
    "STY_DOT",
    "STY_SOLID",
    "STY_THICK",
    "Canvas",
    "D",
    "L",
    "R",
    "U",
    "dottedChar",
    "drawText",
    "drawTextOverEdges",
    "flipGlyphH",
    "flipGlyphV",
    "maskChar",
    "thickChar",
]

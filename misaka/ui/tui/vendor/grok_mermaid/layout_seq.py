"""Sequence diagram layout.

Participants get one column each, with lifelines running the full height and
a box repeated at top and bottom. Column gaps are solved from the widest
thing that has to fit between any two columns — a message label, a note, a
self-message stub — then items stack down the canvas in source order.

Translated from grok-mermaid 0.2.3's ``src/layout-seq.ts``.
"""

from __future__ import annotations

import math
from typing import Final

from .canvas import Canvas, D, L, R, U, drawTextOverEdges
from .labels import WRAP_WIDTH, fitLabel
from .layout import CanvasResult, Placed, drawBox
from .parse import NoteAnchor, SeqItem, Sequence
from .width import stringWidth

PAD: Final = 1
# Minimum columns between adjacent lifelines.
SEQ_GAP: Final = 5
MAX_CANVAS_CELLS: Final = 1 << 21


def sat(a: int, b: int) -> int:
    return max(0, a - b)


def half(n: int) -> int:
    return math.floor(n / 2)


def noteGeometry(xs: list[int], anchor: NoteAnchor, textW: int) -> tuple[int, int]:
    """Where a note box sits, given the lifeline positions: ``(x, w)``."""
    if anchor.kind == "over":
        center = half(xs[anchor.from_] + xs[anchor.to])
        w = max(xs[anchor.to] - xs[anchor.from_] + 5, textW + 2 * PAD + 2)
        return sat(center, half(w)), w
    w = textW + 2 * PAD + 2
    if anchor.kind == "left":
        return sat(xs[anchor.at], 2 + w - 1), w
    return xs[anchor.at] + 2, w


def itemTextW(text: str | None) -> int:
    return 0 if text is None else stringWidth(text)


def layoutSequence(seq: Sequence) -> CanvasResult:
    n = len(seq.labels)
    labels = [fitLabel(label, WRAP_WIDTH) for label in seq.labels]
    boxW = [max(1, stringWidth(label)) + 2 * PAD + 2 for label in labels]
    boxH = 3

    gaps = [max(SEQ_GAP, math.ceil(boxW[i] / 2) + math.ceil(boxW[i + 1] / 2) + 1) for i in range(sat(n, 1))]

    # Each requirement is "columns l..r together need at least `need` cells".
    reqs: list[tuple[int, int, int]] = []
    for item in seq.items:
        if item.kind == "message":
            tw = itemTextW(item.text)
            if item.from_ != item.to:
                reqs.append((min(item.from_, item.to), max(item.from_, item.to), max(tw + 2, 4)))
            elif item.from_ + 1 < n:
                reqs.append((item.from_, item.from_ + 1, 5 + tw + 2))
        elif item.kind == "note":
            assert item.anchor is not None and item.text is not None
            tw = stringWidth(item.text)
            a = item.anchor
            if a.kind == "over" and a.from_ < a.to:
                reqs.append((a.from_, a.to, sat(tw, 1)))
            elif a.kind == "over":
                need = math.ceil((tw + 4) / 2) + 2
                if a.from_ > 0:
                    reqs.append((a.from_ - 1, a.from_, need))
                if a.from_ + 1 < n:
                    reqs.append((a.from_, a.from_ + 1, need))
            elif a.kind == "left" and a.at > 0:
                reqs.append((a.at - 1, a.at, tw + 7))
            elif a.kind == "right" and a.at + 1 < n:
                reqs.append((a.at, a.at + 1, tw + 7))
    # Narrowest spans first, so a wide requirement absorbs what they already gave.
    reqs.sort(key=lambda req: req[1] - req[0])
    for left, right, need in reqs:
        cur = sum(gaps[i] for i in range(left, right))
        if cur < need:
            gaps[right - 1] += need - cur

    xs = [0] * n
    xs[0] = half(boxW[0])
    for i in range(1, n):
        xs[i] = xs[i - 1] + gaps[i - 1]

    canvasW = xs[n - 1] + math.ceil(boxW[n - 1] / 2) + 1
    for item in seq.items:
        if item.kind == "message" and item.from_ == item.to:
            canvasW = max(canvasW, xs[item.from_] + 5 + itemTextW(item.text) + 1)
        elif item.kind == "note":
            assert item.anchor is not None and item.text is not None
            gx, gw = noteGeometry(xs, item.anchor, stringWidth(item.text))
            canvasW = max(canvasW, gx + gw + 1)
        elif item.kind == "divider":
            canvasW = max(canvasW, stringWidth(item.text or "") + 4)

    rows: list[int] = []
    y = boxH + 1
    for item in seq.items:
        rows.append(y)
        y += rowHeight(item)
    bottomTop = y
    canvasH = bottomTop + boxH

    if canvasW * canvasH > MAX_CANVAS_CELLS:
        return None

    canvas = Canvas(canvasW, canvasH)

    for i in range(n):
        for by in (0, bottomTop):
            drawBox(canvas, box(sat(xs[i], half(boxW[i])), by, boxW[i], boxH), [labels[i]], "rect")
    for k, item in enumerate(seq.items):
        if item.kind != "note":
            continue
        assert item.anchor is not None and item.text is not None
        gx, gw = noteGeometry(xs, item.anchor, stringWidth(item.text))
        drawBox(canvas, box(gx, rows[k], gw, 3), [item.text], "rect")

    for x in xs:
        canvas.junction(x, boxH - 1, D)
        canvas.segV(x, boxH, bottomTop - 1)
        canvas.junction(x, bottomTop, U)

    for k, item in enumerate(seq.items):
        r = rows[k]
        if item.kind == "message":
            drawMessage(canvas, item, xs, r)
        elif item.kind == "divider":
            drawDivider(canvas, item.text or "", r, canvasW)

    canvas.finalizeMask()
    return canvas


def rowHeight(item: SeqItem) -> int:
    if item.kind == "note":
        return 4
    if item.kind == "divider":
        return 2
    if item.from_ == item.to:
        return 4
    return 3 if item.text is not None else 2


def box(x: int, y: int, w: int, h: int) -> Placed:
    """Geometry for a box drawn by position and size; ranks are irrelevant here."""
    return Placed(x=x, y=y, w=w, h=h, cx=x + half(w), cy=y + 1, rank=0)


def drawMessage(canvas: Canvas, item: SeqItem, xs: list[int], r: int) -> None:
    lineCh = "╌" if item.dashed else "─"

    if item.from_ == item.to:
        # A stub that leaves the lifeline and returns two rows down.
        x = xs[item.from_]
        canvas.junction(x, r, R)
        canvas.set(x + 1, r, lineCh, "edge")
        canvas.set(x + 2, r, lineCh, "edge")
        canvas.set(x + 3, r, "╮", "edge")
        canvas.set(x + 3, r + 1, "│", "edge")
        canvas.set(x + 1, r + 2, "×" if item.head == "cross" else "◄", "edge")
        canvas.set(x + 2, r + 2, lineCh, "edge")
        canvas.set(x + 3, r + 2, "╯", "edge")
        if item.text is not None:
            drawTextOverEdges(canvas, item.text, x + 5, r + 1, "text")
        return

    x0 = xs[item.from_]
    x1 = xs[item.to]
    rightward = x1 > x0
    # A labelled message writes its text on `r` and draws the arrow below it.
    arrowRow = r + 1 if item.text is not None else r
    lo = min(x0, x1)
    hi = max(x0, x1)

    canvas.junction(x0, arrowRow, R if rightward else L)
    for x in range(lo + 1, hi):
        canvas.set(x, arrowRow, lineCh, "edge")
    headCh = "×" if item.head == "cross" else "▶" if rightward else "◄"
    canvas.set(x1 - 1 if rightward else x1 + 1, arrowRow, headCh, "edge")

    if item.text is not None:
        span = hi - lo - 1
        t = fitLabel(item.text, max(1, span))
        drawTextOverEdges(canvas, t, lo + 1 + half(sat(span, stringWidth(t))), r, "text")


def drawDivider(canvas: Canvas, text: str, r: int, canvasW: int) -> None:
    """A full-width rule labelling a ``loop`` / ``alt`` / ``opt`` block boundary."""
    for x in range(canvasW):
        canvas.set(x, r, "─", "edge")
    drawTextOverEdges(canvas, f" {fitLabel(text, sat(canvasW, 4))} ", 2, r, "edgeLabel")


__all__ = ["layoutSequence"]

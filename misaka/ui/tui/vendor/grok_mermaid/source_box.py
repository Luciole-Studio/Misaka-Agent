"""The raw source in a framed box.

What to show when ``render`` returns ``None``, or returns art too wide for the
space at hand. Both are the caller's call, so this is theirs to invoke — and
theirs to caption, since only they know whether some other view of the
diagram exists to point the reader at.

Translated from grok-mermaid 0.2.3's ``src/source-box.ts``.
"""

from __future__ import annotations

from misaka.utils.js import jsSplitWhitespace, jsTrimEnd

from .labels import srcLines, stripControls
from .types import MermaidArt, Span
from .width import measured, stringWidth


def sat(a: int, b: int) -> int:
    return max(0, a - b)


def sourceBox(src: str, maxWidth: int | None = None) -> MermaidArt:
    """Frame ``src`` in a titled box, hard-wrapping its lines to ``maxWidth`` columns.

    The result can still exceed ``maxWidth``: the body wraps to
    ``max(8, maxWidth - 4)`` and the `` mermaid: <kind> `` title is never truncated,
    so a long first token sets a floor. Check ``width`` if it matters.
    """
    src = stripControls(src)
    header = next((w for w in jsSplitWhitespace(src) if w != ""), "diagram")
    title = f" mermaid: {header} "
    limit = None if maxWidth is None else max(8, sat(maxWidth, 4))

    body: list[str] = []
    started = False
    for raw in srcLines(src):
        line = jsTrimEnd(raw)
        if not started and line == "":
            continue
        started = True
        body.extend(chunkLine(line, limit))

    contentW = max([stringWidth(title), *(stringWidth(line) for line in body), 0])
    inner = contentW + 2

    plain: list[str] = []
    styled: list[list[Span]] = []

    rule = "─" * sat(inner, stringWidth(title))
    plain.append(f"╭{title}{rule}╮")
    styled.append([Span(text="╭", cls="border"), Span(text=title, cls="title"), Span(text=f"{rule}╮", cls="border")])

    for line in body:
        pad = " " * sat(contentW, stringWidth(line))
        plain.append(f"│ {line}{pad} │")
        styled.append([Span(text="│ ", cls="border"), Span(text=line, cls="text"), Span(text=f"{pad} │", cls="border")])

    bottom = f"╰{'─' * inner}╯"
    plain.append(bottom)
    styled.append([Span(text=bottom, cls="border")])

    return MermaidArt(plain=plain, styled=styled, width=inner + 2, warnings=[])


def chunkLine(line: str, limit: int | None) -> list[str]:
    """Hard-break a line at ``limit`` columns, never splitting a wide glyph."""
    if limit is None or stringWidth(line) <= limit:
        return [line]
    out: list[str] = []
    cur = ""
    curW = 0
    for c, cw in measured(line):
        if curW + cw > limit and cur != "":
            out.append(cur)
            cur = ""
            curW = 0
        cur += c
        curW += cw
    if cur != "":
        out.append(cur)
    return out


__all__ = ["sourceBox"]

"""grok-mermaid 0.2.3 (https://github.com/xl0/grok-mermaid, Apache-2.0; ``LICENSES/grok-mermaid.txt``), which pi's TUI
draws Mermaid diagrams with, ported to Python: Mermaid source rendered as Unicode box-drawing art for terminals.

Translated from grok-mermaid 0.2.3's ``src/index.ts``. MISAKA fork: an edge's ``from`` is ``from_`` (a Python keyword),
and the model types are dataclasses for TypeScript's object types.
"""

from __future__ import annotations

from dataclasses import dataclass

from misaka.utils.js import jsTrim, jsTrimEnd

from .ansi import DEFAULT_THEME, AnsiTheme, toAnsi
from .canvas import Canvas
from .labels import stripControls
from .layout import CanvasResult, layoutClass, layoutFlowchart, layoutGrouped
from .layout_seq import layoutSequence
from .parse import (
    DiagramKind,
    diagramKind,
    parseClass,
    parseEr,
    parseGraph,
    parseSequence,
    parseState,
)
from .source_box import sourceBox
from .types import Cls, MermaidArt, Span


def render(src: str) -> MermaidArt | None:
    """Render a Mermaid source block as Unicode box-drawing art.

    Supported: ``graph``/``flowchart`` (including ``subgraph``), ``stateDiagram``,
    ``classDiagram``, ``erDiagram`` and ``sequenceDiagram``.

    The diagram is laid out at whatever size it needs; ``art.width`` reports the
    columns that turned out to be. Deciding what to do when that exceeds the
    space at hand is the caller's — ``sourceBox`` is the usual answer::

        art = render(src)
        show(art if art and art.width <= cols else sourceBox(src, cols))

    ``None`` means there is no art to show: blank input, a syntax error, a diagram
    type this renderer does not draw, or one large enough that laying it out is
    refused. ``diagramKind`` separates the middle two.

    Rendering is best-effort. A flowchart keeps whatever parsed; the stricter
    grammars additionally get one retry without their final line, which is what
    keeps a streaming diagram on screen while its last statement is half-typed.
    Everything given up on is listed in ``art.warnings`` — advisory only, never a
    reason to withhold the art.
    """
    src = stripControls(src)
    if jsTrim(src) == "":
        return None
    drawn = attempt(src)
    if drawn is None:
        return None
    plain, styled, width = drawn.canvas.toLines()
    return MermaidArt(plain=plain, styled=styled, width=width, warnings=drawn.warnings)


@dataclass(slots=True)
class Drawn:
    canvas: Canvas
    warnings: list[str]


def attempt(src: str) -> Drawn | None:
    """Draw ``src``, retrying once without its last line if the grammar rejects it.

    State, class, ER and sequence fail a whole diagram on one unreadable
    statement, and while a source is streaming its last line is usually still
    being typed — so without this a diagram alternates with the source box all
    the way in. Only the final line is dropped, and doing so is always reported,
    so a finished document with a bad last line still says what it lost rather
    than quietly rendering short.
    """
    drawn = draw(src)
    if drawn is not None:
        return drawn

    body = jsTrimEnd(src)
    cut = body.rfind("\n")
    if cut == -1:
        return None
    salvaged = draw(body[:cut])
    if salvaged is None:
        return None

    dropped = jsTrim(body[cut + 1 :])
    return Drawn(canvas=salvaged.canvas, warnings=[*salvaged.warnings, f'dropped, unreadable final line: "{dropped}"'])


def draw(src: str) -> Drawn | None:
    """Dispatch on the declared diagram type; ``None`` means nothing was drawn."""

    def plain(canvas: CanvasResult) -> Drawn | None:
        return None if canvas is None else Drawn(canvas=canvas, warnings=[])

    match diagramKind(src):
        case "flowchart":
            graph = parseGraph(src)
            if graph is None:
                return None
            canvas = layoutFlowchart(graph) if len(graph.groups) == 0 else layoutGrouped(graph)
            return None if canvas is None else Drawn(canvas=canvas, warnings=graph.warnings)
        case "state":
            state = parseState(src)
            return None if state is None else plain(layoutFlowchart(state))
        case "class":
            cls = parseClass(src)
            return None if cls is None else plain(layoutClass(cls[0], cls[1]))
        case "er":
            er = parseEr(src)
            return None if er is None else plain(layoutClass(er[0], er[1]))
        case "sequence":
            seq = parseSequence(src)
            return None if seq is None else plain(layoutSequence(seq))
        case _:
            return None


__all__ = [
    "DEFAULT_THEME",
    "AnsiTheme",
    "Cls",
    "DiagramKind",
    "MermaidArt",
    "Span",
    "diagramKind",
    "render",
    "sourceBox",
    "toAnsi",
]

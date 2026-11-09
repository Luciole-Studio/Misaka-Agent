"""Graph layout: rank, order, place, route, draw.

Follows the Sugiyama outline — assign ranks along the flow axis, reorder
within ranks to cut crossings, then relax positions on the cross axis so
chains stay straight. Edges between adjacent ranks share horizontal "bus"
rows; everything else is routed around the diagram through vertical "lanes".

``BT`` and ``RL`` reuse the ``TD``/``LR`` layouts and flip the finished canvas, so
text never ends up mirrored.

Translated from grok-mermaid 0.2.3's ``src/layout.ts``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final, Literal

from misaka.utils.js import jsRound

from .canvas import (
    CONT,
    STY_DOT,
    STY_SOLID,
    STY_THICK,
    Canvas,
    D,
    L,
    R,
    U,
    drawText,
    drawTextOverEdges,
)
from .graph import ClassInfo, Edge, Graph, Head, Node, Shape
from .labels import MAX_LABEL, MAX_LINES, WRAP_WIDTH, fitLabel, wrapLabel
from .width import measured, stringWidth

# Cells of padding between a box border and its text.
PAD: Final = 1
# Minimum horizontal / vertical space between boxes.
GAP_X: Final = 3
GAP_Y: Final = 2
# Refuse to allocate a canvas larger than this many cells.
MAX_CANVAS_CELLS: Final = 1 << 21

# A laid-out canvas, or `None` when the diagram is empty or over the cell cap.
type CanvasResult = Canvas | None


def sat(a: float, b: float) -> float:
    """Saturating subtraction; Rust's ``usize`` arithmetic never goes negative."""
    return max(0, a - b)


def half(n: float) -> int:
    return math.floor(n / 2)


@dataclass(slots=True)
class Placed:
    x: int
    y: int
    w: int
    h: int
    cx: int
    cy: int
    rank: int


@dataclass(slots=True)
class NodeSizes:
    """Per-node dimensions. ``lay*`` include room for self-edge loops and labels."""

    boxW: list[int]
    boxH: list[int]
    layW: list[int]
    layH: list[int]
    extraH: list[int]
    selfLabelW: list[int]


@dataclass(slots=True)
class NodeExtra:
    """What to draw inside a node box: ``plain``, a ``frame`` with ``sub``, or ``compartments`` with ``sections``."""

    kind: Literal["plain", "frame", "compartments"]
    sub: Canvas | None = None
    sections: list[list[str]] | None = None


@dataclass(slots=True)
class RoutePlan:
    canvasW: int
    canvasH: int
    # Coordinate just past each rank's boxes, where its bus rows begin.
    bandEnd: list[int]
    # Bus track offset per edge.
    edgeBus: list[int]
    # Coordinate of the first lane track.
    laneBase: int
    # Lane track offset per edge.
    edgeLane: list[int]


# ------------------------------------------------------------------ ranking


def computeRanks(graph: Graph) -> list[int]:
    """Longest-path ranking over the graph's DAG.

    Back edges (those closing a cycle) are excluded by a DFS colouring pass, so
    ``A --> B --> C --> A`` still ranks 0, 1, 2 rather than diverging.
    """
    n = len(graph.nodes)
    children: list[list[int]] = [[] for _ in range(n)]
    indeg = [0] * n
    for e in graph.edges:
        if e.from_ != e.to:
            children[e.from_].append(e.to)
            indeg[e.to] += 1

    color = [0] * n
    dag: list[list[int]] = [[] for _ in range(n)]
    order: list[int] = []

    # Roots first so ranks grow from natural entry points, then any leftovers.
    roots = [i for i in range(n) if indeg[i] == 0]
    for start in [*roots, *range(n)]:
        if color[start] == 0:
            dfsDag(start, children, color, dag, order)

    rank = [0] * n
    for i in range(len(order) - 1, -1, -1):
        u = order[i]
        for v in dag[u]:
            rank[v] = max(rank[v], rank[u] + 1)
    return rank


def dfsDag(start: int, children: list[list[int]], color: list[int], dag: list[list[int]], order: list[int]) -> None:
    """Iterative DFS recording postorder and skipping edges back into the stack."""
    stack: list[list[int]] = [[start, 0]]
    color[start] = 1
    while len(stack) > 0:
        frame = stack[-1]
        u = frame[0]
        if frame[1] < len(children[u]):
            v = children[u][frame[1]]
            frame[1] += 1
            if color[v] == 1:
                continue  # grey: a back edge, ignore it
            dag[u].append(v)
            if color[v] == 0:
                color[v] = 1
                stack.append([v, 0])
        else:
            color[u] = 2
            order.append(u)
            stack.pop()


def orderRanks(byRank: list[list[int]], edges: list[Edge], ranks: list[int]) -> None:
    """Reorder nodes within each rank to minimise edge crossings (barycenter
    sweeps): alternate down/up passes sort each rank by the mean position of its
    neighbours, keeping whichever ordering crossed least."""
    n = len(ranks)
    if len(byRank) < 2 or n < 3:
        return

    parents: list[list[int]] = [[] for _ in range(n)]
    children: list[list[int]] = [[] for _ in range(n)]
    for e in edges:
        if e.from_ != e.to and ranks[e.to] > ranks[e.from_]:
            parents[e.to].append(e.from_)
            children[e.from_].append(e.to)

    pos = [0] * n

    def reindex(row: list[int]) -> None:
        for i, v in enumerate(row):
            pos[v] = i

    for row in byRank:
        reindex(row)

    best = [list(row) for row in byRank]
    bestCrossings = countCrossings(edges, ranks, pos)
    if bestCrossings == 0:
        return

    for it in range(8):
        # Alternate sweeping down (sort by parents) and up (sort by children).
        rows = byRank[1:] if it % 2 == 0 else list(reversed(byRank[:-1]))
        neigh = parents if it % 2 == 0 else children
        for row in rows:
            sortByBarycenter(row, neigh, pos)
            reindex(row)
        crossings = countCrossings(edges, ranks, pos)
        if crossings < bestCrossings:
            bestCrossings = crossings
            best = [list(row) for row in byRank]
        if bestCrossings == 0:
            break

    for i, row in enumerate(byRank):
        row[:] = best[i]


def sortByBarycenter(row: list[int], neigh: list[list[int]], pos: list[float]) -> None:
    keyed = [
        (pos[v] if len(neigh[v]) == 0 else sum(pos[u] for u in neigh[v]) / len(neigh[v]), v)
        for v in row
    ]
    keyed.sort(key=lambda item: item[0])
    for i, (_key, v) in enumerate(keyed):
        row[i] = v


def countCrossings(edges: list[Edge], ranks: list[int], pos: list[float]) -> int:
    adjacent = [
        (ranks[e.from_], pos[e.from_], pos[e.to])
        for e in edges
        if e.from_ != e.to and ranks[e.to] == ranks[e.from_] + 1
    ]
    crossings = 0
    for i in range(len(adjacent)):
        a = adjacent[i]
        for j in range(i + 1, len(adjacent)):
            b = adjacent[j]
            if a[0] == b[0] and ((a[1] < b[1] and a[2] > b[2]) or (a[1] > b[1] and a[2] < b[2])):
                crossings += 1
    return crossings


def assignPositions(byRank: list[list[int]], size: list[int], sep: int, edges: list[Edge], ranks: list[int]) -> list[int]:
    """Assign a cross-axis centre to every node so nodes line up under their
    neighbours: each node drifts toward the average of its neighbours while
    ranks keep their order and boxes keep ``sep`` between them."""
    n = len(size)
    parents: list[list[int]] = [[] for _ in range(n)]
    children: list[list[int]] = [[] for _ in range(n)]
    for e in edges:
        if e.from_ != e.to and ranks[e.to] > ranks[e.from_]:
            parents[e.to].append(e.from_)
            children[e.from_].append(e.to)

    pos: list[float] = [0.0] * n
    for row in byRank:
        x = 0.0
        for v in row:
            h = size[v] / 2
            x += h
            pos[v] = x
            x += h + sep

    for it in range(10):
        rows = byRank if it % 2 == 0 else list(reversed(byRank))
        neigh = parents if it % 2 == 0 else children
        for row in rows:
            relaxRank(row, neigh, pos, size, sep)

    minLeft = math.inf
    for v in range(n):
        minLeft = min(minLeft, pos[v] - size[v] / 2)
    if not math.isfinite(minLeft):
        minLeft = 0
    return [max(0, int(jsRound(pos[v] - minLeft))) for v in range(n)]


def relaxRank(nodes: list[int], neigh: list[list[int]], pos: list[float], size: list[int], sep: int) -> None:
    n = len(nodes)
    if n == 0:
        return

    desired = [pos[v] if len(neigh[v]) == 0 else sum(pos[u] for u in neigh[v]) / len(neigh[v]) for v in nodes]

    def halfOf(i: int) -> float:
        return size[nodes[i]] / 2

    # Sweep right then left, then take the midpoint: this centres a node between
    # the tightest packing that respects order from either side.
    left = [0.0] * n
    for i in range(n):
        left[i] = desired[i] if i == 0 else max(desired[i], left[i - 1] + halfOf(i - 1) + sep + halfOf(i))
    right = [0.0] * n
    for i in range(n - 1, -1, -1):
        right[i] = desired[i] if i == n - 1 else min(desired[i], right[i + 1] - halfOf(i + 1) - sep - halfOf(i))
    for i in range(n):
        pos[nodes[i]] = (left[i] + right[i]) / 2
    for i in range(1, n):
        minP = pos[nodes[i - 1]] + halfOf(i - 1) + sep + halfOf(i)
        pos[nodes[i]] = max(pos[nodes[i]], minP)


# ------------------------------------------------------------------- tracks

# A span competing for a track: `(start, end, from, to, edgeIndex)`.
type Span5 = tuple[int, int, int, int, int]


def assignTracks(spans: list[Span5]) -> tuple[list[tuple[int, int]], int]:
    """Pack spans into as few parallel tracks as possible.

    Two spans share a track when they are two cells apart, or when they share an
    endpoint — edges fanning out of one node deliberately reuse a single row so
    a merge draws one arrowhead rather than a stack of them.
    """
    ordered = sorted(spans)
    tracks: list[list[tuple[int, int, int, int]]] = []
    assigned: list[tuple[int, int]] = []
    for s, e, f, t, idx in ordered:
        slot = next(
            (
                index
                for index, members in enumerate(tracks)
                if all(e2 + 2 <= s or e + 2 <= s2 or f2 == f or t2 == t for s2, e2, f2, t2 in members)
            ),
            -1,
        )
        if slot == -1:
            tracks.append([])
            slot = len(tracks) - 1
        tracks[slot].append((s, e, f, t))
        assigned.append((idx, slot))
    return assigned, len(tracks)


def busSpans(graph: Graph, ranks: list[int], centers: list[int], r: int, exact: bool) -> list[Span5]:
    """Edges from rank ``r`` to ``r + 1`` that must jog sideways, so need a bus row."""
    out: list[Span5] = []
    for i, e in enumerate(graph.edges):
        jogs = centers[e.from_] != centers[e.to] if exact else abs(centers[e.from_] - centers[e.to]) > 1
        if e.from_ != e.to and ranks[e.from_] == r and ranks[e.to] == r + 1 and jogs:
            out.append((
                min(centers[e.from_], centers[e.to]),
                max(centers[e.from_], centers[e.to]),
                e.from_,
                e.to,
                i,
            ))
    return out


def laneSpans(graph: Graph, ranks: list[int], placed: list[Placed], vertical: bool) -> list[Span5]:
    """Edges skipping a rank or running backwards; these go around in a lane."""
    out: list[Span5] = []
    for i, e in enumerate(graph.edges):
        if e.from_ == e.to or ranks[e.to] == ranks[e.from_] + 1:
            continue
        pf = placed[e.from_]
        pt = placed[e.to]
        a = min(pf.cy, pt.cy) if vertical else min(pf.cx, pt.cx)
        b = max(pf.cy, pt.cy) if vertical else max(pf.cx, pt.cx)
        out.append((a, b, e.from_, e.to, i))
    return out


# ----------------------------------------------------------------- placement


def placeTd(ranks: list[int], maxRank: int, byRank: list[list[int]], sizes: NodeSizes, graph: Graph, placed: list[Placed]) -> RoutePlan:
    centers = assignPositions(byRank, sizes.layW, GAP_X, graph.edges, ranks)

    edgeBus = [0] * len(graph.edges)
    busTracks = [0] * (maxRank + 1)
    for r in range(maxRank):
        spans = busSpans(graph, ranks, centers, r, False)
        if len(spans) == 0:
            continue
        assigned, count = assignTracks(spans)
        for idx, slot in assigned:
            edgeBus[idx] = slot
        busTracks[r] = count

    rankH = [3 if len(row) == 0 else max(sizes.boxH[i] + sizes.extraH[i] for i in row) for row in byRank]
    rankY = [0] * (maxRank + 1)
    for r in range(1, maxRank + 1):
        rankY[r] = rankY[r - 1] + rankH[r - 1] + max(GAP_Y, busTracks[r - 1] + 1)
    canvasH = rankY[maxRank] + rankH[maxRank]
    bandEnd = [rankY[r] + rankH[r] for r in range(maxRank + 1)]

    diagramW = 1
    for r, row in enumerate(byRank):
        for idx in row:
            w = sizes.boxW[idx]
            h = sizes.boxH[idx]
            cx = centers[idx]
            x = int(sat(cx, half(w)))
            y = rankY[r] + half(rankH[r] - h - sizes.extraH[idx])
            placed[idx] = Placed(x=x, y=y, w=w, h=h, cx=cx, cy=y + half(h), rank=r)
            diagramW = max(diagramW, x + w)
            if sizes.extraH[idx] > 0 and sizes.selfLabelW[idx] > 0:
                diagramW = max(diagramW, x + w + 2 + sizes.selfLabelW[idx])

    contentW = diagramW
    for e in graph.edges:
        if e.from_ == e.to or e.label is None:
            continue
        lw = min(stringWidth(e.label), MAX_LABEL)
        contentW = (
            max(contentW, placed[e.to].cx + 2 + lw)
            if ranks[e.to] == ranks[e.from_] + 1
            else max(contentW, diagramW + lw + 1)
        )

    edgeLane = [0] * len(graph.edges)
    lanes = laneSpans(graph, ranks, placed, True)
    canvasW = contentW
    laneBase = 0
    if len(lanes) > 0:
        assigned, count = assignTracks(lanes)
        for idx, slot in assigned:
            edgeLane[idx] = slot
        canvasW = contentW + 1 + count
        laneBase = contentW + 1

    return RoutePlan(canvasW=canvasW, canvasH=canvasH, bandEnd=bandEnd, edgeBus=edgeBus, laneBase=laneBase, edgeLane=edgeLane)


def placeLr(ranks: list[int], maxRank: int, byRank: list[list[int]], sizes: NodeSizes, graph: Graph, placed: list[Placed]) -> RoutePlan:
    colW = [0 if len(row) == 0 else max(sizes.boxW[i] for i in row) for row in byRank]

    # Left-to-right edge labels sit in the gap between columns, so the gap has
    # to be wide enough for the widest of them.
    labelWidths = [
        min(stringWidth(e.label), MAX_LABEL)
        for e in graph.edges
        if (e.from_ == e.to or ranks[e.to] == ranks[e.from_] + 1) and e.label is not None
    ]
    maxLabel = 0 if len(labelWidths) == 0 else max(labelWidths)
    baseGap = max(GAP_X + 1, maxLabel + 3)

    centers = assignPositions(byRank, sizes.layH, 1, graph.edges, ranks)

    edgeBus = [0] * len(graph.edges)
    busTracks = [0] * (maxRank + 1)
    for r in range(maxRank):
        spans = busSpans(graph, ranks, centers, r, True)
        if len(spans) == 0:
            continue
        assigned, count = assignTracks(spans)
        for idx, slot in assigned:
            edgeBus[idx] = slot
        busTracks[r] = count

    rankX = [0] * (maxRank + 1)
    for r in range(1, maxRank + 1):
        rankX[r] = rankX[r - 1] + colW[r - 1] + max(baseGap, busTracks[r - 1] + 1)
    selfTails = [2 + sizes.selfLabelW[i] for i in byRank[maxRank] if sizes.extraH[i] > 0 and sizes.selfLabelW[i] > 0]
    canvasW = rankX[maxRank] + colW[maxRank] + (0 if len(selfTails) == 0 else max(selfTails))
    bandEnd = [rankX[r] + colW[r] for r in range(maxRank + 1)]

    diagramH = 1
    for r, row in enumerate(byRank):
        x = rankX[r]
        for idx in row:
            w = sizes.boxW[idx]
            h = sizes.boxH[idx]
            cy = centers[idx]
            y = int(sat(cy, half(h + sizes.extraH[idx])))
            placed[idx] = Placed(x=x, y=y, w=w, h=h, cx=x + half(w), cy=y + half(h), rank=r)
            diagramH = max(diagramH, y + h + sizes.extraH[idx])

    edgeLane = [0] * len(graph.edges)
    lanes = laneSpans(graph, ranks, placed, False)
    canvasH = diagramH
    laneBase = 0
    if len(lanes) > 0:
        assigned, count = assignTracks(lanes)
        for idx, slot in assigned:
            edgeLane[idx] = slot
        canvasH = diagramH + 1 + count
        laneBase = diagramH + 1

    return RoutePlan(canvasW=canvasW, canvasH=canvasH, bandEnd=bandEnd, edgeBus=edgeBus, laneBase=laneBase, edgeLane=edgeLane)


# -------------------------------------------------------------------- canvas


def layoutCanvas(graph: Graph, extras: list[NodeExtra]) -> CanvasResult:
    """Rank, place, draw and route a graph onto a fresh canvas."""
    n = len(graph.nodes)
    if n == 0:
        return None

    ranks = computeRanks(graph)
    maxRank = max([*ranks, 0])

    byRank: list[list[int]] = [[] for _ in range(maxRank + 1)]
    for idx, rank in enumerate(ranks):
        byRank[rank].append(idx)
    orderRanks(byRank, graph.edges, ranks)

    wrapped = [wrapLabel(node.label, WRAP_WIDTH, MAX_LINES) for node in graph.nodes]

    def widest(lines: list[str]) -> int:
        return max(1, 1 if len(lines) == 0 else max(stringWidth(line) for line in lines))

    boxW: list[int] = []
    boxH: list[int] = []
    for i, extra in enumerate(extras):
        if extra.kind == "frame":
            assert extra.sub is not None
            boxW.append(max(extra.sub.w + 2, stringWidth(fitLabel(graph.nodes[i].label, WRAP_WIDTH)) + 4))
            boxH.append(extra.sub.h + 2)
        elif extra.kind == "compartments":
            sections = extra.sections or []
            boxW.append(widest([line for section in sections for line in section]) + 2 * PAD + 2)
            filled = sum(1 for section in sections if len(section) > 0)
            boxH.append(sum(len(section) for section in sections) + int(sat(filled, 1)) + 2)
        else:
            boxW.append(widest(wrapped[i]) + 2 * PAD + 2)
            boxH.append(len(wrapped[i]) + 2)

    # A self-edge needs two rows below its box, and room beside it for a label.
    extraH = [0] * n
    selfLabelW = [0] * n
    for e in graph.edges:
        if e.from_ != e.to:
            continue
        extraH[e.from_] = 2
        if e.label is not None:
            selfLabelW[e.from_] = max(selfLabelW[e.from_], min(stringWidth(e.label), MAX_LABEL))
    for i in range(n):
        if extraH[i] > 0:
            boxW[i] = max(boxW[i], 7)

    sizes = NodeSizes(
        boxW=boxW,
        boxH=boxH,
        layW=[w + (2 * (selfLabelW[i] + 3) if selfLabelW[i] > 0 else 0) for i, w in enumerate(boxW)],
        layH=[h + extraH[i] for i, h in enumerate(boxH)],
        extraH=extraH,
        selfLabelW=selfLabelW,
    )

    placed = [Placed(x=0, y=0, w=0, h=0, cx=0, cy=0, rank=0) for _ in range(n)]

    vertical = graph.dir in ("down", "up")
    plan = (
        placeTd(ranks, maxRank, byRank, sizes, graph, placed)
        if vertical
        else placeLr(ranks, maxRank, byRank, sizes, graph, placed)
    )

    if plan.canvasW * plan.canvasH > MAX_CANVAS_CELLS:
        return None

    canvas = Canvas(plan.canvasW, plan.canvasH)
    for idx in range(n):
        extra = extras[idx]
        if extra.kind == "frame":
            assert extra.sub is not None
            drawFrame(canvas, placed[idx], graph.nodes[idx].label, extra.sub)
        elif extra.kind == "compartments":
            drawClassBox(canvas, placed[idx], extra.sections or [])
        else:
            drawBox(canvas, placed[idx], wrapped[idx], graph.nodes[idx].shape)

    for i, edge in enumerate(graph.edges):
        canvas.curStyle = STY_DOT if edge.line == "dotted" else STY_THICK if edge.line == "thick" else STY_SOLID
        if edge.from_ == edge.to:
            routeSelf(canvas, placed[edge.from_], edge)
            continue
        from_ = placed[edge.from_]
        to = placed[edge.to]
        adjacent = to.rank == from_.rank + 1
        bus = plan.bandEnd[from_.rank] + plan.edgeBus[i]
        lane = plan.laneBase + plan.edgeLane[i]
        if vertical:
            if adjacent:
                routeForward(canvas, from_, to, edge, bus)
            else:
                routeBack(canvas, from_, to, edge, lane)
        elif adjacent:
            routeForwardLr(canvas, from_, to, edge, bus)
        else:
            routeBackLr(canvas, from_, to, edge, lane)

    canvas.finalizeMask()
    return canvas


def orient(canvas: Canvas, graph: Graph) -> Canvas:
    """Apply the direction flip a finished canvas needs for ``BT`` / ``RL``."""
    if graph.dir == "up":
        canvas.flipVertical()
    elif graph.dir == "left":
        canvas.flipHorizontal()
    return canvas


def layoutFlowchart(graph: Graph) -> CanvasResult:
    """Flowchart and state diagrams: plain boxes, no extra content."""
    extras = [NodeExtra(kind="plain") for _ in graph.nodes]
    canvas = layoutCanvas(graph, extras)
    return canvas and orient(canvas, graph)


def layoutClass(graph: Graph, infos: list[ClassInfo]) -> CanvasResult:
    """Class and ER diagrams: boxes divided into title / attribute / method rows."""
    extras: list[NodeExtra] = []
    for i, node in enumerate(graph.nodes):
        title: list[str] = []
        if infos[i].annotation is not None:
            title.append(f"«{infos[i].annotation}»")
        title.append(displayGenerics(node.label))
        extras.append(NodeExtra(kind="compartments", sections=[title, infos[i].attrs, infos[i].methods]))
    canvas = layoutCanvas(graph, extras)
    return canvas and orient(canvas, graph)


def displayGenerics(s: str) -> str:
    out = ""
    isOpen = False
    for c in s:
        if c == "~":
            out += ">" if isOpen else "<"
            isOpen = not isOpen
        else:
            out += c
    return out


# -------------------------------------------------------------------- groups

type ItemKey = str


def nodeKey(i: int) -> ItemKey:
    return f"n{i}"


def groupKey(i: int) -> ItemKey:
    return f"g{i}"


def layoutGrouped(graph: Graph) -> CanvasResult:
    """Lay out a flowchart that uses ``subgraph``.

    Each subgraph becomes a framed box holding its own independently laid-out
    canvas. An edge is drawn in the innermost scope containing both endpoints;
    one crossing a subgraph boundary attaches to the frame instead of the node.
    """
    # A node whose id matches a subgraph id stands in for that subgraph.
    proxy: dict[int, int] = {}
    for gi, g in enumerate(graph.groups):
        ni = graph.index.get(g.id)
        if ni is not None:
            proxy[ni] = gi

    def groupChain(g: int | None) -> list[int]:
        chain: list[int] = []
        cur = g
        while cur is not None:
            chain.append(cur)
            cur = graph.groups[cur].parent
        return list(reversed(chain))

    def endpoint(n: int) -> tuple[ItemKey, list[int]]:
        gi = proxy.get(n)
        return (
            (nodeKey(n), groupChain(graph.nodeGroup[n]))
            if gi is None
            else (groupKey(gi), groupChain(graph.groups[gi].parent))
        )

    # Edges bucketed by the scope that draws them; `None` is the top level.
    scopeEdges: dict[int | None, list[tuple[ItemKey, ItemKey, int]]] = {}
    referenced = [False] * len(graph.groups)
    for ei, e in enumerate(graph.edges):
        fKeyRaw, fChain = endpoint(e.from_)
        tKeyRaw, tChain = endpoint(e.to)
        k = 0
        while k < len(fChain) and k < len(tChain) and fChain[k] == tChain[k]:
            k += 1
        scope = None if k == 0 else fChain[k - 1]
        fKey = groupKey(fChain[k]) if len(fChain) > k else fKeyRaw
        tKey = groupKey(tChain[k]) if len(tChain) > k else tKeyRaw
        for key in (fKey, tKey):
            if key.startswith("g"):
                referenced[int(key[1:])] = True
        scopeEdges.setdefault(scope, []).append((fKey, tKey, ei))

    directNodes: dict[int | None, list[int]] = {}
    for ni, g in enumerate(graph.nodeGroup):
        if ni in proxy:
            continue
        directNodes.setdefault(g, []).append(ni)

    # Drop empty subgraphs, but keep any that an edge attaches to.
    keep = [False] * len(graph.groups)
    for gi in range(len(graph.groups) - 1, -1, -1):
        hasNodes = len(directNodes.get(gi, [])) > 0
        hasChildren = any(g.parent == gi and keep[c] for c, g in enumerate(graph.groups))
        keep[gi] = hasNodes or hasChildren or referenced[gi]

    canvas = buildScope(graph, None, scopeEdges, directNodes, keep)
    return canvas and orient(canvas, graph)


def buildScope(
    graph: Graph,
    scope: int | None,
    scopeEdges: dict[int | None, list[tuple[ItemKey, ItemKey, int]]],
    directNodes: dict[int | None, list[int]],
    keep: list[bool],
) -> CanvasResult:
    items: list[ItemKey] = [nodeKey(i) for i in directNodes.get(scope, [])]
    childGroups = [gi for gi in range(len(graph.groups)) if graph.groups[gi].parent == scope and keep[gi]]
    items.extend(groupKey(gi) for gi in childGroups)

    if len(items) == 0:
        return Canvas(1, 1)

    indexOf: dict[ItemKey, int] = {}
    nodes: list[Node] = []
    extras: list[NodeExtra] = []
    for item in items:
        indexOf[item] = len(nodes)
        i = int(item[1:])
        if item.startswith("n"):
            nodes.append(Node(label=graph.nodes[i].label, shape=graph.nodes[i].shape))
            extras.append(NodeExtra(kind="plain"))
        else:
            sub = buildScope(graph, i, scopeEdges, directNodes, keep)
            if sub is None:
                return None
            nodes.append(Node(label=graph.groups[i].label, shape="rect"))
            extras.append(NodeExtra(kind="frame", sub=sub))

    edges: list[Edge] = []
    for f, t, ei in scopeEdges.get(scope, []):
        fi = indexOf.get(f)
        ti = indexOf.get(t)
        if fi is None or ti is None:
            continue
        e = graph.edges[ei]
        edges.append(Edge(from_=fi, to=ti, label=e.label, headTo=e.headTo, headFrom=e.headFrom, line=e.line))

    # Layout only reads nodes/edges/dir, so a bare Graph carrying those is enough.
    synth = Graph(graph.dir)
    synth.nodes = nodes
    synth.edges = edges
    return layoutCanvas(synth, extras)


# ------------------------------------------------------------------- drawing


def drawBox(canvas: Canvas, p: Placed, lines: list[str], shape: Shape) -> None:
    x, y, w, h = p.x, p.y, p.w, p.h
    right = x + w - 1
    bottom = y + h - 1

    rounded = shape in ("round", "diamond")
    canvas.set(x, y, "╭" if rounded else "┌", "border")
    canvas.set(right, y, "╮" if rounded else "┐", "border")
    canvas.set(x, bottom, "╰" if rounded else "└", "border")
    canvas.set(right, bottom, "╯" if rounded else "┘", "border")

    # The perimeter is drawn as bits so edges can tee into it, but it is the box
    # outline, so it claims `border` rather than `edge`.
    for cx in range(x + 1, right):
        canvas.addBits(cx, y, L | R, "border")
        canvas.addBits(cx, bottom, L | R, "border")
    for cy in range(y + 1, bottom):
        canvas.addBits(x, cy, U | D, "border")
        canvas.addBits(right, cy, U | D, "border")

    for cy in range(y, bottom + 1):
        for cx in range(x, right + 1):
            i = canvas.idx(cx, cy)
            if 0 <= i < len(canvas.occupied):
                canvas.occupied[i] = 1

    inner = max(1, int(sat(w, 2 * PAD + 2)))
    for li, line in enumerate(lines):
        text = fitLabel(line, inner)
        textX = x + 1 + PAD + half(sat(inner, stringWidth(text)))
        drawText(canvas, text, textX, y + 1 + li, "text")


def drawClassBox(canvas: Canvas, p: Placed, sections: list[list[str]]) -> None:
    """A class or ER box: sections separated by horizontal rules, title centred."""
    drawBox(canvas, p, [], "rect")
    inner = max(1, int(sat(p.w, 2 * PAD + 2)))
    row = p.y + 1
    first = True
    for si, section in enumerate(sections):
        if len(section) == 0:
            continue
        if not first:
            canvas.set(p.x, row, "├", "border")
            for x in range(p.x + 1, p.x + p.w - 1):
                canvas.set(x, row, "─", "border")
            canvas.set(p.x + p.w - 1, row, "┤", "border")
            row += 1
        first = False
        for line in section:
            text = fitLabel(line, inner)
            tx = p.x + 1 + PAD + half(sat(inner, stringWidth(text))) if si == 0 else p.x + 1 + PAD
            drawTextOverEdges(canvas, text, tx, row, "text")
            row += 1


def drawFrame(canvas: Canvas, p: Placed, title: str, sub: Canvas) -> None:
    """A subgraph frame: a titled box with a finished sub-canvas centred inside."""
    drawBox(canvas, p, [], "rect")
    t = fitLabel(title, int(sat(p.w, 4)))
    drawTextOverEdges(canvas, f" {t} ", p.x + 1, p.y, "text")
    canvas.blit(sub, p.x + 1 + half(p.w - 2 - sub.w), p.y + 1 + half(p.h - 2 - sub.h))


# ------------------------------------------------------------------- routing

_TRIANGLES: Final = {"▼": "▽", "▲": "△", "◄": "◁", "▶": "▷"}


def headGlyph(head: Head, arrow: str) -> str:
    match head:
        case "circle":
            return "o"
        case "cross":
            return "×"
        case "diamondFill":
            return "◆"
        case "diamondOpen":
            return "◇"
        case "triangle":
            return _TRIANGLES.get(arrow, arrow)
        case _:
            return arrow


def routeForward(canvas: Canvas, from_: Placed, to: Placed, edge: Edge, bus: int) -> None:
    """Adjacent ranks, top-down: drop, jog along the bus row, drop into the head."""
    tx = to.cx
    # A jog of one column reads as a kink; snap straight instead.
    bx = tx if abs(from_.cx - tx) <= 1 else from_.cx
    by = from_.y + from_.h - 1
    headRow = to.y - 1

    canvas.junction(bx, by, D)
    canvas.segV(bx, by, bus)
    if bx == tx:
        canvas.segV(bx, bus, headRow)
    else:
        canvas.segH(bus, bx, tx)
        canvas.segV(tx, bus, headRow)

    if edge.headTo == "none":
        canvas.addBits(tx, headRow, U)
    else:
        canvas.set(tx, headRow, headGlyph(edge.headTo, "▼"), "edge")
    if edge.headFrom != "none":
        canvas.set(bx, by, headGlyph(edge.headFrom, "▲"), "edge")

    if edge.label is not None:
        placeLabel(canvas, edge.label, headRow, tx + 1)


def routeSelf(canvas: Canvas, p: Placed, edge: Edge) -> None:
    """A self-edge: a stub loop hanging below the box."""
    bottom = p.y + p.h - 1
    exitX = p.cx + 1
    retX = p.x + p.w - 2
    if retX <= exitX or bottom + 2 >= canvas.h:
        return

    v, h, bl, br = (
        ("╎", "╌", "╰", "╯") if edge.line == "dotted" else ("┃", "━", "┗", "┛") if edge.line == "thick" else ("│", "─", "╰", "╯")
    )

    canvas.junction(exitX, bottom, D)
    canvas.set(exitX, bottom + 1, v, "edge")
    canvas.set(exitX, bottom + 2, bl, "edge")
    for x in range(exitX + 1, retX):
        canvas.set(x, bottom + 2, h, "edge")
    canvas.set(retX, bottom + 2, br, "edge")
    canvas.set(retX, bottom + 1, headGlyph(edge.headTo, "▲"), "edge")
    if edge.label is not None:
        placeLabel(canvas, edge.label, bottom + 1, p.x + p.w + 1)


def routeBack(canvas: Canvas, from_: Placed, to: Placed, edge: Edge, laneX: int) -> None:
    """Skip or back edge, top-down: out the right side, up a lane, back in."""
    sx = from_.x + from_.w - 1
    sy = from_.cy
    tx = to.x + to.w - 1
    tyc = to.cy

    canvas.junction(sx, sy, R)
    canvas.segH(sy, sx, laneX)
    canvas.segV(laneX, sy, tyc)
    canvas.segH(tyc, tx + 1, laneX)

    if edge.headTo == "none":
        canvas.addBits(tx + 1, tyc, R)
    else:
        canvas.set(tx + 1, tyc, headGlyph(edge.headTo, "◄"), "edge")
    if edge.headFrom != "none":
        canvas.set(sx, sy, headGlyph(edge.headFrom, "◄"), "edge")

    if edge.label is not None:
        placeLabel(canvas, edge.label, int(sat(tyc, 1)), int(sat(laneX, stringWidth(edge.label) + 1)))


def routeForwardLr(canvas: Canvas, from_: Placed, to: Placed, edge: Edge, bus: int) -> None:
    """Adjacent ranks, left-to-right: out the right side, jog on the bus column."""
    rx = from_.x + from_.w - 1
    ry = from_.cy
    ly = to.cy
    headCol = to.x - 1

    canvas.junction(rx, ry, R)
    canvas.segH(ry, rx, bus)
    if ry == ly:
        canvas.segH(ry, bus, headCol)
    else:
        canvas.segV(bus, ry, ly)
        canvas.segH(ly, bus, headCol)

    if edge.headTo == "none":
        canvas.addBits(headCol, ly, R)
    else:
        canvas.set(headCol, ly, headGlyph(edge.headTo, "▶"), "edge")
    if edge.headFrom != "none":
        canvas.set(rx, ry, headGlyph(edge.headFrom, "◄"), "edge")

    if edge.label is not None:
        placeLabel(canvas, edge.label, int(sat(ly, 1)), bus + 1)


def routeBackLr(canvas: Canvas, from_: Placed, to: Placed, edge: Edge, laneY: int) -> None:
    """Skip or back edge, left-to-right: down out the bottom, along a lane, back up."""
    sx = from_.cx
    sy = from_.y + from_.h - 1
    tx = to.cx
    ty = to.y + to.h - 1

    canvas.junction(sx, sy, D)
    canvas.segV(sx, sy, laneY)
    canvas.segH(laneY, sx, tx)
    canvas.segV(tx, laneY, ty + 1)

    if edge.headTo == "none":
        canvas.addBits(tx, ty + 1, D)
    else:
        canvas.set(tx, ty + 1, headGlyph(edge.headTo, "▲"), "edge")
    if edge.headFrom != "none":
        canvas.set(sx, sy, headGlyph(edge.headFrom, "▲"), "edge")

    if edge.label is not None:
        placeLabel(canvas, edge.label, int(sat(laneY, 1)), half(sx + tx))


def placeLabel(canvas: Canvas, label: str, row: int, startX: int) -> None:
    """Write an edge label, stopping at the first cell already occupied."""
    if row >= canvas.h:
        return
    text = fitLabel(label, MAX_LABEL)
    x = startX
    for c, cw in measured(text):
        if cw == 0:
            continue
        if x + cw > canvas.w:
            break
        blocked = False
        for k in range(cw):
            i = canvas.idx(x + k, row)
            # A cell past the arrays is `undefined` in JavaScript, which is never a blank.
            if not 0 <= i < len(canvas.ch) or canvas.ch[i] != " " or canvas.mask[i] != 0 or canvas.occupied[i]:
                blocked = True
        if blocked:
            break
        canvas.set(x, row, c, "edgeLabel")
        for k in range(1, cw):
            canvas.set(x + k, row, CONT, "edgeLabel")
        x += cw


__all__ = [
    "CanvasResult",
    "NodeExtra",
    "Placed",
    "assignPositions",
    "assignTracks",
    "computeRanks",
    "countCrossings",
    "drawBox",
    "layoutCanvas",
    "layoutClass",
    "layoutFlowchart",
    "layoutGrouped",
    "orderRanks",
    "orient",
]

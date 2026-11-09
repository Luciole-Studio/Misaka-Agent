"""The shared diagram model. Flowchart, state, class and ER sources all parse
into a ``Graph``; only sequence diagrams have their own model.

Translated from grok-mermaid 0.2.3's ``src/graph.ts``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Literal

from .labels import asciiUpper

# Caps that keep layout bounded; exceeding one drops the diagram to fallback.
MAX_NODES: Final = 128
MAX_EDGES: Final = 512
MAX_GROUPS: Final = 24
MAX_GROUP_DEPTH: Final = 6
# Class members / ER attributes listed per box before eliding with `…`.
MAX_MEMBERS: Final = 8

type Shape = Literal["rect", "round", "diamond"]

# Decoration at one end of an edge.
type Head = Literal["none", "arrow", "circle", "cross", "triangle", "diamondFill", "diamondOpen"]

type LineKind = Literal["solid", "dotted", "thick"]

type Dir = Literal["down", "up", "right", "left"]


@dataclass(slots=True)
class Node:
    label: str
    shape: Shape


@dataclass(slots=True)
class Edge:
    from_: int
    to: int
    label: str | None
    headTo: Head
    headFrom: Head
    line: LineKind


@dataclass(slots=True)
class Group:
    id: str
    label: str
    parent: int | None


@dataclass(slots=True)
class ClassInfo:
    """Extra compartment content for class and ER boxes."""

    annotation: str | None
    attrs: list[str]
    methods: list[str]


def emptyClassInfo() -> ClassInfo:
    return ClassInfo(annotation=None, attrs=[], methods=[])


def parseDir(token: str) -> Dir:
    """``LR``/``RL``/``BT`` as written in a header or ``direction`` statement; else ``down``."""
    match asciiUpper(token):
        case "LR":
            return "right"
        case "RL":
            return "left"
        case "BT":
            return "up"
        case _:
            return "down"


@dataclass(slots=True)
class Graph:
    dir: Dir = "down"
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    index: dict[str, int] = field(default_factory=dict)
    groups: list[Group] = field(default_factory=list)
    # Innermost subgraph each node was declared in, parallel to `nodes`.
    nodeGroup: list[int | None] = field(default_factory=list)
    curGroup: int | None = None
    # Set when a cap was hit; the caller abandons the parse.
    overCap: bool = False
    # Text the flowchart grammar could not read and silently discarded.
    #
    # Flowchart parsing is deliberately lenient — a malformed statement
    # contributes whatever prefix parsed and the rest is dropped — so without
    # these the reader gets a clean diagram that is not what they wrote.
    warnings: list[str] = field(default_factory=list)

    def nodeIndex(self, id: str, label: str | None, shape: Shape) -> int | None:
        """Index of ``id``, creating the node if new. A later declaration carrying a
        label overwrites the placeholder one an edge created. Returns ``None`` once
        ``MAX_NODES`` is reached, which aborts the parse."""
        existing = self.index.get(id)
        if existing is not None:
            if label is not None:
                self.nodes[existing].label = label
                self.nodes[existing].shape = shape
            return existing
        if len(self.nodes) >= MAX_NODES:
            self.overCap = True
            return None
        self.index[id] = len(self.nodes)
        self.nodes.append(Node(label=label if label is not None else id, shape=shape))
        self.nodeGroup.append(self.curGroup)
        return len(self.nodes) - 1

    def nodeLabel(self, id: str, label: str) -> int | None:
        """Set a node's label without disturbing its shape, creating it if new."""
        existing = self.index.get(id)
        if existing is not None:
            self.nodes[existing].label = label
            return existing
        return self.nodeIndex(id, label, "round")

    def pushEdge(self, edge: Edge) -> bool:
        """Append an edge, or flag ``overCap`` when ``MAX_EDGES`` is reached."""
        if len(self.edges) >= MAX_EDGES:
            self.overCap = True
            return False
        self.edges.append(edge)
        return True


__all__ = [
    "MAX_EDGES",
    "MAX_GROUPS",
    "MAX_GROUP_DEPTH",
    "MAX_MEMBERS",
    "MAX_NODES",
    "ClassInfo",
    "Dir",
    "Edge",
    "Graph",
    "Group",
    "Head",
    "LineKind",
    "Node",
    "Shape",
    "emptyClassInfo",
    "parseDir",
]

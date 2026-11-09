"""One version of a graph, held in memory and read-only.

Everything a reader asks of a graph is asked of a snapshot: a node or an edge by id, the nodes or
edges of a kind, the edges leaving or entering a node. Lists come in creation order. Algorithms
run on snapshots and belong to whoever needs them; the graph keeps none of its own.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from .model import Edge, Node


@dataclass(frozen=True, slots=True, eq=False)
class Snapshot:
    """The graph ``graph`` as of application ``version`` (0 before the first)."""

    graph: str
    version: int
    _nodes: dict[str, Node]
    _edges: dict[str, Edge]
    _out: dict[str, tuple[Edge, ...]] = field(default_factory=dict)
    _in: dict[str, tuple[Edge, ...]] = field(default_factory=dict)

    @classmethod
    def of(cls, graph: str, version: int, nodes: Iterable[Node], edges: Iterable[Edge]) -> Snapshot:
        ordered_edges = sorted(edges, key=lambda edge: edge.ordinal)
        out: dict[str, list[Edge]] = {}
        into: dict[str, list[Edge]] = {}
        for edge in ordered_edges:
            out.setdefault(edge.source, []).append(edge)
            into.setdefault(edge.target, []).append(edge)
        return cls(
            graph=graph,
            version=version,
            _nodes={node.id: node for node in sorted(nodes, key=lambda node: node.ordinal)},
            _edges={edge.id: edge for edge in ordered_edges},
            _out={node_id: tuple(found) for node_id, found in out.items()},
            _in={node_id: tuple(found) for node_id, found in into.items()},
        )

    def node(self, node_id: str) -> Node | None:
        return self._nodes.get(node_id)

    def edge(self, edge_id: str) -> Edge | None:
        return self._edges.get(edge_id)

    def nodes(self, kind: str | None = None) -> tuple[Node, ...]:
        return tuple(node for node in self._nodes.values() if kind is None or node.kind == kind)

    def edges(self, kind: str | None = None) -> tuple[Edge, ...]:
        return tuple(edge for edge in self._edges.values() if kind is None or edge.kind == kind)

    def out_edges(self, node_id: str, kind: str | None = None) -> tuple[Edge, ...]:
        """The edges leaving ``node_id``, of ``kind`` when one is named."""
        return tuple(edge for edge in self._out.get(node_id, ()) if kind is None or edge.kind == kind)

    def in_edges(self, node_id: str, kind: str | None = None) -> tuple[Edge, ...]:
        """The edges entering ``node_id``, of ``kind`` when one is named."""
        return tuple(edge for edge in self._in.get(node_id, ()) if kind is None or edge.kind == kind)

    def __contains__(self, element_id: object) -> bool:
        return element_id in self._nodes or element_id in self._edges

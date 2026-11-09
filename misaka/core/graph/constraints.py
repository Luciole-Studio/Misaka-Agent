"""Whether a graph keeps its schema: pure functions over snapshots, no storage.

:func:`check` compares the graph before a change with the graph after it and returns every way the
result breaks the schema. It reads the whole graph each time, which at the sizes the graph is built
for (hundreds of elements) costs nothing and leaves no rule depending on what a change touched.
Rules that are about change itself (``frozen``) are the reason it sees the graph before.
"""

from __future__ import annotations

from collections import Counter

from .model import Edge, EdgeKind, Schema, Violation
from .snapshot import Snapshot

_MISSING = object()


def check(schema: Schema, before: Snapshot, after: Snapshot) -> list[Violation]:
    """Every way ``after`` breaks ``schema``; ``before`` is the graph the change started from."""
    found = _kinds(schema, after) + _counts(schema, after) + _frozen(schema, before, after)
    for name, kind in schema.edges.items():
        edges = after.edges(name)
        found += _endpoints(name, kind, edges, after)
        found += _degrees(name, kind, edges)
        if kind.unique:
            found += _duplicates(name, edges)
        if kind.acyclic:
            found += _acyclic(name, kind, edges)
    return found


def _kinds(schema: Schema, after: Snapshot) -> list[Violation]:
    found = [Violation("node.kind", f"node {node.id} is a {node.kind!r}, a kind the schema does not declare", (node.id,))
             for node in after.nodes() if node.kind not in schema.nodes]
    found += [Violation("edge.kind", f"edge {edge.id} is a {edge.kind!r}, a kind the schema does not declare", (edge.id,))
              for edge in after.edges() if edge.kind not in schema.edges]
    return found


def _counts(schema: Schema, after: Snapshot) -> list[Violation]:
    counts = Counter(node.kind for node in after.nodes())
    return [Violation("node.max", f"{counts[name]} {name!r} nodes exceed the limit of {kind.max}", ())
            for name, kind in schema.nodes.items() if kind.max is not None and counts[name] > kind.max]


def _frozen(schema: Schema, before: Snapshot, after: Snapshot) -> list[Violation]:
    found = []
    for old in before.nodes():
        kind, new = schema.nodes.get(old.kind), after.node(old.id)
        if kind is None or new is None:
            continue
        changed = sorted(name for name in kind.frozen
                         if old.attrs.get(name, _MISSING) != new.attrs.get(name, _MISSING))
        if changed:
            found.append(Violation("node.frozen", f"node {old.id}: {', '.join(changed)} cannot change once set", (old.id,)))
    for old in before.edges():
        kind = schema.edges.get(old.kind)
        if kind is None or not kind.frozen:
            continue
        new = after.edge(old.id)
        if new is None:
            found.append(Violation("edge.frozen", f"edge {old.id} ({old.kind}) cannot be retired", (old.id,)))
        elif new.attrs != old.attrs:
            found.append(Violation("edge.frozen", f"edge {old.id} ({old.kind}) cannot change", (old.id,)))
    return found


def _endpoints(name: str, kind: EdgeKind, edges: tuple[Edge, ...], after: Snapshot) -> list[Violation]:
    found = []
    for edge in edges:
        for side, node_id, allowed in (("from", edge.source, kind.source), ("to", edge.target, kind.target)):
            node = after.node(node_id)
            if node is None:
                found.append(Violation("edge.endpoint", f"edge {edge.id} ({name}) runs {side} {node_id}, which is not a node",
                                       (edge.id, node_id)))
            elif node.kind not in allowed:
                found.append(Violation(
                    "edge.endpoint",
                    f"edge {edge.id} ({name}) runs {side} a {node.kind!r} node; {name} edges run {side} "
                    f"{', '.join(sorted(allowed))}",
                    (edge.id, node_id),
                ))
    return found


def _degrees(name: str, kind: EdgeKind, edges: tuple[Edge, ...]) -> list[Violation]:
    found = []
    for rule, direction, limit, ends in (
        ("edge.max_in", "incoming", kind.max_in, [edge.target for edge in edges]),
        ("edge.max_out", "outgoing", kind.max_out, [edge.source for edge in edges]),
    ):
        if limit is None:
            continue
        found += [Violation(rule, f"node {node_id} has {count} {direction} {name} edges; at most {limit} are allowed",
                            (node_id,))
                  for node_id, count in Counter(ends).items() if count > limit]
    return found


def _duplicates(name: str, edges: tuple[Edge, ...]) -> list[Violation]:
    pairs: dict[tuple[str, str], list[str]] = {}
    for edge in edges:
        pairs.setdefault((edge.source, edge.target), []).append(edge.id)
    return [Violation("edge.unique", f"{len(ids)} {name} edges run from {source} to {target}; one is allowed", tuple(ids))
            for (source, target), ids in pairs.items() if len(ids) > 1]


def _acyclic(name: str, kind: EdgeKind, edges: tuple[Edge, ...]) -> list[Violation]:
    """No cycle of ``name`` edges, and, when bounded, no path of them longer than ``max_depth``."""
    successors: dict[str, list[str]] = {}
    indegree: Counter[str] = Counter()
    for edge in edges:
        successors.setdefault(edge.source, []).append(edge.target)
        successors.setdefault(edge.target, [])
        indegree[edge.target] += 1
    # Kahn's order. A node leaves it only once every edge into it has, so its depth is final by then;
    # a node it never reaches lies on a cycle or downstream of one.
    depth = {node_id: 0 for node_id in successors if not indegree[node_id]}
    ready = list(depth)
    remaining = Counter(indegree)
    order: list[str] = []
    while ready:
        node_id = ready.pop()
        order.append(node_id)
        for successor in successors[node_id]:
            depth[successor] = max(depth.get(successor, 0), depth[node_id] + 1)
            remaining[successor] -= 1
            if not remaining[successor]:
                ready.append(successor)
    if len(order) < len(successors):
        stuck = set(successors) - set(order)
        predecessors: dict[str, list[str]] = {node_id: [] for node_id in successors if node_id in stuck}
        for edge in edges:
            if edge.source in stuck and edge.target in stuck:
                predecessors[edge.target].append(edge.source)
        cycle = _cycle(predecessors)
        return [Violation("edge.acyclic", f"{name} edges form a cycle: {' -> '.join(cycle)}", tuple(cycle[:-1]))]
    if kind.max_depth is None:
        return []
    return [Violation("edge.max_depth", f"node {node_id} lies {depth[node_id]} {name} edges deep; "
                      f"at most {kind.max_depth} are allowed", (node_id,))
            for node_id in order if depth[node_id] > kind.max_depth]


def _cycle(predecessors: dict[str, list[str]]) -> list[str]:
    """One cycle, its first node repeated at the end. Every node in ``predecessors`` has one there
    (a node Kahn's order never reached waits on another it never reached), so walking backwards
    always goes on, and must come round."""
    path: list[str] = []
    seen: dict[str, int] = {}
    node_id = next(iter(predecessors))
    while node_id not in seen:
        seen[node_id] = len(path)
        path.append(node_id)
        node_id = predecessors[node_id][0]
    loop = path[seen[node_id]:][::-1]
    return [*loop, loop[0]]

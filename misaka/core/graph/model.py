"""Values the graph hands out and takes in: schemas, nodes, edges, journal entries, leases, errors.

Every value is immutable. Attributes, payloads, receipts and the other JSON a caller passes in are
frozen on the way in (:func:`freeze`): objects and arrays that refuse to change but still compare
equal to plain ones and serialize as JSON. Snapshots are cached and shared between readers, so
nothing read from one may change under another; :func:`thaw` (or ``copy.deepcopy``) gives a plain
copy to edit and write back.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


class GraphError(ValueError):
    """A request the graph refuses."""


class SchemaError(GraphError):
    """A schema that does not describe a graph."""


class JournalError(GraphError):
    """A journal transition that is not allowed."""


class LeaseError(GraphError):
    """A write by someone who does not hold the graph's lease."""


@dataclass(frozen=True, slots=True)
class Violation:
    """One way a graph would break its schema: the rule, what is wrong, and the elements involved."""

    rule: str
    message: str
    elements: tuple[str, ...] = ()


class ConstraintError(GraphError):
    """A change that would leave the graph breaking its schema. Nothing of it was written."""

    def __init__(self, violations: Iterable[Violation]) -> None:
        self.violations = tuple(violations)
        super().__init__("; ".join(violation.message for violation in self.violations))


# --- frozen JSON ------------------------------------------------------------------------------


def _refuse(self: Any, *args: Any, **kwargs: Any) -> Any:
    raise TypeError("graph values are read-only; change graph.thaw(value) and write that back")


class FrozenDict(dict):
    """A JSON object that cannot change. It is a ``dict``: it compares, iterates and serializes as one."""

    __slots__ = ()
    __setitem__ = __delitem__ = __ior__ = _refuse
    clear = pop = popitem = setdefault = update = _refuse

    def __copy__(self) -> dict[str, Any]:
        return dict(self)

    def __deepcopy__(self, memo: dict[int, Any]) -> Any:
        return thaw(self)

    def __reduce__(self) -> tuple[Any, ...]:
        return (FrozenDict, (dict(self),))


class FrozenList(list):
    """A JSON array that cannot change. It is a ``list``: it compares, iterates and serializes as one."""

    __slots__ = ()
    __setitem__ = __delitem__ = __iadd__ = __imul__ = _refuse
    append = extend = insert = pop = remove = clear = sort = reverse = _refuse

    def __copy__(self) -> list[Any]:
        return list(self)

    def __deepcopy__(self, memo: dict[int, Any]) -> Any:
        return thaw(self)

    def __reduce__(self) -> tuple[Any, ...]:
        return (FrozenList, (list(self),))


def freeze(value: Any) -> Any:
    """``value`` as a frozen JSON value. Tuples become arrays; anything JSON cannot hold is refused."""
    if isinstance(value, FrozenDict | FrozenList):
        return value
    if isinstance(value, Mapping):
        frozen = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise GraphError(f"JSON object keys are strings, not {key!r}")
            frozen[key] = freeze(item)
        return FrozenDict(frozen)
    if isinstance(value, list | tuple):
        return FrozenList(freeze(item) for item in value)
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise GraphError("JSON has no NaN or infinity")
    raise GraphError(f"not a JSON value: {type(value).__name__}")


def thaw(value: Any) -> Any:
    """A plain, mutable copy of a JSON value."""
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [thaw(item) for item in value]
    return value


def dumps(value: Any) -> str:
    """The canonical text of a JSON value: what is stored, and what two payloads compare by."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def loads(text: str | None) -> Any:
    return None if text is None else freeze(json.loads(text))


# --- schema -----------------------------------------------------------------------------------


def _limit(value: Any, what: str, problems: list[str]) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        problems.append(f"{what} must be a whole number of 0 or more, not {value!r}")
        return None
    return value


def _names(value: Any, what: str, problems: list[str]) -> frozenset[str]:
    if isinstance(value, str) or not isinstance(value, Iterable):
        problems.append(f"{what} must be a list of names")
        return frozenset()
    names = list(value)
    if not all(isinstance(name, str) and name for name in names):
        problems.append(f"{what} must be a list of names")
        return frozenset()
    return frozenset(names)


def _flag(value: Any, what: str, problems: list[str]) -> bool:
    if not isinstance(value, bool):
        problems.append(f"{what} must be true or false")
        return False
    return value


def _fields(value: Any, allowed: set[str], what: str, problems: list[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        problems.append(f"{what} must be an object")
        return {}
    extra = sorted(map(str, set(value) - allowed))
    if extra:
        problems.append(f"{what} has unknown fields: {', '.join(extra)}")
    return value


def _table(value: Any, what: str, problems: list[str]) -> dict[str, Any]:
    """An object of named entries (``nodes``, ``edges``), keeping the entries whose names are text."""
    if not isinstance(value, Mapping):
        problems.append(f"{what} must be an object")
        return {}
    named = {name: spec for name, spec in value.items() if isinstance(name, str) and name}
    if len(named) != len(value):
        problems.append(f"{what} must be named by non-empty text")
    return named


@dataclass(frozen=True, slots=True)
class NodeKind:
    """A kind of node: at most ``max`` of them at once, and the attributes fixed once one is made."""

    max: int | None = None
    frozen: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        problems: list[str] = []
        _limit(self.max, "max", problems)
        object.__setattr__(self, "frozen", _names(self.frozen, "frozen", problems))
        if problems:
            raise SchemaError("; ".join(problems))


@dataclass(frozen=True, slots=True)
class EdgeKind:
    """A kind of edge: which node kinds it runs from and to, and the shape its edges must keep.

    ``max_in`` / ``max_out`` bound each node's edges of this kind (a tree is ``max_in=1``);
    ``unique`` allows one such edge per ordered pair of nodes; ``acyclic`` forbids a cycle of
    them; ``max_depth`` bounds the longest path of them from any node without one coming in,
    and needs ``acyclic``; a ``frozen`` edge is never changed or retired once made.
    """

    source: frozenset[str]
    target: frozenset[str]
    max_in: int | None = None
    max_out: int | None = None
    unique: bool = False
    acyclic: bool = False
    max_depth: int | None = None
    frozen: bool = False

    def __post_init__(self) -> None:
        problems: list[str] = []
        object.__setattr__(self, "source", _names(self.source, "from", problems))
        object.__setattr__(self, "target", _names(self.target, "to", problems))
        for field in ("max_in", "max_out", "max_depth"):
            _limit(getattr(self, field), field, problems)
        for field in ("unique", "acyclic", "frozen"):
            _flag(getattr(self, field), field, problems)
        if problems:
            raise SchemaError("; ".join(problems))


@dataclass(frozen=True, slots=True)
class Schema:
    """What a graph may hold: its node kinds, its edge kinds, and the ops whose entries always wait for
    approval. Kind and op names mean nothing to the graph; whoever declares them gives them meaning."""

    nodes: Mapping[str, NodeKind]
    edges: Mapping[str, EdgeKind]
    gates: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "nodes", MappingProxyType(dict(self.nodes)))
        object.__setattr__(self, "edges", MappingProxyType(dict(self.edges)))
        object.__setattr__(self, "gates", frozenset(self.gates))
        problems = []
        for name, kind in self.nodes.items():
            if not isinstance(name, str) or not name or not isinstance(kind, NodeKind):
                problems.append(f"node kind {name!r} must be a name with a NodeKind")
        for name, kind in self.edges.items():
            if not isinstance(name, str) or not name or not isinstance(kind, EdgeKind):
                problems.append(f"edge kind {name!r} must be a name with an EdgeKind")
                continue
            for side, kinds in (("from", kind.source), ("to", kind.target)):
                if not kinds:
                    problems.append(f"edge kind {name!r} needs at least one node kind to run {side}")
                unknown = sorted(kinds - set(self.nodes))
                if unknown:
                    problems.append(f"edge kind {name!r} runs {side} undeclared node kinds: {', '.join(unknown)}")
            if kind.max_depth is not None and not kind.acyclic:
                problems.append(f"edge kind {name!r} bounds its depth, which needs acyclic: true")
        if not all(isinstance(gate, str) and gate for gate in self.gates):
            problems.append("gates must be a list of op names")
        if problems:
            raise SchemaError("; ".join(problems))

    @classmethod
    def from_json(cls, data: Any) -> Schema:
        """Read the JSON form, ``{"nodes": {...}, "edges": {...}, "gates": [...]}``, reporting every problem at once."""
        if isinstance(data, Schema):
            return data
        problems: list[str] = []
        top = _fields(data, {"nodes", "edges", "gates"}, "a schema", problems)
        nodes: dict[str, NodeKind] = {}
        for name, spec in _table(top.get("nodes", {}), "nodes", problems).items():
            spec = _fields(spec, {"max", "frozen"}, f"node kind {name!r}", problems)
            nodes[name] = NodeKind(
                max=_limit(spec.get("max"), f"node kind {name!r}: max", problems),
                frozen=_names(spec.get("frozen", ()), f"node kind {name!r}: frozen", problems),
            )
        edges: dict[str, EdgeKind] = {}
        allowed = {"from", "to", "max_in", "max_out", "unique", "acyclic", "max_depth", "frozen"}
        for name, spec in _table(top.get("edges", {}), "edges", problems).items():
            spec = _fields(spec, allowed, f"edge kind {name!r}", problems)
            edges[name] = EdgeKind(
                source=_names(spec.get("from", ()), f"edge kind {name!r}: from", problems),
                target=_names(spec.get("to", ()), f"edge kind {name!r}: to", problems),
                max_in=_limit(spec.get("max_in"), f"edge kind {name!r}: max_in", problems),
                max_out=_limit(spec.get("max_out"), f"edge kind {name!r}: max_out", problems),
                unique=_flag(spec.get("unique", False), f"edge kind {name!r}: unique", problems),
                acyclic=_flag(spec.get("acyclic", False), f"edge kind {name!r}: acyclic", problems),
                max_depth=_limit(spec.get("max_depth"), f"edge kind {name!r}: max_depth", problems),
                frozen=_flag(spec.get("frozen", False), f"edge kind {name!r}: frozen", problems),
            )
        gates = _names(top.get("gates", ()), "gates", problems)
        if problems:
            raise SchemaError("; ".join(problems))
        return cls(nodes=nodes, edges=edges, gates=gates)

    def to_json(self) -> dict[str, Any]:
        """The JSON form, defaults left out, so equal schemas have equal text."""
        nodes = {}
        for name, kind in sorted(self.nodes.items()):
            spec: dict[str, Any] = {}
            if kind.max is not None:
                spec["max"] = kind.max
            if kind.frozen:
                spec["frozen"] = sorted(kind.frozen)
            nodes[name] = spec
        edges = {}
        for name, edge in sorted(self.edges.items()):
            spec = {"from": sorted(edge.source), "to": sorted(edge.target)}
            for field in ("max_in", "max_out", "max_depth"):
                if getattr(edge, field) is not None:
                    spec[field] = getattr(edge, field)
            for field in ("unique", "acyclic", "frozen"):
                if getattr(edge, field):
                    spec[field] = True
            edges[name] = spec
        data: dict[str, Any] = {"nodes": nodes, "edges": edges}
        if self.gates:
            data["gates"] = sorted(self.gates)
        return data


# --- what the store hands out -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Graph:
    """One graph: its kind (whose graph it is), its schema, and who holds its lease until when."""

    id: str
    kind: str
    schema: Schema
    lease_token: str | None
    lease_expires: int | None
    created_at: int


@dataclass(frozen=True, slots=True)
class Node:
    """A node as one version of the graph has it. ``ordinal`` is its place in creation order;
    ``since`` is the application that made this version of it."""

    id: str
    kind: str
    attrs: FrozenDict
    ordinal: int
    since: int


@dataclass(frozen=True, slots=True)
class Edge:
    """An edge as one version of the graph has it, from ``source`` to ``target``."""

    id: str
    kind: str
    source: str
    target: str
    attrs: FrozenDict
    ordinal: int
    since: int


@dataclass(frozen=True, slots=True)
class Entry:
    """One journal entry: a change proposed for a graph, or a decision kept on its record.

    ``scope`` is ``""`` for the whole graph or the id of the node it concerns; ``key`` names what it
    is about (``None`` for an entry nothing refers to again) and ``version`` counts the proposals of
    that key. ``seq`` is its place in the order of applications, set when it is applied.
    """

    id: int
    graph: str
    scope: str
    key: str | None
    version: int
    op: str
    payload: Any
    gated: bool
    status: str
    proposer: Any
    idem: str | None
    approver: Any
    receipt: Any
    seq: int | None
    created_at: int
    decided_at: int | None
    applied_at: int | None


@dataclass(frozen=True, slots=True)
class Lease:
    """The outcome of asking for a graph's lease: whether it was granted, who holds it now, and who
    held it before (``None`` when nobody did)."""

    acquired: bool
    holder: str | None
    previous: str | None

"""The only way a graph changes: proposals, approvals, applications.

An entry is proposed by anyone its graph's user lets propose, approved when it must wait for that,
and applied by the lease holder, who records a receipt. Its status moves only along ``_MOVES``;
any other move is refused. Applying runs a handler on a :class:`Change`, checks the result against
the schema as a whole, and writes it as the graph's next version -- or, on any failure, nothing.

Handlers are passed in by whoever applies. Two ops need none: ``changes``, a list of plain edits
(the form a caller without code of its own uses), and ``schema``, which replaces the schema once
the whole graph keeps the new one.
"""

from __future__ import annotations

import secrets
import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from misaka.core.platform.tasks import write_txn

from . import constraints, lease, store
from .model import (
    ConstraintError,
    Edge,
    Entry,
    GraphError,
    JournalError,
    Node,
    Schema,
    dumps,
    freeze,
    loads,
)
from .snapshot import Snapshot

PROPOSED, APPROVED, APPLIED, VOID = "proposed", "approved", "applied", "void"

# status, action -> status. An approved entry may be approved again (a fresh go-ahead for the same
# proposal); an applied or void one moves no further.
_MOVES = {
    (PROPOSED, "approve"): APPROVED,
    (APPROVED, "approve"): APPROVED,
    (PROPOSED, "apply"): APPLIED,
    (APPROVED, "apply"): APPLIED,
    (PROPOSED, "void"): VOID,
    (APPROVED, "void"): VOID,
}

Handler = Callable[["Change", Entry], Any]


def _describe(entry: Entry) -> str:
    named = f" {entry.key!r} v{entry.version}" if entry.key is not None else ""
    return f"entry {entry.id} ({entry.op}{named})"


def _move(entry: Entry, action: str) -> str:
    status = _MOVES.get((entry.status, action))
    if status is None:
        raise JournalError(f"{_describe(entry)} is {entry.status}; it cannot {action}")
    return status


def _entry(row: sqlite3.Row) -> Entry:
    return Entry(
        id=row["id"], graph=row["graph"], scope=row["scope"], key=row["key"], version=row["version"], op=row["op"],
        payload=loads(row["payload_json"]), gated=bool(row["gated"]), status=row["status"],
        proposer=loads(row["proposer_json"]), idem=row["idem"], approver=loads(row["approver_json"]),
        receipt=loads(row["receipt_json"]), seq=row["seq"], created_at=row["created_at"],
        decided_at=row["decided_at"], applied_at=row["applied_at"],
    )


def entry(con: sqlite3.Connection, entry_id: int) -> Entry | None:
    row = con.execute("SELECT * FROM graph_journal WHERE id=?", (entry_id,)).fetchone()
    return _entry(row) if row else None


def _require(con: sqlite3.Connection, entry_id: int) -> Entry:
    found = entry(con, entry_id)
    if found is None:
        raise JournalError(f"journal entry {entry_id} does not exist")
    return found


def current(con: sqlite3.Connection, graph_id: str, key: str, *, scope: str = "") -> Entry | None:
    """The latest proposal of ``key`` that still stands: proposed, approved or applied."""
    row = con.execute(
        "SELECT * FROM graph_journal WHERE graph=? AND scope=? AND key=? AND status<>? ORDER BY version DESC LIMIT 1",
        (graph_id, scope, key, VOID)).fetchone()
    return _entry(row) if row else None


def entries(con: sqlite3.Connection, graph_id: str, *, scope: str | None = None, key: str | None = None,
            key_prefix: str | None = None, op: str | None = None,
            status: str | Iterable[str] | None = None) -> tuple[Entry, ...]:
    """The graph's entries in the order they were proposed, every version kept, narrowed by what is named."""
    clauses, values = ["graph=?"], [graph_id]
    for column, value in (("scope", scope), ("key", key), ("op", op)):
        if value is not None:
            clauses.append(f"{column}=?")
            values.append(value)
    if key_prefix is not None:
        clauses.append("substr(key,1,?)=?")
        values += [len(key_prefix), key_prefix]
    if status is not None:
        wanted = [status] if isinstance(status, str) else list(status)
        clauses.append(f"status IN ({','.join('?' * len(wanted))})")
        values += wanted
    rows = con.execute(f"SELECT * FROM graph_journal WHERE {' AND '.join(clauses)} ORDER BY id", values)
    return tuple(_entry(row) for row in rows)


def propose(con: sqlite3.Connection, graph_id: str, op: str, payload: Any = None, *, key: str | None = None,
            scope: str = "", proposer: Any = None, idem: str | None = None, gated: bool = False,
            supersede: bool = False) -> Entry:
    """Record a proposal. It waits for approval when ``gated`` or when the schema gates ``op``.

    Proposing again is safe: the same ``idem`` returns the entry it made, and the same payload for a
    ``key`` that stands returns that entry. A different payload for it is refused unless
    ``supersede``, which voids the standing proposal -- and any approval of it -- and records the new
    one as the key's next version. An applied key cannot be superseded: the graph has moved on.
    """
    if not isinstance(op, str) or not op:
        raise JournalError("an op is a non-empty name")
    if key is not None and (not isinstance(key, str) or not key):
        raise JournalError("a key is a non-empty name, or None")
    if not isinstance(scope, str):
        raise JournalError("a scope is a node id, or '' for the whole graph")
    body, who = freeze(payload), freeze(proposer)
    with write_txn(con):
        graph = store.require(con, graph_id)
        if idem is not None:
            row = con.execute("SELECT * FROM graph_journal WHERE graph=? AND idem=?", (graph_id, idem)).fetchone()
            if row is not None:
                return _entry(row)
        version = 1
        if key is not None:
            standing = current(con, graph_id, key, scope=scope)
            if standing is not None:
                same = standing.op == op and dumps(standing.payload) == dumps(body)
                if same and not supersede:
                    return standing
                if standing.status == APPLIED:
                    raise JournalError(f"{_describe(standing)} is already applied; the graph has moved on, "
                                       "so a change needs a new key")
                if not supersede:
                    raise JournalError(f"{_describe(standing)} was proposed with a different payload; "
                                       "supersede it to replace it")
                _set(con, standing, _move(standing, "void"))
            version = 1 + int(con.execute(
                "SELECT COALESCE(MAX(version),0) FROM graph_journal WHERE graph=? AND scope=? AND key=?",
                (graph_id, scope, key)).fetchone()[0])
        cursor = con.execute(
            "INSERT INTO graph_journal (graph,scope,key,version,op,payload_json,gated,status,proposer_json,idem,"
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (graph_id, scope, key, version, op, dumps(body), int(bool(gated) or op in graph.schema.gates), PROPOSED,
             dumps(who), idem, int(time.time())))
        return _require(con, cursor.lastrowid)


def _set(con: sqlite3.Connection, found: Entry, status: str, **columns: Any) -> None:
    sets = ", ".join(["status=?", "decided_at=?", *(f"{column}=?" for column in columns)])
    con.execute(f"UPDATE graph_journal SET {sets} WHERE id=?",
                (status, int(time.time()), *columns.values(), found.id))


def approve(con: sqlite3.Connection, entry_id: int, *, approver: Any = None) -> Entry:
    """Record the go-ahead for this very version of a proposal. An applied entry is returned as it is."""
    with write_txn(con):
        found = _require(con, entry_id)
        if found.status == APPLIED:
            return found
        _set(con, found, _move(found, "approve"), approver_json=dumps(freeze(approver)))
        return _require(con, entry_id)


def withdraw(con: sqlite3.Connection, entry_id: int) -> Entry:
    """Void a proposal that has not been applied; a void one is returned as it is."""
    with write_txn(con):
        found = _require(con, entry_id)
        if found.status == VOID:
            return found
        _set(con, found, _move(found, "void"))
        return _require(con, entry_id)


def apply(con: sqlite3.Connection, entry_id: int, *, token: str,
          handlers: Mapping[str, Handler] | None = None) -> Any:
    """Apply an entry as the lease holder ``token``, returning its receipt. All or nothing: a handler
    that raises, or a result that breaks the schema, leaves the graph and the entry as they were.
    Applying an applied entry again returns its receipt."""
    with write_txn(con), store.savepoint(con, "graph_apply"):
        found = _require(con, entry_id)
        if found.status == APPLIED:
            return found.receipt
        status = _move(found, "apply")
        if found.gated and found.status != APPROVED:
            raise JournalError(f"{_describe(found)} waits for approval")
        lease.fence(con, found.graph, token)
        handler = _BUILTINS.get(found.op) or (handlers or {}).get(found.op)
        if handler is None:
            raise JournalError(f"nothing applies {found.op!r}; pass its handler")
        graph = store.require(con, found.graph)
        seq = store.version(con, found.graph) + 1
        base = store.snapshot(con, found.graph)
        change = Change(con, graph.id, graph.schema, base, seq)
        receipt = freeze(handler(change, found))
        after = change.view
        violations = constraints.check(change.schema, base, after)
        if violations:
            raise ConstraintError(violations)
        store.write(con, graph.id, seq, base, after, change.touched)
        if dumps(change.schema.to_json()) != dumps(graph.schema.to_json()):
            store.set_schema(con, graph.id, change.schema)
        con.execute("UPDATE graph_journal SET status=?, seq=?, receipt_json=?, applied_at=? WHERE id=?",
                    (status, seq, dumps(receipt), int(time.time()), found.id))
        return receipt


def commit(con: sqlite3.Connection, graph_id: str, op: str, payload: Any = None, *, token: str,
           handlers: Mapping[str, Handler] | None = None, key: str | None = None, scope: str = "",
           proposer: Any = None, idem: str | None = None) -> Any:
    """Propose and apply in one step, as the lease holder: how a holder records its own moves. An op
    the schema gates cannot be committed; it has to wait for its approval."""
    with write_txn(con), store.savepoint(con, "graph_commit"):
        lease.fence(con, graph_id, token)
        found = propose(con, graph_id, op, payload, key=key, scope=scope, proposer=proposer, idem=idem)
        if found.status == APPLIED:
            return found.receipt
        if found.gated and found.status != APPROVED:
            raise JournalError(f"{_describe(found)} waits for approval; propose it instead")
        return apply(con, found.id, token=token, handlers=handlers)


class Change:
    """The graph as one application rewrites it. Edits take effect in :attr:`view` at once; the schema
    is checked once the handler returns, so a change may pass through states the schema forbids on
    its way to one it allows."""

    def __init__(self, con: sqlite3.Connection, graph_id: str, schema: Schema, base: Snapshot, seq: int) -> None:
        self._con, self._graph = con, graph_id
        self.schema = schema
        self.base = base
        self._seq = seq
        self._nodes: dict[str, Node | None] = {}
        self._edges: dict[str, Edge | None] = {}
        self._view: Snapshot | None = base
        self._next_node, self._next_edge = store.next_ordinals(con, graph_id)

    @property
    def view(self) -> Snapshot:
        """The graph with this change's edits so far."""
        if self._view is None:
            nodes = {node.id: node for node in self.base.nodes()} | self._nodes
            edges = {edge.id: edge for edge in self.base.edges()} | self._edges
            self._view = Snapshot.of(self._graph, self._seq, [node for node in nodes.values() if node is not None],
                                     [edge for edge in edges.values() if edge is not None])
        return self._view

    @property
    def touched(self) -> set[str]:
        return set(self._nodes) | set(self._edges)

    def _new_id(self, prefix: str, wanted: str | None) -> str:
        if wanted is not None:
            if not isinstance(wanted, str) or not wanted:
                raise GraphError("an element id is a non-empty name")
            if wanted in self._nodes or wanted in self._edges or store.taken(self._con, self._graph, wanted):
                raise GraphError(f"{wanted} already names a node or an edge of this graph")
            return wanted
        while True:
            made = f"{prefix}_{secrets.token_hex(5)}"
            if made not in self._nodes and made not in self._edges and not store.taken(self._con, self._graph, made):
                return made

    def add_node(self, kind: str, attrs: Mapping[str, Any] | None = None, *, id: str | None = None) -> str:
        """A new node of ``kind``; returns its id."""
        node = Node(id=self._new_id("n", id), kind=_name(kind), attrs=_attrs(attrs), ordinal=self._next_node,
                    since=self._seq)
        self._next_node += 1
        self._nodes[node.id] = node
        self._view = None
        return node.id

    def add_edge(self, kind: str, source: str, target: str, attrs: Mapping[str, Any] | None = None, *,
                 id: str | None = None) -> str:
        """A new edge of ``kind`` from ``source`` to ``target``; returns its id. Its ends are checked with
        the rest of the schema, so the nodes may come later in the same change."""
        edge = Edge(id=self._new_id("e", id), kind=_name(kind), source=_name(source), target=_name(target),
                    attrs=_attrs(attrs), ordinal=self._next_edge, since=self._seq)
        self._next_edge += 1
        self._edges[edge.id] = edge
        self._view = None
        return edge.id

    def update(self, element_id: str, attrs: Mapping[str, Any], *, replace: bool = False) -> None:
        """Set attributes of a node or an edge: merged into what it has, or in place of it with ``replace``."""
        found = self.view.node(element_id) or self.view.edge(element_id)
        if found is None:
            raise GraphError(f"{element_id} is not a node or an edge of this graph")
        merged = _attrs(attrs) if replace else _attrs({**found.attrs, **_attrs(attrs)})
        if merged == found.attrs:
            return
        if isinstance(found, Node):
            self._nodes[found.id] = Node(id=found.id, kind=found.kind, attrs=merged, ordinal=found.ordinal,
                                         since=self._seq)
        else:
            self._edges[found.id] = Edge(id=found.id, kind=found.kind, source=found.source, target=found.target,
                                         attrs=merged, ordinal=found.ordinal, since=self._seq)
        self._view = None

    def retire(self, element_id: str) -> None:
        """Take a node or an edge out of the graph; a node takes its edges with it."""
        view = self.view
        if view.node(element_id) is not None:
            for edge in (*view.out_edges(element_id), *view.in_edges(element_id)):
                self._edges[edge.id] = None
            self._nodes[element_id] = None
        elif view.edge(element_id) is not None:
            self._edges[element_id] = None
        else:
            raise GraphError(f"{element_id} is not a node or an edge of this graph")
        self._view = None


def _name(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise GraphError(f"expected a non-empty name, not {value!r}")
    return value


def _attrs(attrs: Mapping[str, Any] | None) -> Any:
    if attrs is None:
        return freeze({})
    if not isinstance(attrs, Mapping):
        raise GraphError("attributes are a JSON object")
    return freeze(attrs)


def _changes(change: Change, entry: Entry) -> dict[str, Any]:
    """The built-in ``changes`` op: a list of ``{"op": "add_node" | "add_edge" | "update" | "retire", ...}``
    applied in order. Its receipt lists the ids of what it added."""
    if not isinstance(entry.payload, list):
        raise JournalError("a changes payload is a list of edits")
    made = []
    for edit in entry.payload:
        what = edit.get("op") if isinstance(edit, Mapping) else None
        if what == "add_node":
            made.append(change.add_node(edit.get("kind"), edit.get("attrs"), id=edit.get("id")))
        elif what == "add_edge":
            made.append(change.add_edge(edit.get("kind"), edit.get("source"), edit.get("target"), edit.get("attrs"),
                                        id=edit.get("id")))
        elif what == "update":
            change.update(edit.get("id"), edit.get("attrs") or {}, replace=bool(edit.get("replace")))
        elif what == "retire":
            change.retire(edit.get("id"))
        else:
            raise JournalError(f"unknown edit {what!r}; edits are add_node, add_edge, update and retire")
    return {"ids": made}


def _schema(change: Change, entry: Entry) -> None:
    """The built-in ``schema`` op: the payload, in JSON form, replaces the schema."""
    change.schema = Schema.from_json(entry.payload)


_BUILTINS: dict[str, Handler] = {"changes": _changes, "schema": _schema}

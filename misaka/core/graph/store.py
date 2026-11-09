"""Where graphs live: their tables, their versions, and reading a version back.

Rows of nodes and edges are never changed in place. Each carries ``since``, the application that
made it, and ``until``, the one that replaced or retired it (empty while it is current), so every
version of a graph stays readable: the persistent structure of the textbooks, kept in SQLite. A
graph's version is the number of its latest application; a snapshot read outside a transaction is
kept on the connection and reused until that number moves.
"""

from __future__ import annotations

import secrets
import sqlite3
import time
from contextlib import contextmanager
from typing import Any

from misaka.core.platform.tasks import require_schema, write_txn

from .model import Edge, Graph, GraphError, Node, Schema, dumps, loads
from .snapshot import Snapshot

GRAPH_SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS graphs (
  id              TEXT PRIMARY KEY,
  kind            TEXT NOT NULL,
  schema_json     TEXT NOT NULL,
  lease_token     TEXT,
  lease_expires   INTEGER,
  created_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS graph_journal (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  graph           TEXT NOT NULL,
  scope           TEXT NOT NULL DEFAULT '',
  key             TEXT,
  version         INTEGER NOT NULL,
  op              TEXT NOT NULL,
  payload_json    TEXT NOT NULL,
  gated           INTEGER NOT NULL,
  status          TEXT NOT NULL,
  proposer_json   TEXT NOT NULL,
  idem            TEXT,
  approver_json   TEXT,
  receipt_json    TEXT,
  seq             INTEGER,
  created_at      INTEGER NOT NULL,
  decided_at      INTEGER,
  applied_at      INTEGER,
  UNIQUE(graph, scope, key, version)
);
CREATE TABLE IF NOT EXISTS graph_nodes (
  graph           TEXT NOT NULL,
  id              TEXT NOT NULL,
  kind            TEXT NOT NULL,
  attrs_json      TEXT NOT NULL,
  ordinal         INTEGER NOT NULL,
  since           INTEGER NOT NULL,
  until           INTEGER,
  PRIMARY KEY(graph, id, since)
);
CREATE TABLE IF NOT EXISTS graph_edges (
  graph           TEXT NOT NULL,
  id              TEXT NOT NULL,
  kind            TEXT NOT NULL,
  source          TEXT NOT NULL,
  target          TEXT NOT NULL,
  attrs_json      TEXT NOT NULL,
  ordinal         INTEGER NOT NULL,
  since           INTEGER NOT NULL,
  until           INTEGER,
  PRIMARY KEY(graph, id, since)
);
CREATE UNIQUE INDEX IF NOT EXISTS graph_journal_idem ON graph_journal(graph, idem) WHERE idem IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS graph_journal_seq ON graph_journal(graph, seq) WHERE seq IS NOT NULL;
CREATE INDEX IF NOT EXISTS graph_journal_key ON graph_journal(graph, scope, key);
CREATE UNIQUE INDEX IF NOT EXISTS graph_nodes_live ON graph_nodes(graph, id) WHERE until IS NULL;
CREATE INDEX IF NOT EXISTS graph_nodes_kind ON graph_nodes(graph, kind) WHERE until IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS graph_edges_live ON graph_edges(graph, id) WHERE until IS NULL;
CREATE INDEX IF NOT EXISTS graph_edges_out ON graph_edges(graph, source, kind) WHERE until IS NULL;
CREATE INDEX IF NOT EXISTS graph_edges_in ON graph_edges(graph, target, kind) WHERE until IS NULL;
"""


def _execute(con: sqlite3.Connection, script: str) -> None:
    """Run a static script statement by statement: ``executescript`` would commit the caller's transaction."""
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            con.execute(statement)
            statement = ""


@contextmanager
def savepoint(con: sqlite3.Connection, name: str):
    """Undo everything inside on failure, without ending a transaction the caller holds."""
    con.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        con.execute(f"ROLLBACK TO {name}")
        con.execute(f"RELEASE {name}")
        raise
    else:
        con.execute(f"RELEASE {name}")


def _ready(con: sqlite3.Connection) -> bool:
    return bool(con.execute(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='graphs') "
        "AND EXISTS(SELECT 1 FROM schema_migrations WHERE component='graph' AND version=?)",
        (GRAPH_SCHEMA_VERSION,),
    ).fetchone()[0])


def init(con: sqlite3.Connection) -> None:
    """Make the graph tables on a board connection, once; a later call only checks they are there."""
    if _ready(con):
        return
    with write_txn(con), savepoint(con, "graph_init"):
        if _ready(con):
            return
        populated = bool(con.execute(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='graphs')").fetchone()[0])
        if require_schema(con, "graph", GRAPH_SCHEMA_VERSION, populated=populated):
            _execute(con, DDL)


def _graph(row: sqlite3.Row) -> Graph:
    return Graph(id=row["id"], kind=row["kind"], schema=Schema.from_json(loads(row["schema_json"])),
                 lease_token=row["lease_token"], lease_expires=row["lease_expires"], created_at=row["created_at"])


def create(con: sqlite3.Connection, kind: str, schema: Schema | dict[str, Any], *, id: str | None = None) -> Graph:
    """A new, empty graph of ``kind`` that keeps ``schema``."""
    if not isinstance(kind, str) or not kind:
        raise GraphError("a graph's kind is a non-empty name")
    schema = Schema.from_json(schema)
    with write_txn(con):
        if id is None:
            id = "g_" + secrets.token_hex(6)
            while get(con, id) is not None:
                id = "g_" + secrets.token_hex(6)
        elif not isinstance(id, str) or not id:
            raise GraphError("a graph's id is a non-empty name")
        elif get(con, id) is not None:
            raise GraphError(f"graph {id} already exists")
        con.execute("INSERT INTO graphs (id,kind,schema_json,created_at) VALUES (?,?,?,?)",
                    (id, kind, dumps(schema.to_json()), int(time.time())))
    return get(con, id)


def get(con: sqlite3.Connection, graph_id: str) -> Graph | None:
    row = con.execute("SELECT * FROM graphs WHERE id=?", (graph_id,)).fetchone()
    return _graph(row) if row else None


def require(con: sqlite3.Connection, graph_id: str) -> Graph:
    graph = get(con, graph_id)
    if graph is None:
        raise GraphError(f"graph {graph_id} does not exist")
    return graph


def set_schema(con: sqlite3.Connection, graph_id: str, schema: Schema) -> None:
    con.execute("UPDATE graphs SET schema_json=? WHERE id=?", (dumps(schema.to_json()), graph_id))


def version(con: sqlite3.Connection, graph_id: str) -> int:
    """The number of the graph's latest application; 0 before the first."""
    return int(con.execute("SELECT COALESCE(MAX(seq),0) FROM graph_journal WHERE graph=? AND seq IS NOT NULL",
                           (graph_id,)).fetchone()[0])


def _node(row: sqlite3.Row) -> Node:
    return Node(id=row["id"], kind=row["kind"], attrs=loads(row["attrs_json"]), ordinal=row["ordinal"],
                since=row["since"])


def _edge(row: sqlite3.Row) -> Edge:
    return Edge(id=row["id"], kind=row["kind"], source=row["source"], target=row["target"],
                attrs=loads(row["attrs_json"]), ordinal=row["ordinal"], since=row["since"])


def _cache(con: sqlite3.Connection) -> dict[str, Snapshot] | None:
    """The connection's snapshots of committed versions, or None where they cannot be kept: inside a
    transaction (a version read there may be rolled back, and its number reused), or on a connection
    that takes no attributes."""
    if con.in_transaction:
        return None
    cache = getattr(con, "_graph_snapshots", None)
    if cache is None:
        try:
            cache = con._graph_snapshots = {}
        except AttributeError:
            return None
    return cache


def snapshot(con: sqlite3.Connection, graph_id: str, *, at: int | None = None) -> Snapshot:
    """The graph as of application ``at``, or as it is now."""
    require(con, graph_id)
    latest = version(con, graph_id)
    at = latest if at is None else min(int(at), latest)
    cache = _cache(con) if at == latest else None
    if cache is not None and (kept := cache.get(graph_id)) is not None and kept.version == at:
        return kept
    window = "until IS NULL" if at == latest else "since<=? AND (until IS NULL OR until>?)"
    bounds = () if at == latest else (at, at)
    nodes = [_node(row) for row in con.execute(f"SELECT * FROM graph_nodes WHERE graph=? AND {window}",
                                               (graph_id, *bounds))]
    edges = [_edge(row) for row in con.execute(f"SELECT * FROM graph_edges WHERE graph=? AND {window}",
                                               (graph_id, *bounds))]
    found = Snapshot.of(graph_id, at, nodes, edges)
    if cache is not None:
        cache[graph_id] = found
    return found


def locate(con: sqlite3.Connection, element_id: str) -> str | None:
    """The graph whose current version holds a node or an edge called ``element_id``; None when none
    does. Ids are a graph's own, so a name two graphs share is refused rather than guessed."""
    found = {row[0] for row in con.execute(
        "SELECT graph FROM graph_nodes WHERE id=? AND until IS NULL "
        "UNION SELECT graph FROM graph_edges WHERE id=? AND until IS NULL", (element_id, element_id))}
    if len(found) > 1:
        raise GraphError(f"{element_id} names an element of several graphs: {', '.join(sorted(found))}")
    return next(iter(found), None)


def taken(con: sqlite3.Connection, graph_id: str, element_id: str) -> bool:
    """Whether any version of the graph ever had a node or an edge called ``element_id``."""
    return bool(con.execute(
        "SELECT EXISTS(SELECT 1 FROM graph_nodes WHERE graph=? AND id=?) "
        "OR EXISTS(SELECT 1 FROM graph_edges WHERE graph=? AND id=?)",
        (graph_id, element_id, graph_id, element_id),
    ).fetchone()[0])


def next_ordinals(con: sqlite3.Connection, graph_id: str) -> tuple[int, int]:
    """The creation-order numbers the graph's next node and next edge take."""
    row = con.execute(
        "SELECT (SELECT COALESCE(MAX(ordinal),0) FROM graph_nodes WHERE graph=?),"
        "(SELECT COALESCE(MAX(ordinal),0) FROM graph_edges WHERE graph=?)", (graph_id, graph_id)).fetchone()
    return int(row[0]) + 1, int(row[1]) + 1


def _content(element: Node | Edge | None) -> tuple[Any, ...] | None:
    """What a version of an element says, without where it sits in the history."""
    if element is None:
        return None
    if isinstance(element, Node):
        return (element.kind, element.attrs)
    return (element.kind, element.source, element.target, element.attrs)


def write(con: sqlite3.Connection, graph_id: str, seq: int, before: Snapshot, after: Snapshot,
          touched: set[str]) -> None:
    """Record, as application ``seq``, how the elements in ``touched`` differ from ``before`` to ``after``."""
    for element_id in sorted(touched):
        for table, old, new in (("graph_nodes", before.node(element_id), after.node(element_id)),
                                ("graph_edges", before.edge(element_id), after.edge(element_id))):
            if _content(old) == _content(new):
                continue
            if old is not None:
                con.execute(f"UPDATE {table} SET until=? WHERE graph=? AND id=? AND until IS NULL",
                            (seq, graph_id, element_id))
            if new is None:
                continue
            if isinstance(new, Node):
                con.execute("INSERT INTO graph_nodes (graph,id,kind,attrs_json,ordinal,since) VALUES (?,?,?,?,?,?)",
                            (graph_id, new.id, new.kind, dumps(new.attrs), new.ordinal, seq))
            else:
                con.execute(
                    "INSERT INTO graph_edges (graph,id,kind,source,target,attrs_json,ordinal,since) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (graph_id, new.id, new.kind, new.source, new.target, dumps(new.attrs), new.ordinal, seq))

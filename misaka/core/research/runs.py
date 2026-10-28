"""Persistent research-run state and file artifacts.

A run is a directed acyclic graph of nodes, kept as the run's graph (``misaka.core.graph``, its id
the run's): ``inquiry`` nodes joined by ``fork`` edges, with the decisions, options, review issues
and relations around them, and the journal of Last Order's commands.
The root (depth 0) is the question as asked; every other node pursues an option of a decision
some node made -- a possibility (another hypothesis, method, reading, perspective, decomposition,
or a critique of the question itself), never a correction: corrections stay in their node as
revision rounds. A node reached by options of several nodes, or opened to carry several finished
nodes forward together, has several parents. Research prose lives directly in the project from
its first write: ``<workspace>/nodes/<node>/`` per node (its cards under ``cards/``) and
``<workspace>/final/`` for run-level products. Session transcripts share the existing
``~/.misaka/sessions`` store. The graph is the only authority on the run's structure; the graph
files in the project are views written from it and never read back. How each node is being run
(its status, session and runner) is execution, kept here in ``research_execution``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import socket
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from misaka.config import home
from misaka.core import graph
from misaka.core.platform import tasks as task_store
from misaka.utils import atomic

# Cards that review rather than research: a critique records issues, a divergence review records
# alternatives. Neither feeds the evidence ledger.
CRITIQUE_KINDS = frozenset({"red_team", "final_review"})
REVIEW_KINDS = CRITIQUE_KINDS | {"divergence"}

# The run's graph (``misaka.core.graph``) holds its structure: the nodes and what forked them, the
# decisions, options, review issues and relations, and the journal of Last Order's commands. These
# tables hold the rest: the run itself, how each node is being executed, the cards, and the files and
# findings the run registered.
SCHEMA = """
CREATE TABLE IF NOT EXISTS research_runs (
  id              TEXT PRIMARY KEY,
  workspace       TEXT NOT NULL,
  question        TEXT NOT NULL,
  phase           TEXT NOT NULL,
  status          TEXT NOT NULL,
  wave            INTEGER NOT NULL DEFAULT 0,
  limits_json     TEXT NOT NULL,
  token_start     INTEGER NOT NULL DEFAULT 0,
  root_session    TEXT,
  origin_session  TEXT,
  stop_requested  INTEGER NOT NULL DEFAULT 0,
  final_artifact  TEXT,
  last_error      TEXT,
  created_at      INTEGER NOT NULL,
  updated_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS research_execution (
  id              TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'queued',
  session_file    TEXT,
  fork_entry      TEXT,
  context_artifact TEXT,
  runner_pid      INTEGER,
  runner_identity TEXT,
  runner_key      TEXT,
  last_error      TEXT,
  created_at      INTEGER NOT NULL,
  updated_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS research_cards (
  task_id         TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL,
  branch_id       TEXT NOT NULL,
  kind            TEXT NOT NULL,
  round           INTEGER NOT NULL DEFAULT 1,
  local_id        TEXT,
  depends_json    TEXT NOT NULL DEFAULT '[]',
  created_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS research_artifacts (
  id              TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL,
  branch_id       TEXT,
  task_id         TEXT,
  kind            TEXT NOT NULL,
  title           TEXT NOT NULL,
  path            TEXT NOT NULL,
  sha256          TEXT NOT NULL,
  metadata_json   TEXT NOT NULL DEFAULT '{}',
  created_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS research_findings (
  id              TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL,
  branch_id       TEXT,
  task_id         TEXT NOT NULL,
  text            TEXT NOT NULL,
  claim_type      TEXT NOT NULL,
  created_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS research_claims (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  finding_id      TEXT NOT NULL,
  artifact_id     TEXT,
  source_file     TEXT NOT NULL,
  quote           TEXT NOT NULL,
  evidence_sha    TEXT NOT NULL,
  created_at      INTEGER NOT NULL,
  UNIQUE(finding_id,artifact_id,quote)
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS research_execution_run ON research_execution(run_id);
CREATE INDEX IF NOT EXISTS research_cards_run ON research_cards(run_id,branch_id,kind);
CREATE UNIQUE INDEX IF NOT EXISTS research_cards_local ON research_cards(run_id,branch_id,local_id) WHERE local_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS research_artifacts_run ON research_artifacts(run_id,branch_id,kind);
CREATE UNIQUE INDEX IF NOT EXISTS research_artifacts_generated ON research_artifacts(run_id,path) WHERE task_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS research_artifacts_task ON research_artifacts(run_id,task_id,path) WHERE task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS research_findings_run ON research_findings(run_id,branch_id,task_id);
CREATE INDEX IF NOT EXISTS research_claims_finding ON research_claims(finding_id);
"""

ACTIVE = ("active", "waiting_input", "stopping")
# closed = the node's own work is done. Its options wait for the level's reconciliation and its
# children run on their own: neither is the node's status.
NODE_TERMINAL = ("closed", "failed", "parked")
DEFAULT_LIMITS = {"max_depth": 3, "parallel": 4, "sister_parallel": 4, "max_followups": 2,
                  "max_revisions": 2, "max_nodes": 30}
MAX_NODES_CEILING = 500
# A run's own compaction threshold, as a share of the context window: below a tenth a session would
# compact on nearly every turn, above 0.95 there is no room left to compact into.
MIN_CONTEXT_THRESHOLD, MAX_CONTEXT_THRESHOLD = 0.1, 0.95
# v19: the structure moved into the run's graph (v18 is migrated, _migrate_from_18). v20: an option left unopened
# points at what answers it, and an option can answer a path declined before (_migrate_from_19).
RESEARCH_SCHEMA_VERSION = 20
DRIVER_TTL_SECONDS = 300
RESEARCH_TABLES = (
    "research_claims", "research_findings", "research_artifacts", "research_cards", "research_execution",
    "research_runs",
)
# What Last Order may answer a red-team issue with; one she took to her decision ends as ``branch``
# (``option_id`` = the option that takes it up) or, not opened, as ``covered`` (``covered_by``, or the
# ``option_id`` of an option waiting to be opened) or ``decline``.
CRITIQUE_DISPOSITIONS = ("revise", "correct", "rebut", "concede", "covered", "park", "branch")
# What becomes of a divergence proposal at the node's decision. A gap is not a proposal: the
# divergence review's gaps are filled or answered in its own rounds, before the node forks.
PROPOSAL_DISPOSITIONS = ("branch", "covered", "decline")
RELATION_KINDS = ("converge", "diverge", "resonates", "appropriates", "displaces")


def _execute_script(con, source):
    """Execute a static SQL script without ``executescript``'s implicit commit."""
    statement = ""
    for line in source.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            con.execute(statement)
            statement = ""
    if statement.strip():
        raise RuntimeError("Incomplete research schema statement.")


@contextmanager
def _savepoint(con):
    """Make schema-creation rollback independent of a caller's surrounding transaction."""
    name = "research_schema_init"
    con.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        con.execute(f"ROLLBACK TO {name}")
        con.execute(f"RELEASE {name}")
        raise
    else:
        con.execute(f"RELEASE {name}")


def _schema_state(con):
    """One read snapshot of the table and the current marker."""
    row = con.execute(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_runs'),"
        "EXISTS(SELECT 1 FROM schema_migrations WHERE component='research' AND version=?)",
        (RESEARCH_SCHEMA_VERSION,),
    ).fetchone()
    return bool(row[0]), bool(row[1])


def _matches_current_schema(con):
    """True when every active table has the current columns and UNIQUE constraints."""
    expected = sqlite3.connect(":memory:")
    try:
        _execute_script(expected, SCHEMA)

        def signature(db, table):
            columns = tuple(
                (row[1], row[2].upper(), int(row[3]), row[4], int(row[5]))
                for row in db.execute(f'PRAGMA table_info("{table}")')
            )
            unique = sorted(
                tuple(item[2] for item in db.execute(f'PRAGMA index_info("{index[1]}")'))
                for index in db.execute(f'PRAGMA index_list("{table}")')
                if index[2] and index[3] == "u"
            )
            return columns, unique

        return all(signature(con, table) == signature(expected, table)
                   for table in RESEARCH_TABLES)
    finally:
        expected.close()


def _validate_current_schema(con):
    """A version marker is not proof of table shape. Recheck after any SQLite DDL change."""
    cookie = (RESEARCH_SCHEMA_VERSION, con.execute("PRAGMA schema_version").fetchone()[0])
    if getattr(con, "_research_schema_checked", None) == cookie:
        return
    if not _matches_current_schema(con):
        raise RuntimeError(f"Research schema v{RESEARCH_SCHEMA_VERSION} marker does not match its tables. "
                           "Reload the current MISAKA build before starting research; no run was created.")
    if hasattr(con, "__dict__"):
        con._research_schema_checked = cookie


def init(con):
    graph.init(con)
    has_runs, current = _schema_state(con)
    if has_runs and current:
        _validate_current_schema(con)
        return                               # the normal read path takes no SQLite write lock
    # DDL is transactional in SQLite, but ``executescript`` commits implicitly. Keep the new
    # tables, indexes and the version marker under one explicit transaction.
    with task_store.write_txn(con), _savepoint(con):
        # Another process may have created the schema while this one waited for BEGIN IMMEDIATE.
        has_runs, current = _schema_state(con)
        if has_runs and current:
            _validate_current_schema(con)
            return
        recorded = con.execute("SELECT MAX(version) FROM schema_migrations WHERE component='research'").fetchone()[0]
        if recorded in (18, 19):
            # A v18 board's graphs are made with the current schema; a v19 board's are given it.
            if recorded == 18:
                _migrate_from_18(con)
            else:
                _migrate_from_19(con)
            con.execute("INSERT INTO schema_migrations(component,version,applied_at) VALUES('research',?,?)",
                        (RESEARCH_SCHEMA_VERSION, int(time.time())))
            return
        if task_store.require_schema(con, "research", RESEARCH_SCHEMA_VERSION, populated=has_runs):
            _execute_script(con, SCHEMA)
            _execute_script(con, INDEXES)


# --- the run's graph ---------------------------------------------------------------------

def graph_schema(chosen):
    """What a run's graph may hold, under the run's limits ``chosen``: its node count and depth are
    the graph's own bounds, so no writer can pass them."""
    return {
        "nodes": {"inquiry": {"max": chosen["max_nodes"], "frozen": ["depth"]},
                  "decision": {}, "option": {}, "issue": {}, "relation": {}},
        "edges": {
            # parent -> child: the node a decision of the parent opened, or a join of several
            "fork": {"from": ["inquiry"], "to": ["inquiry"], "acyclic": True, "frozen": True,
                     "max_depth": chosen["max_depth"]},
            "decides": {"from": ["inquiry"], "to": ["decision"], "max_in": 1},
            "offers": {"from": ["decision"], "to": ["option"], "max_in": 1},
            "opens": {"from": ["option"], "to": ["inquiry"], "max_out": 1},
            "raised_on": {"from": ["issue"], "to": ["inquiry"], "max_out": 1},
            "becomes": {"from": ["issue"], "to": ["option"], "max_out": 1},
            # What answers a possibility that was not opened as a node of its own: an issue's covering node,
            # or, for an option left unopened, the node that pursues it or the option opened for it. Each
            # possibility has one such pointer, set by whoever judged it; following them -- with
            # ``becomes`` and ``opens`` -- ends at a node or at a record left unopened (views.where).
            "covered_by": {"from": ["issue", "option"], "to": ["inquiry", "option"], "max_out": 1, "acyclic": True},
            # A cross-reference, not a pointer: an option that brings back a path declined or left unopened,
            # and answers the reason recorded for it. The path stays where it was.
            "answers": {"from": ["option"], "to": ["issue", "option"], "unique": True},
            "member": {"from": ["relation"], "to": ["inquiry"], "unique": True},
        },
    }


@contextmanager
def _holding(con, run_id):
    """The token a write to the run's graph goes under, inside one write transaction: the driver
    lease's holder, or -- while nobody drives the run (it is being made, or a command edits it between
    drivers) -- one taken for this write and given back. Research fences its own writers with
    ``check_owner``; this only satisfies the graph's lease."""
    with task_store.write_txn(con):
        holder = graph.get(con, run_id).lease_token
        if holder is not None:
            yield holder
            return
        transient = "research:" + secrets.token_hex(6)
        graph.acquire(con, run_id, transient, ttl=DRIVER_TTL_SECONDS)
        try:
            yield transient
        finally:
            graph.release(con, run_id, transient)


def _commit(con, run_id, op, payload, apply):
    """One change to the run's graph, journaled as ``op`` with ``payload``; ``apply(change)`` makes it."""
    with _holding(con, run_id) as token:
        return graph.commit(con, run_id, op, payload, token=token, handlers={op: lambda change, _entry: apply(change)})


def _migrate_from_18(con):
    """Carry a v18 board forward: each run's structure into its graph, its commands and
    reconciliations into the graph's journal, its driver lease onto the graph, and node execution and
    card links into their tables. Ids are kept, so a migrated run resumes where it was."""
    for index in ("research_tasks_run", "research_tasks_local"):
        con.execute(f"DROP INDEX IF EXISTS {index}")
    con.execute("ALTER TABLE research_run_tasks RENAME TO research_cards")
    _execute_script(con, SCHEMA)                 # research_execution; the other tables are there already
    now = int(time.time())
    for run in con.execute("SELECT * FROM research_runs ORDER BY created_at,rowid").fetchall():
        rid = run["id"]
        graph.create(con, "research", graph_schema(limits(run)), id=rid)
        token = "migration:" + rid
        graph.acquire(con, rid, token, ttl=DRIVER_TTL_SECONDS)
        rows = {
            "branches": con.execute("SELECT * FROM research_branches WHERE run_id=? ORDER BY depth,created_at,rowid",
                                    (rid,)).fetchall(),
            "edges": con.execute("SELECT * FROM research_edges WHERE run_id=? ORDER BY rowid", (rid,)).fetchall(),
            "decisions": con.execute("SELECT * FROM research_decisions WHERE run_id=? ORDER BY created_at,rowid",
                                     (rid,)).fetchall(),
            "options": con.execute("SELECT * FROM research_options WHERE run_id=? ORDER BY rowid", (rid,)).fetchall(),
            "issues": con.execute("SELECT * FROM research_issues WHERE run_id=? ORDER BY created_at,rowid",
                                  (rid,)).fetchall(),
            "relations": con.execute("SELECT * FROM research_relations WHERE run_id=? ORDER BY created_at,rowid",
                                     (rid,)).fetchall(),
        }

        def build(change, _entry, rows=rows):
            for row in rows["branches"]:
                change.add_node("inquiry", {"question": row["question"], "depth": row["depth"]}, id=row["id"])
            for row in rows["edges"]:
                change.add_edge("fork", row["parent_id"], row["child_id"])
            for row in rows["decisions"]:
                change.add_node("decision", {"round": row["round"], "origin": row["origin"], "question": row["question"],
                                             "stakes": row["stakes"], "created_at": row["created_at"]}, id=row["id"])
                change.add_edge("decides", row["node_id"], row["id"])
            for row in rows["options"]:
                change.add_node("option", {"label": row["label"], "premise": row["premise"], "reason": row["reason"],
                                           "created_at": row["created_at"]}, id=row["id"])
                change.add_edge("offers", row["decision_id"], row["id"])
                if row["node_id"]:
                    change.add_edge("opens", row["id"], row["node_id"])
            for row in rows["issues"]:
                change.add_node("issue", {key: row[key] for key in (
                    "task_id", "round", "origin", "kind", "question", "rationale", "priority", "disposition", "reason",
                    "created_at")}, id=row["id"])
                change.add_edge("raised_on", row["id"], row["branch_id"])
                if row["option_id"]:
                    change.add_edge("becomes", row["id"], row["option_id"])
                if row["covered_by"]:
                    change.add_edge("covered_by", row["id"], row["covered_by"])
            for row in rows["relations"]:
                change.add_node("relation", {"kind": row["kind"], "note": row["note"], "created_at": row["created_at"]},
                                id=row["id"])
                for member in con.execute("SELECT node_id,position FROM research_relation_nodes WHERE relation_id=? "
                                          "ORDER BY position", (row["id"],)):
                    change.add_edge("member", row["id"], member["node_id"], {"position": member["position"]})

        graph.commit(con, rid, "migrate", {"from_version": 18}, token=token, handlers={"migrate": build})
        for row in con.execute("SELECT * FROM research_actions WHERE run_id=? ORDER BY created_at,rowid", (rid,)):
            graph.propose(con, rid, "action", json.loads(row["payload_json"]), key=row["action_key"],
                          scope=row["branch_id"], proposer=_proposer(row["session_file"], row["tool_call_id"],
                                                                     row["created_at"], stored=True))
        for row in con.execute("SELECT * FROM research_reconciles WHERE run_id=? ORDER BY round", (rid,)):
            entry = graph.propose(con, rid, "reconcile", json.loads(row["payload_json"]), key=_reconcile_key(row["round"]),
                                  proposer=_proposer(row["session_file"], row["tool_call_id"], row["created_at"],
                                                     stored=True))
            if row["approved_call_id"] and row["approved_call_id"] == row["tool_call_id"]:
                graph.approve(con, entry.id, approver=_APPROVER)
            if row["applied_at"] is not None:
                receipt = json.loads(row["receipt_json"]) if row["receipt_json"] else None
                graph.apply(con, entry.id, token=token, handlers={"reconcile": lambda _change, _entry, r=receipt: r})
        graph.release(con, rid, token)
        if run["driver_lock"] and run["driver_expires"] and run["driver_expires"] > now:
            graph.acquire(con, rid, run["driver_lock"], ttl=run["driver_expires"] - now)
    con.execute(
        "INSERT INTO research_execution (id,run_id,status,session_file,fork_entry,context_artifact,runner_pid,"
        "runner_identity,runner_key,last_error,created_at,updated_at) SELECT id,run_id,status,session_file,fork_entry,"
        "context_artifact,runner_pid,runner_identity,runner_key,last_error,created_at,updated_at FROM research_branches")
    for column in ("driver_lock", "driver_expires"):
        con.execute(f"ALTER TABLE research_runs DROP COLUMN {column}")
    for table in ("research_actions", "research_reconciles", "research_issues", "research_relation_nodes",
                  "research_relations", "research_options", "research_decisions", "research_edges",
                  "research_branches"):
        con.execute(f"DROP TABLE {table}")
    _execute_script(con, INDEXES)


def _migrate_from_19(con):
    """Give every run's graph the v20 schema: the same kinds, with ``covered_by`` widened to options and
    ``answers`` added. Nothing in a graph changes; the schema op is journaled like any other."""
    for run in con.execute("SELECT * FROM research_runs ORDER BY created_at,rowid").fetchall():
        if graph.get(con, run["id"]) is None:
            continue
        with _holding(con, run["id"]) as token:
            graph.commit(con, run["id"], "schema", graph_schema(limits(run)), token=token)


def _lease_holder_is_dead(lock):
    """Whether a driver lock (``driver:<host>:<pid>:<nonce>``) names a process on this host
    that no longer exists. A reused pid reads as alive and leaves the TTL to decide, which
    is the safe side; a foreign host is never judged from here."""
    parts = str(lock or "").split(":")
    if len(parts) != 4 or parts[0] != "driver" or parts[1] != socket.gethostname():
        return False
    try:
        pid = int(parts[2])
    except ValueError:
        return False
    import psutil
    return pid > 0 and not psutil.pid_exists(pid)


def acquire_driver(con, run_id, lock, ttl_seconds=DRIVER_TTL_SECONDS):
    """Take the run's driver lease -- its graph's lease: one process advances a run at a time. False
    when another live lease holds it. A driver that died hard (a crashed pane, SIGKILL) neither renews
    nor releases; waiting its TTL out only held the run (2026-09-23: three refusals, cleared by hand),
    so a holder whose process is gone is taken over at once."""
    with task_store.write_txn(con):
        lease = graph.acquire(con, run_id, lock, ttl=int(ttl_seconds), stale=_lease_holder_is_dead)
        if lease.acquired and lease.previous != lock:
            # Invalidate delayed child spawns in the same transaction as takeover.
            # Keep PID/identity for the successor's existing orphan-reaping path.
            con.execute("UPDATE research_execution SET runner_key=NULL WHERE run_id=?", (run_id,))
    return lease.acquired


def driverless(run):
    """Whether no live driver holds ``run`` (as ``get`` reads it): nobody took its lease, the lease
    ran out, or its holder's process is gone -- the lease ``acquire_driver`` would take over now."""
    lock, expires = run["driver_lock"], run["driver_expires"]
    return not lock or expires is None or expires < int(time.time()) or _lease_holder_is_dead(lock)


def heartbeat_driver(con, run_id, lock, ttl_seconds=DRIVER_TTL_SECONDS):
    return graph.renew(con, run_id, lock, ttl=int(ttl_seconds))


def release_driver(con, run_id, lock):
    graph.release(con, run_id, lock)


def holds_driver(con, run_id, lock):
    """Whether ``lock`` (None: nobody) is the run's driver lease as it stands."""
    found = graph.get(con, run_id)
    return found is not None and found.lease_token == lock


# Cards past these statuses keep their dependency history as it is: the DAG must not be rewritten
# under a moving card, and a finished one (whose execution already finished) has
# nothing left to wait for. Read by ``workflow._submit_tasks``, which replays a plan's edges
# onto cards that may already have run.
SETTLED_TASK_STATUSES = frozenset({"running", "review", "done", "failed", "stopped", "archived"})


def node_dir(node_id):
    """The folder a node owns: ``nodes/<node id>``, the root included. Flat on purpose: a node
    with several parents has no single place in a nested tree. A node id is a name, as
    ``session_dir`` and ``_artifact_name`` already insist."""
    node_id = str(node_id or "")
    if not node_id or Path(node_id).name != node_id or node_id in {".", ".."} or "\\" in node_id:
        raise ValueError(f"A node id is a name, not a path: {node_id!r}")
    return os.path.join("nodes", node_id)


def _bounded_int(raw, key, low, high):
    value = raw.get(key, DEFAULT_LIMITS[key])
    message = f"{key} must be an integer between {low} and {high}"
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(message)  # noqa: TRY004 - startup callers surface ValueError
    try:
        value = int(value)
    except ValueError as error:
        raise ValueError(message) from error
    if not low <= value <= high:
        raise ValueError(message)
    return value


def normalize_limits(raw=None):
    raw = raw or {}
    value = raw.get("max_depth", DEFAULT_LIMITS["max_depth"])
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("max_depth must be an integer")  # noqa: TRY004
    try:
        depth = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("max_depth must be an integer") from error
    if not 0 <= depth <= 12:
        raise ValueError("max_depth must be between 0 and 12")
    concurrency = {}
    for key in ("parallel", "sister_parallel"):
        value = raw.get(key, DEFAULT_LIMITS[key])
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError(f"{key} must be a positive integer")  # noqa: TRY004 - startup callers surface ValueError
        try:
            value = int(value)
        except ValueError as error:
            raise ValueError(f"{key} must be a positive integer") from error
        if value < 1:
            raise ValueError(f"{key} must be a positive integer")
        concurrency[key] = value
    result = {
        "max_depth": depth, **concurrency,
        # How many more times a node may send its Sisters out after its first cards are back, over
        # its whole life (before its first conclusion and while revising one). Conversation with
        # the user is never counted.
        "max_followups": _bounded_int(raw, "max_followups", 0, 6),
        # How many times a node may revise its conclusion after a red-team review; each revision
        # is reviewed again. 0 = the first conclusion is final and every issue is answered otherwise.
        "max_revisions": _bounded_int(raw, "max_revisions", 0, 6),
        # How many nodes the whole graph may hold, the root included; checked when options open.
        "max_nodes": _bounded_int(raw, "max_nodes", 1, MAX_NODES_CEILING),
    }
    if "plan_approval" in raw:
        if not isinstance(raw["plan_approval"], bool):
            raise ValueError("plan_approval must be a boolean")
        result["plan_approval"] = raw["plan_approval"]
    # The run's own session settings; absent, its sessions follow the global ones.
    if raw.get("context_threshold") is not None:
        threshold = raw["context_threshold"]
        if (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
                or not MIN_CONTEXT_THRESHOLD <= threshold <= MAX_CONTEXT_THRESHOLD):
            raise ValueError(f"context_threshold must be a fraction between {MIN_CONTEXT_THRESHOLD} and {MAX_CONTEXT_THRESHOLD}")
        result["context_threshold"] = float(threshold)
    if raw.get("max_output_tokens") is not None:
        cap = raw["max_output_tokens"]
        if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1024:
            raise ValueError("max_output_tokens must be an integer of at least 1024")
        result["max_output_tokens"] = cap
    return result


def session_overrides(run):
    """What the run's sessions -- its Last Order windows and its Sisters' cards -- run with instead
    of the global settings: its compaction threshold and output limit, when it chose them."""
    chosen = limits(run) if run is not None else {}
    return {key: chosen[key] for key in ("context_threshold", "max_output_tokens") if key in chosen}


def prepare_runner(con, table, row_id):
    key = secrets.token_hex(16)
    changed = con.execute(f'UPDATE "{table}" SET runner_key=?, last_error=NULL WHERE id=? '
                          'AND runner_pid IS NULL AND runner_identity IS NULL', (key, row_id)).rowcount
    if changed != 1:
        raise RuntimeError(f"Research execution {row_id} still has a runner")
    return key


def _own_identity(pid):
    """This process's identity, or ``host:pid`` where neither psutil nor ``ps`` can read its start
    time (a restricted container: every node crashed at start there, issue #10 audit, H5). Only
    for the process's own claim -- its PID cannot be reused while it runs. ``processes.identity``
    itself keeps answering None for an unreadable process: everywhere else that None is what keeps
    a reused PID from passing for the recorded one."""
    from misaka.core.platform import processes
    return processes.identity(pid) or f"{socket.gethostname()}:{pid}"


def claim_runner(con, table, row_id, key):
    """Fence delayed/duplicate spawns, including a lost pane.create reply."""
    from misaka.core.platform import processes
    if not key:
        return False
    pid = os.getpid()
    identity = _own_identity(pid)
    # The parent recorded this process's identity from its own psutil, whose start times can
    # sit a whole second from what this process reads (processes.IDENTITY_TOLERANCE_SECONDS),
    # so the fence compares in Python and the row then carries this process's own reading.
    row = con.execute(f'SELECT runner_pid, runner_identity FROM "{table}" WHERE id=? AND runner_key=?',
                      (row_id, key)).fetchone()
    if row is None:
        return False
    if row["runner_pid"] is not None and not (
            int(row["runner_pid"]) == pid and processes.same_identity(row["runner_identity"], identity)):
        return False
    return con.execute(f'UPDATE "{table}" SET runner_pid=?, runner_identity=? WHERE id=? AND runner_key=? '
                       'AND (runner_pid IS NULL OR runner_pid=?)',
                       (pid, identity, row_id, key, pid)).rowcount == 1


def note_claim_failure(con, table, row_id, key):
    """Say which part of the claim fence refused this process, and leave it on the row.

    2026-09-18 (B33): six node processes exited with "superseded execution" within a second
    of spawning, four resumes in a row, and the driver could only report "no exception was
    recorded". Every replay of the fence in isolation succeeded; the live difference was
    never captured because the losing side wrote nothing down. Now it does: the row as the
    process saw it against what the process holds, kept in ``last_error`` (only if nothing
    else is there yet) so the driver's error carries it."""
    from misaka.core.platform import processes
    row = con.execute(f'SELECT runner_key, runner_pid, runner_identity FROM "{table}" WHERE id=?',
                      (row_id,)).fetchone()
    pid = os.getpid()
    identity = _own_identity(pid)
    if row is None:
        reason = f"row {row_id} no longer exists"
    elif not key:
        reason = "this process was started without a runner key"
    elif row["runner_key"] != key:
        reason = (f"runner key mismatch: row holds {(row['runner_key'] or '')[:8] or 'NULL'!r}, "
                  f"this process holds {key[:8]!r}")
    elif row["runner_pid"] is not None and (row["runner_pid"] != pid
                                            or not processes.same_identity(row["runner_identity"], identity)):
        reason = (f"runner identity mismatch: row holds pid {row['runner_pid']} {row['runner_identity']!r}, "
                  f"this process is pid {pid} {identity!r}")
    else:
        reason = "fence refused for no visible reason (the row changed between the claim and this read)"
    text = f"claim refused: {reason}"
    try:
        con.execute(f'UPDATE "{table}" SET last_error=COALESCE(last_error,?) WHERE id=?', (text, row_id))
    except sqlite3.Error:
        pass
    return text


def release_runner(con, table, row_id, key):
    """The runner's routine is over but its process stays alive (an interactive node keeps its
    window open for the user). Consume the key too: release is durable even before the first
    parent poll, and a late spawn reply must never resurrect this execution."""
    con.execute(f'UPDATE "{table}" SET runner_pid=NULL, runner_identity=NULL, runner_key=NULL WHERE id=? AND runner_key=?',
                (row_id, key))


def session_dir(run, scope, *parts):
    """One conversation directory under the existing session store, not a separate run home."""
    from misaka.config import sessions
    values = (run["id"], scope, *parts)
    if any(not p or Path(p).name != p or p in {".", ".."} or "\\" in p for p in values):
        raise ValueError("Research session scope must contain names, not paths.")
    return os.path.join(sessions.sessions_root(), "research", "--".join(values))


def task_contexts(con):
    """Owning project and research lineage, with every card in the same project."""
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_runs'").fetchone():
        return {}
    init(con)
    contexts, snapshots = {}, {}
    for row in con.execute(
            "SELECT c.task_id,c.run_id,c.branch_id AS node_id,c.kind,c.round,r.workspace,r.origin_session "
            "FROM research_cards c JOIN research_runs r ON r.id=c.run_id").fetchall():
        if row["run_id"] not in snapshots:
            snapshots[row["run_id"]] = graph.snapshot(con, row["run_id"])
        found = snapshots[row["run_id"]].node(row["node_id"])
        if found is not None:
            contexts[row["task_id"]] = {**dict(row), "depth": found.attrs["depth"], "node_question": found.attrs["question"]}
    return contexts


def task_context(con, task_id):
    """One card's entry of ``task_contexts``, or None for a card outside every run."""
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_runs'").fetchone():
        return None
    init(con)
    row = con.execute(
        "SELECT c.task_id,c.run_id,c.branch_id AS node_id,c.kind,c.round,r.workspace,r.origin_session "
        "FROM research_cards c JOIN research_runs r ON r.id=c.run_id WHERE c.task_id=?", (task_id,)).fetchone()
    found = graph.snapshot(con, row["run_id"]).node(row["node_id"]) if row else None
    if found is None:
        return None
    return {**dict(row), "depth": found.attrs["depth"], "node_question": found.attrs["question"]}


def project_name(run):
    """Display name of the run's project: the workspace folder's basename."""
    return os.path.basename(run["workspace"].rstrip(os.sep)) or run["workspace"]


def for_session(con, session_id):
    """The runs a conversation started, newest first -- the ones whose mode it is in."""
    if not session_id or not con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_runs'").fetchone():
        return []
    return [dict(row) for row in con.execute(
        "SELECT * FROM research_runs WHERE origin_session=? ORDER BY created_at DESC", (session_id,))]


def is_active(con, run_id):
    """Whether a driver is meant to be advancing this run right now."""
    if not run_id or not con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_runs'").fetchone():
        return False
    row = con.execute("SELECT status FROM research_runs WHERE id=?", (run_id,)).fetchone()
    return bool(row) and row["status"] in ACTIVE


def ensure_layout(run):
    root = Path(run["workspace"])
    for rel in ("nodes", "final"):
        (root / rel).resolve().relative_to(root.resolve())
        (root / rel).mkdir(parents=True, exist_ok=True)
    return str(root)


def _atomic_write(path, content):
    atomic.write_text(path, content)


def _artifact_name(name):
    if not name or Path(name).name != name or name in {".", ".."} or "\\" in name:
        raise ValueError("Generated research artifacts need a filename, not a directory path.")
    return name


def versioned(name, version):
    """``synthesis.md`` for version 1, ``synthesis-<n>.md`` after: how every per-version product of a
    node is named (plans, conclusions, deliberations, a red team's critique of each version)."""
    stem, ext = os.path.splitext(name)
    return name if int(version) <= 1 else f"{stem}-{int(version)}{ext}"


def version_of(path, name):
    """The version ``versioned(name, n)`` put in ``path``'s file name, or None for another file."""
    stem, ext = os.path.splitext(name)
    base = os.path.basename(str(path))
    if base == name:
        return 1
    match = re.fullmatch(re.escape(stem) + r"-(\d+)" + re.escape(ext), base)
    return int(match.group(1)) if match else None


def generated_path(name, *, node_id):
    """A node's artifact, relative to the project: ``nodes/<node>/<name>``."""
    return os.path.join(node_dir(node_id), _artifact_name(name))


def run_path(run, name):
    """A run-level product (question, clarifications, survey, draft, final, partial, workspace
    index, graph views): ``final/<run>-<name>``, so the run's deliverables and the sources they
    cite sit in one folder and two runs of one project never overwrite each other."""
    return os.path.join("final", f"{run['id']}-{_artifact_name(name)}")


def create(con, *, workspace, question, limits=None, token_start=0, origin_session=None):
    workspace = os.path.realpath(os.path.expanduser(str(workspace or "")))
    if not os.path.isdir(workspace):
        raise ValueError(f"A research run needs an existing project folder: {workspace}")
    question = str(question or "").strip()
    if len(question) < 2:
        raise ValueError("The research question cannot be empty.")
    init(con)
    now = int(time.time())
    run_id = "r_" + secrets.token_hex(5)
    spec = normalize_limits(limits)
    # One unit: a run row without its root node is a run ``workflow.run`` would walk straight to
    # finalize as "every node closed", handing back a final report with no research behind it.
    # An interrupted create must leave no row at all.
    with task_store.write_txn(con):
        con.execute(
            "INSERT INTO research_runs "
            "(id,workspace,question,phase,status,limits_json,token_start,origin_session,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, workspace, question, "created", "active",
             json.dumps(spec, ensure_ascii=False), int(token_start), origin_session, now, now),
        )
        graph.create(con, "research", graph_schema(spec), id=run_id)
        row = get(con, run_id)
        ensure_layout(row)
        write_text(con, run_id, "question", 'Original research question', run_path(row, "question.md"),
                   f"""# Original research question

{question}
""")
        create_node(con, run_id, question=question, parents=())
    return get(con, run_id)


def get(con, run_id):
    init(con)
    row = con.execute("SELECT * FROM research_runs WHERE id=?", (run_id,)).fetchone()
    return _run(con, row) if row else None


def _run(con, row):
    """A run as callers read it. Its driver lease is its graph's."""
    found = graph.get(con, row["id"])
    return {**dict(row), "driver_lock": found.lease_token if found else None,
            "driver_expires": found.lease_expires if found else None}


def latest(con, workspace=None, active_only=False):
    init(con)
    q, args = "SELECT * FROM research_runs WHERE 1=1", []
    if workspace:
        q += " AND workspace=?"
        args.append(workspace)
    if active_only:
        q += " AND status IN ('active','waiting_input','stopping')"
    row = con.execute(q + " ORDER BY created_at DESC LIMIT 1", args).fetchone()
    return _run(con, row) if row else None


def listing(con, *, workspace=None):
    init(con)
    if workspace:
        rows = con.execute(
            "SELECT * FROM research_runs WHERE workspace=? ORDER BY created_at DESC", (workspace,)
        ).fetchall()
    else:
        rows = con.execute("SELECT * FROM research_runs ORDER BY created_at DESC").fetchall()
    return [_run(con, row) for row in rows]


def limits(run):
    try:
        return normalize_limits(json.loads(run["limits_json"]))
    except (TypeError, ValueError):
        return dict(DEFAULT_LIMITS)


# What `/research limits` and `misaka research --limits` may change on a running run, and when each
# change is first read. The workflow reads them from the database at those points (current_limits),
# not from the run row it started with.
ADJUSTABLE_LIMITS = {
    "sister_parallel": "at once",
    "parallel": "from the next level of nodes",
    "max_depth": "at the next reconciliation",
    "max_nodes": "at the next reconciliation",
    "max_followups": "at each node's next follow-up decision",
    "max_revisions": "at each node's next review",
}


def limits_changed(before, after):
    """What a change of limits did, and when each changed limit is first read."""
    changed = [key for key in ADJUSTABLE_LIMITS if before[key] != after[key]]
    if not changed:
        return "no limit changed"
    return "; ".join(f"{key} {before[key]} → {after[key]} ({ADJUSTABLE_LIMITS[key]})" for key in changed)


def current_limits(con, run):
    """The run's limits as they stand now: a run's limits can change while it runs (update_limits)."""
    return limits(get(con, run["id"]) or run)


def update_limits(con, run_id, changes):
    """Change a run's limits while it runs: ``(before, after)``. The same bounds as a new run's, and
    never below what the run already holds -- a graph of 12 nodes cannot be limited to 10. Raises
    ValueError saying what to change. The node count and depth are the graph's own bounds, so they
    change in its schema as well."""
    unknown = sorted(set(changes) - set(ADJUSTABLE_LIMITS))
    if unknown:
        raise ValueError(f"not adjustable: {', '.join(unknown)} (adjustable: {', '.join(ADJUSTABLE_LIMITS)})")
    with task_store.write_txn(con):
        run = get(con, run_id)
        if not run:
            raise ValueError(f"Research run not found: {run_id}")
        if run["status"] == "done":
            raise ValueError(f"Research run {run_id} is done; its limits no longer matter.")
        before = limits(run)
        after = normalize_limits({**json.loads(run["limits_json"] or "{}"), **changes})
        inquiries = graph.snapshot(con, run_id).nodes("inquiry")
        held, deepest = len(inquiries), max((int(found.attrs["depth"]) for found in inquiries), default=0)
        if after["max_nodes"] < held:
            raise ValueError(f"max_nodes {after['max_nodes']} is below the {held} nodes the graph already holds")
        if after["max_depth"] < deepest:
            raise ValueError(f"max_depth {after['max_depth']} is below the depth {deepest} a node already has")
        con.execute("UPDATE research_runs SET limits_json=? WHERE id=?", (json.dumps(after, sort_keys=True), run_id))
        if (after["max_nodes"], after["max_depth"]) != (before["max_nodes"], before["max_depth"]):
            with _holding(con, run_id) as token:
                graph.commit(con, run_id, "schema", graph_schema(after), token=token)
    return before, after


def set_state(con, run_id, *, phase=None, status=None, error=None,
              root_session=None, final_artifact=None, wave=None, driver_lock=None):
    """Update run state. With ``driver_lock`` the write lands only while that lease is held:
    a driver that lost its lease cannot fail or finish a run its successor now owns."""
    values, fields = [], []
    for name, value in (("phase", phase), ("status", status), ("last_error", error),
                        ("root_session", root_session and home.stored(root_session)),
                        ("final_artifact", final_artifact)):
        if value is not None:
            fields.append(f"{name}=?")
            values.append(value)
    if wave is not None:
        fields.append("wave=?")
        values.append(int(wave))
    fields.append("updated_at=?")
    values.extend([int(time.time()), run_id])
    with task_store.write_txn(con):
        if driver_lock is not None and not holds_driver(con, run_id, driver_lock):
            raise RuntimeError(f"Research run {run_id}: driver lease lost.")
        con.execute(f"UPDATE research_runs SET {','.join(fields)} WHERE id=?", values)
    return get(con, run_id)


def request_stop(con, run_id):
    con.execute(
        "UPDATE research_runs SET stop_requested=1,status='stopping',updated_at=? "
        "WHERE id=? AND status IN ('active','waiting_input','stopping')",
        (int(time.time()), run_id),
    )


def close(con, run_id, session_id):
    """Take a stopped or failed run out of the conversation that started it: ``for_session`` no
    longer returns it, so its notice stops standing there, and its cards' notices stop going there.
    A resume binds the resuming window again (``workflow.run``). False when it was not that
    conversation's paused run."""
    return con.execute(
        "UPDATE research_runs SET origin_session=NULL,updated_at=? "
        "WHERE id=? AND origin_session=? AND status IN ('stopped','failed')",
        (int(time.time()), run_id, session_id),
    ).rowcount == 1


def resume(con, run_id, *, driver_lock=None, clarification=""):
    with task_store.write_txn(con):
        return _resume(con, run_id, driver_lock=driver_lock, clarification=clarification)


def _resume(con, run_id, *, driver_lock, clarification):
    """Reopen a stopped, failed, or waiting run: same card generations, nodes pick up where they were.
    A finished run is not resumable: its report is final; start a new run."""
    current = get(con, run_id)
    if current is None:
        raise ValueError(f"Research run not found: {run_id}")
    if current["status"] == "done":
        raise ValueError(f"Research run {run_id} is done; start a new run instead of resuming it.")
    if (current["driver_lock"] and current["driver_lock"] != driver_lock
            and (current["driver_expires"] is None or current["driver_expires"] >= time.time())):
        raise ValueError(f"Research run {run_id} is already being driven by another process.")
    if clarification:
        n = len(artifacts(con, run_id, kind="clarification")) + 1
        write_text(con, run_id, "clarification", f"User clarification {n}",
                   run_path(current, f"clarification-{n}.md"), clarification + "\n")
        con.execute("UPDATE research_runs SET question=question||? WHERE id=?",
                    (f"\n\nUser clarification: {clarification}", run_id))
    for branch in nodes(con, run_id):
        round, previous = current_plan(con, run_id, branch["id"])
        if previous and previous["payload"].get("status") == "clarify":
            delete_action(con, run_id, branch["id"], plan_key(round))
    reopen_cards(con, run_id)
    # A failed node replans: every phase of ``_expand`` is idempotent over what it already
    # produced (saved plans, cards and project-local artifacts), so it picks up its own work.
    con.execute(
        "UPDATE research_execution SET status='planning',updated_at=? "
        "WHERE run_id=? AND status IN ('waiting_input','awaiting_approval','failed')", (int(time.time()), run_id),
    )
    con.execute(
        "UPDATE research_runs SET stop_requested=0,status='active',phase='active',last_error='',"
        "updated_at=? WHERE id=?", (int(time.time()), run_id),
    )
    return get(con, run_id)


def help_waiting(con, row):
    """True only for a card parked by its own current SendMessage help request."""
    if row["status"] not in {"blocked", "triage"} or row["block_kind"] != "needs_input":
        return False
    try:
        payload = json.loads(task_store.latest_payload(con, row["id"], row["status"], generation=row["generation"])
                             or "{}")
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("message_id") is not None


def parked(con, row):
    """A card blocked or in triage with no help request of its own waiting on an answer (dispatch's
    "blocked:" verdict, a missing project folder): nothing moves it on by itself. A node used to
    fail on it with no decision of Last Order's -- or as "a dependency cycle" -- and no resume
    reopened it (0.18.10 sweep); it is now a lost card like a failed one."""
    return row["status"] in {"blocked", "triage"} and not help_waiting(con, row)


def reopen_cards(con, run_id, node_id=None):
    """Send the run's (or one node's) stopped, failed and unusable cards back to their Sisters.
    A failed card is retried like a stopped one: without this the node it killed replays the
    identical failure on every resume, and the only way out is editing this database by hand.
    A card its node went on without stays as it is.

    The cards' files are read first. A stop lands in the file before the index commits, so a
    process that died between the two (2026-10-25, R31) left a card stopped in its file and
    todo here: deciding on the index alone skipped it, and the next dispatch tick put the
    file's stop back after this resume had passed it."""
    from misaka.core.platform import cards as card_files
    for row in tasks(con, run_id, node_id=node_id):
        if row["workspace"]:
            card_files.reconcile_one(con, row["workspace"], row["id"])
    given_up = given_up_cards(con, run_id, node_id)
    for row in tasks(con, run_id, node_id=node_id):
        if row["id"] in given_up:
            continue
        unusable = row["status"] == "done" and (
            task_store.latest_payload(con, row["id"], "research_review_missing", generation=row["generation"])
            or _drift_after_declaration(con, row["id"], int(row["generation"])))
        if row["status"] not in ("stopped", "failed") and not unusable and not parked(con, row):
            continue
        target = "todo" if task_store.parent_ids(con, row["id"]) else "ready"
        if task_store.reopen_task(
            con, row["id"], target_status=target,
            expected_generation=row["generation"], invalidate_descendants=False,
        ):
            task_store.add_event(
                con, row["id"], "research_resumed",
                {"from_generation": row["generation"]}, generation=int(row["generation"]) + 1,
            )


RETRY_LIMIT = 3     # ponytail: a fixed cap; a node that fails a fourth time needs a person, not a loop


def retry_node(con, run, node_id, *, session_file, tool_call_id):
    """Last Order's retry of a failed node, without waiting for the level to end and the run to
    be resumed: its failed cards go back to their Sisters and it replans -- every phase picks up
    what it already produced, as on a resume. While the level still runs, its driver starts the
    node again as soon as a slot is free; after the level, the resume does. Each retry is the
    node's command ``retry:<call>``; ``RETRY_LIMIT`` of them end the loop."""
    current = node(con, node_id)
    if current is None or current["run_id"] != run["id"]:
        raise ValueError(f"No node {node_id} in run {run['id']}.")
    if current["status"] != "failed":
        raise ValueError(f"Node {node_id} is {current['status']}, not failed; only a failed node is retried.")
    tried = len(graph.entries(con, run["id"], scope=node_id, key_prefix="retry:", status=_LIVE))
    if tried >= RETRY_LIMIT:
        raise ValueError(f"Node {node_id} was already retried {tried} times; its failure needs the user.")
    with task_store.write_txn(con):
        reopen_cards(con, run["id"], node_id)
        con.execute("UPDATE research_execution SET status='planning',last_error=NULL,updated_at=? WHERE id=?",
                    (int(time.time()), node_id))
        graph.propose(con, run["id"], "action", {"attempt": tried + 1}, key=f"retry:{tool_call_id}", scope=node_id,
                      proposer=_proposer(session_file, tool_call_id))
    return tried + 1


def _drift_after_declaration(con, task_id, generation):
    """A settle found this generation's files changed after its latest declaration. A card that
    declared again since (todo._redeclare_if_done) is not reopened for the older finding."""
    drift = con.execute("SELECT max(id) FROM events WHERE task_id=? AND kind='research_artifact_drift' "
                        "AND generation=?", (task_id, generation)).fetchone()[0]
    declared = con.execute("SELECT max(id) FROM events WHERE task_id=? AND kind='submitted' AND generation=?",
                           (task_id, generation)).fetchone()[0]
    return drift is not None and (declared is None or drift > declared)


def stop_requested(con, run_id):
    row = get(con, run_id)
    return not row or bool(row["stop_requested"])


def task_count(con, run_id):
    return con.execute(
        "SELECT COUNT(*) FROM research_cards WHERE run_id=?", (run_id,)
    ).fetchone()[0]


def link_task(con, run_id, task_id, *, kind, node, local_id=None, dependencies=(), round=1):
    """Attach a card to a node; one project-local output directory protects concurrent cards.
    ``round`` is the card's round within its kind: a research card's planning round, a red-team
    card's review round (a node may plan, and be reviewed, more than once)."""
    run = get(con, run_id)
    if not run:
        raise ValueError(f"Research run not found: {run_id}")
    task = task_store.get(con, task_id)
    if task is None or os.path.realpath(task["workspace"]) != run["workspace"]:
        raise ValueError("Research cards must execute in the run's project folder.")
    # The card lives inside its node's folder: nodes/<node>/cards/<card>.
    output_dir = Path(run["workspace"], node_dir(node["id"]), "cards", task_id)
    output_dir.resolve().relative_to(Path(run["workspace"]).resolve())
    dependencies = list(dependencies)
    con.execute(
        "INSERT INTO research_cards "
        "(task_id,run_id,branch_id,kind,round,local_id,depends_json,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (task_id, run_id, node["id"], kind, int(round), local_id,
         json.dumps(dependencies, ensure_ascii=False), int(time.time())),
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    con.execute("UPDATE tasks SET output_dir=? WHERE id=?", (str(output_dir), task_id))
    # ``cards.create(after_row=...)`` calls this before the child card file exists;
    # create publishes the complete needs list in its first file. Direct callers link
    # already-created cards here as before.
    task = task_store.get(con, task_id)
    if task is not None:
        from misaka.core.platform import cards as card_files
        card_exists = os.path.isfile(card_files.card_path(task["workspace"], task_id))
    else:
        card_exists = False
    if card_exists:
        parents = []
        for dependency in dependencies:
            parent = con.execute(
                "SELECT task_id FROM research_cards WHERE run_id=? AND branch_id=? AND local_id=?",
                (run_id, node["id"], dependency),
            ).fetchone()
            if parent:
                parents.append(parent["task_id"])
        try:
            task_store.link_dependencies(con, parents, task_id)
        except (ValueError, OSError) as error:
            task_store.add_event(con, task_id, "dependency_unlinked",
                                 {"parents": parents, "reason": str(error)})
            raise RuntimeError(f"Research dependency could not be linked onto {task_id}: {error}") from error


def tasks(con, run_id, *, kind=None, node_id=None, round=None):
    q, args = (
        ("SELECT t.*,rt.branch_id,rt.kind AS research_kind,rt.round,rt.local_id,rt.depends_json "
         "FROM research_cards rt JOIN tasks t ON t.id=rt.task_id WHERE rt.run_id=?"),
        [run_id],
    )
    if kind:
        q += " AND rt.kind=?"
        args.append(kind)
    if node_id is not None:
        q += " AND rt.branch_id=?"
        args.append(node_id)
    if round is not None:
        q += " AND rt.round=?"
        args.append(int(round))
    return con.execute(q + " ORDER BY rt.created_at", args).fetchall()


# --- nodes: the research graph --------------------------------------------------------

def create_node(con, run_id, *, question, parents):
    """A node and its fork edges, in the caller's transaction. Its depth is one more than its deepest
    parent's (0 for the root, the only node without parents); the graph keeps the forks acyclic."""
    run = get(con, run_id)
    if not run:
        raise ValueError(f"Research run not found: {run_id}")
    parents = list(dict.fromkeys(parents))
    node_id = _commit(con, run_id, "open_node", {"question": " ".join(str(question).split()), "parents": parents},
                      lambda change: _open_node(con, change, run, question, parents))
    return node(con, node_id)


def _open_node(con, change, run, question, parents):
    """Add an inquiry under ``parents`` to ``change``, with its execution row; returns its id."""
    view = change.view
    parents = list(dict.fromkeys(parents))
    rows = [view.node(parent_id) for parent_id in parents]
    if any(row is None or row.kind != "inquiry" for row in rows):
        raise ValueError("A node's parents must be nodes of the same run.")
    existing = len(view.nodes("inquiry"))
    if not rows and existing:
        raise ValueError("A run has one root; every later node has parents.")
    depth = max(int(row.attrs["depth"]) for row in rows) + 1 if rows else 0
    chosen = current_limits(con, run)
    if depth > chosen["max_depth"]:
        raise RuntimeError("The research run has reached its depth limit.")
    if existing >= chosen["max_nodes"]:
        raise RuntimeError("The research run has reached its node limit.")
    node_id = change.add_node("inquiry", {"question": " ".join(str(question).split()), "depth": depth},
                              id="b_" + secrets.token_hex(5))
    for parent_id in parents:
        change.add_edge("fork", parent_id, node_id)
    now = int(time.time())
    con.execute("INSERT INTO research_execution (id,run_id,status,created_at,updated_at) VALUES (?,?,'queued',?,?)",
                (node_id, run["id"], now, now))
    return node_id


def _node_row(row, found):
    return {**dict(row), "question": found.attrs["question"], "depth": int(found.attrs["depth"])}


def node(con, node_id):
    row = con.execute("SELECT * FROM research_execution WHERE id=?", (node_id,)).fetchone()
    if row is None:
        return None
    found = graph.snapshot(con, row["run_id"]).node(node_id)
    return _node_row(row, found) if found is not None else None


def nodes(con, run_id):
    """The run's nodes, shallowest first, each level in the order its nodes were opened."""
    rows = {row["id"]: row for row in con.execute("SELECT * FROM research_execution WHERE run_id=?", (run_id,))}
    if not rows:
        return []
    found = [item for item in graph.snapshot(con, run_id).nodes("inquiry") if item.id in rows]
    found.sort(key=lambda item: (int(item.attrs["depth"]), item.ordinal))
    return [_node_row(rows[item.id], item) for item in found]


def is_root(node):
    return int(node["depth"]) == 0


def root(con, run_id):
    return next((row for row in nodes(con, run_id) if row["depth"] == 0), None)


def _forks(con, node_id, *, outward):
    row = con.execute("SELECT run_id FROM research_execution WHERE id=?", (node_id,)).fetchone()
    if row is None:
        return []
    snap = graph.snapshot(con, row["run_id"])
    if outward:
        wanted = {edge.target for edge in snap.out_edges(node_id, "fork")}
    else:
        wanted = {edge.source for edge in snap.in_edges(node_id, "fork")}
    return [found for found in nodes(con, row["run_id"]) if found["id"] in wanted]


def parents(con, node_id):
    return _forks(con, node_id, outward=False)


def children(con, node_id):
    return _forks(con, node_id, outward=True)


def edges(con, run_id):
    if graph.get(con, run_id) is None:
        return []
    return [{"child_id": edge.target, "parent_id": edge.source} for edge in graph.snapshot(con, run_id).edges("fork")]


def next_level(con, run_id):
    """The BFS frontier: every node still expanding at the shallowest such depth, oldest first.
    A node deeper than all its parents never runs before them."""
    rows = [n for n in nodes(con, run_id) if n["status"] not in NODE_TERMINAL]
    return [n for n in rows if n["depth"] == rows[0]["depth"]] if rows else []


def set_node(con, node_id, *, status=None, session_file=None, context_artifact=None, owner=None):
    fields, values = [], []
    for name, value in (("status", status), ("session_file", session_file and home.stored(session_file)),
                        ("context_artifact", context_artifact)):
        if value is not None:
            fields.append(f"{name}=?")
            values.append(value)
    fields.append("updated_at=?")
    values.extend([int(time.time()), node_id])
    with task_store.write_txn(con):
        if owner is not None:
            check_owner(con, *owner)
        con.execute(f"UPDATE research_execution SET {','.join(fields)} WHERE id=?", values)
    return node(con, node_id)


def set_fork_entry(con, node_id, entry):
    """Where the node's own conversation begins in its session: the last entry it inherited (a
    fork's parent turns, or the root window's turns before the run). None = the whole session."""
    con.execute("UPDATE research_execution SET fork_entry=?,updated_at=? WHERE id=?",
                (entry, int(time.time()), node_id))


# --- issues: what review cards raised, and what Last Order did with each ------------------

def add_issue(con, run_id, *, node, task_id, round, origin, kind, question, rationale, priority=0):
    """One issue a review card raised: a red-team issue (``origin='critique'``) or a divergence
    proposal (``origin='divergence'``). Only an exact repeat inside the same card's same round,
    case and whitespace aside, is the same issue: the same question from another card or another
    node is its own, answered by its own Last Order, and one a reviewer raises again in a later
    round is answered again -- matched against the earlier round, it was folded into an issue
    already answered and the round looked clean (2026-10-01). Semantic judgement is Last Order's,
    never a match here."""
    question = str(question or "").strip()
    if not question:
        return None
    normalized = " ".join(question.casefold().split())
    for found in graph.snapshot(con, run_id).nodes("issue"):
        if (found.attrs["task_id"] == task_id and found.attrs["round"] == int(round)
                and " ".join(found.attrs["question"].casefold().split()) == normalized):
            return found.id
    attrs = {"task_id": task_id, "round": int(round), "origin": origin, "kind": str(kind or "unclassified"),
             "question": question, "rationale": str(rationale or ""), "priority": int(priority or 0),
             "disposition": None, "reason": None, "created_at": int(time.time())}

    def add(change):
        issue_id = change.add_node("issue", attrs, id="i_" + secrets.token_hex(5))
        change.add_edge("raised_on", issue_id, node["id"])
        return issue_id

    return _commit(con, run_id, "add_issue", {"node": node["id"], **attrs}, add)


def _end(snap, element_id, kind):
    """Where ``element_id``'s single ``kind`` edge leads, or None."""
    found = snap.out_edges(element_id, kind)
    return found[0].target if found else None


def _issue_row(snap, run_id, found):
    return {"id": found.id, "run_id": run_id, "branch_id": _end(snap, found.id, "raised_on"),
            **graph.thaw(found.attrs), "option_id": _end(snap, found.id, "becomes"),
            "covered_by": _end(snap, found.id, "covered_by")}


def drop_issues(con, run_id, issue_ids):
    """Take issues off the graph: a review card's latest submission replaces the ones it raised before."""
    issue_ids = list(issue_ids)
    if issue_ids:
        _commit(con, run_id, "drop_issues", {"issues": issue_ids},
                lambda change: [change.retire(issue_id) for issue_id in issue_ids] and None)


def issues(con, run_id, *, node_id=None, round=None, origin=None, option_id=None):
    if graph.get(con, run_id) is None:
        return []
    snap = graph.snapshot(con, run_id)
    wanted = {"branch_id": node_id, "round": round, "origin": origin, "option_id": option_id}
    rows = [row for row in (_issue_row(snap, run_id, found) for found in snap.nodes("issue"))
            if all(value is None or row[key] == value for key, value in wanted.items())]
    return sorted(rows, key=lambda row: (row["round"], -row["priority"], row["created_at"]))


def issue(con, issue_id):
    run_id = graph.locate(con, issue_id)
    if run_id is None:
        return None
    snap = graph.snapshot(con, run_id)
    found = snap.node(issue_id)
    return _issue_row(snap, run_id, found) if found is not None and found.kind == "issue" else None


def dispose(con, issue_id, disposition, *, option=None, covered_by=None, reason=None):
    """What Last Order did with one issue. Recorded from her accepted command, never inferred:
    ``option`` is the option that takes the issue up -- a ``branch`` issue's own, or the option
    waiting to be opened that a ``covered`` one falls under -- ``covered_by`` the node that already
    pursues a ``covered`` one."""
    run_id = graph.locate(con, issue_id)
    if run_id is None:
        return

    def record(change):
        change.update(issue_id, {"disposition": disposition, "reason": reason})
        for kind, target in (("becomes", option), ("covered_by", covered_by)):
            for edge in change.view.out_edges(issue_id, kind):
                change.retire(edge.id)
            if target:
                change.add_edge(kind, issue_id, target)

    _commit(con, run_id, "dispose", {"issue": issue_id, "disposition": disposition, "option": option,
                                     "covered_by": covered_by, "reason": reason}, record)


def destination(issue):
    """Where an issue went, in words: the option that takes it up or the node that covers it."""
    if issue["option_id"]:
        return f"option {issue['option_id']}"
    if issue["covered_by"]:
        return f"node {issue['covered_by']}"
    return None


def errata(con, run_id):
    """Corrections recorded against conclusions of this run, oldest first. Each is a node's command
    (``erratum:<call>``): ``raised_by`` is that node, ``node`` the one corrected."""
    return [{"raised_by": entry.scope, **graph.thaw(entry.payload)}
            for entry in graph.entries(con, run_id, key_prefix="erratum:", status=_LIVE)]


# --- decisions and options -------------------------------------------------------------

def _derived_id(prefix, *parts):
    """An id fixed by where the record came from, so recording it again changes nothing."""
    return prefix + hashlib.sha256("\x1f".join(str(part) for part in parts).encode()).hexdigest()[:12]


def add_decision(con, run_id, *, node, round, origin, index, question, stakes, options):
    """One decision a node made -- in its first plan (``origin='plan'``) or after its divergence
    review (``origin='decide'``) -- with its options. An option marked ``own`` is the node's own
    line and is pursued by the node itself; every other option waits for the level's
    reconciliation, which opens it as a node or records why not. Replays are idempotent."""
    decision_id = _derived_id("d_", node["id"], origin, round, index)
    if graph.snapshot(con, run_id).node(decision_id) is not None:
        return decision_id
    now = int(time.time())

    def add(change):
        change.add_node("decision", {"round": int(round), "origin": origin, "question": question, "stakes": stakes,
                                     "created_at": now}, id=decision_id)
        change.add_edge("decides", node["id"], decision_id)
        for position, choice in enumerate(options):
            option_id = change.add_node("option", {"label": choice["label"], "premise": choice["premise"],
                                                   "reason": None, "created_at": now},
                                        id=_derived_id("o_", decision_id, position))
            change.add_edge("offers", decision_id, option_id)
            if choice.get("own"):
                change.add_edge("opens", option_id, node["id"])
            for record in dict.fromkeys(choice.get("answers") or ()):
                change.add_edge("answers", option_id, record)
        return decision_id

    return _commit(con, run_id, "add_decision", {"node": node["id"], "origin": origin, "round": int(round),
                                                 "index": index, "question": question, "stakes": stakes,
                                                 "options": list(options)}, add)


def decisions(con, run_id, *, node_id=None):
    if graph.get(con, run_id) is None:
        return []
    snap, rows = graph.snapshot(con, run_id), []
    for found in snap.nodes("decision"):
        made = snap.in_edges(found.id, "decides")
        owner = made[0].source if made else None
        if node_id is None or owner == node_id:
            rows.append({"id": found.id, "run_id": run_id, "node_id": owner, **graph.thaw(found.attrs)})
    return rows


def _option_row(snap, run_id, found):
    offered = snap.in_edges(found.id, "offers")
    return {"id": found.id, "run_id": run_id, "decision_id": offered[0].source if offered else None,
            **graph.thaw(found.attrs), "node_id": _end(snap, found.id, "opens"),
            "covered_by": _end(snap, found.id, "covered_by"),
            "answers": [edge.target for edge in snap.out_edges(found.id, "answers")]}


def options(con, run_id, *, decision_id=None):
    if graph.get(con, run_id) is None:
        return []
    snap = graph.snapshot(con, run_id)
    return [row for row in (_option_row(snap, run_id, found) for found in snap.nodes("option"))
            if decision_id is None or row["decision_id"] == decision_id]


def option(con, option_id):
    run_id = graph.locate(con, option_id)
    if run_id is None:
        return None
    snap = graph.snapshot(con, run_id)
    found = snap.node(option_id)
    return _option_row(snap, run_id, found) if found is not None and found.kind == "option" else None


def _with_decision(snap, row):
    """An option with the decision that offered it: where it was decided, and what was asked."""
    decision = snap.node(row["decision_id"])
    made = snap.in_edges(decision.id, "decides")
    return {**row, "decided_at": made[0].source if made else None, "decision_question": decision.attrs["question"],
            "decision_stakes": decision.attrs["stakes"], "decision_origin": decision.attrs["origin"]}


def _options_with_decisions(con, run_id, keep):
    snap = graph.snapshot(con, run_id)
    found = [(snap.node(row["decision_id"]).ordinal, item.ordinal, _with_decision(snap, row))
             for item in snap.nodes("option") for row in [_option_row(snap, run_id, item)] if keep(row)]
    return [row for *_order, row in sorted(found, key=lambda each: each[:2])]


def pending_options(con, run_id):
    """Options no reconciliation has given an outcome yet: neither a node nor a reason."""
    if graph.get(con, run_id) is None:
        return []
    return _options_with_decisions(con, run_id, lambda row: row["node_id"] is None and row["reason"] is None)


def origin_options(con, node_id):
    """The options that led to this node: decided at another node and pursued here."""
    row = con.execute("SELECT run_id FROM research_execution WHERE id=?", (node_id,)).fetchone()
    if row is None:
        return []
    snap = graph.snapshot(con, row["run_id"])

    def led_here(option_row):
        made = snap.in_edges(option_row["decision_id"], "decides")
        return option_row["node_id"] == node_id and (made[0].source if made else None) != node_id

    return _options_with_decisions(con, row["run_id"], led_here)


def set_option(con, option_id, *, node_id=None, reason=None, covered_by=None):
    run_id = graph.locate(con, option_id)
    if run_id is not None:
        _commit(con, run_id, "set_option", {"option": option_id, "node": node_id, "reason": reason,
                                            "covered_by": covered_by},
                lambda change: _set_option(change, option_id, node_id, reason, covered_by))


def _set_option(change, option_id, node_id, reason, covered_by=None):
    """The outcome of an option: the node that pursues it, or the reason it was not opened -- with,
    when something answers it, that node or the option opened for it (``covered_by``)."""
    change.update(option_id, {"reason": reason})
    for kind in ("opens", "covered_by"):
        for edge in change.view.out_edges(option_id, kind):
            change.retire(edge.id)
    if node_id:
        change.add_edge("opens", option_id, node_id)
    elif covered_by:
        change.add_edge("covered_by", option_id, covered_by)


# --- relations: what links nodes without scheduling anything ------------------------------

def relations(con, run_id):
    if graph.get(con, run_id) is None:
        return []
    snap, rows = graph.snapshot(con, run_id), []
    for found in snap.nodes("relation"):
        members = sorted(snap.out_edges(found.id, "member"), key=lambda edge: edge.attrs.get("position", 0))
        rows.append({"id": found.id, "run_id": run_id, **graph.thaw(found.attrs),
                     "nodes": [edge.target for edge in members]})
    return rows


def _add_relation(change, relation_id, kind, note, members, now):
    """One relation and its member nodes, in order; recording the same id again changes nothing."""
    if change.view.node(relation_id) is not None:
        return
    change.add_node("relation", {"kind": kind, "note": note, "created_at": now}, id=relation_id)
    for position, member in enumerate(dict.fromkeys(members)):
        change.add_edge("member", relation_id, member, {"position": position})


def add_relations(con, run, items):
    """Record relations seen outside a reconciliation (the survey of a finished run). Each is
    identified by what it says, so recording it again changes nothing. Returns how many are new."""
    with task_store.write_txn(con):
        _owned_run(con, run)
        known = {row["id"] for row in relations(con, run["id"])}
        fresh = []
        for item in items:
            members = list(dict.fromkeys(item["nodes"]))
            relation_id = _derived_id("rel_", run["id"], item["kind"], *sorted(members), item["note"])
            if relation_id not in known:
                known.add(relation_id)
                fresh.append((relation_id, item["kind"], item["note"], members))
        if fresh:
            now = int(time.time())
            _commit(con, run["id"], "relate", {"relations": [list(item) for item in fresh]},
                    lambda change: [_add_relation(change, *item, now) for item in fresh] and None)
        return len(fresh)


# --- reconciliation: the level barrier's run-level command -------------------------------

def _reconcile_key(round):
    return f"reconcile:{int(round)}"


def _reconcile_row(run_id, entry):
    who = entry.proposer or {}
    return {"run_id": run_id, "round": int(entry.key.split(":", 1)[1]), "payload": graph.thaw(entry.payload),
            "session_file": _pointer(who.get("session_file")), "tool_call_id": who.get("tool_call_id"),
            "approved_call_id": who.get("tool_call_id") if entry.approver is not None else None,
            "receipt": graph.thaw(entry.receipt) if entry.status == graph.APPLIED else None,
            "applied_at": entry.applied_at, "created_at": who.get("created_at", entry.created_at)}


def reconcile(con, run_id, round):
    entry = graph.current(con, run_id, _reconcile_key(round))
    return _reconcile_row(run_id, entry) if entry else None


def reconcile_rounds(con, run_id):
    """Every reconciliation of the run, oldest first."""
    return sorted((_reconcile_row(run_id, entry) for entry in
                   graph.entries(con, run_id, scope="", key_prefix="reconcile:", status=_LIVE)),
                  key=lambda row: row["round"])


def withdraw_reconcile(con, run_id, round):
    """Drop a reconciliation that was not applied: its round is reconciled again from the start."""
    entry = graph.current(con, run_id, _reconcile_key(round))
    if entry is not None and entry.status != graph.APPLIED:
        graph.withdraw(con, entry.id)


def _owned_run(con, run):
    check_owner(con, run, None, allow_stop=True)
    current = get(con, run["id"])
    if current["stop_requested"] or current["status"] != "active":
        raise ValueError("Research command belongs to a stopped or superseded run.")


def record_reconcile(con, run, round, payload, *, session_file, tool_call_id):
    """Accept the root Last Order's reconciliation for ``round``. A second call before it is
    applied replaces the first, and the user's go-ahead for the earlier one no longer counts."""
    with task_store.write_txn(con):
        _owned_run(con, run)
        prior = reconcile(con, run["id"], round)
        if prior is not None and prior["applied_at"] is not None:
            raise ValueError(f"Reconciliation {round} is already applied; the graph has moved on.")
        graph.propose(con, run["id"], "reconcile", payload, key=_reconcile_key(round),
                      proposer=_proposer(session_file, tool_call_id), supersede=True)
    return reconcile(con, run["id"], round)


def approve_reconcile(con, run, round):
    """The user's go-ahead, recorded by the root Last Order, for the reconciliation as it stands."""
    with task_store.write_txn(con):
        _owned_run(con, run)
        entry = graph.current(con, run["id"], _reconcile_key(round))
        if entry is None:
            raise ValueError("There is no recorded reconciliation to start; record one with misaka_research_reconcile first.")
        graph.approve(con, entry.id, approver=_APPROVER)
    return reconcile(con, run["id"], round)


def reconcile_approved(con, run_id, round):
    current = reconcile(con, run_id, round)
    return bool(current and current["approved_call_id"] == current["tool_call_id"])


def apply_reconcile(con, run, round, resolved, *, considered):
    """Open what a reconciliation decided, in one application to the graph: its nodes and their
    edges, the outcome of every option it answered, its relations, and the receipt. Returns the
    receipt; a reconciliation already applied is returned as it was, never applied twice."""
    with task_store.write_txn(con):
        _owned_run(con, run)
        entry = graph.current(con, run["id"], _reconcile_key(round))
        if entry is None:
            raise ValueError(f"Reconciliation {round} was never recorded.")
        if entry.status == graph.APPLIED:
            return graph.thaw(entry.receipt)

        def open_level(change, _entry):
            now = int(time.time())
            opened = {"nodes": [], "joins": []}
            # Coverage claims first: a reopened proposal becomes an option here, which the outcomes below name.
            as_option = _review_coverage(change, round, resolved.get("coverage") or (), now)
            for item in resolved["nodes"]:
                child = _open_node(con, change, run, item["question"], item["parents"])
                for option_id in item["options"]:
                    _set_option(change, as_option.get(option_id, option_id), child, None)
                opened["nodes"].append(child)
            for item in resolved["joins"]:
                opened["joins"].append(_open_node(con, change, run, item["question"], item["parents"]))
            for item in resolved["not_pursued"]:
                cover = item.get("covered_by")
                _set_option(change, as_option.get(item["option"], item["option"]), None, item["reason"],
                            as_option.get(cover, cover))
            for position, item in enumerate(resolved["relations"]):
                _add_relation(change, _derived_id("rel_", run["id"], round, position), item["kind"], item["note"],
                              item["nodes"], now)
            return {"considered": list(considered), **opened, **({"reopened": as_option} if as_option else {})}

        with _holding(con, run["id"]) as token:
            receipt = graph.apply(con, entry.id, token=token, handlers={"reconcile": open_level})
    return graph.thaw(receipt)


REOPENED_QUESTION = ("Reconciliation {round}: possibilities raised here that the node named as covering them "
                     "does not pursue")
REOPENED_STAKES = ("Each was closed as covered by a node or an option of another line; reading both lines, the "
                   "reconciliation found that line does not answer it, so it is a possibility of its own.")


def _review_coverage(change, round, verdicts, now):
    """The reconciliation's verdict on each coverage claim (``views.coverage_claims``), on the claim's
    issue. A claim that holds keeps its pointer. A reopened one becomes an option of a decision the
    reconciliation records at the node that raised it -- the proposal's question and premise, the verdict's
    note recorded as why it was reopened -- which the reconciliation then opens or leaves unopened like any
    pending option. Returns issue id -> option id."""
    as_option, by_node = {}, {}
    for verdict in verdicts:
        review = {"round": int(round), "verdict": verdict["verdict"], "note": verdict["note"],
                  "claimed": verdict["covered_by"]}
        change.update(verdict["proposal"], {"coverage_review": review})
        if verdict["verdict"] == "reopen":
            by_node.setdefault(verdict["node"], []).append(verdict)
    for node_id, items in by_node.items():
        decision_id = _derived_id("d_", node_id, "reconcile", round, 0)
        change.add_node("decision", {"round": int(round), "origin": "reconcile", "question": REOPENED_QUESTION.format(
            round=int(round)), "stakes": REOPENED_STAKES, "created_at": now}, id=decision_id)
        change.add_edge("decides", node_id, decision_id)
        for position, verdict in enumerate(items):
            issue_id = verdict["proposal"]
            question = change.view.node(issue_id).attrs["question"]
            option_id = change.add_node("option", {"label": question, "premise": verdict["premise"] or question,
                                                   "reason": None, "created_at": now},
                                        id=_derived_id("o_", decision_id, position))
            change.add_edge("offers", decision_id, option_id)
            for kind in ("covered_by", "becomes"):
                for edge in change.view.out_edges(issue_id, kind):
                    change.retire(edge.id)
            change.add_edge("becomes", issue_id, option_id)
            change.update(issue_id, {"disposition": "branch", "reason": (
                f"reconciliation {int(round)}: not covered by {verdict['covered_by']} -- {verdict['note']}")})
            as_option[issue_id] = option_id
    return as_option


_LIVE = (graph.PROPOSED, graph.APPROVED, graph.APPLIED)
# Who approves a reconciliation: the user, through the root Last Order's command.
_APPROVER = {"by": "user"}


def _proposer(session_file, tool_call_id, created_at=None, *, stored=False):
    """Who recorded a command: the session (kept home-relative, as board rows keep it) and the call."""
    return {"session_file": (session_file if stored else session_file and home.stored(session_file)) or None,
            "tool_call_id": tool_call_id, "created_at": int(created_at if created_at is not None else time.time())}


def _pointer(value):
    return str(home.from_stored(value)) if isinstance(value, str) and value else value


def _action(run_id, entry):
    who = entry.proposer or {}
    return {"run_id": run_id, "branch_id": entry.scope, "action_key": entry.key, "payload": graph.thaw(entry.payload),
            "session_file": _pointer(who.get("session_file")), "tool_call_id": who.get("tool_call_id"),
            "created_at": who.get("created_at", entry.created_at)}


def action(con, run_id, node_id, key):
    entry = graph.current(con, run_id, key, scope=node_id)
    return _action(run_id, entry) if entry else None


def actions(con, run_id, node_id, prefix):
    """A node's standing commands whose keys begin with ``prefix``, oldest first."""
    return [_action(run_id, entry) for entry in graph.entries(con, run_id, scope=node_id, key_prefix=prefix,
                                                              status=_LIVE)]


def withdraw_actions(con, run_id, node_id, prefix):
    """Withdraw a node's standing commands whose keys begin with ``prefix``."""
    with task_store.write_txn(con):
        for entry in graph.entries(con, run_id, scope=node_id, key_prefix=prefix, status=_LIVE):
            graph.withdraw(con, entry.id)


def check_owner(con, run, branch=None, *, allow_stop=False):
    """Identity is independent of phase: closing/final review and cleanup retain an owner."""
    current = get(con, run["id"])
    owner = node(con, branch["id"]) if branch is not None else None
    if (not current or current["driver_lock"] != run["driver_lock"]
            or (branch is not None and (not owner or owner["run_id"] != run["id"]
                                        or owner["runner_key"] != branch["runner_key"]))):
        raise ValueError("Research execution belongs to a superseded owner.")
    if not allow_stop and current["stop_requested"]:
        raise InterruptedError("Research stopped.")


@contextmanager
def owned_txn(con, run, branch=None, *, allow_stop=False):
    """Check the captured identity under the same writer lock as its state transition."""
    with task_store.write_txn(con):
        check_owner(con, run, branch, allow_stop=allow_stop)
        yield


def _owned(con, run, branch):
    check_owner(con, run, branch, allow_stop=True)
    current, owner = get(con, run["id"]), node(con, branch["id"])
    if current["stop_requested"] or current["status"] != "active" or owner["status"] in NODE_TERMINAL:
        raise ValueError("Research command belongs to a stopped or superseded node.")


def record_action(con, run, branch, key, payload, *, session_file, tool_call_id):
    """Accept one LO command, not prose inferred by the driver. Replays are idempotent."""
    with task_store.write_txn(con):
        _owned(con, run, branch)
        prior = action(con, run["id"], branch["id"], key)
        if prior:
            if prior["payload"] != payload:
                raise ValueError("This research command was already accepted with different arguments.")
            return prior
        graph.propose(con, run["id"], "action", payload, key=key, scope=branch["id"],
                      proposer=_proposer(session_file, tool_call_id))
    return action(con, run["id"], branch["id"], key)


def replace_action(con, run, branch, key, payload, *, session_file, tool_call_id):
    """Accept a command that supersedes the earlier one of its key: a plan revised while it waits
    for the user, or a fresh start after such a revision. Same ownership rule as record_action. The
    earlier one stays on the journal, voided."""
    with task_store.write_txn(con):
        _owned(con, run, branch)
        graph.propose(con, run["id"], "action", payload, key=key, scope=branch["id"],
                      proposer=_proposer(session_file, tool_call_id), supersede=True)
    return action(con, run["id"], branch["id"], key)


SKIP_KEY = "skip"
DECIDE_KEY = "decide"
# The user's decision on a dissolution of the research question, recorded on the root node.
CONSENT_KEY = "consent:dissolution"


def cards_failed_key(cards):
    """The action key of a node Last Order's decision on ``cards`` that failed for good: the same
    failure is decided once, a card that fails again after a retry is a new decision."""
    return "cards_failed:" + ",".join(f"{card['id']}@{card['generation']}" for card in cards)


def plan_change_key(round):
    """The action-key prefix of the changes Last Order made to a round's cards while they ran."""
    return f"cards:{int(round)}"


def plan_changes(con, run_id, node_id, round):
    """The changes Last Order made to a round's cards while they ran, oldest first."""
    return [row["payload"] for row in actions(con, run_id, node_id, plan_change_key(round) + ":")]


def failed_card_decisions(con, run_id, node_id=None):
    """What node Last Orders decided about cards that failed for good, oldest first: each names
    its ``decision`` and the ``cards`` it covers."""
    return [graph.thaw(entry.payload) for entry in
            graph.entries(con, run_id, scope=node_id, key_prefix="cards_failed:", status=_LIVE)]


def given_up_cards(con, run_id, node_id=None):
    """Cards a node's Last Order went on without: never reopened, never driven again."""
    return {card for decision in failed_card_decisions(con, run_id, node_id) if decision["decision"] == "conclude"
            for card in decision["cards"]}


def skipped(con, run_id, node_id):
    """The user's decision, recorded by the node's Last Order, to close this node unresearched."""
    return action(con, run_id, node_id, SKIP_KEY)


def delete_action(con, run_id, node_id, key):
    entry = graph.current(con, run_id, key, scope=node_id)
    if entry is not None:
        graph.withdraw(con, entry.id)


def plan_key(round):
    """The action key of a round's plan: the first round is the bare ``plan``."""
    return "plan" if int(round) <= 1 else f"plan:{int(round)}"


def start_key(round):
    return "start" if int(round) <= 1 else f"start:{int(round)}"


def dispose_key(round):
    """The action key of Last Order's dispositions for review round ``round``."""
    return f"dispose:{int(round)}"


def gaps_key(version):
    """The action key of Last Order's answer to the divergence review of version ``version``."""
    return f"gaps:{int(version)}"


def dissolve_key(version):
    """The action key saying that conclusion version ``version`` dissolves a question."""
    return f"dissolve:{int(version)}"


def record_run_action(con, run, key, payload, *, session_file, tool_call_id):
    """A run-level command of the root Last Order -- the user's consent to a dissolution, a part of
    the final report -- kept with the root node, whose question it answers. The root has finished its
    own work by then, so ownership is the run's. A later call for the same key replaces the earlier."""
    with task_store.write_txn(con):
        _owned_run(con, run)
        node = root(con, run["id"])
        graph.propose(con, run["id"], "action", payload, key=key, scope=node["id"],
                      proposer=_proposer(session_file, tool_call_id), supersede=True)
    return action(con, run["id"], node["id"], key)


def plan_round(con, run_id, node_id):
    """The highest planning round this node has recorded a plan for; 0 before the first."""
    keys = [entry.key for entry in graph.entries(con, run_id, scope=node_id, key_prefix="plan", status=_LIVE)]
    rounds = [1 if key == "plan" else int(key.split(":", 1)[1]) for key in keys
              if key == "plan" or re.fullmatch(r"plan:\d+", key)]
    return max(rounds) if rounds else 0


def current_plan(con, run_id, node_id):
    """``(round, plan action)`` for the node's latest round; ``(0, None)`` before the first."""
    round = plan_round(con, run_id, node_id)
    return (round, action(con, run_id, node_id, plan_key(round))) if round else (0, None)


def red_team(con, run_id, node_id):
    """The node's red team as its first plan named her (``{"assignee", "reason"}``), or None
    before a plan does: the same Sister reviews every version and runs the divergence review."""
    plan = action(con, run_id, node_id, plan_key(1)) or current_plan(con, run_id, node_id)[1]
    return (plan["payload"].get("red_team") if plan else None) or None


def plan_started(con, run_id, node_id):
    """True once Last Order has recorded the user's go-ahead for the plan as it stands now: a
    start names the plan it was given for, so a revision after it needs a new start, and each
    round has its own start."""
    round, plan = current_plan(con, run_id, node_id)
    start = action(con, run_id, node_id, start_key(round)) if round else None
    return bool(plan and start and start["payload"].get("plan_tool_call_id") == plan["tool_call_id"])


def reframe(con, run_id, node, question):
    """The user agreed to Last Order's reframed question: the root's is the run's question too."""
    question = " ".join(str(question or "").split())
    if not question:
        return
    with task_store.write_txn(con):
        _commit(con, run_id, "reframe", {"node": node["id"], "question": question},
                lambda change: change.update(node["id"], {"question": question}))
        if is_root(node):
            con.execute("UPDATE research_runs SET question=?,updated_at=? WHERE id=?",
                        (question, int(time.time()), run_id))


# --- artifacts -------------------------------------------------------------------------

def write_text(con, run_id, kind, title, relative_path, content, *,
               branch_id=None, task_id=None, metadata=None):
    run = get(con, run_id)
    if not run:
        raise ValueError(f"Research run not found: {run_id}")
    root = Path(run["workspace"]).resolve()
    path = (root / relative_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError("Research artifact path is outside the project workspace.") from error
    from misaka.core.research import publication

    content = str(content)
    sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    # Recover a previous process before entering our own transaction. An in-flight
    # journal of this connection belongs to its outer transaction, not a dead writer.
    if not con.in_transaction:
        publication.recover(con, path)
    try:
        with task_store.write_txn(con):
            old = con.execute(
                "SELECT id FROM research_artifacts WHERE run_id=? AND path=? AND task_id IS ?",
                (run_id, str(path), task_id),
            ).fetchone()
            aid = old["id"] if old else "a_" + secrets.token_hex(5)
            # Register before touching files: an unmanaged outer raw-SQL transaction
            # has no completion hook; its caller should use task_store.write_txn.
            task_store.on_transaction_end(con, lambda: publication.recover(con, path))
            checkpoint = publication.prepare(con, path, aid, sha)
            # Savepoint also restores metadata if a caller catches our I/O error in
            # a larger transaction and decides to commit its other work.
            try:
                with _savepoint(con):
                    if old:
                        con.execute(
                            "UPDATE research_artifacts SET sha256=?,title=?,kind=?,metadata_json=?,created_at=? WHERE id=?",
                            (sha, str(title), str(kind), json.dumps(metadata or {}, ensure_ascii=False), int(time.time()), aid))
                    else:
                        con.execute(
                            "INSERT INTO research_artifacts "
                            "(id,run_id,branch_id,task_id,kind,title,path,sha256,metadata_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (aid, run_id, branch_id, task_id, str(kind), str(title), str(path), sha,
                             json.dumps(metadata or {}, ensure_ascii=False), int(time.time())))
                    _atomic_write(path, content)
            except BaseException:
                publication.restore_attempt(path, checkpoint)
                raise
    except BaseException:
        if not con.in_transaction:
            publication.recover(con, path)
        raise
    if not con.in_transaction:
        publication.recover(con, path)
    return aid, str(path)


def register_file(con, run_id, kind, title, path, *, sha256, branch_id=None, task_id=None, metadata=None):
    """Register a project file at its actual, permanent location without copying it."""
    run = get(con, run_id)
    path = str(Path(path).resolve())
    Path(path).relative_to(Path(run["workspace"]).resolve())
    old = con.execute(
        "SELECT id FROM research_artifacts WHERE run_id=? AND path=? AND task_id IS ?",
        (run_id, path, task_id),
    ).fetchone()
    if old:
        con.execute(
            "UPDATE research_artifacts SET sha256=?,title=?,kind=?,branch_id=?,metadata_json=?,created_at=? "
            "WHERE id=?",
            (sha256, str(title), str(kind), branch_id, json.dumps(metadata or {}, ensure_ascii=False),
             int(time.time()), old["id"]),
        )
        return old["id"], path
    aid = "a_" + secrets.token_hex(5)
    con.execute(
        "INSERT INTO research_artifacts "
        "(id,run_id,branch_id,task_id,kind,title,path,sha256,metadata_json,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (aid, run_id, branch_id, task_id, str(kind), str(title), path, sha256,
         json.dumps(metadata or {}, ensure_ascii=False), int(time.time())),
    )
    return aid, path


def artifact_text(row):
    """Read the registered project file, verifying its frozen digest."""
    data = Path(row["path"]).read_bytes()
    if hashlib.sha256(data).hexdigest() != row["sha256"]:
        raise ValueError(f"artifact {row['id']} changed since it was registered: {row['path']}")
    return data.decode("utf-8")


def frozen_refusal(target):
    """Why a file tool may not write ``target``, or None: a file the workflow wrote and froze for
    a run that is not over -- a node's plan, context and conclusions, the run's survey and report
    draft. A hand edit fails the integrity check of the phase that reads it next; a draft edited
    during its final review failed a whole run that way (B126). A card's deliverable is registered
    with its card and stays its Sister's to change, and a finished run's files are anyone's."""
    from misaka.config import CFG, home

    try:
        con = task_store.connect(os.path.expanduser(CFG["db"]))
    except (OSError, sqlite3.Error):
        return None
    try:
        # Compared as the file system compares names (home.fold): a case variant, or on macOS a
        # decomposed Unicode spelling, is the same file there and edited it past this check.
        wanted = home.fold(str(Path(target).resolve()))
        row = next((found for found in con.execute(
            "SELECT a.kind, a.run_id, a.path FROM research_artifacts a JOIN research_runs r ON r.id=a.run_id "
            "WHERE a.task_id IS NULL AND r.status!='done'") if home.fold(found["path"]) == wanted), None)
    except sqlite3.OperationalError:     # a board research has never touched has no such tables
        return None
    finally:
        con.close()
    if row is None:
        return None
    return (f"{target} is the {row['kind']} research run {row['run_id']} saved and froze: an edit here fails "
            "the integrity check of the phase that reads it next. What it should say goes through the phase "
            "that writes it -- a conclusion is revised in the node's next synthesis, the report draft in the "
            "final adjudication.")


def artifact_path(row):
    return row["path"]


def artifacts(con, run_id, *, branch_id=None, kind=None, task_id=None, run_level=False):
    """Registered files of the run: one node's (``branch_id``), the run's own products
    (``run_level``: question, clarifications, survey, draft, final, partial, workspace index),
    or all of them."""
    q, args = "SELECT * FROM research_artifacts WHERE run_id=?", [run_id]
    if branch_id is not None:
        q += " AND branch_id=?"
        args.append(branch_id)
    elif run_level:
        q += " AND branch_id IS NULL"
    if kind:
        q += " AND kind=?"
        args.append(kind)
    if task_id:
        q += " AND task_id=?"
        args.append(task_id)
    return con.execute(q + " ORDER BY created_at,rowid", args).fetchall()


def artifact(con, artifact_id):
    return con.execute("SELECT * FROM research_artifacts WHERE id=?", (artifact_id,)).fetchone()


def recover_publications(con, run):
    """Resume-only recovery; ordinary artifact/viewer reads never mutate the workspace."""
    from misaka.core.research import publication

    root = Path(run["workspace"])
    paths = {Path(row["path"]) for row in artifacts(con, run["id"])}
    # Include an interrupted first write whose registration rolled back completely.
    for branch in nodes(con, run["id"]):
        for journal in (root / node_dir(branch["id"])).glob(".*.research-publish.json"):
            paths.add(journal.with_name(journal.name[1:-len(".research-publish.json")]))
    sample = root / run_path(run, "checkpoint")
    for journal in sample.parent.glob(f".{run['id']}-*.research-publish.json"):
        paths.add(journal.with_name(journal.name[1:-len(".research-publish.json")]))
    for path in paths:
        path.resolve().relative_to(root.resolve())
        with owned_txn(con, run, allow_stop=True):
            publication.recover(con, path)


def read_artifact(con, artifact_id):
    row = artifact(con, artifact_id)
    if not row:
        return None
    try:
        return artifact_text(row)
    except OSError:
        return None


def summary(con, run_id):
    run = get(con, run_id)
    if not run:
        return None
    return {
        "id": run["id"], "workspace": run["workspace"], "phase": run["phase"],
        "status": run["status"], "wave": run["wave"], "limits": limits(run),
        "tasks": task_count(con, run_id), "nodes": len(nodes(con, run_id)),
        "undisposed_issues": sum(1 for row in issues(con, run_id) if row["disposition"] is None),
        "pending_options": len(pending_options(con, run_id)),
        "stop_requested": bool(run["stop_requested"]),
        "final_artifact": run["final_artifact"], "last_error": run["last_error"],
    }

"""Persistent research-run state and file artifacts.

A run is a tree of nodes (``research_branches``): the root is the first conclusion, every
other node investigates a material issue raised against its parent's conclusion. Research prose
lives directly in the project from its first write: ``<workspace>/nodes/<node>/`` per node (its cards
under ``cards/``) and ``<workspace>/final/`` for run-level products.
Session transcripts share the existing ``~/.misaka/sessions`` store. SQLite stores only workflow state and relationships, so a
Last Order process that died can resume the same run.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from misaka.core.platform import tasks as task_store
from misaka.utils import atomic

REVIEW_KINDS = {"red_team", "final_review"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS research_actions (
  run_id TEXT NOT NULL,
  branch_id TEXT NOT NULL,
  action_key TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  session_file TEXT NOT NULL,
  tool_call_id TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY(run_id,branch_id,action_key)
);
CREATE TABLE IF NOT EXISTS research_runs (
  id              TEXT PRIMARY KEY,
  workspace       TEXT NOT NULL,
  artifact_layout TEXT,
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
  driver_lock     TEXT,
  driver_expires  INTEGER,
  created_at      INTEGER NOT NULL,
  updated_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS research_branches (
  id              TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL,
  parent_id       TEXT,
  trigger_text    TEXT NOT NULL,
  depth           INTEGER NOT NULL,
  status          TEXT NOT NULL DEFAULT 'queued',
  session_file    TEXT,
  context_artifact TEXT,
  runner_pid      INTEGER,
  runner_identity TEXT,
  runner_key      TEXT,
  last_error      TEXT,
  created_at      INTEGER NOT NULL,
  updated_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS research_run_tasks (
  task_id         TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL,
  branch_id       TEXT NOT NULL,
  kind            TEXT NOT NULL,
  wave            INTEGER NOT NULL DEFAULT 0,
  local_id        TEXT,
  issue_id        TEXT,
  depends_json    TEXT NOT NULL DEFAULT '[]',
  created_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS research_issues (
  id              TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL,
  branch_id       TEXT NOT NULL,
  wave            INTEGER NOT NULL,
  kind            TEXT NOT NULL,
  question        TEXT NOT NULL,
  rationale       TEXT NOT NULL,
  priority        INTEGER NOT NULL DEFAULT 0,
  status          TEXT NOT NULL DEFAULT 'open',
  reason          TEXT,
  child_branch_id TEXT,
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
CREATE INDEX IF NOT EXISTS research_branches_run ON research_branches(run_id,depth);
CREATE INDEX IF NOT EXISTS research_tasks_run ON research_run_tasks(run_id,branch_id,kind);
CREATE INDEX IF NOT EXISTS research_issues_run ON research_issues(run_id,branch_id,status);
CREATE INDEX IF NOT EXISTS research_artifacts_run ON research_artifacts(run_id,branch_id,kind);
CREATE UNIQUE INDEX IF NOT EXISTS research_artifacts_generated ON research_artifacts(run_id,path) WHERE task_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS research_artifacts_task ON research_artifacts(run_id,task_id,path) WHERE task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS research_findings_run ON research_findings(run_id,branch_id,task_id);
CREATE INDEX IF NOT EXISTS research_claims_finding ON research_claims(finding_id);
CREATE UNIQUE INDEX IF NOT EXISTS research_tasks_local ON research_run_tasks(run_id,branch_id,local_id) WHERE local_id IS NOT NULL;
"""

ACTIVE = ("active", "waiting_input", "stopping")
NODE_TERMINAL = ("closed", "failed", "parked")
# closing = own research complete, waiting for child nodes; no filesystem merge
DEFAULT_LIMITS = {"max_depth": 3, "parallel": 4, "max_followups": 2}
RESEARCH_SCHEMA_VERSION = 16   # task planning stays in the card session; no separate plan-artifact link
DRIVER_TTL_SECONDS = 300
RESEARCH_TABLES = (
    "research_actions",
    "research_claims", "research_findings", "research_artifacts",
    "research_issues", "research_run_tasks", "research_branches", "research_runs",
)
ACTIVE_RESEARCH_TABLES = RESEARCH_TABLES


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
    """Make migration rollback independent of a caller's surrounding transaction."""
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
    """One read snapshot of the table, current marker and newest marker."""
    row = con.execute(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_runs'),"
        "EXISTS(SELECT 1 FROM schema_migrations WHERE component='research' AND version=?),"
        "(SELECT MAX(version) FROM schema_migrations WHERE component='research')",
        (RESEARCH_SCHEMA_VERSION,),
    ).fetchone()
    return bool(row[0]), bool(row[1]), row[2]


def _reject_newer(newest):
    if newest is not None and int(newest) > RESEARCH_SCHEMA_VERSION:
        raise RuntimeError(f"This board was written by a newer MISAKA (research schema v{newest}; this build knows "
                           f"v{RESEARCH_SCHEMA_VERSION}). Upgrade MISAKA rather than downgrading the data.")


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
                   for table in ACTIVE_RESEARCH_TABLES)
    finally:
        expected.close()


def _reject_interrupted_migration(con):
    """Do not hide tables an older migration renamed before it could rebuild them."""
    backups = []
    for table in ACTIVE_RESEARCH_TABLES:
        active = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        backup = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name GLOB ? LIMIT 1",
            (f"{table}_bak_*",),
        ).fetchone()
        if backup:
            backups.append(backup[0])
        if not active and backup:
            raise RuntimeError(
                f"research: interrupted schema migration left {table} in {backup[0]}; "
                "inspect or restore that backup before retrying"
            )
    if backups and _matches_current_schema(con):
        raise RuntimeError(
            "research: interrupted schema migration left current active tables beside "
            f"{backups[0]}; inspect the backup before retrying"
        )


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
    has_runs, current, newest = _schema_state(con)
    _reject_newer(newest)
    if has_runs and current:
        _validate_current_schema(con)
        return                               # the normal read path takes no SQLite write lock
    if current:
        raise RuntimeError("The research schema marker exists but the research_runs table is missing.")
    _reject_interrupted_migration(con)

    upgraded = None
    # DDL is transactional in SQLite, but ``executescript`` commits implicitly. Keep renames, new
    # tables, row copies, indexes, backfill and the version marker under one explicit transaction.
    with task_store.write_txn(con), _savepoint(con):
        # Another process may have migrated while this one waited for BEGIN IMMEDIATE.
        has_runs, current, newest = _schema_state(con)
        _reject_newer(newest)
        if has_runs and current:
            _validate_current_schema(con)
            return                           # current schema: nothing to migrate, nothing to replay
        if current:
            raise RuntimeError("The research schema marker exists but the research_runs table is missing.")
        _reject_interrupted_migration(con)
        if has_runs:
            # No in-place migration from older schemas. The prose is in files, but the workflow state
            # (phases, waves, driver leases, task links) lives only here, so the tables cannot be rebuilt
            # from the files: the old ones are renamed, not dropped, and carried forward column by column.
            suffix = f"_bak_{time.strftime('%Y%m%d%H%M%S')}"
            renamed = []
            for table in RESEARCH_TABLES:
                if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    continue
                for index in con.execute(f'PRAGMA index_list("{table}")').fetchall():
                    if index[3] == "c":   # named indexes stay global; free the names for the new tables
                        con.execute(f'DROP INDEX IF EXISTS "{index[1]}"')
                con.execute(f'ALTER TABLE "{table}" RENAME TO "{table}{suffix}"')
                renamed.append((table, f"{table}{suffix}"))
        else:
            renamed = []
        _execute_script(con, SCHEMA)
        _execute_script(con, INDEXES)
        if renamed:
            upgraded = (suffix, _carry_forward(con, renamed))
        con.execute("DROP TRIGGER IF EXISTS research_terminal_notification")  # obsolete and no longer consumed
        _backfill_dependencies(con)
        con.execute(
            "INSERT INTO schema_migrations(component,version,applied_at) VALUES(?,?,?)",
            ("research", RESEARCH_SCHEMA_VERSION, int(time.time())),
        )
    if upgraded:
        suffix, copied = upgraded
        print(f"research: schema upgraded to v{RESEARCH_SCHEMA_VERSION}; previous tables kept as *{suffix}, "
              f"rows carried forward: {copied}", file=sys.stderr)


def _carry_forward(con, renamed):
    """Copy every old row over the columns both schemas share.

    The surrounding schema transaction is deliberately aborted if a row violates the new schema:
    silently keeping only some rows would turn the version marker into a lie.
    """
    copied = {}
    for table, backup in renamed:
        new_cols = [row[1] for row in con.execute(f'PRAGMA table_info("{table}")')]
        old_cols = {row[1] for row in con.execute(f'PRAGMA table_info("{backup}")')}
        shared = [col for col in new_cols if col in old_cols]
        expected = con.execute(f'SELECT COUNT(*) FROM "{backup}"').fetchone()[0]
        if not shared:
            if new_cols and expected:
                raise RuntimeError(
                    f"research: {table}: no columns can carry {expected} old row(s) forward"
                )
            continue
        cols = ",".join(f'"{col}"' for col in shared)
        selected = [f'"{col}"' for col in shared]
        if table == "research_run_tasks" and {"local_id", "task_id"} <= old_cols:
            # Before v8 a node's probes could reuse local ids; the new unique index would drop the
            # later ones. Transform only the copy: a backup must remain an exact recovery source.
            selected[shared.index("local_id")] = (
                'CASE WHEN "local_id" IS NOT NULL AND rowid NOT IN '
                f'(SELECT MIN(rowid) FROM "{backup}" WHERE "local_id" IS NOT NULL '
                'GROUP BY "run_id","branch_id","local_id") '
                'THEN "local_id" || \'#\' || "task_id" ELSE "local_id" END'
            )
        source = ",".join(selected)
        copied[table] = con.execute(
            f'INSERT INTO "{table}" ({cols}) SELECT {source} FROM "{backup}"').rowcount
        if copied[table] != expected:
            raise RuntimeError(
                f"research: {table}: copied {copied[table]} of {expected} row(s) from {backup}"
            )
    return copied


def acquire_driver(con, run_id, lock, ttl_seconds=DRIVER_TTL_SECONDS):
    """Take the run's driver lease: one process advances a run at a time. False when another
    live lease holds it."""
    now = int(time.time())
    with task_store.write_txn(con):
        previous = get(con, run_id)
        cur = con.execute(
            "UPDATE research_runs SET driver_lock=?, driver_expires=? WHERE id=? "
            "AND (driver_lock IS NULL OR driver_expires IS NULL OR driver_expires < ? OR driver_lock=?)",
            (lock, now + int(ttl_seconds), run_id, now, lock),
        )
        if cur.rowcount != 1:
            return False
        if previous["driver_lock"] != lock:
            # Invalidate delayed child spawns in the same transaction as takeover.
            # Keep PID/identity for the successor's existing orphan-reaping path.
            con.execute("UPDATE research_branches SET runner_key=NULL WHERE run_id=?", (run_id,))
    return True


def heartbeat_driver(con, run_id, lock, ttl_seconds=DRIVER_TTL_SECONDS):
    cur = con.execute(
        "UPDATE research_runs SET driver_expires=? WHERE id=? AND driver_lock=?",
        (int(time.time()) + int(ttl_seconds), run_id, lock),
    )
    return cur.rowcount == 1


def release_driver(con, run_id, lock):
    con.execute("UPDATE research_runs SET driver_lock=NULL, driver_expires=NULL WHERE id=? AND driver_lock=?",
                (run_id, lock))


# Cards past these statuses keep their dependency history as it is: the DAG must not be rewritten
# under a moving card, and a finished one (whose execution already finished) has
# nothing left to wait for. Read by ``_backfill_dependencies`` and by ``workflow._submit_tasks``,
# the two places that replay a plan's edges onto cards that may already have run.
SETTLED_TASK_STATUSES = frozenset({"running", "review", "done", "failed", "stopped", "archived"})


def _backfill_dependencies(con):
    """Replay the stored local-id dependencies into the generic task DAG.

    Idempotent where nothing is wrong: edges that already exist are skipped, settled cards are
    left alone, and a card whose project folder no longer exists cannot make the
    replay fail. An edge that a *live* card refuses is the one thing it will not swallow -- see
    the raise below."""
    rows = con.execute(
        "SELECT task_id,run_id,branch_id,local_id,depends_json FROM research_run_tasks"
    ).fetchall()
    by_scope = {(row["run_id"], row["branch_id"], row["local_id"]): row["task_id"] for row in rows}
    for row in rows:
        try:
            dependencies = json.loads(row["depends_json"] or "[]")
        except ValueError:
            continue
        parents = [by_scope.get((row["run_id"], row["branch_id"], dep)) for dep in dependencies]
        if any(parent is None for parent in parents):
            continue
        task = task_store.get(con, row["task_id"])
        if task is None or task["status"] in SETTLED_TASK_STATUSES:
            continue
        from misaka.core.platform import cards as card_files
        if not os.path.isfile(card_files.card_path(task["workspace"], row["task_id"])):
            continue        # the card's file is gone: its `needs` cannot be read, so nothing moves
        try:
            task_store.link_dependencies(con, parents, row["task_id"])
        except (ValueError, OSError) as error:
            raise RuntimeError(
                f"Research dependency could not be replayed onto {row['task_id']}: {error}"
            ) from error
        if dependencies:
            task_store.promote_task(con, row["task_id"])


# Where a run's files go inside the project. "project-flat" (2026-09-05 .. 09-11) wrote every
# root artifact as ``root/<run>-<name>`` and every fork artifact as ``forks/<node>-<name>``, with
# each card's output folder beside them. "by-node" gives every node one folder -- its files, its
# cards, and (bundle) the sources they cite -- and puts run-level products under ``final/``.
# Old runs keep their layout; only new runs get the current one.
ARTIFACT_LAYOUT = "by-node"
LAYOUTS = frozenset({"project-flat", ARTIFACT_LAYOUT})


def _by_node(run):
    return run["artifact_layout"] == ARTIFACT_LAYOUT


def require_layout(run):
    if run["artifact_layout"] not in LAYOUTS:
        raise ValueError("This run used the removed worktree layout; start a new run. "
                         "Its artifacts and sessions are preserved.")


def node_dir(run, branch_id=None):
    """The folder a node owns under the by-node layout: ``nodes/<branch id>``, the root's named
    by the run id (it has no branch id of its own in artifact scopes)."""
    return os.path.join("nodes", branch_id or run["id"])


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
    parallel = raw.get("parallel", DEFAULT_LIMITS["parallel"])
    if isinstance(parallel, bool) or not isinstance(parallel, (int, str)):
        raise ValueError("parallel must be a positive integer")  # noqa: TRY004 - startup callers surface ValueError
    try:
        parallel = int(parallel)
    except ValueError as error:
        raise ValueError("parallel must be a positive integer") from error
    if parallel < 1:
        raise ValueError("parallel must be a positive integer")
    # How many more times a node may send its Sisters out after its first cards are back,
    # before it must conclude. Conversation with the user is never counted.
    followups = raw.get("max_followups", DEFAULT_LIMITS["max_followups"])
    if isinstance(followups, bool) or not isinstance(followups, (int, str)):
        raise ValueError("max_followups must be an integer between 0 and 6")  # noqa: TRY004 - startup callers surface ValueError
    try:
        followups = int(followups)
    except ValueError as error:
        raise ValueError("max_followups must be an integer between 0 and 6") from error
    if not 0 <= followups <= 6:
        raise ValueError("max_followups must be an integer between 0 and 6")
    return {"max_depth": depth, "parallel": parallel, "max_followups": followups}


def prepare_runner(con, table, row_id):
    key = secrets.token_hex(16)
    changed = con.execute(f'UPDATE "{table}" SET runner_key=?, last_error=NULL WHERE id=? '
                          'AND runner_pid IS NULL AND runner_identity IS NULL', (key, row_id)).rowcount
    if changed != 1:
        raise RuntimeError(f"Research execution {row_id} still has a runner")
    return key


def claim_runner(con, table, row_id, key):
    """Fence delayed/duplicate spawns, including a lost pane.create reply."""
    from misaka.core.platform import processes
    if not key:
        return False
    pid = os.getpid()
    identity = processes.identity(pid)
    if identity is None:
        raise RuntimeError("Research runner process identity is unreadable")
    return con.execute(f'UPDATE "{table}" SET runner_pid=?, runner_identity=? WHERE id=? AND runner_key=? '
                       'AND (runner_pid IS NULL OR (runner_pid=? AND runner_identity=?))',
                       (pid, identity, row_id, key, pid, identity)).rowcount == 1


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
    return {row["task_id"]: dict(row) for row in con.execute(
        "SELECT t.task_id,t.run_id,t.branch_id AS node_id,b.depth,t.issue_id,t.kind,"
        "r.workspace,r.origin_session,b.trigger_text AS node_question,i.question AS issue_question "
        "FROM research_run_tasks t JOIN research_runs r ON r.id=t.run_id "
        "JOIN research_branches b ON b.id=t.branch_id LEFT JOIN research_issues i ON i.id=t.issue_id")}


def project_name(run):
    """Display name of the run's project: the workspace folder's basename."""
    return os.path.basename(run["workspace"].rstrip(os.sep)) or run["workspace"]


def ensure_layout(run):
    root = Path(run["workspace"])
    for rel in (("nodes", "final") if _by_node(run) else ("root", "forks")):
        (root / rel).resolve().relative_to(root.resolve())
        (root / rel).mkdir(parents=True, exist_ok=True)
    return str(root)


def _atomic_write(path, content):
    atomic.write_text(path, content)


def _commit(con, run, message):
    """Commit project-local artifacts; Git is history, not a delivery mechanism.

    Research cards are not committed one by one at acceptance, so the node and run commits carry
    each card's contract (``cards/<id>.md``) and attachment folder along with the artifacts."""
    from misaka.core.platform import repo
    paths = [os.path.relpath(a["path"], run["workspace"]) for a in artifacts(con, run["id"])]
    for row in tasks(con, run["id"]):
        paths += [os.path.join("cards", f"{row['id']}.md"), os.path.join("cards", str(row["id"]))]
    repo.commit(run["workspace"], [*paths, "PROJECT.md"], message)


def _artifact_name(name):
    if not name or Path(name).name != name or name in {".", ".."} or "\\" in name:
        raise ValueError("Generated research artifacts need a filename, not a directory path.")
    return name


def generated_path(run, name, *, branch_id=None):
    """A node's artifact, relative to the project: ``nodes/<node>/<name>`` under the by-node
    layout; ``root/<run>-<name>`` or ``forks/<node>-<name>`` under project-flat."""
    name = _artifact_name(name)
    if _by_node(run):
        return os.path.join(node_dir(run, branch_id), name)
    return os.path.join("forks" if branch_id else "root", f"{branch_id or run['id']}-{name}")


def run_path(run, name):
    """A run-level product (question, clarifications, survey, draft, final, partial, workspace
    index): ``final/<run>-<name>`` under the by-node layout, so the run's deliverables and the
    sources they cite sit in one folder; project-flat keeps them beside the root artifacts."""
    name = _artifact_name(name)
    if _by_node(run):
        return os.path.join("final", f"{run['id']}-{name}")
    return generated_path(run, name)


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
            "(id,workspace,artifact_layout,question,phase,status,limits_json,token_start,origin_session,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, workspace, ARTIFACT_LAYOUT, question, "created", "active",
             json.dumps(spec, ensure_ascii=False), int(token_start), origin_session, now, now),
        )
        row = get(con, run_id)
        ensure_layout(row)
        write_text(con, run_id, "question", 'Original research question', run_path(row, "question.md"),
                   f"""# Original research question

{question}
""")
        create_node(con, run_id, trigger=question, parent_id=None, depth=0)
    return get(con, run_id)


def get(con, run_id):
    init(con)
    return con.execute("SELECT * FROM research_runs WHERE id=?", (run_id,)).fetchone()


def latest(con, workspace=None, active_only=False):
    init(con)
    q, args = "SELECT * FROM research_runs WHERE 1=1", []
    if workspace:
        q += " AND workspace=?"
        args.append(workspace)
    if active_only:
        q += " AND status IN ('active','waiting_input','stopping')"
    return con.execute(q + " ORDER BY created_at DESC LIMIT 1", args).fetchone()


def listing(con, *, workspace=None):
    init(con)
    if workspace:
        return con.execute(
            "SELECT * FROM research_runs WHERE workspace=? ORDER BY created_at DESC", (workspace,)
        ).fetchall()
    return con.execute("SELECT * FROM research_runs ORDER BY created_at DESC").fetchall()


def limits(run):
    try:
        return normalize_limits(json.loads(run["limits_json"]))
    except (TypeError, ValueError):
        return dict(DEFAULT_LIMITS)


def set_state(con, run_id, *, phase=None, status=None, error=None,
              root_session=None, final_artifact=None, wave=None, driver_lock=None):
    """Update run state. With ``driver_lock`` the write lands only while that lease is held:
    a driver that lost its lease cannot fail or finish a run its successor now owns."""
    values, fields = [], []
    for name, value in (("phase", phase), ("status", status), ("last_error", error),
                        ("root_session", root_session), ("final_artifact", final_artifact)):
        if value is not None:
            fields.append(f"{name}=?")
            values.append(value)
    if wave is not None:
        fields.append("wave=?")
        values.append(int(wave))
    fields.append("updated_at=?")
    values.extend([int(time.time()), run_id])
    where = "id=?"
    if driver_lock is not None:
        where += " AND driver_lock=?"
        values.append(driver_lock)
    cur = con.execute(f"UPDATE research_runs SET {','.join(fields)} WHERE {where}", values)
    if driver_lock is not None and cur.rowcount != 1:
        raise RuntimeError(f"Research run {run_id}: driver lease lost.")
    # No commit per phase: Git records a node when it reaches a terminal state (set_node) and
    # the run when it ends (workflow), not every step in between.
    return get(con, run_id)


def request_stop(con, run_id):
    con.execute(
        "UPDATE research_runs SET stop_requested=1,status='stopping',updated_at=? "
        "WHERE id=? AND status IN ('active','waiting_input','stopping')",
        (int(time.time()), run_id),
    )


def resume(con, run_id, *, driver_lock=None, clarification=""):
    with task_store.write_txn(con):
        return _resume(con, run_id, driver_lock=driver_lock, clarification=clarification)


def _resume(con, run_id, *, driver_lock, clarification):
    """Reopen a stopped, failed, or waiting run: same card generations, nodes pick up where they were.
    A finished run is not resumable: its report is final; start a new run."""
    current = get(con, run_id)
    if current is None:
        raise ValueError(f"Research run not found: {run_id}")
    require_layout(current)
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
    # A failed card is retried like a stopped one: without this the node it killed replays the
    # identical failure on every resume, and the only way out is editing this database by hand.
    for row in tasks(con, run_id):
        unusable = row["status"] == "done" and task_store.latest_payload(
            con, row["id"], "research_review_missing", generation=row["generation"])
        if row["status"] not in ("stopped", "failed") and not unusable:
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
    # A failed node replans: every phase of ``_expand`` is idempotent over what it already
    # produced (saved plans, cards and project-local artifacts), so it picks up its own work.
    con.execute(
        "UPDATE research_branches SET status='planning',updated_at=? "
        "WHERE run_id=? AND status IN ('waiting_input','awaiting_approval','failed')", (int(time.time()), run_id),
    )
    con.execute(
        "UPDATE research_runs SET stop_requested=0,status='active',phase='active',last_error='',"
        "updated_at=? WHERE id=?", (int(time.time()), run_id),
    )
    return get(con, run_id)


def stop_requested(con, run_id):
    row = get(con, run_id)
    return not row or bool(row["stop_requested"])


def task_count(con, run_id):
    return con.execute(
        "SELECT COUNT(*) FROM research_run_tasks WHERE run_id=?", (run_id,)
    ).fetchone()[0]


def link_task(con, run_id, task_id, *, kind, node,
              local_id=None, issue_id=None, dependencies=(), round=1):
    """Attach a card to a node; one project-local output directory protects concurrent cards.
    ``round`` is the planning round the card belongs to (a node may plan more than once)."""
    run = get(con, run_id)
    if not run:
        raise ValueError(f"Research run not found: {run_id}")
    task = task_store.get(con, task_id)
    if task is None or os.path.realpath(task["workspace"]) != run["workspace"]:
        raise ValueError("Research cards must execute in the run's project folder.")
    if _by_node(run):
        # The card lives inside its node's folder: nodes/<node>/cards/<card>.
        output_dir = Path(run["workspace"], node_dir(run, node["id"] if node["parent_id"] else None), "cards", task_id)
    else:
        output_dir = Path(run["workspace"], "forks" if node["parent_id"] else "root", task_id)
    output_dir.resolve().relative_to(Path(run["workspace"]).resolve())
    if issue_id is None:
        origin = con.execute("SELECT id FROM research_issues WHERE child_branch_id=?", (node["id"],)).fetchone()
        issue_id = origin["id"] if origin else None
    dependencies = list(dependencies)
    con.execute(
        "INSERT INTO research_run_tasks "
        "(task_id,run_id,branch_id,kind,wave,local_id,issue_id,depends_json,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (task_id, run_id, node["id"], kind, int(round), local_id, issue_id,
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
                "SELECT task_id FROM research_run_tasks WHERE run_id=? AND branch_id=? AND local_id=?",
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


def tasks(con, run_id, *, kind=None, node_id=None, issue_id=None, round=None):
    q, args = (
        ("SELECT t.*,rt.branch_id,rt.kind AS research_kind,rt.wave,"
        "rt.local_id,rt.issue_id,rt.depends_json "
        "FROM research_run_tasks rt JOIN tasks t ON t.id=rt.task_id WHERE rt.run_id=?"),
        [run_id],
    )
    if kind:
        q += " AND rt.kind=?"
        args.append(kind)
    if node_id is not None:
        q += " AND rt.branch_id=?"
        args.append(node_id)
    if issue_id is not None:
        q += " AND rt.issue_id=?"
        args.append(issue_id)
    if round is not None:
        q += " AND rt.wave=?"
        args.append(int(round))
    return con.execute(q + " ORDER BY rt.created_at", args).fetchall()


# --- nodes: the research tree ---------------------------------------------------------

def create_node(con, run_id, *, trigger, parent_id, depth):
    run = get(con, run_id)
    if not run:
        raise ValueError(f"Research run not found: {run_id}")
    if int(depth) > limits(run)["max_depth"]:
        raise RuntimeError("The research run has reached its depth limit.")
    bid = "b_" + secrets.token_hex(5)
    now = int(time.time())
    con.execute(
        "INSERT INTO research_branches "
        "(id,run_id,parent_id,trigger_text,depth,status,created_at,updated_at) "
        "VALUES (?,?,?,?,?,'queued',?,?)",
        (bid, run_id, parent_id, str(trigger), int(depth), now, now),
    )
    return node(con, bid)


def assign_child(con, run_id, parent, issue_id):
    """One accepted issue assignment creates exactly one depth+1 node, including on replay."""
    with task_store.write_txn(con):
        item = issue(con, issue_id)
        if item is None or item["run_id"] != run_id or item["branch_id"] != parent["id"]:
            raise ValueError("Child assignment must reference this node's material issue.")
        if item["child_branch_id"]:
            return node(con, item["child_branch_id"])
        child = create_node(con, run_id, trigger=item["question"], parent_id=parent["id"], depth=parent["depth"] + 1)
        set_issue(con, issue_id, "assigned", child_branch_id=child["id"])
        return child


def node(con, node_id):
    return con.execute("SELECT * FROM research_branches WHERE id=?", (node_id,)).fetchone()


def nodes(con, run_id, *, parent_id=None):
    q, args = "SELECT * FROM research_branches WHERE run_id=?", [run_id]
    if parent_id is not None:
        q += " AND parent_id=?"
        args.append(parent_id)
    return con.execute(q + " ORDER BY depth,created_at", args).fetchall()


def next_level(con, run_id):
    """The BFS frontier: every node still expanding at the shallowest such depth, oldest first.
    Closing nodes wait for their children, not another model process."""
    rows = [n for n in nodes(con, run_id) if n["status"] not in ("closing", *NODE_TERMINAL)]
    return [n for n in rows if n["depth"] == rows[0]["depth"]] if rows else []


def set_node(con, node_id, *, status=None, session_file=None, context_artifact=None, owner=None):
    fields, values = [], []
    for name, value in (("status", status), ("session_file", session_file),
                        ("context_artifact", context_artifact)):
        if value is not None:
            fields.append(f"{name}=?")
            values.append(value)
    fields.append("updated_at=?")
    values.extend([int(time.time()), node_id])
    with task_store.write_txn(con):
        if owner is not None:
            check_owner(con, *owner)
        con.execute(f"UPDATE research_branches SET {','.join(fields)} WHERE id=?", values)
        if status is not None:
            # Lifecycle only, never a scientific verdict.
            issue_status = "researched" if status == "closed" else "parked" if status in NODE_TERMINAL else "assigned"
            con.execute("UPDATE research_issues SET status=? WHERE child_branch_id=?", (issue_status, node_id))
    row = node(con, node_id)
    if status in NODE_TERMINAL:
        # One commit per node, once its work is over and the row already says so: the history
        # records what the node delivered, not each phase it passed through.
        _commit(con, get(con, row["run_id"]), f"research {row['run_id']}/{node_id}: {status}")
    return row


def add_issue(con, run_id, *, node, kind, question, rationale, priority=0):
    question = str(question or "").strip()
    if not question:
        return None
    # Only exact matches after case/whitespace normalization, and only inside the same node, count
    # as duplicates: another node raising the same question keeps its own issue so its Last Order sees
    # it. Semantic merging is Last Order's job.
    normalized = " ".join(question.casefold().split())
    for row in con.execute("SELECT id,question FROM research_issues WHERE run_id=? AND branch_id=?",
                           (run_id, node["id"])):
        if " ".join(row["question"].casefold().split()) == normalized:
            return row["id"]
    iid = "i_" + secrets.token_hex(5)
    con.execute(
        "INSERT INTO research_issues "
        "(id,run_id,branch_id,wave,kind,question,rationale,priority,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (iid, run_id, node["id"], int(node["depth"]), str(kind or "unclassified"), question,
         str(rationale or ""), int(priority or 0), int(time.time())),
    )
    return iid


def issues(con, run_id, *, node_id=None, status=None):
    q, args = "SELECT * FROM research_issues WHERE run_id=?", [run_id]
    if node_id is not None:
        q += " AND branch_id=?"
        args.append(node_id)
    if status is not None:
        q += " AND status=?"
        args.append(status)
    return con.execute(q + " ORDER BY priority DESC,created_at", args).fetchall()


def issue(con, issue_id):
    return con.execute("SELECT * FROM research_issues WHERE id=?", (issue_id,)).fetchone()


def action(con, run_id, node_id, key):
    row = con.execute(
        "SELECT * FROM research_actions WHERE run_id=? AND branch_id=? AND action_key=?",
        (run_id, node_id, key),
    ).fetchone()
    return {**dict(row), "payload": json.loads(row["payload_json"])} if row else None


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
    if (current["stop_requested"] or current["status"] != "active"
            or owner["status"] in (*NODE_TERMINAL, "closing")):
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
        con.execute(
            "INSERT INTO research_actions VALUES (?,?,?,?,?,?,?)",
            (run["id"], branch["id"], key, json.dumps(payload, ensure_ascii=False),
             session_file, tool_call_id, int(time.time())),
        )
    return action(con, run["id"], branch["id"], key)


def replace_action(con, run, branch, key, payload, *, session_file, tool_call_id):
    """Accept a command that supersedes the earlier one of its key: a plan revised while it waits
    for the user, or a fresh start after such a revision. Same ownership rule as record_action."""
    with task_store.write_txn(con):
        _owned(con, run, branch)
        con.execute("DELETE FROM research_actions WHERE run_id=? AND branch_id=? AND action_key=?",
                    (run["id"], branch["id"], key))
        con.execute(
            "INSERT INTO research_actions VALUES (?,?,?,?,?,?,?)",
            (run["id"], branch["id"], key, json.dumps(payload, ensure_ascii=False),
             session_file, tool_call_id, int(time.time())),
        )
    return action(con, run["id"], branch["id"], key)


SKIP_KEY = "skip"


def skipped(con, run_id, node_id):
    """The user's decision, recorded by the node's Last Order, to close this node unresearched."""
    return action(con, run_id, node_id, SKIP_KEY)


def delete_action(con, run_id, node_id, key):
    con.execute("DELETE FROM research_actions WHERE run_id=? AND branch_id=? AND action_key=?",
                (run_id, node_id, key))


def plan_key(round):
    """The action key of a round's plan: the first round keeps the bare ``plan`` (older runs)."""
    return "plan" if int(round) <= 1 else f"plan:{int(round)}"


def start_key(round):
    return "start" if int(round) <= 1 else f"start:{int(round)}"


def plan_round(con, run_id, node_id):
    """The highest planning round this node has recorded a plan for; 0 before the first."""
    keys = [row[0] for row in con.execute(
        "SELECT action_key FROM research_actions WHERE run_id=? AND branch_id=? "
        "AND (action_key='plan' OR action_key LIKE 'plan:%')", (run_id, node_id))]
    rounds = [1 if key == "plan" else int(key.split(":", 1)[1]) for key in keys]
    return max(rounds) if rounds else 0


def current_plan(con, run_id, node_id):
    """``(round, plan action)`` for the node's latest round; ``(0, None)`` before the first."""
    round = plan_round(con, run_id, node_id)
    return (round, action(con, run_id, node_id, plan_key(round))) if round else (0, None)


def plan_started(con, run_id, node_id):
    """True once Last Order has recorded the user's go-ahead for the plan as it stands now: a
    start names the plan it was given for, so a revision after it needs a new start, and each
    round has its own start."""
    round, plan = current_plan(con, run_id, node_id)
    start = action(con, run_id, node_id, start_key(round)) if round else None
    return bool(plan and start and start["payload"].get("plan_tool_call_id") == plan["tool_call_id"])


def reframe(con, run_id, node, question):
    """The user agreed to Last Order's reframed question: the root's is the run's question (its
    root node's trigger too); a fork's is the issue that node investigates."""
    question = " ".join(str(question or "").split())
    if not question:
        return
    with task_store.write_txn(con):
        con.execute("UPDATE research_branches SET trigger_text=?,updated_at=? WHERE id=?",
                    (question, int(time.time()), node["id"]))
        if node["parent_id"] is None:
            con.execute("UPDATE research_runs SET question=?,updated_at=? WHERE id=?",
                        (question, int(time.time()), run_id))


def set_issue(con, issue_id, status, *, child_branch_id=None, reason=None):
    con.execute(
        "UPDATE research_issues SET status=?,child_branch_id=COALESCE(?,child_branch_id),"
        "reason=COALESCE(?,reason) WHERE id=?",
        (status, child_branch_id, reason, issue_id),
    )


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


def artifact_path(row):
    return row["path"]


def artifacts(con, run_id, *, branch_id=None, kind=None, task_id=None, root_only=False):
    q, args = "SELECT * FROM research_artifacts WHERE run_id=?", [run_id]
    if branch_id is not None:
        q += " AND branch_id=?"
        args.append(branch_id)
    elif root_only:
        q += " AND branch_id IS NULL"
    if kind:
        q += " AND kind=?"
        args.append(kind)
    if task_id:
        q += " AND task_id=?"
        args.append(task_id)
    return con.execute(q + " ORDER BY created_at", args).fetchall()


def artifact(con, artifact_id):
    return con.execute("SELECT * FROM research_artifacts WHERE id=?", (artifact_id,)).fetchone()


def recover_publications(con, run):
    """Resume-only recovery; ordinary artifact/viewer reads never mutate the workspace."""
    from misaka.core.research import publication

    root = Path(run["workspace"])
    paths = {Path(row["path"]) for row in artifacts(con, run["id"])}
    # Include an interrupted first write whose registration rolled back completely.
    for branch in nodes(con, run["id"]):
        sample = root / generated_path(run, "checkpoint", branch_id=branch["id"] if branch["parent_id"] else None)
        prefix = "" if _by_node(run) else f"{branch['id'] if branch['parent_id'] else run['id']}-"
        for journal in sample.parent.glob(f".{prefix}*.research-publish.json"):
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
        "open_issues": len(issues(con, run_id, status="open")),
        "stop_requested": bool(run["stop_requested"]),
        "final_artifact": run["final_artifact"], "last_error": run["last_error"],
    }

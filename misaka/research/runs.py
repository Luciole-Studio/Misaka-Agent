"""Persistent research-run state and file artifacts.

A run is a tree of nodes (``research_branches``): the root is the first conclusion, every
other node re-researches an issue that undermined its parent's conclusion. Research prose
lives under ``<workspace>/research/<run_id>``; node worktrees and session transcripts live
under ``~/.misaka/runs/<run_id>``. SQLite stores only workflow state and relationships, so a
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

from misaka.platform import tasks as task_store
from misaka.utils import atomic

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
  worktree        TEXT,
  session_file    TEXT,
  context_artifact TEXT,
  runner_pid      INTEGER,
  runner_identity TEXT,
  created_at      INTEGER NOT NULL,
  updated_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS research_run_tasks (
  task_id         TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL,
  branch_id       TEXT NOT NULL,
  kind            TEXT NOT NULL,
  wave            INTEGER NOT NULL DEFAULT 0,
  preflight_artifact TEXT,
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
  runner_pid      INTEGER,
  runner_identity TEXT,
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
  created_at      INTEGER NOT NULL,
  UNIQUE(run_id,path)
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
CREATE INDEX IF NOT EXISTS research_findings_run ON research_findings(run_id,branch_id,task_id);
CREATE INDEX IF NOT EXISTS research_claims_finding ON research_claims(finding_id);
CREATE UNIQUE INDEX IF NOT EXISTS research_tasks_local ON research_run_tasks(run_id,branch_id,local_id) WHERE local_id IS NOT NULL;
"""

ACTIVE = ("active", "waiting_input", "stopping")
NODE_TERMINAL = ("closed", "failed", "parked")
# closing = triaged, waiting for its children; conflict = its branch did not merge, a human resolves it
DEFAULT_LIMITS = {"max_depth": 3}
RESEARCH_SCHEMA_VERSION = 10   # 10: node/probe runner identity on the row, visible to a successor driver
DRIVER_TTL_SECONDS = 300
RESEARCH_TABLES = (
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


def init(con):
    has_runs, current, newest = _schema_state(con)
    _reject_newer(newest)
    if has_runs and current:
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
    cur = con.execute(
        "UPDATE research_runs SET driver_lock=?, driver_expires=? WHERE id=? "
        "AND (driver_lock IS NULL OR driver_expires IS NULL OR driver_expires < ? OR driver_lock=?)",
        (lock, now + int(ttl_seconds), run_id, now, lock),
    )
    return cur.rowcount == 1


def heartbeat_driver(con, run_id, lock, ttl_seconds=DRIVER_TTL_SECONDS):
    cur = con.execute(
        "UPDATE research_runs SET driver_expires=? WHERE id=? AND driver_lock=?",
        (int(time.time()) + int(ttl_seconds), run_id, lock),
    )
    return cur.rowcount == 1


def release_driver(con, run_id, lock):
    con.execute("UPDATE research_runs SET driver_lock=NULL, driver_expires=NULL WHERE id=? AND driver_lock=?",
                (run_id, lock))


def relocate_node_tasks(con, node, old_root, new_root):
    """A closed node's line merged into its parent's: its cards now live there, so their
    ``workspace`` / ``output_dir`` follow (the worktree they pointed at is gone)."""
    old_root, new_root = os.path.realpath(old_root), os.path.realpath(new_root)
    moved = 0
    for row in tasks(con, node["run_id"], node_id=node["id"]):
        workspace, output_dir = row["workspace"], row["output_dir"]
        new_ws = new_root if workspace and os.path.realpath(workspace) == old_root else workspace
        new_out = output_dir
        if output_dir and (os.path.realpath(output_dir) == old_root
                           or os.path.realpath(output_dir).startswith(old_root + os.sep)):
            new_out = new_root + os.path.realpath(output_dir)[len(old_root):]
        if (new_ws, new_out) != (workspace, output_dir):
            con.execute("UPDATE tasks SET workspace=?, output_dir=? WHERE id=?", (new_ws, new_out, row["id"]))
            moved += 1
    # Generated and Sister artifacts record the file on the node's current line in
    # metadata while ``path`` remains their eventual project path.  When a worktree
    # is merged and removed, move that source pointer with it or descendants cannot
    # read the evidence until every ancestor has reached the main line.
    for artifact in con.execute(
        "SELECT id,metadata_json FROM research_artifacts WHERE run_id=?", (node["run_id"],)
    ):
        try:
            metadata = json.loads(artifact["metadata_json"] or "{}")
        except ValueError:
            continue
        source = metadata.get("source_workspace")
        if not source:
            continue
        source = os.path.realpath(source)
        if source != old_root and not source.startswith(old_root + os.sep):
            continue
        metadata["source_workspace"] = new_root + source[len(old_root):]
        con.execute(
            "UPDATE research_artifacts SET metadata_json=? WHERE id=?",
            (json.dumps(metadata, ensure_ascii=False), artifact["id"]),
        )
    return moved


# Cards past these statuses keep their dependency history as it is: the DAG must not be rewritten
# under a moving card, and a finished one (whose node line may already be merged and gone) has
# nothing left to wait for. Read by ``_backfill_dependencies`` and by ``workflow._submit_tasks``,
# the two places that replay a plan's edges onto cards that may already have run.
SETTLED_TASK_STATUSES = frozenset({"running", "review", "done", "failed", "stopped", "archived"})


def _backfill_dependencies(con):
    """Replay the stored local-id dependencies into the generic task DAG.

    Idempotent where nothing is wrong: edges that already exist are skipped, settled cards are
    left alone, and a card whose line no longer exists (a closed node's worktree) cannot make the
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
        from misaka.platform import cards as card_files
        if not os.path.isfile(card_files.card_path(task["workspace"], row["task_id"])):
            continue        # the card's line is gone (closed node): its `needs` cannot be read, so nothing moves
        existing = set(task_store.parent_ids(con, row["task_id"]))
        for parent_id in parents:
            if parent_id in existing:
                continue
            try:
                task_store.link_tasks(con, parent_id, row["task_id"])
            except (ValueError, OSError) as error:
                # Everything that is merely history was skipped above -- a settled card, a card
                # whose line is gone, an edge the file already carries. What is left here is a
                # fault: an unreadable ancestor (so the acyclicity walk could not run), a cycle, a
                # plan that names itself, or a file that would not take the write. The child's
                # ``needs`` does not name this parent either way, and there is no status that can
                # hold it back -- nothing in the board writes or honours a ``held`` card, and
                # ``cards.reconcile`` writes the card file's own ``status`` back over any row we
                # set here on the very next tick. So the replay refuses to finish quietly: it says
                # which edge it could not write and why, and ``depends_json`` keeps the plan for
                # the next attempt once the card that blocked the walk is fixed.
                raise RuntimeError(
                    f"Research dependency could not be replayed: {parent_id} -> {row['task_id']} "
                    f"({error}) Fix that card, then run init again; until the edge exists the "
                    "run's cards would execute out of order."
                ) from error
        if dependencies:
            task_store.promote_task(con, row["task_id"])


def normalize_limits(raw=None):
    try:
        depth = int((raw or {}).get("max_depth", DEFAULT_LIMITS["max_depth"]))
    except (TypeError, ValueError) as error:
        raise ValueError("max_depth must be an integer") from error
    if not 0 <= depth <= 12:
        raise ValueError("max_depth must be between 0 and 12")
    return {"max_depth": depth}


def run_dir(run):
    return os.path.join(run["workspace"], "research", run["id"])


def _home(run):
    return os.path.join(os.environ.get("MISAKA_RUNS_HOME") or os.path.expanduser("~/.misaka/runs"),
                        run["id"])


def session_dir(run, *parts):
    """Transcript directory for one of the run's one-shot model calls (kept out of the project folder)."""
    return os.path.join(_home(run), "sessions", *parts)


def project_name(run):
    """Display name of the run's project: the workspace folder's basename."""
    return os.path.basename(run["workspace"].rstrip(os.sep)) or run["workspace"]


def ensure_layout(run):
    root = Path(run_dir(run))
    for rel in ("branches", "tasks"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    return str(root)


def _atomic_write(path, content):
    atomic.write_text(path, content)


def _commit(run, node, message):
    """Record the run's state on git: the project line always, the node's line when it has one."""
    from misaka.platform import repo
    # ponytail: only this run's prose and the project brief; cards commit themselves per transition.
    paths = [os.path.join("research", run["id"]), "PROJECT.md"]
    repo.commit(run["workspace"], paths, message)
    if node is not None and node["worktree"]:
        repo.commit(node["worktree"], paths, message)


def create(con, *, workspace, question, limits=None, token_start=0):
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
            "(id,workspace,question,phase,status,limits_json,token_start,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, workspace, question, "created", "active",
             json.dumps(spec, ensure_ascii=False), int(token_start), now, now),
        )
        row = get(con, run_id)
        ensure_layout(row)
        write_text(con, run_id, "question", 'Original research question', "question.md",
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
    run = get(con, run_id)
    if phase is not None and cur.rowcount == 1:
        _commit(run, None, f"research {run_id}: {phase}")
    return run


def request_stop(con, run_id):
    con.execute(
        "UPDATE research_runs SET stop_requested=1,status='stopping',updated_at=? "
        "WHERE id=? AND status IN ('active','waiting_input','stopping')",
        (int(time.time()), run_id),
    )


def resume(con, run_id):
    """Reopen a stopped, failed, or waiting run: same card generations, nodes pick up where they were.
    A finished run is not resumable: its report is final; start a new run."""
    current = get(con, run_id)
    if current is None:
        raise ValueError(f"Research run not found: {run_id}")
    if current["status"] == "done":
        raise ValueError(f"Research run {run_id} is done; start a new run instead of resuming it.")
    # A failed card is retried like a stopped one: without this the node it killed replays the
    # identical failure on every resume, and the only way out is editing this database by hand.
    for row in tasks(con, run_id):
        if row["status"] not in ("stopped", "failed"):
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
    # produced (a saved plan is reused, cards are deduped by their link, and the node's worktree
    # was deliberately kept), so it picks up its own work. ``conflict`` is not cleared here: it
    # waits for a human to resolve the branch.
    con.execute(
        "UPDATE research_branches SET status='planning',updated_at=? "
        "WHERE run_id=? AND status IN ('waiting_input','failed')", (int(time.time()), run_id),
    )
    con.execute(
        "UPDATE research_runs SET stop_requested=0,status='active',phase='active',last_error='',"
        "updated_at=? WHERE id=?", (int(time.time()), run_id),
    )
    return get(con, run_id)


def stop_requested(con, run_id):
    row = get(con, run_id)
    return not row or bool(row["stop_requested"])


def call_timeout(cfg, default):
    """Per-call failure timeout in seconds (``cfg["call_timeout"]`` overrides ``default``); this is not a research stopping criterion."""
    try:
        value = int((cfg or {}).get("call_timeout") or default)
    except (TypeError, ValueError, AttributeError):
        value = int(default)
    return max(1, value)


def task_count(con, run_id):
    return con.execute(
        "SELECT COUNT(*) FROM research_run_tasks WHERE run_id=?", (run_id,)
    ).fetchone()[0]


def link_task(con, run_id, task_id, *, kind, node, preflight_artifact=None,
              local_id=None, issue_id=None, dependencies=()):
    """Attach a card to a node. The card's workspace is the node's line (set by the card's creation);
    its output_dir lives under that line's copy of the run directory."""
    run = get(con, run_id)
    if not run:
        raise ValueError(f"Research run not found: {run_id}")
    dependencies = list(dependencies)
    con.execute(
        "INSERT INTO research_run_tasks "
        "(task_id,run_id,branch_id,kind,wave,preflight_artifact,local_id,issue_id,depends_json,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (task_id, run_id, node["id"], kind, int(node["depth"]), preflight_artifact, local_id, issue_id,
         json.dumps(dependencies, ensure_ascii=False), int(time.time())),
    )
    output_dir = Path(os.path.realpath(node_root(run, node)), "research", run_id, "tasks", task_id, "work")
    output_dir.mkdir(parents=True, exist_ok=True)
    con.execute("UPDATE tasks SET output_dir=? WHERE id=?", (str(output_dir), task_id))
    # ``cards.create(after_row=...)`` calls this before the child card file exists;
    # the submitter projects the dependencies after creation.  Direct callers link
    # already-created cards here as before.
    task = task_store.get(con, task_id)
    if task is not None:
        from misaka.platform import cards as card_files
        card_exists = os.path.isfile(card_files.card_path(task["workspace"], task_id))
    else:
        card_exists = False
    if card_exists:
        refused = []
        for dependency in dependencies:
            parent = con.execute(
                "SELECT task_id FROM research_run_tasks WHERE run_id=? AND branch_id=? AND local_id=?",
                (run_id, node["id"], dependency),
            ).fetchone()
            if not parent:
                continue
            try:
                task_store.link_tasks(con, parent["task_id"], task_id)
            except (ValueError, OSError) as error:
                refused.append((parent["task_id"], str(error)))
        if refused:
            # An edge we cannot write has to be loud. The obvious softer landing -- park this one
            # card in a `held` status and let ``_backfill_dependencies`` promote it later -- was
            # tried and does not work: nothing else in the board writes or reads `held`, and
            # ``cards.reconcile`` copies the card file's own `status` back over the row on the very
            # next tick, so the card is `ready` again and dispatched before anyone notices. There
            # is no state that holds a card whose file does not name its parent, so the caller is
            # told instead, and ``depends_json`` (inserted above) keeps the plan for a later replay.
            #
            # Residual, deliberately not fixed here: with several parents, the edges that *did* get
            # written already ran ``link_tasks``'s own tail -- ready -> todo plus ``promote_task``
            # -- so a child whose written-out parents are all done is `ready` before this raise is
            # reached, and stays dispatchable. Closing that needs every edge of one node written in
            # a single transaction, which is a design change for a later wave.
            task_store.add_event(con, task_id, "dependency_unlinked",
                                 {"parents": [pid for pid, _ in refused], "reason": refused[0][1]})
            raise RuntimeError(
                f"Research dependency could not be linked onto {task_id}: {refused[0][1]} "
                "The card's `needs` does not name that parent, so it would run out of order."
            )


def tasks(con, run_id, *, kind=None, node_id=None, issue_id=None):
    q, args = (
        ("SELECT t.*,rt.branch_id,rt.kind AS research_kind,rt.wave,rt.preflight_artifact,"
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
    A node in ``conflict`` waits for a human, not for a process."""
    rows = [n for n in nodes(con, run_id) if n["status"] not in ("closing", "conflict", *NODE_TERMINAL)]
    return [n for n in rows if n["depth"] == rows[0]["depth"]] if rows else []


def set_node(con, node_id, *, status=None, session_file=None, context_artifact=None,
             worktree=None):
    fields, values = [], []
    for name, value in (("status", status), ("session_file", session_file),
                        ("context_artifact", context_artifact), ("worktree", worktree)):
        if value is not None:
            fields.append(f"{name}=?")
            values.append(value)
    fields.append("updated_at=?")
    values.extend([int(time.time()), node_id])
    if status is not None:                     # a status change commits the phase just completed
        row = node(con, node_id)
        _commit(get(con, row["run_id"]), row, f"research {row['run_id']}/{node_id}: {row['status']}")
    con.execute(f"UPDATE research_branches SET {','.join(fields)} WHERE id=?", values)
    return node(con, node_id)


def node_root(run, node):
    """Where the node's cards work: its worktree, or the project folder for the root."""
    return node["worktree"] or run["workspace"]


def node_worktree(run, node_id):
    return os.path.join(_home(run), "branches", node_id, "worktree")


def evidence_roots(run):
    """The folders that hold material this run itself worked on.

    The project folder is only half of it: every node below the root works in a worktree under the
    run's own home (``node_worktree``), so a document a card downloaded and indexed does not sit
    inside the project folder until -- and unless -- its node merges. A check that accepted only
    ``run['workspace']`` would therefore reject every citation raised below the root node, which is
    where most of a run's cards live.
    """
    return (run["workspace"], _home(run))


def node_branch(node_id):
    return f"research/{node_id}"


def probe_session_dir(run, issue_id):
    """Where a Last Order fork on one issue keeps its session (forked from the node's)."""
    return session_dir(run, f"probe-{issue_id}")


def node_prefix(node):
    """Artifact path prefix inside the run directory: the root writes at the top."""
    return "" if node["parent_id"] is None else f"branches/{node['id']}/"


# --- issues: the edges the red team proposes -------------------------------------------

def add_issue(con, run_id, *, node, kind, question, rationale, priority=0):
    question = str(question or "").strip()
    if not question:
        return None
    # Only exact matches after case/whitespace normalization, and only inside the same node, count
    # as duplicates: another node raising the same question keeps its own issue so its triage sees
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


def set_issue(con, issue_id, status, *, child_branch_id=None, reason=None):
    con.execute(
        "UPDATE research_issues SET status=?,child_branch_id=COALESCE(?,child_branch_id),"
        "reason=COALESCE(?,reason) WHERE id=?",
        (status, child_branch_id, reason, issue_id),
    )


# --- artifacts -------------------------------------------------------------------------

def write_text(con, run_id, kind, title, relative_path, content, *,
               branch_id=None, task_id=None, metadata=None, source_workspace=None):
    run = get(con, run_id)
    if not run:
        raise ValueError(f"Research run not found: {run_id}")
    root = Path(run_dir(run)).resolve()
    path = (root / relative_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError("Research artifact path is outside the run directory.") from error
    source_root = Path(source_workspace or run["workspace"], "research", run_id).resolve()
    source_path = (source_root / relative_path).resolve()
    try:
        source_path.relative_to(source_root)
    except ValueError as error:
        raise ValueError("Research artifact source path is outside the run directory.") from error
    _atomic_write(source_path, str(content))
    sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
    metadata = dict(metadata or {})
    if source_path != path:
        metadata["source_workspace"] = str(source_path)
    old = con.execute(
        "SELECT id FROM research_artifacts WHERE run_id=? AND path=?", (run_id, str(path))
    ).fetchone()
    if old:
        con.execute(
            "UPDATE research_artifacts SET sha256=?,title=?,kind=?,metadata_json=?,created_at=? "
            "WHERE id=?",
            (sha, str(title), str(kind), json.dumps(metadata, ensure_ascii=False),
             int(time.time()), old["id"]),
        )
        return old["id"], str(source_path)
    aid = "a_" + secrets.token_hex(5)
    con.execute(
        "INSERT INTO research_artifacts "
        "(id,run_id,branch_id,task_id,kind,title,path,sha256,metadata_json,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (aid, run_id, branch_id, task_id, str(kind), str(title), str(path), sha,
         json.dumps(metadata, ensure_ascii=False), int(time.time())),
    )
    return aid, str(source_path)


def register_file(con, run_id, kind, title, path, *, sha256, branch_id=None, task_id=None, metadata=None):
    """Register a file that already exists (a Sister's artifact on its card's line) without copying it.
    ``path`` is where it rests once merged; ``metadata["source_workspace"]`` is where it is now."""
    path = os.path.normpath(path)
    old = con.execute(
        "SELECT id FROM research_artifacts WHERE run_id=? AND path=?", (run_id, path)
    ).fetchone()
    if old:
        con.execute(
            "UPDATE research_artifacts SET sha256=?,title=?,kind=?,task_id=?,metadata_json=?,created_at=? "
            "WHERE id=?",
            (sha256, str(title), str(kind), task_id, json.dumps(metadata or {}, ensure_ascii=False),
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
    """An artifact's text from where it lives now (a card's line before its node merges), else its resting path."""
    try:
        source = json.loads(row["metadata_json"] or "{}").get("source_workspace")
    except ValueError:
        source = None
    for path in (source, row["path"]):
        if path and os.path.isfile(path):
            data = Path(path).read_bytes()
            if hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise ValueError(f"artifact {row['id']} changed since it was registered: {path}")
            return data.decode("utf-8")
    raise FileNotFoundError(row["path"])


def artifact_path(row):
    """The existing location of an artifact, or its eventual project path."""
    try:
        source = json.loads(row["metadata_json"] or "{}").get("source_workspace")
    except ValueError:
        source = None
    return next((path for path in (source, row["path"]) if path and os.path.isfile(path)), row["path"])


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

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
import sys
import time
from pathlib import Path

from misaka.platform import tasks as task_store

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
  created_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS research_artifacts (
  id              TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL,
  branch_id       TEXT,
  task_id         TEXT,
  kind            TEXT NOT NULL,
  title           TEXT NOT NULL,
  path            TEXT NOT NULL UNIQUE,
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
CREATE INDEX IF NOT EXISTS research_findings_run ON research_findings(run_id,branch_id,task_id);
CREATE INDEX IF NOT EXISTS research_claims_finding ON research_claims(finding_id);
"""

ACTIVE = ("active", "waiting_input", "stopping")
TERMINAL = ("done", "failed", "stopped")
NODE_TERMINAL = ("closed", "failed", "parked")
NODE_STATES = ("queued", "planning", "waiting_input", "executing", "synthesizing", "critiquing",
               "probing", "triaging", "closing", *NODE_TERMINAL)   # closing = triaged, waiting for its children
DEFAULT_LIMITS = {"max_depth": 3}
RESEARCH_SCHEMA_VERSION = 7


def init(con):
    columns = {row[1] for row in con.execute("PRAGMA table_info(research_runs)")}
    current = con.execute(
        "SELECT 1 FROM schema_migrations WHERE component='research' AND version=?",
        (RESEARCH_SCHEMA_VERSION,),
    ).fetchone()
    if columns and not current:
        # No migration from older schemas: files are the truth and the tables are meant to be a
        # rebuildable index -- but nothing rebuilds them yet, so the old tables are renamed, not
        # dropped, until a rebuild or an incremental migration exists.
        suffix = f"_bak_{time.strftime('%Y%m%d%H%M%S')}"
        for table in (
            "research_claims", "research_evidence_assessments", "research_findings",
            "research_artifacts", "research_issues", "research_run_tasks",
            "research_branches", "research_runs",
        ):
            if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                continue
            for index in con.execute(f'PRAGMA index_list("{table}")').fetchall():
                if index[3] == "c":   # named indexes stay global; free the names for the new tables
                    con.execute(f'DROP INDEX IF EXISTS "{index[1]}"')
            con.execute(f'ALTER TABLE "{table}" RENAME TO "{table}{suffix}"')
        print(f"research: schema changed to v{RESEARCH_SCHEMA_VERSION}; the previous tables were kept "
              f"as *{suffix} (no rebuild from files exists yet).", file=sys.stderr)
    con.executescript(SCHEMA)
    con.executescript(INDEXES)
    con.execute("DROP TRIGGER IF EXISTS research_terminal_notification")   # older boards: nothing consumes it any more
    _backfill_dependencies(con)
    con.execute(
        "INSERT OR IGNORE INTO schema_migrations(component,version,applied_at) VALUES(?,?,?)",
        ("research", RESEARCH_SCHEMA_VERSION, int(time.time())),
    )


# Cards past these statuses keep their dependency history as it is: the DAG must not be rewritten
# under a moving card, and a finished one (whose node line may already be merged and gone) has
# nothing left to wait for.
_SETTLED_TASK_STATUSES = frozenset({"running", "review", "done", "failed", "stopped", "archived"})


def _backfill_dependencies(con):
    """Replay the stored local-id dependencies into the generic task DAG.

    Idempotent and tolerant: it runs on every ``init``, so edges that already exist are skipped,
    settled cards are left alone, and a card whose line no longer exists (a closed node's
    worktree) cannot make the whole replay fail."""
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
        if task is None or task["status"] in _SETTLED_TASK_STATUSES:
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
            except (ValueError, OSError):
                continue        # settled meanwhile, or the card's line is gone: history, not an error
        if dependencies:
            con.execute(
                "UPDATE tasks SET status='todo' WHERE id=? AND status='held'", (row["task_id"],)
            )
            task_store.promote_task(con, row["task_id"])
        else:
            con.execute(
                "UPDATE tasks SET status='ready' WHERE id=? AND status='held'", (row["task_id"],)
            )


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
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _commit(run, node, message):
    """Record the run's state on git: the project line always, the node's line when it has one."""
    from misaka.platform import repo
    paths = [os.path.join("research", run["id"]), "cards", "PROJECT.md"]
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
              root_session=None, final_artifact=None, wave=None):
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
    con.execute(f"UPDATE research_runs SET {','.join(fields)} WHERE id=?", values)
    run = get(con, run_id)
    if phase is not None:
        _commit(run, None, f"research {run_id}: {phase}")
    return run


def request_stop(con, run_id):
    con.execute(
        "UPDATE research_runs SET stop_requested=1,status='stopping',updated_at=? "
        "WHERE id=? AND status IN ('active','waiting_input','stopping')",
        (int(time.time()), run_id),
    )


def resume(con, run_id):
    """Reopen a stopped, failed, or waiting run: same card generations, nodes pick up where they were."""
    for row in tasks(con, run_id):
        if row["status"] != "stopped":
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
    con.execute(
        "UPDATE research_branches SET status='planning',updated_at=? "
        "WHERE run_id=? AND status='waiting_input'", (int(time.time()), run_id),
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
    for dependency in dependencies:
        parent = con.execute(
            "SELECT task_id FROM research_run_tasks WHERE run_id=? AND branch_id=? AND local_id=?",
            (run_id, node["id"], dependency),
        ).fetchone()
        if parent:
            task_store.link_tasks(con, parent["task_id"], task_id)


def tasks(con, run_id, *, kind=None, node_id=None, issue_id=None):
    q, args = (
        "SELECT t.*,rt.branch_id,rt.kind AS research_kind,rt.wave,rt.preflight_artifact,"
        "rt.local_id,rt.issue_id,rt.depends_json "
        "FROM research_run_tasks rt JOIN tasks t ON t.id=rt.task_id WHERE rt.run_id=?",
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
    """The BFS frontier: every node still expanding at the shallowest such depth, oldest first."""
    rows = [n for n in nodes(con, run_id) if n["status"] not in ("closing", *NODE_TERMINAL)]
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
    # Only exact matches after case/whitespace normalization count as duplicates; semantic merging is Last Order's job.
    normalized = " ".join(question.casefold().split())
    for row in con.execute("SELECT id,question FROM research_issues WHERE run_id=?", (run_id,)):
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
               branch_id=None, task_id=None, metadata=None):
    run = get(con, run_id)
    if not run:
        raise ValueError(f"Research run not found: {run_id}")
    root = Path(run_dir(run)).resolve()
    path = (root / relative_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError("Research artifact path is outside the run directory.") from error
    _atomic_write(path, str(content))
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    old = con.execute("SELECT id FROM research_artifacts WHERE path=?", (str(path),)).fetchone()
    if old:
        con.execute(
            "UPDATE research_artifacts SET sha256=?,title=?,kind=?,metadata_json=?,created_at=? "
            "WHERE id=?",
            (sha, str(title), str(kind), json.dumps(metadata or {}, ensure_ascii=False),
             int(time.time()), old["id"]),
        )
        return old["id"], str(path)
    aid = "a_" + secrets.token_hex(5)
    con.execute(
        "INSERT INTO research_artifacts "
        "(id,run_id,branch_id,task_id,kind,title,path,sha256,metadata_json,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (aid, run_id, branch_id, task_id, str(kind), str(title), str(path), sha,
         json.dumps(metadata or {}, ensure_ascii=False), int(time.time())),
    )
    return aid, str(path)


def register_file(con, run_id, kind, title, path, *, sha256, branch_id=None, task_id=None, metadata=None):
    """Register a file that already exists (a Sister's artifact on its card's line) without copying it.
    ``path`` is where it rests once merged; ``metadata["source_workspace"]`` is where it is now."""
    path = os.path.normpath(path)
    old = con.execute("SELECT id FROM research_artifacts WHERE path=?", (path,)).fetchone()
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
            return Path(path).read_text(encoding="utf-8")
    raise FileNotFoundError(row["path"])


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
        return Path(row["path"]).read_text(encoding="utf-8")
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

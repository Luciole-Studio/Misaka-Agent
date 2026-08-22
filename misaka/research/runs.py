"""Persistent research-run state and file artifacts.

Research prose lives in the project directory. SQLite stores only workflow
state and relationships, so a Last Order process that died can resume the same run.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from pathlib import Path

from misaka.platform import notifications, projects, tasks as task_store


SCHEMA = """
CREATE TABLE IF NOT EXISTS research_runs (
  id              TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL,
  project_name    TEXT NOT NULL,
  project_path    TEXT NOT NULL,
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
  status          TEXT NOT NULL DEFAULT 'planned',
  session_file    TEXT,
  context_artifact TEXT,
  created_at      INTEGER NOT NULL,
  updated_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS research_run_tasks (
  task_id         TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL,
  branch_id       TEXT,
  kind            TEXT NOT NULL,
  wave            INTEGER NOT NULL DEFAULT 0,
  preflight_artifact TEXT,
  local_id        TEXT,
  depends_json    TEXT NOT NULL DEFAULT '[]',
  created_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS research_issues (
  id              TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL,
  branch_id       TEXT,
  wave            INTEGER NOT NULL,
  kind            TEXT NOT NULL,
  question        TEXT NOT NULL,
  rationale       TEXT NOT NULL,
  priority        INTEGER NOT NULL DEFAULT 0,
  status          TEXT NOT NULL DEFAULT 'open',
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
CREATE TABLE IF NOT EXISTS research_evidence_assessments (
  id              TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL,
  branch_id       TEXT,
  finding_id      TEXT,
  wave            INTEGER NOT NULL,
  assessor        TEXT NOT NULL,
  assessment_json TEXT NOT NULL,
  created_at      INTEGER NOT NULL
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS research_branches_run ON research_branches(run_id,depth);
CREATE INDEX IF NOT EXISTS research_tasks_run ON research_run_tasks(run_id,wave,kind);
CREATE INDEX IF NOT EXISTS research_issues_run ON research_issues(run_id,status,priority);
CREATE INDEX IF NOT EXISTS research_artifacts_run ON research_artifacts(run_id,branch_id,kind);
CREATE INDEX IF NOT EXISTS research_findings_run ON research_findings(run_id,branch_id,task_id);
CREATE INDEX IF NOT EXISTS research_claims_finding ON research_claims(finding_id);
CREATE INDEX IF NOT EXISTS research_assessments_run ON research_evidence_assessments(run_id,wave);
"""

ACTIVE = ("active", "waiting_input", "stopping")
TERMINAL = ("done", "failed", "stopped")
DEFAULT_LIMITS = {
    "max_depth": 3,
}
RESEARCH_SCHEMA_VERSION = 4


def init(con):
    columns = {row[1] for row in con.execute("PRAGMA table_info(research_runs)")}
    current = con.execute(
        "SELECT 1 FROM schema_migrations WHERE component='research' AND version=?",
        (RESEARCH_SCHEMA_VERSION,),
    ).fetchone()
    if columns and not current:
        # No migration from older schemas: old runs cannot be given an honest
        # workspace/project identity after the fact, so drop them.
        for table in (
            "research_claims", "research_evidence_assessments", "research_findings",
            "research_artifacts", "research_issues", "research_run_tasks",
            "research_branches", "research_runs",
        ):
            con.execute(f'DROP TABLE IF EXISTS "{table}"')
    con.executescript(SCHEMA)
    for table in ("nodes", "edges", "claims", "clauses", "precedents"):
        con.execute(f'DROP TABLE IF EXISTS "{table}"')
    con.executescript(INDEXES)
    notifications.install_research(con)
    _backfill_task_links(con)
    con.execute(
        "INSERT OR IGNORE INTO schema_migrations(component,version,applied_at) VALUES(?,?,?)",
        ("research", RESEARCH_SCHEMA_VERSION, int(time.time())),
    )


def _backfill_task_links(con):
    """Replay the stored local-id dependencies into the generic task DAG."""
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
        for parent_id in parents:
            task_store.link_tasks(con, parent_id, row["task_id"])
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
    return os.path.join(run["project_path"], "runs", run["id"])


def ensure_layout(run):
    root = Path(run_dir(run))
    for rel in ("branches", "tasks", "critiques", "syntheses", "sessions"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    return str(root)


def _atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def create(con, *, project_id, question, limits=None, token_start=0):
    project = projects.resolve(con, project_id)
    if not project:
        raise ValueError("A research run must belong to an existing project.")
    question = str(question or "").strip()
    if len(question) < 2:
        raise ValueError("The research question cannot be empty.")
    init(con)
    now = int(time.time())
    run_id = "r_" + secrets.token_hex(5)
    spec = normalize_limits(limits)
    con.execute(
        "INSERT INTO research_runs "
        "(id,project_id,project_name,project_path,workspace,question,phase,status,limits_json,"
        "token_start,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, project["id"], project["name"], project["path"], project["workspace"],
         question, "created", "active", json.dumps(spec, ensure_ascii=False),
         int(token_start), now, now),
    )
    row = get(con, run_id)
    ensure_layout(row)
    write_text(con, run_id, "question", 'Original research question', "question.md",
               f"""# Original research question

{question}
""")
    return get(con, run_id)


def get(con, run_id):
    init(con)
    return con.execute("SELECT * FROM research_runs WHERE id=?", (run_id,)).fetchone()


def latest(con, project=None, active_only=False):
    init(con)
    q, args = "SELECT * FROM research_runs WHERE 1=1", []
    if project:
        q += " AND (project_id=? OR project_name=?)"
        args.extend([project, project])
    if active_only:
        q += " AND status IN ('active','waiting_input','stopping')"
    return con.execute(q + " ORDER BY created_at DESC LIMIT 1", args).fetchone()


def listing(con, *, project=None):
    init(con)
    if project:
        return con.execute(
            "SELECT * FROM research_runs WHERE project_id=? OR project_name=? "
            "ORDER BY created_at DESC", (project, project)
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
    return get(con, run_id)


def request_stop(con, run_id):
    con.execute(
        "UPDATE research_runs SET stop_requested=1,status='stopping',updated_at=? "
        "WHERE id=? AND status IN ('active','waiting_input','stopping')",
        (int(time.time()), run_id),
    )


def clear_stop(con, run_id):
    con.execute(
        "UPDATE research_runs SET stop_requested=0,status='active',updated_at=? WHERE id=?",
        (int(time.time()), run_id),
    )


def resume_stopped(con, run_id):
    """Reopen the same task generations after an explicit stop without duplicating cards."""
    linked = tasks(con, run_id)
    stopped = [row for row in linked if row["status"] == "stopped"]
    for row in stopped:
        target = "todo" if task_store.parent_ids(con, row["id"]) else "ready"
        if task_store.reopen_task(
            con, row["id"], target_status=target,
            expected_generation=row["generation"], invalidate_descendants=False,
        ):
            task_store.add_event(
                con, row["id"], "research_resumed",
                {"from_generation": row["generation"]}, generation=int(row["generation"]) + 1,
            )
    kinds = {row["research_kind"] for row in stopped}
    current = get(con, run_id)
    phase = ("executing" if "research" in kinds else
             "synthesizing" if "synthesis" in kinds else current["phase"])
    if not stopped and phase == "finalizing":
        wave = int(current["wave"])
        synth_done = sum(1 for row in linked
                         if row["research_kind"] == "synthesis"
                         and int(row["wave"]) == wave and row["status"] == "done")
        if synth_done < 2 and any(row["research_kind"] == "research"
                                  and row["status"] == "done" for row in linked):
            phase = "synthesizing"
    con.execute(
        "UPDATE research_runs SET stop_requested=0,status='active',phase=?,updated_at=? WHERE id=?",
        (phase, int(time.time()), run_id),
    )
    return phase


def stop_requested(con, run_id):
    row = get(con, run_id)
    return not row or bool(row["stop_requested"])


def call_timeout(cfg, default):
    """Per-call failure timeout; this is not a research stopping criterion."""
    return max(1, int(default))


def task_count(con, run_id):
    return con.execute(
        "SELECT COUNT(*) FROM research_run_tasks WHERE run_id=?", (run_id,)
    ).fetchone()[0]


def link_task(con, run_id, task_id, *, kind, branch_id=None, wave=0,
              preflight_artifact=None, local_id=None, dependencies=()):
    run = get(con, run_id)
    if not run:
        raise ValueError(f"Research run not found: {run_id}")
    dependencies = list(dependencies)
    con.execute(
        "INSERT INTO research_run_tasks "
        "(task_id,run_id,branch_id,kind,wave,preflight_artifact,local_id,depends_json,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (task_id, run_id, branch_id, kind, int(wave), preflight_artifact, local_id,
         json.dumps(dependencies, ensure_ascii=False), int(time.time())),
    )
    output_dir = Path(run_dir(run), "tasks", task_id, "work")
    output_dir.mkdir(parents=True, exist_ok=True)
    con.execute(
        "UPDATE tasks SET workspace=?,output_dir=? WHERE id=?",
        (run["workspace"], str(output_dir), task_id),
    )
    for dependency in dependencies:
        parent = con.execute(
            "SELECT task_id FROM research_run_tasks WHERE run_id=? AND branch_id IS ? "
            "AND local_id=?",
            (run_id, branch_id, dependency),
        ).fetchone()
        if parent:
            task_store.link_tasks(con, parent["task_id"], task_id)


def tasks(con, run_id, *, kind=None, branch_id=None):
    q, args = (
        "SELECT t.*,rt.branch_id,rt.kind AS research_kind,rt.wave,rt.preflight_artifact,"
        "rt.local_id,rt.depends_json "
        "FROM research_run_tasks rt JOIN tasks t ON t.id=rt.task_id WHERE rt.run_id=?",
        [run_id],
    )
    if kind:
        q += " AND rt.kind=?"
        args.append(kind)
    if branch_id is not None:
        q += " AND rt.branch_id=?"
        args.append(branch_id)
    return con.execute(q + " ORDER BY rt.created_at", args).fetchall()


def create_branch(con, run_id, *, trigger, parent_id=None, depth=1,
                  context_artifact=None):
    run = get(con, run_id)
    if not run:
        raise ValueError(f"Research run not found: {run_id}")
    if int(depth) > limits(run)["max_depth"]:
        raise RuntimeError("The research run has reached its branch-depth limit.")
    bid = "b_" + secrets.token_hex(5)
    now = int(time.time())
    con.execute(
        "INSERT INTO research_branches "
        "(id,run_id,parent_id,trigger_text,depth,status,context_artifact,created_at,updated_at) "
        "VALUES (?,?,?,?,?,'planned',?,?,?)",
        (bid, run_id, parent_id, str(trigger), int(depth), context_artifact, now, now),
    )
    return con.execute("SELECT * FROM research_branches WHERE id=?", (bid,)).fetchone()


def set_branch(con, branch_id, *, status=None, session_file=None, context_artifact=None):
    fields, values = [], []
    for name, value in (("status", status), ("session_file", session_file),
                        ("context_artifact", context_artifact)):
        if value is not None:
            fields.append(f"{name}=?")
            values.append(value)
    fields.append("updated_at=?")
    values.extend([int(time.time()), branch_id])
    con.execute(f"UPDATE research_branches SET {','.join(fields)} WHERE id=?", values)


def branches(con, run_id):
    return con.execute(
        "SELECT * FROM research_branches WHERE run_id=? ORDER BY created_at", (run_id,)
    ).fetchall()


def add_issue(con, run_id, *, wave, kind, question, rationale,
              priority=0, branch_id=None):
    question = str(question or "").strip()
    if not question:
        return None
    # Only exact matches after case/whitespace normalization count as duplicates; semantic merging is Last Order's job.
    normalized = " ".join(question.casefold().split())
    for row in con.execute(
        "SELECT id,question FROM research_issues WHERE run_id=? AND status!='dropped'", (run_id,)
    ):
        if " ".join(row["question"].casefold().split()) == normalized:
            return row["id"]
    iid = "i_" + secrets.token_hex(5)
    con.execute(
        "INSERT INTO research_issues "
        "(id,run_id,branch_id,wave,kind,question,rationale,priority,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (iid, run_id, branch_id, int(wave), str(kind or 'unclassified'), question,
         str(rationale or ""), int(priority or 0), int(time.time())),
    )
    return iid


def open_issues(con, run_id):
    return con.execute(
        "SELECT * FROM research_issues WHERE run_id=? AND status='open' "
        "ORDER BY priority DESC,created_at", (run_id,),
    ).fetchall()


def set_issue(con, issue_id, status, child_branch_id=None):
    con.execute(
        "UPDATE research_issues SET status=?,child_branch_id=COALESCE(?,child_branch_id) WHERE id=?",
        (status, child_branch_id, issue_id),
    )


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


def artifacts(con, run_id, *, branch_id=None, kind=None, root_only=False):
    q, args = "SELECT * FROM research_artifacts WHERE run_id=?", [run_id]
    if branch_id is not None:
        q += " AND branch_id=?"
        args.append(branch_id)
    elif root_only:
        q += " AND branch_id IS NULL"
    if kind:
        q += " AND kind=?"
        args.append(kind)
    return con.execute(q + " ORDER BY created_at", args).fetchall()


def add_assessment(con, run_id, assessment, *, wave, assessor="redteam", branch_id=None):
    """Persist an agent's evidence assessment verbatim; Python never reduces it to a score."""
    if not isinstance(assessment, dict):
        return None
    finding_id = str(assessment.get("finding_id") or "").strip() or None
    if finding_id and not con.execute(
        "SELECT 1 FROM research_findings WHERE id=? AND run_id=?", (finding_id, run_id)
    ).fetchone():
        return None
    encoded = json.dumps(assessment, ensure_ascii=False, sort_keys=True)
    old = con.execute(
        "SELECT id,assessment_json FROM research_evidence_assessments "
        "WHERE run_id=? AND wave=? AND assessor=?", (run_id, int(wave), str(assessor))
    ).fetchall()
    if any(json.dumps(json.loads(row["assessment_json"]), ensure_ascii=False, sort_keys=True) == encoded
           for row in old):
        return next(row["id"] for row in old
                    if json.dumps(json.loads(row["assessment_json"]), ensure_ascii=False,
                                  sort_keys=True) == encoded)
    aid = "ea_" + secrets.token_hex(5)
    con.execute(
        "INSERT INTO research_evidence_assessments "
        "(id,run_id,branch_id,finding_id,wave,assessor,assessment_json,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (aid, run_id, branch_id, finding_id, int(wave), str(assessor),
         encoded, int(time.time())),
    )
    return aid


def assessments(con, run_id):
    return con.execute(
        "SELECT * FROM research_evidence_assessments WHERE run_id=? ORDER BY created_at",
        (run_id,),
    ).fetchall()


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
        "id": run["id"], "project_id": run["project_id"],
        "project": run["project_name"], "workspace": run["workspace"],
        "phase": run["phase"],
        "status": run["status"], "wave": run["wave"], "limits": limits(run),
        "tasks": task_count(con, run_id), "branches": len(branches(con, run_id)),
        "open_issues": len(open_issues(con, run_id)),
        "stop_requested": bool(run["stop_requested"]),
        "final_artifact": run["final_artifact"], "last_error": run["last_error"],
    }

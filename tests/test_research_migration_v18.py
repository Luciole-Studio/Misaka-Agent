"""A board written by research schema v18 opens on this build, its runs carried into their graphs.

``V18`` is that schema as it shipped (misaka/core/research/runs.py before the graph core). The board
here holds one of everything v18 kept: nodes and an edge, a decision with an option pursued by its own
node, one opened as a child and one declined, issues gone to an option and to a node, a relation, Last
Order's commands, an applied and approved reconciliation, a card link and a live driver lease.
"""

from __future__ import annotations

import json
import time
from contextlib import closing

from misaka.core import graph
from misaka.core.platform import tasks
from misaka.core.research import runs

V18 = """
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
  question        TEXT NOT NULL,
  depth           INTEGER NOT NULL,
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
CREATE TABLE IF NOT EXISTS research_edges (
  run_id          TEXT NOT NULL,
  child_id        TEXT NOT NULL,
  parent_id       TEXT NOT NULL,
  PRIMARY KEY(child_id,parent_id)
);
CREATE TABLE IF NOT EXISTS research_decisions (
  id              TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL,
  node_id         TEXT NOT NULL,
  round           INTEGER NOT NULL,
  origin          TEXT NOT NULL,
  question        TEXT NOT NULL,
  stakes          TEXT NOT NULL,
  created_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS research_options (
  id              TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL,
  decision_id     TEXT NOT NULL,
  label           TEXT NOT NULL,
  premise         TEXT NOT NULL,
  node_id         TEXT,
  reason          TEXT,
  created_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS research_relations (
  id              TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL,
  kind            TEXT NOT NULL,
  note            TEXT NOT NULL,
  created_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS research_relation_nodes (
  relation_id     TEXT NOT NULL,
  node_id         TEXT NOT NULL,
  position        INTEGER NOT NULL,
  PRIMARY KEY(relation_id,node_id)
);
CREATE TABLE IF NOT EXISTS research_reconciles (
  run_id          TEXT NOT NULL,
  round           INTEGER NOT NULL,
  payload_json    TEXT NOT NULL,
  session_file    TEXT NOT NULL,
  tool_call_id    TEXT NOT NULL,
  approved_call_id TEXT,
  receipt_json    TEXT,
  applied_at      INTEGER,
  created_at      INTEGER NOT NULL,
  PRIMARY KEY(run_id,round)
);
CREATE TABLE IF NOT EXISTS research_run_tasks (
  task_id         TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL,
  branch_id       TEXT NOT NULL,
  kind            TEXT NOT NULL,
  round           INTEGER NOT NULL DEFAULT 1,
  local_id        TEXT,
  depends_json    TEXT NOT NULL DEFAULT '[]',
  created_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS research_issues (
  id              TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL,
  branch_id       TEXT NOT NULL,
  task_id         TEXT NOT NULL,
  round           INTEGER NOT NULL,
  origin          TEXT NOT NULL,
  kind            TEXT NOT NULL,
  question        TEXT NOT NULL,
  rationale       TEXT NOT NULL,
  priority        INTEGER NOT NULL DEFAULT 0,
  disposition     TEXT,
  option_id       TEXT,
  covered_by      TEXT,
  reason          TEXT,
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
CREATE INDEX IF NOT EXISTS research_branches_run ON research_branches(run_id,depth);
CREATE INDEX IF NOT EXISTS research_edges_parent ON research_edges(parent_id);
CREATE INDEX IF NOT EXISTS research_edges_run ON research_edges(run_id);
CREATE INDEX IF NOT EXISTS research_decisions_run ON research_decisions(run_id,node_id);
CREATE INDEX IF NOT EXISTS research_options_run ON research_options(run_id,decision_id);
CREATE INDEX IF NOT EXISTS research_relations_run ON research_relations(run_id);
CREATE INDEX IF NOT EXISTS research_relation_nodes_node ON research_relation_nodes(node_id);
CREATE INDEX IF NOT EXISTS research_tasks_run ON research_run_tasks(run_id,branch_id,kind);
CREATE INDEX IF NOT EXISTS research_issues_run ON research_issues(run_id,branch_id,round);
CREATE INDEX IF NOT EXISTS research_issues_option ON research_issues(option_id);
CREATE INDEX IF NOT EXISTS research_artifacts_run ON research_artifacts(run_id,branch_id,kind);
CREATE UNIQUE INDEX IF NOT EXISTS research_artifacts_generated ON research_artifacts(run_id,path) WHERE task_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS research_artifacts_task ON research_artifacts(run_id,task_id,path) WHERE task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS research_findings_run ON research_findings(run_id,branch_id,task_id);
CREATE INDEX IF NOT EXISTS research_claims_finding ON research_claims(finding_id);
CREATE UNIQUE INDEX IF NOT EXISTS research_tasks_local ON research_run_tasks(run_id,branch_id,local_id) WHERE local_id IS NOT NULL;
"""
LIMITS = {"max_depth": 3, "parallel": 4, "sister_parallel": 4, "max_followups": 2, "max_revisions": 2, "max_nodes": 30}


def _seed(con, workspace):
    con.executescript(V18)
    con.execute("INSERT INTO schema_migrations(component,version,applied_at) VALUES('research',18,0)")
    rows = {
        "research_runs": [("r_old", workspace, "Why?", "active", "active", 1, json.dumps(LIMITS), 0, None, "origin", 0,
                           None, None, "driver:host:1:x", int(time.time()) + 600, 1, 1)],
        "research_branches": [("b_root", "r_old", "Why?", 0, "closed", "state/sessions/root.jsonl", None, None, None,
                               None, "k", None, 1, 1),
                              ("b_child", "r_old", "How?", 1, "queued", None, None, None, None, None, None, None, 2, 2)],
        "research_edges": [("r_old", "b_child", "b_root")],
        "research_decisions": [("d_1", "r_old", "b_root", 1, "plan", "Which way?", "it turns on it", 2)],
        "research_options": [("o_own", "r_old", "d_1", "own", "as planned", "b_root", None, 2),
                             ("o_open", "r_old", "d_1", "open", "other", "b_child", None, 2),
                             ("o_no", "r_old", "d_1", "decline", "thin", None, "no sources", 2)],
        "research_relations": [("rel_1", "r_old", "converge", "same finding", 3)],
        "research_relation_nodes": [("rel_1", "b_child", 0), ("rel_1", "b_root", 1)],
        "research_issues": [("i_1", "r_old", "b_root", "t_1", 1, "critique", "k", "q1", "r", 2, "branch", "o_open", None,
                             None, 4),
                            ("i_2", "r_old", "b_root", "t_1", 1, "critique", "k", "q2", "r", 1, "covered", None,
                             "b_child", "owned there", 4),
                            ("i_3", "r_old", "b_root", "t_1", 1, "critique", "k", "q3", "r", 0, None, None, None, None, 4)],
        "research_actions": [("r_old", "b_root", "plan", json.dumps({"tasks": []}), "state/sessions/lo.jsonl", "call-1", 5),
                             ("r_old", "b_root", "start", json.dumps({"plan_tool_call_id": "call-1", "summary": "go"}),
                              "state/sessions/lo.jsonl", "call-2", 6),
                             ("r_old", "b_child", "erratum:call-3", json.dumps({"node": "b_root", "text": "fix"}),
                              "state/sessions/child.jsonl", "call-3", 7)],
        "research_reconciles": [("r_old", 1, json.dumps({"nodes": []}), "state/sessions/lo.jsonl", "call-4", "call-4",
                                 json.dumps({"considered": ["o_open"], "nodes": ["b_child"], "joins": []}), 8, 8)],
        "research_run_tasks": [("t_1", "r_old", "b_root", "research", 1, "a", "[]", 9)],
    }
    for table, values in rows.items():
        for value in values:
            con.execute(f"INSERT INTO {table} VALUES ({','.join('?' * len(value))})", value)


def test_a_v18_board_is_carried_into_graphs_and_resumes_where_it_was(tmp_path):
    board = str(tmp_path / "board.db")
    with closing(tasks.connect(board)) as con:
        _seed(con, str(tmp_path))
        runs.init(con)
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert not tables & {"research_branches", "research_edges", "research_decisions", "research_options",
                             "research_relations", "research_relation_nodes", "research_issues", "research_actions",
                             "research_reconciles", "research_run_tasks"}
        assert "driver_lock" not in {row[1] for row in con.execute('PRAGMA table_info("research_runs")')}
        assert runs.get(con, "r_old")["driver_lock"] == "driver:host:1:x"          # the lease is the graph's now

        assert [(n["id"], n["depth"], n["status"]) for n in runs.nodes(con, "r_old")] == [
            ("b_root", 0, "closed"), ("b_child", 1, "queued")]
        assert runs.node(con, "b_root")["session_file"].endswith("root.jsonl")
        assert [row["id"] for row in runs.parents(con, "b_child")] == ["b_root"]
        assert [(row["id"], row["node_id"], row["reason"]) for row in runs.options(con, "r_old")] == [
            ("o_own", "b_root", None), ("o_open", "b_child", None), ("o_no", None, "no sources")]
        assert runs.pending_options(con, "r_old") == []
        assert [row["id"] for row in runs.origin_options(con, "b_child")] == ["o_open"]
        found = {row["id"]: row for row in runs.issues(con, "r_old")}
        assert (found["i_1"]["option_id"], found["i_2"]["covered_by"], found["i_3"]["disposition"]) == (
            "o_open", "b_child", None)
        assert [(row["kind"], row["nodes"]) for row in runs.relations(con, "r_old")] == [("converge", ["b_child", "b_root"])]

        assert runs.plan_started(con, "r_old", "b_root")
        assert runs.action(con, "r_old", "b_root", "plan")["session_file"].endswith("lo.jsonl")
        assert runs.errata(con, "r_old") == [{"raised_by": "b_child", "node": "b_root", "text": "fix"}]
        reconciled = runs.reconcile(con, "r_old", 1)
        assert reconciled["applied_at"] and reconciled["receipt"]["nodes"] == ["b_child"]
        assert runs.reconcile_approved(con, "r_old", 1)
        assert runs.task_contexts(con)["t_1"]["node_id"] == "b_root"
        assert graph.entries(con, "r_old", status=graph.APPLIED)[0].op == "migrate"
    with closing(tasks.connect(board)) as con:          # opened again, the board is simply current
        runs.init(con)
        assert runs.get(con, "r_old")["status"] == "active" and len(runs.nodes(con, "r_old")) == 2

"""The research graph's storage and structural rules: nodes and edges, decisions and options,
reconciliation, closing, and the views written into the project. Structure only -- nothing
here judges what a node found."""
import json
from contextlib import closing

import pytest

from misaka.core import graph as graph_core
from misaka.core.platform import tasks
from misaka.core.research import runs, views, workflow


@pytest.fixture
def board(tmp_path):
    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        yield con


def _run(con, tmp_path, **limits):
    run = runs.create(con, workspace=str(tmp_path), question="Why did the reform fail?", limits=limits or None)
    assert runs.acquire_driver(con, run["id"], "driver:test")
    return runs.get(con, run["id"]), runs.root(con, run["id"])


def _finish(con, node, status="closed"):
    con.execute("UPDATE research_execution SET status=? WHERE id=?", (status, node["id"]))
    return runs.node(con, node["id"])


def _conclude(con, run, node, text="Conclusion"):
    runs.write_text(con, run["id"], "synthesis", "Conclusion", runs.generated_path("synthesis.md", node_id=node["id"]),
                    text, branch_id=node["id"])


def _decide(con, run, node, *labels, own=False, index=0):
    options = [{"label": label, "premise": f"premise of {label}"} for label in labels]
    if own:
        options.insert(0, {"label": "own line", "premise": "as concluded", "own": True})
    did = runs.add_decision(con, run["id"], node=node, round=1, origin="decide", index=index,
                            question="Which path?", stakes="The answer turns on it", options=options)
    return did, [row["id"] for row in runs.options(con, run["id"], decision_id=did)]


def test_the_root_is_depth_zero_and_the_only_node_without_parents(board, tmp_path):
    run, root = _run(board, tmp_path)
    assert runs.is_root(root) and root["question"] == "Why did the reform fail?"
    assert runs.parents(board, root["id"]) == []
    with pytest.raises(ValueError, match="one root"):
        runs.create_node(board, run["id"], question="A second root", parents=())


def test_depth_is_one_more_than_the_deepest_parent(board, tmp_path):
    run, root = _run(board, tmp_path, max_depth=3)
    a = runs.create_node(board, run["id"], question="A", parents=[root["id"]])
    b = runs.create_node(board, run["id"], question="B", parents=[a["id"]])
    join = runs.create_node(board, run["id"], question="A and B", parents=[root["id"], b["id"]])
    assert (a["depth"], b["depth"], join["depth"]) == (1, 2, 3)
    assert {p["id"] for p in runs.parents(board, join["id"])} == {root["id"], b["id"]}
    assert views.kind(board, join) == "join" and views.kind(board, a) == "alt" and views.kind(board, root) == "root"
    assert views.ancestors(board, join["id"]) == {root["id"], a["id"], b["id"]}
    assert {c["id"] for c in runs.children(board, root["id"])} == {a["id"], join["id"]}


def test_a_node_forks_the_lowest_node_its_parents_share(board, tmp_path):
    """2026-09-28 (user): a join is managed by its parents' lowest common ancestor. One parent is its
    own; where the parents share several, each carrying only its own line, the search goes on up."""
    run, root = _run(board, tmp_path, max_depth=5)
    make = lambda question, *parents: runs.create_node(board, run["id"], question=question,
                                                        parents=[p["id"] for p in parents])
    a, b = make("A", root), make("B", root)
    x = make("X", a)
    assert views.fork_source(board, x)["id"] == a["id"]
    assert views.fork_source(board, make("A and B", a, b))["id"] == root["id"]
    # A parent that the other descends from is what both lines stand on.
    k = make("K", make("X and B", x, b))
    assert views.fork_source(board, make("K and X", k, x))["id"] == x["id"]
    # P and Q both rest on X and on B, and neither of those on the other: only the root is shared.
    p, q = make("P", x, b), make("Q", x, b)
    assert views.fork_source(board, make("P and Q", p, q))["id"] == root["id"]


def test_depth_and_node_limits_are_enforced_where_nodes_are_made(board, tmp_path):
    run, root = _run(board, tmp_path, max_depth=1, max_nodes=2)
    child = runs.create_node(board, run["id"], question="A", parents=[root["id"]])
    with pytest.raises(RuntimeError, match="depth limit"):
        runs.create_node(board, run["id"], question="Too deep", parents=[child["id"]])
    with pytest.raises(RuntimeError, match="node limit"):
        runs.create_node(board, run["id"], question="Too many", parents=[root["id"]])


def test_a_parent_from_another_run_is_refused(board, tmp_path):
    run, _root = _run(board, tmp_path)
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = runs.create(board, workspace=str(other_dir), question="Another question")
    with pytest.raises(ValueError, match="same run"):
        runs.create_node(board, run["id"], question="Crossed", parents=[runs.root(board, other["id"])["id"]])


def test_decisions_are_idempotent_and_the_own_line_is_resolved_at_once(board, tmp_path):
    run, root = _run(board, tmp_path)
    did, option_ids = _decide(board, run, root, "Fiscal", own=True)
    again, _ = _decide(board, run, root, "Fiscal", own=True)
    assert did == again and len(runs.options(board, run["id"], decision_id=did)) == 2
    own = runs.option(board, option_ids[0])
    assert own["node_id"] == root["id"]
    assert [row["id"] for row in runs.pending_options(board, run["id"])] == [option_ids[1]]


def _pending(con, run):
    return {row["id"]: row for row in runs.pending_options(con, run["id"])}


def test_reconcile_is_needed_for_pending_options_or_new_conclusions_only(board, tmp_path):
    run, root = _run(board, tmp_path)
    assert views.reconcile_state(board, run) == (False, [])
    root = _finish(board, root)
    _conclude(board, run, root)
    # One concluded node and nothing pending: no join or relation can exist.
    assert views.reconcile_state(board, run)[0] is False
    _decide(board, run, root, "Fiscal", "Coalition")
    needed, considered = views.reconcile_state(board, run)
    assert needed and considered == [root["id"]]


def test_reconciliation_merges_same_question_options_into_one_node_with_two_parents(board, tmp_path):
    run, root = _run(board, tmp_path, max_depth=3)
    a = runs.create_node(board, run["id"], question="A", parents=[root["id"]])
    b = runs.create_node(board, run["id"], question="B", parents=[root["id"]])
    root, a, b = _finish(board, root), _finish(board, a), _finish(board, b)
    _, a_options = _decide(board, run, a, "Local alliances", "Outside shock", index=0)
    _, b_options = _decide(board, run, b, "Local alliances too", "Class", index=0)
    payload = {"nodes": [{"question": "What did local alliances do?", "options": [a_options[0], b_options[0]],
                          "rationale": "Both ask it"}],
               "joins": [], "relations": [],
               "not_pursued": [{"option": a_options[1], "reason": "complements node " + b["id"]},
                               {"option": b_options[1], "reason": "the node limit"}]}
    resolved = views.resolve_reconcile(board, run, payload)
    assert resolved["nodes"][0]["parents"] == [a["id"], b["id"]]
    runs.record_reconcile(board, run, 1, {**payload, "summary": "merge"}, session_file=str(tmp_path / "s.jsonl"),
                          tool_call_id="call-1")
    receipt = runs.apply_reconcile(board, run, 1, resolved, considered=[root["id"], a["id"], b["id"]])
    merged = runs.node(board, receipt["nodes"][0])
    assert merged["depth"] == 2 and views.kind(board, merged) == "join"
    assert {o["id"] for o in runs.origin_options(board, merged["id"])} == {a_options[0], b_options[0]}
    # Two edges ending at one node: the paths list sends both options to it.
    paths = views.render_paths(views.snapshot(board, runs.get(board, run["id"])))
    assert paths.count(f"→ node `{merged['id']}`") == 2
    assert runs.pending_options(board, run["id"]) == []
    assert runs.option(board, b_options[1])["reason"] == "the node limit"
    # Applied once: a replay returns the same receipt and creates nothing.
    assert runs.apply_reconcile(board, run, 1, resolved, considered=[]) == receipt
    assert len(runs.nodes(board, run["id"])) == 4


@pytest.mark.parametrize("problem", ["missing", "twice", "unknown", "over_budget", "too_deep"])
def test_reconciliation_structure_is_checked(board, tmp_path, problem):
    run, root = _run(board, tmp_path, max_depth=1 if problem == "too_deep" else 3,
                     max_nodes=2 if problem == "over_budget" else 30)
    root = _finish(board, root)
    _, (first, second) = _decide(board, run, root, "One", "Two")
    nodes = [{"question": "One", "options": [first], "rationale": "r"},
             {"question": "Two", "options": [second], "rationale": "r"}]
    if problem == "too_deep":
        child = _finish(board, runs.create_node(board, run["id"], question="Deep", parents=[root["id"]]))
        _, (deep_a, deep_b) = _decide(board, run, child, "Deeper", "Deepest", index=1)
        nodes.append({"question": "Deeper", "options": [deep_a, deep_b], "rationale": "r"})
    payload = {"nodes": nodes, "joins": [], "not_pursued": [], "relations": []}
    if problem == "missing":
        payload["nodes"] = nodes[:1]
    elif problem == "twice":
        payload["not_pursued"] = [{"option": first, "reason": "also"}]
    elif problem == "unknown":
        payload["not_pursued"] = [{"option": "o_nope", "reason": "?"}]
    with pytest.raises(ValueError, match="Reconciliation not accepted"):
        views.resolve_reconcile(board, run, payload)


def test_joins_carry_finished_unrelated_nodes_only(board, tmp_path):
    run, root = _run(board, tmp_path, max_depth=3)
    a = runs.create_node(board, run["id"], question="A", parents=[root["id"]])
    b = runs.create_node(board, run["id"], question="B", parents=[root["id"]])
    root, a = _finish(board, root), _finish(board, a)

    def join(*parents):
        return {"nodes": [], "not_pursued": [], "relations": [],
                "joins": [{"parents": list(parents), "question": "Together", "rationale": "Same place"}]}

    with pytest.raises(ValueError, match="has not finished its own work"):
        views.resolve_reconcile(board, run, join(a["id"], b["id"]))
    b = _finish(board, b, "closed")
    with pytest.raises(ValueError, match="descends from"):
        views.resolve_reconcile(board, run, join(a["id"], root["id"]))
    with pytest.raises(ValueError, match="fewer than two"):
        views.resolve_reconcile(board, run, join(a["id"], a["id"]))
    resolved = views.resolve_reconcile(board, run, join(a["id"], b["id"]))
    runs.record_reconcile(board, run, 1, {**join(a["id"], b["id"]), "summary": "join"},
                          session_file=str(tmp_path / "s.jsonl"), tool_call_id="call")
    receipt = runs.apply_reconcile(board, run, 1, resolved, considered=[a["id"], b["id"]])
    joined = runs.node(board, receipt["joins"][0])
    assert joined["depth"] == 2 and views.kind(board, joined) == "join"
    # A closed node that gains a child stays closed: the child runs on its own.
    assert runs.node(board, b["id"])["status"] == "closed"


def test_a_closed_node_is_read_by_document_id_at_the_reconciliation(board, tmp_path, monkeypatch):
    """2026-09-29: the reconciliation got conclusion paths and found relations by grepping each
    conclusion for the other nodes' ids. A node's texts are indexed when it closes, and the
    reconciliation reads each conclusion by its document id."""
    from misaka.core.documents import index as corpus
    from misaka.core.research import report

    run, root = _run(board, tmp_path)
    _conclude(board, run, root, "# Conclusion\n\nThe reform failed for want of revenue.\n")
    assert runs.prepare_runner(board, "research_execution", root["id"])
    workflow._close(board, run, runs.node(board, root["id"]), "closed")
    path = views.syntheses(board, run, root)[-1]["path"]
    doc_id = corpus.sha256_file(path)[:12]
    assert corpus.resolve_doc(doc_id, workspace=run["workspace"])           # indexed at close
    [finished] = workflow._reconcile_material(board, run)["finished_nodes"]
    assert finished["conclusion_doc"] == doc_id
    # Indexing is derived: when it fails, the node closes all the same.
    monkeypatch.setattr(report, "index_run", lambda *a, **k: 1 / 0)
    child = runs.create_node(board, run["id"], question="A", parents=[root["id"]])
    assert runs.prepare_runner(board, "research_execution", child["id"])
    assert workflow._close(board, run, runs.node(board, child["id"]), "closed") == "closed"


def test_a_revised_reconciliation_needs_a_new_go_ahead(board, tmp_path):
    run, _root = _run(board, tmp_path)
    payload = {"nodes": [], "joins": [], "not_pursued": [], "relations": [], "summary": "nothing"}
    runs.record_reconcile(board, run, 1, payload, session_file=str(tmp_path / "s.jsonl"), tool_call_id="first")
    runs.approve_reconcile(board, run, 1)
    assert runs.reconcile_approved(board, run["id"], 1)
    runs.record_reconcile(board, run, 1, {**payload, "summary": "revised"}, session_file=str(tmp_path / "s.jsonl"),
                          tool_call_id="second")
    assert not runs.reconcile_approved(board, run["id"], 1)


def test_a_stopped_run_accepts_no_reconciliation(board, tmp_path):
    run, _root = _run(board, tmp_path)
    runs.request_stop(board, run["id"])
    with pytest.raises(ValueError, match="stopped or superseded run"):
        runs.record_reconcile(board, run, 1, {"summary": "late"}, session_file=str(tmp_path / "s.jsonl"),
                              tool_call_id="late")


def test_a_node_closes_when_its_own_work_is_done_and_its_options_wait_for_the_barrier(board, tmp_path):
    """2026-09-28 (user): a DAG, not a tree -- a node's status is its own work; its options are the
    reconciliation's and its children run on their own. (Depth 1 showed `closing` for hours.)"""
    run, root = _run(board, tmp_path)
    assert runs.prepare_runner(board, "research_execution", root["id"])
    root = runs.node(board, root["id"])
    _did, (first, _second) = _decide(board, run, root, "One", "Two")
    assert workflow._close(board, run, root, "closed") == "closed"
    assert runs.node(board, root["id"])["status"] == "closed"
    assert {o["id"] for o in views.reconcilable_options(board, run)} == {first, _second}
    # The barrier waits for the whole level: a node still running keeps the reconciliation off.
    child = runs.create_node(board, run["id"], question="One", parents=[root["id"]])
    assert views.reconcile_state(board, run)[0] is False
    _finish(board, child)
    assert views.reconcile_state(board, run)[0] is True


def test_views_are_written_from_the_records_and_never_read_back(board, tmp_path):
    run, root = _run(board, tmp_path)
    _finish(board, root)
    _conclude(board, run, root, "The reform failed for fiscal reasons.")
    _decide(board, run, root, "Fiscal", own=True)
    views.write_views(board, run, final=True)
    view = (tmp_path / views.graph_path(run)).read_text(encoding="utf-8")
    assert "```mermaid" in view and root["id"] in view and "this node's own line" in view
    node_view = (tmp_path / views.node_view_path(root["id"])).read_text(encoding="utf-8")
    assert "synthesis.md" in node_view and "Decisions made here" in node_view
    snapshot = json.loads((tmp_path / runs.run_path(run, views.GRAPH_SNAPSHOT)).read_text(encoding="utf-8"))
    assert snapshot["nodes"][0]["id"] == root["id"]
    # A view is rewritten, never trusted: editing it changes nothing the run reads.
    (tmp_path / views.graph_path(run)).write_text("tampered", encoding="utf-8")
    assert views.snapshot(board, run)["nodes"][0]["question"] == root["question"]


def test_the_node_view_names_its_red_team_with_both_jobs(board, tmp_path):
    """2026-09-27: the plan chose its red team as a fact-checker only, never weighing the
    divergence review she also runs; the choice and its reason are now on the node's page."""
    run, root = _run(board, tmp_path)
    runs.record_action(board, run, root, runs.plan_key(1),
                       {"red_team": {"assignee": "10043", "reason": "critic and finder of the untaken frames"}},
                       session_file=str(tmp_path / "lo.jsonl"), tool_call_id="plan-1")
    assert runs.red_team(board, run["id"], root["id"])["assignee"] == "10043"
    assert workflow._red_team_assignee(board, run, root) == "10043"
    views.write_views(board, run)
    node_view = (tmp_path / views.node_view_path(root["id"])).read_text(encoding="utf-8")
    assert "Red team and divergence review: Sister 10043 — critic and finder of the untaken frames" in node_view


def test_the_plan_contract_chooses_the_red_team_for_both_jobs():
    from misaka.core.research import commands, planner
    assert "divergence review" in planner.ROOT_CONTRACT and "`red_team.reason` says why" in planner.ROOT_CONTRACT
    schema = commands.Plan.model_json_schema()
    assert "divergence review" in schema["properties"]["red_team"]["anyOf"][0].get("description", "") \
        or "divergence review" in schema["properties"]["red_team"].get("description", "")
    assert "divergence review" in schema["$defs"]["RedTeam"]["properties"]["reason"]["description"]


def test_a_view_failure_is_logged_not_raised(board, tmp_path, monkeypatch, caplog):
    run, _root = _run(board, tmp_path)
    monkeypatch.setattr(views, "render_graph", lambda data: (_ for _ in ()).throw(OSError("disk full")))
    views.write_views(board, run)
    assert "graph views could not be written" in caplog.text


def test_an_older_board_is_refused_and_left_as_it_is(tmp_path):
    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        runs.init(con)
        con.execute("UPDATE schema_migrations SET version=16 WHERE component='research'")
        con.execute("DROP TABLE research_cards")
        if hasattr(con, "_research_schema_checked"):
            del con._research_schema_checked
    with closing(tasks.connect(str(tmp_path / "board.db"))) as con, pytest.raises(RuntimeError) as error:
        runs.init(con)
    assert "v16" in str(error.value) and "pre-release data is not migrated" in str(error.value)
    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='research_cards'").fetchone()


def test_relations_keep_their_members_as_rows_in_order(board, tmp_path):
    """v18: a relation's nodes are rows, not a JSON list, so "which relations touch this node" is a
    query; the order a relation names them in is kept, and recording it again adds nothing."""
    run, root = _run(board, tmp_path, max_depth=3)
    a = runs.create_node(board, run["id"], question="A", parents=[root["id"]])
    b = runs.create_node(board, run["id"], question="B", parents=[root["id"]])
    items = [{"kind": "converge", "nodes": [b["id"], a["id"], b["id"]], "note": "same finding"},
             {"kind": "diverge", "nodes": [a["id"], root["id"]], "note": "opposite causes"}]
    assert runs.add_relations(board, run, items) == 2
    assert runs.add_relations(board, run, items) == 0
    assert [(r["kind"], r["nodes"]) for r in runs.relations(board, run["id"])] == [
        ("converge", [b["id"], a["id"]]), ("diverge", [a["id"], root["id"]])]
    touching = sorted(r["kind"] for r in runs.relations(board, run["id"]) if a["id"] in r["nodes"])
    assert touching == ["converge", "diverge"]


def test_an_issue_records_where_it_went_in_a_column_of_its_own(board, tmp_path):
    """v18: an issue taken up by an option names the option, one another node covers names the node;
    nothing reads the same column two ways."""
    run, root = _run(board, tmp_path)
    other = runs.create_node(board, run["id"], question="Other", parents=[root["id"]])["id"]
    _decision, (option,) = _decide(board, run, root, "path")
    taken, covered, declined = (runs.add_issue(board, run["id"], node=root, task_id="d", round=1,
                                               origin="divergence", kind="k", question=q, rationale="r")
                                for q in ("taken", "covered", "declined"))
    runs.dispose(board, taken, "branch", option=option)
    runs.dispose(board, covered, "covered", covered_by=other)
    runs.dispose(board, declined, "decline", reason="no sources")
    rows = {row["question"]: row for row in runs.issues(board, run["id"])}
    assert (rows["taken"]["option_id"], rows["taken"]["covered_by"]) == (option, None)
    assert (rows["covered"]["option_id"], rows["covered"]["covered_by"]) == (None, other)
    assert [runs.destination(rows[q]) for q in ("taken", "covered", "declined")] == [
        f"option {option}", f"node {other}", None]
    with pytest.raises(graph_core.ConstraintError):              # an issue goes only where something is
        runs.dispose(board, declined, "covered", covered_by="b_nowhere")

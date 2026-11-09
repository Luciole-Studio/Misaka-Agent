"""What each research phase accepts: forks in a plan, dispositions of a review, the decision after
a divergence review, the alternatives a divergence card records, the boundary of a node's own
deliberation, and when a plan must wait for the user. Structure is checked; content never is."""
import os
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from misaka.core.network import todo, worker
from misaka.core.network.wiring.collaboration import collaboration_sections
from misaka.core.platform import cards, tasks
from misaka.core.research import commands, planner, runs
from misaka.core.session_manager import SessionManager

ROSTER = [{"id": "10032"}, {"id": "10033"}]
TASK = {"local_id": "a", "title": "A", "question": "Q", "rationale": "R", "deliverable": "a.md",
        "assignee": "10032", "assignee_reason": "fit"}


def _plan(**fields):
    return commands.Plan.model_validate({"status": "ready", "plan_markdown": "Plan", **fields}).model_dump()


def _decision(*options):
    return {"question": "Which method?", "stakes": "the answer", "options": list(options)}


def _option(label, own=False):
    return {"label": label, "premise": f"premise {label}", "own": own}


def test_a_plan_may_fork_only_where_forks_are_allowed():
    fork = _decision(_option("archival"), _option("quantitative"))
    plan = _plan(tasks=[TASK], red_team={"assignee": "10033", "reason": "r"}, decisions=[fork])
    assert planner.validate_plan(plan, ROSTER, decisions_allowed=True)["decisions"][0]["options"][1]["label"] == "quantitative"
    with pytest.raises(ValueError, match="first plan below max_depth"):
        planner.validate_plan(plan, ROSTER, decisions_allowed=False)


def test_a_pure_branch_point_needs_no_cards_but_the_root_still_names_its_red_team():
    branch_point = _plan(decisions=[_decision(_option("A"), _option("B"))])
    assert planner.validate_plan(branch_point, ROSTER, decisions_allowed=True)["tasks"] == []
    with pytest.raises(ValueError, match="red team"):
        planner.validate_plan(branch_point, ROSTER, decisions_allowed=True, root=True)
    with pytest.raises(ValueError, match="at least one task or one decision"):
        planner.validate_plan(_plan(), ROSTER, decisions_allowed=True)


@pytest.mark.parametrize("options,tasks_,message", [
    ([_option("A", own=True), _option("B", own=True)], [TASK], "more than one option"),
    ([_option("A", own=True), _option("B")], [], "own line"),
])
def test_the_own_line_is_one_option_and_needs_the_node_s_cards(options, tasks_, message):
    plan = _plan(tasks=tasks_, red_team={"assignee": "10033", "reason": "r"}, decisions=[_decision(*options)])
    with pytest.raises(ValueError, match=message):
        planner.validate_plan(plan, ROSTER, decisions_allowed=True)


def test_mainline_plus_one_new_path_is_a_fork_but_a_single_path_is_not():
    commands.Decision.model_validate(_decision(_option("as planned", own=True), _option("coalitions")))
    with pytest.raises(ValueError):
        commands.Decision.model_validate(_decision(_option("coalitions")))


@pytest.fixture
def node_run(tmp_path):
    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        run = runs.create(con, workspace=str(tmp_path), question="Why?", limits={"max_depth": 1, "max_revisions": 1})
        assert runs.acquire_driver(con, run["id"], "driver:test")
        yield SimpleNamespace(con=con, run=runs.get(con, run["id"]), root=runs.root(con, run["id"]))


class Captured(Exception):
    pass


def _validator(monkeypatch, phase):
    """The validate function a phase turn hands its tool, captured without a model turn."""
    captured = {}

    def command(con, run, cfg, worker_, node, prompt, *, validate, **_kwargs):
        captured["validate"] = validate
        raise Captured

    monkeypatch.setattr(planner, "_command", command)
    monkeypatch.setattr(planner, "review_context", lambda *a, **k: "")
    with pytest.raises(Captured):
        phase()
    return captured["validate"]


def test_dispositions_cover_every_issue_once_and_respect_the_node_s_limits(node_run, monkeypatch):
    con, run, root = node_run.con, node_run.run, node_run.root
    a = runs.add_issue(con, run["id"], node=root, task_id="t", round=1, origin="critique", kind="fact",
                       question="A?", rationale="r")
    b = runs.add_issue(con, run["id"], node=root, task_id="t", round=1, origin="critique", kind="fact",
                       question="B?", rationale="r")
    issues = runs.issues(con, run["id"], round=1)
    red = {"id": "t"}

    def check(revisions_left, node, *items):
        validate = _validator(monkeypatch, lambda: planner.dispose(con, run, {}, None, node, red, issues, round=1,
                                                                   revisions_left=revisions_left))
        return validate(commands.Dispositions.model_validate({"dispositions": list(items)}).model_dump())

    rebut = {"issue_id": b, "disposition": "rebut", "reason": "no"}
    check(1, root, {"issue_id": a, "disposition": "revise", "reason": "fix"}, rebut)
    with pytest.raises(ValueError, match="exactly one disposition"):
        check(1, root, rebut)
    with pytest.raises(ValueError, match="No revision is left"):
        check(0, root, {"issue_id": a, "disposition": "revise", "reason": "fix"}, rebut)
    # A plain error is corrected when no revision is left -- appended, not left standing (2026-09-28).
    check(0, root, {"issue_id": a, "disposition": "correct", "reason": "1707, not 1701"}, rebut)
    with pytest.raises(ValueError, match="a correction is a revise"):
        check(1, root, {"issue_id": a, "disposition": "correct", "reason": "1707"}, rebut)
    with pytest.raises(ValueError, match="another node"):
        check(1, root, {"issue_id": a, "disposition": "covered", "covered_by": root["id"], "reason": "mine"}, rebut)
    deep = runs.create_node(con, run["id"], question="At the limit", parents=[root["id"]])
    with pytest.raises(ValueError, match="cannot fork"):
        check(1, deep, {"issue_id": a, "disposition": "branch", "reason": "alt"}, rebut)


def test_the_decision_takes_up_or_declines_every_proposal_and_branch_issue(node_run, monkeypatch):
    con, run, root = node_run.con, node_run.run, node_run.root
    proposal = runs.add_issue(con, run["id"], node=root, task_id="d", round=1, origin="divergence",
                              kind="method", question="Count petitions", rationale="r")
    branch = runs.add_issue(con, run["id"], node=root, task_id="t", round=1, origin="critique",
                            kind="concept", question="The framework breaks", rationale="r")
    validate = _validator(monkeypatch, lambda: planner.decide(con, run, {}, None, root, divergence=[],
                                                              proposals=[runs.issue(con, proposal)],
                                                              branches=[runs.issue(con, branch)]))

    def check(payload):
        return validate(commands.Decide.model_validate(payload).model_dump())

    fork = {"question": "Which frame?", "stakes": "s",
            "options": [{"label": "as concluded", "premise": "p", "own": True},
                        {"label": "new frame", "premise": "q", "sources": [branch]}]}
    check({"decisions": [fork], "declined": [{"issue_id": proposal, "disposition": "decline", "reason": "a complement"}]})
    with pytest.raises(ValueError, match="exactly once"):
        check({"decisions": [fork], "declined": []})
    with pytest.raises(ValueError, match="another node"):
        check({"decisions": [fork], "declined": [{"issue_id": proposal, "disposition": "covered",
                                                  "covered_by": "b_nowhere", "reason": "?"}]})
    own_made_of = {**fork, "options": [{"label": "as concluded", "premise": "p", "own": True, "sources": [proposal]},
                                       {"label": "new frame", "premise": "q", "sources": [branch]}]}
    with pytest.raises(ValueError, match="own line"):
        check({"decisions": [own_made_of]})
    with pytest.raises(ValueError, match="Only covered takes covered_by"):
        check({"decisions": [fork], "declined": [{"issue_id": proposal, "disposition": "decline",
                                                  "covered_by": "the planned fork", "reason": "later"}]})


def test_a_proposal_an_option_waiting_to_be_opened_takes_up_is_covered_by_it(node_run, monkeypatch):
    """2026-09-29: 48 of 69 divergence proposals were declined, many as falling under an option the
    root had decided and the reconciliation had not opened yet -- `covered` could name only a node.
    Covered by that option, the proposal goes with it: the node opened for it is told it takes it up."""
    from misaka.core.research import views, workflow

    con, run, root = node_run.con, node_run.run, node_run.root
    did = runs.add_decision(con, run["id"], node=root, round=1, origin="plan", index=0, question="Which frame?",
                            stakes="s", options=[_option("structural limits"), _option("subaltern agency")])
    waiting, declined = (row["id"] for row in runs.options(con, run["id"], decision_id=did))
    runs.set_option(con, declined, reason="complements the first")
    node = runs.create_node(con, run["id"], question="Naval gap", parents=[root["id"]])
    proposal = runs.add_issue(con, run["id"], node=node, task_id="d", round=1, origin="divergence",
                              kind="theory", question="World-systems theory", rationale="r")
    validate = _validator(monkeypatch, lambda: planner.decide(con, run, {}, None, node, divergence=[],
                                                              proposals=[runs.issue(con, proposal)], branches=[]))

    def covered_by(target):
        return validate(commands.Decide.model_validate({"declined": [
            {"issue_id": proposal, "disposition": "covered", "covered_by": target, "reason": "its premise"}]}).model_dump())

    with pytest.raises(ValueError, match="option waiting to be opened"):
        covered_by(declined)
    payload = covered_by(waiting)
    assert runs.prepare_runner(con, "research_execution", node["id"])
    node = runs.node(con, node["id"])
    workflow._apply_decisions(con, run, node, node, payload)
    issue = runs.issue(con, proposal)
    assert (issue["disposition"], issue["option_id"], issue["covered_by"]) == ("covered", waiting, None)
    assert f"takes up `{proposal}`" in views.render_paths(views.snapshot(con, runs.get(con, run["id"])))
    opened = runs.create_node(con, run["id"], question="Structural limits", parents=[root["id"]])
    runs.set_option(con, waiting, node_id=opened["id"])
    assert f"takes up `{proposal}` (raised at node {node['id']}): World-systems theory" in planner.position(con, run, opened)


def test_deliverables_come_first_and_conversations_only_supplement_them():
    """2026-09-29 (user): read the deliverables; a conversation is looked up only where one is
    unclear or needs its context. The research prompts name no conversation tool: LCM is a
    pluggable extension, and its own tool guidance says how its search ranks against products
    (tests/test_project_lcm.py)."""
    for contract in (planner.RESEARCH_SISTER_DISCIPLINE, planner.SYNTHESIS_CONTRACT):
        text = " ".join(contract.split())
        assert "must read them first" in text and "Only where one is unclear or needs the context" in text
        assert "A conversation supplements a deliverable, never replaces it." in text
        assert "lcm_" not in text


def test_a_reframed_question_waits_for_the_user_even_in_an_automatic_run(node_run):
    run = node_run.con.execute("SELECT * FROM research_runs").fetchone()
    cfg = {"research_plan_approval": False}
    assert planner.plan_needs_user(cfg, run, {"reframed_question": ""}) is False
    assert planner.plan_needs_user(cfg, run, {"reframed_question": "Why did it look like failure?"}) is True


def _say(manager, text):
    return manager.appendMessage({"role": "assistant", "content": [{"type": "thinking", "thinking": text}],
                                  "timestamp": int(time.time() * 1000)})


def test_deliberation_is_the_node_s_own_turns_on_the_current_branch(tmp_path):
    manager = SessionManager.create(str(tmp_path), str(tmp_path / "s"))
    manager.appendMessage({"role": "user", "content": "go", "timestamp": 1})
    _say(manager, "INHERITED thinking about B and C")
    boundary = manager.getLeafId()
    _say(manager, "ABANDONED line")
    abandoned = manager.getLeafId()
    manager.branch(boundary)                     # the conversation left that line
    _say(manager, "OWN thinking about A")
    text = planner.deliberation_text(manager.getSessionFile(), boundary)
    assert "OWN thinking about A" in text
    assert "INHERITED" not in text and "ABANDONED" not in text
    assert abandoned != manager.getLeafId()
    lost = planner.deliberation_text(manager.getSessionFile(), "no-such-entry")
    assert "not on this conversation's current branch" in lost and "INHERITED" in lost


def _write_reply(manager, text):
    return manager.appendMessage({"role": "assistant", "content": [{"type": "text", "text": text}],
                                  "timestamp": int(time.time() * 1000)})


def test_a_re_review_s_deliberation_is_only_what_the_red_team_does_not_hold(tmp_path):
    """2026-09-27: version 2's deliberation.md ran to 168 KB -- round 1's deliberation again (the
    re-review goes on in her own conversation) and every conclusion she was given by path."""
    from misaka.core.research import workflow
    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        run = runs.create(con, workspace=str(tmp_path), question="Why?")
        root = runs.root(con, run["id"])
        manager = SessionManager.create(str(tmp_path), str(tmp_path / "s"))
        manager.appendMessage({"role": "user", "content": "go", "timestamp": 1})
        con.execute("UPDATE research_runs SET root_session=? WHERE id=?", (manager.getSessionFile(), run["id"]))
        con.execute("UPDATE research_execution SET fork_entry=? WHERE id=?", (manager.getLeafId(), root["id"]))
        run, root = runs.get(con, run["id"]), runs.node(con, root["id"])
        _say(manager, "ROUND ONE reasoning")
        _write_reply(manager, "# Conclusion v1\nFiscal limits decided it.")
        runs.write_text(con, run["id"], "synthesis", "c", runs.generated_path("synthesis.md", node_id=root["id"]),
                        "# Conclusion v1\nFiscal limits decided it.\n", branch_id=root["id"])
        first, through = workflow._deliberation(con, run, root, 1)
        assert "ROUND ONE" in first and "Fiscal limits decided it" not in first
        workflow._write(con, run, root, "deliberation", "d", "deliberation.md", first, version=1, through=through)
        _say(manager, "ROUND TWO reasoning after the review")
        _write_reply(manager, "# Conclusion v2\nFiscal and coalition limits together.")
        runs.write_text(con, run["id"], "synthesis", "c", runs.generated_path("synthesis-2.md", node_id=root["id"]),
                        "# Conclusion v2\nFiscal and coalition limits together.\n", branch_id=root["id"])
        second, _ = workflow._deliberation(con, run, root, 2)
        assert "ROUND TWO" in second and "ROUND ONE" not in second and "coalition limits" not in second
        assert "since the previous review" in second and "deliberation.md" in second
        body = planner.red_team_body(root, synthesis_path="s", plan_path="p", graph_path="g", deliberation_path="d2",
                                     rereview={"round": 2, "previous": "s1", "critiques": [], "dispositions": ""})
        assert "notes since your previous review" in body


def _running_card(tmp_path, con, monkeypatch, kind):
    run = runs.create(con, workspace=str(tmp_path), question="Why?")
    root = runs.root(con, run["id"])
    tid = cards.create(con, str(tmp_path), "Review", "## deliverable\nreview.md\n", "10033",
                       after_row=lambda tid: runs.link_task(con, run["id"], tid, kind=kind, node=root))
    out = tasks.get(con, tid)["output_dir"]
    con.execute("UPDATE tasks SET status='running', claim_lock='lock1', claim_expires=?, worker_pid=? WHERE id=?",
                (int(time.time()) + 1800, os.getpid(), tid))
    worker.record_output_baseline(con, tasks.get(con, tid))
    monkeypatch.setenv("MISAKA_USAGE_TASK_ID", tid)
    monkeypatch.setenv("MISAKA_USAGE_CLAIM_LOCK", "lock1")
    monkeypatch.setenv("MISAKA_USAGE_GENERATION", "1")
    monkeypatch.delenv("MISAKA_SISTER_OWNER_CLAIM_LOCK", raising=False)
    part = todo.TodoPart(tid, "10033")
    part._con = con
    note = next(tool for tool in part.tools if tool.name == "misaka_card_note")
    return tid, out, note


ALTERNATIVE = {"kind": "hypothesis", "proposal": "Coalitions", "premise": "politics", "rationale": "changes it"}


async def test_only_a_divergence_card_records_alternatives_and_it_must(tmp_path, monkeypatch):
    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        tid, out, note = _running_card(tmp_path, con, monkeypatch, "divergence")
        with pytest.raises(ValueError, match="red-team card"):
            await note.execute("c", {"text": "x", "issues": []}, None, None, None)
        Path(out, "review.md").write_text("# Alternatives\n", encoding="utf-8")
        with pytest.raises(worker.IncompleteSubmission, match="alternatives"):
            worker.build_submission(con, tasks.get(con, tid), "done")
        await note.execute("c", {"text": "proposals", "alternatives": [ALTERNATIVE]}, None, None, None)
        submission = worker.build_submission(con, tasks.get(con, tid), "done")
        assert submission["alternatives"][0]["proposal"] == "Coalitions" and "issues" not in submission


async def test_review_records_survive_acceptance_and_reach_their_node(tmp_path, monkeypatch):
    """2026-09-27: the alternatives were built into the submission and dropped when it was
    accepted -- the submitted payload carried only a red team's issues -- so the root node failed
    with "has not recorded alternatives" right after a divergence review that had."""
    from misaka.core.network import dispatch
    from misaka.core.research import workflow
    for kind, record, received in (("divergence", {"alternatives": [ALTERNATIVE]}, "Coalitions"),
                                   ("red_team", {"issues": [{"kind": "logic", "question": "Q?", "rationale": "R",
                                                             "priority": 1, "material": True}]}, "Q?")):
        (tmp_path / kind).mkdir()
        with closing(tasks.connect(str(tmp_path / f"{kind}.db"))) as con:
            tid, out, note = _running_card(tmp_path / kind, con, monkeypatch, kind)
            await note.execute("c", {"text": "recorded", **record}, None, None, None)
            Path(out, "review.md").write_text("# Review\n", encoding="utf-8")
            row = tasks.get(con, tid)
            prepared = dispatch.prepare_submission(row, worker.build_submission(con, row, "done"))
            assert dispatch.accept_state(con, row, prepared, generation=1, claim_lock="lock1")
            link = con.execute("SELECT run_id, branch_id FROM research_cards WHERE task_id=?", (tid,)).fetchone()
            run, node = runs.get(con, link["run_id"]), runs.node(con, link["branch_id"])
            receive = (workflow._receive_divergence(con, run, node, tasks.get(con, tid), 1) if kind == "divergence"
                       else workflow._receive_critique(con, run, node, tasks.get(con, tid), 1))
            assert receive is None
            assert [r["question"] for r in runs.issues(con, run["id"]) if r["task_id"] == tid] == [received]


async def test_a_red_team_card_cannot_record_alternatives(tmp_path, monkeypatch):
    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        _tid, _out, note = _running_card(tmp_path, con, monkeypatch, "red_team")
        with pytest.raises(ValueError, match="divergence-review card"):
            await note.execute("c", {"text": "x", "alternatives": [ALTERNATIVE]}, None, None, None)


async def test_a_finding_sent_in_the_wrong_shape_is_refused_not_dropped(tmp_path, monkeypatch):
    """2026-09-27: the prompts described a flat note (`text`, `claim_type`, `source_file`, `quote`),
    the tool took `findings=[...]` and ignored what it did not know -- every declaration of a run
    answered "Logged on the card." and none reached the ledger."""
    import pydantic
    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        tid, out, note = _running_card(tmp_path, con, monkeypatch, "research")
        flat = {"text": "France had 23 ships of the line", "claim_type": "fact",
                "source_file": "web:warhistory.org/x", "quote": "23 ships"}
        with pytest.raises(pydantic.ValidationError, match="claim_type"):
            await note.execute("c", flat, None, None, None)
        reply = await note.execute("c", {"text": "fleet size", "findings": [
            {k: v for k, v in flat.items()}], "uncertain": ["tonnage unknown"]}, None, None, None)
        assert reply["content"][0]["text"] == "Logged on the card; recorded 1 finding(s), 1 uncertainty(ies)."
        assert (await note.execute("c", {"text": "a dead end"}, None, None, None))["content"][0]["text"] \
            == "Logged on the card; nothing declared."
        with pytest.raises(pydantic.ValidationError, match="page_number"):
            await note.execute("c", {"text": "x", "findings": [{"text": "y", "page_number": 3}]}, None, None, None)
        Path(out, "review.md").write_text("# Fleet\n", encoding="utf-8")
        submission = worker.build_submission(con, tasks.get(con, tid), "done")
        assert submission["findings"][0]["quote"] == "23 ships" and submission["uncertain"] == ["tonnage unknown"]


def test_the_divergence_review_excavates_presuppositions_and_leaves_the_verdict_to_research():
    """2026-09-27 (user): the divergence review brings to light what the conclusion presupposes and what each
    choice gained, gave up and traded, without ruling on it; a possibility not taken is a fork, a gap is not."""
    from misaka.core.research.prompting import RESEARCH_LO_ORCHESTRATION
    def flat(text):
        return " ".join(text.split())

    contract = flat(planner.DIVERGENCE_CONTRACT)
    assert "## presuppositions" in contract and "excavation, not verdict" in contract
    assert "Do not rule on whether it holds: that is research." in contract
    assert "a consensus is a fact about the literature, not about the matter itself" in contract
    assert "those it gave up and those it never chose" in contract and "the gaps it left" in contract
    assert "that view is no reason to decline it" in flat(planner.DECIDE_CONTRACT)
    assert "its gaps were filled or answered in the divergence rounds" in flat(planner.DECIDE_CONTRACT)
    assert "gap: true" in contract and "fills it before anything is opened from it" in flat(contract)
    assert "gains, gives up and trades" in flat(planner.SYNTHESIS_CONTRACT)
    assert "Attend to what you do not know" in flat(RESEARCH_LO_ORCHESTRATION)


def test_a_possibility_is_opened_once_whatever_frame_proposes_it_again():
    """2026-09-27 (user): a possibility an upper level already branched must not come back as a lower
    node because another node proposed it from its own frame; a meeting of two lines is a join."""
    def flat(text):
        return " ".join(text.split())

    divergence = flat(planner.DIVERGENCE_CONTRACT)
    assert "The paths list: `{paths_path}`" in divergence and "Read it before anything else" in divergence
    assert "What is on it is not proposed again" in divergence and "name that node or option in `covered_by`" in divergence
    assert "A possibility is opened once. One on the paths list" in flat(planner.DECIDE_CONTRACT)
    assert "a possibility on it is not decided again" in flat(planner.INTERLOCUTORS)
    reconcile = flat(planner.RECONCILE_CONTRACT)
    assert "A possibility is opened once" in reconcile and "join the two nodes" in reconcile
    assert "opens only when the option answers the reason recorded then" in reconcile


def test_between_phases_last_order_leaves_the_next_phase_to_the_workflow():
    """2026-09-27: woken by a Sister's message as the last card came in, Last Order wrote her own
    conclusion.md, tried to message herself for a review and created an ally card, before the
    synthesis instruction queued behind her turn arrived."""
    from misaka.core.research.prompting import RESEARCH_LO_ORCHESTRATION
    assert "Between phases" in RESEARCH_LO_ORCHESTRATION and "create cards of your own" in RESEARCH_LO_ORCHESTRATION


def test_a_review_names_the_sort_of_its_items_in_its_own_words():
    """The sort of an issue or an alternative is description, not a switch: a closed list refused
    a divergence review's "theory" (2026-09-27)."""
    assert commands.Alternative.model_validate({**ALTERNATIVE, "kind": "world-systems theory"}).kind == "world-systems theory"
    assert commands.Alternative.model_validate({k: v for k, v in ALTERNATIVE.items() if k != "kind"}).kind == ""
    issue = {"question": "Q?", "rationale": "R", "priority": 1, "material": True}
    assert commands.Issue.model_validate({**issue, "kind": "historiographical framing"}).kind == "historiographical framing"


def test_a_revision_keeps_what_the_review_did_not_touch():
    """2026-09-27: version 3 of the root conclusion cut the sections no issue touched to a third."""
    assert "at full length" in planner.SYNTHESIS_REVISION and "goes in \"Revisions\"" in planner.SYNTHESIS_REVISION
    assert "introduced or dropped" in planner.RED_TEAM_REREVIEW


def test_a_saved_answer_is_told_it_is_the_file_itself():
    """2026-09-27: the root's synthesis.md opened with "I'll now write the full node conclusion..."."""
    assert "not a remark about what you are about to write" in planner.MARKDOWN_OUTPUT


def test_the_red_team_is_pointed_at_what_each_card_actually_read():
    assert "`SOURCES.md` lists what that card actually" in planner.RED_TEAM_CONTRACT


def test_the_prompts_describe_the_note_the_tool_takes():
    for text in (planner.RESEARCH_SISTER_DISCIPLINE, planner.task_body(TASK)):
        assert "`findings`" in text and "`uncertain`" in text


def test_the_system_prompt_describes_the_phase_tools_that_exist():
    text = "\n".join(collaboration_sections(["misaka_research_dispose", "misaka_research_decide",
                                             "misaka_research_reconcile", "misaka_research_assign"]))
    assert "misaka_research_dispose" in text and "corrections stay in the node" in text
    assert "misaka_research_reconcile" in text and "misaka_research_investigate" not in text


def test_a_dependency_is_only_for_output_a_card_needs():
    """2026-09-27: every card of the root depended on one "baseline and standards" card, so eleven waited
    while one ran; a shared standard belongs in the plan itself."""
    flat = " ".join(planner.ROOT_CONTRACT.split())
    assert "give one only when a card needs another's output as its input" in flat
    assert "belong in `plan_markdown`, so the cards start together" in flat


async def test_a_review_is_checked_against_its_own_file_before_it_is_submitted(tmp_path, monkeypatch):
    """2026-10-01 (B117): the divergence review wrote five gaps in divergence.md and recorded one;
    only the record is acted on, so four gaps were never answered and no later review found them.
    Once per attempt the reviewer is shown her record and asked to check her file against it."""
    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        tid, out, note = _running_card(tmp_path, con, monkeypatch, "divergence")
        part = todo.TodoPart(tid, "10033")
        part._con = con
        complete = next(tool for tool in part.tools if tool.name == "misaka_card_complete")
        Path(out, "review.md").write_text("# Gaps\nG1 ... `gap: true`\nG2 ... `gap: true`\n", encoding="utf-8")
        gap = {**ALTERNATIVE, "proposal": "G1: who read the governed", "gap": True}
        await note.execute("c", {"text": "proposals", "alternatives": [gap]}, None, None, None)

        with pytest.raises(ValueError, match=r"(?s)check the file you wrote.*- \[gap\] G1: who read the governed"):
            await complete.execute("c", {"summary": "done"}, None, None, None)
        assert worker.declared_completion(con, tasks.get(con, tid)) is None
        # She records the complete list, and completes.
        await note.execute("c", {"text": "proposals", "alternatives": [
            gap, {**gap, "proposal": "G2: what the clerks wrote"}]}, None, None, None)
        await complete.execute("c", {"summary": "done"}, None, None, None)
        assert worker.declared_completion(con, tasks.get(con, tid)) is not None
        assert len(worker.build_submission(con, tasks.get(con, tid), "done")["alternatives"]) == 2

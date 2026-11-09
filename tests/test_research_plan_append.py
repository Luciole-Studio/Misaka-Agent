"""A plan larger than one reply is submitted in several `misaka_research_assign` calls.

2026-09-23: the root plan turn wrote a plan longer than the reply's output cap, lost it to
truncation, and then shrank the plan to fit one call -- the tool replaced the record wholesale,
so it could not be split. An append call folds onto the recorded plan instead."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from misaka.core.platform import tasks
from misaka.core.research import commands, planner, runs
from misaka.core.research.prompting import RESEARCH_LO_ORCHESTRATION

ROSTER = [{"id": "10032"}, {"id": "10043"}]


def task(local_id, **fields):
    return {"local_id": local_id, "title": local_id, "question": "q", "rationale": "r",
            "deliverable": "findings.md", "assignee": "10032", "assignee_reason": "fits", **fields}


def first_plan():
    return {"status": "ready", "plan_markdown": "design", "tasks": [task("a")],
            "red_team": {"assignee": "10043", "reason": "critic"}}


def validate(value):
    return planner.validate_plan(value, ROSTER)


@pytest.fixture
def state(tmp_path, monkeypatch):
    con = tasks.connect(str(tmp_path / "board.db"))
    run = runs.create(con, workspace=str(tmp_path), question="fixture")
    root = runs.nodes(con, run["id"])[0]
    assert runs.acquire_driver(con, run["id"], "driver")
    runs.prepare_runner(con, "research_execution", root["id"])
    try:
        yield con, runs.get(con, run["id"]), runs.node(con, root["id"])
    finally:
        con.close()


def ctx(session_file):
    return SimpleNamespace(sessionManager=SimpleNamespace(sessionFile=session_file))


def test_merge_keeps_recorded_tasks_and_replaces_by_local_id():
    recorded = validate({**first_plan(), "tasks": [task("a"), task("b", title="old b")]})
    merged = commands.merge_plan(recorded, {"status": "ready", "plan_markdown": "", "tasks": [
        task("b", title="new b"), task("c")], "red_team": None, "reframed_question": ""})
    assert [t["local_id"] for t in merged["tasks"]] == ["a", "b", "c"]
    assert merged["tasks"][1]["title"] == "new b"
    assert merged["plan_markdown"] == "design"
    assert merged["red_team"]["assignee"] == "10043"
    replaced = commands.merge_plan(recorded, {"status": "ready", "plan_markdown": "v2", "tasks": [],
                                              "red_team": {"assignee": "10032", "reason": "swap"}})
    assert (replaced["plan_markdown"], replaced["red_team"]["assignee"]) == ("v2", "10032")


async def test_append_during_the_wait_folds_onto_the_recorded_plan(state):
    con, run, root = state
    runs.set_node(con, root["id"], status="awaiting_approval")
    session_file = str(Path(run["workspace"]) / "session.jsonl")
    runs.record_action(con, run, root, "plan", validate(first_plan()), session_file=session_file, tool_call_id="c1")
    runs.record_action(con, run, root, "start", {}, session_file=session_file, tool_call_id="s1")
    assign = next(t for t in commands.review_tools(con, run, root, validate=validate, session_file=session_file)
                  if t.name == "misaka_research_assign")
    result = await assign.execute("c2", {"status": "ready", "append": True, "tasks": [task("b", dependencies=["a"])]},
                                  None, None, ctx(session_file))
    recorded = runs.action(con, run["id"], root["id"], "plan")
    assert [t["local_id"] for t in recorded["payload"]["tasks"]] == ["a", "b"]
    assert recorded["payload"]["plan_markdown"] == "design"
    assert "append" not in recorded["payload"]
    assert recorded["tool_call_id"] == "c2"
    assert runs.action(con, run["id"], root["id"], "start") is None    # a changed plan needs a fresh go-ahead
    assert "Recorded 2 card(s) and 0 decision(s)." in result["content"][0]["text"]


async def test_append_is_validated_as_one_plan_and_a_bad_half_changes_nothing(state):
    con, run, root = state
    runs.set_node(con, root["id"], status="awaiting_approval")
    session_file = str(Path(run["workspace"]) / "session.jsonl")
    runs.record_action(con, run, root, "plan", validate(first_plan()), session_file=session_file, tool_call_id="c1")
    assign = next(t for t in commands.review_tools(con, run, root, validate=validate, session_file=session_file)
                  if t.name == "misaka_research_assign")
    with pytest.raises(ValueError, match="unknown dependencies"):
        await assign.execute("c2", {"status": "ready", "append": True, "tasks": [task("b", dependencies=["zz"])]},
                             None, None, ctx(session_file))
    recorded = runs.action(con, run["id"], root["id"], "plan")
    assert [t["local_id"] for t in recorded["payload"]["tasks"]] == ["a"]
    assert recorded["tool_call_id"] == "c1"


async def test_phase_tool_appends_within_the_planning_turn(state):
    con, run, root = state
    session_file = str(Path(run["workspace"]) / "session.jsonl")
    tool = commands.tool(con, run, root, key="plan", name="misaka_research_assign", description="d",
                         model=commands.Plan, validate=validate, session_dir=run["workspace"],
                         session_file=session_file, merge=commands.merge_plan)
    await tool.execute("c1", first_plan(), None, None, ctx(session_file))
    await tool.execute("c2", {"status": "ready", "append": True, "tasks": [task("b")]}, None, None, ctx(session_file))
    recorded = runs.action(con, run["id"], root["id"], "plan")
    assert [t["local_id"] for t in recorded["payload"]["tasks"]] == ["a", "b"]
    assert recorded["tool_call_id"] == "c2"
    # A first call marked append, with nothing recorded yet, is an ordinary first submission.
    runs.delete_action(con, run["id"], root["id"], "plan")
    await tool.execute("c3", {**first_plan(), "append": True}, None, None, ctx(session_file))
    assert runs.action(con, run["id"], root["id"], "plan")["payload"]["tasks"][0]["local_id"] == "a"


async def test_a_refused_call_says_that_nothing_was_recorded(state):
    """2026-09-27: after a refusal Last Order took her plan as accepted and sent an empty append."""
    con, run, root = state
    session_file = str(Path(run["workspace"]) / "session.jsonl")
    tool = commands.tool(con, run, root, key="plan", name="misaka_research_assign", description="d",
                         model=commands.Plan, validate=validate, session_dir=run["workspace"],
                         session_file=session_file, merge=commands.merge_plan)
    with pytest.raises(ValueError, match="Nothing from this call was recorded; send the corrected call in full"):
        await tool.execute("c1", {**first_plan(), "tasks": [task("a", deliverable="备忘录：说明")]},
                           None, None, ctx(session_file))
    assert runs.action(con, run["id"], root["id"], "plan") is None


def test_a_follow_up_round_keeps_the_first_plans_red_team():
    """2026-09-27: a follow-up plan without `red_team` was refused, though the red team is always
    the one the node's first plan named."""
    follow = {"status": "ready", "plan_markdown": "gap", "tasks": [task("b")]}
    with pytest.raises(ValueError, match="red team"):
        planner.validate_plan(follow, ROSTER)
    assert planner.validate_plan(follow, ROSTER, red_team_required=False)["red_team"] is None
    named = {**follow, "red_team": {"assignee": "10043", "reason": "r"}}
    assert planner.validate_plan(named, ROSTER, red_team_required=False)["red_team"] is None
    assert "leave\n`red_team` out" in planner.SYNTHESIS_FOLLOWUP and "no conclusion yet" in planner.SYNTHESIS_FOLLOWUP


async def test_an_accepted_follow_up_is_told_not_to_write_the_conclusion(state):
    con, run, root = state
    session_file = str(Path(run["workspace"]) / "session.jsonl")
    tool = commands.tool(con, run, root, key="plan:2", name="misaka_research_assign", description="d",
                         model=commands.Plan, validate=lambda value: planner.validate_plan(
                             value, ROSTER, red_team_required=False),
                         session_dir=run["workspace"], session_file=session_file, supersede=True,
                         reply=lambda _payload: "Accepted: round 2 is recorded. Do not write the conclusion now.")
    reply = await tool.execute("c1", {"status": "ready", "plan_markdown": "gap", "tasks": [task("b")]},
                               None, None, ctx(session_file))
    assert reply["content"][0]["text"] == "Accepted: round 2 is recorded. Do not write the conclusion now."


def test_an_append_without_a_design_is_still_refused_as_a_first_plan():
    with pytest.raises(ValueError, match="plan_markdown"):
        validate({"status": "ready", "tasks": [task("a")], "red_team": {"assignee": "10043", "reason": "r"}})


def test_the_prompts_ask_for_full_granularity_and_offer_appending():
    assert "`append: true`" in planner.ROOT_CONTRACT
    assert "coverage table" in planner.ROOT_CONTRACT
    assert "never shrink" in planner.ROOT_CONTRACT
    assert "`append: true`" in planner.PLAN_WAITS
    assert "only paces dispatch" in RESEARCH_LO_ORCHESTRATION
    assert "budget limits" not in RESEARCH_LO_ORCHESTRATION
    nudge = planner.output_limit_nudge("misaka_research_assign")
    assert "`append: true`" in nudge and "do not redo it" in nudge and "compact" not in nudge
    assert "append" not in planner.output_limit_nudge("misaka_research_investigate")


def test_an_accepted_plan_is_told_what_was_recorded_and_a_fork_left_in_prose():
    """2026-09-27: the root's plan_markdown described a methodological fork but the call recorded
    decisions=[]; the reply said only "queued", so nothing opened and nobody noticed."""
    none = {"tasks": [{"local_id": "a"}], "decisions": []}
    assert "a fork described" not in commands.plan_receipt(none, forks=False)
    told = commands.plan_receipt(none, forks=True)
    assert "Recorded 1 card(s) and 0 decision(s)." in told and "opens nothing" in told and "append=true" in told
    assert "opens nothing" not in commands.plan_receipt({"tasks": [], "decisions": [{}]}, forks=True)

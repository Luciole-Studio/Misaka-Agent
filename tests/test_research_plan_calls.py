"""How Last Order's plan calls are recorded (0.18.10 sweep): a full plan sent again in the same
turn replaces the first, an append to a follow-up round keeps the cards before it, a fork sent
twice is one fork, and a local_id given twice in one call is refused."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from misaka.core.platform import tasks
from misaka.core.research import commands, planner, runs

ROSTER = [{"id": "10032"}, {"id": "10043"}]

def task(local_id, **fields):
    return {"local_id": local_id, "title": local_id, "question": "q", "rationale": "r",
            "deliverable": "findings.md", "assignee": "10032", "assignee_reason": "fits", **fields}

def first_plan():
    return {"status": "ready", "plan_markdown": "design", "tasks": [task("a")],
            "red_team": {"assignee": "10043", "reason": "critic"}}

def validate(value):
    return planner.validate_plan(value, ROSTER, decisions_allowed=True, root=True)

@pytest.fixture
def state(tmp_path):
    con = tasks.connect(str(tmp_path / "board.db"))
    run = runs.create(con, workspace=str(tmp_path), question="fixture")
    root = runs.nodes(con, run["id"])[0]
    assert runs.acquire_driver(con, run["id"], "driver")
    runs.prepare_runner(con, "research_execution", root["id"])
    yield con, runs.get(con, run["id"]), runs.node(con, root["id"])
    con.close()

def ctx(f): return SimpleNamespace(sessionManager=SimpleNamespace(sessionFile=f))

def phase_tool(con, run, root, session_file):
    return commands.tool(con, run, root, key="plan", name="misaka_research_assign", description="d",
                         model=commands.Plan, validate=validate, session_dir=run["workspace"],
                         session_file=session_file, merge=commands.merge_plan)

async def test_a_full_plan_sent_again_in_the_turn_replaces_the_first(state):
    con, run, root = state
    sf = str(Path(run["workspace"]) / "s.jsonl")
    tool = phase_tool(con, run, root, sf)
    await tool.execute("c1", first_plan(), None, None, ctx(sf))
    await tool.execute("c2", {**first_plan(), "tasks": [task("a"), task("b")]}, None, None, ctx(sf))
    assert [t["local_id"] for t in runs.action(con, run["id"], root["id"], "plan")["payload"]["tasks"]] == ["a", "b"]


async def test_a_fork_appended_twice_is_one_fork(state):
    con, run, root = state
    sf = str(Path(run["workspace"]) / "s.jsonl")
    tool = phase_tool(con, run, root, sf)
    await tool.execute("c1", first_plan(), None, None, ctx(sf))
    fork = {"question": "which frame", "stakes": "s", "options": [
        {"label": "A", "premise": "pa"}, {"label": "B", "premise": "pb"}]}
    append = {"status": "ready", "append": True, "decisions": [fork]}
    await tool.execute("c2", append, None, None, ctx(sf))
    await tool.execute("c3", append, None, None, ctx(sf))
    assert len(runs.action(con, run["id"], root["id"], "plan")["payload"]["decisions"]) == 1


async def test_a_local_id_given_twice_in_one_append_is_refused(state):
    con, run, root = state
    sf = str(Path(run["workspace"]) / "s.jsonl")
    tool = phase_tool(con, run, root, sf)
    await tool.execute("c1", first_plan(), None, None, ctx(sf))
    with pytest.raises(ValueError, match="more than one task"):
        await tool.execute("c2", {"status": "ready", "append": True,
                                  "tasks": [task("x", title="X1"), task("x", title="X2")]}, None, None, ctx(sf))


async def test_an_append_to_a_follow_up_round_keeps_the_cards_before_it(state, monkeypatch):
    from misaka.core.research import workflow
    con, run, root = state
    sf = str(Path(run["workspace"]) / "s.jsonl")
    runs.set_node(con, root["id"], session_file=sf)
    runs.set_state(con, run["id"], root_session=sf, driver_lock="driver")
    run = runs.get(con, run["id"])
    root = runs.node(con, root["id"])
    monkeypatch.setattr(planner, "_roster", lambda cfg: ROSTER)
    monkeypatch.setattr(planner, "_lo_session", lambda run, node, *p: run["workspace"])
    tool = workflow._followup_tool(con, {}, run, root, 1)
    await tool.execute("f1", {"status": "ready", "plan_markdown": "gap plan", "tasks": [task("a2"), task("b2")]},
                       None, None, ctx(sf))
    await tool.execute("f2", {"status": "ready", "append": True, "tasks": [task("c2")]}, None, None, ctx(sf))
    payload = runs.action(con, run["id"], root["id"], "plan:2")["payload"]
    assert [t["local_id"] for t in payload["tasks"]] == ["a2", "b2", "c2"] and payload["plan_markdown"] == "gap plan"

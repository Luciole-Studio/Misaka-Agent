"""Last Order changes her node's cards while they run (GitHub issue #9).

Once accepted, a plan could not be changed: the ordinary card tools are the driver's in a research
node, and the Sisters created after the plan was read were never given a card. While a node's
research cards run, `misaka_research_cards` now records cards to add, unstarted cards to give to
another Sister and unstarted cards to cancel; it is checked as a plan's tasks are, and the driver
applies it at its next look at the cards."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from misaka.core.platform import repo, tasks
from misaka.core.research import commands, planner, runs, workflow

ROSTER = [{"id": "10032"}, {"id": "10043"}, {"id": "10077"}]


def task(local_id, **fields):
    return {"local_id": local_id, "title": local_id, "question": "q", "rationale": "r",
            "deliverable": f"{local_id}.md", "assignee": "10032", "assignee_reason": "fits", **fields}


@pytest.fixture
def executing(tmp_path, monkeypatch):
    """A root whose plan's three cards -- a, b waiting on a, and c -- are on the board, a running."""
    monkeypatch.setattr(repo, "enabled", lambda *a, **k: False)
    monkeypatch.setattr(tasks, "task_state_dir", lambda tid: str(tmp_path / "task-state" / tid))
    con = tasks.connect(str(tmp_path / "board.db"))
    run = runs.create(con, workspace=str(tmp_path), question="How was the salt monopoly run?")
    root = runs.nodes(con, run["id"])[0]
    assert runs.acquire_driver(con, run["id"], "driver")
    runs.prepare_runner(con, "research_execution", root["id"])
    run, root = runs.get(con, run["id"]), runs.node(con, root["id"])
    session_file = str(Path(run["workspace"]) / "session.jsonl")
    plan = planner.validate_plan({"status": "ready", "plan_markdown": "design",
                                  "tasks": [task("a"), task("b", dependencies=["a"]), task("c")],
                                  "red_team": {"assignee": "10043", "reason": "critic"}}, ROSTER)
    runs.record_action(con, run, root, runs.plan_key(1), plan, session_file=session_file, tool_call_id="p1")
    asyncio.run(workflow._submit_tasks(con, run, root, plan["tasks"], kind="research", round=1))
    runs.set_node(con, root["id"], status="executing", session_file=session_file)
    cards = workflow._round_cards(con, run, root, 1)
    con.execute("UPDATE tasks SET status='running' WHERE id=?", (cards["a"]["id"],))
    tool = commands.plan_change_tool(con, run, runs.node(con, root["id"]), round=1, session_file=session_file,
                                     validate=lambda change: workflow._check_plan_change(
                                         con, run, runs.node(con, root["id"]), 1, ROSTER, change))
    try:
        yield con, run, runs.node(con, root["id"]), tool, SimpleNamespace(
            sessionManager=SimpleNamespace(sessionFile=session_file))
    finally:
        con.close()


def change(tool, ctx, call_id="x1", **fields):
    return asyncio.run(tool.execute(call_id, {"reason": "the user asked", **fields}, None, None, ctx))


def test_cards_are_added_reassigned_and_cancelled_while_the_others_run(executing):
    con, run, root, tool, ctx = executing
    before = workflow._round_cards(con, run, root, 1)
    said = change(tool, ctx, add=[task("d", assignee="10077", dependencies=["a"])],
                  reassign=[{"card": before["b"]["id"], "assignee": "10077", "assignee_reason": "new Sister"}],
                  cancel=[{"card": "c", "reason": "covered by d"}])
    assert "1 card(s) to add, 1 to reassign, 1 to cancel" in said["content"][0]["text"]
    events = []
    assert asyncio.run(workflow._apply_plan_changes(con, run, root, 1, applied := [0], events.append,
                                                    (run, root)))
    after = workflow._round_cards(con, run, root, 1)
    assert sorted(after) == ["a", "b", "d"]
    assert after["a"]["id"] == before["a"]["id"], "the running card goes on untouched"
    assert after["b"]["assignee"] == "10077" and after["b"]["id"] != before["b"]["id"]
    assert after["d"]["assignee"] == "10077"
    assert tasks.get(con, before["c"]["id"]) is None
    assert set(tasks.parent_ids(con, after["b"]["id"])) == {before["a"]["id"]}, "the rebuilt card still waits on a"
    assert events[-1]["stage"] == "plan_changed" and applied == [1]
    written = Path(run["workspace"]) / workflow._artifact_path(con, run, root, "plan")
    assert "## Changed while the cards ran\n- the user asked: added d → Sister 10077; b → Sister 10077; cancelled c" \
        in written.read_text(encoding="utf-8")
    assert not asyncio.run(workflow._apply_plan_changes(con, run, root, 1, applied, events.append, (run, root)))


def test_a_card_whose_process_is_already_starting_goes_on(executing):
    """Between the runner starting a card's process and the card taking its claim, the card is
    still `ready`: removing it then would pull it out from under the process."""
    con, run, root, tool, ctx = executing
    before = workflow._round_cards(con, run, root, 1)
    change(tool, ctx, cancel=[{"card": "c", "reason": "covered"}])
    events = []

    async def pending(task_ids):
        return {before["c"]["id"]} & set(task_ids)

    assert asyncio.run(workflow._apply_plan_changes(con, run, root, 1, [0], events.append, (run, root),
                                                    pending=pending))
    assert tasks.get(con, before["c"]["id"]) is not None
    assert "Already started, so they go on as they were: c." in events[-1]["message"]


@pytest.mark.parametrize("fields, said", [
    ({"cancel": [{"card": "a", "reason": "x"}]}, "is running; only a card that has not started"),
    ({"cancel": [{"card": "zz", "reason": "x"}]}, "is not one of this round's cards"),
    ({"cancel": [{"card": "a", "reason": "x"}, {"card": "c", "reason": "x"}]}, "running"),
    ({"reassign": [{"card": "b", "assignee": "10099", "assignee_reason": "x"}]}, "not in the Sister roster"),
    ({"add": [task("c")]}, "already a card of this round"),
    ({"add": [task("e", dependencies=["zz"])]}, "unknown dependencies"),
    ({}, "Name at least one card"),
])
def test_a_change_the_plan_rules_refuse_records_nothing(executing, fields, said):
    con, run, root, tool, ctx = executing
    with pytest.raises(ValueError, match=said):
        change(tool, ctx, **fields)
    assert runs.plan_changes(con, run["id"], root["id"], 1) == []


def test_cancelling_a_card_another_waits_on_is_refused(executing):
    con, run, root, tool, ctx = executing
    con.execute("UPDATE tasks SET status='ready' WHERE id=?", (workflow._round_cards(con, run, root, 1)["a"]["id"],))
    with pytest.raises(ValueError, match="unknown dependencies"):
        change(tool, ctx, cancel=[{"card": "a", "reason": "x"}])


def test_only_while_the_cards_run(executing):
    con, _run, root, tool, ctx = executing
    runs.set_node(con, root["id"], status="synthesizing")
    with pytest.raises(ValueError, match="not running now"):
        change(tool, ctx, cancel=[{"card": "c", "reason": "x"}])

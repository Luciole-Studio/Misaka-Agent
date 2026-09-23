"""Per-run plan approval survives setup, retries, new connections and node boundaries."""

import asyncio
import json
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from misaka.core.platform import cards, repo, tasks
from misaka.core.research import planner, runs, workflow
from misaka.core.research.wiring import research


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "_commit", lambda *a: None)
    monkeypatch.setattr(repo, "enabled", lambda *a: False)
    monkeypatch.setattr(tasks, "task_state_dir", lambda tid: str(tmp_path / "state" / tid))
    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        runs.init(con)
        yield con


@pytest.mark.parametrize("value", [None, 0, 1, "false", "true", "automatic", [], {}])
def test_saved_approval_requires_a_real_boolean(value):
    with pytest.raises(ValueError, match="plan_approval"):
        runs.normalize_limits({"plan_approval": value})


@pytest.mark.parametrize("saved", [None, False, True])
@pytest.mark.parametrize("default", [False, True])
def test_reopened_run_uses_saved_mode_or_legacy_default(board, tmp_path, saved, default):
    limits = {} if saved is None else {"plan_approval": saved}
    run = runs.create(board, workspace=str(tmp_path), question="Approval policy", limits=limits)
    original = run["limits_json"]
    runs.set_state(board, run["id"], status="waiting_input")
    with closing(tasks.connect(str(tmp_path / "board.db"))) as restored:
        row = runs.get(restored, run["id"])
        cfg = {"research_plan_approval": default}
        expected = default if saved is None else saved
        assert planner.plan_waits_for_user(cfg, row) is expected
        assert planner.plan_approval_prompt(cfg, row) == (
            planner.PLAN_WAITS if expected else planner.PLAN_AUTOMATIC)
        assert row["limits_json"] == original
        assert ("plan_approval" in json.loads(original)) is (saved is not None)
        if saved is not None:
            assert runs.limits(row)["plan_approval"] is saved
        resumed = runs.resume(restored, run["id"])
        assert resumed["status"] == "active" and resumed["limits_json"] == original
        assert planner.plan_waits_for_user(cfg, resumed) is expected
    assert planner.plan_waits_for_user(cfg) is default


@pytest.fixture
async def chat(board, tmp_path, monkeypatch):
    state = SimpleNamespace(cfg={}, notices=[], messages=[], user_messages=[], questions=[], driven=[],
                            result={"answers": {}}, fail_start=False,
                            started=asyncio.Event(), startup_failed=asyncio.Event())
    monkeypatch.setattr(research, "_con", lambda: board)
    monkeypatch.setattr(research, "_cfg", lambda: dict(state.cfg))
    monkeypatch.delenv("MISAKA_NET_PANE", raising=False)

    def init_project(*args, **kwargs):
        if state.fail_start:
            state.fail_start = False
            raise OSError("fixture startup failure")

    def send_message(payload, options):
        state.messages.append(payload)
        if payload.get("details", {}).get("stage") == "startup_error":
            state.startup_failed.set()

    async def drive(con, cfg, spawner, **kwargs):
        state.driven.append(runs.get(con, kwargs["run_id"]))
        state.started.set()
        return {"reason": "waiting_input", "questions": ["Fixture scope?"]}

    async def custom(factory):
        component = factory(None, None, None, lambda result: None)
        state.questions[:] = component.questions
        return state.result

    monkeypatch.setattr(cards, "init_project", init_project)
    monkeypatch.setattr(workflow, "run", drive)
    state.part = research.ResearchPart()
    state.part.attach(SimpleNamespace(moments=SimpleNamespace(
        send_message=send_message, send_user_message=state.user_messages.append)))
    state.ctx = SimpleNamespace(
        cwd=str(tmp_path), isIdle=lambda: True, sessionManager=SimpleNamespace(sessionId="fixture"),
        ui=SimpleNamespace(custom=custom, notify=lambda text, kind: state.notices.append((text, kind))))
    state.command = state.part.commands[0].handler
    try:
        yield state
    finally:
        await state.part._cleanup({}, state.ctx)


@pytest.mark.parametrize("default", [False, True])
@pytest.mark.parametrize("entry", ["picker", "picker-default", "pending", "direct", "retry"])
async def test_chat_mode_survives_pending_and_startup_retry(chat, board, tmp_path, default, entry):
    chat.cfg["research_plan_approval"] = default
    expected = not default if entry in {"picker", "retry"} else default
    label = "Require approval" if expected else "Automatic"
    mode = "required" if expected else "automatic"
    if entry in {"picker", "retry"}:
        chat.result["answers"][research._APPROVAL_QUESTION] = label
    chat.fail_start = entry == "retry"
    raw = "Fixture question" if entry == "direct" else "2" if entry == "pending" else ""
    await chat.command(raw, chat.ctx)
    if entry not in {"pending", "direct"}:
        assert [q["question"] for q in chat.questions] == [
            research._DEPTH_QUESTION, research._PARALLEL_QUESTION,
            research._SISTER_PARALLEL_QUESTION, research._ROUNDS_QUESTION, research._APPROVAL_QUESTION]
        expected_labels = ["Require approval", "Automatic"] if default else ["Automatic", "Require approval"]
        assert [option["label"] for option in chat.questions[-1]["options"]] == expected_labels
    # Selecting a mode is a per-run snapshot, not a write to the global setting.
    assert chat.cfg["research_plan_approval"] is default
    chat.cfg["research_plan_approval"] = not expected
    if entry != "direct":
        await chat.command("status", chat.ctx)
        assert f"plan approval {mode}" in chat.notices[-1][0]
        assert await chat.part.input({"text": "Fixture question", "source": "user"}, chat.ctx) == {"action": "handled"}
    if entry == "retry":
        await asyncio.wait_for(chat.startup_failed.wait(), 3)
        await asyncio.sleep(0)  # Let the failed startup task's done callback remove it.
        await chat.command("status", chat.ctx)
        assert f"plan approval {mode}" in chat.notices[-1][0]
        await chat.part.input({"text": "Fixture question", "source": "user"}, chat.ctx)
    await asyncio.wait_for(chat.started.wait(), 3)
    await asyncio.sleep(0)
    assert len(chat.driven) == 1
    run = chat.driven[0]
    assert runs.limits(run)["plan_approval"] is expected
    assert f"plan approval {mode}" in research._status(board, run["id"], str(tmp_path))
    assert any(f"plan approval {mode}" in text for text, _kind in chat.notices)


@pytest.mark.parametrize("action", ["cancel", "clarify", "invalid"])
async def test_dismissed_or_invalid_picker_does_not_arm_research(chat, board, action):
    chat.cfg["research_plan_approval"] = True
    chat.result = ({"answers": {research._APPROVAL_QUESTION: "whatever"}}
                   if action == "invalid" else {"action": action})
    await chat.command("", chat.ctx)
    assert await chat.part.input({"text": "Ordinary message", "source": "user"}, chat.ctx) == {"action": "continue"}
    assert runs.listing(board) == []
    assert chat.driven == []
    assert bool(chat.user_messages) is (action == "clarify")
    if action == "invalid":
        assert chat.notices[-1][1] == "error"
    # A dismissed selection must not leak a mode into the next explicit activation.
    chat.cfg["research_plan_approval"] = False
    await chat.command("2 Fixture question", chat.ctx)
    await asyncio.wait_for(chat.started.wait(), 3)
    assert runs.limits(chat.driven[0])["plan_approval"] is False


@pytest.mark.parametrize("cancel_command", ["stop", ""])
async def test_pending_stop_or_toggle_discards_selected_mode(chat, board, cancel_command):
    chat.cfg["research_plan_approval"] = True
    chat.result = {"answers": {research._APPROVAL_QUESTION: "Automatic"}}
    await chat.command("", chat.ctx)
    await chat.command(cancel_command, chat.ctx)
    assert await chat.part.input({"text": "Ordinary message", "source": "user"}, chat.ctx) == {"action": "continue"}
    assert runs.listing(board) == []
    await chat.command("2 Fixture question", chat.ctx)
    await asyncio.wait_for(chat.started.wait(), 3)
    assert runs.limits(chat.driven[0])["plan_approval"] is True


@pytest.mark.parametrize("saved", [False, True])
@pytest.mark.parametrize("fork", [False, True])
@pytest.mark.parametrize("phase", ["initial", "followup"])
def test_initial_and_followup_prompts_use_saved_mode(board, tmp_path, monkeypatch, saved, fork, phase):
    run = runs.create(board, workspace=str(tmp_path), question="Prompt policy", limits={"plan_approval": saved})
    root = runs.nodes(board, run["id"])[0]
    node = runs.create_node(board, run["id"], trigger="Child", parent_id=root["id"], depth=1) if fork else root
    monkeypatch.setattr(planner, "_roster", lambda cfg: [])
    monkeypatch.setattr(planner, "_lo_session", lambda *a: str(tmp_path / "session"))
    cfg = {"research_plan_approval": not saved}
    if phase == "initial":
        call = Mock(return_value=({"payload": {}, "session_file": "fixture.jsonl"}, "raw"))
        monkeypatch.setattr(planner, "_command", call)
        planner.plan(run, cfg, object(), node, con=board)
        prompt = call.call_args.args[5]
    else:
        monkeypatch.setattr(planner, "task_sources", lambda *a: {})
        monkeypatch.setattr(planner, "find_most_recent_session", lambda *a: None)
        call = Mock(return_value=(None, "Conclusion", None))
        monkeypatch.setattr(planner, "_call", call)
        planner.synthesize(board, run, cfg, object(), node, [], followup=object(), left=1)
        prompt = call.call_args.args[2]
    assert (planner.PLAN_WAITS if saved else planner.PLAN_AUTOMATIC) in prompt
    assert (planner.PLAN_AUTOMATIC if saved else planner.PLAN_WAITS) not in prompt


class CardsSubmitted(Exception):
    pass


@pytest.mark.parametrize("saved", [False, True])
@pytest.mark.parametrize("fork", [False, True])
@pytest.mark.parametrize("round_number", [1, 2])
async def test_driver_uses_saved_mode_for_each_node_and_round(board, tmp_path, monkeypatch, saved, fork, round_number):
    run = runs.create(board, workspace=str(tmp_path), question="Driver policy", limits={"plan_approval": saved})
    root = runs.nodes(board, run["id"])[0]
    node = runs.create_node(board, run["id"], trigger="Child", parent_id=root["id"], depth=1) if fork else root
    plan = {"status": "ready", "plan_markdown": "Fixture plan", "tasks": [], "red_team": {}}
    runs.record_action(board, run, node, runs.plan_key(round_number), plan,
                       session_file="fixture.jsonl", tool_call_id="fixture-plan")
    wait = AsyncMock(return_value="stopped")
    submit = AsyncMock(side_effect=CardsSubmitted)
    monkeypatch.setattr(workflow, "_await_approval", wait)
    monkeypatch.setattr(workflow, "_submit_tasks", submit)
    monkeypatch.setattr(planner, "publish_project_brief", lambda *a, **k: None)
    with closing(tasks.connect(str(tmp_path / "board.db"))) as restored:
        operation = workflow._expand_owned(
            restored, {"research_plan_approval": not saved}, None, None,
            runs.get(restored, run["id"]), runs.node(restored, node["id"]),
            context=None, tool_call_id="fixture", poll_seconds=0, progress=None)
        if saved:
            assert await operation == "stopped"
            wait.assert_awaited_once()
            assert wait.await_args.kwargs["round"] == round_number
            submit.assert_not_awaited()
        else:
            with pytest.raises(CardsSubmitted):
                await operation
            wait.assert_not_awaited()
            submit.assert_awaited_once()
            assert submit.await_args.kwargs["round"] == round_number


async def test_automatic_mode_still_stops_for_clarification(board, tmp_path, monkeypatch):
    run = runs.create(board, workspace=str(tmp_path), question="Clarification", limits={"plan_approval": False})
    node = runs.nodes(board, run["id"])[0]
    plan = {"status": "clarify", "plan_markdown": "Need scope", "clarifying_questions": ["Which scope?"]}
    runs.record_action(board, run, node, "plan", plan, session_file="fixture.jsonl", tool_call_id="fixture-plan")
    wait, submit = AsyncMock(), AsyncMock()
    monkeypatch.setattr(workflow, "_await_approval", wait)
    monkeypatch.setattr(workflow, "_submit_tasks", submit)
    result = await workflow._expand_owned(
        board, {"research_plan_approval": False}, None, None, run, node,
        context=None, tool_call_id="fixture", poll_seconds=0, progress=None)
    assert result["reason"] == "waiting_input" and result["questions"] == ["Which scope?"]
    assert runs.node(board, node["id"])["status"] == "waiting_input"
    wait.assert_not_awaited()
    submit.assert_not_awaited()

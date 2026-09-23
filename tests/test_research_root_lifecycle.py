"""The real driver owns one root through node expansion and final adjudication; no model calls."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from misaka.core.platform import tasks
from misaka.core.research import node, report, runs, window, workflow


@pytest.fixture
def root_run(tmp_path, monkeypatch):
    monkeypatch.delenv("MISAKA_NET_PANE", raising=False)
    monkeypatch.setattr(runs, "_commit", lambda *a: None)
    monkeypatch.setattr(workflow, "_bundle", lambda *a, **k: None)
    monkeypatch.setattr(workflow, "_try_refresh_workspace_index", lambda *a: None)
    monkeypatch.setattr(workflow, "settle_done_tasks", lambda *a, **k: None)
    monkeypatch.setattr(workflow, "_settle_stopped_tasks", AsyncMock())
    con = tasks.connect(str(tmp_path / "board.db"))
    run = runs.create(con, workspace=str(tmp_path), question="Fixture root question")
    root = runs.nodes(con, run["id"])[0]
    session = SimpleNamespace(sessionManager=SimpleNamespace(sessionFile=str(tmp_path / "root.jsonl")))
    owner = SimpleNamespace(session=session, session_file=session.sessionManager.sessionFile,
                            close=AsyncMock(), headless=True)
    events = []

    @asynccontextmanager
    async def resident(db, cfg, current, root_node):
        assert current["driver_lock"] and root_node["runner_key"]
        assert root_node["runner_pid"] is None
        events.append("open")
        try:
            yield owner
        finally:
            await owner.close()
            events.append("dispose")

    monkeypatch.setattr(window, "node_session", resident)
    monkeypatch.setattr(window, "WindowLO", lambda supplied, check, *, describe: owner)
    runner = SimpleNamespace(close=lambda: events.append("runner-close"))
    monkeypatch.setattr(node, "HeadlessRunner", lambda *a: runner)
    monkeypatch.setattr(report, "review_receipt", lambda *a: {})
    monkeypatch.setattr(workflow, "_submit_tasks", AsyncMock(return_value={"@final-review": "fixture-review"}))

    def assert_owner(stage, lo):
        assert lo is owner and "dispose" not in events
        assert runs.get(con, run["id"])["root_session"] == owner.session_file
        events.append(stage)

    async def expand(db, cfg, active_runner, lo, current, root_node, **kwargs):
        assert_owner("expand", lo)
        assert kwargs["session"] is session and active_runner is runner
        runs.record_action(con, current, root_node, "plan", {"red_team": {"assignee": "10032"}},
                           session_file=owner.session_file, tool_call_id="fixture-plan")
        runs.set_node(con, root["id"], status="closed")
        return "closed"

    def prepare(db, current, cfg, lo, **kwargs):
        assert_owner("draft", lo)
        assert runs.node(con, root["id"])["status"] == "closed"
        return {"id": "draft", "sha256": "fixture", "path": str(tmp_path / "draft.md")}

    async def review(*args, **kwargs):
        assert_owner("review", owner)
        assert kwargs["session"] is session
        await asyncio.sleep(0)
        return "done"

    def finalize(db, current, cfg, lo, **kwargs):
        assert_owner("final", lo)
        aid, path = runs.write_text(con, run["id"], "final", "Final", runs.run_path(current, "final.md"), "Fixture result")
        return {"artifact": aid, "path": path}

    monkeypatch.setattr(workflow, "_expand", expand)
    monkeypatch.setattr(report, "prepare", prepare)
    monkeypatch.setattr(workflow, "_drive_tasks", review)
    monkeypatch.setattr(report, "finalize", finalize)
    try:
        yield con, run, root, owner, events
    finally:
        con.close()


@pytest.mark.parametrize("foreground", [False, True])
async def test_same_root_handles_expansion_draft_review_and_final(root_run, foreground):
    con, run, root, owner, events = root_run
    spawner = SimpleNamespace(spawn=lambda *a: pytest.fail("Root must not become a child process"))
    result = await workflow.run(con, {}, spawner, run_id=run["id"],
                                session=owner.session if foreground else None)
    assert result["reason"] == "done"
    assert events == (["expand", "draft", "review", "final", "runner-close"] if foreground else
                      ["open", "expand", "draft", "review", "final", "dispose", "runner-close"])
    owner.close.assert_awaited_once()
    assert runs.get(con, run["id"])["driver_lock"] is None
    assert runs.node(con, root["id"])["runner_pid"] is None
    assert Path(result["final"]["path"]).read_text() == "Fixture result"


@pytest.mark.parametrize("outcome", ["error", "cancel", "waiting_input"])
async def test_owned_root_disposes_on_all_early_exits(root_run, monkeypatch, outcome):
    con, run, _root, owner, events = root_run

    async def expand(*args, **kwargs):
        if outcome == "error":
            raise RuntimeError("fixture expansion failed")
        if outcome == "cancel":
            raise asyncio.CancelledError
        return {"reason": "waiting_input", "questions": ["Which period?"]}

    monkeypatch.setattr(workflow, "_expand", expand)
    if outcome == "error":
        with pytest.raises(RuntimeError, match="fixture expansion failed"):
            await workflow.run(con, {}, object(), run_id=run["id"])
    elif outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await workflow.run(con, {}, object(), run_id=run["id"])
    else:
        result = await workflow.run(con, {}, object(), run_id=run["id"])
        assert result["reason"] == "waiting_input"
    assert events == ["open", "dispose", "runner-close"]
    owner.close.assert_awaited_once()
    assert runs.get(con, run["id"])["driver_lock"] is None


async def test_resume_keeps_saved_root_session_through_finalization(root_run, monkeypatch):
    con, run, root, owner, events = root_run
    runs.set_state(con, run["id"], root_session=owner.session_file)
    runs.record_action(con, runs.get(con, run["id"]), root, "plan", {"red_team": {"assignee": "10032"}},
                       session_file=owner.session_file, tool_call_id="saved-plan")
    runs.set_state(con, run["id"], status="failed")
    runs.set_node(con, root["id"], status="closed")
    monkeypatch.setattr(workflow, "_expand", AsyncMock(side_effect=AssertionError("Closed root must not repeat research")))
    result = await workflow.run(con, {}, object(), run_id=run["id"], resume=True)
    assert result["reason"] == "done"
    assert events == ["open", "draft", "review", "final", "dispose", "runner-close"]
    assert runs.get(con, run["id"])["root_session"] == owner.session_file


async def test_root_stays_alive_while_fork_level_runs(root_run, monkeypatch):
    con, run, root, owner, events = root_run
    expand_root = workflow._expand
    spawner = object()

    async def expand(*args, **kwargs):
        await expand_root(*args, **kwargs)
        runs.create_node(con, run["id"], trigger="Fixture issue", parent_id=root["id"], depth=1)
        return "closed"

    async def forks(db, cfg, active_spawner, current, level, **kwargs):
        assert active_spawner is spawner
        assert all(n["parent_id"] == root["id"] for n in level)
        assert "dispose" not in events
        owner.close.assert_not_awaited()
        events.append("forks")
        await asyncio.sleep(0)
        for child in level:
            runs.set_node(con, child["id"], status="closed")
        return "done"

    monkeypatch.setattr(workflow, "_expand", expand)
    monkeypatch.setattr(workflow, "_expand_level", forks)
    result = await workflow.run(con, {}, spawner, run_id=run["id"])
    assert result["reason"] == "done"
    assert events == ["open", "expand", "forks", "draft", "review", "final", "dispose", "runner-close"]


@pytest.mark.parametrize("stage", ["open", "runner-init", "runner-close"])
async def test_root_startup_and_cleanup_failures_release_driver(root_run, monkeypatch, stage):
    con, run, _root, owner, events = root_run

    def fail(*args):
        raise RuntimeError("fixture " + stage)

    if stage == "open":
        @asynccontextmanager
        async def broken_session(*args):
            fail()
            yield
        monkeypatch.setattr(window, "node_session", broken_session)
    elif stage == "runner-init":
        monkeypatch.setattr(node, "HeadlessRunner", fail)
    else:
        monkeypatch.setattr(node, "HeadlessRunner", lambda *a: SimpleNamespace(close=fail))
    with pytest.raises(RuntimeError, match="fixture " + stage):
        await workflow.run(con, {}, object(), run_id=run["id"])
    assert runs.get(con, run["id"])["driver_lock"] is None
    if stage == "open":
        owner.close.assert_not_awaited()
    else:
        owner.close.assert_awaited_once()
        assert "dispose" in events


@pytest.mark.parametrize("approval", [False, True])
def test_cli_keeps_approval_policy_and_prints_progress(root_run, monkeypatch, capsys, approval):

    from misaka.cli import app
    from misaka.core.research import planner

    con, run, _root, _owner, _events = root_run
    monkeypatch.setattr(app, "current_config", lambda: {"db": "fixture", "research_plan_approval": approval})
    monkeypatch.setattr(app.db, "connect", lambda *a: con)
    monkeypatch.setattr(planner, "sister_catalog", lambda *a: [{"id": "10032"}])

    async def drive(db, cfg, spawner, **kwargs):
        assert cfg["research_plan_approval"] is approval
        assert kwargs["resume"]
        await workflow._progress(kwargs["progress"], "plan_review", "Fixture attach instructions")
        return {"reason": "waiting_input", "questions": ["Fixture scope question?"]}

    monkeypatch.setattr(workflow, "run", drive)
    app._cmd_research(SimpleNamespace(node=None, resume=run["id"]))
    output = capsys.readouterr().out
    assert "Fixture attach instructions" in output
    assert "Fixture scope question?" in output
    assert "Unattended run" not in output

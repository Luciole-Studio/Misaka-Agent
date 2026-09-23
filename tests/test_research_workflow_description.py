"""Research descriptions follow the owning window, not one entry-point's callbacks."""
import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from misaka.core.network.wiring.capabilities import SisterCapabilitiesPart
from misaka.core.platform import tasks
from misaka.core.research import runs, window, workflow
from misaka.core.session_control import SessionControl


@pytest.fixture
def research(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "_commit", lambda *args: None)
    con = tasks.connect(str(tmp_path / "board.db"))
    run = runs.create(con, workspace=str(tmp_path), question="Description fixture")
    root = runs.nodes(con, run["id"])[0]
    publisher = SisterCapabilitiesPart(str(tmp_path), [])
    session = SimpleNamespace(
        sessionManager=SimpleNamespace(sessionFile=str(tmp_path / "root.jsonl")),
        moments=SimpleNamespace(parts=[publisher]), isIdle=True, isStreaming=False,
        waitForIdle=AsyncMock())
    control = SessionControl(session, None)
    session.moments.parts.append(SimpleNamespace(control=control))
    try:
        yield con, run, root, session, control, publisher
    finally:
        con.close()


@pytest.mark.parametrize("outcome", ["normal", "cancel", "replacement", "cleanup_error"])
async def test_window_description_restores_on_exit_without_touching_ingress(research, outcome):
    con, run, root, session, control, publisher = research
    previous = control.describe
    original_input, original_check = control.on_input, control.check_active
    replacement = lambda: {"owner": "next"}
    if outcome == "cleanup_error":
        @contextmanager
        def broken_snapshot():
            yield
            raise RuntimeError("snapshot cleanup failed")
        publisher.snapshot = broken_snapshot
    describe = lambda: window.node_description(con, root["id"])
    owner = window.WindowLO(session, lambda: None, describe=describe)
    assert control.describe is describe
    assert control.describe() == {
        "run_id": run["id"], "node": root["id"], "depth": 0,
        "run_phase": "created", "run_status": "active", "node_phase": "queued"}
    runs.set_node(con, root["id"], status="closed")
    runs.set_state(con, run["id"], phase="finalizing")
    assert control.describe()["node_phase"] == "closed"
    assert control.describe()["run_phase"] == "finalizing"
    assert "phase" not in control.describe()
    assert control.on_input is original_input and control.check_active is original_check
    if outcome == "replacement":
        control.describe = replacement
    if outcome == "cancel":
        entered = asyncio.Event()

        async def pending():
            entered.set()
            try:
                await asyncio.Future()
            finally:
                await asyncio.sleep(0)
        task = asyncio.create_task(pending())
        owner.pending.add(task)
        await entered.wait()
        closing = asyncio.create_task(owner.close())
        await asyncio.sleep(0)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert task.done()
    elif outcome == "cleanup_error":
        with pytest.raises(RuntimeError, match="snapshot cleanup failed"):
            await owner.close()
    else:
        await owner.close()
    await owner.close()  # ExitStack cleanup is idempotent even after an exit error.
    assert control.describe is (replacement if outcome == "replacement" else previous)
    assert control.on_input is original_input and control.check_active is original_check
    con.close()
    control.describe()  # no stale callback may read the now-closed research database


async def test_resumed_window_rebinds_live_run_and_node_status(research):
    con, run, root, session, control, _ = research
    previous = control.describe
    for phase, status in [("waiting_input", "waiting_input"), ("active", "active")]:
        runs.set_state(con, run["id"], phase=phase, status=status)
        owner = window.WindowLO(session, lambda: None,
                                describe=lambda: window.node_description(con, root["id"]))
        assert control.describe()["run_phase"] == phase
        assert control.describe()["run_status"] == status
        await owner.close()
        assert control.describe is previous


@pytest.mark.parametrize("child", [False, True])
@pytest.mark.parametrize("outcome", ["normal", "error", "cancel"])
async def test_headless_root_and_branch_share_live_description(research, monkeypatch, child, outcome):
    from misaka.core import wiring
    from misaka.core.platform import session as platform_session
    from misaka.core.research import planner

    con, run, root, session, control, _ = research
    previous = control.describe
    branch = runs.create_node(con, run["id"], trigger="Child", parent_id=root["id"], depth=1) if child else root
    assert runs.acquire_driver(con, run["id"], "driver")
    runs.prepare_runner(con, "research_branches", branch["id"])
    run, branch = runs.get(con, run["id"]), runs.node(con, branch["id"])
    monkeypatch.setattr(wiring, "role_session_setup", lambda *args, **kwargs: ([], object(), {}))
    monkeypatch.setattr(planner, "_lo_session", lambda *args: run["workspace"])
    monkeypatch.setattr(platform_session, "open_session", AsyncMock(return_value=(object(), session, None)))
    disposed = []

    async def dispose(runtime):
        assert control.describe is previous
        disposed.append(runtime)
    monkeypatch.setattr(platform_session, "dispose", dispose)
    async def drive():
        async with window.node_session(con, {"roles_root": run["workspace"], "db": "fixture"}, run, branch):
            description = control.describe()
            assert description == {"run_id": run["id"], "node": branch["id"], "depth": int(child),
                                   "run_phase": "created", "run_status": "active", "node_phase": "queued"}
            assert control.on_input is not None
            control.check_active()
            runs.set_node(con, branch["id"], status="executing")
            assert control.describe()["node_phase"] == "executing"
            if outcome == "error":
                raise RuntimeError("headless failed")
            if outcome == "cancel":
                raise asyncio.CancelledError
    if outcome == "normal":
        await drive()
    else:
        with pytest.raises(RuntimeError if outcome == "error" else asyncio.CancelledError):
            await drive()
    assert control.describe is previous
    assert len(disposed) == 1


@pytest.mark.parametrize("outcome", ["waiting", "error", "cancel"])
async def test_foreground_workflow_binds_and_restores_description(research, monkeypatch, outcome):
    from misaka.core.research import node

    con, run, root, session, control, _ = research
    previous = control.describe
    monkeypatch.delenv("MISAKA_NET_PANE", raising=False)
    monkeypatch.setattr(workflow, "_bundle", lambda *args, **kwargs: None)
    monkeypatch.setattr(workflow, "_try_refresh_workspace_index", lambda *args: None)
    monkeypatch.setattr(workflow, "_settle_stopped_tasks", AsyncMock())
    monkeypatch.setattr(workflow, "settle_done_tasks", lambda *args, **kwargs: None)
    monkeypatch.setattr(node, "HeadlessRunner", lambda *args: SimpleNamespace(close=lambda: None))

    async def expand(*args, **kwargs):
        assert control.describe() == {"run_id": run["id"], "node": root["id"], "depth": 0,
                                      "run_phase": "active", "run_status": "active", "node_phase": "queued"}
        if outcome == "error":
            raise RuntimeError("expand failed")
        if outcome == "cancel":
            raise asyncio.CancelledError
        return {"reason": "waiting_input", "questions": ["Which scope?"]}
    monkeypatch.setattr(workflow, "_expand", expand)
    if outcome == "waiting":
        assert (await workflow.run(con, {}, object(), run_id=run["id"], session=session))["reason"] == "waiting_input"
    else:
        with pytest.raises(RuntimeError if outcome == "error" else asyncio.CancelledError):
            await workflow.run(con, {}, object(), run_id=run["id"], session=session)
    assert control.describe is previous
    assert runs.get(con, run["id"])["driver_lock"] is None


@pytest.mark.parametrize("resume", [False, True])
async def test_root_description_outlives_closed_node_through_report_and_resume(research, monkeypatch, resume):
    from misaka.core.research import node, report

    con, run, root, session, control, _ = research
    previous = control.describe
    monkeypatch.delenv("MISAKA_NET_PANE", raising=False)
    monkeypatch.setattr(workflow, "_bundle", lambda *args, **kwargs: None)
    monkeypatch.setattr(workflow, "_try_refresh_workspace_index", lambda *args: None)
    monkeypatch.setattr(workflow, "settle_done_tasks", lambda *args, **kwargs: None)
    monkeypatch.setattr(node, "HeadlessRunner", lambda *args: SimpleNamespace(close=lambda: None))
    runs.record_action(con, run, root, "plan", {"red_team": {"assignee": "10032"}},
                       session_file=session.sessionManager.sessionFile, tool_call_id="plan")
    if resume:
        runs.set_node(con, root["id"], status="closed")
        runs.set_state(con, run["id"], status="failed", phase="finalizing")

    async def expand(*args, **kwargs):
        assert not resume, "Resume must not repeat a closed node"
        assert control.describe()["run_phase"] == "active"
        runs.set_node(con, root["id"], status="closed")
        return "closed"
    monkeypatch.setattr(workflow, "_expand", expand)
    seen = []

    def check_finalizing(stage):
        description = control.describe()
        assert description["run_phase"] == "finalizing"
        assert description["run_status"] == "active"
        assert description["node_phase"] == "closed"
        seen.append(stage)

    def prepare(*args, **kwargs):
        check_finalizing("draft")
        return {"id": "draft", "sha256": "fixture", "path": str(run["workspace"] + "/draft.md")}

    async def review(*args, **kwargs):
        check_finalizing("review")
        return "done"

    def finalize(*args, **kwargs):
        check_finalizing("final")
        aid, path = runs.write_text(con, run["id"], "final", "Final", runs.run_path(run, "final.md"), "Fixture")
        return {"artifact": aid, "path": path}
    monkeypatch.setattr(report, "prepare", prepare)
    monkeypatch.setattr(workflow, "_submit_tasks", AsyncMock(return_value={"@final-review": "review"}))
    monkeypatch.setattr(workflow, "_drive_tasks", review)
    monkeypatch.setattr(report, "review_receipt", lambda *args: {})
    monkeypatch.setattr(report, "finalize", finalize)
    result = await workflow.run(con, {}, object(), run_id=run["id"], session=session, resume=resume)
    assert result["reason"] == "done"
    assert seen == ["draft", "review", "final"]
    assert control.describe is previous
    assert runs.get(con, run["id"])["status"] == "done"

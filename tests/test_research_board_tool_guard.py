"""The model's tool filter and the Board's execution boundary agree on Research ownership."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from misaka.core.network.sister_runtime import SisterRuntime
from misaka.core.network.wiring import network
from misaka.core.platform import cards, tasks
from misaka.core.research import runs
from misaka.core.wiring import ToolCollector


@pytest.fixture
def board(tmp_path, monkeypatch):
    for key in ("MISAKA_NET_PANE", "MISAKA_DM_CARD_ALLOWLIST"):
        monkeypatch.delenv(key, raising=False)
    con = tasks.connect(str(tmp_path / "board.db"))
    monkeypatch.setattr(network, "_CON", con)
    ordinary = cards.create(con, str(tmp_path), "Ordinary", "## deliverable\nnotes.md\n", "10032")
    research = cards.create(con, str(tmp_path), "Research", "## deliverable\nnotes.md\n", "10033")
    run = runs.create(con, workspace=str(tmp_path), question="Fixture research")
    node = runs.create_node(con, run["id"], trigger="Fixture research", parent_id=None, depth=0)
    runs.link_task(con, run["id"], research, kind="investigate", node=node)
    con.commit()
    runtime = SimpleNamespace(con=con, _closing=False, _sister_semaphore=SimpleNamespace(available=5),
                              _reconcile=lambda *_: None,
                              launch=AsyncMock(side_effect=lambda task_id, **_: {"task_id": task_id, "status": "started", "sister": "10032"}),
                              stop=AsyncMock(return_value={"status": "stopped"}),
                              message=AsyncMock(return_value={"mode": "steer"}))

    async def launch_ready(**options):
        return await SisterRuntime.launch_ready(runtime, **options)

    runtime.launch_ready = launch_ready
    collector = ToolCollector()
    network._install(collector, runtime)
    tools = {tool.name: tool for tool in collector.tools}
    ctx = SimpleNamespace(cwd=str(tmp_path))

    async def call(name, **args):
        return await tools[name].execute("fixture", args, None, None, ctx)

    yield SimpleNamespace(con=con, ordinary=ordinary, research=research, call=call, runtime=runtime)
    con.close()


@pytest.mark.parametrize("name,args", [
    ("misaka_dispatch", {"task_ids": "research", "confirmed": True}),
    ("misaka_sister", {"task_id": "research", "confirmed": True}),
    ("misaka_card_link", {"parent_id": "ordinary", "child_id": "research"}),
    ("misaka_card_link", {"parent_id": "research", "child_id": "ordinary"}),
    ("misaka_card_request_review", {"task_id": "research", "reviewer": None}),
    ("misaka_card_review", {"task_id": "research", "reviewer": "10032", "decision": "approve"}),
    ("misaka_card_unblock", {"task_id": "research"}),
    ("misaka_card_requeue", {"task_id": "research", "confirmed": True}),
    ("misaka_card_delete", {"task_id": "research", "confirmed": True}),
])
async def test_ordinary_entry_points_reject_driver_owned_targets(board, name, args):
    args = {key: getattr(board, value) if value in {"research", "ordinary"} else value
            for key, value in args.items()}
    if "task_ids" in args:
        args["task_ids"] = [args["task_ids"]]
    before = dict(tasks.get(board.con, board.research))
    with pytest.raises(ValueError, match="its driver owns"):
        await board.call(name, **args)
    assert dict(tasks.get(board.con, board.research)) == before
    board.runtime.launch.assert_not_called()


@pytest.mark.parametrize("panel", [False, True])
async def test_bulk_dispatch_checks_actual_targets_before_starting_anything(board, monkeypatch, panel):
    from misaka.ui.panel import client as net

    sent = []
    monkeypatch.setattr(net, "request", lambda *args: sent.append(args) or {"pane_id": "fixture"})
    if panel:
        monkeypatch.setenv("MISAKA_NET_PANE", "fixture")
    with pytest.raises(ValueError, match="its driver owns"):
        await board.call("misaka_dispatch", confirmed=True)
    board.runtime.launch.assert_not_called()
    assert sent == []
    # Explicit ordinary work remains runnable beside Research; [] means no work.
    await board.call("misaka_dispatch", confirmed=True, task_ids=[])
    assert sent == []
    board.runtime.launch.assert_not_called()
    await board.call("misaka_dispatch", confirmed=True, task_ids=[board.ordinary])
    if panel:
        assert len(sent) == 1 and sent[0][1]["task_id"] == board.ordinary
    else:
        board.runtime.launch.assert_awaited_once()
        assert board.runtime.launch.call_args.args == (board.ordinary,)
    assert tasks.get(board.con, board.research)["status"] == "ready"


async def test_normal_review_and_research_stop_keep_their_existing_contracts(board):
    await board.call("misaka_card_request_review", task_id=board.ordinary, reviewer=None)
    await board.call("misaka_sister_stop", task_id=board.research, confirmed=True)
    board.runtime.stop.assert_awaited_once()
    assert board.runtime.stop.call_args.kwargs["confirmed"] is True


@pytest.mark.parametrize("status,confirmed,starts", [("done", False, False), ("done", True, True),
                                                    ("blocked", False, True)])
async def test_research_continuation_preserves_confirmation_and_generation(board, monkeypatch, status, confirmed, starts):
    from misaka.ui.panel import client as net

    board.con.execute("UPDATE tasks SET status=?,block_kind='needs_input',generation=3 WHERE id=?",
                      (status, board.research))
    board.con.commit()
    monkeypatch.setenv("MISAKA_NET_PANE", "fixture")
    monkeypatch.setattr(network, "_pane_for_card", lambda *_: None)
    sent = []
    monkeypatch.setattr(net, "request", lambda *args: sent.append(args) or {"pane_id": "fixture"})
    await board.call("misaka_sister_message", task_id=board.research, message="Fixture reply",
                     summary="Fixture", generation=3, confirmed=confirmed)
    assert bool(sent) == starts
    if starts:
        assert sent[0][0] == "pane.continue_card"
        assert sent[0][1]["expected_generation"] == 3
    with pytest.raises(ValueError, match="stale"):
        await board.call("misaka_sister_message", task_id=board.research, message="Stale reply",
                         summary="Fixture", generation=2, confirmed=True)
    assert len(sent) == int(starts)

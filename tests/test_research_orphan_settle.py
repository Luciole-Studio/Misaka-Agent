"""A run whose driver died stopping it is settled by the next driver (R61); `/research close`
takes a stopped run out of its conversation.

2026-10-26 and 10-27: closing a window gives its process two seconds before SIGKILL, and the
settle of a stopping run took 4 s on a small project and 40 s on an 18 GB one. Both runs stayed
``stopping`` under a lease naming a dead process. ``/research stop`` launched a driver only for a
run with no lease at all, so typing it changed nothing (10-28 13:31), and the workflow's rules
stood in front of every request of the conversation. Now the stop command and the conversation's
window opening settle such a run, and the window is told where its partial report is rather than
handed it: r_77d38618b4's was 3.4 MB (R44)."""
import asyncio
import os
import socket
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from misaka.core.platform import repo, tasks
from misaka.core.research import runs, workflow
from misaka.core.research.wiring import research


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(repo, "enabled", lambda *a: False)
    monkeypatch.setattr(workflow, "_bundle", lambda *a, **k: None)
    monkeypatch.setattr(workflow, "_try_refresh_workspace_index", lambda *a: None)
    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        runs.init(con)
        yield con


def dead_lock():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return f"driver:{socket.gethostname()}:{proc.pid}:dead00"


def live_lock():
    return f"driver:{socket.gethostname()}:{os.getpid()}:live00"


def stuck(con, workspace, session="lo"):
    """A run its driver died settling: stopping, the lease still the dead driver's."""
    run = runs.create(con, workspace=workspace, question="Fixture question", origin_session=session)
    assert runs.acquire_driver(con, run["id"], dead_lock())
    runs.request_stop(con, run["id"])
    return run["id"]


def window(board, tmp_path, monkeypatch, session="lo"):
    monkeypatch.setattr(research, "_con", lambda: board)
    monkeypatch.setattr(research, "_cfg", dict)
    monkeypatch.delenv("MISAKA_NET_PANE", raising=False)
    state = SimpleNamespace(sent=[], notices=[], part=research.ResearchPart())
    state.part.attach(SimpleNamespace(moments=SimpleNamespace(
        send_message=lambda payload, options: state.sent.append(payload), send_user_message=lambda text: None)))
    state.ctx = SimpleNamespace(
        cwd=str(tmp_path), isIdle=lambda: True,
        sessionManager=SimpleNamespace(sessionId=session, sessionFile=str(tmp_path / f"{session}.jsonl")),
        ui=SimpleNamespace(notify=lambda text, kind: state.notices.append((text, kind))))
    state.command = lambda raw: state.part.commands[0].handler(raw, state.ctx)
    return state


async def until(check, seconds=10):
    for _ in range(int(seconds / 0.02)):
        if check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out")


@pytest.mark.parametrize("lease", ["none", "expired", "dead holder", "live holder", "foreign host"])
def test_driverless_is_the_lease_acquire_driver_would_take(board, tmp_path, lease):
    rid = runs.create(board, workspace=str(tmp_path), question="Fixture question")["id"]
    if lease == "expired":
        assert runs.acquire_driver(board, rid, live_lock(), ttl_seconds=-5)
    elif lease == "dead holder":
        assert runs.acquire_driver(board, rid, dead_lock())
    elif lease == "live holder":
        assert runs.acquire_driver(board, rid, live_lock())
    elif lease == "foreign host":
        assert runs.acquire_driver(board, rid, "driver:another-host:1:abc123")
    free = runs.driverless(runs.get(board, rid))
    assert free == (lease in {"none", "expired", "dead holder"})
    assert runs.acquire_driver(board, rid, f"driver:{socket.gethostname()}:{os.getpid()}:next00") == free


async def test_stop_settles_a_run_whose_driver_died(board, tmp_path, monkeypatch):
    lo = window(board, tmp_path, monkeypatch)
    rid = stuck(board, str(tmp_path))
    try:
        await lo.command(f"stop {rid}")
        await until(lambda: lo.sent)
    finally:
        await lo.part._cleanup({}, lo.ctx)
    run = runs.get(board, rid)
    assert run["status"] == "stopped" and run["driver_lock"] is None
    (final,) = lo.sent
    report = runs.artifacts(board, rid, kind="partial")[-1]["path"]
    assert final["customType"] == "research-final"
    assert report in final["content"] and "The user requested a stop." in final["content"]
    assert Path(report).read_text(encoding="utf-8").startswith("# Incomplete research run")
    assert "# Incomplete research run" not in final["content"]       # where the report is, not the report


async def test_opening_the_conversation_settles_only_its_own_orphans(board, tmp_path, monkeypatch):
    lo = window(board, tmp_path, monkeypatch)
    orphan = stuck(board, str(tmp_path))
    theirs = stuck(board, str(tmp_path), session="another")
    driven = stuck(board, str(tmp_path))
    assert runs.acquire_driver(board, driven, live_lock())            # a live driver is stopping it
    running = runs.create(board, workspace=str(tmp_path), question="Fixture question", origin_session="lo")["id"]
    try:
        await lo.part.session_start({"type": "session_start"}, lo.ctx)
        await until(lambda: runs.get(board, orphan)["status"] == "stopped" and lo.sent)
    finally:
        await lo.part._cleanup({}, lo.ctx)
    assert [message["details"]["run_id"] for message in lo.sent] == [orphan]
    assert runs.get(board, theirs)["status"] == "stopping"
    assert runs.get(board, driven)["status"] == "stopping"
    assert runs.get(board, running)["status"] == "active"


async def test_two_windows_of_one_conversation_settle_it_once_and_quietly(board, tmp_path, monkeypatch):
    first, second = window(board, tmp_path, monkeypatch), window(board, tmp_path, monkeypatch)
    rid = stuck(board, str(tmp_path))
    try:
        await first.part.session_start({"type": "session_start"}, first.ctx)
        await second.part.session_start({"type": "session_start"}, second.ctx)
        await until(lambda: runs.get(board, rid)["status"] == "stopped" and (first.sent or second.sent))
        await asyncio.sleep(0.2)
    finally:
        await first.part._cleanup({}, first.ctx)
        await second.part._cleanup({}, second.ctx)
    kinds = [message["customType"] for message in first.sent + second.sent]
    assert kinds == ["research-final"]


@pytest.mark.parametrize("status", ["stopped", "failed"])
async def test_close_takes_a_paused_run_out_of_its_conversation(board, tmp_path, monkeypatch, status):
    lo = window(board, tmp_path, monkeypatch)
    rid = runs.create(board, workspace=str(tmp_path), question="Fixture question", origin_session="lo")["id"]
    runs.set_state(board, rid, status=status)
    assert "[Research Workflow paused]" in research.mode_notice(runs.for_session(board, "lo"))
    try:
        await lo.command("close")
    finally:
        await lo.part._cleanup({}, lo.ctx)
    assert research.mode_notice(runs.for_session(board, "lo")) is None
    assert runs.get(board, rid)["status"] == status                  # the run itself is as it was
    assert lo.notices[-1][1] == "info" and "Research mode closed" in lo.notices[-1][0]


async def test_close_refuses_an_open_run_and_another_conversations_run(board, tmp_path, monkeypatch):
    lo = window(board, tmp_path, monkeypatch)
    live = runs.create(board, workspace=str(tmp_path), question="Fixture question", origin_session="lo")["id"]
    theirs = runs.create(board, workspace=str(tmp_path), question="Fixture question", origin_session="another")["id"]
    runs.set_state(board, theirs, status="stopped")
    try:
        await lo.command("close")
        await lo.command(f"close {theirs}")
    finally:
        await lo.part._cleanup({}, lo.ctx)
    assert lo.notices[0][1] == "error" and live in lo.notices[0][0]
    assert "not a stopped or failed run of this conversation" in lo.notices[1][0]
    assert [run["id"] for run in runs.for_session(board, "lo")] == [live]
    assert [run["id"] for run in runs.for_session(board, "another")] == [theirs]


async def test_a_driver_started_from_a_window_binds_a_closed_run_to_it_again(board, tmp_path, monkeypatch):
    rid = stuck(board, str(tmp_path))
    assert (await workflow.run(board, {}, object(), run_id=rid))["reason"] == "stopped"
    assert runs.close(board, rid, "lo")
    assert runs.for_session(board, "lo") == []
    await workflow.run(board, {}, object(), run_id=rid, origin_session="lo")     # what /research resume starts
    assert [run["id"] for run in runs.for_session(board, "lo")] == [rid]


def test_close_parses_like_stop():
    assert research.parse_command("close") == {"action": "close", "run_id": None}
    assert research.parse_command("close r_abc") == {"action": "close", "run_id": "r_abc"}
    assert research.parse_command("close reading of Capital")["action"] == "activate"

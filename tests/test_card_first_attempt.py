"""Outside the panel, a never-ran card is started the same way: first attempt plus a note.

2026-09-18 (B16): the in-process runtime raised "no resumable Sister session" for a card that
was stopped before its first turn, and every other tool refused it too."""
import asyncio
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from misaka.core.network import messages, sister_runtime, worker
from misaka.core.platform import cards, tasks

BODY = "## deliverable\nnotes.md\n"


@pytest.fixture
def board(tmp_path, request):
    con = tasks.connect(str(tmp_path / "board.db"))
    request.addfinalizer(con.close)
    return con


def _never_ran(con, tmp_path):
    task_id = cards.create(con, str(tmp_path), "T never ran", BODY, "10032")
    con.execute("UPDATE tasks SET status='stopped' WHERE id=?", (task_id,))
    tasks._mirror_status(con, task_id)
    con.commit()
    return task_id


async def test_a_never_ran_card_is_reopened_launched_and_told(board, tmp_path, monkeypatch):
    from misaka.core.network.ally import presets
    from misaka.ui.panel import client

    task_id = _never_ran(board, tmp_path)
    runtime = sister_runtime.SisterRuntime(None, lambda: board, dict)
    mailed = []
    monkeypatch.setattr(runtime, "_mail_card", lambda row, text, summary: mailed.append(
        (row["id"], text, summary, row["generation"])))
    monkeypatch.setattr(runtime, "_reconcile", lambda *_: None)
    monkeypatch.setattr(presets, "names", lambda: {"10032"})
    monkeypatch.setattr(client, "ensure", lambda: None)

    def run_pane(method, params):
        assert method == "pane.run_card" and params == {"task_id": task_id}
        assert tasks.get(board, task_id)["generation"] == 2
        return {"pane_id": "first-pane"}

    monkeypatch.setattr(client, "request", run_pane)
    out = await asyncio.wait_for(runtime.message(
        task_id, "begin with volume one", summary="v1", confirmed=True,
        expected_generation=1, context=SimpleNamespace(),
    ), timeout=3)
    assert out["mode"] == "first-attempt" and out["pane"] == "first-pane"
    row = tasks.get(board, task_id)
    assert row["generation"] == 2
    assert mailed == [(task_id, "begin with volume one", "v1", 2)]


async def test_starting_a_never_ran_card_needs_the_users_word(board, tmp_path):
    task_id = _never_ran(board, tmp_path)
    runtime = sister_runtime.SisterRuntime(None, lambda: board, dict)
    with pytest.raises(ValueError, match="confirmation is required"):
        await runtime.message(task_id, "go", summary="go", confirmed=False,
                              expected_generation=1, context=SimpleNamespace())
    assert tasks.get(board, task_id)["status"] == "stopped"


async def test_a_stale_generation_is_refused_before_anything_starts(board, tmp_path):
    task_id = _never_ran(board, tmp_path)
    runtime = sister_runtime.SisterRuntime(None, lambda: board, dict)
    with pytest.raises(RuntimeError, match="changed"):
        await runtime.message(task_id, "go", summary="go", confirmed=True,
                              expected_generation=9, context=SimpleNamespace())


@pytest.mark.parametrize("bounded", [True, False])
async def test_resuming_a_card_near_the_budget_cap_keeps_bounded_tools(board, tmp_path, monkeypatch, bounded):
    (tmp_path / "input.txt").write_text("source", encoding="utf-8")
    task_id = cards.create(board, str(tmp_path), "Resume", BODY, "10032",
                           local_work={"input_paths": ["input.txt"]} if bounded else None)
    transcript = tmp_path / "session.jsonl"
    board.execute("UPDATE tasks SET status='stopped',agent_id='fixture',session_file=? WHERE id=?",
                  (str(transcript), task_id))
    tasks._mirror_status(board, task_id)
    board.commit()
    row = tasks.get(board, task_id)
    runtime = sister_runtime.SisterRuntime(None, lambda: board, dict)
    agent = SimpleNamespace(transcript=transcript, id="fixture")
    manager = SimpleNamespace(_session_dir=lambda _: None, _find_task_async=AsyncMock(return_value=agent),
                              send_message=AsyncMock())

    def restore_manager(_session, _cfg, prepared, _workspace):
        manager.file_scope = prepared.get("_file_scope")
        return manager

    monkeypatch.setattr(sister_runtime, "_SisterManager", restore_manager)
    monkeypatch.setattr(sister_runtime.budget, "status", lambda *_args, **_kwargs: {"mode": "beast"})
    monkeypatch.setattr(runtime, "_supervise", AsyncMock())
    assert bool(worker.load_card_file_scope(board, row)) is bounded
    result = await runtime.message(task_id, "continue", summary="resume", confirmed=True,
                                   expected_generation=row["generation"], context=SimpleNamespace())
    await runtime._handles[task_id].supervisor
    assert result["mode"] == "resume" and manager.beast is not bounded
    manager.send_message.assert_awaited_once()


def test_the_note_is_mailed_to_that_attempt(tmp_path, monkeypatch):
    real_connect = messages.connect
    monkeypatch.setattr(messages, "connect", lambda path=None: real_connect(str(tmp_path / "messages.db")))
    row = {"id": "t_aaaaaa", "assignee": "10032", "generation": 3, "workspace": str(tmp_path)}
    sister_runtime.SisterRuntime._mail_card(row, "the note", "note")
    with closing(real_connect(str(tmp_path / "messages.db"))) as con:
        rows = messages.pending(con, "10032", task_id="t_aaaaaa")
    assert len(rows) == 1 and rows[0]["to_task"] == "t_aaaaaa" and rows[0]["generation"] == 3
    assert rows[0]["sender"] == "last-order"

"""Outside the panel, a never-ran card is started the same way: first attempt plus a note.

2026-09-18 (B16): the in-process runtime raised "no resumable Sister session" for a card that
was stopped before its first turn, and every other tool refused it too."""
import asyncio
from contextlib import closing
from types import SimpleNamespace
from typing import ClassVar

import pytest

from misaka.core.network import messages, sister_runtime
from misaka.core.platform import cards, tasks

BODY = "## deliverable\nnotes.md\n"


class _Runtime:
    """A SisterRuntime, as much of it as `message` reads for a never-ran card."""

    message = sister_runtime.SisterRuntime.message
    _mail_card = staticmethod(lambda row, text, summary: _Runtime.mailed.append((row["id"], text, summary, row["generation"])))
    mailed: ClassVar[list] = []

    def __init__(self, con):
        self.con = con
        self._lock = asyncio.Lock()
        self._closing = False
        self._owned_claims = set()
        self._handles = {}
        self.launched = []
        self.events = []

    async def launch(self, task_id, *, context, tool_call_id, on_update=None):
        self.launched.append((task_id, tool_call_id))
        self._handles[task_id] = SimpleNamespace(board_id=task_id)
        return {"launched": True}

    def _event(self, handle, kind, payload):
        self.events.append((handle.board_id, kind, payload))

    async def _restore(self, task_id, context):
        raise AssertionError("a never-ran card must not be restored")


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


async def test_a_never_ran_card_is_reopened_launched_and_told(board, tmp_path):
    _Runtime.mailed.clear()
    task_id = _never_ran(board, tmp_path)
    runtime = _Runtime(board)
    out = await runtime.message(task_id, "begin with volume one", summary="v1", confirmed=True,
                                expected_generation=1, context=SimpleNamespace())
    assert out["mode"] == "first-attempt" and out["launched"] is True
    assert runtime.launched == [(task_id, "message")]
    row = tasks.get(board, task_id)
    assert row["generation"] == 2                              # reopened as a fresh attempt
    assert _Runtime.mailed == [(task_id, "begin with volume one", "v1", 2)]
    assert runtime.events == [(task_id, "message", {"summary": "v1", "mode": "first-attempt"})]


async def test_starting_a_never_ran_card_needs_the_users_word(board, tmp_path):
    task_id = _never_ran(board, tmp_path)
    with pytest.raises(ValueError, match="confirmation is required"):
        await _Runtime(board).message(task_id, "go", summary="go", confirmed=False,
                                      expected_generation=1, context=SimpleNamespace())
    assert tasks.get(board, task_id)["status"] == "stopped"


async def test_a_stale_generation_is_refused_before_anything_starts(board, tmp_path):
    task_id = _never_ran(board, tmp_path)
    with pytest.raises(RuntimeError, match="changed"):
        await _Runtime(board).message(task_id, "go", summary="go", confirmed=True,
                                      expected_generation=9, context=SimpleNamespace())


def test_the_note_is_mailed_to_that_attempt(tmp_path, monkeypatch):
    real_connect = messages.connect
    monkeypatch.setattr(messages, "connect", lambda path=None: real_connect(str(tmp_path / "messages.db")))
    row = {"id": "t_aaaaaa", "assignee": "10032", "generation": 3, "workspace": str(tmp_path)}
    sister_runtime.SisterRuntime._mail_card(row, "the note", "note")   # opens and closes its own
    with closing(real_connect(str(tmp_path / "messages.db"))) as con:
        rows = messages.pending(con, "10032", task_id="t_aaaaaa")
    assert len(rows) == 1 and rows[0]["to_task"] == "t_aaaaaa" and rows[0]["generation"] == 3
    assert rows[0]["sender"] == "last-order"

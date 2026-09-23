"""Steering a running Sister goes through her inbox, not through her pane's keyboard.

2026-09-18: `misaka_sister_message` typed the note into the card's pane and pressed Enter.
That lost the Enter behind a full pty input queue (B1) and could land in a pane that was only
showing the card (B14). With a card address in the mailbox (B8), the note is a durable row
that the Sister's own inbox hands her at the next tool boundary. An ally -- a third-party CLI
in a pane -- reads no mailbox, so it still gets keystrokes."""
import json
from types import SimpleNamespace

import pytest

from misaka.core.network import messages
from misaka.core.network.wiring import network
from misaka.core.platform import cards, tasks
from misaka.core.wiring import ToolCollector

BODY = "## deliverable\nnotes.md\n"
SESSION = "01a0b19c-78f1-73f1-9359-7c4f1f84d959"


@pytest.fixture
def world(tmp_path, monkeypatch, request):
    con = tasks.connect(str(tmp_path / "board.db"))
    request.addfinalizer(con.close)
    monkeypatch.setattr(network, "_CON", con)
    real_connect = messages.connect
    mail = real_connect(str(tmp_path / "messages.db"))
    request.addfinalizer(mail.close)
    monkeypatch.setattr(messages, "connect", lambda path=None: real_connect(str(tmp_path / "messages.db")))
    sent = []
    monkeypatch.setattr("misaka.ui.panel.client.request",
                        lambda method, params=None: sent.append((method, params)) or {"sent": True})
    monkeypatch.setattr(network, "_pane_for_card",
                        lambda task_id: {"id": "p7", "card": task_id, "alive": True, "claimed": True})
    collector = ToolCollector()
    network._install(collector, SimpleNamespace())
    tool = next(t for t in collector.tools if t.name == "misaka_sister_message")
    ctx = SimpleNamespace(cwd=str(tmp_path), sessionManager=SimpleNamespace(sessionId=SESSION))

    async def message(task_id, text, generation=1):
        return await tool.execute("fixture", {"task_id": task_id, "message": text, "summary": text[:20],
                                              "generation": generation}, None, None, ctx)

    return SimpleNamespace(con=con, tmp=tmp_path, message=message, sent=sent,
                           mail=lambda: mail)


def _running(world, *, executor=None):
    task_id = cards.create(world.con, str(world.tmp), "T running", BODY, "10032")
    world.con.execute("UPDATE tasks SET status='running', claim_lock='net:x', executor=? WHERE id=?",
                      (json.dumps(executor) if executor else None, task_id))
    world.con.commit()
    return task_id


async def test_a_running_sister_is_steered_through_her_inbox(world):
    task_id = _running(world)
    out = await world.message(task_id, "check the 1982 edition first")
    text = out["content"][0]["text"]
    assert "queued for card" in text and task_id in text
    rows = messages.pending(world.mail(), "10032", task_id=task_id)
    assert [r["body"] for r in rows] == ["check the 1982 edition first"]
    assert rows[0]["to_task"] == task_id and rows[0]["generation"] == 1
    assert rows[0]["sender"] == "last-order" and rows[0]["sender_session"] == SESSION
    assert world.sent == [], "no keystrokes went to the pane"


async def test_the_note_is_bound_to_the_attempt_that_is_running(world):
    task_id = _running(world)
    world.con.execute("UPDATE tasks SET generation=3 WHERE id=?", (task_id,))
    world.con.commit()
    await world.message(task_id, "again", generation=3)
    rows = messages.pending(world.mail(), "10032", task_id=task_id)
    assert rows[0]["generation"] == 3


async def test_an_ally_still_gets_keystrokes(world):
    task_id = _running(world, executor={"argv": ["codex"]})
    out = await world.message(task_id, "try the other branch")
    assert "typed into" in out["content"][0]["text"]
    assert world.sent == [("pane.send", {"id": "p7", "card": task_id, "text": "try the other branch",
                                         "enter": True, "expected_generation": 1})]
    assert messages.pending(world.mail(), "10032", task_id=task_id) == []


async def test_a_stale_generation_is_refused_before_anything_is_sent(world):
    task_id = _running(world)
    with pytest.raises(ValueError, match="stale"):
        await world.message(task_id, "hello", generation=9)
    assert world.sent == [] and messages.pending(world.mail(), "10032", task_id=task_id) == []

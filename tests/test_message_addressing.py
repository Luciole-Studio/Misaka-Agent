"""A message can be addressed to a role, to a card, or to a session.

2026-09-18 (B8): addresses were roles only, so every session of a role shared one inbox and
whichever polled first took the mail. Two cards of the same Sister could not talk at all
(a message to your own role would land on any of them, yourself included), and a Sister in a
research tree could not answer her own node's Last Order rather than any Last Order. A card
id now reaches the one session running that card at that attempt; a session id reaches that
one conversation; both are delivered to a live session only."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from misaka.core import session_catalog
from misaka.core.network import messages
from misaka.core.platform import cards, processes, tasks

BODY = "## deliverable\nnotes.md\n"


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A board, a mailbox and a catalog index of this test's own."""
    board_path = tmp_path / "board.db"
    monkeypatch.setenv("MISAKA_USAGE_DB", str(board_path))
    monkeypatch.delenv("MISAKA_SISTER_OWNER_DB", raising=False)
    index = tmp_path / "sessions" / ".catalog"
    index.mkdir(parents=True)
    monkeypatch.setattr(session_catalog, "_index_dir", lambda: index)
    monkeypatch.setattr(messages, "sisters", lambda: {"10032", "10038"})
    monkeypatch.setattr(processes, "identity_is_alive", lambda pid, identity: pid == os.getpid())
    board = tasks.connect(str(board_path))
    mail = messages.connect(str(tmp_path / "messages.db"))
    workspace = tmp_path / "project"
    workspace.mkdir()
    return SimpleNamespace(board=board, mail=mail, index=index, workspace=str(workspace), tmp=tmp_path)


def _running(world, assignee="10032", generation=1, workspace=None):
    task_id = cards.create(world.board, workspace or world.workspace, "T card", BODY, assignee)
    world.board.execute("UPDATE tasks SET status='running', generation=? WHERE id=?", (generation, task_id))
    world.board.commit()
    return task_id


def _live(world, session_id, *, role="10032", task_id=None, inbox="10032", alive=True):
    value = {"id": session_id, "path": None, "pid": os.getpid() if alive else 424242,
             "identity": "x", "role": role, "kind": "card" if task_id else "foreground",
             "task_id": task_id, "workspace": world.workspace, "inbox": inbox, "state": "working"}
    (world.index / f"{session_id}.json").write_text(json.dumps(value), encoding="utf-8")
    return session_id


SESSION_A = "01a0b19c-78f1-73f1-9359-7c4f1f84d959"
SESSION_B = "01a0b19c-78f1-7d15-ad0e-5562fc0be98a"


def _resolve(addr, **kw):
    kw.setdefault("sender", "10032")
    return messages.resolve_address(addr, **kw)


# ── resolution ─────────────────────────────────────────────────────────────────────────

def test_a_role_is_a_broadcast(world):
    assert _resolve("last-order")["to_addr"] == "last-order"
    target = _resolve("10038")
    assert target == {"to_addr": "10038", "to_task": None, "to_session": None, "generation": None,
                      "label": "10038"}


def test_a_card_is_addressed_by_its_id(world):
    sibling = _running(world, generation=3)
    _live(world, SESSION_A, task_id=sibling)
    target = _resolve(sibling, sender_task="t_000000", workspace=world.workspace)
    assert target["to_addr"] == "10032" and target["to_task"] == sibling
    assert target["generation"] == 3 and target["to_session"] is None


def test_a_session_is_addressed_by_its_id(world):
    _live(world, SESSION_A, role="last-order", inbox="last-order")
    target = _resolve(SESSION_A, sender_session=SESSION_B)
    assert target == {"to_addr": "last-order", "to_task": None, "to_session": SESSION_A,
                      "generation": None, "label": f"session {SESSION_A} (last-order)"}


def test_your_own_role_card_and_session_are_refused(world):
    with pytest.raises(ValueError, match="your own role"):
        _resolve("10032")
    mine = _running(world)
    _live(world, SESSION_A, task_id=mine)
    with pytest.raises(ValueError, match="this card"):
        _resolve(mine, sender_task=mine, workspace=world.workspace)
    with pytest.raises(ValueError, match="this session"):
        _resolve(SESSION_A, sender_session=SESSION_A)


def test_a_card_that_is_not_running_or_has_no_live_session_is_refused(world):
    stopped = cards.create(world.board, world.workspace, "T stopped", BODY, "10032")
    with pytest.raises(ValueError, match="no live session"):
        _resolve(stopped, workspace=world.workspace)
    running = _running(world)                      # running on the board, nobody live
    with pytest.raises(ValueError, match="no live session"):
        _resolve(running, workspace=world.workspace)
    _live(world, SESSION_A, task_id=running, alive=False)
    with pytest.raises(ValueError, match="no live session"):
        _resolve(running, workspace=world.workspace)


def test_a_card_in_another_project_is_not_addressable(world):
    elsewhere = world.tmp / "elsewhere"
    elsewhere.mkdir()
    other = _running(world, workspace=str(elsewhere))
    _live(world, SESSION_A, task_id=other)
    with pytest.raises(ValueError, match="another project"):
        _resolve(other, workspace=world.workspace)


def test_a_session_that_is_gone_or_reads_no_mail_is_refused(world):
    with pytest.raises(ValueError, match="not live"):
        _resolve(SESSION_A)
    _live(world, SESSION_A, role="10038", inbox=None)
    with pytest.raises(ValueError, match="reads no mail"):
        _resolve(SESSION_A)


def test_an_unknown_address_lists_what_can_be_reached(world):
    sibling = _running(world)
    with pytest.raises(ValueError) as refusal:
        _resolve("nobody", sender_task="t_000000", workspace=world.workspace)
    text = str(refusal.value)
    assert "last-order" in text and "10038" in text and "10032" not in text.split("running cards")[0]
    assert f"{sibling} (10032)" in text and "session id" in text


# ── delivery ───────────────────────────────────────────────────────────────────────────

def _send(world, to_addr, body, **kw):
    return messages.send(world.mail, to_addr, body, summary=body[:20], sender="10032", **kw)


def test_a_card_address_is_read_only_by_that_card(world):
    _send(world, "10032", "broadcast")
    _send(world, "10032", "for A", to_task="t_aaaaaa", generation=2)
    _send(world, "10032", "for B", to_task="t_bbbbbb", generation=1)
    bodies = lambda **kw: [r["body"] for r in messages.pending(world.mail, "10032", **kw)]
    assert bodies(task_id="t_aaaaaa") == ["broadcast", "for A"]
    assert bodies(task_id="t_bbbbbb") == ["broadcast", "for B"]
    assert bodies() == ["broadcast"]                 # a session that is no card


def test_a_session_address_is_read_only_by_that_session(world):
    _send(world, "last-order", "for the root", to_session=SESSION_A)
    _send(world, "last-order", "for everyone")
    bodies = lambda **kw: [r["body"] for r in messages.pending(world.mail, "last-order", **kw)]
    assert bodies(session_id=SESSION_A) == ["for the root", "for everyone"]
    assert bodies(session_id=SESSION_B) == ["for everyone"]
    assert bodies() == ["for everyone"]


def test_a_note_for_an_earlier_attempt_is_dropped_not_read(world):
    stale = _send(world, "10032", "for attempt 1", to_task="t_aaaaaa", generation=1)
    fresh = _send(world, "10032", "for attempt 2", to_task="t_aaaaaa", generation=2)
    rows = messages.pending(world.mail, "10032", task_id="t_aaaaaa")
    deliverable, discard = messages.delivery_plan(rows, task_generation=2)
    assert deliverable == {fresh} and discard == {stale}


def test_a_broadcast_is_never_dropped_for_generation(world):
    mid = _send(world, "10032", "hello all")
    rows = messages.pending(world.mail, "10032", task_id="t_aaaaaa")
    deliverable, discard = messages.delivery_plan(rows, task_generation=7)
    assert deliverable == {mid} and discard == set()


def test_a_mailbox_shaped_by_another_build_is_refused_not_reshaped(tmp_path):
    """No upgrade path is shipped (2026-09-22): the queue stays exactly as that build left it."""
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(str(path))
    old.executescript(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, to_addr TEXT NOT NULL, sender TEXT,"
        " body TEXT NOT NULL, summary TEXT, task_id TEXT, generation INTEGER, workspace TEXT,"
        " created_at INTEGER NOT NULL, delivered_at INTEGER, lease_expires INTEGER, lease_token TEXT);"
        "INSERT INTO messages (to_addr, body, created_at) VALUES ('10032', 'legacy', 1);")
    old.commit()
    old.close()
    with pytest.raises(RuntimeError, match="another MISAKA"):
        messages.connect(str(path))
    old = sqlite3.connect(str(path))
    assert {row[1] for row in old.execute("PRAGMA table_info(messages)")} == {
        "id", "to_addr", "sender", "body", "summary", "task_id", "generation", "workspace",
        "created_at", "delivered_at", "lease_expires", "lease_token"}                  # untouched
    old.close()


# ── the tool end to end ────────────────────────────────────────────────────────────────

class _Manager:
    def __init__(self, session_id):
        self._id = session_id

    def getSessionId(self):
        return self._id


def _part(world, *, sender="10032", card_task=None, session_id=SESSION_B):
    part = messages.MessagesPart(sender=sender, card_task=card_task, workspace=world.workspace)
    part.attach(SimpleNamespace(sessionManager=_Manager(session_id)))
    return part


def _reopen(world):
    return _connect(str(world.tmp / "messages.db"))


_connect = messages.connect      # the real one, before a test points the module's at its own file


async def test_sendmessage_to_a_sibling_card_lands_in_that_cards_inbox_only(world, monkeypatch):
    monkeypatch.setattr(messages, "connect", lambda path=None: _reopen(world))
    mine = _running(world)
    sibling = _running(world, generation=2)
    _live(world, SESSION_A, task_id=sibling)
    part = _part(world, card_task=mine)
    out = await part._send("fixture", {"to": sibling, "message": "the bibliography is in T0", "summary": "T0 done"},
                           None, None, SimpleNamespace())
    assert out["details"]["to_task"] == sibling and out["details"]["live"] is True
    assert "queued for card" in out["content"][0]["text"]
    rows = messages.pending(_reopen(world), "10032", task_id=sibling)
    assert [r["body"] for r in rows] == [f"[card {mine}]\nthe bibliography is in T0"]
    assert rows[0]["sender_task"] == mine and rows[0]["sender_session"] == SESSION_B
    assert rows[0]["generation"] == 2
    assert messages.pending(_reopen(world), "10032", task_id=mine) == []


async def test_sendmessage_to_a_session_wakes_nobody(world, monkeypatch):
    monkeypatch.setattr(messages, "connect", lambda path=None: _reopen(world))
    spawned = []
    monkeypatch.setattr(messages.subprocess, "Popen", lambda *a, **k: spawned.append(a) or SimpleNamespace())
    _live(world, SESSION_A, role="last-order", inbox="last-order")
    part = _part(world, card_task=_running(world))
    out = await part._send("fixture", {"to": SESSION_A, "message": "my node's plan is ready", "summary": "plan"},
                           None, None, SimpleNamespace())
    assert out["details"]["to_session"] == SESSION_A
    assert spawned == []
    rows = messages.pending(_reopen(world), "last-order", session_id=SESSION_A)
    assert len(rows) == 1 and rows[0]["to_session"] == SESSION_A


async def test_a_role_address_still_wakes_a_contact_session_when_nobody_is_live(world, monkeypatch):
    monkeypatch.setattr(messages, "connect", lambda path=None: _reopen(world))
    spawned = []
    monkeypatch.setattr(messages.subprocess, "Popen", lambda argv, **k: spawned.append(argv) or SimpleNamespace())
    part = _part(world, card_task=_running(world))
    out = await part._send("fixture", {"to": "10038", "message": "hello", "summary": "hi"}, None, None,
                           SimpleNamespace())
    assert out["details"]["live"] is False and len(spawned) == 1
    assert spawned[0][-1] == "10038"


async def test_a_help_request_stays_a_role_address(world, monkeypatch):
    monkeypatch.setattr(messages, "connect", lambda path=None: _reopen(world))
    part = _part(world, card_task=_running(world))
    with pytest.raises(ValueError, match="request_input=true requires"):
        await part._send("fixture", {"to": SESSION_A, "message": "help", "summary": "help", "request_input": True},
                         None, None, SimpleNamespace())


async def test_delivered_mail_says_which_card_and_session_sent_it(world):
    _send(world, "10032", "sibling note", to_task="t_aaaaaa", sender_task="t_bbbbbb", sender_session=SESSION_B)
    rows = messages.pending(world.mail, "10032", task_id="t_aaaaaa")
    seen = {}

    async def sendCustomMessage(message, options):
        seen.update(message)
        options["_onPersist"]()

    part = messages.MessagesPart(sender="10032", card_task="t_aaaaaa")
    part.attach(SimpleNamespace(sendCustomMessage=sendCustomMessage, _customMessageReceipts={}))
    await part._deliver_messages(rows, set(), None)
    assert "<from-card>t_bbbbbb</from-card>" in seen["content"]
    assert f"<from-session>{SESSION_B}</from-session>" in seen["content"]
    assert "send to its <from-session> id" in seen["content"]


def test_the_reader_hands_its_identity_to_the_queue(monkeypatch):
    """The pump asks for its own session's and card's rows; the queue does the narrowing."""
    asked = {}

    def fake_pending(con, to_addr, **kw):
        asked.update(kw)
        return []

    monkeypatch.setattr(messages, "pending", fake_pending)
    monkeypatch.setenv("MISAKA_USAGE_GENERATION", "4")
    part = messages.MessagesPart(sender="10032", card_task="t_aaaaaa", receive=True)
    part.attach(SimpleNamespace(sessionManager=_Manager(SESSION_A)))
    import asyncio
    asyncio.run(part._deliver_once(None))
    assert asked["session_id"] == SESSION_A and asked["task_id"] == "t_aaaaaa"
    assert part._task_generation() == 4


def test_card_and_session_ids_are_told_apart_from_roles():
    assert messages.CARD_ID.match("t_1a2b3c") and not messages.CARD_ID.match("t_1a2b3c4")
    assert messages.SESSION_ID.match(SESSION_A) and not messages.SESSION_ID.match("10032")
    assert not messages.CARD_ID.match("last-order") and not messages.SESSION_ID.match("last-order")


def test_the_catalog_finds_a_live_session_and_a_live_card(world):
    _live(world, SESSION_A, task_id="t_aaaaaa")
    _live(world, SESSION_B, role="last-order", inbox="last-order", alive=False)
    assert session_catalog.live_session(SESSION_A)["task_id"] == "t_aaaaaa"
    assert session_catalog.live_session(SESSION_B) is None
    assert session_catalog.live_card_session("t_aaaaaa")["id"] == SESSION_A
    assert session_catalog.live_card_session("t_zzzzzz") is None
    Path(world.index / f"{SESSION_A}.json").unlink()
    assert session_catalog.live_session(SESSION_A) is None

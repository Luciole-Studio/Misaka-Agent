"""One card, one window, one writer on its transcript.

2026-09-18 (B29/B14/B18/B19/B22): a card reworked after it finished always got a second pane.
`continue_card` looked only at the board status, so the finished card-shell kept running on the
same transcript beside the new one; `misaka_sister_resume` added a third, because reopening a
card's session launched another full `card-shell --resume` -- an agent that could run turns on
that transcript and whose catalog record overwrote the runner's. Last Order, holding three panes
for one card and a tool that refused to close any of them, ended up killing processes by PID.
"""
import asyncio
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from misaka.config import sessions as session_roots
from misaka.core.platform import cards, tasks
from misaka.ui.panel import daemon as d

BODY = "## deliverable\nnotes.md\n"
IDLE = [sys.executable, "-c", "import time; time.sleep(60)"]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MISAKA_HOME", str(tmp_path))
    monkeypatch.setitem(d.CFG, "messages_db", str(tmp_path / "messages.db"))
    profiles = tmp_path / "profiles"
    (profiles / "10032").mkdir(parents=True)
    monkeypatch.setitem(d.CFG, "profiles_root", str(profiles))
    monkeypatch.setattr(d, "CARD_SHELL", IDLE)
    return tmp_path


@pytest.fixture
def panel(home, request):
    daemon = d.Daemon(str(home / "net.sock"), str(home / "net.json"))
    daemon._con = tasks.connect(str(home / "board.db"))
    request.addfinalizer(daemon._con.close)
    try:
        yield daemon
    finally:
        for pane in list(daemon.panes.values()):
            # Only panes with a real child are closed: `close` signals the pane's process
            # group, and a stand-in pid would aim that at somebody else's.
            if not isinstance(pane.proc, subprocess.Popen):
                daemon.panes.pop(pane.id, None)
                continue
            try:
                daemon.close(pane.id)
            except Exception:  # noqa: BLE001, S110 - teardown of a pane the test already closed
                pass
        if daemon._mcon is not None:
            daemon._mcon.close()


def _card(panel, workspace, *, status="done", title="T4 crossover"):
    con = panel._board()
    task_id = cards.create(con, str(workspace), title, BODY, "10032")
    con.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
    tasks._mirror_status(con, task_id)
    con.commit()
    row = tasks.get(con, task_id)
    directory = session_roots.card_session_dir(row)
    os.makedirs(directory, exist_ok=True)
    transcript = os.path.join(directory, "2026-09-18T00-00-00-000Z_fixture.jsonl")
    with open(transcript, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "session", "id": "fixture", "cwd": str(workspace)}) + "\n")
    return task_id, transcript


async def _settle(proc, tries=60):
    for _ in range(tries):
        if proc.poll() is not None:
            return True
        await asyncio.sleep(0.05)
    return False


# ── choosing the card's window ─────────────────────────────────────────────────────────

def test_the_newest_live_pane_is_the_cards_window(panel):
    panel.panes = {}
    for index, (alive, started) in enumerate([(True, 10), (True, 30), (False, 50)]):
        pane = d.Pane(f"p{index}", "probe", [], "/", card="t_1")
        pane.started_at = started
        pane.proc = SimpleNamespace(poll=lambda alive=alive: None if alive else 1, pid=-9000 - index)
        panel.panes[pane.id] = pane
    panel.panes["px"] = d.Pane("px", "other", [], "/", card="t_2")
    assert panel._card_pane_to_reuse("t_1").id == "p1"          # live beats newer-but-dead
    assert panel._card_pane_to_reuse("t_2").id == "px"
    assert panel._card_pane_to_reuse("t_missing") is None


def test_a_dead_pane_is_still_the_cards_window(panel):
    """A card-shell that crashed leaves its pane behind; the next attempt takes it over."""
    panel.panes = {}
    pane = d.Pane("p1", "probe", [], "/", card="t_1")
    pane.proc = SimpleNamespace(poll=lambda: 1, pid=-9001)
    panel.panes["p1"] = pane
    assert panel._card_pane_to_reuse("t_1") is pane


# ── replacing the program in place ─────────────────────────────────────────────────────

async def test_replacing_a_program_keeps_the_pane_its_seat_and_its_size(panel, home):
    pane = panel.create(IDLE, str(home), title="before")
    pane_id, seat = pane.id, panel._tab_holding(pane.id)
    pane.resize(24, 100)
    first = pane.proc
    await panel._replace_program(pane, [*IDLE, "second"], title="after")
    assert panel.panes[pane_id] is pane
    assert panel._tab_holding(pane.id) == seat
    assert pane.size() == (24, 100)
    assert pane.title == "after" and pane.argv[-1] == "second"
    assert pane.proc is not first and pane.alive()
    assert await _settle(first), "the program that left is gone"
    assert pane.exit_code is None and pane.fd is not None


async def test_a_replaced_program_starts_on_a_clean_terminal(panel, home):
    pane = panel.create([sys.executable, "-c",
                         "import sys,time; sys.stdout.write('OLD OUTPUT\\n'); sys.stdout.flush(); time.sleep(60)"],
                        str(home))
    for _ in range(40):
        if b"OLD" in bytes(pane.buf):
            break
        await asyncio.sleep(0.05)
    assert b"OLD" in bytes(pane.buf)
    await panel._replace_program(pane, IDLE)
    assert bytes(pane.buf) == b""
    assert "OLD" not in "".join(pane.term.recent_text(pane.size()[0]))


# ── continuing a card ──────────────────────────────────────────────────────────────────

async def test_continuing_a_card_comes_back_in_its_own_window(panel, home):
    task_id, _ = _card(panel, home)
    first = panel.create([*IDLE, task_id], str(home), title="10032·" + task_id, card=task_id)
    first_proc = first.proc
    pane = await panel.continue_card(task_id, "please redo the ledger")
    assert pane is first, "the card came back in the pane it already had"
    assert len(panel.panes) == 1
    assert pane.proc is not first_proc and pane.alive()
    assert await _settle(first_proc)
    assert "--say" in pane.argv and pane.argv[-1] == "please redo the ledger"
    row = tasks.get(panel._board(), task_id)
    assert row["status"] == "running"
    assert pane.claim_lock == row["claim_lock"] and pane.generation == row["generation"]


async def test_continuing_a_card_closes_the_panes_it_is_not_taking_over(panel, home):
    task_id, _ = _card(panel, home)
    stale = panel.create([*IDLE, "stale"], str(home), card=task_id)
    stale.started_at -= 60
    newest = panel.create([*IDLE, "newest"], str(home), card=task_id)
    stale_proc = stale.proc
    pane = await panel.continue_card(task_id, "again please")
    assert pane is newest
    assert stale.id not in panel.panes
    assert await _settle(stale_proc), "the other pane's program is gone too"


async def test_a_card_with_no_window_still_opens_one(panel, home):
    task_id, _ = _card(panel, home)
    pane = await panel.continue_card(task_id, "start over")
    assert pane.card == task_id and pane.alive()
    assert len(panel.panes) == 1


async def test_a_stale_generation_is_refused_before_anything_is_touched(panel, home):
    task_id, _ = _card(panel, home)
    pane = panel.create([*IDLE, task_id], str(home), card=task_id)
    with pytest.raises(ValueError, match="generation"):
        await panel.continue_card(task_id, "redo", expected_generation=99)
    assert pane.alive() and tasks.get(panel._board(), task_id)["status"] == "done"


async def test_a_requeued_card_runs_in_the_window_it_left_open(panel, home):
    task_id, _ = _card(panel, home, status="ready")
    first = panel.create([*IDLE, task_id], str(home), card=task_id)
    first_proc = first.proc
    pane = await panel.run_card(task_id)
    assert pane is first and len(panel.panes) == 1
    assert await _settle(first_proc)
    assert tasks.get(panel._board(), task_id)["status"] == "running"


# ── looking at a card ──────────────────────────────────────────────────────────────────

async def test_looking_at_a_card_opens_a_reader_not_a_second_sister(panel, home, monkeypatch):
    task_id, transcript = _card(panel, home)
    captured = {}

    def fake_create(argv, cwd, *, title="", card=None, env=None, place=None):
        captured.update(argv=list(argv), cwd=cwd, card=card)
        pane = d.Pane("pv", title, list(argv), cwd, card=card)
        pane.proc = SimpleNamespace(poll=lambda: None, pid=-9002)
        panel.panes[pane.id] = pane
        return pane

    monkeypatch.setattr(panel, "create", fake_create)
    pane = await panel.open_card_session(task_id)
    assert pane.card == task_id
    assert captured["argv"] == [sys.executable, "-m", "misaka", "chat", "--read-only",
                                "--session", transcript]
    assert "card-shell" not in " ".join(captured["argv"])


async def test_a_card_already_on_screen_is_not_opened_twice(panel, home, monkeypatch):
    task_id, _ = _card(panel, home)
    existing = d.Pane("p1", "10032", [], str(home), card=task_id)
    existing.proc = SimpleNamespace(poll=lambda: None, pid=-9003)
    panel.panes["p1"] = existing
    monkeypatch.setattr(panel, "create", lambda *a, **k: pytest.fail("opened a second pane"))
    assert await panel.open_card_session(task_id) is existing


async def test_a_running_card_is_never_opened_as_a_reader(panel, home):
    task_id, _ = _card(panel, home, status="running")
    with pytest.raises(ValueError, match="still running"):
        await panel.open_card_session(task_id)


async def test_a_card_whose_window_went_dark_gets_the_reader_in_it(panel, home):
    """A pane whose program exited is still the card's window (2026-09-18, review of B14)."""
    task_id, transcript = _card(panel, home)
    pane = panel.create([sys.executable, "-c", "raise SystemExit(0)"], str(home), card=task_id)
    for _ in range(60):
        if not pane.alive():
            break
        await asyncio.sleep(0.05)
    assert not pane.alive()
    reopened = await panel.open_card_session(task_id)
    assert reopened is pane and len(panel.panes) == 1
    assert pane.argv == [sys.executable, "-m", "misaka", "chat", "--read-only", "--session", transcript]


# ── telling the attempt from the reader ────────────────────────────────────────────────

def test_the_pane_listing_says_which_pane_holds_the_claim(panel, home):
    reader = d.Pane("p1", "reader", [], str(home), card="t_1")
    reader.proc = SimpleNamespace(poll=lambda: None, pid=-9004)
    runner = d.Pane("p2", "runner", [], str(home), card="t_1")
    runner.proc = SimpleNamespace(poll=lambda: None, pid=-9005)
    runner.claim_lock = "net:lock"
    panel.panes = {"p1": reader, "p2": runner}
    rows = {row["id"]: row for row in panel._api("panes.list", {})["panes"]}
    assert rows["p1"]["claimed"] is False
    assert rows["p2"]["claimed"] is True


def test_stopping_a_card_stops_the_attempt_not_the_reader(panel, home, monkeypatch):
    task_id, _ = _card(panel, home, status="running")
    reader = d.Pane("p1", "reader", [], str(home), card=task_id)
    reader.proc = SimpleNamespace(poll=lambda: None, pid=-9004)
    runner = d.Pane("p2", "runner", [], str(home), card=task_id)
    runner.proc = SimpleNamespace(poll=lambda: None, pid=-9005)
    runner.claim_lock = "net:lock"
    panel.panes = {"p1": reader, "p2": runner}
    closed = []
    monkeypatch.setattr(panel, "close", lambda pane_id, **kwargs: closed.append(pane_id))
    panel._api("card.stop", {"task_id": task_id})
    assert closed == ["p2"]


def test_last_order_steers_the_pane_holding_the_claim(monkeypatch):
    from misaka.core.network.wiring import network

    listing = {"panes": [
        {"id": "p1", "card": "t_1", "alive": True, "claimed": False},
        {"id": "p2", "card": "t_1", "alive": True, "claimed": True},
    ]}
    monkeypatch.setattr("misaka.ui.panel.client.request", lambda method, params=None: listing)
    assert network._pane_for_card("t_1")["id"] == "p2"
    listing["panes"] = [listing["panes"][0]]
    assert network._pane_for_card("t_1")["id"] == "p1"          # a reader is still the window
    assert network._pane_for_card("t_2") is None


async def test_a_leftover_card_pane_can_be_closed_but_a_running_one_cannot():
    """B22: `misaka_ally_close` refused every pane carrying a card, so Last Order used `kill`."""
    from misaka.core.network.ally import extension as allies
    from misaka.core.wiring import ToolCollector

    collector = ToolCollector()
    allies.register(collector)
    close = next(t for t in collector.tools if t.name == "misaka_ally_close")
    listing = {"panes": [
        {"id": "p1", "title": "reader", "card": "t_1", "claimed": False},
        {"id": "p2", "title": "runner", "card": "t_1", "claimed": True},
        {"id": "p3", "title": "older daemon", "card": "t_1"},
        {"id": "p4", "title": "a shell", "card": None},
    ]}
    calls = []

    def request(method, params=None):
        calls.append((method, params))
        return listing if method == "panes.list" else {"closed": True}

    from misaka.ui.panel import client
    original, client.request = client.request, request
    try:
        with pytest.raises(ValueError, match="misaka_sister_stop"):
            await close.execute("fixture", {"pane_id": "p2", "confirmed": True}, None, None,
                                SimpleNamespace())
        await close.execute("fixture", {"pane_id": "p1", "confirmed": True}, None, None,
                            SimpleNamespace())
        # An older daemon does not say which pane holds the claim; then no card pane is closed.
        with pytest.raises(ValueError, match="misaka_sister_stop"):
            await close.execute("fixture", {"pane_id": "p3", "confirmed": True}, None, None,
                                SimpleNamespace())
        await close.execute("fixture", {"pane_id": "p4", "confirmed": True}, None, None,
                            SimpleNamespace())
    finally:
        client.request = original
    assert ("pane.close", {"id": "p1"}) in calls
    assert ("pane.close", {"id": "p4"}) in calls
    assert ("pane.close", {"id": "p3"}) not in calls


async def test_last_order_can_see_the_leftover_card_panes_it_may_close():
    """A close it cannot aim is no use: the listing has to show a finished card's pane, and
    which pane holds the claim (2026-09-18, review of B22)."""
    import json as json_module

    from misaka.core.network.ally import extension as allies
    from misaka.core.wiring import ToolCollector

    collector = ToolCollector()
    allies.register(collector)
    listing = next(t for t in collector.tools if t.name == "misaka_ally_list")
    panes = {"panes": [
        {"id": "p1", "title": "reader", "card": "t_1", "alive": True, "claimed": False},
        {"id": "p2", "title": "runner", "card": "t_1", "alive": True, "claimed": True},
        {"id": "p3", "title": "finished", "card": "t_2", "alive": False, "claimed": False},
        {"id": "p4", "title": "dead shell", "card": None, "alive": False},
    ]}
    from misaka.ui.panel import client
    original, client.request = client.request, lambda method, params=None: panes
    try:
        out = await listing.execute("fixture", {}, None, None, SimpleNamespace())
    finally:
        client.request = original
    rows = {r["pane"]: r for r in json_module.loads(out["content"][0]["text"])}
    assert set(rows) == {"p1", "p2", "p3"}, "a dead card pane is listed; a dead shell is not"
    assert rows["p2"]["claimed"] and not rows["p1"]["claimed"]
    assert rows["p3"]["alive"] is False and rows["p3"]["claimed"] is False


async def test_two_requests_for_one_card_do_not_race_for_its_window(panel, home):
    """Stopping the program in a card's window happens before the board's claim decides who won
    it, so a second request must wait rather than replace the program a second time."""
    task_id, _ = _card(panel, home)
    panel.create([*IDLE, task_id], str(home), card=task_id)
    first, second = await asyncio.gather(
        panel.continue_card(task_id, "one"),
        panel.continue_card(task_id, "two"),
        return_exceptions=True,
    )
    hosted = [r for r in (first, second) if isinstance(r, d.Pane)]
    refused = [r for r in (first, second) if isinstance(r, Exception)]
    assert len(hosted) == 1 and len(refused) == 1, (first, second)
    assert len(panel.panes) == 1
    assert hosted[0].alive() and hosted[0].claim_lock
    # Only reachable once the first request has finished claiming: the loser meets an owned
    # card rather than a second chance at its window.
    assert "still running" in str(refused[0]), refused[0]


async def test_a_refused_claim_leaves_the_card_window_alone(panel, home, monkeypatch):
    """The board decides whether an attempt may exist; a window is only emptied once it has
    said yes (2026-09-18, review of B29)."""
    from misaka.core.platform import tasks as db

    task_id, _ = _card(panel, home)
    pane = panel.create([*IDLE, task_id], str(home), card=task_id)
    proc = pane.proc
    monkeypatch.setattr(db, "claim_resume", lambda *a, **k: False)
    with pytest.raises(ValueError, match="could not be claimed"):
        await panel.continue_card(task_id, "redo")
    assert pane.alive() and pane.proc is proc, "the window kept the program it had"
    assert panel.panes[pane.id] is pane


async def test_stopping_a_card_mid_takeover_does_not_crash(panel, home):
    """A pane between programs has no pid of its own; the stop still stands."""
    task_id, _ = _card(panel, home, status="running")
    pane = panel.create([*IDLE, task_id], str(home), card=task_id)
    await panel._stop_program(pane)             # as `_replace_program` leaves it, briefly
    assert pane.proc is None
    reply = panel._api("card.stop", {"task_id": task_id})
    assert reply["stopped"] is True and reply["pid"] is None
    assert pane.id not in panel.panes


async def test_a_card_that_never_ran_starts_from_its_contract_with_the_note_mailed(panel, home):
    """2026-09-18 (B16): a card stopped seconds after its claim has no transcript, so there is
    nothing to resume. Continuing it is its first attempt, and Last Order's note reaches it
    through the card's own inbox rather than a resume flag."""
    from misaka.core.network import messages
    from misaka.core.platform import tasks as db

    task_id, transcript = _card(panel, home, status="stopped")
    os.unlink(transcript)                                   # never got as far as a transcript
    panel._mcon = messages.connect(str(home / "messages.db"))
    pane = await panel.continue_card(task_id, "start with the 1982 edition")
    assert "--resume" not in pane.argv and "--say" not in pane.argv
    row = db.get(panel._board(), task_id)
    assert row["status"] == "running"
    mail = messages.pending(panel._mcon, "10032", task_id=task_id)
    assert [m["body"] for m in mail] == ["start with the 1982 edition"]
    assert mail[0]["to_task"] == task_id and mail[0]["generation"] == row["generation"]
    assert mail[0]["sender"] == "last-order"
    kinds = [r["kind"] for r in panel._board().execute("SELECT kind FROM events WHERE task_id=?", (task_id,))]
    assert "started" in kinds and "continued" not in kinds


async def test_a_card_with_a_transcript_is_still_resumed(panel, home):
    task_id, _ = _card(panel, home)
    panel._mcon = None
    pane = await panel.continue_card(task_id, "carry on")
    assert "--resume" in pane.argv and pane.argv[-1] == "carry on"

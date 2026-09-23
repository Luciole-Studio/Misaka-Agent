"""A card is submitted only by a turn that called ``misaka_card_complete``.

2026-09-18 (B27): a turn ending in plain text used to complete the card whatever it had
produced, so every note Last Order sent a running Sister -- "continue", "not approved,
rework", a side errand -- ended a turn and the turn ended the card, even one whose reply
said "not submitting yet". Ten of ten submissions that day answered a message, never the
contract. Now completion is explicit, gated on the contract deliverable, and a turn without
it leaves the card running."""
import os
import time

import pytest

from misaka.core.network import todo, worker
from misaka.core.platform import tasks

BODY = ("## research question\nWhy?\n\n## deliverable\nchoson-tusol-genealogy.md\n"
        "Write it under the deliverable location the card names.\n\n## boundaries\nnone\n")


class _Moments:
    def __init__(self):
        self.sent = []

    def send_message(self, message, options):
        self.sent.append((message, options))


class _Agent:
    async def waitForIdle(self):
        return None


class _Session:
    def __init__(self):
        self.moments = _Moments()
        self.agent = _Agent()


def _board(tmp_path, request, body=BODY):
    """A running card the way the daemon leaves one: file written (the file is the contract),
    row claimed under ``lock1`` at generation 1, output directory set."""
    from misaka.core.platform import cards
    con = tasks.connect(str(tmp_path / "board.db"))
    request.addfinalizer(con.close)
    out = tmp_path / "nodes" / "cards" / "t1"
    out.mkdir(parents=True)
    tid = cards.create(con, str(tmp_path), "T5 genealogy", body, "10032")
    con.execute("UPDATE tasks SET status='running', claim_lock='lock1', claim_expires=?, output_dir=?, "
                "worker_pid=? WHERE id=?", (int(time.time()) + 1800, str(out), os.getpid(), tid))
    from misaka.core.network import worker
    worker.record_output_baseline(con, tasks.get(con, tid))    # as the runtime does before the first turn
    return con, tid, out


def _part(monkeypatch, con, tid):
    monkeypatch.setenv("MISAKA_USAGE_TASK_ID", tid)
    monkeypatch.setenv("MISAKA_USAGE_CLAIM_LOCK", "lock1")
    monkeypatch.setenv("MISAKA_USAGE_GENERATION", "1")
    monkeypatch.delenv("MISAKA_SISTER_OWNER_CLAIM_LOCK", raising=False)
    part = todo.TodoPart(tid, "10032")
    part._con = con
    part.attach(_Session())
    return part


def _turn(reason, text):
    return {"type": "agent_end", "messages": [
        {"role": "assistant", "stopReason": reason, "content": [{"type": "text", "text": text}]}]}


def _tool(part, name):
    return next(tool for tool in part.tools if tool.name == name)


async def _end_turn(part, text="done for now"):
    await part.agent_end(_turn("stop", text))
    await part.agent_settled()


def test_the_contract_deliverable_is_read_from_the_card_body(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    task = {"body": BODY, "output_dir": str(out)}
    assert worker.contract_deliverable(task) == "choson-tusol-genealogy.md"
    assert worker.missing_deliverable(task) == "choson-tusol-genealogy.md"
    (out / "choson-tusol-genealogy.md").write_text("")
    assert worker.missing_deliverable(task) == "choson-tusol-genealogy.md"     # empty is not delivered
    (out / "choson-tusol-genealogy.md").write_text("# report\n")
    assert worker.missing_deliverable(task) is None
    assert worker.missing_deliverable({"body": "## research question\nq\n", "output_dir": str(out)}) is None
    assert worker.contract_deliverable({"body": "## deliverable\n`quoted.md`\n"}) == "quoted.md"


async def test_a_reply_turn_leaves_the_card_running_and_says_so_once(tmp_path, monkeypatch, request):
    con, tid, _out = _board(tmp_path, request)
    part = _part(monkeypatch, con, tid)
    await _end_turn(part, "Received, working on the genealogy next.")
    await _end_turn(part, "Still working.")
    assert tasks.get(con, tid)["status"] == "running"
    held = [m for m, _ in part.session.moments.sent if m["customType"] == "submission-held"]
    assert len(held) == 1
    assert "choson-tusol-genealogy.md" in held[0]["content"]
    assert "keep working" in held[0]["content"]
    assert tasks.latest_payload(con, tid, "submitted") is None


async def test_complete_refuses_while_the_deliverable_is_missing(tmp_path, monkeypatch, request):
    con, tid, _out = _board(tmp_path, request)
    part = _part(monkeypatch, con, tid)
    with pytest.raises(ValueError, match="choson-tusol-genealogy.md"):
        await _tool(part, "misaka_card_complete").execute(None, {"summary": "all done"}, None, None, None)
    await _end_turn(part, "all done")
    assert tasks.get(con, tid)["status"] == "running"


async def test_complete_then_a_turn_end_submits_the_card(tmp_path, monkeypatch, request):
    con, tid, out = _board(tmp_path, request)
    part = _part(monkeypatch, con, tid)
    (out / "choson-tusol-genealogy.md").write_text("# genealogy\nKwon Kun -> Toegye.\n")
    reply = await _tool(part, "misaka_card_complete").execute(
        None, {"summary": "Genealogy traced from Kwon Kun to the Horak debate."}, None, None, None)
    assert "Completion recorded" in reply["content"][0]["text"]
    await _end_turn(part, "Finished; see the deliverable.")
    row = tasks.get(con, tid)
    assert row["status"] == "done"
    assert "Kwon Kun" in (tasks.latest_payload(con, tid, "submitted") or "")


async def test_completion_does_not_carry_into_a_turn_that_did_not_end_cleanly(tmp_path, monkeypatch, request):
    con, tid, out = _board(tmp_path, request)
    part = _part(monkeypatch, con, tid)
    (out / "choson-tusol-genealogy.md").write_text("# genealogy\n")
    await _tool(part, "misaka_card_complete").execute(None, {"summary": "done"}, None, None, None)
    await part.agent_end(_turn("aborted", ""))
    await part.agent_settled()
    assert tasks.get(con, tid)["status"] == "running"     # an aborted turn submits nothing
    await _end_turn(part, "and now it ends")
    assert tasks.get(con, tid)["status"] == "done"        # the recorded completion still stands


def test_bookkeeping_lists_know_the_tool():
    from misaka.core.network.wiring.collaboration import SISTER_TOOLS
    from misaka.core.platform.session import BOOKKEEPING_TOOLS
    assert "misaka_card_complete" in BOOKKEEPING_TOOLS
    assert "misaka_card_complete" in SISTER_TOOLS
    assert "misaka_card_complete" in worker.COMPLETION_INSTRUCTIONS

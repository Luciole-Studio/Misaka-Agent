"""A card is submitted only by a turn that called ``misaka_card_complete``.

2026-09-18 (B27): a turn ending in plain text used to complete the card whatever it had
produced, so every note Last Order sent a running Sister -- "continue", "not approved,
rework", a side errand -- ended a turn and the turn ended the card, even one whose reply
said "not submitting yet". Ten of ten submissions that day answered a message, never the
contract. Now completion is explicit, gated on the contract deliverable, and a turn without
it leaves the card running."""
import asyncio
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
    isStreaming = False

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
    (out / "choson-tusol-genealogy.md").write_text("", encoding="utf-8")
    assert worker.missing_deliverable(task) == "choson-tusol-genealogy.md"     # empty is not delivered
    (out / "choson-tusol-genealogy.md").write_text("# report\n", encoding="utf-8")
    assert worker.missing_deliverable(task) is None
    assert worker.missing_deliverable({"body": "## research question\nq\n", "output_dir": str(out)}) is None
    assert worker.contract_deliverable({"body": "## deliverable\n`quoted.md`\n"}) == "quoted.md"


async def test_a_reply_turn_leaves_the_card_running_and_says_so_once(tmp_path, monkeypatch, request):
    con, tid, _out = _board(tmp_path, request)
    part = _part(monkeypatch, con, tid)
    await _end_turn(part, "Received, working on the genealogy next.")
    await _end_turn(part, "Still working.")
    assert tasks.get(con, tid)["status"] == "running"
    held = [(m, options) for m, options in part.session.moments.sent if m["customType"] == "submission-held"]
    assert len(held) == 1
    assert "choson-tusol-genealogy.md" in held[0][0]["content"]
    assert "keep working" in held[0][0]["content"]
    # It starts the turn itself: in a research run nobody else writes to the card again (2026-09-27).
    assert held[0][1]["triggerTurn"] is True
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
    (out / "choson-tusol-genealogy.md").write_text("# genealogy\nKwon Kun -> Toegye.\n", encoding="utf-8")
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
    (out / "choson-tusol-genealogy.md").write_text("# genealogy\n", encoding="utf-8")
    await _tool(part, "misaka_card_complete").execute(None, {"summary": "done"}, None, None, None)
    await part.agent_end(_turn("aborted", ""))
    await part.agent_settled()
    assert tasks.get(con, tid)["status"] == "running"     # an aborted turn submits nothing
    await _end_turn(part, "and now it ends")
    assert tasks.get(con, tid)["status"] == "done"        # the recorded completion still stands


async def test_a_run_that_ends_in_an_error_fails_the_attempt_instead_of_holding_the_card(tmp_path, monkeypatch, request):
    """2026-09-27: eight research panes whose first turn died on a broken LCM cache sat with
    their cards running, claims heartbeated and nobody to type. A headless card fails the same
    attempt; the board's failure model takes it from there."""
    con, tid, _out = _board(tmp_path, request)
    part = _part(monkeypatch, con, tid)
    await part.agent_end({"type": "agent_end", "messages": [
        {"role": "assistant", "stopReason": "error", "content": [],
         "errorMessage": "Not a MISAKA LCM cache; leave existing files untouched: /p/.misaka/lcm"}]})
    await part.agent_settled()
    row = tasks.get(con, tid)
    assert row["status"] == "ready" and row["claim_lock"] is None and row["consecutive_failures"] == 1
    assert "Not a MISAKA LCM cache" in row["last_failure_error"]
    assert "Not a MISAKA LCM cache" in (tasks.latest_payload(con, tid, "failed") or "")


async def test_a_cut_reply_gets_one_more_turn_and_a_second_cut_fails_the_attempt(tmp_path, monkeypatch, request):
    con, tid, _out = _board(tmp_path, request)
    part = _part(monkeypatch, con, tid)
    cut = {"type": "agent_end", "messages": [
        {"role": "assistant", "stopReason": "length", "content": [], "usage": {"output": 32000}}]}
    await part.agent_end(cut)
    await part.agent_settled()
    assert tasks.get(con, tid)["status"] == "running"
    [(message, options)] = [(m, o) for m, o in part.session.moments.sent if m["customType"] == "submission-incomplete"]
    assert "cut off" in message["content"] and "32000 tokens" in message["content"]
    assert options == {"deliverAs": "followUp", "triggerTurn": True}
    await part.agent_end(cut)
    await part.agent_settled()
    row = tasks.get(con, tid)
    assert row["status"] == "ready" and row["consecutive_failures"] == 1
    assert "32000 tokens" in row["last_failure_error"]


async def test_an_aborted_run_is_a_person_at_the_pane_and_changes_nothing(tmp_path, monkeypatch, request):
    con, tid, _out = _board(tmp_path, request)
    part = _part(monkeypatch, con, tid)
    await part.agent_end(_turn("aborted", ""))
    await part.agent_settled()
    row = tasks.get(con, tid)
    assert row["status"] == "running" and row["consecutive_failures"] == 0


def test_a_deliverable_written_by_an_earlier_attempt_is_still_the_cards_product(tmp_path, request):
    """2026-09-27: a divergence card reopened after a resume declared again without rewriting its
    file; only files changed in the attempt were listed, and divergence.md lost its registration."""
    con, tid, out = _board(tmp_path, request)
    (out / "choson-tusol-genealogy.md").write_text("# genealogy\n", encoding="utf-8")    # written by attempt 1
    con.execute("UPDATE tasks SET generation=2 WHERE id=?", (tid,))    # reopened to declare again
    worker.record_output_baseline(con, tasks.get(con, tid))            # attempt 2 starts with the file there
    submission = worker.build_submission(con, tasks.get(con, tid), "declared again")
    assert (out / "choson-tusol-genealogy.md").relative_to(tmp_path).as_posix() in submission["artifacts"]


def test_the_completion_tool_is_listed_where_sisters_and_the_contract_name_it():
    from misaka.core.network.wiring.collaboration import SISTER_TOOLS
    assert "misaka_card_complete" in SISTER_TOOLS
    assert "misaka_card_complete" in worker.COMPLETION_INSTRUCTIONS


async def test_an_earlier_attempts_declaration_does_not_submit_the_next(tmp_path, monkeypatch, request):
    """A card sent back to ready keeps its generation; the next attempt's first turn submitted it
    with the last attempt's summary -- a reviewer's request for changes bypassed, silently."""
    import os

    from misaka.core.network import todo
    from misaka.core.platform import tasks
    con, tid, out = _board(tmp_path, request)
    con.execute("UPDATE tasks SET reviewer='10033' WHERE id=?", (tid,))
    con.commit()
    part = _part(monkeypatch, con, tid)
    (out / "choson-tusol-genealogy.md").write_text("# v1\n", encoding="utf-8")
    await _tool(part, "misaka_card_complete").execute(None, {"summary": "v1 summary"}, None, None, None)
    await _end_turn(part, "done")
    assert tasks.claim_review(con, tid, "10033", "rv1", generation=1)
    assert tasks.request_review_changes(con, tid, "rv1", "Section 2 is wrong; rewrite it.", generation=1)
    assert tasks.claim(con, tid, "lock2", generation=1, pid=os.getpid())
    monkeypatch.setenv("MISAKA_USAGE_CLAIM_LOCK", "lock2")
    rework = todo.TodoPart(tid, "10032")
    rework._con = con
    rework.attach(_Session())
    await rework.agent_start()
    await _end_turn(rework, "Read the feedback; I will rewrite section 2 next.")
    assert tasks.get(con, tid)["status"] == "running"
    assert con.execute("SELECT COUNT(*) FROM events WHERE task_id=? AND kind='submitted'", (tid,)).fetchone()[0] == 1


REFUSED = ("Cannot complete: the contract deliverable `choson-tusol-genealogy.md` is not under the output "
           "directory yet. Write it, then call misaka_card_complete again.")


async def _refused(part):
    await part.tool_result({"toolName": "misaka_card_complete", "isError": True, "input": {}, "details": {},
                            "content": [{"type": "text", "text": REFUSED}]})


async def test_a_card_left_idle_after_the_notice_ends_its_attempt_unsettled(tmp_path, monkeypatch, request):
    """2026-10-27: a card whose completion was refused ended its turns without the call; after the one
    notice nothing would wake it again, and it sat running. Its attempt now ends as a card process
    that exits without settling does: one failure, and the next attempt is told why."""
    con, tid, _out = _board(tmp_path, request)
    monkeypatch.setattr(todo, "UNSETTLED_GRACE_SECONDS", 0)
    part = _part(monkeypatch, con, tid)
    await _refused(part)
    await _end_turn(part, "I could not complete it.")              # the notice
    assert part._idle_watch is None
    await _end_turn(part, "Waiting.")                              # nothing after it
    await part._idle_watch
    row = tasks.get(con, tid)
    assert row["status"] == "ready" and row["claim_lock"] is None and row["consecutive_failures"] == 1
    assert "nothing was left to wake the session" in row["last_failure_error"]
    assert "choson-tusol-genealogy.md" in row["last_failure_error"]
    assert "Cannot complete" in row["last_failure_error"]          # what the refusal said
    assert '"failure_kind": "protocol_violation"' in tasks.latest_payload(con, tid, "retry_scheduled")
    assert "## Previous attempt" in worker.card_prompt({**dict(row), "_handoffs": [], **worker.card_extras(con, row)})


async def test_a_turn_that_starts_keeps_the_card_running(tmp_path, monkeypatch, request):
    con, tid, _out = _board(tmp_path, request)
    monkeypatch.setattr(todo, "UNSETTLED_GRACE_SECONDS", 0)
    part = _part(monkeypatch, con, tid)
    await _end_turn(part)
    await _end_turn(part)
    watch = part._idle_watch
    await part.agent_start()                                       # a message woke her
    await asyncio.gather(watch, return_exceptions=True)
    assert watch.cancelled() and tasks.get(con, tid)["status"] == "running"


async def test_a_session_already_running_again_keeps_the_card_running(tmp_path, monkeypatch, request):
    con, tid, _out = _board(tmp_path, request)
    monkeypatch.setattr(todo, "UNSETTLED_GRACE_SECONDS", 0)
    part = _part(monkeypatch, con, tid)
    await _end_turn(part)
    await _end_turn(part)
    part.session.isStreaming = True
    await part._idle_watch
    assert tasks.get(con, tid)["status"] == "running"


async def test_a_background_agent_still_out_keeps_the_card_running(tmp_path, monkeypatch, request):
    """Its notice starts her next turn."""
    from misaka.core.subagent import extension as subagent

    con, tid, _out = _board(tmp_path, request)
    monkeypatch.setattr(todo, "UNSETTLED_GRACE_SECONDS", 0)
    monkeypatch.setattr(subagent, "has_background_tasks", lambda: True)
    part = _part(monkeypatch, con, tid)
    await _end_turn(part)
    await _end_turn(part)
    for _ in range(5):
        await asyncio.sleep(0)
    assert not part._idle_watch.done() and tasks.get(con, tid)["status"] == "running"
    await part.session_shutdown()

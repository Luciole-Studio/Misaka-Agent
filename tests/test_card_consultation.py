"""A message to a finished card is a consultation: her session answers it, and the card stays as
it was (R40). 2026-10-25: Last Order's note woke delivered card Ea as a new attempt; her window
hung loading (R38) and was closed, which stopped the card, and the research run's cascade then
stopped the two synthesis cards built on her -- a delivered card treated as a failed parent. pi's
durable harness keeps a finished task's receipt immutable and runs a question to a finished
subagent as new work of its conversation; CCB keeps its runtime agent state apart from its task
list. Here the consultation is a run of the card (task_runs, phase ``consult``), never its attempt."""
import asyncio
import os
import sys
import time

import pytest
from test_card_pane_reuse import IDLE, _card, home, panel  # noqa: F401 - fixtures

from misaka.core.network import messages
from misaka.core.network.sister_runtime import SisterRuntime
from misaka.core.platform import admission, tasks
from misaka.ui.panel import daemon as d

LOCK = "consult:test"


def _parent_and_child(panel, home):  # noqa: F811
    """Delivered card A, and card X built on A and on F, which is still working (as X3 was)."""
    a, _ = _card(panel, home, title="Ea delivery")
    f, _ = _card(panel, home, title="Fa still working", status="ready")
    x, _ = _card(panel, home, title="X3 synthesis", status="todo")
    con = panel._board()
    tasks.link_dependencies(con, [a, f], x)
    con.commit()
    return con, a, x


# ── the board ───────────────────────────────────────────────────────────────────────────────

def test_a_consultation_leaves_the_card_as_it_is_and_a_card_has_one(panel, home):  # noqa: F811
    a, _ = _card(panel, home, title="A")
    con = panel._board()
    run = tasks.start_consultation(con, a, generation=1, lock=LOCK, asker={"sender": "last-order"})
    row = tasks.get(con, a)
    assert (row["status"], row["generation"], row["claim_lock"]) == ("done", 1, None)
    assert row["current_run_id"] != run["id"], "the delivering run stays the card's run"
    assert (run["phase"], run["status"]) == ("consult", "running")
    again = tasks.start_consultation(con, a, generation=1, lock="consult:other")
    assert again["id"] == run["id"], "a second message joins the running consultation"
    assert tasks.start_consultation(con, a, generation=2, lock="x") is None, "not at another generation"
    assert tasks.end_consultation(con, run["id"], "consult:other", "done") is None, "fenced by its lock"
    ended = tasks.end_consultation(con, run["id"], LOCK, "done")
    assert ended["status"] == "done" and tasks.live_consultation(con, a) is None
    assert tasks.get(con, a)["status"] == "done"


def test_an_unfinished_card_is_not_consulted(panel, home):  # noqa: F811
    for status in ("ready", "todo", "running", "blocked"):
        tid, _ = _card(panel, home, title=f"T {status}", status=status)
        assert tasks.start_consultation(panel._board(), tid, generation=1, lock=LOCK) is None, status


def test_no_attempt_is_claimed_while_she_answers(panel, home):  # noqa: F811
    """One session, one writer: a rework or a fresh attempt waits for the answer to end."""
    a, _ = _card(panel, home, title="A")
    con = panel._board()
    run = tasks.start_consultation(con, a, generation=1, lock=LOCK)
    assert not tasks.claim_resume(con, a, "lock2", os.getpid(), expected_generation=1)
    tasks.reopen_task(con, a, target_status="ready", expected_generation=1, invalidate_descendants=False)
    assert not tasks.claim(con, a, "lock3", generation=2)
    assert "answering a message" in tasks.claim_refusal(a)
    tasks.end_consultation(con, run["id"], LOCK, "done")
    assert tasks.claim(con, a, "lock3", generation=2)


def test_a_consultation_holds_an_admission_slot_while_it_runs(panel, home):  # noqa: F811
    a, _ = _card(panel, home, title="A")
    con = panel._board()
    run = tasks.start_consultation(con, a, generation=1, lock=LOCK)
    assert admission.occupied(con) == {a: "10032"}
    tasks.end_consultation(con, run["id"], LOCK, "done")
    assert admission.occupied(con) == {}


def test_a_consultation_whose_process_is_gone_is_abandoned(panel, home):  # noqa: F811
    a, _ = _card(panel, home, title="A")
    b, _ = _card(panel, home, title="B")
    con = panel._board()
    import subprocess
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    dead = tasks.start_consultation(con, a, generation=1, lock=LOCK, pid=gone.pid)
    alive = tasks.start_consultation(con, b, generation=1, lock=LOCK, pid=os.getpid())
    ended = tasks.abandon_consultations(con)
    assert [run["id"] for run in ended] == [dead["id"]]
    assert tasks.get_run(con, dead["id"])["status"] == "stopped"
    assert tasks.get_run(con, alive["id"])["status"] == "running"
    unbound = tasks.end_consultation(con, alive["id"], LOCK, "done") and tasks.start_consultation(
        con, b, generation=1, lock="consult:2")
    assert tasks.abandon_consultations(con, now=int(time.time()) + tasks.CONSULTATION_STALE_SECONDS)[0]["id"] \
        == unbound["id"], "one nobody took up is abandoned once it is stale"


# ── the daemon ──────────────────────────────────────────────────────────────────────────────

async def test_closing_her_window_ends_the_consultation_not_the_card(panel, home):  # noqa: F811
    """R40 itself: the window closed on a consultation of a delivered card. The card stays done,
    and the card built on her keeps waiting for its other parent instead of being stopped."""
    con, a, x = _parent_and_child(panel, home)
    pane, run_id = await panel.consult_card(a, "a question", asker={"sender": "last-order"})

    panel.close(pane.id)

    row = tasks.get(con, a)
    assert (row["status"], row["generation"]) == ("done", 1)
    run = tasks.get_run(con, run_id)
    assert (run["status"], run["summary"]) == ("stopped", "her window was closed")
    assert tasks.dependency_state(con, x)[0] == "waiting", "not failed: her delivery stands"


async def test_a_reopened_window_reads_the_question_from_its_file(panel, home):  # noqa: F811
    """Her closed window is reopened on her session with the question as its turn; the question
    goes in a file, as every message a pane's program starts with does (issue #13)."""
    from misaka.utils.prompt_file import readPromptFile
    _con, a, _x = _parent_and_child(panel, home)
    question = "which statute? " * 4000                  # past a Windows command line (32,767)
    pane, _run_id = await panel.consult_card(a, question, asker={"sender": "last-order"})
    assert pane.argv[-4:-1] == ["--resume", "--consult", "--say-file"]
    assert readPromptFile(pane.argv[-1]) == question
    panel.close(pane.id)
    assert not os.path.exists(pane.argv[-1])


async def test_a_question_that_cannot_be_written_opens_no_consultation(panel, home, monkeypatch):  # noqa: F811
    con, a, _x = _parent_and_child(panel, home)

    def full(*_args):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(d, "writePromptToTempFile", full)
    with pytest.raises(OSError):
        await panel.consult_card(a, "a question", asker={"sender": "last-order"})
    assert tasks.live_consultation(con, a) is None
    assert (tasks.get(con, a)["status"], tasks.get(con, a)["generation"]) == ("done", 1)


async def test_a_card_asking_is_told_when_the_answer_never_comes(panel, home):  # noqa: F811
    _con, a, _x = _parent_and_child(panel, home)
    asker, _ = _card(panel, home, title="K1a asking", status="running")
    pane, _run_id = await panel.consult_card(a, "which statute?", asker={"sender": "10032", "sender_task": asker})
    panel.close(pane.id, reason="the panel shut down")
    [note] = messages.pending(panel._mailbox(), "10032", task_id=asker)
    assert "did not answer your message: the panel shut down" in note["body"]
    assert note["generation"] == 1 and note["to_task"] == asker


async def test_her_process_ending_settles_the_consultation(panel, home, monkeypatch):  # noqa: F811
    con, a, _x = _parent_and_child(panel, home)
    monkeypatch.setattr(d, "CARD_SHELL", [sys.executable, "-c", "import sys; sys.exit(3)"])
    pane, run_id = await panel.consult_card(a, "a question")
    await asyncio.wait_for(asyncio.to_thread(pane.proc.wait), 10)
    panel._watch_consultations(tasks)
    run = tasks.get_run(con, run_id)
    assert (run["status"], run["exit_code"]) == ("failed", 3) and "exited with code 3" in run["summary"]
    assert pane.consult is None and tasks.get(con, a)["status"] == "done"


async def test_rework_takes_over_from_a_consultation(panel, home):  # noqa: F811
    con, a, _x = _parent_and_child(panel, home)
    _pane, run_id = await panel.consult_card(a, "a question")
    await panel.continue_card(a, "redo the timeline")
    assert tasks.get_run(con, run_id)["summary"] == "superseded by a new attempt"
    row = tasks.get(con, a)
    assert (row["status"], row["generation"]) == ("running", 2)


async def test_her_open_window_takes_the_message_through_her_inbox(panel, home):  # noqa: F811
    """No second process, no reload of her session (R38 took up to an hour to reload one)."""
    con, a, _x = _parent_and_child(panel, home)
    first, run_id = await panel.consult_card(a, "first question")
    proc = first.proc
    tasks.end_consultation(con, run_id, first.consult[1], "done")       # she answered
    panel._watch_consultations(tasks)
    assert first.consult is None and first.alive()

    again, second = await panel.consult_card(a, "second question", asker={"sender": "last-order"})

    assert again is first and again.proc is proc and second != run_id
    assert tasks.get_run(con, second)["pid"] == proc.pid
    [mail] = messages.pending(panel._mailbox(), "10032", task_id=a)
    assert mail["body"] == "second question" and mail["generation"] == 1


async def test_a_card_that_never_ran_has_nothing_to_answer_from(panel, home):  # noqa: F811
    con = panel._board()
    from misaka.core.platform import cards
    tid = cards.create(con, str(home), "Never ran", "## deliverable\nx.md\n", "10032")
    tasks.mark_stopped(con, tid)
    con.commit()
    with pytest.raises(ValueError, match="stopped before it ran"):
        await panel.consult_card(tid, "a question")


async def test_a_card_stop_reaches_one_consultation(panel, home):  # noqa: F811
    con, a, _x = _parent_and_child(panel, home)
    _pane, run_id = await panel.consult_card(a, "a question")
    reply = panel._api("card.stop", {"task_id": a, "consultation": run_id,
                                     "reason": "the research run was stopped"})
    assert reply == {"stopped": True}
    assert tasks.get_run(con, run_id)["summary"] == "the research run was stopped"


# ── Last Order waiting (B121) ───────────────────────────────────────────────────────────────

async def test_last_order_waits_for_the_consultation_not_the_card(panel, home):  # noqa: F811
    """B121: a message put into a finished card's open window left Last Order nothing to wait on,
    and she decided without the answer. The card stays done; the wait is on the consultation."""
    a, _ = _card(panel, home, title="A")
    con = panel._board()
    run = tasks.start_consultation(con, a, generation=1, lock=LOCK)
    runtime = SisterRuntime(None, lambda: con, dict)

    early = await runtime.output(a, block=True, timeout_ms=200, signal=None)
    assert early["retrieval_status"] == "timeout"

    async def answer():
        await asyncio.sleep(0.3)
        tasks.end_consultation(con, run["id"], LOCK, "done")

    answering = asyncio.create_task(answer())
    result = await runtime.output(a, block=True, timeout_ms=10_000, signal=None)
    await answering
    assert result["retrieval_status"] == "success"
    assert result["task"]["boardStatus"] == "done"
    assert result["task"]["consultation"]["status"] == "done"

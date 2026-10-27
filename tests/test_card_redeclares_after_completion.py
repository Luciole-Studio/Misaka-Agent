"""Completion does not end a Sister's session. A note from Last Order that crosses the
completion, or another Sister's DM, starts more turns in it, and the deliverables those turns
change stayed declared under the old digests; the run's settle then saw bytes that did not match
the declaration (2026-09-24: 18 changed paths on 6 cards, a whole run failed). The declaration
now follows the work: at the end of any further clean turn the runtime rebuilds the submission
from the output folder and records it as a newer ``submitted`` row, without a model turn."""
import hashlib
import json
import os
import time

import pytest

from misaka.core.network import todo
from misaka.core.platform import tasks

BODY = ("## research question\nWhy?\n\n## deliverable\nreport.md\n"
        "Write it under the deliverable location the card names.\n\n## boundaries\nnone\n")


class _Moments:
    def send_message(self, message, options):
        return None


class _Agent:
    async def waitForIdle(self):
        return None


class _Session:
    def __init__(self, session_file):
        self.moments = _Moments()
        self.agent = _Agent()
        self.sessionManager = type("SM", (), {"sessionFile": session_file})()


@pytest.fixture
def card(tmp_path, monkeypatch, request):
    from misaka.core.network import worker
    from misaka.core.platform import cards
    con = tasks.connect(str(tmp_path / "board.db"))
    request.addfinalizer(con.close)
    out = tmp_path / "nodes" / "cards" / "t1"
    out.mkdir(parents=True)
    tid = cards.create(con, str(tmp_path), "T1 report", BODY, "10032")
    session_file = str(tmp_path / "sister.jsonl")
    con.execute("UPDATE tasks SET status='running', claim_lock='lock1', claim_expires=?, output_dir=?, "
                "worker_pid=?, session_file=? WHERE id=?",
                (int(time.time()) + 1800, str(out), os.getpid(), session_file, tid))
    worker.record_output_baseline(con, tasks.get(con, tid))
    monkeypatch.setenv("MISAKA_USAGE_TASK_ID", tid)
    monkeypatch.setenv("MISAKA_USAGE_CLAIM_LOCK", "lock1")
    monkeypatch.setenv("MISAKA_USAGE_GENERATION", "1")
    monkeypatch.delenv("MISAKA_SISTER_OWNER_CLAIM_LOCK", raising=False)
    part = todo.TodoPart(tid, "10032")
    part._con = con
    part.attach(_Session(session_file))
    return con, tid, out, part


def _turn(reason, text):
    return {"type": "agent_end", "messages": [
        {"role": "assistant", "stopReason": reason, "content": [{"type": "text", "text": text}]}]}


async def _end_turn(part, text="done for now", reason="stop"):
    await part.agent_end(_turn(reason, text))
    await part.agent_settled()


def _submissions(con, tid):
    return [json.loads(r["payload"]) for r in con.execute(
        "SELECT payload FROM events WHERE task_id=? AND kind='submitted' ORDER BY id", (tid,))]


async def _complete(con, tid, out, part):
    (out / "report.md").write_text("first version\n", encoding="utf-8")
    from misaka.core.network import worker
    worker.declare_completion(con, tasks.get(con, tid), "delivered")   # what misaka_card_complete records
    await _end_turn(part, "delivered")
    assert tasks.get(con, tid)["status"] == "done"
    assert len(_submissions(con, tid)) == 1


async def test_a_later_turn_that_changed_the_deliverable_declares_it_again(card):
    con, tid, out, part = card
    await _complete(con, tid, out, part)
    (out / "report.md").write_bytes(b"revised after Last Order's note\n")
    (out / "extra.csv").write_text("a,b\n", encoding="utf-8")

    await _end_turn(part, "revised as asked")

    rows = _submissions(con, tid)
    assert len(rows) == 2 and rows[-1]["summary"] == rows[0]["summary"]
    rel = (out / "report.md").relative_to(out.parents[2]).as_posix()
    assert rows[-1]["artifact_digests"][rel] == hashlib.sha256(b"revised after Last Order's note\n").hexdigest()
    assert any(path.endswith("extra.csv") for path in rows[-1]["artifacts"])
    row = tasks.get(con, tid)
    assert row["status"] == "done" and int(row["generation"]) == 1
    assert tasks.latest_payload(con, tid, "redeclared", generation=1)


async def test_a_turn_that_changed_nothing_declares_nothing(card):
    con, tid, out, part = card
    await _complete(con, tid, out, part)
    await _end_turn(part, "just a reply to Last Order")
    assert len(_submissions(con, tid)) == 1
    assert tasks.latest_payload(con, tid, "redeclared", generation=1) is None


async def test_an_aborted_turn_and_another_session_do_not_declare(card):
    con, tid, out, part = card
    await _complete(con, tid, out, part)
    (out / "report.md").write_text("changed\n", encoding="utf-8")
    await _end_turn(part, "", reason="aborted")
    assert len(_submissions(con, tid)) == 1
    part.session.sessionManager.sessionFile = str(out.parents[2] / "someone-else.jsonl")
    await _end_turn(part, "a reader pane's turn")
    assert len(_submissions(con, tid)) == 1


async def _note(part, **params):
    tool = next(t for t in part.tools if t.name == "misaka_card_note")
    return await tool.execute("fixture", {"text": "note", **params}, None, None, None)


async def test_findings_recorded_after_the_completion_are_declared_with_the_card(card):
    """2026-10-01 (B110): card A finished, then answered card B's question with better-dated
    evidence; misaka_card_note refused ("Card ownership changed..."), and the evidence stayed only
    in her message to B -- the red team and the report never saw it."""
    con, tid, out, part = card
    await _complete(con, tid, out, part)
    finding = {"text": "Wukong Bike stopped on 13 June 2017", "claim_type": "fact", "source_file": "report.md"}

    result = await _note(part, findings=[finding])

    assert "recorded 1 finding(s)" in result["content"][0]["text"]
    await _end_turn(part, "answered card B")
    rows = _submissions(con, tid)
    assert len(rows) == 2 and [f["text"] for f in rows[-1]["findings"]] == [finding["text"]]
    assert tasks.latest_payload(con, tid, "redeclared", generation=1)


async def test_a_session_that_no_longer_holds_the_card_still_cannot_record_evidence(card):
    con, tid, out, part = card
    await _complete(con, tid, out, part)
    con.execute("UPDATE tasks SET generation=2 WHERE id=?", (tid,))    # a newer attempt owns the card
    with pytest.raises(ValueError, match="ownership changed"):
        await _note(part, findings=[{"text": "late", "claim_type": "fact"}])


async def test_a_red_team_revising_its_critique_after_completion_redeclares_it(card):
    """Its critique carries the claim of the attempt that recorded it; read against the done card
    (no claim left) it was not found, and the revised critique was never declared (0.18.10 sweep)."""
    from misaka.core.platform import tasks
    con, tid, out, part = card
    con.execute("CREATE TABLE IF NOT EXISTS research_cards (task_id TEXT PRIMARY KEY, run_id TEXT, branch_id TEXT, "
                "kind TEXT, round INT, local_id TEXT, depends_json TEXT, created_at INT)")
    con.execute("INSERT INTO research_cards VALUES (?,?,?,?,?,?,?,?)", (tid, "r", "b", "red_team", 1, None, "[]", 0))
    con.commit()
    tasks.add_event(con, tid, "research_critique", {"issues": [], "claim_lock": "lock1"}, generation=1, claim_lock="lock1")
    await _complete(con, tid, out, part)
    (out / "report.md").write_bytes(b"revised critique after Last Order's note\n")
    await _end_turn(part, "revised as asked")
    assert len(_submissions(con, tid)) == 2
    assert tasks.latest_payload(con, tid, "redeclare_failed", generation=1) is None


# ── consultations (R40): a finished card's later turns are runs of the card, not attempts ──

def _consultation(con, tid):
    return con.execute("SELECT * FROM task_runs WHERE task_id=? AND phase='consult' ORDER BY started_at DESC, "
                       "attempt DESC LIMIT 1", (tid,)).fetchone()


async def test_a_turn_typed_in_her_finished_window_is_a_consultation(card):
    con, tid, out, part = card
    await _complete(con, tid, out, part)

    await part.agent_start()
    run = _consultation(con, tid)
    assert (run["status"], run["pid"], run["generation"]) == ("running", os.getpid(), 1)
    assert tasks.get(con, tid)["status"] == "done"
    (out / "report.md").write_bytes(b"corrected while answering\n")
    await _end_turn(part, "answered")

    assert _consultation(con, tid)["status"] == "done"
    assert len(_submissions(con, tid)) == 2, "what the answer changed is declared again"
    row = tasks.get(con, tid)
    assert (row["status"], row["generation"]) == ("done", 1)


async def test_the_consultation_opened_for_her_window_is_the_one_she_answers(card):
    con, tid, out, part = card
    await _complete(con, tid, out, part)
    opened = tasks.start_consultation(con, tid, generation=1, lock="consult:daemon", pid=os.getpid(),
                                      asker={"sender": "last-order"})
    await part.agent_start()
    await _end_turn(part, "answered")
    assert tasks.get_run(con, opened["id"])["status"] == "done"
    assert con.execute("SELECT COUNT(*) FROM task_runs WHERE phase='consult'").fetchone()[0] == 1


@pytest.mark.parametrize(("reason", "status", "why"), [
    ("aborted", "stopped", "interrupted in her window"),
    ("error", "failed", "request error"),
])
async def test_a_consultation_that_does_not_end_cleanly_is_settled_as_such(card, reason, status, why):
    con, tid, out, part = card
    await _complete(con, tid, out, part)
    await part.agent_start()
    await _end_turn(part, "", reason=reason)
    run = _consultation(con, tid)
    assert (run["status"], run["summary"]) == (status, why)
    assert tasks.get(con, tid)["status"] == "done"


async def test_her_session_closing_mid_answer_settles_the_consultation(card):
    con, tid, out, part = card
    await _complete(con, tid, out, part)
    await part.agent_start()
    part._con = tasks.connect(os.path.join(tasks.get(con, tid)["workspace"], "board.db"))   # hers to close
    await part.session_shutdown()
    assert _consultation(con, tid)["summary"] == "her session ended before she answered"


async def test_completing_while_answering_is_explained_not_refused(card):
    con, tid, out, part = card
    await _complete(con, tid, out, part)
    await part.agent_start()
    tool = next(t for t in part.tools if t.name == "misaka_card_complete")
    result = await tool.execute("fixture", {"summary": "again"}, None, None, None)
    assert "already finished and stays so" in result["content"][0]["text"]


async def test_a_consultation_started_by_the_daemon_records_findings_and_declares_them(card, monkeypatch):
    """The pane the daemon opens for a closed window carries the consultation, not a claim."""
    con, tid, out, part = card
    await _complete(con, tid, out, part)
    opened = tasks.start_consultation(con, tid, generation=1, lock="consult:pane")
    monkeypatch.delenv("MISAKA_USAGE_CLAIM_LOCK")
    monkeypatch.setenv("MISAKA_CONSULT_RUN", opened["id"])
    monkeypatch.setenv("MISAKA_CONSULT_LOCK", "consult:pane")
    await part.agent_start()
    await _note(part, findings=[{"text": "the decree is No. 226", "claim_type": "fact", "source_file": "report.md"}])
    await _end_turn(part, "answered")
    assert tasks.get_run(con, opened["id"])["status"] == "done"
    assert [f["text"] for f in _submissions(con, tid)[-1]["findings"]] == ["the decree is No. 226"]


async def test_a_review_is_not_revised_in_an_answer(card, monkeypatch):
    """A red team's issues are its round's answer; revising them is a re-review (rework)."""
    from misaka.core.research import runs
    con, tid, out, part = card
    await _complete(con, tid, out, part)
    run = runs.create(con, workspace=tasks.get(con, tid)["workspace"], question="Why?")
    runs.link_task(con, run["id"], tid, kind="red_team", node=runs.root(con, run["id"]))
    con.commit()
    opened = tasks.start_consultation(con, tid, generation=1, lock="consult:pane")
    monkeypatch.delenv("MISAKA_USAGE_CLAIM_LOCK")
    monkeypatch.setenv("MISAKA_CONSULT_RUN", opened["id"])
    monkeypatch.setenv("MISAKA_CONSULT_LOCK", "consult:pane")
    with pytest.raises(ValueError, match="re-review"):
        await _note(part, issues=[])

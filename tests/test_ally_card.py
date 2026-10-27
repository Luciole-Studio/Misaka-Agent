"""An ally's card over ACP (misaka/core/network/ally). The runner drives a real agent process -- a
scripted one, ``fake_acp_agent.py`` -- which reaches the card's tools through the real MCP bridge
(``misaka ally-bridge``), started the way an ally starts it: from ``session/new``'s mcpServers."""
import asyncio
import io
import json
import os
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

from misaka.config import CFG, current_config
from misaka.core import session_catalog
from misaka.core.network import dispatch, messages, worker
from misaka.core.network.ally import card as ally_card
from misaka.core.platform import cards, processes, tasks
from misaka.core.research import runs

FAKE = str(Path(__file__).with_name("fake_acp_agent.py"))
BODY = "## goal\nWrite the report.\n\n## deliverable\nreport.md\n\n## acceptance criteria\n- report.md exists\n"


class Ally:
    """One card assigned to the scripted ally ``fake``, in a project of its own."""

    def __init__(self, tmp_path, *, kind=None):
        self.script, self.log = tmp_path / "script.json", tmp_path / "agent.log"
        Path(os.environ["MISAKA_HOME"], "settings.json").write_text(json.dumps({"allies": {"fake": {
            "command": [sys.executable, FAKE, str(self.script), str(self.log)],
            "description": "A scripted agent."}}}), encoding="utf-8")
        self.project = tmp_path / "project"
        self.project.mkdir()
        self.con = tasks.connect(CFG["db"])
        if kind is None:
            self.tid = cards.create(self.con, str(self.project), "Write the report", BODY, "fake")
            self.out = self.project / "out"
            self.out.mkdir()
            self.con.execute("UPDATE tasks SET output_dir=? WHERE id=?", (str(self.out), self.tid))
        else:
            run = runs.create(self.con, workspace=str(self.project), question="Why?")
            self.run, root = run, runs.root(self.con, run["id"])
            self.tid = cards.create(self.con, str(self.project), "Review", "## deliverable\nreport.md\n", "fake",
                                    after_row=lambda tid: runs.link_task(self.con, run["id"], tid, kind=kind, node=root))
            self.out = Path(tasks.get(self.con, self.tid)["output_dir"])

    def play(self, *turns, **extra):
        self.script.write_text(json.dumps({"turns": list(turns), **extra}), encoding="utf-8")

    def claim(self, lock="lock1", out=None):
        row = tasks.get(self.con, self.tid)
        assert tasks.claim(self.con, self.tid, lock, generation=int(row["generation"]), pid=os.getpid())
        return self.attempt(lock, out)

    def attempt(self, lock, out=None):
        row = tasks.get(self.con, self.tid)
        task = {**dict(row), "_handoffs": [], **worker.card_extras(self.con, row)}
        return ally_card.Attempt(task, int(row["generation"]), lock, db_path=CFG["db"], out=out)

    @property
    def row(self):
        return tasks.get(self.con, self.tid)

    def logged(self, kind):
        if not self.log.exists():
            return []
        return [entry for entry in map(json.loads, self.log.read_text(encoding="utf-8").splitlines()) if entry["kind"] == kind]

    def prompts(self):
        return [entry["text"] for entry in self.logged("prompt")]

    def payload(self, kind):
        return json.loads(tasks.latest_payload(self.con, self.tid, kind, generation=self.row["generation"]) or "null")

    def finish(self, summary="The report is written."):
        """A turn that delivers: the file, then the declaration."""
        return [{"write": [str(self.out / "report.md"), "# Report\n"]},
                {"call": ["misaka_card_complete", {"summary": summary}]}, {"say": "Done."}]


@pytest.fixture
def make(tmp_path):
    made = []

    def make(**kwargs):
        made.append(Ally(tmp_path, **kwargs))
        return made[-1]

    yield make
    for ally in made:
        ally.con.close()


async def _until(check, timeout=60):
    deadline = time.monotonic() + timeout
    while not (value := check()):
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.05)
    return value


def _messages(path):
    return [json.loads(line)["message"] for line in Path(path).read_text(encoding="utf-8").splitlines()
            if json.loads(line).get("type") == "message"]


async def test_a_turn_that_declares_completion_submits_the_card_like_a_sisters(make):
    ally = make()
    ally.play([{"call": ["misaka_card_note", {"text": "outline settled"}]}, *ally.finish()])
    attempt = ally.claim()
    await attempt.run()

    row = ally.row
    assert row["status"] == "done"
    submitted = ally.payload("submitted")
    assert submitted["summary"] == "The report is written."
    assert "out/report.md" in submitted["artifacts"] and submitted["artifact_digests"]["out/report.md"]
    assert "outline settled" in "\n".join(cards.read_log(str(ally.project), ally.tid))
    # The card's tools, lent through the bridge, are the Sister's definitions.
    assert {"misaka_card_complete", "misaka_card_note", "misaka_todo", "misaka_my_card", "SendMessage",
            "misaka_research_view", "doc_list", "doc_read"} <= set(ally.logged("tools")[0]["names"])
    # The ACP session is recorded on the card, the turns as a pi transcript beside a Sister's.
    assert row["agent_id"] == ally.logged("new")[0]["session"]
    roles = [m["role"] for m in _messages(attempt.transcript.path)]
    assert roles[0] == "user" and "toolResult" in roles and roles[-1] == "assistant"
    assert _messages(attempt.transcript.path)[-1]["content"][-1]["text"] == "Done."
    record = session_catalog.find_session(attempt.transcript.session_id)
    assert (record["task_id"], record["inbox"], record["steer"], record["state"]) == (ally.tid, "fake", False, "saved")


async def test_a_turn_that_ends_without_the_call_is_told_once_and_the_card_keeps_running(make):
    ally = make()
    ally.play([{"call": ["misaka_card_complete", {"summary": "too soon"}]}, {"say": "Tried."}],
              [{"say": "Still working."}])
    await ally.claim().run()

    first_call = ally.logged("tool")[0]
    assert first_call["error"] and "report.md" in first_call["output"]      # the deliverable is not there yet
    prompts = ally.prompts()
    assert len(prompts) == 2 and prompts[1].startswith("[Card still running]") and "report.md" in prompts[1]
    assert ally.row["status"] == "running"        # in process, the dispatcher settles what is left


async def test_a_permission_request_is_answered_by_kind_not_by_id(make):
    ally = make()
    ally.play([{"permission": True}, *ally.finish()])
    await ally.claim().run()
    assert ally.logged("permission")[0]["outcome"] == {"outcome": "selected", "optionId": "go"}
    assert ally.row["status"] == "done"


async def test_a_turn_with_nothing_in_it_fails_with_the_agents_stderr(make):
    ally = make()
    ally.play([{"stderr": "Error: 401 Unauthorized -- run /login"}])
    await ally.claim().run()
    assert ally.row["status"] == "failed"
    assert "401 Unauthorized" in ally.payload("failed")["reason"]


async def test_a_cut_reply_gets_one_more_turn(make):
    ally = make()
    ally.play([{"say": "Part one"}, {"stop": "max_tokens"}], ally.finish())
    await ally.claim().run()
    assert ally.prompts()[1].startswith("Your last reply was cut off (max_tokens)")
    assert ally.row["status"] == "done"


async def test_mail_waits_for_the_running_turn_and_mail_to_an_idle_ally_starts_one(make):
    ally = make()
    ally.play([{"say": "On it."}, {"sleep": 3}], [{"say": "Noted."}], [{"say": "Waiting."}], ally.finish())
    attempt = ally.claim(out=io.StringIO())
    running = asyncio.create_task(attempt.run())

    def mail(text):
        with closing(messages.connect()) as con:
            messages.send(con, "fake", text, summary="note", sender="last-order", to_task=ally.tid, generation=1)

    await _until(lambda: ally.prompts())
    mail("First note.")
    await _until(lambda: len(ally.prompts()) >= 3)          # the mail, then the one reminder
    first, second = ally.logged("prompt")[:2]
    assert "First note." in second["text"] and second["at"] - first["at"] >= 2.9   # after the turn, not in it
    assert ally.prompts()[2].startswith("[Card still running]")
    await _until(lambda: attempt.busy is False and len(ally.logged("prompt")) == 3 and not attempt.transcript.calls_open)
    mail("Second note.")                                    # the ally is idle: it starts a turn
    await asyncio.wait_for(running, 60)
    assert "Second note." in ally.prompts()[3] and ally.row["status"] == "done"


async def test_a_line_typed_during_a_turn_waits_and_ctrl_c_cancels_the_turn(make):
    ally = make()
    ally.play([{"say": "A long job."}, {"sleep": 30}], [{"say": "Hurrying."}], ally.finish())
    out = io.StringIO()
    attempt = ally.claim(out=out)
    running = asyncio.create_task(attempt.run())
    await _until(lambda: ally.prompts())
    attempt.typed_line("please hurry")
    assert "[queued" in out.getvalue()
    attempt.interrupt()
    await asyncio.wait_for(running, 60)
    assert ally.logged("cancel") and ally.logged("cancelled")
    assert ally.prompts()[1] == "please hurry"            # the typed line, as the next turn
    assert {"role": "user", "text": "please hurry"} in [
        {"role": m["role"], "text": m["content"][0]["text"]} for m in _messages(attempt.transcript.path)
        if m["role"] == "user"]
    assert ally.row["status"] == "done"


async def test_continuing_a_card_loads_the_allys_session_and_drops_its_replay(make):
    ally = make()
    ally.play(ally.finish("First pass."), load=True)
    first = ally.claim()
    await first.run()
    session = ally.row["agent_id"]
    assert tasks.claim_resume(ally.con, ally.tid, "lock2", os.getpid(), expected_generation=1)

    ally.play(ally.finish("Second pass."), load=True, replay=["OLD HISTORY"])
    second = ally.attempt("lock2")
    await second.run(say="Add a summary.")

    assert ally.logged("load")[-1]["session"] == session
    assert ally.prompts()[-1] == "Add a summary."             # the session has the card: the message alone
    assert second.transcript.path == first.transcript.path   # the card's one transcript goes on
    text = Path(second.transcript.path).read_text(encoding="utf-8")
    assert "First pass." in text and "Second pass." in text and "OLD HISTORY" not in text
    assert ally.row["status"] == "done" and ally.payload("submitted")["summary"] == "Second pass."


async def test_a_session_the_agent_cannot_load_is_pointed_at_the_transcript(make):
    ally = make()
    ally.play(ally.finish("First pass."))                     # no loadSession
    first = ally.claim()
    await first.run()
    assert tasks.claim_resume(ally.con, ally.tid, "lock2", os.getpid(), expected_generation=1)
    ally.play(ally.finish("Second pass."))
    await ally.attempt("lock2").run(say="Add a summary.")
    prompt = ally.prompts()[-1]
    assert first.transcript.path in prompt and "## goal" in prompt and prompt.endswith("## Message\nAdd a summary.")


async def test_leaving_cancels_the_running_turn_before_the_agent_goes(make):
    ally = make()
    ally.play([{"sleep": 30}])
    attempt = ally.claim(out=io.StringIO())
    running = asyncio.create_task(attempt.run())
    await _until(lambda: ally.prompts())
    attempt.leave()
    await asyncio.wait_for(running, 30)
    assert ally.logged("cancel")
    assert ally.row["status"] == "running"              # the host settles an attempt that left


async def test_the_ally_sends_as_itself_from_its_card_and_space(make, monkeypatch):
    monkeypatch.setenv("MISAKA_NET_SPACE", "space-a")
    lo = "0123abcd-0000-4000-8000-000000000001"
    session_catalog._write(session_catalog._index_dir() / "lo.json", {
        "id": lo, "path": None, "role": "last-order", "kind": "foreground", "inbox": "last-order",
        "space": "space-a", "pid": os.getpid(), "identity": processes.identity(os.getpid()), "state": "idle"})
    ally = make()
    ally.play([{"call": ["SendMessage", {"to": "last-order", "message": "Which edition?", "summary": "edition"}]},
               *ally.finish()])
    attempt = ally.claim()
    await attempt.run()
    with closing(messages.connect()) as con:
        row = con.execute("SELECT * FROM messages WHERE sender='fake'").fetchone()
    assert (row["to_session"], row["sender_task"], row["sender_session"]) == (
        lo, ally.tid, attempt.transcript.session_id)
    assert row["body"].startswith(f"[card {ally.tid}]")


async def test_a_red_team_card_given_to_an_ally_records_its_issues_through_the_bridge(make):
    ally = make(kind="red_team")
    issue = {"kind": "logic", "question": "Does it follow?", "rationale": "A gap.", "priority": 1, "material": True}
    write, complete, say = ally.finish()
    ally.play([{"call": ["misaka_research_view", {"view": "run"}]},
               {"call": ["misaka_card_note", {"text": "one gap", "issues": [issue]}]},
               write, complete, complete, say])      # a review is checked against its record first (B117)
    await ally.claim().run()
    assert ally.prompts()[0].startswith("[Research card]")      # ACP has no system prompt
    assert sum(bool(call["error"]) for call in ally.logged("tool")) == 1   # the first completion
    assert json.loads(tasks.latest_payload(ally.con, ally.tid, "review_record_checked"))["items"] == 1
    assert ally.row["status"] == "done"
    assert ally.payload("submitted")["issues"][0]["question"] == "Does it follow?"


def test_the_headless_host_runs_an_ally_in_process(make):
    ally = make()
    ally.play(ally.finish())
    assert dispatch.run_task(ally.con, ally.row, current_config())
    assert ally.row["status"] == "done"


def test_the_planning_roster_lists_usable_allies(make):
    from misaka.core.network import roster
    make()
    entry = next(item for item in roster.capability_catalog() if item["id"] == "fake")
    assert "not a Sister" in entry["description"] and "A scripted agent." in entry["description"]
    assert "fake" in roster.executors()


@pytest.mark.parametrize("space", ["space-a", "space-b"])
def test_a_name_reaches_the_ally_card_in_the_senders_space_only(make, space):
    ally = make()
    for index, other in enumerate(("space-a", "space-b")):
        tid = ally.tid if other == space else cards.create(ally.con, str(ally.project), "Other", BODY, "fake")
        assert tasks.claim(ally.con, tid, f"lock-{index}", generation=1, pid=os.getpid())
        session_catalog._write(session_catalog._index_dir() / f"{other}.json", {
            "id": f"0123abcd-0000-4000-8000-00000000000{index}", "path": None, "role": "fake", "kind": "card",
            "task_id": tid, "inbox": "fake", "space": other, "steer": False, "pid": os.getpid(),
            "identity": processes.identity(os.getpid()), "state": "idle"})
    target = messages.resolve_address("fake", sender="last-order", space=space)
    assert target["to_task"] == ally.tid and target["after_turn"] is True


async def test_last_order_without_a_panel_hands_an_ally_card_to_the_daemon(make, monkeypatch):
    """An ally's runner is a program of its own, never a subagent of Last Order's session."""
    from misaka.core.network.sister_runtime import SisterRuntime
    from misaka.ui.panel import client
    ally = make()
    calls = []
    monkeypatch.setattr(client, "ensure", lambda: calls.append("ensure"))
    monkeypatch.setattr(client, "request", lambda method, params=None: calls.append((method, params)) or {"pane_id": "p9"})
    result = await SisterRuntime(None, lambda: ally.con, current_config).launch(ally.tid, context=None, tool_call_id="c")
    assert calls == ["ensure", ("pane.run_card", {"task_id": ally.tid})]
    assert (result["status"], result["pane"]) == ("running", "p9")


async def test_a_finished_allys_card_answers_a_message_and_stays_done(make):
    """R40: a message to a finished card is a consultation, an ally's as a Sister's. Her runner
    takes no claim, what the turn changes is declared again in the same generation, and the
    consultation, not the card, records how it ended."""
    ally = make()
    ally.play(ally.finish("First pass."), load=True)
    await ally.claim().run()
    consultation = tasks.start_consultation(ally.con, ally.tid, generation=1, lock="consult:pane")
    finding = {"text": "the decree is No. 226", "claim_type": "fact", "source_file": "out/report.md"}
    ally.play([{"write": [str(ally.out / "report.md"), "# Report\nwith the decree\n"]},
               {"call": ["misaka_card_note", {"text": "answered K1a", "findings": [finding]}]},
               {"say": "It is No. 226."}], load=True)
    row = ally.row
    task = {**dict(row), "_handoffs": [], **worker.card_extras(ally.con, row)}
    attempt = ally_card.Attempt(task, 1, None, db_path=CFG["db"], consult=(consultation["id"], "consult:pane"))

    await attempt.run(say="Which decree?")

    assert ally.prompts()[-1] == "Which decree?"
    row = ally.row
    assert (row["status"], row["generation"], row["claim_lock"]) == ("done", 1, None)
    assert tasks.get_run(ally.con, consultation["id"])["status"] == "done"
    declared = ally.payload("submitted")
    assert declared["artifact_digests"]["out/report.md"] and tasks.latest_payload(ally.con, ally.tid, "redeclared",
                                                                                   generation=1)
    assert "the decree is No. 226" in [f["text"] for f in declared["findings"]]

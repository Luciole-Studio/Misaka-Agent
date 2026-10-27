"""A node's routine from plan to decision, and the level's reconciliation after it.

Corrections stay in the node: a `revise` reworks the conclusion, which the same Sister reviews
again on the same card, continued in her own conversation; the review loop ends when nothing is revised. The same Sister then proposes
alternatives on a card of their own, and the node's decision turns proposals into options, which
open as nodes only when the root reconciles the level. The model turns are scripted; everything
the driver records is real."""
import asyncio
import hashlib
import json
import re
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from misaka.core.platform import cards, repo, tasks
from misaka.core.research import (
    commands,
    context,
    planner,
    report,
    runs,
    views,
    workflow,
)
from misaka.core.session_manager import SessionManager

RED, RESEARCHER = "10033", "10032"


@pytest.fixture
def lab(tmp_path, monkeypatch):
    monkeypatch.setattr(repo, "enabled", lambda *a: False)
    monkeypatch.setattr(tasks, "task_state_dir", lambda tid: str(tmp_path / "state" / tid))
    monkeypatch.setattr(workflow, "_bundle", lambda *a, **k: None)
    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        run = runs.create(con, workspace=str(tmp_path), question="Why did the reform fail?",
                          limits={"max_depth": 1, "max_revisions": 1, "max_followups": 0, "plan_approval": False})
        assert runs.acquire_driver(con, run["id"], "driver:test")
        run = runs.get(con, run["id"])
        root = runs.root(con, run["id"])
        runs.prepare_runner(con, "research_execution", root["id"])
        root = runs.node(con, root["id"])
        # The root's conversation, as the user's window would hold it.
        manager = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
        manager.appendMessage({"role": "user", "content": "before the run", "timestamp": 1})
        manager.appendMessage({"role": "assistant", "content": [{"type": "text", "text": "ok"}], "timestamp": 2})
        con.execute("UPDATE research_runs SET root_session=? WHERE id=?", (manager.getSessionFile(), run["id"]))
        run = runs.get(con, run["id"])
        plan = {"status": "ready", "plan_markdown": "Plan", "decisions": [], "clarifying_questions": [],
                "reframed_question": "", "methods": [], "extensions": {},
                "red_team": {"assignee": RED, "reason": "critic"},
                "tasks": [{"local_id": "fiscal", "title": "Fiscal records", "question": "What did the treasury hold?",
                           "rationale": "r", "deliverable": "fiscal.md", "assignee": RESEARCHER, "dependencies": []}]}
        runs.record_action(con, run, root, "plan", plan, session_file=run["root_session"], tool_call_id="plan")
        yield SimpleNamespace(con=con, run=run, root=root, tmp=tmp_path, monkeypatch=monkeypatch)


def _script(lab, *, reviews, alternatives, dispositions, decide, cards_failed=None, gaps=None):
    """Scripted model turns: every card comes back done with its submission, and every phase
    command is validated and recorded exactly as the real tool would record it."""
    con = lab.con
    syntheses = []
    lab.prompts = {}

    async def drive(con_, cfg, runner, run_id, *, scope, **_kwargs):
        assert scope, "an empty card scope is never driven (the real driver calls it a failure)"
        for row in runs.tasks(con, run_id):
            if row["id"] not in scope or row["status"] == "done":
                continue
            payload = {"summary": "done", "artifacts": [], "artifact_digests": {}, "findings": []}
            if row["research_kind"] == "red_team":
                payload["issues"] = reviews[row["generation"]]      # one card: each review is an attempt
                critique = Path(row["output_dir"], runs.versioned("critique.md", row["generation"]))
                critique.parent.mkdir(parents=True, exist_ok=True)
                critique.write_text(f"Critique of version {row['generation']}\n", encoding="utf-8")
                rel = str(critique.relative_to(row["workspace"]))
                payload["artifacts"] = [rel]
                payload["artifact_digests"] = {rel: hashlib.sha256(critique.read_bytes()).hexdigest()}
            if row["research_kind"] == "divergence":       # one card: each round is an attempt
                payload["alternatives"] = (alternatives[row["generation"]] if isinstance(alternatives, dict)
                                           else alternatives)
            con.execute("UPDATE tasks SET status='done' WHERE id=?", (row["id"],))
            cards.set_fields(row["workspace"], row["id"], status="done", generation=row["generation"])   # the file is the contract
            tasks.add_event(con, row["id"], "submitted", payload, generation=row["generation"])
        return "done"

    lab.material = []

    def synthesize(con_, run, cfg, worker, node, rows, *, revision=None, **_kwargs):
        syntheses.append(revision)
        lab.material.append(sorted(row["id"] for row in rows))
        return f"Conclusion {len(syntheses)}\n"

    def command(con_, run, cfg, worker, node, prompt, *, key, model, validate, **_kwargs):
        lab.prompts[key] = prompt
        answer = (dispositions(con, key) if key.startswith("dispose:")
                  else gaps(con, key) if key.startswith("gaps:")
                  else cards_failed(con, key) if key.startswith("cards_failed:") else decide(con))
        payload = validate(model.model_validate(answer).model_dump())
        runs.record_action(con, run, node, key, payload, session_file=run["root_session"], tool_call_id=key)
        return runs.action(con, run["id"], node["id"], key), ""

    lab.monkeypatch.setattr(workflow, "_drive_tasks", drive)
    lab.monkeypatch.setattr(planner, "synthesize", synthesize)
    lab.monkeypatch.setattr(planner, "_command", command)
    return syntheses


def _issue(question, material=True):
    return {"kind": "fact", "question": question, "rationale": "because", "priority": 0, "material": material}


class _Runner:
    """Continues a settled card the way the daemon does: ``claim_resume`` (a new generation), after
    which the Sister writes the file the message names."""

    def __init__(self, con):
        self.con, self.continued = con, []

    async def continue_card(self, task_id, say, *, expected_generation):
        assert tasks.claim_resume(self.con, task_id, "resume-lock", 1, expected_generation=expected_generation)
        self.continued.append((task_id, expected_generation, say))
        row = tasks.get(self.con, task_id)
        name = re.search(r"Deliver `([^`]+)`", say).group(1)        # the red team's and the divergence review's
        Path(row["output_dir"]).mkdir(parents=True, exist_ok=True)
        Path(row["output_dir"], name).write_text(f"Review of version {row['generation']}\n", encoding="utf-8")


async def _expand(lab, runner=None):
    return await workflow._expand(lab.con, {}, runner, None, lab.run, lab.root, context=None,
                                  tool_call_id="fixture", poll_seconds=0, progress=None)


async def test_a_revision_is_reviewed_again_then_alternatives_become_a_decision(lab):
    con, run, root = lab.con, lab.run, lab.root

    def dispositions(con_, key):
        issues = runs.issues(con, run["id"], node_id=root["id"], round=int(key.split(":")[1]), origin="critique")
        first, second = issues
        return {"dispositions": [
            {"issue_id": first["id"], "disposition": "revise", "reason": "the dates are wrong"},
            {"issue_id": second["id"], "disposition": "concede", "reason": "the sample is thin"}]}

    def decide(con_):
        hypothesis, method = runs.issues(con, run["id"], node_id=root["id"], origin="divergence")
        return {"decisions": [{"question": "Which cause leads?", "stakes": "the answer's core",
                               "options": [{"label": "fiscal", "premise": "as concluded", "own": True},
                                           {"label": hypothesis["question"], "premise": "coalitions",
                                            "sources": [hypothesis["id"]]}]}],
                "declined": [{"issue_id": method["id"], "disposition": "decline", "reason": "a complement"}]}

    syntheses = _script(lab, reviews={1: [_issue("Dates?"), _issue("Sample?"), _issue("Style", False)], 2: []},
                        alternatives=[{"kind": "hypothesis", "proposal": "Local coalitions blocked it",
                                       "premise": "politics first", "rationale": "changes the cause", "gap": False},
                                      {"kind": "method", "proposal": "Count petitions",
                                       "premise": "quantitative", "rationale": "adds evidence", "gap": False}],
                        dispositions=dispositions, decide=decide)
    runner = _Runner(con)
    assert await _expand(lab, runner) == "closed"

    # One red-team card for the node: version 2's review continued it in her own conversation.
    [red] = runs.tasks(con, run["id"], kind="red_team", node_id=root["id"])
    assert (red["local_id"], red["assignee"], red["generation"]) == ("@red-team", RED, 2)
    [(task_id, from_generation, say)] = runner.continued
    assert (task_id, from_generation) == (red["id"], 1)
    assert "Deliver `critique-2.md`" in say and "re-review (round 2)" in say
    # Each round's critique stays registered after the card's next attempt settles.
    assert [Path(a["path"]).name for a in runs.artifacts(con, run["id"], kind="critique", task_id=red["id"])] \
        == ["critique.md", "critique-2.md"]
    divergence = runs.tasks(con, run["id"], kind="divergence", node_id=root["id"])
    assert [(row["local_id"], row["assignee"]) for row in divergence] == [("@divergence", RED)]
    assert [Path(row["path"]).name for row in views.syntheses(con, run, root)] == ["synthesis.md", "synthesis-2.md"]
    # The rework was told what it reworks: the round-1 review and what was done with each issue.
    assert syntheses[0] is None and syntheses[1]["version"] == 2 and "revise" in syntheses[1]["dispositions"]
    critique = runs.issues(con, run["id"], node_id=root["id"], origin="critique")
    assert [(i["question"], i["disposition"]) for i in critique] == [("Dates?", "revise"), ("Sample?", "concede")]
    proposals = {i["question"]: i for i in runs.issues(con, run["id"], node_id=root["id"], origin="divergence")}
    pending = runs.pending_options(con, run["id"])
    assert len(pending) == 1 and pending[0]["label"] == "Local coalitions blocked it"
    assert proposals["Local coalitions blocked it"]["disposition"] == "branch"
    assert proposals["Local coalitions blocked it"]["option_id"] == pending[0]["id"]
    assert proposals["Count petitions"]["disposition"] == "decline"
    assert runs.node(con, root["id"])["status"] == "closed"
    # The clean second review came back to the conversation without waking a model -- through the real
    # return path, which a mock here once hid a crash in (2026-09-27, B79).
    returned = [entry for entry in SessionManager.open(run["root_session"]).getEntries()
                if entry.get("type") == "custom_message" and entry.get("customType") == "research-review"]
    assert len(returned) == 1 and "Critique of version 2" in returned[0]["content"]
    # The views followed the node through its phases.
    node_view = (lab.tmp / views.node_view_path(root["id"])).read_text(encoding="utf-8")
    assert "attempt 2" in node_view and "Divergence review" in node_view and "synthesis-2.md" in node_view
    assert "Which cause leads?" in (lab.tmp / views.graph_path(run)).read_text(encoding="utf-8")


async def test_the_red_team_loop_runs_first_then_the_divergence_loop_until_no_gap_is_left(lab):
    """2026-10-01 (user): conclusion -> red team -> revision -> red team again, until she finds nothing
    or the revisions are spent; only then the divergence review -> its gaps filled -> review again,
    until only possibilities not taken are left; then the node's decision."""
    con, run, root = lab.con, lab.run, lab.root
    order = []

    def dispositions(con_, key):
        order.append(key)
        [issue] = runs.issues(con, run["id"], node_id=root["id"], round=int(key.split(":")[1]), origin="critique")
        return {"dispositions": [{"issue_id": issue["id"], "disposition": "revise", "reason": "fix the dates"}]}

    def gaps(con_, key):
        order.append(key)
        [gap] = runs.issues(con, run["id"], node_id=root["id"], round=int(key.split(":")[1]), origin="gap")
        return {"dispositions": [{"issue_id": gap["id"], "disposition": "revise", "reason": "read the governed"}]}

    def decide(con_):
        order.append("decide")
        [rival] = runs.issues(con, run["id"], node_id=root["id"], origin="divergence")
        return {"decisions": [], "declined": [{"issue_id": rival["id"], "disposition": "decline", "reason": "later"}]}

    rival = {"kind": "framework", "proposal": "Composite monarchy", "premise": "q", "rationale": "frame", "gap": False}
    hole = {"kind": "source base", "proposal": "Nobody read the governed", "premise": "rulers only",
            "rationale": "any answer needs them", "gap": True}
    syntheses = _script(lab, reviews={1: [_issue("Dates?")], 2: []},
                        alternatives={1: [rival, hole], 2: []},
                        dispositions=dispositions, gaps=gaps, decide=decide)
    runner = _Runner(con)
    assert await _expand(lab, runner) == "closed"

    # The red team's loop, then the divergence review's: one revision each, each reviewed again.
    assert order == ["dispose:1", "gaps:2", "decide"]
    assert [Path(row["path"]).name for row in views.syntheses(con, run, root)] == \
        ["synthesis.md", "synthesis-2.md", "synthesis-3.md"]
    [red] = runs.tasks(con, run["id"], kind="red_team", node_id=root["id"])
    [review] = runs.tasks(con, run["id"], kind="divergence", node_id=root["id"])
    assert red["generation"] == 2 and review["generation"] == 2      # the red team never saw version 3
    assert [(task_id, say.count("re-review (round 2)")) for task_id, _gen, say in runner.continued] == \
        [(red["id"], 1), (review["id"], 1)]
    assert "Deliver `divergence-2.md`" in runner.continued[1][2]
    # The divergence review saw the version the red team left, and the gap revision was told so.
    assert [r["version"] for r in workflow._divergence_requests(con, review)] == [2, 3]
    assert syntheses[1]["reviewer"] == "red_team" and syntheses[2]["reviewer"] == "divergence"
    assert "revise" in syntheses[2]["dispositions"]
    assert [i["disposition"] for i in runs.issues(con, run["id"], node_id=root["id"], origin="gap")] == ["revise"]


async def test_the_divergence_loop_stops_at_the_revision_limit(lab):
    con, run, root = lab.con, lab.run, lab.root
    order = []

    def gaps(con_, key):
        order.append(key)
        version = int(key.split(":")[1])
        [gap] = runs.issues(con, run["id"], node_id=root["id"], round=version, origin="gap")
        # One gap revision is allowed (max_revisions = 1): the second round must answer another way.
        return {"dispositions": [{"issue_id": gap["id"], "disposition": "revise" if version == 1 else "concede",
                                  "reason": "r"}]}

    hole = {"kind": "source base", "proposal": "Nobody read the governed", "premise": "rulers only",
            "rationale": "needed", "gap": True}
    _script(lab, reviews={1: []}, alternatives={1: [hole], 2: [hole]}, dispositions=None, gaps=gaps,
            decide=lambda con_: (order.append("decide"), {"decisions": [], "declined": []})[1])
    assert await _expand(lab, _Runner(con)) == "closed"
    assert order == ["gaps:1", "gaps:2", "decide"]


async def test_an_issue_the_red_team_raises_again_word_for_word_is_answered_again(lab):
    """Matched against the earlier round, it folded into the issue already answered, and the round
    looked clean: the loop ended on an objection nobody answered (2026-10-01)."""
    con, run, root = lab.con, lab.run, lab.root
    order = []

    def dispositions(con_, key):
        order.append(key)
        [issue] = runs.issues(con, run["id"], node_id=root["id"], round=int(key.split(":")[1]), origin="critique")
        return {"dispositions": [{"issue_id": issue["id"], "reason": "r",
                                  "disposition": "revise" if key == "dispose:1" else "rebut"}]}

    _script(lab, reviews={1: [_issue("Dates?")], 2: [_issue("Dates?")]}, alternatives=[], dispositions=dispositions,
            decide=lambda con_: (order.append("decide"), {"decisions": [], "declined": []})[1])
    assert await _expand(lab, _Runner(con)) == "closed"
    assert order == ["dispose:1", "dispose:2", "decide"]


async def test_a_conclusion_waits_for_a_card_woken_to_answer_a_message(lab):
    """2026-10-01: Last Order woke card B to ask about her material, disposed without waiting, and
    version 2 was written while B answered -- B was missing from its material map. Since R40 her
    answer is a consultation and B stays done: the node waits for the consultation instead."""
    con, run, root = lab.con, lab.run, lab.root
    asked = {}

    def dispositions(con_, key):
        [card] = runs.tasks(con, run["id"], kind="research", node_id=root["id"])
        if not asked:
            # A SendMessage to the finished card: a consultation, not an attempt.
            consultation = tasks.start_consultation(con, card["id"], generation=card["generation"],
                                                    lock="consult:test", asker={"sender": "last-order"})
            asked.update(card=card["id"], run=consultation["id"])
        [issue] = runs.issues(con, run["id"], node_id=root["id"], round=int(key.split(":")[1]), origin="critique")
        return {"dispositions": [{"issue_id": issue["id"], "disposition": "revise", "reason": "r"}]}

    _script(lab, reviews={1: [_issue("Dates?")], 2: []}, alternatives=[], dispositions=dispositions,
            decide=lambda con_: {"decisions": [], "declined": []})
    written_while_answering = []
    scripted = planner.synthesize

    def synthesize(con_, *args, **kwargs):
        written_while_answering.append(bool(asked) and tasks.live_consultation(con, asked["card"]) is not None)
        return scripted(con_, *args, **kwargs)

    lab.monkeypatch.setattr(planner, "synthesize", synthesize)
    real_sleep, polls = asyncio.sleep, []

    async def answering(seconds):
        if asked and tasks.live_consultation(con, asked["card"]) is not None:
            polls.append(seconds)
            if len(polls) == 3:                   # she answers while the node waits
                tasks.end_consultation(con, asked["run"], "consult:test", "done")
        await real_sleep(0)

    lab.monkeypatch.setattr(workflow.asyncio, "sleep", answering)
    assert await _expand(lab, _Runner(con)) == "closed"
    [card] = runs.tasks(con, run["id"], kind="research", node_id=root["id"])
    assert (card["generation"], card["status"]) == (1, "done")
    assert len(polls) == 3 and written_while_answering == [False, False]
    assert lab.material == [[card["id"]], [card["id"]]]      # version 2 was written from the card too


@pytest.mark.parametrize("crash_at", ["gaps", "synthesis-3", "decide"])
async def test_a_node_resumed_inside_the_divergence_loop_picks_up_where_it_stopped(lab, crash_at):
    """Resume and retry put a node back to planning and every phase fast-forwards through what it
    already recorded: the divergence loop's states must too -- no second card, no second answer,
    no version written twice."""
    con, run, root = lab.con, lab.run, lab.root
    order, crashed = [], []

    def once(point):
        if crash_at == point and not crashed:
            crashed.append(point)
            raise RuntimeError(f"provider fell over at {point}")

    def dispositions(con_, key):
        order.append(key)
        [issue] = runs.issues(con, run["id"], node_id=root["id"], round=int(key.split(":")[1]), origin="critique")
        return {"dispositions": [{"issue_id": issue["id"], "disposition": "revise", "reason": "r"}]}

    def gaps(con_, key):
        once("gaps")
        order.append(key)
        [gap] = runs.issues(con, run["id"], node_id=root["id"], round=int(key.split(":")[1]), origin="gap")
        return {"dispositions": [{"issue_id": gap["id"], "disposition": "revise", "reason": "r"}]}

    def decide(con_):
        once("decide")
        order.append("decide")
        return {"decisions": [], "declined": [{"issue_id": i["id"], "disposition": "decline", "reason": "later"}
                                              for i in runs.issues(con, run["id"], node_id=root["id"], origin="divergence")]}

    hole = {"kind": "source base", "proposal": "Nobody read the governed", "premise": "p", "rationale": "r", "gap": True}
    rival = {"kind": "framework", "proposal": "Composite monarchy", "premise": "q", "rationale": "r", "gap": False}
    _script(lab, reviews={1: [_issue("Dates?")], 2: []}, alternatives={1: [rival, hole], 2: []},
            dispositions=dispositions, gaps=gaps, decide=decide)
    scripted = planner.synthesize

    def synthesize(*args, **kwargs):
        if kwargs.get("revision") and kwargs["revision"]["version"] == 3:
            once("synthesis-3")
        return scripted(*args, **kwargs)

    lab.monkeypatch.setattr(planner, "synthesize", synthesize)
    runner = _Runner(con)
    with pytest.raises(RuntimeError, match="provider fell over"):
        await _expand(lab, runner)
    # What a retry does: the node goes back to planning, and every phase picks up what it recorded.
    con.execute("UPDATE research_execution SET status='planning' WHERE id=?", (root["id"],))
    lab.root = runs.node(con, root["id"])
    assert await _expand(lab, runner) == "closed"

    assert order == ["dispose:1", "gaps:2", "decide"]
    assert [Path(r["path"]).name for r in views.syntheses(con, run, root)] == \
        ["synthesis.md", "synthesis-2.md", "synthesis-3.md"]
    assert len(runs.tasks(con, run["id"], kind="red_team", node_id=root["id"])) == 1
    [review] = runs.tasks(con, run["id"], kind="divergence", node_id=root["id"])
    assert review["generation"] == 2 and [r["version"] for r in workflow._divergence_requests(con, review)] == [2, 3]
    assert len(runs.issues(con, run["id"], node_id=root["id"], origin="gap")) == 1


async def test_a_join_runs_both_review_loops_like_any_node(lab):
    """A node two paths closed into: its own red-team loop, then its divergence loop until no gap is
    left, then its own decision -- nothing of either parent's loops carries into it."""
    con, run, root = lab.con, lab.run, lab.root
    # A join sits one below its deepest parent: depth 2 here, so a limit of 3 leaves it room to fork
    # (at the limit it would close after its red-team loop, with no divergence review).
    runs.update_limits(con, run["id"], {"max_depth": 3})
    lab.run = run = runs.get(con, run["id"])
    a = runs.create_node(con, run["id"], question="Fiscal limits?", parents=[root["id"]])
    b = runs.create_node(con, run["id"], question="Local coalitions?", parents=[root["id"]])
    for parent in (a, b):
        con.execute("UPDATE research_execution SET status='closed' WHERE id=?", (parent["id"],))
    join = runs.create_node(con, run["id"], question="Both at once?", parents=[a["id"], b["id"]])
    workflow._prepare_sessions(con, run)
    runs.prepare_runner(con, "research_execution", join["id"])
    join = runs.node(con, join["id"])
    plan = dict(runs.action(con, run["id"], root["id"], "plan")["payload"])
    plan["tasks"] = [{**plan["tasks"][0], "local_id": "both", "title": "Both causes"}]
    runs.record_action(con, run, join, "plan", plan, session_file=join["session_file"], tool_call_id="plan-join")
    lab.root = join
    order = []

    def dispositions(con_, key):
        order.append(key)
        [issue] = runs.issues(con, run["id"], node_id=join["id"], round=int(key.split(":")[1]), origin="critique")
        return {"dispositions": [{"issue_id": issue["id"], "disposition": "revise", "reason": "r"}]}

    def gaps(con_, key):
        order.append(key)
        [gap] = runs.issues(con, run["id"], node_id=join["id"], round=int(key.split(":")[1]), origin="gap")
        return {"dispositions": [{"issue_id": gap["id"], "disposition": "revise", "reason": "r"}]}

    hole = {"kind": "source base", "proposal": "Nobody read the governed", "premise": "p", "rationale": "r", "gap": True}
    _script(lab, reviews={1: [_issue("Dates?")], 2: []}, alternatives={1: [hole], 2: []},
            dispositions=dispositions, gaps=gaps,
            decide=lambda con_: (order.append("decide"), {"decisions": [], "declined": []})[1])
    assert await _expand(lab, _Runner(con)) == "closed"
    assert order == ["dispose:1", "gaps:2", "decide"]
    assert [Path(r["path"]).name for r in views.syntheses(con, run, join)] == \
        ["synthesis.md", "synthesis-2.md", "synthesis-3.md"]
    assert join["depth"] == 2
    assert sorted(row["id"] for row in runs.parents(con, join["id"])) == sorted([a["id"], b["id"]])
    for parent in (a, b):
        assert not runs.issues(con, run["id"], node_id=parent["id"])        # nothing landed on a parent


def _fail_once(lab, local_id):
    """Around the scripted driver: the card ``local_id`` fails for good on its first attempt, as a
    provider refusal did after the Sister's own attempts (2026-09-29); the rest come back done."""
    scripted = workflow._drive_tasks

    async def drive(con_, cfg, runner, run_id, *, scope, **kwargs):
        con = lab.con
        for row in runs.tasks(con, run_id):
            if row["id"] in scope and row["local_id"] == local_id and row["generation"] == 1 and row["status"] != "failed":
                con.execute("UPDATE tasks SET status='failed' WHERE id=?", (row["id"],))
                cards.set_fields(row["workspace"], row["id"], status="failed", generation=1)
                tasks.add_event(con, row["id"], "failed", {"reason": "Error code: 502 - Upstream access forbidden"},
                                generation=1)
                if scope - {row["id"]}:
                    await scripted(con_, cfg, runner, run_id, scope=scope - {row["id"]}, **kwargs)
                return "failed"
        return await scripted(con_, cfg, runner, run_id, scope=scope, **kwargs)

    lab.monkeypatch.setattr(workflow, "_drive_tasks", drive)


def _two_cards(lab):
    plan = runs.action(lab.con, lab.run["id"], lab.root["id"], "plan")["payload"]
    plan["tasks"].append({**plan["tasks"][0], "local_id": "archive", "title": "Provincial archive",
                          "deliverable": "archive.md"})
    runs.replace_action(lab.con, lab.run, lab.root, "plan", plan, session_file=lab.run["root_session"],
                        tool_call_id="plan-2")


async def test_a_card_that_failed_for_good_goes_back_to_its_sister_when_last_order_says_so(lab):
    """2026-09-29: a card refused by the provider after its Sister's own attempts failed its whole
    node, which then waited for the root to retry it. The node's Last Order decides first."""
    con, run, root = lab.con, lab.run, lab.root
    _script(lab, reviews={1: []}, alternatives=[], dispositions=None,
            decide=lambda con_: {"decisions": [], "declined": []},
            cards_failed=lambda con_, key: {"decision": "retry", "reason": "the provider refused; it passes"})
    _fail_once(lab, "fiscal")
    assert await _expand(lab, _Runner(con)) == "closed"
    [card] = runs.tasks(con, run["id"], kind="research", node_id=root["id"])
    assert (card["status"], card["generation"]) == ("done", 2)
    [key] = [k for k in lab.prompts if k.startswith("cards_failed:")]
    assert key == runs.cards_failed_key([{"id": card["id"], "generation": 1}]) and "502" in lab.prompts[key]
    assert runs.failed_card_decisions(con, run["id"], root["id"])[0]["cards"] == [card["id"]]


async def test_a_node_goes_on_without_a_card_its_last_order_gave_up(lab):
    con, run, root = lab.con, lab.run, lab.root
    _two_cards(lab)
    _script(lab, reviews={1: []}, alternatives=[], dispositions=None,
            decide=lambda con_: {"decisions": [], "declined": []},
            cards_failed=lambda con_, key: {"decision": "conclude", "reason": "the fiscal records carry the answer"})
    _fail_once(lab, "archive")
    assert await _expand(lab, _Runner(con)) == "closed"
    by_id = {row["local_id"]: row for row in runs.tasks(con, run["id"], kind="research", node_id=root["id"])}
    assert (by_id["fiscal"]["status"], by_id["archive"]["status"]) == ("done", "failed")
    assert runs.given_up_cards(con, run["id"]) == {by_id["archive"]["id"]}
    runs.reopen_cards(con, run["id"])                       # a resume leaves it as the node left it
    assert tasks.get(con, by_id["archive"]["id"])["generation"] == 1


def test_with_no_retry_left_a_failed_review_fails_its_node_without_a_turn(lab):
    con, run, root = lab.con, lab.run, lab.root
    for call in ("a", "b"):
        runs.record_action(con, run, root, f"cards_failed:{call}", {"decision": "retry", "reason": "r", "cards": []},
                           session_file=run["root_session"], tool_call_id=call)
    lab.monkeypatch.setattr(planner, "_command", lambda *a, **k: pytest.fail("asked with nothing to choose"))
    lost = [{"id": "t_red", "generation": 3, "title": "Red team", "assignee": RED, "status": "failed"}]
    assert planner.cards_failed(con, run, {}, None, root, kind="red_team", lost=lost)["decision"] == "fail"


async def test_revise_is_refused_once_the_node_has_no_revision_left(lab):
    con, run, root = lab.con, lab.run, lab.root
    calls = []

    def dispositions(con_, key):
        calls.append(key)
        issue = runs.issues(con, run["id"], node_id=root["id"], round=int(key.split(":")[1]), origin="critique")[0]
        return {"dispositions": [{"issue_id": issue["id"], "disposition": "revise", "reason": "again"}]}

    _script(lab, reviews={1: [_issue("Dates?")], 2: [_issue("Still dates?")]}, alternatives=[],
            dispositions=dispositions, decide=lambda con_: {"decisions": [], "declined": []})
    with pytest.raises(ValueError, match="No revision is left"):
        await _expand(lab, _Runner(con))
    assert calls == ["dispose:1", "dispose:2"]
    # Each disposition reads its own round's critique, not the rounds before it.
    assert "Critique of version 1" in lab.prompts["dispose:1"] and "critique-2.md" not in lab.prompts["dispose:1"]
    assert "Critique of version 2" in lab.prompts["dispose:2"] and "Critique of version 1" not in lab.prompts["dispose:2"]
    assert runs.action(con, run["id"], root["id"], "dispose:2") is None


async def test_the_reconciliation_opens_a_fork_of_the_parent_conversation(lab):
    con, run, root = lab.con, lab.run, lab.root
    root = workflow._close(con, run, root, "closed") and runs.node(con, root["id"])
    _did = runs.add_decision(con, run["id"], node=root, round=1, origin="decide", index=0, question="Which cause?",
                             stakes="core", options=[{"label": "fiscal", "premise": "p", "own": True},
                                                     {"label": "coalitions", "premise": "q"}])
    option = runs.pending_options(con, run["id"])[0]
    asked = []

    def reconcile(con_, run_, cfg, window, *, round, material, validate):
        asked.append(material)
        payload = commands.Reconcile.model_validate({
            "nodes": [{"question": "Did local coalitions block it?", "options": [option["id"]], "rationale": "r"}],
            "summary": "open coalitions"}).model_dump()
        validate(payload)
        return runs.record_reconcile(con, run_, round, payload, session_file=run_["root_session"], tool_call_id="rc")

    lab.monkeypatch.setattr(planner, "reconcile", reconcile)
    assert await workflow._reconcile(con, {}, run, object(), poll_seconds=0, progress=None,
                                     check_active=lambda: None) is None
    assert [o["option"] for o in asked[0]["pending_options"]] == [option["id"]]
    child = runs.children(con, root["id"])[0]
    assert child["question"] == "Did local coalitions block it?" and child["status"] == "queued"
    workflow._prepare_sessions(con, run)
    child = runs.node(con, child["id"])
    # One parent: the child is a fork of the root's conversation, and knows where its own turns begin.
    forked = SessionManager.open(child["session_file"])
    assert forked.getHeader()["parentSession"] == run["root_session"]
    assert child["fork_entry"] == SessionManager.open(run["root_session"]).getLeafId()
    assert planner.deliberation_text(child["session_file"], child["fork_entry"]) == ""
    # Nothing is pending and no new conclusion exists: the next barrier needs no reconciliation.
    assert views.reconcile_state(con, runs.get(con, run["id"]))[0] is False


async def test_a_join_is_a_fork_of_the_lowest_node_its_parents_share(lab):
    con, run, root = lab.con, lab.run, lab.root
    runs.update_limits(con, run["id"], {"max_depth": 2})
    run = runs.get(con, run["id"])
    a = runs.create_node(con, run["id"], question="Fiscal limits?", parents=[root["id"]])
    b = runs.create_node(con, run["id"], question="Local coalitions?", parents=[root["id"]])
    workflow._prepare_sessions(con, run)
    join = runs.create_node(con, run["id"], question="Both at once?", parents=[a["id"], b["id"]])
    workflow._prepare_sessions(con, run)
    join = runs.node(con, join["id"])
    # Neither parent's conversation: the root's, which both of them continue.
    assert SessionManager.open(join["session_file"]).getHeader()["parentSession"] == run["root_session"]
    assert join["fork_entry"] == SessionManager.open(run["root_session"]).getLeafId()
    packet = context.build(con, run, node=join)
    assert packet["node"]["forked_from"] == root["id"]
    assert f"a fork of node `{root['id']}`'s, the lowest node your parents share" in context.render(packet)


async def test_a_gap_is_filled_inside_the_node_that_left_it_and_never_handed_down(lab):
    """2026-09-28 (user): a gap any answer needs is settled before the node forks, so no node opened from
    it inherits it, no sibling researches it again and no red team finds it a second time. (Depth 2
    researched 27 gaps about 68 times.) 2026-10-01 (user): the gaps are answered in the divergence
    review's own rounds, after the red team's loop -- not beside the red team's issues."""
    con, run, root = lab.con, lab.run, lab.root
    runs.add_decision(con, run["id"], node=root, round=1, origin="plan", index=0, question="Which method?",
                      stakes="s", options=[{"label": "decision points", "premise": "p", "own": True},
                                           {"label": "structural limits", "premise": "s"}])
    runs.prepare_runner(con, "research_execution", root["id"])
    root = runs.node(con, root["id"])
    red, review = (cards.create(con, run["workspace"], title, f"## deliverable\n{name}\n", RED, after_row=lambda tid, kind=kind:
                                runs.link_task(con, run["id"], tid, kind=kind, node=root, local_id=f"@{kind}"))
                   for title, name, kind in (("Red team", "critique.md", "red_team"), ("Divergence", "divergence.md", "divergence")))
    for tid in (red, review):
        con.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    tasks.add_event(con, red, "submitted", {"issues": [_issue("Dates?")]}, generation=1)
    tasks.add_event(con, review, "submitted", {"alternatives": [
        {"kind": "framework", "proposal": "Composite monarchy, not constitutional types", "premise": "q",
         "rationale": "another frame", "gap": False},
        {"kind": "source base", "proposal": "Nobody read the governed", "premise": "only rulers speak",
         "rationale": "any answer needs the governed", "gap": True}]}, generation=1)
    assert workflow._receive_critique(con, run, root, tasks.get(con, red), 1) is None
    assert workflow._receive_divergence(con, run, root, tasks.get(con, review), 1) is None
    by_origin = {i["origin"]: i for i in runs.issues(con, run["id"], node_id=root["id"])}
    assert set(by_origin) == {"critique", "divergence", "gap"}
    gap, rival = by_origin["gap"], by_origin["divergence"]
    # The red team's issue and the divergence review's gap are answered in turns of their own.
    calls = []

    def command(con_, run_, cfg, worker, node, prompt, *, key, model, validate, **_kwargs):
        calls.append(key)
        if key == "dispose:1":
            assert "Dates?" in prompt and "Nobody read the governed" not in prompt
            answer = [{"issue_id": by_origin["critique"]["id"], "disposition": "rebut", "reason": "no"}]
        else:
            assert "Nobody read the governed" in prompt and "Dates?" not in prompt
            with pytest.raises(ValueError, match="filled or answered"):
                validate(model.model_validate({"dispositions": [
                    {"issue_id": gap["id"], "disposition": "branch", "reason": "?"}]}).model_dump())
            answer = [{"issue_id": gap["id"], "disposition": "concede", "reason": "no such records survive"}]
        payload = validate(model.model_validate({"dispositions": answer}).model_dump())
        runs.record_action(con, run, node, key, payload, session_file=run["root_session"], tool_call_id=key)
        return runs.action(con, run["id"], node["id"], key), ""

    lab.monkeypatch.setattr(planner, "_command", command)
    dispositions = [*planner.dispose(con, run, {}, None, root, tasks.get(con, red), [by_origin["critique"]],
                                     round=1, revisions_left=1),
                    *planner.fill_gaps(con, run, {}, None, root, tasks.get(con, review), [gap],
                                       version=1, round=1, revisions_left=1)]
    assert calls == ["dispose:1", "gaps:1"] and len(dispositions) == 2
    for item in dispositions:
        runs.dispose(con, item["issue_id"], item["disposition"], reason=item["reason"])
    # The decision sees the possibility only: the gap is settled, and no gaps list exists to hand it on.
    expected = {rival["id"]}

    def decide_command(con_, run_, cfg, worker, node, prompt, *, key, model, validate, **_kwargs):
        with pytest.raises(ValueError, match="exactly once"):
            validate(model.model_validate({"decisions": [], "declined": [
                {"issue_id": gap["id"], "disposition": "decline", "reason": "?"}]}).model_dump())
        payload = validate(model.model_validate({"decisions": [{"question": "Which frame?", "stakes": "s", "options": [
            {"label": "constitutional types", "premise": "p", "own": True},
            {"label": "composite monarchy", "premise": "q", "sources": [rival["id"]]}]}]}).model_dump())
        runs.record_action(con, run, node, key, payload, session_file=run["root_session"], tool_call_id=key)
        return runs.action(con, run["id"], node["id"], key), ""

    lab.monkeypatch.setattr(planner, "_command", decide_command)
    payload = planner.decide(con, run, {}, None, root, divergence=[], proposals=[rival], branches=[])
    assert {s for d in payload["decisions"] for o in d["options"] for s in o["sources"]} == expected
    workflow._apply_decisions(con, run, root, root, payload)
    assert runs.issue(con, gap["id"])["disposition"] == "concede"
    root = workflow._close(con, run, root, "closed") and runs.node(con, root["id"])
    pending = runs.pending_options(con, run["id"])
    assert [o["label"] for o in pending] == ["structural limits", "composite monarchy"]
    paths = views.render_paths(views.snapshot(con, runs.get(con, run["id"])))
    assert "gap" not in paths and f"takes up `{rival['id']}`" in paths

    def reconcile(con_, run_, cfg, window, *, round, material, validate):
        payload = commands.Reconcile.model_validate({
            "nodes": [{"question": o["label"], "options": [o["id"]], "rationale": "r"} for o in pending],
            "summary": "open both"}).model_dump()
        validate(payload)
        return runs.record_reconcile(con, run_, round, payload, session_file=run_["root_session"], tool_call_id="rc")

    lab.monkeypatch.setattr(planner, "reconcile", reconcile)
    assert await workflow._reconcile(con, {}, run, object(), poll_seconds=0, progress=None,
                                     check_active=lambda: None) is None
    children = runs.children(con, root["id"])
    assert len(children) == 2                     # the two possibilities; the gap opened nothing
    for child in children:
        # The child sees the parent's conceded cost among the parent's issues, and takes up nothing.
        packet = context.render(context.build(con, run, node=child))
        assert "Gaps this node takes up" not in packet and "→ concede: no such records survive" in packet
        assert "Nobody read the governed" not in planner.position(con, run, child)
    record = report._record(con, runs.get(con, run["id"]))
    assert [c["issue"] for c in record["costs"]] == [gap["id"]] and "gaps" not in record


async def test_an_erratum_corrects_an_earlier_conclusion_without_rewriting_it(lab):
    """2026-09-28 (user, 21): a later node that finds an earlier conclusion wrong -- a term nobody
    coined, a date -- records the correction; the earlier text stands, the correction travels with it."""
    con, run, root = lab.con, lab.run, lab.root
    _conclude = lambda node: runs.write_text(con, run["id"], "synthesis", "c", runs.generated_path(
        "synthesis.md", node_id=node["id"]), "Lebow's cuspal counterfactuals\n", branch_id=node["id"])
    _conclude(root)
    child = runs.create_node(con, run["id"], question="Check the term", parents=[root["id"]])
    workflow._prepare_sessions(con, run)
    child = runs.node(con, child["id"])
    tool = commands.erratum_tool(con, run, child, session_file=child["session_file"])
    ctx = SimpleNamespace(sessionManager=SimpleNamespace(sessionFile=child["session_file"]))
    erratum = {"node": root["id"], "claim": "Lebow's cuspal counterfactuals", "correction":
               "Lebow writes of plausible-world and miracle counterfactuals; no 'cuspal' pair exists",
               "grounds": "nodes/child/cards/t_1/C5.md"}
    with pytest.raises(ValueError, match="another node"):
        await tool.execute("call-0", {**erratum, "node": child["id"]}, None, None, ctx)
    await tool.execute("call-1", erratum, None, None, ctx)
    [recorded] = runs.errata(con, run["id"])
    assert recorded["raised_by"] == child["id"] and recorded["node"] == root["id"]
    assert runs.artifact_text(views.syntheses(con, run, root)[-1]) == "Lebow's cuspal counterfactuals\n"
    # It reaches every node below the corrected one, the views and the report's record.
    grandchild = runs.create_node(con, run["id"], question="Below", parents=[child["id"]]) \
        if runs.limits(run)["max_depth"] > 1 else child
    assert "no 'cuspal' pair exists" in context.render(context.build(con, run, node=grandchild))
    assert "no 'cuspal' pair exists" in planner.errata_above(con, run, grandchild)
    data = views.snapshot(con, runs.get(con, run["id"]))
    assert "no 'cuspal' pair exists" in views.render_graph(data)
    assert "Corrections recorded against this node" in views.render_node(data, root["id"])
    assert report._record(con, runs.get(con, run["id"]))["errata"] == [recorded]


async def test_a_waiting_reconciliation_is_applied_only_after_the_go_ahead(lab):
    con, run, root = lab.con, lab.run, lab.root
    con.execute("UPDATE research_runs SET limits_json=? WHERE id=?",
                (json.dumps({**runs.limits(run), "plan_approval": True}), run["id"]))
    run = runs.get(con, run["id"])
    workflow._close(con, run, runs.node(con, root["id"]), "closed")
    runs.add_decision(con, run["id"], node=runs.node(con, root["id"]), round=1, origin="decide", index=0,
                      question="Which?", stakes="s", options=[{"label": "a", "premise": "p", "own": True},
                                                              {"label": "b", "premise": "q"}])
    option = runs.pending_options(con, run["id"])[0]

    def reconcile(con_, run_, cfg, window, *, round, material, validate):
        payload = {"nodes": [], "joins": [], "relations": [], "summary": "not now",
                   "not_pursued": [{"option": option["id"], "reason": "a correction, not a possibility"}]}
        return runs.record_reconcile(con, run_, round, payload, session_file=run_["root_session"], tool_call_id="rc")

    waits = []

    async def wait(con_, run_, window, *, round, **_kwargs):
        waits.append(round)
        assert runs.reconcile(con, run_["id"], round)["applied_at"] is None
        runs.approve_reconcile(con, run_, round)

    lab.monkeypatch.setattr(planner, "reconcile", reconcile)
    lab.monkeypatch.setattr(workflow, "_await_reconcile_approval", wait)
    assert await workflow._reconcile(con, {}, run, object(), poll_seconds=0, progress=None,
                                     check_active=lambda: None) is None
    assert waits == [1]
    assert runs.option(con, option["id"])["reason"] == "a correction, not a possibility"
    assert runs.reconcile(con, run["id"], 1)["applied_at"] is not None


async def test_a_clean_review_is_returned_with_its_own_round_s_critique(lab):
    """2026-09-27: the third round of a continued red-team card found nothing material; returning it
    called review_context without the round and failed the run (TypeError)."""
    con, run, root = lab.con, lab.run, lab.root
    red = {"id": "t_red", "generation": 3, "title": "Red team", "workspace": str(lab.tmp)}
    folder = lab.tmp / "nodes" / root["id"] / "cards" / "t_red"
    folder.mkdir(parents=True)
    for version in (2, 3):
        critique = folder / runs.versioned("critique.md", version)
        critique.write_text(f"Critique of version {version}\n", encoding="utf-8")
        runs.register_file(con, run["id"], "critique", critique.name, str(critique),
                           sha256=hashlib.sha256(critique.read_bytes()).hexdigest(),
                           branch_id=root["id"], task_id="t_red")
    await workflow._return_review(con, run, runs.node(con, root["id"]), red, [], "No material issues on version 3.",
                                  round=3)
    [returned] = [entry for entry in SessionManager.open(run["root_session"]).getEntries()
                  if entry.get("type") == "custom_message" and entry.get("customType") == "research-review"]
    assert "Critique of version 3" in returned["content"] and "Critique of version 2" not in returned["content"]


async def test_a_node_at_max_depth_is_reviewed_without_a_divergence_card_and_closes(lab):
    """2026-09-28 (r_84479c4dd5 depth 2): a node at max_depth has no divergence review, and driving
    its empty card scope after the red team returned 'failed' -- every last-level node failed at
    critiquing the moment its review came back."""
    con, run, root = lab.con, lab.run, lab.root
    runs.update_limits(con, run["id"], {"max_depth": 0})
    lab.run = runs.get(con, run["id"])
    _script(lab, reviews={1: []}, alternatives=[], dispositions=lambda con_, key: {"dispositions": []},
            decide=lambda con_: {"decisions": [], "declined": []})
    assert await _expand(lab, _Runner(con)) == "closed"
    assert runs.tasks(con, run["id"], kind="divergence", node_id=root["id"]) == []
    assert runs.node(con, root["id"])["status"] == "closed" and not runs.node(con, root["id"])["last_error"]


def test_a_correction_is_appended_to_the_conclusion_as_it_stands(lab):
    """2026-09-29: told to state the corrected text, Last Order also edited the conclusion file
    herself before the command; the integrity check on the registered text then failed the node."""
    con, run, root = lab.con, lab.run, lab.root
    workflow._write(con, run, root, "synthesis", "Conclusion", "synthesis.md", "The treaty was signed in 1701.\n", version=1)
    row = workflow._synthesis(con, run, root, 1)
    Path(row["path"]).write_text("The treaty was signed in 1707.\n", encoding="utf-8")     # her own edit, after registration
    with pytest.raises(ValueError, match="changed since it was registered"):
        runs.artifact_text(row)
    workflow._append_corrections(con, run, root, 1, [{"disposition": "correct", "reason": "1707, not 1701"},
                                                     {"disposition": "rebut", "reason": "no"}])
    row = workflow._synthesis(con, run, root, 1)
    text = runs.artifact_text(row)                                       # registered again as it stands
    assert text.startswith("The treaty was signed in 1707.") and text.rstrip().endswith("- 1707, not 1701")
    assert "## Corrections" in text

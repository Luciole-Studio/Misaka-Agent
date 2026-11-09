"""The research graph as a conversation between lines of inquiry: a node answers its rivals as well
as its parents, a join confronts the paths it carries, a dissolution of the user's question waits
for the user, the survey records connections between nodes, and a re-review continues the red
team's own card in her own conversation."""
import asyncio
import os
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from misaka.core import graph as graph_core
from misaka.core.network import worker
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
from misaka.utils.prompt_file import readPromptFile


@pytest.fixture
def lab(tmp_path, monkeypatch):
    monkeypatch.setattr(repo, "enabled", lambda *a: False)
    monkeypatch.setattr(tasks, "task_state_dir", lambda tid: str(tmp_path / "state" / tid))
    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        run = runs.create(con, workspace=str(tmp_path), question="Why did the reform fail?", limits={"max_depth": 2})
        assert runs.acquire_driver(con, run["id"], "driver:test")
        manager = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
        manager.appendMessage({"role": "user", "content": "start", "timestamp": 1})
        manager.appendMessage({"role": "assistant", "content": [{"type": "text", "text": "ok"}], "timestamp": 2})
        con.execute("UPDATE research_runs SET root_session=? WHERE id=?", (manager.getSessionFile(), run["id"]))
        yield SimpleNamespace(con=con, run=runs.get(con, run["id"]), root=runs.root(con, run["id"]), tmp=tmp_path,
                              monkeypatch=monkeypatch)


def _conclude(con, run, node, text="Conclusion"):
    runs.write_text(con, run["id"], "synthesis", "Conclusion", runs.generated_path("synthesis.md", node_id=node["id"]),
                    text, branch_id=node["id"])


def _frames(lab):
    """The root forks into two frames; one of them is opened as a node, the other not."""
    con, run, root = lab.con, lab.run, lab.root
    did = runs.add_decision(con, run["id"], node=root, round=1, origin="plan", index=0,
                            question="Which frame explains it?", stakes="the cause the answer names",
                            options=[{"label": "fiscal", "premise": "money constrained it"},
                                     {"label": "coalitions", "premise": "local alliances blocked it"},
                                     {"label": "personality", "premise": "the minister's character"}])
    fiscal, coalitions, personality = runs.options(con, run["id"], decision_id=did)
    a = runs.create_node(con, run["id"], question="Did money constrain it?", parents=[root["id"]])
    b = runs.create_node(con, run["id"], question="Did alliances block it?", parents=[root["id"]])
    runs.set_option(con, fiscal["id"], node_id=a["id"])
    runs.set_option(con, coalitions["id"], node_id=b["id"])
    runs.set_option(con, personality["id"], reason="no archive can speak to it")
    return a, b


def test_a_node_is_told_its_rival_options_and_what_became_of_them(lab):
    a, b = _frames(lab)
    _conclude(lab.con, lab.run, b, "Alliances blocked it.")
    text = planner.position(lab.con, lab.run, a)
    assert 'option "fiscal"' in text
    assert 'rival "coalitions"' in text and f"pursued by node {b['id']}" in text and "synthesis.md" in text
    assert 'rival "personality"' in text and "not pursued: no archive can speak to it" in text
    assert "strongest case of each rival option" in text
    assert "Confront them" not in text
    packet = context.build(lab.con, lab.run, node=a)
    assert {r["label"] for r in packet["reached_by"][0]["rivals"]} == {"coalitions", "personality"}
    assert 'rival "coalitions"' in context.render(packet)


def test_a_join_is_told_to_confront_its_parents_not_to_average_them(lab):
    a, b = _frames(lab)
    for node in (a, b):
        lab.con.execute("UPDATE research_execution SET status='closing' WHERE id=?", (node["id"],))
    join = runs.create_node(lab.con, lab.run["id"], question="How did money and alliances lock each other?",
                            parents=[a["id"], b["id"]])
    text = planner.position(lab.con, lab.run, join)
    assert "a join of nodes" in text and "Confront them" in text and "Never average" in text


def _dissolve(lab, node, question, version=1):
    runs.record_action(lab.con, lab.run, node, runs.dissolve_key(version), {"question": question, "grounds": "a false alternative"},
                       session_file=lab.run["root_session"], tool_call_id=f"dissolve-{node['id']}-{version}")


def test_a_dissolution_belongs_to_the_conclusion_it_was_written_with(lab):
    a, _b = _frames(lab)
    _conclude(lab.con, lab.run, a)
    _dissolve(lab, a, "research_question")
    assert views.dissolution(lab.con, lab.run, a)["question"] == "research_question"
    assert [n["id"] for n in views.dissolving_nodes(lab.con, lab.run)] == [a["id"]]
    runs.write_text(lab.con, lab.run["id"], "synthesis", "Revised", runs.generated_path("synthesis-2.md", node_id=a["id"]),
                    "Revised conclusion", branch_id=a["id"])
    assert views.dissolution(lab.con, lab.run, a) is None        # the revision answers the question after all
    assert views.dissolving_nodes(lab.con, lab.run) == []


async def test_the_root_s_dissolution_is_always_of_the_research_question(lab):
    tool = workflow._dissolve_tool(lab.con, lab.run, lab.root, 1)
    runs.set_node(lab.con, lab.root["id"], status="synthesizing")
    ctx = SimpleNamespace(sessionManager=SimpleNamespace(sessionFile=lab.run["root_session"]))
    await tool.execute("call", {"question": "this_node", "grounds": "a confusion"}, None, None, ctx)
    recorded = runs.action(lab.con, lab.run["id"], lab.root["id"], runs.dissolve_key(1))
    assert recorded["payload"]["question"] == "research_question"


async def test_the_final_report_waits_for_the_user_s_word_on_a_dissolved_question(lab):
    a, _b = _frames(lab)
    _conclude(lab.con, lab.run, a)
    _dissolve(lab, a, "research_question")
    presented, registered = [], []

    def present(con, run, cfg, window, *, material):
        presented.append(material)

    lab.monkeypatch.setattr(planner, "present_dissolution", present)
    session = SimpleNamespace(registerCustomTools=registered.extend, unregisterCustomTools=lambda tools: None)
    window = SimpleNamespace(session=session)

    async def consent_later():
        while not registered:
            await asyncio.sleep(0)
        ctx = SimpleNamespace(sessionManager=SimpleNamespace(sessionFile=lab.run["root_session"]))
        await registered[0].execute("call", {"agreed": False, "note": "answer it as asked"}, None, None, ctx)

    user = asyncio.create_task(consent_later())
    assert await workflow._await_consent(lab.con, {}, lab.run, window, poll_seconds=0, progress=None,
                                         check_active=lambda: None) is None
    await user
    assert presented[0][0]["node"] == a["id"] and registered[0].name == "misaka_research_consent"
    record = report._record(lab.con, lab.run)
    assert record["dissolution_consent"] == {"agreed": False, "note": "answer it as asked"}
    assert record["dissolutions"][0]["node"] == a["id"]
    # Decided once: a resumed run does not ask again.
    assert await workflow._await_consent(lab.con, {}, lab.run, window, poll_seconds=0, progress=None,
                                         check_active=lambda: None) is None
    assert len(presented) == 1


async def test_a_node_s_own_question_dissolving_needs_no_consent(lab):
    a, _b = _frames(lab)
    _conclude(lab.con, lab.run, a)
    _dissolve(lab, a, "this_node")
    lab.monkeypatch.setattr(planner, "present_dissolution", AsyncMock(side_effect=AssertionError("asked")))
    assert await workflow._await_consent(lab.con, {}, lab.run, SimpleNamespace(session=None), poll_seconds=0,
                                         progress=None, check_active=lambda: None) is None


async def test_the_survey_records_connections_once(lab):
    a, b = _frames(lab)
    tool = commands.relate_tool(lab.con, lab.run, session_file=lab.run["root_session"],
                                validate=lambda value: views.resolve_relations(lab.con, lab.run, value))
    ctx = SimpleNamespace(sessionManager=SimpleNamespace(sessionFile=lab.run["root_session"]))
    relation = {"kind": "resonates", "nodes": [a["id"], b["id"]], "note": "both read the same petitions"}
    await tool.execute("c1", {"relations": [relation]}, None, None, ctx)
    await tool.execute("c2", {"relations": [relation]}, None, None, ctx)
    assert [(r["kind"], r["nodes"]) for r in runs.relations(lab.con, lab.run["id"])] == [("resonates", [a["id"], b["id"]])]
    with pytest.raises(ValueError, match="at least two distinct nodes"):
        await tool.execute("c3", {"relations": [{**relation, "nodes": [a["id"], a["id"]]}]}, None, None, ctx)


def _red_card(lab):
    con, run, root = lab.con, lab.run, lab.root
    runs.prepare_runner(con, "research_execution", root["id"])
    root = runs.node(con, root["id"])
    tid = cards.create(con, run["workspace"], "Red team", "## deliverable\ncritique.md\n", "10033",
                       after_row=lambda tid: runs.link_task(con, run["id"], tid, kind="red_team", node=root, round=1))
    return root, tid


def test_one_red_team_card_keeps_each_round_s_issues_and_files_apart(lab):
    """A re-review continues the round-1 card: its answered round-1 issues stay, its round-2 issues
    are recorded beside them, and each version's critique is its own file."""
    con, run = lab.con, lab.run
    root, tid = _red_card(lab)
    card = tasks.get(con, tid)
    issue = {"kind": "", "question": "Dates?", "rationale": "r", "priority": 0}
    workflow._receive_issues(con, run, root, card, round=1, items=[{"origin": "critique", **issue}])
    [first] = runs.issues(con, run["id"], node_id=root["id"], round=1)
    runs.dispose(con, first["id"], "revise", reason="fixed")
    workflow._receive_issues(con, run, root, card, round=2, items=[{"origin": "critique", **issue, "question": "Still?"}])
    assert [(i["round"], i["question"], i["disposition"]) for i in runs.issues(con, run["id"], node_id=root["id"])] == [
        (1, "Dates?", "revise"), (2, "Still?", None)]
    out = Path(card["output_dir"])
    out.mkdir(parents=True, exist_ok=True)
    for name in ("critique.md", "critique-2.md"):
        (out / name).write_text(f"# {name}\n", encoding="utf-8")
        runs.write_text(con, run["id"], "critique", name, os.path.relpath(out / name, run["workspace"]),
                        f"# {name}\n", branch_id=root["id"], task_id=tid)
    assert [Path(p).name for p in workflow._critique_paths(con, run, root, [2])] == ["critique-2.md"]
    assert [Path(p).name for p in workflow._critique_paths(con, run, root, [1, 2])] == ["critique.md", "critique-2.md"]
    assert (runs.version_of("x/critique-2.md", "critique.md"), runs.version_of("critique.md", "critique.md"),
            runs.version_of("notes.md", "critique.md")) == (2, 1, None)


def test_a_headless_continuation_claims_a_new_generation_and_the_message_is_the_turn(lab):
    from misaka.core.network import dispatch
    con, run = lab.con, lab.run
    _root, tid = _red_card(lab)
    con.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    cards.set_fields(run["workspace"], tid, status="done")
    seen = []
    lab.monkeypatch.setattr(dispatch, "_profile_dir", lambda cfg, assignee: str(lab.tmp))
    lab.monkeypatch.setattr(worker, "run_card", lambda task, *a, **k: seen.append(task) or {"settled": True})
    assert dispatch.run_task(con, tasks.get(con, tid), {"provider": "p", "default_model": "m"}, say="Review version 2.")
    [task] = seen
    assert task["_say"] == "Review version 2." and int(task["generation"]) == 2
    assert tasks.latest_payload(con, tid, "continued", generation=2) is not None
    # A card that is not settled is not continued.
    assert not dispatch.run_task(con, tasks.get(con, tid), {"provider": "p", "default_model": "m"}, say="again")


async def test_the_runners_continue_a_card_the_way_the_sister_tools_do(lab):
    from misaka.core.research import node as node_module
    from misaka.ui.panel import client as net
    sent = []
    lab.monkeypatch.setattr(net, "request", lambda method, params=None: sent.append((method, params)) or {})
    await node_module.PaneRunner(lab.con, {}, "root", "p1").continue_card("t_1", "go on", expected_generation=3)
    assert sent == [("pane.continue_card", {"task_id": "t_1", "say": "go on", "expected_generation": 3,
                                            "place": {"grid": "p1"}})]
    headless = node_module.HeadlessRunner(lab.con, {})
    spawned, exited = [], []
    lab.monkeypatch.setattr(headless._processes, "spawn", lambda argv, **k: spawned.append(argv) or SimpleNamespace(
        poll=lambda: 0 if exited else None, pid=1))
    await headless.continue_card("t_1", "go on", expected_generation=3)
    # The message travels in a file, never on the command line (issue #13: WinError 206).
    [argv] = spawned
    assert argv[:-1] == [*node_module.CARD_ARGV, "t_1", "--say-file"] and "t_1" in headless.flying
    assert readPromptFile(argv[-1]) == "go on"
    exited.append(True)
    headless._reap()
    assert "t_1" not in headless.flying and not os.path.exists(argv[-1])
    assert not os.path.exists(os.path.dirname(argv[-1]))


async def test_a_review_brief_too_long_for_a_command_line_reaches_the_card_whole(lab, tmp_path):
    """Issue #13: a red-team brief carries the node's whole evidence ledger -- 20,000 to 460,000
    characters measured on real runs -- and Windows refuses a command line past 32,767
    (WinError 206). The child reads it from the file, every character of it."""
    from misaka.cli import research_node
    from misaka.core.research import node as node_module
    brief = ("证据 " + "x" * 97 + "\r\n") * 5000 + "\ufeff-- end"
    headless = node_module.HeadlessRunner(lab.con, {})
    spawned, exited = [], []
    lab.monkeypatch.setattr(headless._processes, "spawn", lambda argv, **k: spawned.append(argv) or SimpleNamespace(
        poll=lambda: 0 if exited else None, pid=1))
    await headless.continue_card("t_1", brief, expected_generation=1)
    [argv] = spawned
    assert sum(len(arg) for arg in argv) < 1000
    seen = []
    from misaka.config import env as env_file
    from misaka.utils import loop_watchdog
    for owner, name in ((research_node, "setupCli"), (research_node.bootstrap, "install"),
                        (loop_watchdog, "configure"), (env_file, "load")):
        lab.monkeypatch.setattr(owner, name, lambda *a, **k: None)   # process-wide entry wiring
    lab.monkeypatch.setattr(research_node.node, "main_card", lambda task_id, *, say: seen.append((task_id, say)) or 0)
    assert research_node.main(argv[3:]) == 0
    assert seen == [("t_1", brief)]
    exited.append(True)
    headless._reap()
    assert not os.path.exists(os.path.dirname(argv[-1]))


async def test_a_card_child_that_will_not_start_ends_the_node_at_the_failure_limit(lab):
    """Hermes' dispatcher counts a worker that will not start against the card's failure limit
    (outcome "spawn_failed") and parks it there; the drive loop's retry used to ask again every
    poll for as long as the run lived (issue #13: tens of thousands of retries)."""
    from misaka.core.platform import tasks as task_store
    from misaka.core.research import node as node_module
    headless = node_module.HeadlessRunner(lab.con, {})
    files = []

    def refuse(argv, **_kwargs):
        files.append(argv[-1])
        raise OSError(206, "The filename or extension is too long")

    lab.monkeypatch.setattr(headless._processes, "spawn", refuse)
    for _ in range(task_store.FAILURE_LIMIT - 1):
        with pytest.raises(OSError):
            await headless.continue_card("t_1", "go on", expected_generation=1)
    with pytest.raises(node_module.CardNotStarted, match="could not be started 3 times in a row") as raised:
        await headless.continue_card("t_1", "go on", expected_generation=1)
    assert not isinstance(raised.value, (ValueError, RuntimeError, OSError)), "the drive loop must not retry it"
    assert not any(os.path.exists(path) for path in files), "no message file outlives a child that never started"
    # A child that does start begins the count again.
    exited = []
    lab.monkeypatch.setattr(headless._processes, "spawn", lambda argv, **k: SimpleNamespace(
        poll=lambda: 0 if exited else None, pid=1))
    await headless.continue_card("t_2", "go on", expected_generation=1)
    assert headless._spawn_failures == {"t_1": task_store.FAILURE_LIMIT}
    directory, _path = headless._prompt_files["t_2"]
    exited.append(True)
    headless._reap()
    assert not os.path.exists(directory)


def test_a_card_goes_on_in_its_newest_own_conversation(tmp_path):
    folder = tmp_path / "cards" / "t_1"
    folder.mkdir(parents=True)
    assert worker.continue_flags(None, str(folder)) is None
    older, newer = folder / "a.jsonl", folder / "b.jsonl"
    older.write_text("{}\n", encoding="utf-8")
    newer.write_text("{}\n", encoding="utf-8")
    os.utime(older, (1, 1))
    assert worker.continue_flags(str(older), str(folder)) == ["--session", str(newer)]


def test_the_final_report_is_written_from_indexed_documents_one_command_per_part(lab):
    """2026-09-28: the report stages inlined 2.3 MB of the run's texts; the turn compacted and a
    two-line reply was saved as the draft. Now the run's products are indexed, the prompt carries
    their ids, and each part exists only when its command delivered it -- through the real tool."""
    con, run, root = lab.con, lab.run, lab.root
    a, b = _frames(lab)
    for node, text in ((root, "Root conclusion.\n"), (a, "Money constrained it.\n"), (b, "Alliances blocked it.\n")):
        _conclude(con, run, node, text)
        con.execute("UPDATE research_execution SET status='closed' WHERE id=?", (node["id"],))
    seen, tool_calls = [], []
    ctx = SimpleNamespace(sessionManager=SimpleNamespace(sessionFile=run["root_session"]))

    def run_llm_json(_profile, prompt, _provider, _model, **options):
        seen.append(prompt)
        names = [tool.name for tool in options["extra_tools"]]
        assert "misaka_research_report" in names
        assert "declared_findings" not in prompt                  # no material catalog inlined
        assert '"conclusion": ' in prompt or "ended without calling" in prompt   # the doc-id catalog, or the nudge
        assert '"rationale"' not in prompt.split("# Catalog")[-1].split("# Corrections")[0]   # counts, not issue texts
        # Only the node a survey section is about is listed in full; the draft lists none so.
        assert prompt.count('"cards": [') == (1 if "Finished-run survey" in prompt else 0)
        assert "## Part" not in prompt                            # no survey section is sent back inline
        if len(seen) == 1:
            return None, "I will write it next.", None            # a prose reply: nothing is recorded
        deliver = next(tool for tool in options["extra_tools"] if tool.name == "misaka_research_report")
        text = f"## Part {len(seen)}\n\nWritten from the documents."
        asyncio.run(deliver.execute(f"call-{len(seen)}", {"markdown": text}, None, None, ctx))
        tool_calls.append(text)
        return None, text, None

    worker = SimpleNamespace(run_llm_json=run_llm_json, session=None)
    cfg = {"roles_root": str(lab.tmp), "provider": "p", "default_model": "m"}
    draft = report.prepare(con, run, cfg, worker)
    # Three survey sections (one per node; the first needed the nudge) and one draft.
    assert len(tool_calls) == 4 and len(seen) == 5
    assert "ended without calling `misaka_research_report`" in seen[1]
    survey = report._checkpoint(con, run, "survey")
    assert Path(survey["path"]).read_text(encoding="utf-8").count("## Part") == 3 and Path(draft["path"]).read_text(encoding="utf-8").startswith("## Part 5")
    assert f"document `{report.doc_id(run, survey['path'])}`" in seen[-1]  # the draft reads the survey by its id
    keys = {entry.key for entry in graph_core.entries(con, run["id"])}
    assert {f"survey:{n['id']}" for n in (root, a, b)} <= keys and "draft" in keys
    # The run's products are in the corpus: the three conclusions, then the survey and draft themselves.
    docs = report.index_run(con, run)
    assert len(docs) == 5 and all(len(doc_id) == 12 for doc_id in docs.values())
    # A resume redrafts nothing: the saved checkpoints are returned as they are.
    assert report.prepare(con, run, cfg, worker)["id"] == draft["id"] and len(seen) == 5
    review = report.review_body(con, run, draft)
    assert draft["sha256"] in review and "doc_read" in review and "declared_findings" not in review

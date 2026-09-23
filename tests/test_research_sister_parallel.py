"""The persisted per-LO Sister limit reaches every Research entry point and card driver."""
import asyncio
import json
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from misaka.core.platform import cards, repo, tasks
from misaka.core.research import runs, workflow
from misaka.core.research.wiring import research


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "_commit", lambda *a: None)
    monkeypatch.setattr(repo, "enabled", lambda *a: False)
    monkeypatch.setattr(tasks, "task_state_dir", lambda tid: str(tmp_path / "state" / tid))
    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        runs.init(con)
        yield con


@pytest.mark.parametrize("key", ["parallel", "sister_parallel"])
@pytest.mark.parametrize("value", [True, False, None, 0, -1, 1.5, "0", "-1", "1.5", "eight"])
def test_concurrency_requires_positive_integer(key, value):
    with pytest.raises(ValueError, match=f"{key} must be a positive integer"):
        runs.normalize_limits({key: value})


@pytest.mark.parametrize("value", [8, "8"])
def test_concurrency_settings_are_independent(value):
    assert runs.normalize_limits({"parallel": 1, "sister_parallel": value}) == {
        "max_depth": 3, "parallel": 1, "sister_parallel": 8, "max_followups": 2}
    assert runs.normalize_limits({"parallel": value})["sister_parallel"] == 4


def test_old_run_defaults_without_rewriting_saved_limits(board, tmp_path):
    run = runs.create(board, workspace=str(tmp_path), question="Legacy run")
    old = json.dumps({"max_depth": 2, "parallel": 8, "max_followups": 2})
    board.execute("UPDATE research_runs SET limits_json=? WHERE id=?", (old, run["id"]))
    restored = runs.get(board, run["id"])
    assert runs.limits(restored) == {"max_depth": 2, "parallel": 8, "sister_parallel": 4, "max_followups": 2}
    assert restored["limits_json"] == old


@pytest.mark.parametrize("option", ["--sister-parallel 8", "--sister-parallel=8"])
def test_slash_parser_preserves_question_and_both_caps(option):
    spec = research.parse_command(f"start 2 --parallel 1 {option} --followups 0 What's changed --parallel 9?")
    assert spec == {"action": "activate", "depth": 2, "parallel": 1, "sister_parallel": 8,
                    "rounds": 0, "explicit": True, "question": "What's changed --parallel 9?"}


@pytest.mark.parametrize("text", ["--sister-parallel", "--sister-parallel=", "--sister-parallel 0 Q",
                                 "--sister-parallel -1 Q", "--sister-parallel 1.5 Q",
                                 "--sister-parallel 4 --sister-parallel 8 Q"])
def test_slash_parser_rejects_invalid_cap(text):
    with pytest.raises(ValueError):
        research.parse_command(text)


@pytest.mark.parametrize("entry", ["direct", "pending", "picker", "retry"])
async def test_chat_saves_and_displays_sister_limit(board, tmp_path, monkeypatch, entry):
    monkeypatch.setattr(research, "_con", lambda: board)
    monkeypatch.setattr(research, "_cfg", dict)
    monkeypatch.delenv("MISAKA_NET_PANE", raising=False)
    failures = [entry == "retry"]

    def init_project(*args, **kwargs):
        if failures and failures.pop():
            raise OSError("fixture startup failure")

    monkeypatch.setattr(cards, "init_project", init_project)
    notices, messages, driven, questions = [], [], [], []
    completed = asyncio.Event()

    async def drive(con, cfg, spawner, **kwargs):
        driven.append(runs.get(con, kwargs["run_id"]))
        completed.set()
        return {"reason": "waiting_input", "questions": ["Fixture question?"]}

    async def custom(factory):
        component = factory(None, None, None, lambda result: None)
        questions.extend(component.questions)
        return {"answers": {research._DEPTH_QUESTION: "2", research._PARALLEL_QUESTION: "1",
                            research._SISTER_PARALLEL_QUESTION: "8", research._ROUNDS_QUESTION: "0"}}

    monkeypatch.setattr(workflow, "run", drive)
    part = research.ResearchPart()
    part.attach(SimpleNamespace(moments=SimpleNamespace(
        send_message=lambda payload, options: messages.append(payload))))
    ctx = SimpleNamespace(cwd=str(tmp_path), isIdle=lambda: True,
                          sessionManager=SimpleNamespace(sessionId="fixture"),
                          ui=SimpleNamespace(custom=custom, notify=lambda text, kind: notices.append(text)))
    command = part.commands[0].handler
    options = "2 --parallel 1 --sister-parallel 8 --followups 0"
    try:
        if entry == "direct":
            await command(options + " Fixture question", ctx)
        else:
            await command("" if entry == "picker" else options, ctx)
            await command("status", ctx)
            assert "LO parallelism 1 | Sister cards per LO 8" in notices[-1]
            assert await part.input({"text": "Fixture question", "source": "user"}, ctx) == {"action": "handled"}
        if entry == "retry":
            async with asyncio.timeout(3):
                while not any(m.get("details", {}).get("stage") == "startup_error" for m in messages):
                    await asyncio.sleep(.001)
            await command("status", ctx)
            assert "Sister cards per LO 8" in notices[-1]
            await part.input({"text": "Fixture question", "source": "user"}, ctx)
        await asyncio.wait_for(completed.wait(), 3)
        await asyncio.sleep(0)
        assert len(driven) == 1
        assert runs.limits(driven[0]) == {"max_depth": 2, "parallel": 1, "sister_parallel": 8, "max_followups": 0,
                                         "plan_approval": True}
        assert "LO parallelism 1 | Sister cards per LO 8" in research._status(board, driven[0]["id"], str(tmp_path))
        if entry == "picker":
            assert len(questions) == 5
            question = next(q for q in questions if q["question"] == research._SISTER_PARALLEL_QUESTION)
            assert [o["label"] for o in question["options"]] == ["4", "1", "8"]
    finally:
        await part._cleanup({}, ctx)


def test_cli_persists_and_resume_keeps_saved_setting(board, tmp_path, monkeypatch):
    from misaka.cli import app
    from misaka.core.research import planner

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(app, "current_config", lambda: {"db": "fixture"})
    monkeypatch.setattr(app.db, "connect", lambda *a: board)
    monkeypatch.setattr(planner, "sister_catalog", lambda *a: [{"id": "10032"}])
    monkeypatch.setattr(cards, "init_project", Mock())
    calls = []

    async def drive(con, cfg, spawner, **kwargs):
        row = runs.get(con, kwargs["run_id"])
        assert runs.limits(row)["sister_parallel"] == 8
        assert runs.limits(row)["parallel"] == 1
        calls.append(row)
        return {"reason": "waiting_input"}

    monkeypatch.setattr(workflow, "run", drive)
    parser = app._parser()
    app._cmd_research(parser.parse_args(["research", "Question", "--parallel", "1", "--sister-parallel", "8"]))
    app._cmd_research(parser.parse_args(["research", "--resume", calls[0]["id"]]))
    assert len(calls) == 2 and calls[0]["limits_json"] == calls[1]["limits_json"]


class BatchObserved(Exception):
    pass


@pytest.mark.parametrize("saved,expected", [({"parallel": 1, "sister_parallel": 8}, 8),
                                           ({"parallel": 8, "sister_parallel": 1}, 1),
                                           ({"parallel": 8}, 4)])
@pytest.mark.parametrize("fork", [False, True])
async def test_restored_root_and_fork_dispatch_saved_width(board, tmp_path, saved, expected, fork):
    run = runs.create(board, workspace=str(tmp_path), question="Dispatch", limits=saved)
    # Exercise genuinely old JSON as well as explicitly configured new runs.
    board.execute("UPDATE research_runs SET limits_json=? WHERE id=?", (json.dumps(saved), run["id"]))
    root = runs.nodes(board, run["id"])[0]
    node = runs.create_node(board, run["id"], trigger="Child", parent_id=root["id"], depth=1) if fork else root
    ids = []
    for i in range(18):
        tid = tasks.create_task(board, f"Card {i}", assignee="10032", workspace=str(tmp_path))
        runs.link_task(board, run["id"], tid, kind="investigate", node=node)
        board.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        ids.append(tid)
    board.commit()
    # A restarted driver/new node connection reads the persisted run, not its caller's cfg.
    with closing(tasks.connect(str(tmp_path / "board.db"))) as restored:
        batches = []

        async def launch_ready(**kwargs):
            batches.append(kwargs["task_ids"])
            raise BatchObserved

        with pytest.raises(BatchObserved):
            await workflow._drive_tasks_inner(restored, {"research_parallel": 2},
                                             SimpleNamespace(launch_ready=launch_ready), run["id"],
                                             scope=set(ids), captured={}, poll_seconds=0)
        assert len(batches) == 1 and len(batches[0]) == expected
        assert set(batches[0]) <= set(ids)


@pytest.mark.parametrize("cap,expected_new", [(8, 4), (4, 0), (2, 0)])
async def test_active_help_waiting_and_unsettled_cards_share_slots(board, tmp_path, cap, expected_new):
    run = runs.create(board, workspace=str(tmp_path), question="Slots", limits={"sister_parallel": cap})
    root = runs.nodes(board, run["id"])[0]
    ids = []
    for i, status in enumerate(["running", "review", "blocked", "done", *["ready"] * 10]):
        tid = tasks.create_task(board, f"Card {i}", assignee="10032", workspace=str(tmp_path))
        runs.link_task(board, run["id"], tid, kind="investigate", node=root)
        board.execute("UPDATE tasks SET status=?,block_kind='needs_input' WHERE id=?", (status, tid))
        ids.append(tid)
    tasks.add_event(board, ids[2], "blocked", {"message_id": "help"}, generation=tasks.get(board, ids[2])["generation"])
    # Some statuses and pending processes overlap: count their union, not their sum.
    async def pending(scope):
        return {ids[0], ids[3]}

    batches = []

    async def launch_ready(**kwargs):
        batches.append(kwargs["task_ids"])
        raise BatchObserved

    with pytest.raises(BatchObserved):
        await workflow._drive_tasks_inner(board, {}, SimpleNamespace(pending=pending, launch_ready=launch_ready),
                                         run["id"], scope=set(ids), captured={}, poll_seconds=0)
    assert set(ids[:3]) <= set(batches[0])
    assert len(set(batches[0]) & set(ids[4:])) == expected_new


@pytest.mark.parametrize("host_cap,sister_cap,expected", [(12, 12, 8), (3, 12, 3), (12, 2, 2)])
async def test_sister_width_keeps_global_admission_and_dependencies(board, tmp_path, host_cap, sister_cap, expected):
    run = runs.create(board, workspace=str(tmp_path), question="Admission", limits={"sister_parallel": 8})
    root = runs.nodes(board, run["id"])[0]
    ids = []
    for i in range(10):
        tid = cards.create(board, str(tmp_path), f"Card {i}", "## deliverable\nnotes.md\n", "10032")
        runs.link_task(board, run["id"], tid, kind="investigate", node=root)
        ids.append(tid)
    dependent = cards.create(board, str(tmp_path), "Dependent", "## deliverable\nnotes.md\n", "10032", needs=[ids[0]])
    runs.link_task(board, run["id"], dependent, kind="investigate", node=root)
    admitted = []

    async def launch_ready(**kwargs):
        assert dependent not in kwargs["task_ids"]
        for tid in kwargs["task_ids"]:
            if tasks.claim(board, tid, "fixture", host_cap=host_cap, assignee_cap=sister_cap):
                admitted.append(tid)
        raise BatchObserved

    with pytest.raises(BatchObserved):
        await workflow._drive_tasks_inner(board, {}, SimpleNamespace(launch_ready=launch_ready), run["id"],
                                         scope={*ids, dependent}, captured={}, poll_seconds=0)
    assert len(admitted) == expected
    assert tasks.get(board, dependent)["status"] == "todo"

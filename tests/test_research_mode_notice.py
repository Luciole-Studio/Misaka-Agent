"""The research workflow's rules stand in front of every request while a run is open.

2026-09-18 (B31): the discipline was one custom message at run start; 16 minutes and one
compaction later Last Order was back to being an ordinary assistant and, with the run paused,
started restarting cards and editing the board by hand. Now `ResearchPart.context` rebuilds a
standing notice from the board on each request -- the discipline while a driver runs, a "wait
for /research resume" notice while the run is paused, nothing once it is done -- and nothing
is written into the transcript or the system prompt.
2026-09-18 (B26): the completion notice of a research card is skipped only while its run is
being driven; under a paused run it is news."""
from types import SimpleNamespace

import pytest

from misaka.core.platform import tasks
from misaka.core.research import runs, workflow
from misaka.core.research.wiring import research


@pytest.fixture
def board(tmp_path):
    con = tasks.connect(str(tmp_path / "board.db"))
    runs.init(con)
    return con


def _run(con, tmp_path, session="s1", status=None):
    run = runs.create(con, workspace=str(tmp_path), question="why is the sky blue", origin_session=session)
    if status:
        runs.set_state(con, run["id"], status=status)
    return run["id"]


def test_no_run_means_no_notice(board):
    assert research.mode_notice([]) is None
    assert runs.for_session(board, "s1") == []
    assert runs.for_session(board, None) == []


def test_a_board_without_research_tables_is_not_a_failure(tmp_path):
    con = tasks.connect(str(tmp_path / "plain.db"))
    assert runs.for_session(con, "s1") == []
    assert runs.is_active(con, "r_x") is False


def test_for_session_returns_only_this_conversations_runs(board, tmp_path):
    mine = _run(board, tmp_path, "s1")
    _run(board, tmp_path, "s2")
    assert [run["id"] for run in runs.for_session(board, "s1")] == [mine]


def test_an_active_run_puts_the_discipline_in_front(board, tmp_path):
    rid = _run(board, tmp_path)
    notice = research.mode_notice(runs.for_session(board, "s1"))
    assert notice.startswith(workflow.RESEARCH_DISCIPLINE.rstrip())
    assert rid in notice
    assert runs.is_active(board, rid)


def test_waiting_for_the_user_still_counts_as_driven(board, tmp_path):
    rid = _run(board, tmp_path, status="waiting_input")
    notice = research.mode_notice(runs.for_session(board, "s1"))
    assert "[Research Workflow active]" in notice
    assert "including while waiting for clarification, do not deliver" in notice
    assert "internal working conclusions and drafts are not delivered answers" in notice
    assert runs.is_active(board, rid)


@pytest.mark.parametrize("status", ["failed", "stopped"])
def test_a_paused_run_tells_lo_to_wait_for_the_user(board, tmp_path, status):
    rid = _run(board, tmp_path, status=status)
    notice = research.mode_notice(runs.for_session(board, "s1"))
    assert "[Research Workflow paused]" in notice
    assert rid in notice and status in notice
    assert "/research resume" in notice
    assert "do not continue, restart or rework" in notice
    assert "Do not deliver an answer to the unfinished" in notice
    assert "explain the plan or pause, and discuss other topics" in notice
    assert not runs.is_active(board, rid)


def test_a_finished_run_leaves_the_conversation_alone(board, tmp_path):
    _run(board, tmp_path, status="done")
    assert research.mode_notice(runs.for_session(board, "s1")) is None


def test_the_discipline_wins_over_a_paused_sibling(board, tmp_path):
    _run(board, tmp_path, status="failed")
    live = _run(board, tmp_path)
    notice = research.mode_notice(runs.for_session(board, "s1"))
    assert notice.startswith("[Research Workflow active]")
    assert live in notice


def _user(text):
    return {"role": "user", "content": [{"type": "text", "text": text}], "timestamp": 1}


def _ctx(session_id="s1"):
    return SimpleNamespace(sessionManager=SimpleNamespace(sessionId=session_id))


async def test_context_hook_prepends_an_ephemeral_notice(board, tmp_path, monkeypatch):
    monkeypatch.setattr(research, "_con", lambda: board)
    part = research.ResearchPart.__new__(research.ResearchPart)
    rid = _run(board, tmp_path)
    messages = [_user("hello")]
    result = await part.context({"messages": messages}, _ctx())
    assert result is not None
    head, *rest = result["messages"]
    assert head["role"] == "custom" and head["customType"] == "research-mode"
    assert head["display"] is False and rid in head["content"]
    assert isinstance(head["timestamp"], int)
    assert rest == messages                     # the transcript itself is untouched
    assert messages == [_user("hello")]         # and nothing was written into it


async def test_context_hook_is_silent_without_a_run(board, monkeypatch):
    monkeypatch.setattr(research, "_con", lambda: board)
    part = research.ResearchPart.__new__(research.ResearchPart)
    assert await part.context({"messages": [_user("hello")]}, _ctx()) is None
    assert await part.context({"messages": [_user("hello")]}, _ctx(None)) is None


async def test_context_hook_survives_a_broken_board(monkeypatch):
    def boom():
        raise RuntimeError("no board")
    monkeypatch.setattr(research, "_con", boom)
    part = research.ResearchPart.__new__(research.ResearchPart)
    assert await part.context({"messages": [_user("hello")]}, _ctx()) is None


def _done(run_id):
    return {"research": {"run_id": run_id, "task_id": "t1"}, "boardStatus": "done", "status": "done"}


def test_a_finished_card_is_noise_only_while_its_run_is_driven():
    assert research._feed_noise("sister-notification", _done("r1"), {"r1"})
    assert not research._feed_noise("sister-notification", _done("r1"), set())
    assert not research._feed_noise("sister-notification", _done("r1"), {"r2"})
    assert not research._feed_noise("sister-notification", {**_done("r1"), "boardStatus": "failed"}, {"r1"})
    assert not research._feed_noise("sister-notification", {"boardStatus": "done"}, {"r1"})


async def test_context_hook_keeps_finished_cards_of_a_paused_run(board, tmp_path, monkeypatch):
    monkeypatch.setattr(research, "_con", lambda: board)
    part = research.ResearchPart.__new__(research.ResearchPart)
    rid = _run(board, tmp_path, status="failed")
    note = {"role": "custom", "customType": "sister-notification", "details": _done(rid),
            "content": "card done", "timestamp": 2}
    result = await part.context({"messages": [_user("hello"), note]}, _ctx())
    assert note in result["messages"]
    runs.set_state(board, rid, status="active")
    result = await part.context({"messages": [_user("hello"), note]}, _ctx())
    assert note not in result["messages"]

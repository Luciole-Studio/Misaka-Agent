"""Putting a stopped card back in the queue is a tool, not a hand-written status.

2026-09-18 (B15/B25): nothing could return a stopped card to the queue -- `misaka_card_unblock`
refuses anything but blocked or triage, `misaka_sister` wants ready, `misaka_sister_message`
wants a session -- so Last Order wrote `UPDATE tasks SET status='ready'` twice, watched the card
file put it back both times (B24), and finally wrote a script that imported the repository and
called `tasks.reopen_task` itself. That call was the right one; it just had no door."""
from types import SimpleNamespace

import pytest

from misaka.core.network.wiring import network
from misaka.core.platform import cards, tasks
from misaka.core.research import runs
from misaka.core.wiring import ToolCollector

BODY = "## deliverable\nnotes.md\n"


@pytest.fixture
def board(tmp_path, monkeypatch, request):
    con = tasks.connect(str(tmp_path / "board.db"))
    request.addfinalizer(con.close)
    monkeypatch.setattr(network, "_CON", con)
    return con


@pytest.fixture
def requeue(tmp_path):
    collector = ToolCollector()
    network._install(collector, SimpleNamespace())
    tool = next(t for t in collector.tools if t.name == "misaka_card_requeue")
    ctx = SimpleNamespace(cwd=str(tmp_path))          # the tool is scoped to this project
    async def call(**args):
        return await tool.execute("fixture", args, None, None, ctx)
    call.definition = tool
    return call


def _card(con, tmp_path, status="stopped", title="T4 crossover"):
    task_id = cards.create(con, str(tmp_path), title, BODY, "10032")
    if status != "ready":
        con.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
        tasks._mirror_status(con, task_id)      # the card file is the contract, so it says so too
        con.commit()
    return task_id


def _text(result):
    return "".join(part.get("text", "") for part in (result or {}).get("content", []))


async def test_a_stopped_card_comes_back_as_a_fresh_attempt(board, tmp_path, requeue):
    task_id = _card(board, tmp_path)
    before = tasks.get(board, task_id)["generation"]
    result = _text(await requeue(task_id=task_id, confirmed=True))
    row = tasks.get(board, task_id)
    assert row["status"] == "ready"
    assert row["generation"] == before + 1
    assert row["claim_lock"] is None and row["worker_pid"] is None
    assert task_id in result and "ready" in result
    # Nothing polls for ready cards, so the reply must not imply the work restarts by itself.
    assert "not running yet" in result and "misaka_dispatch" in result
    kinds = [r["kind"] for r in board.execute("SELECT kind FROM events WHERE task_id=?", (task_id,))]
    assert "reopened" in kinds


async def test_a_failed_card_is_requeued_too(board, tmp_path, requeue):
    task_id = _card(board, tmp_path, status="failed")
    await requeue(task_id=task_id, confirmed=True)
    assert tasks.get(board, task_id)["status"] == "ready"


async def test_requeueing_needs_the_users_word(board, tmp_path, requeue):
    task_id = _card(board, tmp_path)
    with pytest.raises(ValueError, match="confirmed=true"):
        await requeue(task_id=task_id, confirmed=False)
    assert tasks.get(board, task_id)["status"] == "stopped"


@pytest.mark.parametrize("status,hint", [
    ("blocked", "misaka_card_unblock"),
    ("done", "misaka_sister_message"),
    ("ready", "misaka_card_unblock"),
])
async def test_only_a_stopped_or_failed_card_is_requeued(board, tmp_path, requeue, status, hint):
    task_id = _card(board, tmp_path, status=status)
    with pytest.raises(ValueError, match=hint):
        await requeue(task_id=task_id, confirmed=True)


async def test_a_card_from_another_project_is_not_visible(board, tmp_path, requeue):
    other = tmp_path.parent / "other-project"
    other.mkdir(exist_ok=True)
    elsewhere = _card(board, other)
    with pytest.raises(ValueError, match="not found in this project"):
        await requeue(task_id=elsewhere, confirmed=True)


async def test_a_research_card_is_left_to_the_user_and_research_resume(board, tmp_path, requeue):
    """`/research resume` is human-only by the user's rule: the run's driver owns its cards,
    and requeueing one behind the driver's back is what B13's mass-stop was repaired with."""
    task_id = _card(board, tmp_path)
    run = runs.create(board, workspace=str(tmp_path), question="why is the sky blue")
    node = runs.create_node(board, run["id"], trigger="why is the sky blue", parent_id=None, depth=0)
    runs.link_task(board, run["id"], task_id, kind="investigate", node=node)
    board.commit()
    with pytest.raises(ValueError, match="/research resume"):
        await requeue(task_id=task_id, confirmed=True)
    assert tasks.get(board, task_id)["status"] == "stopped"


async def test_a_card_that_moved_underneath_is_refused(board, tmp_path, requeue, monkeypatch):
    task_id = _card(board, tmp_path)
    monkeypatch.setattr(tasks, "reopen_task", lambda *a, **k: False)
    with pytest.raises(ValueError, match="changed underneath"):
        await requeue(task_id=task_id, confirmed=True)


def test_the_tool_asks_for_confirmation_and_warns_against_hand_written_statuses(requeue):
    tool = requeue.definition
    assert "confirmed" in tool.parameters["required"]
    guidance = " ".join(tool.promptGuidelines or [])
    assert "by hand" in guidance and "misaka_card_requeue" in guidance
    assert "/research resume" in tool.description

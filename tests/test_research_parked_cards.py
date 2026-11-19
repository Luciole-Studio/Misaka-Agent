"""A card blocked with no help request of its own waiting (dispatch's "blocked:" verdict, a
missing project folder) is a lost card: Last Order decides, and a resume reopens it (0.18.10
sweep). The node used to fail with no decision, or "as a dependency cycle", and stay failed."""
import asyncio

from test_research_node_routine import _expand, lab  # noqa: F401 - the fixture

from misaka.core.platform import tasks
from misaka.core.research import planner, runs


class BlockingRunner:
    """The card's process exits with a `blocked:` verdict, as dispatch records it."""

    def __init__(self, con):
        self.con = con

    async def launch_ready(self, *, context, tool_call_id, task_ids):
        for tid in task_ids:
            row = tasks.get(self.con, tid)
            if row["status"] == "ready":
                tasks.block_task(self.con, tid, "needs_input", "the archive login is required",
                                 generation=row["generation"])


async def test_a_parked_card_is_put_to_last_order_and_reopened(lab):  # noqa: F811
    con, run, root = lab.con, lab.run, lab.root
    asked = []
    lab.monkeypatch.setattr(planner, "cards_failed", lambda *a, **k: asked.append(k["lost"]) or (
        {"decision": "retry", "reason": "log in first"} if len(asked) == 1 else {"decision": "fail", "reason": "still"}))
    assert await asyncio.wait_for(_expand(lab, BlockingRunner(con)), 10) == "failed"
    [card] = runs.tasks(con, run["id"], kind="research", node_id=root["id"])
    assert len(asked) == 2 and card["generation"] == 2, "Last Order was asked, and her retry reopened the card"
    runs.resume(con, run["id"], driver_lock="driver:test")
    assert tasks.get(con, card["id"])["status"] in ("ready", "todo"), "a resume reopens it too"


async def test_a_card_waiting_on_a_parked_one_is_no_dependency_cycle(lab):  # noqa: F811
    con, run, root = lab.con, lab.run, lab.root
    plan = runs.action(con, run["id"], root["id"], "plan")["payload"]
    plan["tasks"].append({**plan["tasks"][0], "local_id": "after", "title": "After", "deliverable": "after.md",
                          "dependencies": ["fiscal"]})
    runs.replace_action(con, run, root, "plan", plan, session_file=run["root_session"], tool_call_id="plan-2")
    lab.monkeypatch.setattr(planner, "cards_failed", lambda *a, **k: {"decision": "fail", "reason": "blocked"})
    assert await asyncio.wait_for(_expand(lab, BlockingRunner(con)), 10) == "failed"


async def test_a_board_busy_for_a_moment_does_not_lose_the_drivers_lease(monkeypatch):
    """Another process held the board past the 5 s busy timeout: the renewal raised, and the run
    failed as "driver lease lost" with most of its lease to go (0.18.10 sweep)."""
    import sqlite3

    from misaka.core.research import workflow
    monkeypatch.setattr(runs, "DRIVER_TTL_SECONDS", 90)
    tries = []

    def heartbeat(con, run_id, lock):
        tries.append(1)
        if len(tries) == 1:
            raise sqlite3.OperationalError("database is locked")
        raise asyncio.CancelledError

    monkeypatch.setattr(runs, "heartbeat_driver", heartbeat)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(workflow.asyncio, "sleep", lambda seconds: real_sleep(0))
    lost = asyncio.Event()
    try:
        await workflow._keep_lease(None, "r_1", "driver", lost)
    except asyncio.CancelledError:
        pass
    assert len(tries) == 2 and not lost.is_set()

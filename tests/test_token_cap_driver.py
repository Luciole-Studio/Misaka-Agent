"""A research driver reads research.token_cap as its cards do (0.18.9 sweep, G2)."""
import asyncio
from contextlib import closing
from types import SimpleNamespace

import pytest

from misaka.core.network import dispatch
from misaka.core.platform import budget, repo, tasks
from misaka.core.research import runs, workflow


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(repo, "enabled", lambda *a, **k: False)
    monkeypatch.setattr(tasks, "task_state_dir", lambda tid: str(tmp_path / "state" / tid))
    path = str(tmp_path / "board.db")
    with closing(tasks.connect(path)) as con:
        runs.init(con)
        run = runs.create(con, workspace=str(tmp_path), question="How was the salt monopoly run?")
        from misaka.core.platform import cards
        card = cards.create(con, str(tmp_path), "licences", "body", "10032")
        runs.link_task(con, run["id"], card, kind="research", node=runs.root(con, run["id"]), local_id="a")
        yield SimpleNamespace(con=con, path=path, run=run["id"], card=card, tmp=tmp_path)


async def test_a_driver_reads_the_cap_as_its_cards_do(board, monkeypatch, tmp_path):
    """The user set research.token_cap while the run went on, and the run had already spent it: the
    driver kept the value it started with (none), and relaunched a card the cap refused on every
    poll, for ever."""
    con = board.con
    (tmp_path / "profiles" / "10032").mkdir(parents=True)
    monkeypatch.setattr(dispatch, "_profile_dir", lambda cfg, who: str(tmp_path / "profiles" / who))
    monkeypatch.setattr(workflow, "_roster_check", lambda *a, **k: asyncio.sleep(0))
    monkeypatch.setattr(budget, "default_cap", lambda: 1_000)
    budget.settle_request(con, None, board.run, 1, 1_000)
    card_cfg = {"token_cap": 1_000, "db": board.path, "provider": "anthropic", "default_model": "x",
                "profiles_root": str(tmp_path / "profiles"), "roles_root": str(tmp_path / "roles")}
    launches = []

    class Runner:
        async def pending(self, scope):
            return set()

        async def launch_ready(self, *, task_ids, **_):
            for tid in task_ids:
                if tasks.get(con, tid)["status"] == "ready":
                    launches.append(dispatch.run_task(con, tasks.get(con, tid), card_cfg))

    captured = {r["id"]: r for r in runs.tasks(con, board.run)}
    driver = asyncio.create_task(workflow._drive_tasks_inner(
        con, {"token_cap": 0}, Runner(), board.run, scope={board.card}, captured=captured, poll_seconds=0.01))
    await asyncio.wait_for(asyncio.gather(driver, return_exceptions=True), 5)
    assert len(launches) <= 1, "the driver halts instead of relaunching the refused card"


def test_a_wait_for_the_user_ends_as_a_cap_halt_once_the_cap_is_spent(board, monkeypatch):
    """Talking a plan over with Last Order can spend the cap: the wait for the go-ahead then waited
    until someone stopped the run by hand."""
    run = runs.get(board.con, board.run)
    monkeypatch.setattr(budget, "default_cap", lambda: 1_000)
    workflow._halt_at_cap(board.con, run)
    budget.settle_request(board.con, None, board.run, 1, 1_000)
    with pytest.raises(RuntimeError, match="token_cap reached"):
        workflow._halt_at_cap(board.con, run)

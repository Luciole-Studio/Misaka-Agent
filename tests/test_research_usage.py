"""What a research run spent, by conversation and by model (GitHub issue #9).

Spending could not be seen anywhere: the board's ledger keeps a token total per turn, and the
price of each call is on its message in the session file, where nothing added it up."""
import json
from contextlib import closing
from types import SimpleNamespace

import pytest

from misaka.config import sessions
from misaka.core.platform import repo, tasks
from misaka.core.research import runs, usage


def _call(model, tokens, cost, at_ms):
    usage_row = {"input": tokens // 2, "output": tokens - tokens // 2, "cacheRead": 0, "cacheWrite": 0,
                 "totalTokens": tokens, "cost": {"total": cost}}
    return {"type": "message", "message": {"role": "assistant", "provider": "p", "model": model,
                                            "usage": usage_row, "timestamp": at_ms}}


def _session(path, *entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
    return path


@pytest.fixture
def spent_run(tmp_path, monkeypatch):
    monkeypatch.setattr(repo, "enabled", lambda *a: False)
    monkeypatch.setattr(tasks, "task_state_dir", lambda tid: str(tmp_path / "state" / tid))
    monkeypatch.setattr(sessions, "sessions_root", lambda: str(tmp_path / "sessions"))
    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        runs.init(con)
        run = runs.create(con, workspace=str(tmp_path), question="How was the salt monopoly run?")
        started = run["created_at"] * 1000
        window = _session(tmp_path / "sessions" / "last-order" / "window.jsonl",
                          {"type": "session"},
                          _call("big", 900, 9.0, started - 5000),        # the window's talk before the run
                          _call("big", 100, 1.0, started + 1000))
        runs.set_state(con, run["id"], root_session=str(window))
        root = runs.root(con, run["id"])
        with tasks.write_txn(con):
            child = runs.create_node(con, run["id"], question="Who bought the licences?", parents=[root["id"]])
        _session(tmp_path / "sessions" / "research" / f"{run['id']}--node-{child['id']}" / "a.jsonl",
                 _call("big", 200, 2.0, started + 2000), {"type": "message", "message": {"role": "user"}})
        tid = tasks.create_task(con, "licences", assignee="10032", workspace=str(tmp_path))
        runs.link_task(con, run["id"], tid, kind="research", node=root, local_id="licences")
        card = tmp_path / "sessions" / "cards" / tid
        _session(card / "first.jsonl", _call("small", 50, 0.25, started + 3000))
        _session(card / "retry.jsonl", _call("unpriced", 40, 0, started + 4000))
        con.execute("UPDATE tasks SET session_dir=? WHERE id=?", (str(card), tid))
        yield con, runs.get(con, run["id"]), child, tid


def test_a_run_is_added_up_by_conversation_and_model(spent_run):
    con, run, child, tid = spent_run
    spent = usage.run_usage(con, run)
    parts = {label: (part["calls"], part["tokens"], part["cost"]) for label, part in spent["parts"]}
    assert parts == {"root Last Order": (1, 100, 1.0), f"node {child['id']}": (1, 200, 2.0),
                     f"card {tid} (Sister 10032)": (2, 90, 0.25)}, "the window's talk before the run is not the run's"
    assert (spent["total"]["tokens"], spent["total"]["cost"], spent["total"]["unpriced"]) == (390, 3.25, 1)
    assert set(spent["models"]) == {"p/big", "p/small", "p/unpriced"}
    assert usage.money(spent["total"]) == "$3.25 (+1 calls with no known price)"


def test_misaka_usage_prints_the_run_and_the_list(spent_run, monkeypatch, capsys):
    from misaka.cli import app
    con, run, _child, tid = spent_run
    monkeypatch.setattr(app, "current_config", lambda: {"db": "board.db", "token_cap": 0})
    monkeypatch.setattr(app.db, "connect", lambda path: con)
    app._cmd_usage(SimpleNamespace(run=run["id"], limit=10))
    out = capsys.readouterr().out
    assert f"card {tid} (Sister 10032)" in out and "$3.25" in out and "By model:" in out
    app._cmd_usage(SimpleNamespace(run=None, limit=10))
    assert run["id"] in capsys.readouterr().out


def test_a_window_is_charged_to_a_run_only_until_it_ended_or_the_next_run_began(tmp_path, monkeypatch):
    """0.18.7 bounded only a done run: a stopped run in the same window was charged for everything
    said there afterwards, the next run's Last Order included."""
    monkeypatch.setattr(repo, "enabled", lambda *a: False)
    monkeypatch.setattr(tasks, "task_state_dir", lambda tid: str(tmp_path / "state" / tid))
    monkeypatch.setattr(sessions, "sessions_root", lambda: str(tmp_path / "sessions"))
    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        runs.init(con)
        first = runs.create(con, workspace=str(tmp_path), question="Run A")
        second = runs.create(con, workspace=str(tmp_path), question="Run B")
        window = _session(tmp_path / "sessions" / "last-order" / "window.jsonl",
                          _call("big", 100, 1.0, 1_050_000),      # run A, before it stopped
                          _call("big", 300, 3.0, 2_000_000),      # unrelated talk after it stopped
                          _call("big", 5000, 50.0, 5_050_000))    # run B
        for run, status, created, updated in ((first, "stopped", 1000, 1100), (second, "active", 5000, 5100)):
            runs.set_state(con, run["id"], root_session=str(window))
            con.execute("UPDATE research_runs SET status=?, created_at=?, updated_at=? WHERE id=?",
                        (status, created, updated, run["id"]))
        assert usage.run_usage(con, runs.get(con, first["id"]))["total"]["cost"] == 1.0
        assert usage.run_usage(con, runs.get(con, second["id"]))["total"]["cost"] == 50.0
        con.execute("UPDATE research_runs SET status='failed', updated_at=99999 WHERE id=?", (first["id"],))
        assert usage.run_usage(con, runs.get(con, first["id"]))["total"]["cost"] == 4.0, "never past run B's start"


@pytest.mark.parametrize("status", ["active", "waiting_input", "stopping"])
def test_a_run_still_going_is_counted_to_now(tmp_path, monkeypatch, status):
    monkeypatch.setattr(repo, "enabled", lambda *a: False)
    monkeypatch.setattr(tasks, "task_state_dir", lambda tid: str(tmp_path / "state" / tid))
    monkeypatch.setattr(sessions, "sessions_root", lambda: str(tmp_path / "sessions"))
    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        runs.init(con)
        run = runs.create(con, workspace=str(tmp_path), question="Run A")
        window = _session(tmp_path / "sessions" / "last-order" / "window.jsonl",
                          _call("big", 100, 1.0, 1_050_000), _call("big", 700, 7.0, 9_000_000))
        runs.set_state(con, run["id"], root_session=str(window))
        con.execute("UPDATE research_runs SET status=?, created_at=1000, updated_at=1100 WHERE id=?",
                    (status, run["id"]))
        assert usage.run_usage(con, runs.get(con, run["id"]))["total"]["cost"] == 8.0


def test_usage_outside_assistant_messages_is_counted(tmp_path):
    """pi 0.99+ records spending outside assistant messages too: a cache warm-up as a ``usage``
    entry, a compaction or branch summary's call on that entry, a tool's own call on its result."""
    def row(tokens, cost):
        return {"input": tokens, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": tokens,
                "cost": {"total": cost}}
    path = _session(tmp_path / "s.jsonl",
                    {"type": "session"},
                    _call("big", 100, 1.0, 1_000),
                    {"type": "usage", "kind": "cache_warm", "provider": "p", "model": "big",
                     "usage": row(10, 0.1), "timestamp": "1970-01-01T00:00:02.000Z"},
                    {"type": "compaction", "summary": "s", "usage": row(20, 0.2), "timestamp": "1970-01-01T00:00:03.000Z"},
                    {"type": "message", "message": {"role": "toolResult", "usage": row(30, 0.3), "timestamp": 4_000}},
                    {"type": "message", "message": {"role": "user", "content": "hi", "timestamp": 5_000}})
    models = {}
    total = usage.session_usage([str(path)], by_model=models)
    assert total["tokens"] == 160 and total["calls"] == 4
    assert models["p/big"]["tokens"] == 110 and models["Tools/summaries"]["tokens"] == 50
    assert usage.session_usage([str(path)], since_ms=2_500)["tokens"] == 50

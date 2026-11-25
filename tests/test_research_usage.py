"""What a research run spent, by conversation and by model (GitHub issue #9).

Spending could not be seen anywhere: the board's ledger keeps a token total per turn, and the
price of each call is on its message in the session file, where nothing added it up."""
import asyncio
import json
import os
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from misaka.config import home, sessions
from misaka.core.platform import budget, repo, tasks
from misaka.core.research import runs, usage
from misaka.core.runtime_trace import scope_event, trace_scope
from misaka.core.session_manager import SessionManager


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
    now = int(time.time())
    monkeypatch.setattr(runs, "time", SimpleNamespace(time=lambda: now))
    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        runs.init(con)
        run = runs.create(con, workspace=str(tmp_path), question="How was the salt monopoly run?")
        started = run["created_at"] * 1000
        window = _session(tmp_path / "sessions" / "last-order" / "window.jsonl",
                          {"type": "session"},
                          _call("big", 900, 9.0, started - 5000),
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


def test_forked_research_history_does_not_charge_the_parent_request_twice(spent_run):
    con, run, child, _ = spent_run
    started = run["created_at"] * 1000
    session_root = Path(sessions.sessions_root())
    root_session = SessionManager.create(run["workspace"], str(session_root / "last-order"))
    with trace_scope(run["id"], False):
        root_session.appendMessage(_call("big", 100, 1.0, started + 1000)["message"])
    runs.set_state(con, run["id"], root_session=root_session.getSessionFile())
    child_dir = session_root / "research" / f"{run['id']}--node-{child['id']}"
    fork = SessionManager.forkFrom(root_session.getSessionFile(), run["workspace"], str(child_dir))
    # Same provider, model, timestamp and amounts, but a fresh request in the child.
    with trace_scope(run["id"], False):
        fork.appendMessage(_call("big", 100, 1.0, started + 1000)["message"])

    spent = usage.run_usage(con, runs.get(con, run["id"]))

    assert spent["total"]["calls"] == 5
    assert spent["total"]["tokens"] == 490
    assert spent["models"]["p/big"]["tokens"] == 400


def test_request_identity_deduplicates_only_the_same_request(tmp_path):
    a, b = (_call("big", 100, 1.0, 2000) for _ in range(2))
    a["message"]["requestId"] = "request-a"
    b["message"]["requestId"] = "request-b"
    first = _session(tmp_path / "first.jsonl", a)
    second = _session(tmp_path / "second.jsonl", a, b)

    assert usage.session_usage([first, second])["calls"] == 2


def test_usage_keeps_the_spelling_of_distinct_resolved_windows_paths(tmp_path, monkeypatch):
    upper, lower = tmp_path / "Sales.jsonl", tmp_path / "sales.jsonl"
    with monkeypatch.context() as patch:
        # Model an NTFS directory that contains both names as returned by realpath.
        patch.setattr(os.path, "realpath", os.fspath)
        patch.setattr(os.path, "normcase", lambda path: os.fspath(path).lower())
        assert usage._path_key(upper) != usage._path_key(lower)


@pytest.mark.parametrize("seed_format", ["current", "legacy", "unproven"])
def test_usage_cli_counts_native_fork_seed_only_once(tmp_path, monkeypatch, capsys, seed_format):
    from misaka.cli import app
    from misaka.core.subagent import fork

    monkeypatch.setattr(repo, "enabled", lambda *a: False)
    monkeypatch.setattr(sessions, "sessions_root", lambda: str(tmp_path / "sessions"))
    board = tmp_path / "board.db"
    monkeypatch.setattr(app, "current_config", lambda: {"db": str(board), "token_cap": 0})
    with closing(tasks.connect(str(board))) as con:
        runs.init(con)
        run = runs.create(con, workspace=str(tmp_path), question="Fork usage")
        parent = SessionManager.create(str(tmp_path), runs.session_dir(run, "root-lo"))
        at_ms = run["created_at"] * 1000 + 1000
        parent_call = _call("big", 100, 1.0, at_ms)["message"]
        parent_call["content"] = [{"type": "toolCall", "id": "fork-tool", "name": "Agent", "arguments": {}}]
        with trace_scope(run["id"], False):
            parent.appendMessage(parent_call)
        runs.set_state(con, run["id"], root_session=parent.getSessionFile())
        directory = Path(sessions.subagent_session_dir(parent.getSessionId(), parent.getSessionFile()))
        directory.mkdir(parents=True)
        transcript = directory / "agent-usage.jsonl"
        task = SimpleNamespace(id="usage", cwd=str(tmp_path), transcript=transcript,
                               metadata_path=transcript.with_suffix(".meta.json"),
                               parent_session_id=parent.getSessionId(), tool_call_id="fork-tool")
        session = SimpleNamespace(agent=SimpleNamespace(state=SimpleNamespace(messages=[parent_call], tools=[])),
                                  systemPrompt="test")
        fork.capture(task, session, seed=True)
        if seed_format != "current":
            rows = [json.loads(line) for line in transcript.read_text(encoding="utf-8").splitlines()]
            # Simulate the original transcript without its new bookkeeping entry.
            _session(transcript, *(row for row in rows if row.get("type") != "custom"))
            task.metadata_path.write_text(json.dumps({
                "forked": True, "agentId": task.id, "parentSessionId": task.parent_session_id,
                "toolUseId": "wrong" if seed_format == "unproven" else task.tool_call_id,
            }), encoding="utf-8")
        child = SessionManager.open(str(transcript))
        child.appendMessage({"role": "user", "content": fork.child_message("work"), "timestamp": at_ms})
        with trace_scope(run["id"], False):
            child.appendMessage(_call("big", 50, 0.5, at_ms)["message"])

        monkeypatch.setattr(app.db, "connect", lambda path: con)
        app._cmd_usage(SimpleNamespace(run=run["id"], limit=10))
        out = capsys.readouterr().out
        assert any(line.strip().startswith("total") and "150 tokens" in line and "$1.50" in line
                   for line in out.splitlines())
        if seed_format == "unproven":
            unverified = next(line for line in out.splitlines() if "Unverified fork history" in line)
            assert "100 tokens" in unverified
        else:
            assert "Unverified fork history" not in out


@pytest.mark.parametrize("first_legacy", [False, True])
@pytest.mark.parametrize("second_legacy", [False, True])
def test_usage_cli_counts_only_the_panel_windows_children_in_each_run(
    tmp_path, monkeypatch, capsys, first_legacy, second_legacy,
):
    from misaka.cli import app
    from misaka.core.subagent import fork

    monkeypatch.setattr(repo, "enabled", lambda *a: False)
    board = home.path("db")
    monkeypatch.setattr(app, "current_config", lambda: {"db": str(board), "token_cap": 0})
    directory = sessions.role_dir()
    window = SessionManager.create(str(tmp_path), str(directory))
    other_window = SessionManager.create(str(tmp_path), str(directory))
    with closing(tasks.connect(str(board))) as con:
        runs.init(con)
        monkeypatch.setattr(app.db, "connect", lambda path: con)
        first = runs.create(con, workspace=str(tmp_path), question="First in window")
        second = runs.create(con, workspace=str(tmp_path), question="Next in same window")
        other = runs.create(con, workspace=str(tmp_path), question="Another window")

        def use_legacy_entries(path):
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            for entry in rows:
                entry.pop("usageRunId", None)
            _session(path, *rows)

        def add_fork(parent, agent_id, started, parent_cost, child_cost, *, legacy=False):
            parent_call = _call("big", parent_cost, parent_cost / 100, started)["message"]
            parent_call["content"] = [{"type": "toolCall", "id": agent_id, "name": "Agent", "arguments": {}}]
            parent.appendMessage(parent_call)
            child_dir = Path(sessions.subagent_session_dir(parent.getSessionId(), parent.getSessionFile()))
            transcript = child_dir / f"agent-{agent_id}.jsonl"
            task = SimpleNamespace(id=agent_id, cwd=str(tmp_path), transcript=transcript,
                                   metadata_path=transcript.with_suffix(".meta.json"),
                                   parent_session_id=parent.getSessionId(), tool_call_id=agent_id)
            history = parent.buildSessionContext().messages
            session = SimpleNamespace(agent=SimpleNamespace(state=SimpleNamespace(messages=history, tools=[])),
                                      systemPrompt="test")
            fork.capture(task, session, seed=True)
            if legacy:
                rows = [json.loads(line) for line in transcript.read_text(encoding="utf-8").splitlines()]
                _session(transcript, *(row for row in rows if row.get("type") != "custom"))
                task.metadata_path.write_text(json.dumps({
                    "forked": True, "agentId": task.id, "parentSessionId": task.parent_session_id,
                    "toolUseId": task.tool_call_id,
                }), encoding="utf-8")
            child = SessionManager.open(str(transcript))
            child.appendMessage({"role": "user", "content": fork.child_message("work"), "timestamp": started})
            child.appendMessage(_call("big", child_cost, child_cost / 100, started)["message"])
            use_legacy_entries(transcript)

        add_fork(window, "first", 1_001_500, 100, 50, legacy=True)
        add_fork(window, "next", 1_002_000, 200, 75)
        add_fork(other_window, "other", 1_001_500, 800, 900)
        # Both shared windows use the transcript format from before usage ownership was recorded.
        for manager in (window, other_window):
            use_legacy_entries(Path(manager.getSessionFile()))
        con.execute("UPDATE research_runs SET root_session=? WHERE id=?",
                    (home.stored(other_window.getSessionFile()), other["id"]))
        for run, legacy in ((first, first_legacy), (second, second_legacy)):
            stored = window.getSessionFile() if legacy else home.stored(window.getSessionFile())
            con.execute("UPDATE research_runs SET root_session=? WHERE id=?", (stored, run["id"]))
            stored_root = con.execute("SELECT root_session FROM research_runs WHERE id=?", (run["id"],)).fetchone()[0]
            assert Path(stored_root).is_absolute() is legacy
            assert runs.get(con, run["id"])["root_session"] == window.getSessionFile()
        for run, status, stamp in ((first, "stopped", 1000), (other, "active", 1001), (second, "active", 1002)):
            con.execute("UPDATE research_runs SET status=?,created_at=?,updated_at=? WHERE id=?",
                        (status, stamp, stamp, run["id"]))
        for run, total in ((first, "150"), (second, "275"), (other, "1,700")):
            app._cmd_usage(SimpleNamespace(run=run["id"], limit=10))
            report = capsys.readouterr().out
            summary = next(line for line in report.splitlines() if line.strip().startswith("total"))
            assert summary.split()[3:5] == [total, "tokens"]
            assert "Estimated panel usage (included)" in report
            assert "Unverified fork history" not in report
        app._cmd_usage(SimpleNamespace(run=None, limit=10))
        assert capsys.readouterr().out.count("includes panel estimate") == 3


def test_usage_cli_attributes_shared_panel_entries_without_trace_files(tmp_path, monkeypatch, capsys):
    from misaka.cli import app

    monkeypatch.setattr(repo, "enabled", lambda *a: False)
    monkeypatch.setattr("misaka.core.runtime_trace._append", lambda *args, **kwargs: None)
    board = home.path("db")
    monkeypatch.setattr(app, "current_config", lambda: {"db": str(board), "token_cap": 0})
    window = SessionManager.create(str(tmp_path), str(sessions.role_dir()))
    with closing(tasks.connect(str(board))) as con:
        first = runs.create(con, workspace=str(tmp_path), question="Before resume")
        second = runs.create(con, workspace=str(tmp_path), question="Same second")
        stamp = first["created_at"]
        for run in (first, second):
            runs.set_state(con, run["id"], root_session=window.getSessionFile())
            con.execute("UPDATE research_runs SET status='done',created_at=?,updated_at=? WHERE id=?",
                        (stamp, stamp, run["id"]))

        def call(request, tokens, offset):
            message = _call("big", tokens, tokens / 100, stamp * 1000 + offset)["message"]
            message["requestId"] = request
            window.appendMessage(message)
            return message["usage"]

        with trace_scope(first["id"], False):
            call("first", 100, 100)
        with trace_scope(second["id"], False):
            call("second", 100, 100)
        call("ordinary-after-end", 700, 3000)
        with trace_scope(first["id"], False):
            call("resumed-first", 20, 3500)
            auxiliary = _call("big", 30, 0.3, 0)["message"]["usage"]
            window.appendUsage("cache_warm", "p", "big", auxiliary)
            with trace_scope(None, True):
                window.appendUsage("cache_warm", "p", "big", auxiliary)
        call("ordinary-after-resume", 800, 4000)

        monkeypatch.setattr(app.db, "connect", lambda path: con)
        for run, expected in ((first, "150"), (second, "100")):
            app._cmd_usage(SimpleNamespace(run=run["id"], limit=10))
            out = capsys.readouterr().out
            summary = next(line for line in out.splitlines() if line.strip().startswith("total"))
            assert summary.split()[3:5] == [expected, "tokens"]
            assert "Estimated panel usage" not in out and "Unassigned panel usage" not in out

        ownership = [entry["usageRunId"] for entry in window.getEntries() if entry.get("type") in {"message", "usage"}]
        assert ownership == [first["id"], second["id"], None, first["id"], first["id"], None, None]
        fork = SessionManager.forkFrom(window.getSessionFile(), str(tmp_path), str(tmp_path / "fork"))
        assert [entry["usageRunId"] for entry in fork.getEntries() if "usageRunId" in entry] == ownership


async def test_resume_here_preserves_usage_across_windows(tmp_path, monkeypatch, capsys):
    from misaka.cli import app
    from misaka.core.network.wiring.capabilities import SisterCapabilitiesPart
    from misaka.core.research import node, workflow
    from misaka.core.research.wiring import research

    monkeypatch.delenv("MISAKA_NET_PANE", raising=False)
    monkeypatch.setattr(repo, "enabled", lambda *a: False)
    monkeypatch.setattr("misaka.core.runtime_trace._append", lambda *a, **kw: None)
    monkeypatch.setattr("misaka.core.session_catalog.saved_session_candidates",
                        lambda *a: pytest.fail("Complete binding history should not consult the catalog"))
    board = home.path("db")
    monkeypatch.setattr(app, "current_config", lambda: {"db": str(board), "token_cap": 0})
    a, b = [SessionManager.create(str(tmp_path), str(sessions.role_dir())) for _ in range(2)]
    with closing(tasks.connect(str(board))) as con:
        monkeypatch.setattr(research, "_con", lambda: con)
        monkeypatch.setattr(research, "_cfg", dict)
        monkeypatch.setattr(workflow, "_reconcile", AsyncMock(return_value=None))
        monkeypatch.setattr(workflow, "_views", AsyncMock())
        monkeypatch.setattr(workflow, "_prepare_sessions", lambda *a: None)
        monkeypatch.setattr(node, "HeadlessRunner", lambda *a: SimpleNamespace(close=lambda: None))
        run = runs.create(con, workspace=str(tmp_path), question="Adopt a second window")
        run_id = run["id"]
        other = runs.create(con, workspace=str(tmp_path), question="Unrelated research")
        stamp = run["created_at"] * 1000

        def append(manager, request_id, tokens):
            message = _call("big", tokens, 0, stamp)["message"]
            manager.appendMessage({**message, "requestId": request_id})

        with trace_scope(run_id, False):
            append(a, "in-a", 100)
        for manager in (a, b):
            append(manager, "ordinary", 700)
            with trace_scope(other["id"], False):
                append(manager, "other-run", 900)
        runs.set_state(con, run_id, status="stopped", root_session=a.sessionFile)

        async def expand(*args, session, **kwargs):
            if session.sessionManager is b and not any(
                row.get("message", {}).get("requestId") == "in-b" for row in b.getEntries()
            ):
                append(b, "in-b", 40)
                b.appendUsage("cache_warm", "p", "big", _call("big", 10, 0, stamp)["message"]["usage"])
                child = Path(sessions.subagent_session_dir(b.sessionId, b.sessionFile))
                SessionManager.forkFrom(b.sessionFile, str(tmp_path), str(child))
            return {"reason": "waiting_input", "questions": ["Pause at this boundary."]}

        monkeypatch.setattr(workflow, "_expand", expand)

        async def resume(manager, *, here):
            settled = asyncio.Event()
            outcomes = []

            def send(payload, _options):
                if payload["customType"] in {"research-error", "research-clarification"}:
                    outcomes.append(payload)
                    settled.set()

            part = research.ResearchPart()
            part.attach(SimpleNamespace(sessionManager=manager, moments=SimpleNamespace(
                parts=[SisterCapabilitiesPart(str(tmp_path), [])], send_message=send)))
            notices = []
            ctx = SimpleNamespace(cwd=str(tmp_path), isIdle=lambda: True, sessionManager=manager,
                                  ui=SimpleNamespace(notify=lambda message, kind: notices.append((message, kind))))
            suffix = " --here" if here else ""
            await part.commands[0].handler(f"resume {run_id}{suffix}", ctx)
            try:
                assert notices[-1][1] == "info", notices
                await asyncio.wait_for(settled.wait(), 5)
                assert outcomes[-1]["customType"] == "research-clarification", outcomes
                assert runs.get(con, run_id)["root_session"] == manager.sessionFile
            finally:
                await part._cleanup({}, ctx)

        await resume(b, here=True)
        with closing(tasks.connect(str(board))) as reopened:
            monkeypatch.setattr(app.db, "connect", lambda _path: reopened)
            for index, manager in enumerate((b, a, a)):
                if index:
                    await resume(manager, here=index == 1)
                app._cmd_usage(SimpleNamespace(run=run_id, limit=10))
                out = capsys.readouterr().out
                total = next(line for line in out.splitlines() if line.strip().startswith("total"))
                assert total.split()[1:5] == ["3", "calls", "150", "tokens"]
                assert "Window history incomplete" not in out
            app._cmd_usage(SimpleNamespace(run=None, limit=10))
            row = next(line for line in capsys.readouterr().out.splitlines() if line.startswith(run_id))
            assert "150 tokens" in row
            assert len(con.execute("SELECT id FROM events WHERE run_id=? AND kind=?",
                                   (run_id, runs._ROOT_SESSION_BOUND)).fetchall()) == 3


@pytest.mark.parametrize("failure", ["UPDATE OF root_session ON research_runs", "INSERT ON events"])
def test_root_binding_rolls_back_with_its_event_and_checks_driver(tmp_path, failure):
    with closing(tasks.connect(str(home.path("db")))) as con:
        run_id = runs.create(con, workspace=str(tmp_path), question="Atomic root binding")["id"]
        a, b = Path(sessions.role_dir()) / "a.jsonl", Path(sessions.role_dir()) / "b.jsonl"
        _session(a, {"type": "session", "id": "window-a"})
        runs.set_state(con, run_id, root_session=str(a))
        assert runs.acquire_driver(con, run_id, "old-driver")
        runs.release_driver(con, run_id, "old-driver")
        assert runs.acquire_driver(con, run_id, "new-driver")
        with pytest.raises(RuntimeError, match="driver lease lost"):
            runs.set_state(con, run_id, root_session=str(b), driver_lock="old-driver")
        con.execute(f"CREATE TEMP TRIGGER fail_binding BEFORE {failure} "
                    "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
            runs.set_state(con, run_id, root_session=str(b), driver_lock="new-driver")
        assert runs.get(con, run_id)["root_session"] == str(a)
        windows, complete = runs.root_session_history(con, run_id)
        assert complete and set(windows) == {usage._path_key(a)}


def test_window_binding_records_time_after_entering_the_board_transaction(tmp_path, monkeypatch):
    now = 1000
    monkeypatch.setattr(runs, "time", SimpleNamespace(time=lambda: now))
    with closing(tasks.connect(str(home.path("db")))) as con:
        run_id = runs.create(con, workspace=str(tmp_path), question="Time the window binding")["id"]
        write_txn = tasks.write_txn

        @contextmanager
        def delayed(connection):
            nonlocal now
            with write_txn(connection):
                now = 1005
                yield

        monkeypatch.setattr(tasks, "write_txn", delayed)
        updated = runs.set_state(con, run_id, root_session=str(tmp_path / "new.jsonl"))
        (bound_at,) = con.execute("SELECT created_at FROM events WHERE task_id=? AND kind=?",
                                  (run_id, runs._ROOT_SESSION_BOUND)).fetchone()
        assert updated["updated_at"] == bound_at == 1005


def test_legacy_absolute_window_alias_uses_the_same_cutoff(tmp_path, monkeypatch, directory_alias):
    real, alias = directory_alias
    window = _session(real / "panel.jsonl", {"type": "session", "id": "panel"},
                      _call("big", 100, 1, 1_050_000), _call("big", 50, 1, 2_050_000))
    now = 1000
    monkeypatch.setattr(runs, "time", SimpleNamespace(time=lambda: now))
    with closing(tasks.connect(str(home.path("db")))) as con:
        first = runs.create(con, workspace=str(tmp_path), question="First legacy run")
        now = 2000
        other = runs.create(con, workspace=str(tmp_path), question="Second legacy run")
        con.execute("UPDATE research_runs SET root_session=? WHERE id=?", (str(window), first["id"]))
        con.execute("UPDATE research_runs SET root_session=? WHERE id=?", (str(alias / window.name), other["id"]))
        spent = usage.run_usage(con, runs.get(con, first["id"]))
        assert spent["estimated_panel_usage"]["tokens"] == 100


def test_receipt_lookup_retains_evidence_when_a_second_candidate_is_damaged(tmp_path):
    from misaka.core import session_catalog

    header = {"type": "session", "id": "shared"}
    call = {**_call("fixture", 100, 0, 1_000), "id": "accepted"}
    call["message"]["requestId"] = "request"
    good = _session(tmp_path / "good.jsonl", header, call)
    damaged = _session(tmp_path / "damaged.jsonl", header)
    with damaged.open("ab") as stream:
        stream.write(b'{"type":"message",')
    index = session_catalog._index_dir()
    index.mkdir(parents=True, exist_ok=True)
    for path in (good, damaged):
        session_catalog._write(index / f"{path.stem}.json", {"id": "shared", "path": str(path)})
    receipt = {"event": "model_receipt_saved", "session_id": "shared", "entry_id": "accepted", "request_id": "request"}
    assert usage._trace_windows([receipt], set()) == ({usage._path_key(good)}, True)


@pytest.mark.parametrize("evidence", ["journal", "catalog", "ambiguous", "duplicate-current-id"])
def test_legacy_rebound_windows_are_recovered_only_with_evidence(tmp_path, monkeypatch, capsys, evidence):
    from misaka.cli import app
    from misaka.core import session_catalog

    board = home.path("db")
    monkeypatch.setattr(app, "current_config", lambda: {"db": str(board), "token_cap": 0})
    with closing(tasks.connect(str(board))) as con:
        monkeypatch.setattr(app.db, "connect", lambda _path: con)
        run = runs.create(con, workspace=str(tmp_path), question="Window changed before upgrading")
        run_id = run["id"]
        stamp = run["created_at"] * 1000
        directory = Path(sessions.role_dir())
        first_call = {**_call("big", 100, 0, stamp), "id": "accepted"}
        if evidence == "journal":
            first_call["usageRunId"] = run_id
        else:
            first_call["message"]["requestId"] = "first"
        a = _session(directory / "old.jsonl", {"type": "session", "id": "old-window"}, first_call)
        current_id = "old-window" if evidence == "duplicate-current-id" else "current-window"
        b = _session(directory / "current.jsonl", {"type": "session", "id": current_id},
                     {**_call("big", 50, 0, stamp), "usageRunId": run_id}, _call("big", 900, 0, stamp))
        con.execute("UPDATE research_runs SET root_session=? WHERE id=?", (home.stored(b), run_id))
        if evidence == "journal":
            runs.record_action(con, run, runs.root(con, run_id), "plan", {},
                               session_file=str(a), tool_call_id="plan")
        else:
            with trace_scope(run_id, False):
                scope_event("model_receipt_saved", session_id="old-window", entry_id="accepted", request_id="first")
                scope_event("model_receipt_saved", session_id="unmatched-window", entry_id="lost", request_id="second")
            index = session_catalog._index_dir()
            index.mkdir(parents=True)
            session_catalog._write(index / "one.json", {"id": "old-window", "path": str(a)})
            session_catalog._write(index / "stale.json", {"id": "unmatched-window", "path": str(b)})
            if evidence == "ambiguous":
                copy = _session(directory / "copy.jsonl", {"type": "session", "id": "old-window"}, first_call)
                session_catalog._write(index / "two.json", {"id": "old-window", "path": str(copy)})

        glob = Path.glob
        catalog_scans = []
        read_usage_file = usage._read_usage_file
        loaded = []

        def count_catalog(path, *args, **kwargs):
            if path == session_catalog._index_dir():
                catalog_scans.append(path)
            return glob(path, *args, **kwargs)

        def count_usage_read(path):
            loaded.append(path)
            return read_usage_file(path)

        monkeypatch.setattr(Path, "glob", count_catalog)
        monkeypatch.setattr(usage, "_read_usage_file", count_usage_read)
        app._cmd_usage(SimpleNamespace(run=run_id, limit=10))
        out = capsys.readouterr().out
        total = next(line for line in out.splitlines() if line.strip().startswith("total"))
        assert total.split()[3] == ("50" if evidence == "ambiguous" else "150")
        assert "Window history incomplete" in out and "Unassigned panel usage" in out
        assert len(catalog_scans) == (0 if evidence == "journal" else 1)
        assert len(loaded) == len(set(loaded))


@pytest.mark.parametrize("other_started", [0, 3])
def test_legacy_estimate_does_not_absorb_a_run_that_left_the_window(tmp_path, monkeypatch, other_started):
    now = 0
    monkeypatch.setattr(runs, "time", SimpleNamespace(time=lambda: now))
    with closing(tasks.connect(str(home.path("db")))) as con:
        a = Path(sessions.role_dir()) / "shared.jsonl"
        b = _session(Path(sessions.role_dir()) / "new.jsonl", {"type": "session", "id": "new-window"})
        ids = {}
        for now, question in sorted(((1, "Run to report"), (other_started, "Run that leaves"))):
            run_id = runs.create(con, workspace=str(tmp_path), question=question)["id"]
            runs.set_state(con, run_id, root_session=str(a))
            ids[question] = run_id
        first, second = ids["Run to report"], ids["Run that leaves"]
        alias = str(a).swapcase() if a.drive else str(a)
        con.execute("UPDATE research_runs SET root_session=? WHERE id=?", (alias, second))
        now = 4
        runs.set_state(con, second, root_session=str(b))
        _session(a, {"type": "session", "id": "shared-window"},
                 _call("big", 100, 0, 2_500), _call("big", 50, 0, 3_500),
                 _call("big", 40, 0, 4_500), _call("big", 60, 0, 5_500),
                 {**_call("big", 20, 0, 2_500), "usageRunId": first})
        now = 6
        after = usage.run_usage(con, runs.get(con, first))
        assert after["total"]["tokens"] == (180 if other_started == 3 else 80)
        assert after["unassigned_panel_usage"]["tokens"] == (90 if other_started == 3 else 190)


@pytest.mark.parametrize(("first_binding", "prior", "expected"), [
    (5000, "recorded", 100), (1080, "recorded", 0), (5000, "legacy", 0),
    (5000, "unbound", 100), (5000, "bad_clock", 0), (5000, "bad_updated", 0),
])
def test_usage_cli_ignores_nonoverlapping_window_adoption(tmp_path, monkeypatch, capsys, first_binding, prior, expected):
    from misaka.cli import app

    board = home.path("db")
    monkeypatch.setattr(app, "current_config", lambda: {"db": str(board)})
    now = 900
    monkeypatch.setattr(runs, "time", SimpleNamespace(time=lambda: now))
    with closing(tasks.connect(str(board))) as con:
        monkeypatch.setattr(app.db, "connect", lambda _path: con)
        other = runs.create(con, workspace=str(tmp_path), question="Adopt A later")["id"]
        a = _session(Path(sessions.role_dir()) / "a.jsonl", {"type": "session", "id": "a"},
                     _call("fixture", 100, 0, 1_080_000 if first_binding == 1080 else 1_001_000))
        b = _session(Path(sessions.role_dir()) / "b.jsonl", {"type": "session", "id": "b"})
        if prior in {"recorded", "bad_clock", "bad_updated"}:
            runs.set_state(con, other, root_session=str(b))
        elif prior == "legacy":
            con.execute("UPDATE research_runs SET root_session=? WHERE id=?", (home.stored(b), other))
        now = 1000
        first = runs.create(con, workspace=str(tmp_path), question="Finished in window A")["id"]
        runs.set_state(con, first, root_session=str(a))
        now = 1020
        runs.set_state(con, first, status="stopped")
        now = first_binding
        runs.set_state(con, other, root_session=str(a))
        if prior == "bad_clock":
            con.execute("UPDATE events SET created_at=899 WHERE task_id=? AND kind=? AND created_at=?",
                        (other, runs._ROOT_SESSION_BOUND, first_binding))
        if prior == "bad_updated":
            con.execute("UPDATE research_runs SET updated_at='unknown' WHERE id=?", (other,))

        app._cmd_usage(SimpleNamespace(run=first, limit=10))
        output = capsys.readouterr().out
        total = next(line for line in output.splitlines() if line.strip().startswith("total"))
        assert total.split()[3:5] == [str(expected), "tokens"]
        assert ("Estimated panel usage (included)" in output) is bool(expected)
        assert ("Unassigned panel usage (outside total)" in output) is not bool(expected)


@pytest.mark.parametrize("damage,expected", [
    ("empty", 50), ("truncated", 150), ("bad_line", 175), ("bad_usage", 150),
    ("bad_token_type", 150), ("negative_tokens", 150),
])
def test_usage_cli_flags_incomplete_prior_window(tmp_path, monkeypatch, capsys, damage, expected):
    from misaka.cli import app

    board = home.path("db")
    monkeypatch.setattr(app, "current_config", lambda: {"db": str(board)})
    with closing(tasks.connect(str(board))) as con:
        monkeypatch.setattr(app.db, "connect", lambda _path: con)
        run = runs.create(con, workspace=str(tmp_path), question="Read the surviving requests")
        run_id = run["id"]
        directory = Path(sessions.role_dir())
        stamp = run["created_at"] * 1000
        first = {**_call("fixture", 100, 0, stamp), "usageRunId": run_id}
        a = _session(directory / "a.jsonl", {"type": "session", "id": "a"})
        with a.open("ab") as stream:
            if damage == "bad_line":
                stream.write(b"{\xff}\n")
            invalid_usages = {"bad_usage": {"input": "damaged"},
                              "bad_token_type": {"input": True, "output": 2.7, "totalTokens": 3},
                              "negative_tokens": {"input": -1, "totalTokens": -1}}
            if damage in invalid_usages:
                invalid = {**first, "message": {**first["message"], "usage": invalid_usages[damage]}}
                stream.write((json.dumps(invalid) + "\n").encode())
            if damage != "empty":
                stream.write((json.dumps(first) + "\n").encode())
            if damage == "truncated":
                stream.write(b'{"type":"message",')
        if damage == "empty":
            a.write_bytes(b"")
        if damage == "bad_line":
            child = Path(sessions.subagent_session_dir("a", a)) / "child.jsonl"
            _session(child, {"type": "session", "id": "child"},
                     {**_call("fixture", 25, 0, stamp), "usageRunId": run_id})
        b = _session(directory / "b.jsonl", {"type": "session", "id": "b"},
                     {**_call("fixture", 50, 0, stamp), "usageRunId": run_id})
        runs.set_state(con, run_id, root_session=str(a))
        runs.set_state(con, run_id, root_session=str(b))

        app._cmd_usage(SimpleNamespace(run=run_id, limit=10))
        output = capsys.readouterr().out
        total = next(line for line in output.splitlines() if line.strip().startswith("total"))
        assert total.split()[3:5] == [str(expected), "tokens"]
        assert "Window history incomplete" in output
        assert "Charged to the token cap (research.token_cap): 0 tokens." in output


def test_usage_cli_distinguishes_transcript_totals_from_validated_provider_usage(tmp_path, monkeypatch, capsys):
    from misaka.cli import app, trace

    board = home.path("db")
    monkeypatch.setattr(app, "current_config", lambda: {"db": str(board)})
    with closing(tasks.connect(str(board))) as con:
        monkeypatch.setattr(app.db, "connect", lambda _path: con)
        run = runs.create(con, workspace=str(tmp_path), question="Compare recorded and reported usage")
        run_id = run["id"]
        session = SessionManager.create(str(tmp_path), str(sessions.role_dir()))
        with trace_scope(run_id, False):
            for request, tokens in (("reported", 50), ("conflicting", 10)):
                session.appendMessage({**_call("fixture", tokens, 0, run["created_at"] * 1000)["message"],
                                       "requestId": request})
                scope_event("provider_usage", request_id=request, status=request,
                            raw={"total_tokens": tokens if request == "reported" else 2},
                            normalized={"input": tokens // 2, "output": tokens // 2, "cache_read": 0,
                                        "cache_write": 0, "total": tokens},
                            issues=[] if request == "reported" else ["total_tokens_mismatch"])
        runs.set_state(con, run_id, root_session=session.sessionFile)
        budget.settle_request(con, None, run_id, 0, 60)

        app._cmd_usage(SimpleNamespace(run=run_id, limit=10))
        output = capsys.readouterr().out
        total = next(line for line in output.splitlines() if line.strip().startswith("total"))
        assert total.split()[3:5] == ["60", "tokens"]
        assert "normalized session records" in output and f"misaka trace show {run_id}" in output
        assert "Charged to the token cap (research.token_cap): 60 tokens." in output
        trace.run(["show", run_id, "--json"])
        summary = json.loads(capsys.readouterr().out)["usage"]
        assert summary["reported_tokens"]["total"] == 50 and summary["conflicting"] == 1


@pytest.mark.parametrize("unreadable", [False, True])
def test_missing_prior_window_leaves_a_subtotal_and_separate_budget(spent_run, monkeypatch, unreadable):
    con, run, _child, _tid = spent_run
    past = run["root_session"]
    if not unreadable:
        runs.set_state(con, run["id"], root_session=str(Path(past).with_name("never-saved.jsonl")))
    current = _session(Path(past).with_name("current.jsonl"), {"type": "session", "id": "current-window"},
                       {**_call("big", 50, 0, run["created_at"] * 1000), "usageRunId": run["id"]})
    runs.set_state(con, run["id"], root_session=str(current))
    if unreadable:
        def open_transcript(path, *args, **kwargs):
            if path == usage._path_key(past):
                raise PermissionError("prior transcript is inaccessible")
            return open(path, *args, **kwargs)

        monkeypatch.setattr(usage, "open", open_transcript, raising=False)
    updated = runs.get(con, run["id"])
    budget = {"recorded_tokens": 460, "reserved_tokens": 25, "expired_reservation_tokens": 10}
    out = usage.report(con, updated, board_budget=budget)
    total = next(line for line in out.splitlines() if line.strip().startswith("total"))
    assert total.split()[3] == "340"
    assert "Window history incomplete" in out
    assert "460 tokens" in out and "Active reservations: 25 tokens" in out


@pytest.mark.parametrize("trace_accessible", [True, False])
def test_usage_cli_joins_older_panel_rows_to_trace_requests(tmp_path, monkeypatch, capsys, trace_accessible):
    from misaka.cli import app

    monkeypatch.setattr(repo, "enabled", lambda *a: False)
    board = home.path("db")
    monkeypatch.setattr(app, "current_config", lambda: {"db": str(board), "token_cap": 0})
    with closing(tasks.connect(str(board))) as con:
        first = runs.create(con, workspace=str(tmp_path), question="Older panel history")
        second = runs.create(con, workspace=str(tmp_path), question="Another older run")
        ordinary = "ordinary-window"
        stamp = first["created_at"]
        rows = []
        for owner, request, tokens, offset in (
            (first["id"], "first", 100, 100), (second["id"], "second", 50, 200),
            (ordinary, "ordinary", 500, 4000), (first["id"], "resumed", 20, 120_000),
            (None, "unlinked", 40, 500), (None, None, 10, 550),
        ):
            row = _call("big", tokens, 0, stamp * 1000 + offset)
            if request:
                row["message"]["requestId"] = request
            if owner:
                with trace_scope(owner, False):
                    scope_event("model_started", request_id=request)
            rows.append(row)
        window = _session(Path(sessions.role_dir()) / "old-window.jsonl", {"type": "session", "id": ordinary}, *rows)
        for run in (first, second):
            runs.set_state(con, run["id"], root_session=str(window))
            con.execute("UPDATE research_runs SET status='done',created_at=?,updated_at=? WHERE id=?",
                        (stamp, stamp, run["id"]))

        if not trace_accessible:
            real_scandir = os.scandir
            first_trace = home.path("runtime_traces") / first["id"]

            def inaccessible_directory(path):
                if isinstance(path, (str, os.PathLike)) and Path(path) == first_trace:
                    raise PermissionError("trace directory unreadable")
                return real_scandir(path)

            monkeypatch.setattr(os, "scandir", inaccessible_directory)

        monkeypatch.setattr(app.db, "connect", lambda path: con)
        for run, expected in ((first, "120" if trace_accessible else "0"), (second, "50")):
            app._cmd_usage(SimpleNamespace(run=run["id"], limit=10))
            out = capsys.readouterr().out
            summary = next(line for line in out.splitlines() if line.strip().startswith("total"))
            assert summary.split()[3:5] == [expected, "tokens"]
            unknown = next(line for line in out.splitlines() if "Unassigned panel usage" in line)
            assert unknown.split()[7:9] == ["50" if trace_accessible else "150", "tokens"]
        app._cmd_usage(SimpleNamespace(run=None, limit=10))
        assert capsys.readouterr().out.count("excludes unassigned panel usage") == 2


def test_reopened_dedicated_session_does_not_charge_other_usage(tmp_path, monkeypatch):
    monkeypatch.setattr(repo, "enabled", lambda *a: False)
    monkeypatch.setattr(sessions, "sessions_root", lambda: str(tmp_path / "sessions"))
    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        run = runs.create(con, workspace=str(tmp_path), question="Dedicated session")
        session = SessionManager.create(str(tmp_path), runs.session_dir(run, "root-lo"))
        stamp = run["created_at"] * 1000
        with trace_scope(run["id"], False):
            session.appendMessage(_call("big", 100, 1, stamp)["message"])
        runs.set_state(con, run["id"], root_session=session.getSessionFile())

        reopened = SessionManager.open(session.getSessionFile())
        reopened.appendMessage(_call("big", 200, 2, stamp)["message"])
        with trace_scope("other-run", False):
            reopened.appendMessage(_call("big", 300, 3, stamp)["message"])

        run = runs.get(con, run["id"])
        assert usage.run_usage(con, run)["total"]["tokens"] == 100
        assert usage.session_usage([session.getSessionFile()])["tokens"] == 600


def test_budget_summary_separates_reservations_without_changing_the_board(spent_run):
    con, run, _, tid = spent_run
    now = int(time.time())
    for lease_id, task_id, tokens, expires in (
        ("active", tid, 70, now + 60), ("expired", tid, 90, now - 60), ("unrelated", "not-this-run", 99, now + 60),
    ):
        con.execute("INSERT INTO budget_reservations VALUES (?,?,1,?,?,?)", (lease_id, task_id, tokens, expires, now))
    budget.settle_request(con, None, run["id"], 0, 100)
    tasks.add_event(con, tid, "budget_usage", {"totalTokens": 30, "expiredReservation": "old"})

    database = Path(con.execute("PRAGMA database_list").fetchone()[2])
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as readonly:
        readonly.row_factory = sqlite3.Row
        assert usage.research_budget_summary(readonly, run["id"]) == {
            "recorded_tokens": 130, "reserved_tokens": 70, "expired_reservation_tokens": 30,
        }
    assert con.execute("SELECT COUNT(*) FROM budget_reservations").fetchone()[0] == 3


def test_budget_summary_uses_one_snapshot_during_reservation_settlement(spent_run, monkeypatch):
    writer, run, _, _ = spent_run
    now = int(time.time())
    writer.execute("INSERT INTO budget_reservations VALUES ('race',?,1,70,?,?)", (run["id"], now + 60, now))
    database = Path(writer.execute("PRAGMA database_list").fetchone()[2])
    original = budget.spent

    def settle(con, *, task_ids=None):
        # The summary has read the reservation but hasn't read recorded spending.
        assert budget.settle_request(writer, "race", run["id"], 1, 30)
        return original(con, task_ids=task_ids)

    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as readonly:
        readonly.row_factory = sqlite3.Row
        with monkeypatch.context() as patch:
            patch.setattr(budget, "spent", settle)
            assert usage.research_budget_summary(readonly, run["id"]) == {
                "recorded_tokens": 0, "reserved_tokens": 70, "expired_reservation_tokens": 0,
            }
        assert not readonly.in_transaction
        assert usage.research_budget_summary(readonly, run["id"]) == {
            "recorded_tokens": 30, "reserved_tokens": 0, "expired_reservation_tokens": 0,
        }


def test_a_card_whose_session_folder_was_never_stored_is_still_counted(spent_run):
    """A run started from the command line stores no folder on its cards, and the report left them
    out: one such run showed $0.91 of the $2.52 its session files add up to (2026-11-19)."""
    con, run, _child, tid = spent_run
    con.execute("UPDATE tasks SET session_dir=NULL WHERE id=?", (tid,))

    spent = usage.run_usage(con, run)

    assert dict(spent["parts"])[f"card {tid} (Sister 10032)"]["calls"] == 2
    assert spent["total"]["cost"] == 3.25


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
                          _call("big", 100, 1.0, 1_050_000),
                          _call("big", 300, 3.0, 2_000_000),
                          _call("big", 5000, 50.0, 5_050_000))
        for run, status, created, updated in ((first, "stopped", 1000, 1100), (second, "active", 5000, 5100)):
            con.execute("UPDATE research_runs SET root_session=?, status=?, created_at=?, updated_at=? WHERE id=?",
                        (home.stored(window), status, created, updated, run["id"]))
        assert usage.run_usage(con, runs.get(con, first["id"]))["total"]["cost"] == 1.0
        assert usage.run_usage(con, runs.get(con, second["id"]))["total"]["cost"] == 50.0
        con.execute("UPDATE research_runs SET status='failed', updated_at=99999 WHERE id=?", (first["id"],))
        assert usage.run_usage(con, runs.get(con, first["id"]))["total"]["cost"] == 4.0, "never past run B's start"


@pytest.mark.parametrize("status", ["active", "waiting_input", "stopping"])
def test_a_run_still_going_is_counted_to_now(tmp_path, monkeypatch, status):
    monkeypatch.setattr(repo, "enabled", lambda *a: False)
    monkeypatch.setattr(tasks, "task_state_dir", lambda tid: str(tmp_path / "state" / tid))
    monkeypatch.setattr(sessions, "sessions_root", lambda: str(tmp_path / "sessions"))
    now = 1000
    monkeypatch.setattr(runs, "time", SimpleNamespace(time=lambda: now))
    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        runs.init(con)
        run = runs.create(con, workspace=str(tmp_path), question="Run A")
        window = _session(tmp_path / "sessions" / "last-order" / "window.jsonl",
                          _call("big", 100, 1.0, 1_050_000), _call("big", 700, 7.0, 9_000_000))
        runs.set_state(con, run["id"], root_session=str(window))
        now = 1100
        runs.set_state(con, run["id"], status=status)
        now = 9001
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


def test_symlinked_sessions_dir_does_not_double_count_root_lo_usage(tmp_path, monkeypatch, directory_alias):
    monkeypatch.setattr(repo, "enabled", lambda *a: False)
    monkeypatch.setattr(tasks, "task_state_dir", lambda tid: str(tmp_path / "state" / tid))
    real_sessions, link_sessions = directory_alias
    monkeypatch.setattr(sessions, "sessions_root", lambda: str(link_sessions))

    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        runs.init(con)
        run = runs.create(con, workspace=str(tmp_path), question="Symlinked sessions")
        run_id = run["id"]
        root_dir = link_sessions / "research" / f"{run_id}--root-lo"
        root_dir.mkdir(parents=True)
        sess = _session(
            root_dir / "sess.jsonl",
            {"type": "session", "id": "s_root", "version": 3},
            {"type": "usage", "kind": "cache_warm", "provider": "p", "model": "big", "usageRunId": run_id,
             "usage": {"input": 60, "output": 40, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 100,
                       "cost": {"total": 0.1}},
             "timestamp": "2026-11-25T00:00:00+00:00"},
        )
        if not link_sessions.samefile(real_sessions):
            canonical_file = real_sessions / sess.relative_to(link_sessions)
            canonical_file.parent.mkdir(parents=True)
            canonical_file.write_bytes(sess.read_bytes())
        runs.set_state(con, run_id, root_session=str(sess))
        read_usage_file = usage._read_usage_file
        reads = []

        def count_read(path):
            reads.append(path)
            return read_usage_file(path)

        monkeypatch.setattr(usage, "_read_usage_file", count_read)
        spent = usage.run_usage(con, runs.get(con, run_id))
        assert reads == [usage._path_key(sess)]
        assert spent["total"]["tokens"] == 100
        assert [(label, part["tokens"]) for label, part in spent["parts"]] == [("root Last Order", 100)]


def test_shared_panel_keeps_only_the_overlapping_legacy_usage_unassigned(tmp_path, monkeypatch, capsys):
    from misaka.cli import app

    monkeypatch.setattr(repo, "enabled", lambda *a: False)
    monkeypatch.setattr(tasks, "task_state_dir", lambda tid: str(tmp_path / "state" / tid))
    monkeypatch.setattr(sessions, "sessions_root", lambda: str(tmp_path / "sessions"))
    board = tmp_path / "board.db"
    monkeypatch.setattr(app, "current_config", lambda: {"db": str(board)})
    now = 1000
    monkeypatch.setattr(runs, "time", SimpleNamespace(time=lambda: now))
    with closing(tasks.connect(str(board))) as con:
        earlier = runs.create(con, workspace=str(tmp_path), question="Earlier run")
        now = 2000
        current = runs.create(con, workspace=str(tmp_path), question="Current run")
        window = _session(
            tmp_path / "sessions" / "last-order" / "shared.jsonl",
            {"type": "session", "id": "s_shared", "version": 3},
            _call("big", 100, 1.0, 2_050_000),
            _call("big", 50, 0.5, 2_150_000),
        )
        runs.set_state(con, current["id"], root_session=str(window))
        now = 2100
        runs.set_state(con, earlier["id"], root_session=str(window))
        now = 2200
        runs.set_state(con, current["id"], status="stopped")
        now = 2500
        runs.set_state(con, earlier["id"], status="stopped")

    app.main(["usage", "--run", current["id"]])
    lines = capsys.readouterr().out.splitlines()
    assert next(line for line in lines if "Estimated panel usage" in line).split()[-3:] == ["100", "tokens", "$1.00"]
    assert next(line for line in lines if "Unassigned panel usage" in line).split()[-3:] == ["50", "tokens", "$0.50"]


def test_later_run_adopting_a_window_after_stop_does_not_cut_off_its_usage(tmp_path, monkeypatch, capsys):
    from misaka.cli import app

    board = tmp_path / "board.db"
    monkeypatch.setattr(app, "current_config", lambda: {"db": str(board)})
    now = 1000
    monkeypatch.setattr(runs, "time", SimpleNamespace(time=lambda: now))
    a = _session(Path(sessions.role_dir()) / "a.jsonl", {"type": "session", "id": "a"},
                 _call("big", 20, 0.2, 1_020_000), _call("big", 100, 1.0, 1_070_000))
    b = _session(Path(sessions.role_dir()) / "b.jsonl", {"type": "session", "id": "b"})
    with closing(tasks.connect(str(board))) as con:
        first = runs.create(con, workspace=str(tmp_path), question="First window A")
        runs.set_state(con, first["id"], root_session=str(a))
        now = 1050
        other = runs.create(con, workspace=str(tmp_path), question="Start in window B")
        runs.set_state(con, other["id"], root_session=str(b))
        now = 1080
        runs.set_state(con, first["id"], status="stopped")
        now = 1300
        runs.set_state(con, other["id"], root_session=str(a))

    app.main(["usage", "--run", first["id"]])
    lines = capsys.readouterr().out.splitlines()
    assert next(line for line in lines if line.strip().startswith("total")).split()[-3:] == ["120", "tokens", "$1.20"]
    assert not any("Unassigned panel usage" in line for line in lines)


def test_legacy_entry_during_its_first_binding_second_is_unassigned(tmp_path, monkeypatch):
    now = 90
    monkeypatch.setattr(runs, "time", SimpleNamespace(time=lambda: now))
    with closing(tasks.connect(str(home.path("db")))) as con:
        run = runs.create(con, workspace=str(tmp_path), question="Bind during a busy second")
        panel = _session(Path(sessions.role_dir()) / "panel.jsonl", {"type": "session", "id": "panel"},
                         _call("big", 100, 1.0, 100_100), _call("big", 50, 0.5, 101_100),
                         {**_call("big", 5, 0.05, 100_950), "usageRunId": run["id"]})
        now = 100.9
        runs.set_state(con, run["id"], root_session=str(panel))
        now = 102
        report = usage.run_usage(con, runs.get(con, run["id"]))
        assert report["estimated_panel_usage"]["tokens"] == 50
        assert report["unassigned_panel_usage"]["tokens"] == 100
        assert report["total"]["tokens"] == 55


def test_root_lo_and_panel_window_merge_under_single_root_last_order_row(tmp_path, monkeypatch, capsys):
    from misaka.cli import app

    monkeypatch.setattr(repo, "enabled", lambda *a: False)
    monkeypatch.setattr(tasks, "task_state_dir", lambda tid: str(tmp_path / "state" / tid))
    monkeypatch.setattr(sessions, "sessions_root", lambda: str(tmp_path / "sessions"))
    board = tmp_path / "board.db"
    monkeypatch.setattr(app, "current_config", lambda: {"db": str(board)})
    with closing(tasks.connect(str(board))) as con:
        runs.init(con)
        run = runs.create(con, workspace=str(tmp_path), question="CLI then panel")
        run_id = run["id"]
        cli_sess = _session(
            tmp_path / "sessions" / "research" / f"{run_id}--root-lo" / "cli.jsonl",
            {"type": "session", "id": "s_cli", "version": 3},
            {**_call("big", 100, 0.012, 1_000_000), "usageRunId": run_id},
        )
        panel_sess = _session(
            tmp_path / "sessions" / "last-order" / "panel.jsonl",
            {"type": "session", "id": "s_panel", "version": 3},
            {**_call("big", 50, 0.001, 2_000_000), "usageRunId": run_id},
        )
        _session(tmp_path / "sessions" / "research" / f"{run_id}--node-before-root" / "node.jsonl",
                 _call("big", 1, 0.002, 1_500_000))
        runs.set_state(con, run_id, root_session=str(cli_sess))
        runs.set_state(con, run_id, root_session=str(panel_sess))

    app.main(["usage", "--run", run_id])
    report = capsys.readouterr().out.splitlines()
    root_rows = [line.split() for line in report if line.strip().startswith("root Last Order")]
    assert [row[-3:] for row in root_rows] == [["150", "tokens", "$0.01"]]
    for prefix in ("total", "p/big"):
        assert next(line for line in report if line.strip().startswith(prefix)).split()[-3:] == ["151", "tokens", "$0.01"]

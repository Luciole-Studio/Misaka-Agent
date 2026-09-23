"""A resident window must not keep its completed runner's database callbacks."""
import asyncio
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from misaka.core.network.wiring.capabilities import SisterCapabilitiesPart
from misaka.core.research import runs, workflow
from misaka.core.research.wiring import node
from misaka.core.session_control import SessionControl


@pytest.mark.parametrize("outcome", ["closed", "waiting", "failed", "cancelled", "replaced", "close_error", "release_error"])
async def test_runner_restores_only_its_callbacks_and_closes_connection(monkeypatch, outcome):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE research_runs(id, stop_requested, driver_lock, phase, status);
        INSERT INTO research_runs VALUES('run', 0, 'driver', 'active', 'active');
        CREATE TABLE research_branches(id, runner_key, depth, status, last_error, run_id);
        INSERT INTO research_branches VALUES('node', 'key', 1, 'executing', NULL, 'run');
    """)
    session = SimpleNamespace(
        sessionManager=SimpleNamespace(sessionFile="/tmp/unused-fixture.jsonl"),
        moments=SimpleNamespace(parts=[SisterCapabilitiesPart("/tmp")], send_message=lambda *args: None))
    control = SessionControl(session, None)
    previous_check, previous_describe = control.check_active, control.describe
    previous_input = control.on_input
    replacement_check = lambda: None
    replacement_describe = lambda: {"owner": "replacement"}
    session.moments.parts.append(SimpleNamespace(control=control))
    part = node.NodePart("run", "node", "key")
    part.attach(session)

    async def expand(*args, **kwargs):
        control.check_active()
        assert control.describe() == {"run_id": "run", "node": "node", "depth": 1,
                                      "run_phase": "active", "run_status": "active", "node_phase": "executing"}
        con.execute("UPDATE research_branches SET runner_key='other'")
        with pytest.raises(RuntimeError, match="changed owners"):
            control.check_active()
        con.execute("UPDATE research_branches SET runner_key='key'")
        if outcome == "cancelled":
            raise asyncio.CancelledError
        if outcome == "failed":
            raise RuntimeError("fixture failure")
        if outcome == "replaced":
            control.check_active, control.describe = replacement_check, replacement_describe
        if outcome == "waiting":
            return {"questions": ["continue?"]}
        con.execute("UPDATE research_branches SET status='closed'")
        return "closed"

    monkeypatch.setattr(node.task_store, "connect", lambda *_: con)
    monkeypatch.setattr(node, "current_config", dict)
    monkeypatch.setattr(runs, "init", lambda *_: None)
    monkeypatch.setattr(runs, "set_node", lambda *_, **__: None)
    released = []

    def release(*args):
        released.append(args[1:])
        if outcome == "release_error":
            raise RuntimeError("release failure")

    monkeypatch.setattr(runs, "release_runner", release)
    monkeypatch.setattr(workflow, "expand_node", expand)
    if outcome == "close_error":
        @contextmanager
        def broken_snapshot():
            yield
            raise RuntimeError("close failure")
        monkeypatch.setattr(session.moments.parts[0], "snapshot", broken_snapshot)
    try:
        if outcome == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await part.run()
        elif outcome in {"close_error", "release_error"}:
            with pytest.raises(RuntimeError, match="failure"):
                await part.run()
        else:
            await part.run()
        assert released == [("research_branches", "node", "key")]
        assert control.check_active is (replacement_check if outcome == "replaced" else previous_check)
        assert control.describe is (replacement_describe if outcome == "replaced" else previous_describe)
        assert control.on_input is previous_input
        control.check_active()
        control.describe()
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            con.execute("SELECT 1")
    finally:
        con.close()

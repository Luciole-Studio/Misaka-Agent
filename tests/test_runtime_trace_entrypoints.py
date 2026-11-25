"""A diagnostic choice follows the execution that owns it."""

import asyncio
import json
import subprocess
import sys
from contextlib import closing
from types import SimpleNamespace

import pytest

from misaka.cli import app
from misaka.config import home
from misaka.core.platform import cards, tasks
from misaka.core.research import runs, workflow
from misaka.core.research.wiring import research
from misaka.core.runtime_trace import current_scope, trace_scope


@pytest.fixture
def board():
    with closing(tasks.connect(str(home.path("db")))) as con:
        runs.init(con)
        yield con


def trace_rows(run_id):
    return [json.loads(line) for path in (home.path("runtime_traces") / run_id).glob("*.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines()]


def test_panel_passes_diagnostics_to_new_chat_commands(monkeypatch):
    from misaka.ui.panel import panel

    received = []
    monkeypatch.setattr(panel, "launch", lambda **options: received.append(options))
    app._cmd_panel(app._parser().parse_args(["panel", "--diagnostics"]))
    assert received == [{"diagnostics": True}]
    assert panel._chat_argv("--session", "saved", diagnostics=True)[3:] == [
        "chat", "--diagnostics", "--session", "saved",
    ]


def test_diagnostic_panel_handles_a_pane_finishing_during_startup(monkeypatch, tmp_path):
    from misaka.ui.panel import panel

    class StartupReachedLayout(Exception):
        pass

    old_pane = {"id": "old", "title": "Last Order", "alive": True, "cwd": str(tmp_path)}
    new_pane = {**old_pane, "id": "diagnostic"}
    working = {**old_pane, "reported": {"state": "working"}}
    idle = {**old_pane, "reported": {"state": "idle"}}
    listings = iter([[working], [working], [idle, new_pane]])
    sizes = iter([(24, 80)])

    def terminal_size():
        if (size := next(sizes, None)) is not None:
            return size
        raise StartupReachedLayout

    def request(name, options=None):
        if name == "ping":
            return {"panels": []}
        if name == "panes.list":
            return {"panes": next(listings)}
        if name == "pane.create":
            assert "--diagnostics" in options["argv"]
            return {"pane_id": new_pane["id"]}
        pytest.fail(f"Unexpected startup request: {name}")

    monkeypatch.delenv("MISAKA_NET_PANE", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(panel.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(panel.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(panel, "_term_size", terminal_size)
    monkeypatch.setattr(panel.net, "ensure", lambda: None)
    monkeypatch.setattr(panel.net, "CFG", {"net_sock": "fixture"})
    monkeypatch.setattr(panel, "_Sock", lambda _path: SimpleNamespace(request=request))
    with pytest.raises(StartupReachedLayout):
        panel.launch(diagnostics=True)


def test_chat_enables_diagnostics_when_reopening(monkeypatch, tmp_path):
    from misaka.cli import chat, engine
    from misaka.core import wiring

    cwd = str(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(chat.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(chat, "assembly", lambda *_args: (cwd, "unused"))
    monkeypatch.setattr(wiring, "role_session_setup", lambda *_args, **_kw: (
        [], SimpleNamespace(engine_options=dict), {},
    ))
    for key in ("MISAKA_APP_TITLE", "MISAKA_TAGLINE", "MISAKA_INPUT_HISTORY"):
        monkeypatch.setenv(key, "fixture")

    async def engine_main(flags, _opts):
        assert current_scope("chat-fixture").diagnostics
        assert "-c" in flags
        return 0

    monkeypatch.setattr(engine, "main", engine_main)
    with pytest.raises(SystemExit) as exit_info:
        app._cmd_chat(app._parser().parse_args(["chat", "--continue", "--diagnostics"]))
    assert exit_info.value.code == 0


async def test_slash_research_saves_selected_mode(board, monkeypatch, tmp_path):
    monkeypatch.setattr(research, "_con", lambda: board)
    monkeypatch.setattr(research, "_cfg", dict)
    completed = asyncio.Event()

    async def drive(con, _cfg, _spawner, *, run_id, **_kwargs):
        assert runs.diagnostics_enabled(runs.get(con, run_id))
        completed.set()
        return {"reason": "waiting_input"}

    monkeypatch.setattr(workflow, "run", drive)
    part = research.ResearchPart()
    part.attach(SimpleNamespace(moments=SimpleNamespace(send_message=lambda *_args: None)))
    ctx = SimpleNamespace(cwd=str(tmp_path), isIdle=lambda: True,
                          sessionManager=SimpleNamespace(sessionId="fixture"),
                          ui=SimpleNamespace(notify=lambda *_args: None))
    try:
        await part.commands[0].handler("--diagnostics How much was sold?", ctx)
        await asyncio.wait_for(completed.wait(), 3)
    finally:
        await part._cleanup({}, ctx)


def test_cli_research_resume_keeps_mode(board, monkeypatch, tmp_path):
    from misaka.core.research import planner

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(app, "current_config", lambda: {"db": "fixture"})
    monkeypatch.setattr(app.db, "connect", lambda *_args: board)
    monkeypatch.setattr(planner, "sister_catalog", lambda *_args: [{"id": "10032"}])
    monkeypatch.setattr(cards, "init_project", lambda *_args, **_kwargs: None)
    calls = []

    async def drive(_con, _cfg, _spawner, _run, *, resume, **_kwargs):
        scope = current_scope()
        calls.append((scope.run_id, scope.diagnostics, resume))
        return {"reason": "waiting_input"}

    monkeypatch.setattr(workflow, "_drive_run", drive)
    parser = app._parser()
    app._cmd_research(parser.parse_args(["research", "--diagnostics", "How much was sold?"]))
    run_id = calls[0][0]
    app._cmd_research(parser.parse_args(["research", "--resume", run_id]))
    assert calls == [(run_id, True, False), (run_id, True, True)]
    rows = trace_rows(run_id)
    assert [row["mode"] for row in rows] == ["diagnostic"] * 4
    assert [row["status"] for row in rows] == ["started", "waiting_input", "resumed", "waiting_input"]
    with pytest.raises(SystemExit, match="starting a new Research run"):
        app._cmd_research(parser.parse_args(["research", "--diagnostics", "--resume", run_id]))


def test_native_card_subprocess_uses_persisted_mode_and_attempt(board, monkeypatch, tmp_path):
    from misaka.core.platform import repo

    monkeypatch.setattr(repo, "enabled", lambda *_args: False)
    run = runs.create(board, workspace=tmp_path, question="How much was sold?", limits={"diagnostics": True})
    node = runs.root(board, run["id"])
    card_id = cards.create(board, tmp_path, "Verify the total", "Check the sum", "10032",
                           after_row=lambda tid: runs.link_task(board, run["id"], tid, kind="investigate", node=node))
    assert tasks.claim(board, card_id, "local-test", generation=1)
    script = """
import asyncio
import os
import sys
from misaka.cli import subagent_child
from misaka.config import home
from misaka.core.runtime_trace import current_scope, scope_event
from misaka.core.subagent import child

async def check_trace(parent_watch):
    assert current_scope().diagnostics
    scope_event('node_test', status='finished')
    parent_watch.cancel()
    await asyncio.gather(parent_watch, return_exceptions=True)
    return 0

os.environ['MISAKA_SISTER_OWNER_TASK_ID'] = sys.argv[1]
os.environ['MISAKA_SISTER_OWNER_DB'] = str(home.path('db'))
child._attach_durable_sister_owner = lambda: (True, None)
child._run_attached = check_trace
sys.exit(subagent_child.main())
"""
    subprocess.run([sys.executable, "-c", script, card_id], check=True, timeout=20, capture_output=True, text=True)
    rows = trace_rows(run["id"])
    assert [row["event"] for row in rows] == ["card_started", "node_test", "card_finished"]
    assert {(row["mode"], row["card_id"], row["generation"], row["card_attempt"]) for row in rows} == {
        ("diagnostic", card_id, 1, 1),
    }
    assert len({row["segment"] for row in rows}) == 1


def test_panel_card_trace_uses_the_loaded_owner_and_legacy_mode(board, monkeypatch, tmp_path):
    from misaka.core.network import worker
    from misaka.core.platform import repo

    monkeypatch.setattr(repo, "enabled", lambda *_args: False)
    run = runs.create(board, workspace=tmp_path, question="Which sources support the total?")
    node = runs.root(board, run["id"])
    card_id = cards.create(board, tmp_path, "Verify the total", "Check the sum", "10032",
                           after_row=lambda tid: runs.link_task(board, run["id"], tid, kind="investigate", node=node))
    assert tasks.claim(board, card_id, "panel-test", generation=1)
    row = tasks.get(board, card_id)
    launch_task = {**dict(row), **worker.card_extras(board, row, include_materials=False)}
    assert launch_task["_owner"] == "research"

    with trace_scope("window-fixture", True):
        window = current_scope()
        with worker.load_card_trace(str(home.path("db")), launch_task):
            scope = current_scope()
            assert (scope.run_id, scope.diagnostics, scope.card_id) == (run["id"], False, card_id)
        assert current_scope() == window

    rows = trace_rows(run["id"])
    assert [row["event"] for row in rows] == ["card_started", "card_finished"]
    assert {(row["mode"], row["generation"], row["card_attempt"]) for row in rows} == {("default", 1, 1)}


async def test_legacy_research_uses_default_inside_diagnostic_chat(board, monkeypatch, tmp_path):
    run = runs.create(board, workspace=tmp_path, question="Ordinary Research?", limits={})

    async def drive(*_args, **_kwargs):
        assert current_scope().run_id == run["id"]
        assert not current_scope().diagnostics
        return {"reason": "waiting_input"}

    monkeypatch.setattr(workflow, "_drive_run", drive)
    with trace_scope("chat-fixture", True):
        await workflow.run(board, {}, None, run_id=run["id"])
        assert current_scope().run_id == "chat-fixture"
        assert current_scope().diagnostics

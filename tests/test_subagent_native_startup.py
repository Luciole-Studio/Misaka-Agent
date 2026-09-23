"""Native registration -> CLI child -> HTTP fixture -> persisted resume.

Only the model endpoint is a fixture; session opening and JSONL IPC are real.
"""
import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from misaka.config import home


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith(("MISAKA_", "HERMES_", "LCM_", "PI_")) or key.endswith(("_API_KEY", "_TOKEN")):
            monkeypatch.delenv(key, raising=False)
    for key, leaf in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"),
                      ("XDG_CACHE_HOME", "cache"), ("XDG_DATA_HOME", "data"),
                      ("HERMES_HOME", "hermes")):
        (tmp_path / leaf).mkdir()
        monkeypatch.setenv(key, str(tmp_path / leaf))
    monkeypatch.setenv(home.ENV_HOME, str(tmp_path))
    home.path("models").parent.mkdir(parents=True, exist_ok=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    profile = home.path("profiles_root") / "10032"
    profile.mkdir(parents=True)
    (profile / "SOUL.md").write_text("Fixture Sister.\n")
    (home.path("roles_root") / "MISAKA.md").write_text("Fixture shared identity.\n")
    # Prevent editable installs from sending children to a different checkout.
    guard = tmp_path / "python"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(
        "import sys\n"
        "def audit(event, args):\n"
        "    if event == 'socket.connect' and isinstance(args[1], tuple) and args[1][0] != '127.0.0.1':\n"
        "        raise RuntimeError('test forbids external network connections')\n"
        "sys.addaudithook(audit)\n"
    )
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join((str(guard), str(Path(__file__).resolve().parents[1]))))
    return tmp_path


@pytest.fixture
def endpoint(isolated, request):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            events = [
                {"type": "message_start", "message": {
                    "id": "fixture-message", "type": "message", "role": "assistant",
                    "model": "fixture-model", "content": [], "stop_reason": None,
                    "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 0}}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Fixture answer."}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                 "usage": {"output_tokens": 3}},
                {"type": "message_stop"},
            ]
            write = getattr(request, "param", None)
            if write and len(requests) == 1:
                events[1:4] = [
                    {"type": "content_block_start", "index": 0, "content_block": {
                        "type": "tool_use", "id": "fixture-write", "name": "write", "input": {}}},
                    {"type": "content_block_delta", "index": 0, "delta": {
                        "type": "input_json_delta", "partial_json": json.dumps({"path": write, "content": "Written by child."})}},
                    {"type": "content_block_stop", "index": 0},
                ]
                events[4]["delta"]["stop_reason"] = "tool_use"
            body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    home.path("models").write_text(json.dumps({"providers": {"fixture": {
        "baseUrl": f"http://127.0.0.1:{server.server_port}", "api": "anthropic-messages",
        "apiKey": "fixture-not-a-real-key", "models": [{"id": "fixture-model", "name": "Fixture",
        "reasoning": False, "input": ["text"], "contextWindow": 200000, "maxTokens": 1024,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}]}}}))
    try:
        yield requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def host(root):
    from misaka.core.session_manager import SessionManager
    from misaka.core.subagent.runtime import RoleContext, SubagentManager

    workspace = str(root / "workspace")
    model = {"provider": "fixture", "id": "fixture-model"}
    session = NS(cwd=workspace, model=model, modelRegistry=NS(
                 getAvailable=lambda: [model], getAll=lambda: [model],
                 find=lambda provider, model_id: model if (provider, model_id) == ("fixture", "fixture-model") else None,
                 hasConfiguredAuth=lambda _model: True),
                 sessionManager=SessionManager.create(workspace, str(root / "sessions")),
                 settingsManager=NS(getGlobalSettings=dict, getProjectSettings=dict),
                 getActiveToolNames=list, isProjectTrusted=lambda: True,
                 # The fork snapshot records the session's effective prompt (pi 0.87: the
                 # agent state's prompt is replayed from the transcript and read-only).
                 systemPrompt="Fixture parent.",
                 agent=NS(state=NS(messages=[], tools=[])))
    manager = SubagentManager(session, RoleContext(
        role="sisters/10032", mcp_role="sisters/10032", workspace=workspace,
        profile_dir=str(home.path("profiles_root") / "10032")))
    return manager, session


async def create(manager, session, kind="explorer", background=False):
    if kind == "fork":
        from misaka.core.subagent.fork import definition
        selected = definition(session)
    else:
        selected = manager.resolve_definition(kind, session.cwd)
    return await manager.create_task(
        definition=selected, description="Native startup regression", prompt="FIRST_MARKER",
        model=None, background=background, name=None, isolation=None, cwd=None,
        tool_call_id="", context=session)


@pytest.mark.parametrize("kind", ["explorer", "general"])
@pytest.mark.parametrize("background", [False, True])
async def test_native_first_turn_and_resume(isolated, endpoint, kind, background):
    from misaka.core.session_manager import SessionManager

    manager, session = host(isolated)
    try:
        task = await create(manager, session, kind, background)
        assert task.transcript.is_file(), "New child needs a durable session before --session opens it"
        original_id = SessionManager.open(str(task.transcript)).getSessionId()
        assert task.transcript.stat().st_mode & 0o777 == 0o600
        assert not task.initial_prompt_sent
        async with asyncio.timeout(20):
            await manager._drive(task, "FIRST_MARKER", notify=False)
        assert task.status == "completed", (task.error, task.stderr)
        assert task.turn_count == 1 and task.initial_prompt_sent
        assert "Fixture answer." in str(task.result)
        if background:
            # Also exercise registry rehydration, not just an in-memory task.
            await manager.close()
            manager = type(manager)(session, manager.role_context)
        result = await manager.send_message(task.id, "SECOND_MARKER", context=session)
        assert result["success"]
        task = manager._tasks[task.id]
        async with asyncio.timeout(20):
            await task.runner
        assert task.status == "completed", (task.error, task.stderr)
        assert task.turn_count == 2 and task.process is None
        assert SessionManager.open(str(task.transcript)).getSessionId() == original_id
        assert len(endpoint) == 2
        assert "FIRST_MARKER" in json.dumps(endpoint[1]["messages"])
        assert "SECOND_MARKER" in json.dumps(endpoint[1]["messages"])
        assert "Fixture answer." in json.dumps(endpoint[1]["messages"])
        entries = [json.loads(line) for line in task.transcript.read_text().splitlines()]
        assert sum(e["type"] == "session" for e in entries) == 1
        users = [e for e in entries if e.get("message", {}).get("role") == "user"]
        assert len(users) == 2
    finally:
        await manager.close()


@pytest.mark.parametrize("endpoint", ["outputs/card/report.md"], indirect=True)
async def test_background_child_uses_live_card_output_grant(isolated, endpoint, monkeypatch):
    import sqlite3

    from misaka.core.network.todo import TodoPart
    from misaka.core.platform import tasks

    manager, session = host(isolated)
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE tasks(id TEXT, status TEXT, generation INTEGER, claim_lock TEXT, workspace TEXT, output_dir TEXT)")
    output = Path(session.cwd) / "outputs/card"
    con.execute("INSERT INTO tasks VALUES ('fixture-card', 'running', 1, 'fixture-owner', ?, ?)", (session.cwd, str(output)))
    part = TodoPart.__new__(TodoPart)
    part.task_id, part._con, part._bdb, part.session = "fixture-card", con, tasks, session
    session.moments = NS(parts=[part])
    session.getActiveToolNames = lambda: ["write", "read", "edit"]
    monkeypatch.setenv("MISAKA_SISTER_OWNER_TASK_ID", "fixture-card")
    monkeypatch.setenv("MISAKA_SISTER_OWNER_GENERATION", "1")
    monkeypatch.setenv("MISAKA_SISTER_OWNER_CLAIM_LOCK", "fixture-owner")
    try:
        task = await create(manager, session, "general", True)
        assert manager._child_permission_mode(task) == "default"
        assert not manager._child_can_prompt(task)
        async with asyncio.timeout(20):
            await manager._drive(task, "Write the assigned report.", notify=False)
        assert task.status == "completed", (task.error, task.stderr)
        assert (output / "report.md").read_text() == "Written by child."
        assert len(endpoint) == 2
        entries = [json.loads(line) for line in task.transcript.read_text().splitlines()]
        results = [entry["message"] for entry in entries if entry.get("message", {}).get("role") == "toolResult"]
        assert results and all(result["isError"] is False for result in results)
    finally:
        await manager.close()
        con.close()


async def test_fork_keeps_seed_history(isolated, endpoint):
    manager, session = host(isolated)
    session.agent.state.messages = [{"role": "user", "content": [{"type": "text", "text": "PARENT_MARKER"}], "timestamp": 0}]
    try:
        task = await create(manager, session, "fork", True)
        assert "PARENT_MARKER" in task.transcript.read_text()
        async with asyncio.timeout(20):
            await manager._drive(task, "FORK_MARKER", notify=False)
        assert task.status == "completed", (task.error, task.stderr)
        assert len(endpoint) == 1
        assert "PARENT_MARKER" in json.dumps(endpoint[0]["messages"])
        assert "FORK_MARKER" in json.dumps(endpoint[0]["messages"])
    finally:
        await manager.close()


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
async def test_resume_rejects_lost_history(isolated, damage):
    from misaka.core.session_manager import SessionManager

    manager, session = host(isolated)
    try:
        task = await create(manager, session)
        task.status = "completed"
        task.initial_prompt_sent = True
        task._done.set()
        task._settled.set()
        await task.persist()
        if damage == "missing":
            task.transcript.unlink(missing_ok=True)
        else:
            task.transcript.write_text("not-json\n")
        with pytest.raises(ValueError, match="does not exist|malformed"):
            await manager.send_message(task.id, "resume", context=session)
        with pytest.raises((FileNotFoundError, ValueError)):
            SessionManager.open(str(task.transcript))
        assert task.runner is None
        assert task.status == "completed"
        if damage == "missing":
            assert not task.transcript.exists()
        else:
            assert task.transcript.read_text() == "not-json\n"
    finally:
        await manager.close()


async def test_native_startup_failure_preserves_traceback(isolated):
    manager, session = host(isolated)
    try:
        task = await create(manager, session)
        task.transcript.unlink(missing_ok=True)
        task.stderr.append("STALE_ERROR_FROM_PREVIOUS_TURN")
        async with asyncio.timeout(15):
            await manager._drive(task, "must not be sent", notify=False)
        assert task.status == "failed"
        assert "FileNotFoundError" in task.error
        assert str(task.transcript) in task.error
        assert "exit code 1" in task.error
        assert "STALE_ERROR" not in task.error
        assert json.loads(task.metadata_path.read_text())["error"] == task.error
        assert not task.initial_prompt_sent and task.turn_count == 0 and task.process is None
    finally:
        await manager.close()


async def test_failed_initialization_does_not_register_a_task(isolated, monkeypatch):
    from misaka.core.session_manager import SessionManager

    def fail_write(_self):
        raise OSError("fixture disk full")

    monkeypatch.setattr(SessionManager, "rewrite_file", fail_write)
    manager, session = host(isolated)
    try:
        with pytest.raises(OSError, match="fixture disk full"):
            await create(manager, session)
        assert not manager._tasks and manager._reserved == 0
        assert not list((isolated / "sessions").rglob("*.meta.json"))
    finally:
        await manager.close()


@pytest.mark.parametrize("ready", [False, True])
async def test_abrupt_exit_reports_stage_and_code(isolated, monkeypatch, ready):
    manager, session = host(isolated)
    launch = asyncio.create_subprocess_exec

    async def abruptly_exit(*_args, **kwargs):
        code = "import json,sys\n"
        if ready:
            code += "print(json.dumps({'type':'child_ready'}),flush=True)\nsys.stdin.readline()\n"
        code += "sys.exit(7)\n"
        return await launch(sys.executable, "-c", code, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", abruptly_exit)
    try:
        task = await create(manager, session)
        await manager._drive(task, "question", notify=False)
        assert task.status == "failed"
        assert "exit code 7" in task.error
        assert ("did not accept the prompt" if ready else "before becoming ready") in task.error
    finally:
        await manager.close()


async def test_lcm_child_inherits_resumed_project_not_launcher(isolated, endpoint):
    from misaka.core.session_manager import SessionManager
    manager, parent = host(isolated)
    project = isolated / 'original-project'
    project.mkdir()
    parent.sessionManager.appendCustomEntry('lcm-project', {'workspace': str(project.resolve())})
    try:
        task = await create(manager, parent)
        async with asyncio.timeout(20):
            await manager._drive(task, 'PROJECT_OWNER_MARKER', notify=False)
        assert task.status == 'completed', (task.error, task.stderr)
        child = SessionManager.openInMemory(str(task.transcript))
        marker = next(e for e in child.getEntries() if e.get('customType') == 'lcm-project')
        assert marker['data']['workspace'] == str(project.resolve())
    finally:
        await manager.close()

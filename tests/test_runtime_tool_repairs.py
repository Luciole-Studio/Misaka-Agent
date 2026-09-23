"""Live-run regressions, reproduced only with temporary documents/cards/children."""
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import threading
from io import BytesIO
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from misaka.core.documents.wiring import documents
from misaka.core.platform import processes
from misaka.core.subagent import configuration, policy
from misaka.core.subagent.runtime import RoleContext, _reap_process_tree


@pytest.fixture
def pdf(tmp_path):
    # PIL is a mandatory dependency; no new PDF fixture dependency.
    path = tmp_path / "source.pdf"
    with Image.new("RGB", (300, 100), "white") as page:
        page.save(path, "PDF", save_all=True, append_images=[page, page])
    return path


async def test_parallel_page_renders_and_bad_document_are_isolated(pdf, tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a PDF")
    calls = [asyncio.to_thread(documents._render_page, pdf, page, 300) for page in (1, 2, 3, 1)]
    calls.append(asyncio.to_thread(documents._render_page, bad, 1, 1))
    results = await asyncio.gather(*calls, return_exceptions=True)
    for png, count in results[:-1]:
        assert count == 3 and png.startswith(b"\x89PNG\r\n\x1a\n")
        with Image.open(BytesIO(png)) as image:
            assert max(image.size) == 2000
    assert isinstance(results[-1], RuntimeError)
    assert await asyncio.to_thread(documents._render_page, pdf, 4, 1) == (None, 3)
    assert (await asyncio.to_thread(documents._render_page, pdf, 2, 1))[1] == 3


@pytest.mark.parametrize("failure", ["missing", "non_pdf", "page", "scale", "nan", "render", "crash", "timeout"])
async def test_page_failures_are_real_tool_errors(tmp_path, monkeypatch, failure):
    from misaka.agent.agent_loop import PreparedToolCall, execute_prepared_tool_call
    from misaka.core.wiring import ToolCollector

    source = str(tmp_path / ("source.epub" if failure == "non_pdf" else "source.pdf"))
    monkeypatch.setattr(documents, "_source", lambda *_: (None if failure == "missing" else str(tmp_path), source, "Title"))
    if failure in {"render", "page"}:
        def render(*_args):
            if failure == "render":
                raise RuntimeError("Failed to load page")
            return None, 3
        monkeypatch.setattr(documents, "_render_page", render)
    elif failure in {"crash", "timeout"}:
        def run(*_args, **kwargs):
            assert kwargs["timeout"] > 0
            if failure == "timeout":
                raise subprocess.TimeoutExpired("renderer", kwargs["timeout"])
            return NS(returncode=-11, stdout=b"", stderr=b"native failure")
        monkeypatch.setattr(documents.subprocess, "run", run)
    collector = ToolCollector()
    documents.register(collector)
    definition = next(tool for tool in collector.tools if tool.name == "doc_page_image")
    params = {"doc_id": "fixture", "page": 4, "scale": float("nan") if failure == "nan" else 0 if failure == "scale" else 1}

    async def execute(call_id, args, signal, update):
        return await definition.execute(call_id, args, signal, update, NS(cwd=str(tmp_path)))

    call = NS(id="call", name=definition.name, arguments=params)
    result = await execute_prepared_tool_call(PreparedToolCall("prepared", call, NS(execute=execute), params), None, AsyncMock())
    assert result.isError is True
    assert result.result.content[0].text


@pytest.fixture
def card(tmp_path, monkeypatch):
    from misaka.core.network.todo import TodoPart
    from misaka.core.platform import tasks

    for key in ("MISAKA_SISTER_OWNER_TASK_ID", "MISAKA_SISTER_OWNER_GENERATION", "MISAKA_SISTER_OWNER_CLAIM_LOCK",
                "MISAKA_SUBAGENT_PERMISSION_SETTINGS", "MISAKA_SUBAGENT_PERMISSION_MODE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MISAKA_USAGE_TASK_ID", "fixture-card")
    monkeypatch.setenv("MISAKA_USAGE_GENERATION", "1")
    monkeypatch.setenv("MISAKA_USAGE_CLAIM_LOCK", "fixture-owner")
    monkeypatch.setattr(policy, "_permission_settings_provider", None)
    monkeypatch.setattr(policy, "_permission_broker", None)
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE tasks(id TEXT, status TEXT, generation INTEGER, claim_lock TEXT, workspace TEXT, output_dir TEXT)")
    output = tmp_path / "outputs/card"
    con.execute("INSERT INTO tasks VALUES ('fixture-card', 'running', 1, 'fixture-owner', ?, ?)", (str(tmp_path), str(output)))
    part = TodoPart.__new__(TodoPart)
    part.task_id, part._con, part._bdb = "fixture-card", con, tasks
    session = NS(cwd=str(tmp_path), settingsManager=NS(getGlobalSettings=dict, getProjectSettings=dict),
                 getActiveToolNames=lambda: ["write", "edit", "read"], moments=NS(parts=[part]))
    part.session = session
    try:
        yield session, con, output
    finally:
        con.close()


async def test_card_grant_writes_and_is_revoked_without_heartbeats(card, monkeypatch):
    from misaka.core.tools.write import create_write_tool_definition

    session, con, output = card
    context = RoleContext(role="general", profile_dir="", mcp_role="", workspace=session.cwd,
                          permission_mode="default", permission_can_prompt=False)
    worker = policy.AgentPolicy(context)
    worker.session = NS(cwd=session.cwd)

    async def refresh():
        return {"settings": configuration.permission_settings(session), "mode": "default"}

    monkeypatch.setattr(policy, "_permission_settings_provider", refresh)
    before_changes = con.total_changes
    for path in ("outputs/card/nested/result.md", str(output / "absolute.md")):
        args = {"path": path, "content": "Verified output"}
        assert await worker.before_tool({"toolName": "write", "input": args}) is None
        await create_write_tool_definition(session.cwd).execute("write", args)
    assert (output / "nested/result.md").read_text() == "Verified output"
    assert con.total_changes == before_changes  # grant lookups never renew ownership
    # The next live refresh, including nested children, must drop a stale grant.
    con.execute("UPDATE tasks SET generation=2")
    blocked = await worker.before_tool({"toolName": "write", "input": {"path": str(output / "stale.md")}})
    assert blocked["block"] is True
    assert not (output / "stale.md").exists()


@pytest.mark.parametrize("case", ["outside", "traversal", "symlink", "root_symlink", "protected", "deny", "ask",
                                  "deny_absolute", "deny_relative", "ask_absolute", "ask_relative", "plan", "ordinary",
                                  "no_write", "untrusted", "trust_revoked", "other_cwd", "no_output", "project_output",
                                  "stale_lock", "done", "bash"])
async def test_card_grants_keep_permission_boundaries(card, monkeypatch, case, tmp_path):
    session, con, output = card
    args, name, mode = {"path": "outputs/card/result.md"}, "write", "default"
    if case == "outside":
        args["path"] = str(tmp_path.parent / "outside.md")
    elif case == "traversal":
        args["path"] = "outputs/card/../../elsewhere.md"
    elif case == "symlink":
        output.mkdir(parents=True)
        (output / "link").symlink_to(tmp_path, target_is_directory=True)
        args["path"] = "outputs/card/link/elsewhere.md"
    elif case == "root_symlink":
        output.parent.mkdir()
        destination = tmp_path / "another-card"
        destination.mkdir()
        output.symlink_to(destination, target_is_directory=True)
    elif case == "protected":
        args["path"] = "outputs/card/.env"
    elif case in {"deny", "ask"}:
        session.settingsManager.getGlobalSettings = lambda: {"permissions": {case: ["write"]}}
    elif case.startswith(("deny_", "ask_")):
        behavior, spelling = case.split("_")
        rule = str(output / "result.md") if spelling == "absolute" else args["path"]
        if spelling == "relative":
            args["path"] = str(output / "result.md")
        session.settingsManager.getGlobalSettings = lambda: {"permissions": {behavior: [f"write({rule})"]}}
    elif case == "plan":
        mode = "plan"
    elif case == "ordinary":
        session.moments.parts = []
    elif case == "no_write":
        session.getActiveToolNames = lambda: ["read"]
    elif case == "trust_revoked":
        session.settingsManager.isProjectTrusted = lambda: False
    elif case == "no_output":
        con.execute("UPDATE tasks SET output_dir=NULL")
    elif case == "project_output":
        con.execute("UPDATE tasks SET output_dir=workspace")
    elif case == "stale_lock":
        con.execute("UPDATE tasks SET claim_lock='other-owner'")
    elif case == "done":
        con.execute("UPDATE tasks SET status='done'")
    elif case == "bash":
        name, args = "bash", {"command": "touch outputs/card/result.md"}
    settings = configuration.permission_settings(session, include_project=case != "untrusted",
                                                cwd=str(tmp_path.parent) if case == "other_cwd" else None)
    monkeypatch.setenv("MISAKA_SUBAGENT_PERMISSION_SETTINGS", json.dumps(settings))
    worker = policy.AgentPolicy(RoleContext(role="general", profile_dir="", mcp_role="", workspace=session.cwd,
                                           permission_mode=mode, permission_can_prompt=False))
    worker.session = NS(cwd=session.cwd)
    result = await worker.before_tool({"toolName": name, "input": args})
    assert result and result["block"] is True, (case, result, settings)


@pytest.mark.parametrize("scope", [[], {"bash": ["/tmp"]}, {"write": ["relative"]}, {"write": "/tmp"}])
def test_malformed_inherited_write_scopes_fail_closed(monkeypatch, scope):
    monkeypatch.setenv("MISAKA_SUBAGENT_PERMISSION_SETTINGS", json.dumps([{"writeDirectories": scope}]))
    with pytest.raises(ValueError, match="writeDirectories"):
        configuration.inherited_permissions()


@pytest.mark.parametrize("ignore_term", [False, True])
async def test_asyncio_alone_reaps_owned_child(monkeypatch, caplog, ignore_term):
    code = "import signal,time; " + ("signal.signal(signal.SIGTERM,signal.SIG_IGN); " if ignore_term else "") + "print('ready',flush=True); time.sleep(60)"
    child = await asyncio.create_subprocess_exec(sys.executable, "-c", code, stdout=asyncio.subprocess.PIPE)
    original = processes.psutil.wait_procs

    def wait_procs(children, **kwargs):
        assert all(process.pid != child.pid for process in children), "psutil must not steal asyncio's waitpid"
        return original(children, **kwargs)

    monkeypatch.setattr(processes.psutil, "wait_procs", wait_procs)
    try:
        assert await asyncio.wait_for(child.stdout.readline(), 5) == b"ready\n"
        await asyncio.wait_for(_reap_process_tree(child), 10)
        assert child.returncode == (-9 if ignore_term else -15)
        assert "Unknown child process" not in caplog.text
    finally:
        if child.returncode is None:
            child.kill()
        await child.wait()


@pytest.mark.parametrize("reap_root", [False, True])
def test_tree_cleanup_still_reaps_descendants(monkeypatch, reap_root):
    root = NS(pid=101, suspend=lambda: None, resume=lambda: None, terminate=lambda: None,
              children=lambda **_: [], kill=lambda: None)
    child = NS(**{**vars(root), "pid": 102})
    monkeypatch.setattr(processes, "snapshot", lambda _: [])
    monkeypatch.setattr(processes, "_resolve", lambda _: [root, child])
    waited = []

    def wait_procs(children, **_kwargs):
        waited.extend(process.pid for process in children)
        return children, []

    monkeypatch.setattr(processes.psutil, "wait_procs", wait_procs)
    processes.terminate(root.pid, reap_root=reap_root)
    assert waited == ([101, 102] if reap_root else [102])


async def test_slow_asyncio_watcher_does_not_lose_child_status(monkeypatch, caplog):
    # Reproduce the race deterministically: let psutil finish first, without
    # changing either wait implementation or touching any non-fixture PID.
    release = threading.Event()
    waitpid, wait_procs = os.waitpid, processes.psutil.wait_procs

    def delayed_waitpid(pid, options):
        if threading.current_thread().name.startswith("asyncio-waitpid"):
            release.wait(5)
        return waitpid(pid, options)

    def tree_wait(children, **kwargs):
        try:
            return wait_procs(children, **kwargs)
        finally:
            release.set()

    monkeypatch.setattr(os, "waitpid", delayed_waitpid)
    monkeypatch.setattr(processes.psutil, "wait_procs", tree_wait)
    child = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; print('ready',flush=True); time.sleep(60)",
                                                  stdout=asyncio.subprocess.PIPE)
    try:
        assert await asyncio.wait_for(child.stdout.readline(), 5) == b"ready\n"
        await asyncio.wait_for(_reap_process_tree(child), 10)
        assert child.returncode == -15
        assert "Unknown child process" not in caplog.text
    finally:
        release.set()
        if child.returncode is None:
            child.kill()
        await child.wait()

"""Opt-in diagnostic records follow actual tool ownership, not inferred content quality."""

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from webconf import write_web

from misaka.config import home
from misaka.config.product import CFG
from misaka.core.session_manager import SessionManager
from misaka.core.web import WebPart, cache, config, registry
from misaka.core.wiring import SessionSpec


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in (*config._CREDENTIAL_VARS, *config._ENDPOINT_VARS, "WEB_TOOLS_DEBUG",
                 "MISAKA_USAGE_DB", "MISAKA_USAGE_TASK_ID", "MISAKA_USAGE_GENERATION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(CFG, "web_cache", str(tmp_path / "cache"))
    monkeypatch.setenv("MISAKA_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    write()
    cache.search_memo.clear()
    registry.reset_for_tests()
    yield
    cache.search_memo.clear()
    registry.reset_for_tests()


def write(**overrides):
    write_web({
        "backend": "parallel", "keyless_rescue": False,
        "env": {"PARALLEL_API_KEY": "fixture-secret-token"}, **overrides,
    })


def owner(tmp_path, profile="profile"):
    return WebPart(SessionSpec(str(tmp_path / profile), "sister", str(tmp_path), "bare"))


def transport(monkeypatch, handler=None):
    real = httpx.AsyncClient
    requests = []

    def reply(request):
        requests.append(request)
        return handler(request) if handler else httpx.Response(200, json={
            "results": [{"title": "fixture", "url": "https://example.com/report", "excerpts": ["private body"]}],
        })

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(**{**kw, "transport": httpx.MockTransport(reply), "proxy": None}))
    return requests


async def search(part, query="private research question", call_id="call", ctx=None):
    tool = next(tool for tool in part.tools if tool.name == "web_search")
    return await tool.execute(call_id, {"query": query, "limit": 2}, None, None, ctx)


def records(tmp_path, profile="profile"):
    return [json.loads(p.read_text()) for p in (tmp_path / profile / "logs/web").glob("web-debug-*.json")]


async def test_enabled_real_tool_records_session_call_attempt_and_private_parameters(tmp_path, monkeypatch):
    write(debug_enabled=True)
    requests = transport(monkeypatch)
    part = owner(tmp_path)
    session = SessionManager.inMemory(str(tmp_path))
    try:
        await search(part, ctx=SimpleNamespace(sessionManager=session))
    finally:
        await part.session_shutdown({}, None)
    rows = records(tmp_path)
    assert len(rows) == len(requests) == 1
    row = rows[0]
    assert row["session_id"] == session.getSessionId() and row["tool_call_id"] == "call"
    assert row["tool"] == "web_search" and row["execution_outcome"] == "returned"
    assert row["attempt_count"] == 1 and row["metrics"]["results_count"] == 1
    assert row["parameters"]["query"]["sha256"] == hashlib.sha256(b"private research question").hexdigest()[:16]
    assert row["parameters"]["limit"] == 2
    raw = json.dumps(row)
    assert all(secret not in raw for secret in ("private research question", "fixture-secret-token", "private body"))


async def test_enabling_debug_keeps_material_cache_and_records_hit(tmp_path, monkeypatch):
    requests = transport(monkeypatch)
    part = owner(tmp_path)
    try:
        first = await search(part)
        write(debug_enabled=True)
        assert await search(part) == first
    finally:
        await part.session_shutdown({}, None)
    assert len(requests) == 1
    row, = records(tmp_path)
    assert row["attempt_count"] == 0
    assert any(event["kind"] == "cache_hit" and event["cache"] == "search" for event in row["events"])


@pytest.mark.parametrize("flag", [None, "false", "0", "off"])
async def test_disabled_and_unused_paths_never_create_log_directory(flag, tmp_path, monkeypatch):
    if flag is not None:
        monkeypatch.setenv("WEB_TOOLS_DEBUG", flag)
        write(debug_enabled=True)
    requests = transport(monkeypatch)
    part = owner(tmp_path)
    assert not (tmp_path / "profile/logs").exists()
    try:
        await search(part)
    finally:
        await part.session_shutdown({}, None)
    assert len(requests) == 1 and not (tmp_path / "profile/logs").exists()


async def test_environment_precedence_and_keyword_signature_preserve_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_TOOLS_DEBUG", "true")
    write(debug_enabled=False)
    transport(monkeypatch)
    part = owner(tmp_path)
    ctx = SimpleNamespace(sessionManager=SessionManager.inMemory(str(tmp_path)))
    try:
        tool = next(tool for tool in part.tools if tool.name == "web_search")
        await tool.execute(tool_call_id="kw-call", raw={"query": "secret", "limit": 0, "format": None},
                           signal=None, on_update=None, ctx=ctx)
    finally:
        await part.session_shutdown({}, None)
    row, = records(tmp_path)
    assert row["tool_call_id"] == "kw-call" and row["session_id"] == ctx.sessionManager.getSessionId()
    assert row["parameters"]["limit"] == 0 and row["parameters"]["format"] is None


@pytest.mark.parametrize("name,extract", [
    *[(name, False) for name in ("parallel", "exa", "firecrawl", "keenable", "tavily", "brave-free", "searxng", "xai")],
    *[(name, True) for name in ("parallel", "exa", "firecrawl", "keenable", "tavily")],
])
async def test_every_http_provider_records_actual_attempts(name, extract, tmp_path, monkeypatch):
    from misaka.core.web import bounded
    from misaka.core.web.backends import xai

    monkeypatch.setattr(xai, "_auth_path", lambda: str(tmp_path / "auth.json"))
    async def resolve(*_args):
        return ["93.184.216.34"]
    monkeypatch.setattr(bounded, "_resolve_host", resolve)
    keys = {key: "fixture-secret-token" for key in config._CREDENTIAL_VARS}
    keys["SEARXNG_URL"] = "https://search.example"
    write(backend=name, env=keys, debug_enabled=True)
    doc = {"url": "https://example.org/", "title": "private title", "content": "private body",
           "text": "private body", "full_content": "private body"}
    requests = transport(monkeypatch, lambda _r: httpx.Response(200, json={
        "success": True, "results": [doc], "content": "private body",
        "data": {"web": [], "markdown": "private body"}, "web": {"results": []}, "output": [],
    }))
    part = owner(tmp_path)
    try:
        if extract:
            tool = next(t for t in part.tools if t.name == "web_extract")
            result = await tool.execute("extract", {"urls": [doc["url"]]}, None, None, None)
            assert result["isError"] is False and result["details"]["saved_paths"]
            assert all(doc["content"] in Path(path).read_text() for path in result["details"]["saved_paths"])
        else:
            await search(part)
    finally:
        await part.session_shutdown({}, None)
    row, = records(tmp_path)
    attempts = [e for e in row["events"] if e["kind"] == "attempt"]
    assert len(requests) == len(attempts) == row["attempt_count"] == 1
    assert attempts[0]["backend"] == name and attempts[0]["unit"] == "http_request"
    assert "private body" not in json.dumps(row) and "fixture-secret-token" not in json.dumps(row)


@pytest.mark.parametrize("name", ["parallel", "exa", "firecrawl", "keenable"])
@pytest.mark.parametrize("extract", [False, True])
async def test_keyless_attempts_are_not_double_recorded(name, extract, tmp_path, monkeypatch):
    from misaka.core.web import bounded

    async def resolve(*_args):
        return ["93.184.216.34"]
    monkeypatch.setattr(bounded, "_resolve_host", resolve)
    write(backend=name, env={}, debug_enabled=True, keyless_fallback=True)
    doc = {"url": "https://example.org/", "title": "fixture", "content": "body"}
    def reply(request):
        if name == "keenable" and extract:
            assert request.method == "GET" and request.url.path == "/v1/fetch/public"
            assert request.url.params["url"] == doc["url"]
            return httpx.Response(200, json=doc)
        data = {"results": [doc], "content": "body", "data": {"markdown": "body"}, "success": True}
        if json.loads(request.content).get("method") == "tools/call":
            value = json.dumps(data) if name == "parallel" else "Title: fixture\nURL: https://example.org/\nHighlights:\nbody"
            data = {"result": {"content": [{"type": "text", "text": value}]}}
        return httpx.Response(200, json=data)
    requests = transport(monkeypatch, reply)
    part = owner(tmp_path)
    try:
        if extract:
            tool = next(t for t in part.tools if t.name == "web_extract")
            result = await tool.execute("extract", {"urls": [doc["url"]]}, None, None, None)
            assert result["isError"] is False and result["details"]["saved_paths"]
            assert all(doc["content"] in Path(path).read_text() for path in result["details"]["saved_paths"])
        else:
            await search(part)
    finally:
        await part.session_shutdown({}, None)
    row, = records(tmp_path)
    assert len(requests) == row["attempt_count"] == 1
    assert next(e for e in row["events"] if e["kind"] == "attempt")["backend"] == name


async def test_retry_attempt_ids_join_the_existing_ledger_once(tmp_path, monkeypatch):
    from misaka.core.platform import budget, tasks
    from misaka.core.web.backends import parallel

    write(debug_enabled=True)
    monkeypatch.setattr(parallel, "_retry_delay", lambda *_: 0)
    count = 0
    def reply(_request):
        nonlocal count
        count += 1
        return httpx.Response(503 if count == 1 else 200, json={"results": []})
    requests = transport(monkeypatch, reply)
    part = owner(tmp_path)
    try:
        with budget.usage_context(str(tmp_path / "ledger.db"), "fixture", 1):
            await search(part)
    finally:
        await part.session_shutdown({}, None)
    row, = records(tmp_path)
    con = tasks.connect(str(tmp_path / "ledger.db"))
    try:
        billed = [json.loads(r[0]) for r in con.execute("SELECT payload FROM events WHERE kind='external_call' ORDER BY id")]
    finally:
        con.close()
    assert len(requests) == len(billed) == row["attempt_count"] == 2
    assert [b["web_attempt"] for b in billed] == [1, 2]
    assert all(b["web_trace_id"] == row["trace_id"] for b in billed)
    # Context completion is not HTTP success: a 503 also completed its request.
    assert [e["outcome"] for e in row["events"] if e["kind"] == "attempt"] == ["completed", "completed"]


async def test_extract_cache_and_truncation_metrics_do_not_duplicate_material(tmp_path, monkeypatch):
    from misaka.core.web import bounded

    write(debug_enabled=True)
    async def resolve(*_args):
        return ["93.184.216.34"]
    monkeypatch.setattr(bounded, "_resolve_host", resolve)
    body = "private page text\n" * 1200
    requests = transport(monkeypatch, lambda _r: httpx.Response(200, json={"results": [
        {"url": "https://example.org/", "title": "report", "full_content": body},
    ]}))
    part = owner(tmp_path)
    try:
        tool = next(t for t in part.tools if t.name == "web_extract")
        first = await tool.execute("one", {"urls": ["https://example.org/"], "char_limit": 2000}, None, None, None)
        second = await tool.execute("two", {"urls": ["https://example.org/"], "char_limit": 2000}, None, None, None)
        assert first == second
    finally:
        await part.session_shutdown({}, None)
    rows = {r["tool_call_id"]: r for r in records(tmp_path)}
    assert len(requests) == 1 and rows["two"]["attempt_count"] == 0
    assert rows["one"]["metrics"]["pages_extracted"] == rows["two"]["metrics"]["pages_extracted"] == 1
    assert rows["one"]["metrics"]["pages_truncated"] == rows["two"]["metrics"]["pages_truncated"] == 1
    assert any(e["kind"] == "cache_hit" for e in rows["two"]["events"])
    assert "private page text" not in json.dumps(rows)
    assert first["details"]["saved_paths"] and Path(first["details"]["saved_paths"][0]).exists()


async def test_same_call_ids_across_concurrent_profiles_never_overwrite(tmp_path, monkeypatch):
    write(debug_enabled=True, cache_enabled=False)
    requests = transport(monkeypatch)
    a, b = owner(tmp_path, "a"), owner(tmp_path, "b")
    try:
        await asyncio.gather(*(search(part, query=str(i)) for part in (a, b) for i in range(12)))
    finally:
        await a.session_shutdown({}, None)
        await b.session_shutdown({}, None)
    aa, bb = records(tmp_path, "a"), records(tmp_path, "b")
    assert len(aa) == len(bb) == 12 and len(requests) == 24
    assert len({r["trace_id"] for r in [*aa, *bb]}) == 24
    assert {r["owner_id"] for r in aa}.isdisjoint({r["owner_id"] for r in bb})


@pytest.mark.parametrize("cancel_follower", [False, True])
async def test_shared_flight_links_real_leader_without_another_attempt(cancel_follower, tmp_path, monkeypatch):
    from misaka.core.web.accounting import account_call
    from misaka.core.web.provider import WebSearchProvider

    write(debug_enabled=True, backend="delayed")
    started, release = asyncio.Event(), asyncio.Event()
    class Delayed(WebSearchProvider):
        name = "delayed"
        def is_available(self):
            return True
        async def search(self, query, limit=5):
            async with account_call("web_search", self.name, query):
                started.set()
                await release.wait()
            return {"success": True, "data": {"web": []}}
    part = owner(tmp_path)
    part.configure_tools([SimpleNamespace(path="fixture", webProviders={"delayed": Delayed()})])
    leader = asyncio.create_task(search(part, call_id="leader"))
    await started.wait()
    follower = asyncio.create_task(search(part, call_id="follower"))
    await asyncio.sleep(0.02)
    try:
        if cancel_follower:
            follower.cancel()
            with pytest.raises(asyncio.CancelledError):
                await follower
            assert not leader.done()
        release.set()
        await leader
        if not cancel_follower:
            await follower
    finally:
        release.set()
        await part.session_shutdown({}, None)
        await asyncio.gather(leader, follower, return_exceptions=True)
    rows = {r["tool_call_id"]: r for r in records(tmp_path)}
    assert rows["leader"]["attempt_count"] == 1 and rows["follower"]["attempt_count"] == 0
    assert rows["follower"]["execution_outcome"] == ("cancelled" if cancel_follower else "returned")
    linked = [e for e in rows["follower"]["events"] if e["kind"].startswith("flight_")]
    assert linked and all(e["leader_trace_id"] == rows["leader"]["trace_id"] for e in linked)


@pytest.mark.parametrize("failure", ["timeout", "exception"])
async def test_failed_execution_records_type_without_raw_exception_text(failure, tmp_path):
    from misaka.core.web.runtime import WebRuntime
    from misaka.core.web.scope import WebScope

    write(debug_enabled=True, operation_timeout={"default": 0.01})
    runtime = WebRuntime(WebScope(str(tmp_path / "profile")))
    async def work():
        if failure == "exception":
            raise ValueError("secret exception query and token")
        await asyncio.sleep(5)
    try:
        with pytest.raises(ValueError if failure == "exception" else TimeoutError):
            await runtime.run(work)
    finally:
        await runtime.close()
    row, = records(tmp_path)
    assert row["execution_outcome"] == ("error" if failure == "exception" else "timeout")
    assert "secret exception" not in json.dumps(row)


async def test_disk_failure_keeps_original_result_and_does_not_refetch(tmp_path, monkeypatch, caplog):
    from misaka.core.web import debug

    write(debug_enabled=True)
    def fail(*_args, **_kwargs):
        raise OSError("secret disk path")
    monkeypatch.setattr(debug, "write_bytes", fail)
    requests = transport(monkeypatch)
    part = owner(tmp_path)
    try:
        result = await search(part)
        assert result == await search(part)
    finally:
        await part.session_shutdown({}, None)
    assert len(requests) == 1 and not records(tmp_path)
    assert "write failed (OSError)" in caplog.text and "secret disk path" not in caplog.text


async def test_cancellation_during_flush_drains_owned_write_before_owner_returns(tmp_path, monkeypatch):
    import threading

    from misaka.core.web import debug

    write(debug_enabled=True)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = debug._save
    def save(root, trace):
        started.set()
        release.wait(3)
        original(root, trace)
        finished.set()
    monkeypatch.setattr(debug, "_save", save)
    transport(monkeypatch)
    part = owner(tmp_path)
    call = asyncio.create_task(search(part))
    try:
        for _ in range(200):
            if started.is_set():
                break
            await asyncio.sleep(0.002)
        assert started.is_set()
        call.cancel()
        await asyncio.sleep(0.01)
        call.cancel()
        await asyncio.sleep(0.01)
        assert not call.done() and not finished.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert finished.is_set() and not part.runtime._calls
    finally:
        release.set()
        await part.session_shutdown({}, None)
        await asyncio.gather(call, return_exceptions=True)
    assert len(records(tmp_path)) == 1


async def test_retention_permissions_and_bounded_events_preserve_unrelated_files(tmp_path, monkeypatch):
    import os
    import time

    from misaka.core.web import debug
    from misaka.core.web.runtime import WebRuntime
    from misaka.core.web.scope import WebScope

    write(debug_enabled=True)
    monkeypatch.setattr(debug, "MAX_FILES", 3)
    monkeypatch.setattr(debug, "MAX_BYTES", 2048)
    root = tmp_path / "profile/logs/web"
    root.mkdir(parents=True)
    untouched = root / "notes.json"
    untouched.write_text("keep")
    target = tmp_path / "private.json"
    target.write_text("keep private")
    (root / ("web-debug-" + "a" * 32 + ".json")).symlink_to(target)
    old = root / ("web-debug-" + "b" * 32 + ".json")
    old.write_text("old")
    os.utime(old, (time.time() - 9 * 86400,) * 2)
    runtime = WebRuntime(WebScope(str(tmp_path / "profile")))
    async def work():
        for _ in range(300):
            debug.event("fixture", backend="é" * 200)
        return {"content": []}
    try:
        for _ in range(6):
            await runtime.run(work)
    finally:
        await runtime.close()
    files = [p for p in root.glob("web-debug-*.json") if not p.is_symlink()]
    assert len(files) == 3 and not old.exists()
    assert untouched.read_text() == "keep" and target.read_text() == "keep private"
    assert root.stat().st_mode & 0o777 == 0o700
    for path in files:
        assert path.stat().st_size <= 2048 and path.stat().st_mode & 0o777 == 0o600
        row = json.loads(path.read_text())
        assert row["events_dropped"] + len(row["events"]) == 300


async def test_real_ddgs_worker_records_native_operation_not_invented_http_calls(tmp_path, monkeypatch):
    from misaka.core.web.backends import ddgs

    write(backend="ddgs", env={}, debug_enabled=True)
    stub = tmp_path / "native-api"
    stub.mkdir()
    (stub / "ddgs.py").write_text(
        'class DDGS:\n def __init__(self, timeout, verify=True): pass\n def __enter__(self): return self\n'
        ' def __exit__(self, *args): pass\n def text(self, query, max_results):\n'
        '  return [{"title":"fixture", "href":"https://example.org/", "body":"private body"}]\n')
    monkeypatch.setenv("PYTHONPATH", str(stub))
    monkeypatch.setattr(ddgs.DDGSWebSearchProvider, "is_available", lambda _self: True)
    part = owner(tmp_path)
    try:
        result = await search(part)
    finally:
        await part.session_shutdown({}, None)
    assert '"success": true' in result["content"][0]["text"]
    row, = records(tmp_path)
    attempts = [e for e in row["events"] if e["kind"] == "attempt"]
    assert len(attempts) == row["attempt_count"] == 1
    assert attempts[0]["backend"] == "ddgs" and attempts[0]["unit"] == "provider_operation"


@pytest.mark.parametrize("tool_name", ["web_fetch", "download_file"])
async def test_actual_loopback_http_is_recorded_without_copying_bodies(tool_name, tmp_path):
    write(debug_enabled=True, allow_private_urls=True)
    body = b"<html><body><p>private loopback page</p></body></html>" if tool_name == "web_fetch" else b"PK\x03\x04private archive"
    received, handlers = [], set()
    async def serve(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            received.append(await reader.readuntil(b"\r\n\r\n"))
            mime = "text/html" if tool_name == "web_fetch" else "application/zip"
            writer.write((f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nContent-Type: {mime}\r\nConnection: close\r\n\r\n").encode() + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)
    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    part = owner(tmp_path)
    try:
        tool = next(t for t in part.tools if t.name == tool_name)
        args = {"url": f"http://127.0.0.1:{port}/" + ("report.html" if tool_name == "web_fetch" else "report.zip")}
        result = await tool.execute("socket", args, None, None, None)
    finally:
        await part.session_shutdown({}, None)
        server.close()
        await server.wait_closed()
        await asyncio.gather(*handlers)
    row, = records(tmp_path)
    assert len(received) == row["attempt_count"] == 1
    assert row["metrics"]["result_content_chars"] > 0
    assert "private loopback page" not in json.dumps(row) and "private archive" not in json.dumps(row)
    if tool_name == "download_file":
        assert row["metrics"]["bytes"] == len(body) and Path(result.details["path"]).read_bytes() == body


async def test_keyed_failure_and_real_keyless_rescue_have_one_trace(tmp_path, monkeypatch):
    from misaka.core.web.backends import parallel

    write(debug_enabled=True, keyless_rescue=True)
    monkeypatch.setattr(parallel, "_retry_delay", lambda *_: 0)
    def reply(request):
        if request.url.host == "api.parallel.ai":
            return httpx.Response(503, text="private rejected response")
        return httpx.Response(200, json={"result": {"content": [{
            "type": "text", "text": json.dumps({"results": []}),
        }]}})
    requests = transport(monkeypatch, reply)
    part = owner(tmp_path)
    try:
        response = await search(part)
    finally:
        await part.session_shutdown({}, None)
    row, = records(tmp_path)
    assert '"rescued_from": "parallel"' in response["content"][0]["text"]
    assert len(requests) == row["attempt_count"] == 4
    assert len([e for e in row["events"] if e["kind"] == "rescue"]) == 1
    assert row["metrics"]["reported_errors"] == 0 and "private rejected response" not in json.dumps(row)


async def test_invalid_policy_and_negative_cache_are_not_misreported_as_network_calls(tmp_path, monkeypatch):
    from misaka.core.web import bounded, negative_cache

    write(debug_enabled=True)
    async def resolve(host, _port):
        return ["127.0.0.1" if host == "127.0.0.1" else "93.184.216.34"]
    monkeypatch.setattr(bounded, "_resolve_host", resolve)
    requests = transport(monkeypatch, lambda _r: httpx.Response(403))
    part = owner(tmp_path)
    try:
        tool = next(t for t in part.tools if t.name == "web_fetch")
        with pytest.raises(RuntimeError, match="policy"):
            await tool.execute("blocked", {"url": "http://127.0.0.1/"}, None, None, None)
        with part.scope.activate():
            negative_cache.record_failure("https://example.org/", 403)
        with pytest.raises(RuntimeError):
            await tool.execute("cached", {"url": "https://example.org/"}, None, None, None)
    finally:
        await part.session_shutdown({}, None)
    rows = {r["tool_call_id"]: r for r in records(tmp_path)}
    assert not requests and all(r["attempt_count"] == 0 for r in rows.values())
    assert not any(e["kind"] == "cache_hit" for e in rows["blocked"]["events"])
    assert any(e["kind"] == "cache_hit" and e["cache"] == "negative_fetch" for e in rows["cached"]["events"])


def test_cli_profile_settings_and_validation_are_consumed(tmp_path, capsys):
    from misaka.cli import app
    from misaka.core.web import debug
    from misaka.core.web.scope import WebScope

    profile = str(tmp_path / "profile")
    app.main(["web", "set", "debug_enabled", "true"])
    app.main(["web", "set", "debug_enabled", "off", "--profile", profile])
    app.main(["web", "status", "--profile", profile])
    output = capsys.readouterr().out
    assert "Web debug: off" in output and str(Path(profile) / "logs/web") in output
    with WebScope(profile).activate(snapshot=True):
        assert not debug.enabled()
    app.main(["web", "unset", "debug_enabled", "--profile", profile])
    with WebScope(profile).activate(snapshot=True):
        assert debug.enabled()
    original = home.path("settings").read_bytes()
    with pytest.raises(ValueError, match="true/false"):
        config.set_config("debug_enabled", "invalid")
    assert home.path("settings").read_bytes() == original
    assert not (tmp_path / "profile/logs").exists()


async def test_unserializable_metadata_and_executor_failure_do_not_change_result(tmp_path, monkeypatch):
    from misaka.core.web import debug
    from misaka.core.web.runtime import WebRuntime
    from misaka.core.web.scope import WebScope

    write(debug_enabled=True)
    runtime = WebRuntime(WebScope(str(tmp_path / "profile")))
    marker = object()
    async def work():
        debug.original_json({"unused": marker})
        return marker
    try:
        assert await runtime.run(work) is marker
        row, = records(tmp_path)
        assert row["metrics"]["original_json_serializable"] is False
        async def fail(*_args):
            raise RuntimeError("executor closed secret")
        monkeypatch.setattr(debug, "settle_thread_call", fail)
        assert await runtime.run(work) is marker
        async def bad():
            raise ValueError("original failure")
        with pytest.raises(ValueError, match="original failure"):
            await runtime.run(bad)
    finally:
        await runtime.close()


async def test_no_raw_provider_argument_is_mistaken_for_a_tool_call_id(tmp_path):
    from misaka.core.web.runtime import WebRuntime
    from misaka.core.web.scope import WebScope

    write(debug_enabled=True)
    runtime = WebRuntime(WebScope(str(tmp_path / "profile")))
    async def provider(urls, format=None):
        return []
    try:
        await runtime.run(provider, ["https://secret.example/?token=private"], _tool_name="web_extract")
    finally:
        await runtime.close()
    row, = records(tmp_path)
    assert row["tool_call_id"] is None and "private" not in json.dumps(row)


def test_failed_write_does_not_prune_previous_records(tmp_path, monkeypatch):
    from misaka.core.web import debug

    monkeypatch.setattr(debug, "MAX_FILES", 1)
    previous = tmp_path / ("web-debug-" + "a" * 32 + ".json")
    previous.write_text("keep")
    def fail(*_args, **_kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(debug, "write_bytes", fail)
    debug._save(tmp_path, {"trace_id": "b" * 32, "events": [], "events_dropped": 0})
    assert previous.read_text() == "keep"


async def test_process_writers_share_retention_lock_without_corrupting_records(tmp_path):
    import sys

    root = tmp_path / "logs"
    code = (
        'import sys,uuid\nfrom pathlib import Path\nfrom misaka.core.web import debug\n'
        'debug.MAX_FILES=3\n'
        'for i in range(6):\n'
        ' debug._save(Path(sys.argv[1]), {"trace_id":uuid.uuid4().hex,"events":[],"events_dropped":0})\n'
    )
    workers = [await asyncio.create_subprocess_exec(sys.executable, "-P", "-c", code, str(root),
               stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE) for _ in range(3)]
    try:
        async with asyncio.timeout(20):
            outputs = await asyncio.gather(*(worker.communicate() for worker in workers))
        assert all(worker.returncode == 0 for worker in workers), outputs
        assert all(not error for _out, error in outputs), outputs
    finally:
        for worker in workers:
            if worker.returncode is None:
                worker.kill()
        await asyncio.gather(*(worker.wait() for worker in workers))
    paths = list(root.glob("web-debug-*.json"))
    assert len(paths) == 3 and len({json.loads(p.read_text())["trace_id"] for p in paths}) == 3


async def test_bad_optional_context_and_invalid_signature_still_leave_failure_records(tmp_path, monkeypatch):
    write(debug_enabled=True)
    requests = transport(monkeypatch)
    part = owner(tmp_path)
    try:
        await search(part, ctx=SimpleNamespace(sessionManager=object()))
        tool = next(t for t in part.tools if t.name == "web_search")
        with pytest.raises(TypeError):
            await tool.execute("missing-parameters")
    finally:
        await part.session_shutdown({}, None)
    rows = records(tmp_path)
    assert len(rows) == 2 and len(requests) == 1
    assert {r["metadata_error_type"] for r in rows} == {"AttributeError", "TypeError"}
    assert {r["execution_outcome"] for r in rows} == {"returned", "error"}


async def test_reload_retains_session_correlation_without_duplicate_wrappers(tmp_path, monkeypatch):
    write(debug_enabled=True)
    requests = transport(monkeypatch)
    part = owner(tmp_path)
    ctx = SimpleNamespace(sessionManager=SessionManager.inMemory(str(tmp_path)))
    try:
        await search(part, query="before", ctx=ctx)
        await part.session_shutdown({"reason": "reload"}, ctx)
        part.configure_tools([])
        await search(part, query="after", ctx=ctx)
    finally:
        await part.session_shutdown({}, ctx)
    rows = records(tmp_path)
    assert len(rows) == len(requests) == 2
    assert {r["session_id"] for r in rows} == {ctx.sessionManager.getSessionId()}
    assert len({r["owner_id"] for r in rows}) == 2


async def test_inflight_keeps_debug_snapshot_and_nested_disabled_owner_does_not_charge_parent(tmp_path, monkeypatch):
    from misaka.core.web.accounting import account_call
    from misaka.core.web.runtime import WebRuntime
    from misaka.core.web.scope import WebScope

    write(debug_enabled=True)
    a, b = WebRuntime(WebScope(str(tmp_path / "a"))), WebRuntime(WebScope(str(tmp_path / "b")))
    (tmp_path / "b").mkdir()
    write_web(json.loads('{"debug_enabled": false}'), profile=tmp_path / 'b')
    async def child():
        async with account_call("web_search", "fixture", "child secret"):
            pass
    async def parent():
        write(debug_enabled=False)
        await b.run(child)
        async with account_call("web_search", "fixture", "parent secret"):
            pass
    try:
        await a.run(parent)
    finally:
        await a.close()
        await b.close()
    row, = records(tmp_path, "a")
    assert row["attempt_count"] == 1 and not records(tmp_path, "b")
    assert [e["subject"]["sha256"] for e in row["events"] if e["kind"] == "attempt"] == [hashlib.sha256(b"parent secret").hexdigest()[:16]]


async def test_recursive_invalid_parameters_stay_bounded_without_changing_validation(tmp_path):
    from misaka.core.web import debug

    write(debug_enabled=True)
    recursive = []
    recursive.append(recursive)
    part = owner(tmp_path)
    try:
        tool = next(t for t in part.tools if t.name == "web_extract")
        with pytest.raises(RuntimeError, match="Invalid URL item at index 0") as caught:
            await tool.execute("invalid", {"urls": recursive}, None, None, None)
    finally:
        await part.session_shutdown({}, None)
    row, = records(tmp_path)
    assert row["attempt_count"] == 0 and row["parameters"]["urls"]["count"] == 1
    assert len(json.dumps(row)) < debug.MAX_BYTES
    assert "expected a URL string" in str(caught.value)


@pytest.mark.parametrize("value", [None, "false", 0, []])
async def test_malformed_debug_flag_is_reported_without_changing_search(value, tmp_path, monkeypatch, caplog):
    from misaka.core.web import debug

    write(debug_enabled=value)
    requests = transport(monkeypatch)
    part = owner(tmp_path)
    try:
        assert '"success": true' in (await search(part))["content"][0]["text"]
    finally:
        await part.session_shutdown({}, None)
    with pytest.raises(ValueError, match="debug_enabled"):
        debug.enabled()
    assert len(requests) == 1 and not records(tmp_path)
    assert "not started (ValueError)" in caplog.text

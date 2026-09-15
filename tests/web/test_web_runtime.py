"""Production WebPart lifecycle and provider transports, with no external services."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from misaka.config.product import CFG
from misaka.core import web
from misaka.core.web import cache, config, registry
from misaka.core.web.backends import parallel
from misaka.core.web.runtime import WebRuntime, api_client
from misaka.core.wiring import PART_MODULES, TOOL_MODULES, SessionSpec, assemble


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for key in ("MISAKA_USAGE_DB", "MISAKA_USAGE_TASK_ID", "MISAKA_USAGE_GENERATION"):
        monkeypatch.delenv(key, raising=False)
    for name in config._CREDENTIAL_VARS + config._ENDPOINT_VARS:
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "web.json"
    path.write_text(json.dumps({"backend": "parallel", "keyless_rescue": False,
                                "cache_enabled": False, "env": {"PARALLEL_API_KEY": "TOKEN"}}))
    monkeypatch.setitem(CFG, "web_config", str(path))
    cache.search_memo.clear()
    registry.reset_for_tests()
    yield
    cache.search_memo.clear()
    registry.reset_for_tests()


def http_fixture(monkeypatch, handler):
    real = httpx.AsyncClient
    clients = []

    def factory(**kwargs):
        client = real(transport=httpx.MockTransport(handler), **(kwargs | {"proxy": None}))
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return clients


def part(tmp_path):
    return web.part(SessionSpec(str(tmp_path / "profile"), "sister", str(tmp_path), "bare"))


async def search(owner, query="query"):
    definition = next(tool for tool in owner.tools if tool.name == "web_search")
    return await definition.execute("call", {"query": query}, None, None, None)


async def test_tool_wrapper_preserves_keyword_arguments(monkeypatch, tmp_path):
    clients = http_fixture(monkeypatch, lambda r: httpx.Response(200, json={"results": []}))
    owner = part(tmp_path)
    definition = next(tool for tool in owner.tools if tool.name == "web_search")
    try:
        result = await definition.execute(tool_call_id="call", raw={"query": "query"},
                                          signal=None, on_update=None, ctx=None)
        assert '"success": true' in result["content"][0]["text"]
    finally:
        await owner.session_shutdown({"reason": "quit"}, None)
    assert len(clients) == 1 and clients[0].is_closed


async def test_real_part_is_lazy_reuses_pool_and_registers_only_once(monkeypatch, tmp_path):
    clients = http_fixture(monkeypatch, lambda r: httpx.Response(200, json={"results": []}))
    spec = SessionSpec(str(tmp_path / "profile"), "sister", str(tmp_path), "bare")
    assembly = assemble(spec)
    names = [tool.name for tool in assembly.custom_tools]
    for name in ("web_search", "web_extract", "web_fetch", "download_file"):
        assert names.count(name) == 1
    assert "misaka.core.web" not in TOOL_MODULES
    assert PART_MODULES.count("misaka.core.web") == 1
    owner = next(p for p in assembly.parts if isinstance(p, web.WebPart))
    await owner.session_start({}, None)
    assert clients == []
    await search(owner, "first")
    await search(owner, "second")
    assert len(clients) == 1 and not clients[0].is_closed
    await owner.session_shutdown({"reason": "quit"}, None)
    await owner.session_shutdown({"reason": "quit"}, None)
    assert clients[0].is_closed
    with pytest.raises(RuntimeError, match="closed"):
        await search(owner)


async def test_reload_replaces_closed_pool_without_changing_tool_definitions(monkeypatch, tmp_path):
    clients = http_fixture(monkeypatch, lambda r: httpx.Response(200, json={"results": []}))
    owner = part(tmp_path)
    definitions = list(owner.tools)
    await search(owner)
    await owner.session_shutdown({"reason": "reload"}, None)
    assert clients[0].is_closed
    await owner.session_start({"reason": "reload"}, None)
    await search(owner, "after reload")
    assert len(clients) == 2 and owner.tools == definitions
    await owner.session_shutdown({"reason": "quit"}, None)
    assert all(c.is_closed for c in clients)


async def test_closing_one_session_leaves_another_sessions_pool_open(monkeypatch, tmp_path):
    clients = http_fixture(monkeypatch, lambda r: httpx.Response(200, json={"results": []}))
    first, second = part(tmp_path / "one"), part(tmp_path / "two")
    await search(first)
    await search(second)
    assert len(clients) == 2
    await first.session_shutdown({"reason": "quit"}, None)
    assert clients[0].is_closed and not clients[1].is_closed
    await search(second)
    assert len(clients) == 2
    await second.session_shutdown({"reason": "quit"}, None)


async def test_cancelled_shutdown_drains_inflight_cleanup(monkeypatch, tmp_path):
    entered, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handler(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            await release.wait()

    clients = http_fixture(monkeypatch, handler)
    owner = part(tmp_path)
    call = asyncio.create_task(search(owner))
    await entered.wait()
    closing = asyncio.create_task(owner.session_shutdown({"reason": "quit"}, None))
    await cleanup.wait()
    closing.cancel()
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done() and not clients[0].is_closed
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(closing, 1)
    with pytest.raises(asyncio.CancelledError):
        await call
    assert clients[0].is_closed
    await owner.session_shutdown({"reason": "quit"}, None)
    assert not owner.runtime._calls


async def test_credential_rotation_waits_for_old_borrower(monkeypatch):
    clients = http_fixture(monkeypatch, lambda r: httpx.Response(200, json={}))
    owner = WebRuntime()
    entered, release = asyncio.Event(), asyncio.Event()

    async def old_call():
        async with api_client("p", "https://example.test", "old"):
            entered.set()
            await release.wait()

    async def new_call():
        async with api_client("p", "https://example.test", "new"):
            assert not clients[0].is_closed

    old = asyncio.create_task(owner.run(old_call))
    await entered.wait()
    await owner.run(new_call)
    assert len(clients) == 2 and not clients[0].is_closed
    release.set()
    await old
    assert clients[0].is_closed and not clients[1].is_closed
    await owner.close()
    assert clients[1].is_closed


async def test_endpoint_login_rotation_does_not_reuse_cookies(monkeypatch):
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={}, headers={"set-cookie": "login=A; Path=/"})

    clients = http_fixture(monkeypatch, handler)
    owner = WebRuntime()
    try:
        monkeypatch.setenv("PARALLEL_BASE_URL", "https://A:password-a@example.test")
        await owner.run(parallel._post, "TOKEN", "search", {})
        monkeypatch.setenv("PARALLEL_BASE_URL", "https://B:password-b@example.test")
        await owner.run(parallel._post, "TOKEN", "extract", {})
        assert len(clients) == 2 and clients[0].is_closed
        assert "cookie" not in sent[1].headers
    finally:
        await owner.close()
    assert all(c.is_closed for c in clients)


async def test_standalone_provider_call_closes_its_client_on_http_failure(monkeypatch):
    clients = http_fixture(monkeypatch, lambda r: httpx.Response(401, json={"error": "bad key"}))
    with pytest.raises(httpx.HTTPStatusError):
        await parallel._post("TOKEN", "search", {"objective": "query"})
    assert len(clients) == 1 and clients[0].is_closed


async def test_same_origin_redirect_works_cross_origin_never_receives_payload(monkeypatch):
    sent = []
    target = "/moved"

    def handler(request):
        sent.append(str(request.url))
        if request.url.path == "/v1beta/search":
            return httpx.Response(307, headers={"location": target})
        return httpx.Response(200, json={"results": []})

    http_fixture(monkeypatch, handler)
    await parallel._post("TOKEN", "search", {"objective": "query"})
    assert len(sent) == 2
    sent.clear()

    target = "https://other.example/collect"
    with pytest.raises(httpx.RequestError, match="Cross-origin"):
        await parallel._post("TOKEN", "search", {"objective": "query"})
    assert len(sent) == 1


async def test_config_file_base_url_is_used_by_both_parallel_operations(monkeypatch):
    cfg = json.loads(Path(CFG["web_config"]).read_text())
    cfg["env"]["PARALLEL_BASE_URL"] = "https://example.test/prefix/"
    Path(CFG["web_config"]).write_text(json.dumps(cfg))
    sent = []

    def handler(request):
        sent.append(str(request.url))
        return httpx.Response(200, json={"results": [], "errors": []})

    http_fixture(monkeypatch, handler)
    await parallel._keyed_search("TOKEN", "query", 5)
    await parallel._keyed_extract("TOKEN", ["https://page.test"])
    assert sent == ["https://example.test/prefix/v1beta/search", "https://example.test/prefix/v1beta/extract"]


async def test_real_tcp_connection_is_reused_and_closed(monkeypatch):
    connections, requests, handlers = [], [], set()
    disconnected = asyncio.Event()

    async def serve(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        connections.append(writer)
        try:
            while True:
                try:
                    head = await reader.readuntil(b"\r\n\r\n")
                except asyncio.IncompleteReadError:
                    break
                size = next(int(line.split(b":", 1)[1]) for line in head.splitlines()
                            if line.lower().startswith(b"content-length:"))
                requests.append(await reader.readexactly(size))
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}')
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)
            disconnected.set()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setenv("PARALLEL_BASE_URL", f"http://127.0.0.1:{port}")
    # This fixture never uses an operator's proxy.
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(**(kw | {"trust_env": False, "proxy": None})))
    owner = WebRuntime()
    try:
        for _ in range(2):
            await owner.run(parallel._post, "TOKEN", "search", {"objective": "fixture"})
        assert len(connections) == 1 and len(requests) == 2
        await owner.close()
        await asyncio.wait_for(disconnected.wait(), 1)
    finally:
        await owner.close()
        server.close()
        await server.wait_closed()
        await asyncio.gather(*handlers)


async def test_retiring_failed_client_does_not_mask_request_error_or_leak_secret(monkeypatch, caplog):
    clients = http_fixture(monkeypatch, lambda r: httpx.Response(200, json={}))
    owner = WebRuntime()
    entered, release = asyncio.Event(), asyncio.Event()

    async def request(key):
        async with api_client("p", "https://example.test", key) as client:
            if key == "old":
                original_close = client.aclose

                async def fail_close():
                    await original_close()
                    raise RuntimeError("do not log private TOKEN")

                monkeypatch.setattr(client, "aclose", fail_close)
                entered.set()
                await release.wait()
                raise ValueError("primary request failed")

    old = asyncio.create_task(owner.run(request, "old"))
    await entered.wait()
    await owner.run(request, "new")
    release.set()
    with pytest.raises(ValueError, match="primary request failed"):
        await old
    assert clients[0].is_closed and "cleanup failed (RuntimeError)" in caplog.text
    assert "TOKEN" not in caplog.text
    await owner.close()
    assert clients[1].is_closed


async def test_close_during_vendor_backoff_cancels_without_another_attempt(monkeypatch):
    backoff = asyncio.Event()
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(429)

    async def wait(_seconds):
        backoff.set()
        await asyncio.Event().wait()

    clients = http_fixture(monkeypatch, handler)
    monkeypatch.setattr(asyncio, "sleep", wait)
    owner = WebRuntime()
    call = asyncio.create_task(owner.run(parallel._post, "TOKEN", "search", {}))
    await backoff.wait()
    await owner.close()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert len(sent) == 1 and clients[0].is_closed


def test_pool_owner_rejects_a_second_event_loop():
    owner = WebRuntime()

    async def noop():
        return "ok"

    with asyncio.Runner() as first, asyncio.Runner() as second:
        assert first.run(owner.run(noop)) == "ok"
        with pytest.raises(RuntimeError, match="one event loop"):
            second.run(owner.run(noop))
        with pytest.raises(RuntimeError, match="owning event loop"):
            second.run(owner.close())
        first.run(owner.close())


async def test_product_runtime_disposal_awaits_web_part_before_session_invalidation(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from misaka.core.agent_session_runtime import AgentSessionRuntime
    from misaka.core.moments import Moments

    clients = http_fixture(monkeypatch, lambda r: httpx.Response(200, json={"results": []}))
    owner = part(tmp_path)
    stages = []

    def invalidate():
        assert clients[0].is_closed
        stages.append('session')

    session = SimpleNamespace(extensionRunner=SimpleNamespace(
        create_context=lambda: None, has_handlers=lambda _name: False,
    ), dispose=invalidate)
    session.moments = Moments(session, [owner])
    runtime = AgentSessionRuntime(session, None, None)
    await session.moments.session_start({'reason': 'startup'})
    await search(owner)
    await runtime.dispose()
    assert stages == ['session'] and owner.runtime.closed


async def test_start_waits_for_a_previous_shutdown_to_finish(monkeypatch, tmp_path):
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handler(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    clients = http_fixture(monkeypatch, handler)
    owner = part(tmp_path)
    previous = owner.runtime
    call = asyncio.create_task(search(owner))
    await entered.wait()
    closing = asyncio.create_task(owner.session_shutdown({'reason': 'replace'}, None))
    await cleaning.wait()
    starting = asyncio.create_task(owner.session_start({'reason': 'resume'}, None))
    await asyncio.sleep(0)
    assert not starting.done() and owner.runtime is previous
    release.set()
    await asyncio.gather(closing, starting)
    with pytest.raises(asyncio.CancelledError):
        await call
    assert clients[0].is_closed and owner.runtime is not previous
    await owner.session_shutdown({'reason': 'quit'}, None)

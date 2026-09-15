"""Timeout policy reaches real tool/transport owners; no external services."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from misaka.config.product import CFG
from misaka.core.web import WebPart, cache, config, registry
from misaka.core.web.provider import WebSearchProvider
from misaka.core.wiring import SessionSpec


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in config._CREDENTIAL_VARS + config._ENDPOINT_VARS:
        monkeypatch.delenv(name, raising=False)
    for name in ("MISAKA_USAGE_DB", "MISAKA_USAGE_TASK_ID", "MISAKA_USAGE_GENERATION"):
        monkeypatch.delenv(name, raising=False)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.setitem(CFG, "web_config", str(tmp_path / "web.json"))
    monkeypatch.setitem(CFG, "web_cache", str(tmp_path / "cache"))
    monkeypatch.setenv("MISAKA_CODING_AGENT_DIR", str(tmp_path / "agent"))
    monkeypatch.chdir(tmp_path)
    write(keyless_fallback=False, keyless_rescue=False)
    cache.search_memo.clear()
    registry.reset_for_tests()
    yield
    cache.search_memo.clear()
    registry.reset_for_tests()


def write(**settings):
    Path(CFG["web_config"]).write_text(json.dumps(settings))


def part(tmp_path, provider=None):
    owner = WebPart(SessionSpec(str(tmp_path / "profile"), "sister", str(tmp_path), "bare"))
    if provider is not None:
        owner.configure_tools([SimpleNamespace(path="fixture", webProviders={provider.name: provider})])
    return owner


async def search(owner, query="query"):
    tool = next(tool for tool in owner.tools if tool.name == "web_search")
    return await tool.execute("call", {"query": query}, None, None, None)


class Delayed(WebSearchProvider):
    name = "delayed"

    def __init__(self, delay=0):
        self.delay, self.calls = delay, 0
        self.started, self.finished = asyncio.Event(), asyncio.Event()

    def is_available(self):
        return True

    async def search(self, query, limit=5):
        self.calls += 1
        self.started.set()
        try:
            await asyncio.sleep(self.delay)
            return {"success": True, "data": {"web": []}}
        finally:
            self.finished.set()


async def test_parallel_configured_sdk_phase_values_reach_request(tmp_path, monkeypatch):
    expected = {"connect": 5, "read": 600, "write": 600, "pool": 600}
    write(backend="parallel", env={"PARALLEL_API_KEY": "fixture"},
          http_timeout={"parallel": expected}, keyless_rescue=False)
    seen, clients = [], []
    real = httpx.AsyncClient

    def factory(**kwargs):
        def respond(request):
            seen.append(request.extensions["timeout"])
            return httpx.Response(200, json={"results": []})
        client = real(transport=httpx.MockTransport(respond), **(kwargs | {"proxy": None}))
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    owner = part(tmp_path)
    try:
        await search(owner)
    finally:
        await owner.session_shutdown({}, None)
    assert seen == [expected]
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("name,extract", [
    *[(name, False) for name in ("parallel", "exa", "firecrawl", "keenable", "tavily", "brave-free", "searxng", "xai")],
    *[(name, True) for name in ("parallel", "exa", "firecrawl", "keenable", "tavily")],
])
async def test_every_http_provider_uses_the_configured_phase_policy(name, extract, tmp_path, monkeypatch):
    from misaka.core.web.backends import xai

    monkeypatch.setattr(xai, "_auth_path", lambda: str(tmp_path / "auth.json"))
    keys = {key: "fixture" for key in config._CREDENTIAL_VARS}
    keys["SEARXNG_URL"] = "https://search.example"
    expected = {"connect": 0.3, "read": 1.2, "write": 0, "pool": None}
    write(backend=name, env=keys, http_timeout={name: expected}, keyless_rescue=False)
    seen, clients = [], []
    real = httpx.AsyncClient
    document = {"url": "https://example.org/", "title": "fixture", "content": "body", "text": "body"}

    def respond(request):
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json={"success": True, "results": [document], "content": "body",
                                        "data": {"web": [], "markdown": "body"},
                                        "web": {"results": []}, "output": []})

    def factory(**kwargs):
        client = real(transport=httpx.MockTransport(respond), **(kwargs | {"proxy": None}))
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    owner = part(tmp_path)
    with owner.scope.activate():
        provider = registry.get_provider(name)
    assert not extract or provider.supports_extract()
    try:
        if extract:
            await owner.runtime.run(provider.extract, [document["url"]], _tool_name="web_extract")
        else:
            await search(owner)
        assert seen == [expected]
    finally:
        await owner.session_shutdown({}, None)
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("name", ["parallel", "exa", "firecrawl", "keenable"])
@pytest.mark.parametrize("extract", [False, True])
async def test_ring_transports_use_vendor_policy_not_pool_label(name, extract, tmp_path, monkeypatch):
    expected = {"connect": 2, "read": 3, "write": 4, "pool": 5}
    write(backend=name, keyless_fallback=True, keyless_rescue=False,
          http_timeout={"default": 99, name: expected})
    seen = []
    real = httpx.AsyncClient
    document = {"url": "https://example.org/", "title": "fixture", "content": "body"}

    def respond(request):
        seen.append(request.extensions["timeout"])
        data = {"results": [document], "content": "body", "data": {"markdown": "body"}, "success": True}
        if json.loads(request.content).get("method") == "tools/call":
            text = (json.dumps(data) if name == "parallel"
                    else "Title: fixture\nURL: https://example.org/\nHighlights:\nbody")
            data = {"result": {"content": [{"type": "text", "text": text}]}}
        return httpx.Response(200, json=data)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(respond), **(kwargs | {"proxy": None})))
    owner = part(tmp_path)
    with owner.scope.activate():
        provider = registry.get_provider(name)
    try:
        if extract:
            await owner.runtime.run(provider.extract, [document["url"]], _tool_name="web_extract")
        else:
            await search(owner)
        assert seen == [expected]
    finally:
        await owner.session_shutdown({}, None)


@pytest.mark.parametrize("section,key,value", [
    ("http_timeout", "parallel", True), ("http_timeout", "parallel", -1),
    ("http_timeout", "parallel", {"read": "3"}), ("http_timeout", "parallel", {"typo": 3}),
    ("http_timeout", "parallel", []), ("http_timeout", "parallel", float("nan")),
    ("http_timeout", "parallel", float("inf")), ("http_timeout", "parallel", 10**1000),
    ("http_timeout", "ddgs", {"read": 3}), ("http_timeout", "ddgs", 0),
    ("operation_timeout", "web_search", None), ("operation_timeout", "web_search", False),
    ("operation_timeout", "web_search", -1), ("operation_timeout", "web_search", "5"),
    ("operation_timeout", "web_search", float("inf")), ("operation_timeout", "typo", 3),
])
def test_cli_rejects_invalid_values_without_replacing_config(section, key, value):
    before = Path(CFG["web_config"]).read_bytes()
    with pytest.raises(ValueError):
        config.set_config(f"{section}.{key}", json.dumps(value))
    assert Path(CFG["web_config"]).read_bytes() == before


async def test_hand_edited_invalid_config_stops_before_provider_or_rescue(tmp_path):
    write(backend="delayed", http_timeout={"delayed": {"read": False}})
    provider = Delayed()
    owner = part(tmp_path, provider)
    try:
        with pytest.raises(ValueError, match="http_timeout.delayed.read"):
            await search(owner)
        assert provider.calls == 0
    finally:
        await owner.session_shutdown({}, None)


def test_profile_inheritance_and_falsey_phase_values_reach_cli(tmp_path, capsys):
    from misaka.cli import app
    from misaka.core.web.scope import WebScope
    from misaka.core.web.timeouts import http_timeout, operation_seconds

    write(http_timeout={"default": {"read": 60}, "parallel": {"connect": 5}})
    profile = tmp_path / "profile"
    app.main(["web", "set", "http_timeout.parallel", '{"write": 0, "pool": null}',
              "--profile", str(profile)])
    app.main(["web", "set", "operation_timeout.web_search", "600", "--profile", str(profile)])
    with WebScope(str(profile)).activate(snapshot=True):
        assert http_timeout("parallel", 15).as_dict() == {"connect": 5, "read": 60, "write": 0, "pool": None}
        assert operation_seconds("web_search") == 600
    app.main(["web", "status", "--profile", str(profile)])
    assert "web_search=600" in capsys.readouterr().out
    app.main(["web", "unset", "http_timeout.parallel", "--profile", str(profile)])
    with WebScope(str(profile)).activate(snapshot=True):
        assert http_timeout("parallel", 15).as_dict() == {"connect": 5, "read": 60, "write": 15, "pool": 15}


async def test_expired_follower_leaves_leader_running(tmp_path):
    write(backend="delayed", operation_timeout={"web_search": 0.3}, keyless_rescue=False)
    provider = Delayed(0.08)
    owner = part(tmp_path, provider)
    leader = asyncio.create_task(search(owner))
    await provider.started.wait()
    write(backend="delayed", operation_timeout={"web_search": 0.01}, keyless_rescue=False)
    try:
        with pytest.raises(TimeoutError, match="whole operation"):
            await search(owner)
        assert not leader.done()
        assert '"success": true' in str(await leader)
        assert provider.calls == 1 and not owner.runtime._calls
    finally:
        await owner.session_shutdown({}, None)
        await asyncio.gather(leader, return_exceptions=True)


async def test_timeout_waits_for_owned_thread_and_leaves_no_late_effect(tmp_path):
    import threading

    from misaka.utils.async_lifecycle import run_in_thread

    write(backend="delayed", operation_timeout={"web_search": 0.03}, keyless_rescue=False)
    started, release, stopped = threading.Event(), threading.Event(), threading.Event()

    def blocking():
        started.set()
        release.wait(2)
        stopped.set()

    provider = Delayed()

    async def threaded(*_args):
        await run_in_thread(blocking)

    provider.search = threaded
    owner = part(tmp_path, provider)
    call = asyncio.create_task(search(owner))
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.001)
        assert started.is_set()
        await asyncio.sleep(0.05)
        assert not call.done() and not stopped.is_set()
        release.set()
        with pytest.raises(TimeoutError, match="whole operation"):
            await call
        assert stopped.is_set() and not owner.runtime._calls
    finally:
        release.set()
        await owner.session_shutdown({}, None)
        await asyncio.gather(call, return_exceptions=True)


@pytest.mark.parametrize("behavior", ["zero", "swallow_cancel", "block_loop"])
async def test_expiry_is_not_a_success_when_a_provider_swallows_or_delays_cancel(behavior, tmp_path):
    import time

    write(backend="delayed", operation_timeout={"web_search": 0 if behavior == "zero" else 0.005})
    provider = Delayed()

    async def stubborn(*_args):
        provider.calls += 1
        if behavior == "block_loop":
            time.sleep(0.02)  # noqa: ASYNC251 - prove that a blocking provider cannot report late success
        else:
            try:
                await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                pass
        return {"success": True, "data": {"web": []}}

    provider.search = stubborn
    owner = part(tmp_path, provider)
    try:
        with pytest.raises(TimeoutError, match="whole operation"):
            await search(owner)
        assert provider.calls == (0 if behavior == "zero" else 1)
    finally:
        await owner.session_shutdown({}, None)


async def test_an_inner_timeout_retains_its_error_identity():
    from misaka.core.web.runtime import WebRuntime

    async def inner():
        raise TimeoutError("inner timeout, not the tool deadline")

    owner = WebRuntime()
    try:
        with pytest.raises(TimeoutError, match="inner timeout, not the tool deadline"):
            await owner.run(inner)
    finally:
        await owner.close()


async def test_whole_tool_deadline_cancels_and_drains_provider(tmp_path):
    write(backend="delayed", operation_timeout={"web_search": 0.005}, keyless_rescue=False)
    provider = Delayed(0.05)
    owner = part(tmp_path, provider)
    try:
        with pytest.raises(TimeoutError, match="whole operation"):
            await search(owner)
        assert provider.finished.is_set() and not owner.runtime._calls
    finally:
        await owner.session_shutdown({}, None)


async def test_long_leader_does_not_trigger_old_follower_replay(tmp_path, monkeypatch):
    write(backend="delayed", operation_timeout={"web_search": 0.3}, keyless_rescue=False)
    provider = Delayed(0.04)
    owner = part(tmp_path, provider)
    real_wait = asyncio.wait

    async def accelerate_old_timer(futures, *, timeout=None, **kwargs):
        return await real_wait(futures, timeout=0.005 if timeout == 90 else timeout, **kwargs)

    monkeypatch.setattr(asyncio, "wait", accelerate_old_timer)
    leader = asyncio.create_task(search(owner))
    await provider.started.wait()
    try:
        assert await search(owner) == await leader
        assert provider.calls == 1
    finally:
        await owner.session_shutdown({}, None)
        await asyncio.gather(leader, return_exceptions=True)


@pytest.mark.parametrize("policy", [
    {"http_timeout": {"default": {"read": 600}}, "operation_timeout": {"web_search": 900}},
    {"xai": {"timeout": 600}},
])
async def test_timeout_change_keeps_successful_material_cache(policy, tmp_path):
    write(backend="delayed", keyless_rescue=False)
    provider = Delayed()
    owner = part(tmp_path, provider)
    try:
        await search(owner)
        write(backend="delayed", keyless_rescue=False, **policy)
        await search(owner)
        assert provider.calls == 1
    finally:
        await owner.session_shutdown({}, None)


@pytest.fixture
async def stalled_server():
    entered, release = asyncio.Event(), asyncio.Event()
    handlers = set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            await reader.readuntil(b"\r\n\r\n")
            entered.set()
            await release.wait()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            handlers.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield SimpleNamespace(url=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                              entered=entered, release=release)
    finally:
        release.set()
        server.close()
        await server.wait_closed()
        await asyncio.gather(*handlers, return_exceptions=True)


async def test_read_phase_timeout_on_a_real_socket(stalled_server):
    from misaka.core.web.runtime import WebRuntime, api_client

    write(http_timeout={"socket": {"read": 0.03}}, operation_timeout={"default": 1})

    async def request():
        async with api_client("socket", stalled_server.url) as client:
            return await client.get(stalled_server.url)

    owner = WebRuntime()
    try:
        with pytest.raises(httpx.ReadTimeout):
            await owner.run(request)
        assert stalled_server.entered.is_set() and not owner._calls
    finally:
        await owner.close()


async def test_pool_phase_timeout_on_a_real_shared_connection(stalled_server, monkeypatch):
    from misaka.core.web.runtime import WebRuntime, api_client

    write(http_timeout={"socket": {"pool": 0.03, "read": 1}})
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(limits=httpx.Limits(max_connections=1), **kw))

    async def request():
        async with api_client("socket", stalled_server.url) as client:
            return await client.get(stalled_server.url)

    owner = WebRuntime()
    leader = asyncio.create_task(owner.run(request))
    await asyncio.wait_for(stalled_server.entered.wait(), 1)
    try:
        with pytest.raises(httpx.PoolTimeout):
            await owner.run(request)
        assert not leader.done()
        stalled_server.release.set()
        assert (await leader).text == "ok"
    finally:
        await owner.close()
        await asyncio.gather(leader, return_exceptions=True)


async def test_whole_fetch_deadline_includes_stalled_headers(stalled_server, tmp_path):
    write(allow_private_urls=True, http_timeout={"direct": {"read": None}},
          operation_timeout={"web_fetch": 0.05})
    owner = part(tmp_path)
    tool = next(tool for tool in owner.tools if tool.name == "web_fetch")
    try:
        with pytest.raises(TimeoutError, match="whole operation"):
            await asyncio.wait_for(tool.execute("call", {"url": stalled_server.url},
                                                None, None, None), 1)
        assert stalled_server.entered.is_set() and not owner.runtime._calls
    finally:
        await owner.session_shutdown({}, None)


async def test_transfer_timeout_closes_stream_and_deletes_partial_file(tmp_path, monkeypatch):
    from misaka.core.tools._web import bounded

    write(operation_timeout={"download_transfer": 0.02},
          http_timeout={"direct": {"read": None}})
    closed, seen = asyncio.Event(), []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"a,b\n"
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    async def public(_url, *, proxy=None):
        return ["93.184.216.34"]

    def respond(request):
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, headers={"Content-Type": "text/csv"}, stream=Body())

    real = httpx.AsyncClient
    monkeypatch.setattr(bounded, "vet_public_url", public)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(**(kw | {"transport": httpx.MockTransport(respond), "proxy": None})))
    owner = part(tmp_path)
    tool = next(tool for tool in owner.tools if tool.name == "download_file")
    try:
        with pytest.raises(RuntimeError, match="timed out"):
            await tool.execute("call", {"url": "https://example.org/data.csv"}, None, None, None)
        assert closed.is_set() and seen == [{"connect": 60, "read": None, "write": 60, "pool": 60}]
        assert not list(tmp_path.rglob(".partial-*")) and not list(tmp_path.rglob("data.csv"))
    finally:
        await owner.session_shutdown({}, None)


async def test_ddgs_worker_receives_profile_scalar_timeout_as_data(tmp_path, monkeypatch):
    from misaka.core.web.backends import ddgs
    from misaka.core.web.runtime import WebRuntime
    from misaka.core.web.scope import WebScope

    module = tmp_path / "native"
    module.mkdir()
    (tmp_path / "ddgs.py").write_text("raise AssertionError('workspace module must not shadow the dependency')\n")
    (module / "ddgs.py").write_text(
        "import os\n"
        "class DDGS:\n"
        " def __init__(self,timeout,verify=True): self.timeout=timeout\n"
        " def __enter__(self): return self\n"
        " def __exit__(self,*args): pass\n"
        " def text(self,*args,**kwargs):\n"
        "  assert 'PARALLEL_API_KEY' not in os.environ\n"
        "  return [{'href':'https://example.org/', 'title':'fixture', 'body':str(self.timeout)}]\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(module))
    monkeypatch.setenv("PARALLEL_API_KEY", "should-not-reach-worker")
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "web.json").write_text(json.dumps({"http_timeout": {"ddgs": 7.5}}))
    owner = WebRuntime(WebScope(str(profile)))
    try:
        result = await owner.run(ddgs._run_ddgs_search_bounded, "q", 1)
        assert result[0]["description"] == "7.5"
    finally:
        await owner.close()


async def test_ddgs_deadline_kills_and_reaps_owned_worker(tmp_path, monkeypatch):
    import sys

    import psutil

    from misaka.core.web.backends import ddgs

    pidfile = tmp_path / "pid"
    script = ("import os,sys,time,signal;sys.stdin.read();"
              f"open({str(pidfile)!r},'w').write(str(os.getpid()));"
              "signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)")
    monkeypatch.setattr(ddgs, "_worker_argv", lambda: [sys.executable, "-c", script])
    monkeypatch.setattr(ddgs, "_TERMINATE_GRACE_SECS", 0.02)
    write(operation_timeout={"ddgs": 0.2})
    with pytest.raises(TimeoutError, match="timed out after 0.2s"):
        await ddgs._run_ddgs_search_bounded("query", 1)
    assert not psutil.pid_exists(int(pidfile.read_text()))


async def test_timeout_rotation_preserves_busy_pool_and_retires_it_after_drain(tmp_path, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    real, clients, seen = httpx.AsyncClient, [], []

    async def respond(request):
        seen.append(request.extensions["timeout"]["read"])
        if len(seen) == 1:
            started.set()
            await release.wait()
        return httpx.Response(200, json={"results": []})

    def factory(**kw):
        client = real(transport=httpx.MockTransport(respond), **(kw | {"proxy": None}))
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    settings = {"backend": "parallel", "env": {"PARALLEL_API_KEY": "fixture"}, "keyless_rescue": False}
    write(**settings, http_timeout={"parallel": {"read": 10}})
    owner = part(tmp_path)
    first = asyncio.create_task(search(owner, "first"))
    await started.wait()
    try:
        write(**settings, http_timeout={"parallel": {"read": 20}})
        await search(owner, "second")
        assert len(clients) == 2 and not clients[0].is_closed
        release.set()
        await first
        assert clients[0].is_closed and not clients[1].is_closed and seen == [10, 20]
        await search(owner, "first")  # A completed result does not depend on its timeout policy.
        assert seen == [10, 20]
    finally:
        release.set()
        await owner.session_shutdown({}, None)
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.parametrize("retry_pause", [0, 0.1])
async def test_deadline_stops_vendor_retry_without_starting_rescue(retry_pause, tmp_path, monkeypatch):
    from misaka.core.web import dispatch
    from misaka.core.web.backends import parallel

    write(backend="parallel", env={"PARALLEL_API_KEY": "fixture"},
          operation_timeout={"web_search": 0.02}, keyless_rescue=True)
    attempts, rescues = [], []
    real = httpx.AsyncClient

    async def respond(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(502)
        await asyncio.Event().wait()

    async def rescue(*_args):
        rescues.append(True)
        return {"success": True, "data": {"web": []}}

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(respond), **(kw | {"proxy": None})))
    monkeypatch.setattr(parallel, "_retry_delay", lambda *_args: retry_pause)
    monkeypatch.setattr(dispatch, "search_with_failover", rescue)
    owner = part(tmp_path)
    try:
        with pytest.raises(TimeoutError, match="whole operation"):
            await search(owner)
        assert len(attempts) == (2 if retry_pause == 0 else 1) and not rescues
    finally:
        await owner.session_shutdown({}, None)


@pytest.mark.parametrize("operation", ["ddgs", "firecrawl_scrape"])
async def test_zero_suboperation_does_not_start_worker_or_request(operation, monkeypatch):
    from misaka.core.web.backends import ddgs, firecrawl

    write(operation_timeout={operation: 0})

    async def unexpected(*_args, **_kwargs):
        pytest.fail("A zero operation budget must stop before starting work")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unexpected)
    monkeypatch.setattr(firecrawl, "_post", unexpected)
    with pytest.raises(TimeoutError, match="0s"):
        if operation == "ddgs":
            await ddgs._run_ddgs_search_bounded("q", 1)
        else:
            await firecrawl._scrape("https://example.org", {}, "https://example.org", ["markdown"])


async def test_xai_deadline_drains_auth_store_creation_before_return(tmp_path, monkeypatch):
    import threading

    from misaka.core.web.backends import xai

    release, finished = threading.Event(), threading.Event()
    write(backend="xai", env={"XAI_API_KEY": "fixture"}, operation_timeout={"web_search": 0.02})
    monkeypatch.setattr(xai, "_auth_path", lambda: str(tmp_path / "auth.json"))

    def slow_create(_path):
        release.wait(2)
        finished.set()

    monkeypatch.setattr(xai.AuthStorage, "create", slow_create)
    owner = part(tmp_path)
    call = asyncio.create_task(search(owner))
    try:
        await asyncio.sleep(0.04)
        assert not call.done() and not finished.is_set()
        release.set()
        with pytest.raises(TimeoutError, match="whole operation"):
            await call
        assert finished.is_set()
    finally:
        release.set()
        await owner.session_shutdown({}, None)
        await asyncio.gather(call, return_exceptions=True)


async def test_ddgs_cleanup_waits_for_exit_after_kill(monkeypatch):
    from misaka.core.web.backends import ddgs

    killed, release = asyncio.Event(), asyncio.Event()

    class Process:
        pid, returncode = 123, None

        def terminate(self):
            pass

        def kill(self):
            killed.set()

        async def wait(self):
            await release.wait()
            self.returncode = -9
            return self.returncode

    monkeypatch.setattr(ddgs, "_TERMINATE_GRACE_SECS", 0.001)
    process = Process()
    cleanup = asyncio.create_task(ddgs._terminate_and_reap(process))
    try:
        await killed.wait()
        await asyncio.sleep(0.01)
        assert not cleanup.done() and process.returncode is None
    finally:
        release.set()
        await cleanup
    assert process.returncode == -9

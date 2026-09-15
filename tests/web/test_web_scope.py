"""Profile/extension/setup contracts through the real WebPart, CLI and SDK paths."""

import asyncio
import json
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from misaka.cli import app
from misaka.config.product import CFG
from misaka.core.web import WebPart, cache, config, dispatch, keyless, registry
from misaka.core.web.provider import WebSearchProvider
from misaka.core.web.runtime import WebRuntime
from misaka.core.web.scope import WebScope, cache_namespace
from misaka.core.wiring import SessionSpec


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in config._CREDENTIAL_VARS + config._ENDPOINT_VARS + ("MISAKA_ALLOW_PRIVATE_URLS", "CUSTOM_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(CFG, "web_config", str(tmp_path / "web.json"))
    monkeypatch.setitem(CFG, "web_cache", str(tmp_path / "cache"))
    monkeypatch.setenv("MISAKA_CODING_AGENT_DIR", str(tmp_path / "agent"))
    monkeypatch.chdir(tmp_path)
    write(Path(CFG["web_config"]), keyless_fallback=False, keyless_rescue=False)
    registry.reset_for_tests()
    cache.search_memo.clear()
    yield
    registry.reset_for_tests()
    cache.search_memo.clear()


def write(path, **values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values))


class Provider(WebSearchProvider):
    name = "fixture"

    def __init__(self, marker="fixture", *, available=True):
        self.marker, self.available, self.calls = marker, available, 0

    def is_available(self):
        return self.available

    def supports_extract(self):
        return True

    async def search(self, query, limit=5):
        self.calls += 1
        await asyncio.sleep(0)
        return {"success": True, "data": {"web": [{"title": self.marker, "url": "https://example.org/",
                                                 "description": query, "position": 1}]}}

    def get_setup_schema(self):
        return {"name": self.marker, "env_vars": [{"key": "CUSTOM_TOKEN", "prompt": "Custom token"}]}


def extension(provider, owner="fixture.py"):
    return SimpleNamespace(path=owner, webProviders={provider.name: provider})


def part(path):
    return WebPart(SessionSpec(str(path), "sister", str(path / "workspace"), "bare"))


async def search(owner, query="same query"):
    definition = next(tool for tool in owner.tools if tool.name == "web_search")
    return await definition.execute("call", {"query": query}, None, None, None)


def test_profile_defaults_falsey_overrides_and_writes_do_not_copy_shared_secrets(tmp_path):
    write(Path(CFG["web_config"]), env={"CUSTOM_TOKEN": "shared-secret-token", "EXA_API_KEY": "shared-exa"},
          provider_tier={"exa": "free"}, keyless_rescue=True,
          website_blocklist={"enabled": True, "shared_files": ["global.txt"]})
    profile = tmp_path / "profile"
    write(profile / "web.json", env={"CUSTOM_TOKEN": ""}, keyless_rescue=False)
    with WebScope(str(profile)).activate():
        assert config.provider_env("CUSTOM_TOKEN") == ""
        assert config.provider_env("EXA_API_KEY") == "shared-exa"
        assert config.keyless_rescue_enabled() is False
        assert config.web_config()["website_blocklist"]["shared_files"] == [str(tmp_path / "global.txt")]
        config.set_config("search_backend", "exa")
        assert config.provider_tier("exa") == "auto"
        doc = json.loads((profile / "web.json").read_text())
        assert doc["env"] == {"CUSTOM_TOKEN": ""}
        assert "shared-secret-token" not in (profile / "web.json").read_text()
        config.unset_config("env.CUSTOM_TOKEN")
        assert config.provider_env("CUSTOM_TOKEN") == "shared-secret-token"


async def test_two_real_parts_do_not_share_provider_config_or_single_flight(tmp_path):
    left, right = part(tmp_path / "left"), part(tmp_path / "right")
    a, b = Provider("left"), Provider("right")
    left.configure_tools([extension(a)])
    right.configure_tools([extension(b)])
    try:
        results = await asyncio.gather(search(left), search(right), search(left))
        assert a.calls == b.calls == 1
        assert "left" in str(results[0]) and "right" in str(results[1])
        assert "right" not in str(results[0]) and "left" not in str(results[1])
        assert registry.get_provider("fixture") is None
    finally:
        await asyncio.gather(left.runtime.close(), right.runtime.close())


async def test_call_snapshot_survives_credential_rotation_and_redacts_original_secret(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    write(profile / "web.json", env={"CUSTOM_TOKEN": "original-secret-token"})
    scope = WebScope(str(profile))
    with scope.activate():
        registry.register_provider(Provider())
    owner = WebRuntime(scope)
    entered, release = asyncio.Event(), asyncio.Event()

    async def inspect():
        before = config.provider_env("CUSTOM_TOKEN"), cache_namespace()
        entered.set()
        await release.wait()
        assert config.provider_env("CUSTOM_TOKEN") == before[0]
        assert cache_namespace() == before[1]
        assert config.redact_secrets(before[0]) == config.REDACTED
        return before

    running = asyncio.create_task(owner.run(inspect))
    await entered.wait()
    write(profile / "web.json", env={"CUSTOM_TOKEN": "rotated-secret-token"})
    monkeypatch.setenv("CUSTOM_TOKEN", "new-process-token")
    release.set()
    before = await running

    async def after():
        assert config.provider_env("CUSTOM_TOKEN") == "new-process-token"
        assert cache_namespace() != before[1]
        assert "new-process-token" not in cache_namespace()

    await owner.run(after)
    await owner.close()


@pytest.mark.parametrize("change", ["profile", "credential", "endpoint", "plugin"])
def test_both_cache_halves_and_flight_use_the_same_isolation_boundary(tmp_path, change):
    profile = tmp_path / "a"
    scope = WebScope(str(profile))
    url = "https://example.org/page"
    response = {"success": True, "data": {"web": []}}
    with scope.activate():
        registry.ensure_backends_registered()
        cache.search_memo.store("exa", "query", 5, response)
        cache.extract_cache_put(url, "original", format="markdown", title="title", provider="exa")
        assert cache.search_memo.lookup("exa", "query", 5) is not None
        assert cache.extract_cache_get(url, format="markdown", provider="exa")["content"] == "original"
        old_flight = cache.flight_key("exa", "query", 5)
        if change == "credential":
            config.set_config("env.EXA_API_KEY", "new-credential-value")
        elif change == "endpoint":
            config.set_config("env.PARALLEL_BASE_URL", "https://other.example/api")
        elif change == "plugin":
            registry.replace_extension_providers([extension(Provider())])
    changed = WebScope(str(tmp_path / "b")) if change == "profile" else scope
    with changed.activate():
        assert cache.search_memo.lookup("exa", "query", 5) is None
        assert cache.extract_cache_get(url, format="markdown", provider="exa") is None
        assert cache.flight_key("exa", "query", 5) != old_flight


async def test_profile_policy_and_private_url_flag_reach_direct_and_vendor_tools(tmp_path):
    from misaka.core.tools._web.url_safety import allow_private_urls
    from misaka.core.tools._web.website_policy import check_website_access

    left, right = part(tmp_path / "left"), part(tmp_path / "right")
    write(tmp_path / "left" / "web.json", allow_private_urls=True,
          website_blocklist={"enabled": True, "shared_files": ["blocked.txt"]})
    (tmp_path / "left" / "blocked.txt").write_text("blocked.example\n")

    async def inspect(expected):
        assert allow_private_urls() is expected
        assert bool(check_website_access("https://blocked.example/")) is expected

    try:
        await asyncio.gather(left.runtime.run(inspect, True), right.runtime.run(inspect, False))
        fetch = next(tool for tool in left.tools if tool.name == "web_fetch")
        with pytest.raises(RuntimeError, match="Blocked by website policy"):
            await fetch.execute("call", {"url": "https://blocked.example/"}, None, None, None)
    finally:
        await asyncio.gather(left.runtime.close(), right.runtime.close())


def test_ring_rotation_is_owned_by_scope_and_disable_applies_to_rescue():
    left, right = WebScope(), WebScope()
    left.cursor[0] = right.cursor[0] = 0
    with left.activate():
        config.set_config("disabled_providers", "exa,parallel,keenable")
        assert keyless.ring_order("not-pinned") == ["firecrawl"]
        assert left.cursor[0] == 1
    assert right.cursor[0] == 0
    with right.activate():
        config.set_config("search_backend", "exa")
        provider, _, error = dispatch.resolve_provider()
        assert provider is None and "disabled" in error


async def test_builtin_name_override_does_not_inherit_builtin_availability_or_ring_tier():
    provider = Provider(available=False)
    provider.name = "exa"
    config.set_config("env.EXA_API_KEY", "present-but-not-for-this-plugin")
    registry.register_provider(provider)
    assert registry.is_backend_available("exa") is False
    assert dispatch.serves_keyless(provider) is False
    assert dispatch.memo_identity(provider) == "exa"


@pytest.mark.parametrize("name", ["exa", "parallel", "firecrawl", "keenable"])
async def test_inherited_ring_transport_is_not_rescued_twice(name, tmp_path, monkeypatch):
    from importlib import import_module

    module = import_module(f"misaka.core.web.backends.{name}")
    base = getattr(module, f"{name.title()}WebSearchProvider")

    class Derived(base):
        pass

    calls = []

    async def failed_search(*_args):
        calls.append("search")
        return {"success": False, "error": "ring exhausted"}

    async def failed_extract(_name, urls):
        calls.append("extract")
        return [{"url": url, "error": "ring exhausted"} for url in urls]

    async def duplicate_search(*args):
        calls.append("duplicate search")
        return await failed_search(*args)

    async def duplicate_extract(*args):
        calls.append("duplicate extract")
        return await failed_extract(*args)

    monkeypatch.setattr(module, "search_with_failover", failed_search)
    monkeypatch.setattr(module, "extract_with_failover", failed_extract)
    monkeypatch.setattr(dispatch, "search_with_failover", duplicate_search)
    monkeypatch.setattr(dispatch, "extract_with_failover", duplicate_extract)
    write(Path(CFG["web_config"]), backend=name, keyless_fallback=True, keyless_rescue=True)
    owner, provider = part(tmp_path / name), Derived()
    owner.configure_tools([extension(provider)])
    try:
        result = await owner.runtime.run(dispatch.web_search, "query")
        extracted, rescued = await owner.runtime.run(dispatch.web_extract, provider, ["https://example.org/"])
        assert result["success"] is False and extracted[0]["error"] == "ring exhausted"
        assert calls == ["search", "extract"]
        assert rescued is False
        with owner.scope.activate(snapshot=True):
            assert dispatch.serves_keyless(provider) is True
            assert dispatch.memo_identity(provider) == dispatch.KEYLESS_MEMO_IDENTITY
    finally:
        await owner.runtime.close()


def test_config_transaction_preserves_concurrent_writes_and_invalid_documents(tmp_path):
    with ThreadPoolExecutor(max_workers=6) as workers:
        list(workers.map(lambda index: config.set_config(f"env.KEY_{index}", str(index)), range(18)))
    assert len(config.web_config()["env"]) == 18
    path = Path(CFG["web_config"])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.write_text("{malformed original")
    with pytest.raises(ValueError):
        config.set_config("backend", "exa")
    assert path.read_text() == "{malformed original"


def test_cli_schema_consumer_saves_paid_tier_and_secret_without_printing_it(tmp_path, monkeypatch, capsys):
    profile = tmp_path / "cli-profile"
    monkeypatch.setattr("getpass.getpass", lambda _: "saved-cli-secret")
    app.main(["web", "setup", "exa", "--profile", str(profile), "--tier", "paid", "--capability", "search"])
    doc = json.loads((profile / "web.json").read_text())
    assert doc["search_backend"] == "exa" and "extract_backend" not in doc
    assert doc["provider_tier"]["exa"] == "paid"
    assert doc["env"]["EXA_API_KEY"] == "saved-cli-secret"
    assert "saved-cli-secret" not in capsys.readouterr().out
    assert "EXA_API_KEY" not in config.web_config().get("env", {})


def test_cli_free_variant_has_no_credential_prompt_and_disable_is_real(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("getpass.getpass", lambda _: pytest.fail("free tier has no key prompt"))
    app.main(["web", "setup", "parallel", "--tier", "free"])
    assert config.provider_tier("parallel") == "free"
    app.main(["web", "disable", "parallel"])
    app.main(["web", "status"])
    assert "disabled" in capsys.readouterr().out
    app.main(["web", "enable", "parallel"])
    assert not config.provider_disabled("parallel")


def test_cli_does_not_select_an_unsupported_capability(capsys):
    before = Path(CFG["web_config"]).read_bytes()
    with pytest.raises(SystemExit) as error:
        app.main(["web", "setup", "ddgs", "--capability", "extract", "--yes"])
    assert error.value.code == 2
    assert "supports only search" in capsys.readouterr().err
    assert Path(CFG["web_config"]).read_bytes() == before


async def make_session(tmp_path, factories, *, allowed=None):
    from misaka.core.auth_storage import AuthStorage
    from misaka.core.model_registry import ModelRegistry
    from misaka.core.resource_loader import DefaultResourceLoader
    from misaka.core.sdk import create_agent_session
    from misaka.core.session_manager import SessionManager

    owner = part(tmp_path / "profile")
    storage = AuthStorage.inMemory()
    loader = DefaultResourceLoader({"cwd": str(tmp_path), "agentDir": str(tmp_path / "agent"),
                                    "extensionFactories": factories, "noExtensions": True})
    await loader.reload()
    options = {"cwd": str(tmp_path), "agentDir": str(tmp_path / "agent"), "authStorage": storage,
               "modelRegistry": ModelRegistry.inMemory(storage), "resourceLoader": loader,
               "sessionManager": SessionManager.inMemory(str(tmp_path)),
               "parts": [owner], "customTools": list(owner.tools)}
    if allowed is not None:
        options["tools"] = allowed
    session = (await create_agent_session(options))["session"]
    return owner, session, loader


async def test_real_sdk_publishes_extension_provider_and_headless_reload_removes_it(tmp_path):
    apis = []

    def factory(api):
        apis.append(api)
        api.registerWebSearchProvider(Provider("loaded-owner"))

    owner, session, loader = await make_session(tmp_path, [factory])
    try:
        assert "web_search" in session.getActiveToolNames()
        result = await session._toolRegistry["web_search"].execute("call", {"query": "one"}, None, None)
        assert "loaded-owner" in str(result)
        loader.extensionFactories = []
        await session.reload()  # Deliberately no bindExtensions/UI/session_start.
        assert "web_search" not in session.getActiveToolNames()
        assert "web_search" not in session._toolRegistry
        assert "web_fetch" in session._toolRegistry
        with pytest.raises(RuntimeError, match="no longer active|reload"):
            apis[0].registerWebSearchProvider(Provider("stale"))
        with owner.scope.activate():
            assert registry.get_provider("fixture") is None
    finally:
        await owner.runtime.close()
        session.dispose()


async def test_live_unregister_restores_lower_owner_without_duplicate_tools(tmp_path):
    apis = []

    def first(api):
        apis.append(api)
        api.registerWebSearchProvider(Provider("first"))

    def second(api):
        apis.append(api)
        api.registerWebSearchProvider(Provider("second"))

    owner, session, _ = await make_session(tmp_path, [first, second])
    try:
        assert "second" in str(await search(owner))
        apis[1].unregisterWebSearchProvider("fixture")
        assert "first" in str(await search(owner))
        assert [tool.name for tool in session._customTools].count("web_search") == 1
        apis[0].unregisterWebSearchProvider("fixture")
        assert "web_search" not in session.getActiveToolNames()
        apis[1].registerWebSearchProvider(Provider("third"))
        assert "web_search" in session.getActiveToolNames()
        assert "third" in str(await search(owner))
    finally:
        await owner.runtime.close()
        session.dispose()


@pytest.mark.parametrize("allowed", [[], ["read"]])
async def test_extension_provider_never_bypasses_sdk_tool_ceiling(tmp_path, allowed):
    owner, session, _ = await make_session(tmp_path, [lambda api: api.registerWebSearchProvider(Provider())], allowed=allowed)
    try:
        assert "web_search" not in session.getActiveToolNames()
        assert "web_search" not in session._toolRegistry
    finally:
        await owner.runtime.close()
        session.dispose()


async def test_failed_extension_never_publishes_or_removes_another_owner(tmp_path):
    apis = []

    def broken(api):
        apis.append(api)
        api.registerWebSearchProvider(Provider("broken"))
        raise ValueError("factory failure")

    owner, session, loader = await make_session(tmp_path, [lambda api: api.registerWebSearchProvider(Provider("valid")), broken])
    try:
        assert loader.getExtensions().errors
        assert "valid" in str(await search(owner))
        with pytest.raises(RuntimeError, match="failed to load"):
            apis[0].unregisterWebSearchProvider("fixture")
    finally:
        await owner.runtime.close()
        session.dispose()


async def test_profile_settings_reach_real_parallel_requests_and_rotate_pools(tmp_path, monkeypatch):
    sent, clients = [], []
    real = httpx.AsyncClient

    async def handle(request):
        sent.append((request.url.host, request.headers["x-api-key"]))
        await asyncio.sleep(0)
        return httpx.Response(200, json={"results": []})

    def client(**kwargs):
        instance = real(transport=httpx.MockTransport(handle), **(kwargs | {"proxy": None}))
        clients.append(instance)
        return instance

    monkeypatch.setattr(httpx, "AsyncClient", client)
    profiles = [tmp_path / "one", tmp_path / "two"]
    for directory in profiles:
        write(directory / "web.json", backend="parallel", env={
            "PARALLEL_API_KEY": directory.name + "-credential",
            "PARALLEL_BASE_URL": f"https://{directory.name}.example"})
    left, right = map(part, profiles)
    try:
        await asyncio.gather(search(left), search(right))
        await asyncio.gather(search(left), search(right))
        assert sorted(sent) == [("one.example", "one-credential"), ("two.example", "two-credential")]
        with left.scope.activate():
            config.set_config("env.PARALLEL_API_KEY", "rotated-credential")
        await search(left)
        assert sent[-1] == ("one.example", "rotated-credential")
        assert len(sent) == 3 and len(clients) == 3
    finally:
        await asyncio.gather(left.runtime.close(), right.runtime.close())
    assert all(client.is_closed for client in clients)


def plugin_file(path, marker):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "from pathlib import Path\n"
        "from misaka.core.web.provider import WebSearchProvider\n"
        "class Custom(WebSearchProvider):\n"
        "    name = 'external'\n"
        "    def is_available(self): return True\n"
        "def default(api):\n"
        f"    marker = Path({str(marker)!r})\n"
        "    marker.write_text(str(int(marker.read_text()) + 1) if marker.exists() else '1')\n"
        "    api.registerWebSearchProvider(Custom())\n"
    )


def test_cli_discovers_explicit_plugin_once_and_does_not_leak_it(tmp_path, capsys):
    path, marker = tmp_path / "plugin.py", tmp_path / "count"
    plugin_file(path, marker)
    app.main(["web", "providers", "--extension", str(path)])
    assert "external: search" in capsys.readouterr().out
    assert marker.read_text() == "1"  # Trust bootstrap and final load share one factory.
    assert registry.get_provider("external") is None
    app.main(["web", "setup", "external", "--extension", str(path), "--yes"])
    assert config.config_name("search_backend") == "external"
    assert config.config_name("extract_backend") == ""


def test_cli_project_extension_requires_explicit_selection_even_when_trusted(tmp_path, capsys):
    from misaka.config import CONFIG_DIR_NAME
    from misaka.core.project_trust import ProjectTrustStore

    path, marker = tmp_path / CONFIG_DIR_NAME / "extensions" / "plugin.py", tmp_path / "executed"
    plugin_file(path, marker)
    app.main(["web", "providers"])
    assert not marker.exists()
    assert "external: search" not in capsys.readouterr().out
    ProjectTrustStore(str(tmp_path / "agent")).set(str(tmp_path), True)
    app.main(["web", "providers"])
    assert not marker.exists()  # MISAKA deliberately does not auto-execute project Python.
    assert "external: search" not in capsys.readouterr().out
    app.main(["web", "providers", "--extension", str(path)])
    assert marker.read_text() == "1"
    assert "external: search" in capsys.readouterr().out


def test_cli_reports_failed_plugin_and_publishes_no_partial_provider(tmp_path, capsys):
    path, marker = tmp_path / "broken.py", tmp_path / "executed"
    plugin_file(path, marker)
    with path.open("a") as handle:
        handle.write("    raise ValueError('plugin setup broke')\n")
    app.main(["web", "providers", "--extension", str(path)])
    output = capsys.readouterr()
    assert "plugin setup broke" in output.err
    assert "external: search" not in output.out


def test_install_and_oauth_post_setup_only_run_when_explicit(monkeypatch):
    from misaka.core.auth_storage import AuthStorage

    installs, logins = [], []
    monkeypatch.setattr("subprocess.run", lambda args, **kwargs: installs.append((args, kwargs)))

    async def login(_storage, provider, callbacks):
        logins.append(provider)
        callbacks.onDeviceCode(SimpleNamespace(verificationUri="https://auth.x.ai/device", userCode="CODE"))

    monkeypatch.setattr(AuthStorage, "login", login)
    app.main(["web", "providers"])
    app.main(["web", "status"])
    assert not installs and not logins
    app.main(["web", "setup", "ddgs", "--install", "--yes"])
    assert len(installs) == 1 and installs[0][0][-1] == "ddgs"
    assert installs[0][1] == {"check": True}
    app.main(["web", "setup", "xai", "--login", "--yes"])
    assert logins == ["xai"]


@pytest.mark.parametrize("schema", [[], {"variants": {}}, {"env_vars": [{}]}, {"env_vars": [{"key": "bad.key"}]}])
def test_invalid_setup_schema_does_not_replace_an_existing_provider(schema):
    previous = Provider("valid")
    registry.register_provider(previous)
    invalid = Provider("invalid")
    invalid.get_setup_schema = lambda: schema
    with pytest.raises((TypeError, ValueError)):
        registry.register_provider(invalid)
    assert registry.get_provider("fixture") is previous


async def test_cancelled_factory_has_no_registration_and_cannot_write_late(tmp_path):
    from misaka.core.event_bus import createEventBus
    from misaka.core.extensions.loader import (
        create_extension_runtime,
        load_extension_from_factory,
    )

    entered, apis = asyncio.Event(), []

    async def factory(api):
        apis.append(api)
        api.registerWebSearchProvider(Provider())
        entered.set()
        await asyncio.Event().wait()

    runtime = create_extension_runtime()
    pending = asyncio.create_task(load_extension_from_factory(factory, str(tmp_path), createEventBus(), runtime, "cancelled"))
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert registry.get_provider("fixture") is None
    with pytest.raises(RuntimeError, match="failed to load"):
        apis[0].registerWebSearchProvider(Provider("late"))
    runtime.invalidate()


async def test_interrupted_reload_does_not_reopen_old_extension_providers(tmp_path, monkeypatch):
    owner, session, loader = await make_session(tmp_path, [lambda api: api.registerWebSearchProvider(Provider())])
    entered = asyncio.Event()

    async def pending_reload(*_args):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(type(loader), "reload", pending_reload)
    reloading = asyncio.create_task(session.reload())
    await entered.wait()
    reloading.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await reloading
        assert owner.runtime.closed
        with owner.scope.activate():
            assert registry.get_provider("fixture") is None
        with pytest.raises(RuntimeError, match="closed"):
            await search(owner)
    finally:
        await owner.runtime.close()
        session.dispose()


async def test_direct_fetch_single_flight_never_shares_a_different_profiles_redirect_policy(tmp_path, monkeypatch):
    from misaka.core.tools import web_fetch
    from misaka.core.tools._web.website_policy import check_website_access

    workspace = str(tmp_path / "shared-workspace")
    left = WebPart(SessionSpec(str(tmp_path / "left"), "sister", workspace, "bare"))
    right = WebPart(SessionSpec(str(tmp_path / "right"), "sister", workspace, "bare"))
    write(tmp_path / "right" / "web.json", website_blocklist={"enabled": True, "domains": ["final.example"]})
    entered, follower, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    flights, fetches = [], []
    real_flight = web_fetch.single_flight

    async def flight(key, fn):
        flights.append(key)
        if len(flights) == 2:
            follower.set()
        return await real_flight(key, fn)

    async def fetch(*_args):
        fetches.append(1)
        entered.set()
        await release.wait()
        blocked = check_website_access("https://final.example/")
        return web_fetch._Outcome("blocked" if blocked else "allowed")

    monkeypatch.setattr(web_fetch, "single_flight", flight)
    monkeypatch.setattr(web_fetch, "_fetch", fetch)

    async def call(owner):
        definition = next(tool for tool in owner.tools if tool.name == "web_fetch")
        return await definition.execute("call", {"url": "https://start.example/"}, None, None, None)

    a = asyncio.create_task(call(left))
    await entered.wait()
    b = asyncio.create_task(call(right))
    await follower.wait()
    release.set()
    try:
        results = await asyncio.gather(a, b)
        assert len(fetches) == 2
        assert "allowed" in str(results[0]) and "blocked" in str(results[1])
    finally:
        await asyncio.gather(left.runtime.close(), right.runtime.close())


def test_negative_fetch_cache_belongs_to_the_session_not_the_process():
    from misaka.core.tools._web import negative_cache

    left, right = WebScope(), WebScope()
    url = "https://example.org/"
    with left.activate():
        negative_cache.record_failure(url, 403)
        assert negative_cache.skip_reason(url) is not None
    with right.activate():
        assert negative_cache.skip_reason(url) is None

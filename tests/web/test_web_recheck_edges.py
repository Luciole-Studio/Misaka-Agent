"""Interaction probes added after the initial Web repair, using only temporary state."""
import json
from pathlib import Path

import httpx
import pytest

from misaka.config.product import CFG
from misaka.core.tools._web import bounded, website_policy
from misaka.core.web import cache, config, extract

URL = "https://example.org/paper"


@pytest.fixture(autouse=True)
def isolated_web(monkeypatch, tmp_path):
    monkeypatch.setitem(CFG, "web_config", str(tmp_path / "web.json"))
    monkeypatch.setitem(CFG, "web_cache", str(tmp_path / "cache"))
    for name in config._CREDENTIAL_VARS + config._ENDPOINT_VARS:
        monkeypatch.delenv(name, raising=False)
    website_policy.invalidate_cache()
    yield
    website_policy.invalidate_cache()


def configure(**values):
    Path(CFG["web_config"]).write_text(json.dumps(values))
    website_policy.invalidate_cache()


def fake_http(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(
        transport=httpx.MockTransport(handler), **(kw | {"proxy": None})))

    async def resolve(*_args):
        return ["93.184.216.34"]

    monkeypatch.setattr(bounded, "_resolve_host", resolve)


@pytest.mark.parametrize("corrupt", ["{", "[" * 60_000 + "]" * 60_000], ids=["syntax", "deep-nesting"])
def test_corrupt_cache_page_is_a_miss(corrupt):
    cache.extract_cache_put(URL, "previous paragraph", provider="firecrawl")
    cache._entry_file_path(URL, None, "firecrawl").write_text(corrupt)
    assert cache.extract_cache_get(URL, provider="firecrawl") is None


@pytest.mark.parametrize("corrupt", ["{", "[" * 60_000 + "]" * 60_000], ids=["syntax", "deep-nesting"])
async def test_corrupt_cached_page_does_not_lose_the_extract_batch(monkeypatch, tmp_path, corrupt):
    configure(backend="firecrawl", env={"FIRECRAWL_API_KEY": "fixture-key"})
    cache.extract_cache_put(URL, "old paragraph", provider="firecrawl")
    cache._entry_file_path(URL, None, "firecrawl").write_text(corrupt)
    calls = []

    def handle(request):
        url = json.loads(request.content)["url"]
        calls.append(url)
        return httpx.Response(200, json={"success": True, "data": {
            "markdown": "fresh paragraph", "metadata": {"sourceURL": url}}})

    fake_http(monkeypatch, handle)
    urls = [URL, "https://example.org/second"]
    out = json.loads(await extract.web_extract_tool(urls, cwd=str(tmp_path)))
    assert [entry["url"] for entry in out["results"]] == urls
    assert all(not entry.get("error") for entry in out["results"])
    assert calls == urls


@pytest.mark.parametrize("final_url", [URL, "https://blocked.example.org/final"])
async def test_cached_page_obeys_new_blocklist_on_its_final_source(monkeypatch, tmp_path, final_url):
    configure(backend="firecrawl", env={"FIRECRAWL_API_KEY": "fixture-key"})
    calls = []

    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"success": True, "data": {
            "markdown": "page now blocked", "metadata": {"sourceURL": final_url}}})

    fake_http(monkeypatch, handle)
    first = json.loads(await extract.web_extract_tool([URL], cwd=str(tmp_path / "first")))
    assert first["results"][0]["content"] == "page now blocked"
    configure(backend="firecrawl", env={"FIRECRAWL_API_KEY": "fixture-key"},
              website_blocklist={"enabled": True, "domains": [httpx.URL(final_url).host]})
    second = json.loads(await extract.web_extract_tool([URL], cwd=str(tmp_path / "second")))
    entry = second["results"][0]
    assert entry.get("blocked_by_policy") and entry["error"]
    assert entry["content"] == "" and "saved_path" not in entry
    assert len(calls) == 1
    assert not list((tmp_path / "second").rglob("*.md"))


@pytest.mark.parametrize("host", ["blocked.example.org", "429.example.org", "rate-limit.example.org"])
async def test_policy_refusal_is_not_a_keyless_throttle(monkeypatch, tmp_path, host):
    configure(backend="firecrawl", cache_enabled=False,
              website_blocklist={"enabled": True, "domains": [host]})
    calls = []

    def handle(request):
        calls.append(str(request.url))
        if request.url.path == "/v2/scrape":
            return httpx.Response(200, json={"success": True, "data": {
                "markdown": "blocked redirect body",
                "metadata": {"sourceURL": f"https://{host}/final"}}})
        # The next ring member does not report its redirect target.
        return httpx.Response(200, json={"content": "blocked redirect body"})

    fake_http(monkeypatch, handle)
    out = json.loads(await extract.web_extract_tool([URL], cwd=str(tmp_path)))
    assert len(calls) == 1, calls
    entry = out["results"][0]
    assert entry.get("blocked_by_policy") and entry["error"]
    assert entry["content"] == "" and "saved_path" not in entry


async def test_real_throttle_still_uses_the_next_vendor(monkeypatch, tmp_path):
    configure(backend="firecrawl", cache_enabled=False)
    calls = []

    def handle(request):
        calls.append(str(request.url))
        if request.url.path == "/v2/scrape":
            return httpx.Response(429, text="busy")
        return httpx.Response(200, json={"content": "success via next vendor"})

    fake_http(monkeypatch, handle)
    out = json.loads(await extract.web_extract_tool([URL], cwd=str(tmp_path)))
    assert len(calls) == 2 and "keenable" in calls[1]
    assert out["results"][0]["content"] == "success via next vendor"
    assert not out["results"][0].get("error")


async def test_mixed_policy_block_and_throttle_stops_batch_failover(monkeypatch, tmp_path):
    configure(backend="firecrawl", cache_enabled=False,
              website_blocklist={"enabled": True, "domains": ["429.example.org"]})
    calls = []

    def handle(request):
        calls.append(str(request.url))
        if request.url.path != "/v2/scrape":
            return httpx.Response(200, json={"content": "wrongly rescued"})
        if json.loads(request.content)["url"].endswith("/limited"):
            return httpx.Response(429, text="busy")
        return httpx.Response(200, json={"success": True, "data": {
            "markdown": "blocked body", "metadata": {"sourceURL": "https://429.example.org/final"}}})

    fake_http(monkeypatch, handle)
    out = json.loads(await extract.web_extract_tool([URL, "https://example.org/limited"], cwd=str(tmp_path)))
    assert len(calls) == 2
    assert out["results"][0].get("blocked_by_policy")
    assert "429" in out["results"][1]["error"]
    assert all(not entry["content"] for entry in out["results"])


async def test_a_blocked_cached_redirect_does_not_discard_other_pages(monkeypatch, tmp_path):
    configure(backend="firecrawl", env={"FIRECRAWL_API_KEY": "fixture-key"},
              website_blocklist={"enabled": True, "domains": ["blocked.example.org"]})
    cache.extract_cache_put(URL, "blocked body", provider="firecrawl",
                            metadata={"sourceURL": "https://blocked.example.org/final"})
    calls = []

    def handle(request):
        url = json.loads(request.content)["url"]
        calls.append(url)
        return httpx.Response(200, json={"success": True, "data": {
            "markdown": "allowed page", "metadata": {"sourceURL": url}}})

    fake_http(monkeypatch, handle)
    other = "https://example.org/allowed"
    out = json.loads(await extract.web_extract_tool([URL, other], cwd=str(tmp_path)))
    assert out["results"][0].get("blocked_by_policy")
    assert out["results"][1]["content"] == "allowed page"
    assert calls == [other]
    assert len(list(tmp_path.rglob("*.md"))) == 1


async def test_cancelled_xai_force_refresh_releases_lock_without_publishing(monkeypatch, tmp_path):
    import asyncio
    from types import SimpleNamespace

    from misaka.ai.utils.oauth import OAuthCredentials
    from misaka.core import auth_storage
    from misaka.core.web.backends import xai

    path = str(tmp_path / "auth.json")
    store = auth_storage.AuthStorage.create(path)
    original = {"type": "oauth", "access": "old", "refresh": "refresh", "expires": 9999999999999}
    store.set("xai", original)
    entered = asyncio.Event()

    async def refresh(_credentials):
        entered.set()
        await asyncio.Future()
        return OAuthCredentials(access="unexpected", refresh="unexpected", expires=9999999999999)

    monkeypatch.setattr(auth_storage, "getOAuthProvider", lambda _: SimpleNamespace(
        refreshToken=refresh, getApiKey=lambda c: c.access))
    task = asyncio.create_task(xai._force_refresh_oauth_token(xai.OAuthAccount(store, None), "old"))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    writer = auth_storage.AuthStorage.create(path)
    assert writer.get("xai") == original
    await asyncio.wait_for(asyncio.to_thread(writer.logout, "xai"), 2)
    assert auth_storage.AuthStorage.create(path).get("xai") is None


def test_concurrent_download_publications_keep_every_complete_file(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from misaka.core.tools.download_file import _publish

    directory = tmp_path / "downloads"
    directory.mkdir()

    def publish(index):
        data = bytes([index]) * 1024
        part = directory / f".partial-{index}"
        part.write_bytes(data)
        final = _publish(str(part), str(directory), "same.bin")
        assert not part.exists()
        return final, data

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(publish, range(16)))
    assert len({path for path, _ in results}) == 16
    assert all(Path(path).read_bytes() == data for path, data in results)
    assert len(list(directory.iterdir())) == 16

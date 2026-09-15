"""Desired-behavior regressions for the confirmed September 9 Web audit findings."""
from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from misaka.config.product import CFG
from misaka.core.tools import download_file, web_fetch
from misaka.core.tools._web import bounded, negative_cache, website_policy
from misaka.core.web import cache, config, dispatch, extract, keyless, tool

URL = "https://example.org/paper"


@pytest.fixture(autouse=True)
def isolated_web(monkeypatch, tmp_path):
    monkeypatch.setitem(CFG, "web_config", str(tmp_path / "web.json"))
    monkeypatch.setitem(CFG, "web_cache", str(tmp_path / "cache"))
    for key in config._CREDENTIAL_VARS + config._ENDPOINT_VARS:
        monkeypatch.delenv(key, raising=False)
    cache.search_memo.clear()
    negative_cache.clear()
    website_policy.invalidate_cache()
    yield
    cache.search_memo.clear()
    negative_cache.clear()
    website_policy.invalidate_cache()


class Signal:
    def __init__(self):
        self.event = asyncio.Event()

    @property
    def aborted(self):
        return self.event.is_set()

    async def wait(self):
        await self.event.wait()


async def no_dns(_url, *, proxy=None):
    return ["93.184.216.34"]


def configure(**values):
    from misaka.config.product import CFG
    Path(CFG["web_config"]).write_text(json.dumps(values))


def extractor(monkeypatch, page):
    calls = []
    provider = SimpleNamespace(name="exa")

    async def fetch(_provider, urls, **_kwargs):
        calls.append(list(urls))
        return [dict(page)], False

    monkeypatch.setattr(extract, "vet_public_url", no_dns)
    monkeypatch.setattr(extract, "resolve_extractor", lambda: (provider, "exa", None))
    monkeypatch.setattr(extract, "dispatch_extract", fetch)
    return calls


async def test_evidence_title_or_provider_change_preserves_old_artifact(tmp_path):
    from misaka.core.research.runs import artifact_text

    entry = {"url": URL, "title": "First title", "content": "the same paragraph"}
    first, _ = extract._store_page(str(tmp_path), entry, entry["content"], "exa")
    before = (tmp_path / first).read_bytes()
    second, _ = extract._store_page(
        str(tmp_path), {**entry, "title": "Changed title"}, entry["content"], "tavily"
    )
    after = (tmp_path / first).read_bytes()
    assert first != second and before == after
    row = {"id": "audit-artifact", "path": str(tmp_path / first),
           "sha256": hashlib.sha256(before).hexdigest()}
    assert "the same paragraph" in artifact_text(row)


async def test_cache_preserves_final_url_and_vendor(monkeypatch, tmp_path):
    page = {"url": URL, "title": "Paper", "content": "same paragraph",
            "metadata": {"sourceURL": "https://example.org/final", "served_by": "parallel"}}
    calls = extractor(monkeypatch, page)
    first = json.loads(await extract.web_extract_tool([URL], cwd=str(tmp_path)))
    first_body = (tmp_path / first["results"][0]["saved_path"]).read_text()
    second = json.loads(await extract.web_extract_tool([URL], cwd=str(tmp_path)))
    second_body = (tmp_path / second["results"][0]["saved_path"]).read_text()
    assert len(calls) == 1
    assert "https://example.org/final" in first_body and "parallel" in first_body
    assert second_body == first_body


async def test_fetch_leaves_artifacts_in_each_callers_workspace(monkeypatch, tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    @asynccontextmanager
    async def stream(url, **_kwargs):
        calls.append(url)
        entered.set()
        await release.wait()
        yield httpx.Response(200, text="A readable paragraph.",
                             headers={"content-type": "text/plain"},
                             request=httpx.Request("GET", url))

    monkeypatch.setattr(web_fetch, "open_checked_stream", stream)
    a, b = tmp_path / "workspace-a", tmp_path / "workspace-b"
    a.mkdir(); b.mkdir()
    leader = asyncio.create_task(web_fetch.create_web_fetch_tool_definition(str(a)).execute("a", {"url": URL}))
    await entered.wait()
    follower = asyncio.create_task(web_fetch.create_web_fetch_tool_definition(str(b)).execute("b", {"url": URL}))
    await asyncio.sleep(0)  # follower reaches the real single-flight await
    release.set()
    one, two = await asyncio.gather(leader, follower)
    assert len(calls) == 2
    assert one.details["saved_path"] == two.details["saved_path"]
    assert (a / two.details["saved_path"]).exists()
    assert (b / two.details["saved_path"]).exists()


async def test_selfhost_firecrawl_is_not_the_public_ring(monkeypatch):
    from misaka.core.web.backends import exa, firecrawl
    configure(backend="firecrawl", env={"FIRECRAWL_API_URL": "https://search.example.org"})
    requests, ring_calls = [], []
    real_client = httpx.AsyncClient

    def handle(request):
        requests.append(str(request.url))
        return httpx.Response(200, json={"success": True, "data": {"web": [
            {"url": URL, "title": "SELF HOST ONLY", "description": "private index"}]}})

    monkeypatch.setattr(firecrawl.httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(handle), **(kw | {"proxy": None})))

    async def ring(*_args, **_kwargs):
        ring_calls.append(True)
        return {"success": True, "data": {"web": [{"url": URL, "title": "PUBLIC RING"}]}}

    monkeypatch.setattr(exa, "search_with_failover", ring)
    provider = firecrawl.FirecrawlWebSearchProvider()
    assert dispatch.serves_keyless(provider) is False
    assert dispatch.rescue_eligible(provider) is True
    first = json.loads(await tool.web_search_tool("same question"))
    configure(backend="exa")
    second = json.loads(await tool.web_search_tool("same question"))
    assert "SELF HOST ONLY" in json.dumps(first) and "PUBLIC RING" in json.dumps(second)
    assert requests == ["https://search.example.org/v2/search"] and len(ring_calls) == 1


async def test_cancelled_extract_parent_reaps_vendor_task(monkeypatch):
    configure(cache_enabled=False)
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    provider = SimpleNamespace(name="exa")
    worker = []

    async def fetch(*_args, **_kwargs):
        worker.append(asyncio.current_task())
        entered.set()
        try:
            await release.wait()
            return [{"url": URL, "content": "later result"}], False
        finally:
            finished.set()

    monkeypatch.setattr(extract, "vet_public_url", no_dns)
    monkeypatch.setattr(extract, "resolve_extractor", lambda: (provider, "exa", None))
    monkeypatch.setattr(extract, "dispatch_extract", fetch)
    parent = asyncio.create_task(extract.web_extract_tool([URL], signal=Signal()))
    await entered.wait()
    parent.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parent
    orphan = not finished.is_set() and not worker[0].done()
    release.set()
    await asyncio.gather(*worker, return_exceptions=True)
    assert not orphan


async def test_extract_abort_during_save_is_not_success(monkeypatch, tmp_path):
    configure(cache_enabled=False)
    extractor(monkeypatch, {"url": URL, "content": "paragraph"})
    signal = Signal()
    entered, release = threading.Event(), threading.Event()
    original = extract._prepare

    def stalled(*args):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("audit gate")
        return original(*args)

    monkeypatch.setattr(extract, "_prepare", stalled)
    pending = asyncio.create_task(extract.web_extract_tool([URL], cwd=str(tmp_path), signal=signal))
    assert await asyncio.to_thread(entered.wait, 5)
    signal.event.set()
    await asyncio.sleep(0)
    release.set()
    result = json.loads(await pending)
    assert result["success"] is False and result["error"] == "Interrupted"


async def test_normalized_backend_selection_clears_stale_tier():
    configure(provider_tier={"exa": "free"})
    config.set_config("backend", " EXA ")
    assert config.config_name("backend") == "exa" and config.provider_tier("exa") == "auto"
    config.set_config("backend", "exa")
    assert config.provider_tier("exa") != "free"


async def test_empty_env_status_matches_effective_key(monkeypatch):
    configure(env={"TAVILY_API_KEY": "file-placeholder-key"})
    monkeypatch.setenv("TAVILY_API_KEY", "")
    row = next(r for r in config.credential_status() if r[0] == "TAVILY_API_KEY")
    assert row == ("TAVILY_API_KEY", False, "env") and config.provider_env("TAVILY_API_KEY") == ""


async def test_extract_error_url_fits_output_budget():
    # Real ingress rejects this URL without DNS, then copies it into the error result.
    result = await extract.web_extract_tool(["https://example.org/" + "x" * 150000])
    assert len(result) <= extract.MAX_RESULT_SIZE_CHARS
    assert json.loads(result)["results"][0]["error"]


async def test_redaction_precedes_json_escaping():
    secret = 'fixture-pass\\word"suffix'
    configure(env={"TAVILY_API_KEY": secret})
    result = json.loads(await extract._render([{"url": URL, "content": secret}], None, None, "tavily"))
    assert secret not in result["results"][0]["content"]
    assert secret not in config.redact_secrets(secret)


async def test_publish_failure_leaves_no_final_or_staging_file(monkeypatch, tmp_path):
    @asynccontextmanager
    async def stream(url, **_kwargs):
        yield httpx.Response(200, content=b"GIF89a" + b"x" * 40,
                             headers={"content-type": "image/gif"}, request=httpx.Request("GET", url))

    monkeypatch.setattr(download_file, "open_checked_stream", stream)
    original = download_file.os.link
    def fail(src, dst):
        if Path(dst).name == "image.gif":
            raise OSError(errno.EIO, "audit publish failure")
        return original(src, dst)
    monkeypatch.setattr(download_file.os, "link", fail)
    result = await download_file.create_download_file_tool_definition(str(tmp_path)).execute(
        "audit", {"url": "https://example.org/image.gif"})
    files = {p.name: p.stat().st_size for p in (tmp_path / "downloads").iterdir()}
    assert files == {}
    assert "could not be written" in result.content[0].text


async def test_fetch_alias_keeps_callers_requested_url(monkeypatch, tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    pdf, abstract = "https://arxiv.org/pdf/2401.12345", "https://arxiv.org/abs/2401.12345"
    requests = []

    @asynccontextmanager
    async def stream(url, **_kw):
        requests.append(url)
        entered.set()
        await release.wait()
        yield httpx.Response(200, text="Abstract text", headers={"content-type": "text/plain"},
                             request=httpx.Request("GET", url))

    monkeypatch.setattr(web_fetch, "open_checked_stream", stream)
    definition = web_fetch.create_web_fetch_tool_definition(str(tmp_path))
    first = asyncio.create_task(definition.execute("pdf", {"url": pdf}))
    await entered.wait()
    second = asyncio.create_task(definition.execute("abstract", {"url": abstract}))
    await asyncio.sleep(0)
    release.set()
    _, result = await asyncio.gather(first, second)
    assert requests == [abstract, abstract] and result.details["url"] == abstract


async def test_download_does_not_make_doc_verify_a_citation_gate(monkeypatch, tmp_path):
    monkeypatch.setattr(download_file.corpus, "ingest", lambda *a, **kw: ("fixture-doc", 2))
    note, did = await download_file._index_in_corpus("fixture.txt", str(tmp_path))
    assert did == "fixture-doc" and "doc_verify every quotation before you cite it" not in note


async def test_redirect_stops_before_operator_blocked_host(monkeypatch, tmp_path):
    configure(website_blocklist={"enabled": True, "domains": ["blocked.example.org"]})
    website_policy.invalidate_cache()
    assert website_policy.check_website_access("https://blocked.example.org/page")
    calls = []
    async def resolve(*_args):
        return ["93.184.216.34"]
    monkeypatch.setattr(bounded, "_resolve_host", resolve)

    def handle(request):
        calls.append(request.headers["host"])
        if len(calls) == 1:
            return httpx.Response(302, headers={"location": "https://blocked.example.org/page"})
        return httpx.Response(200, text="operator blocked this site",
                              headers={"content-type": "text/plain"})

    original = bounded.open_checked_stream
    monkeypatch.setattr(web_fetch, "open_checked_stream", lambda url, **kw: original(
        url, transport=httpx.MockTransport(handle), **kw))
    result = await web_fetch.create_web_fetch_tool_definition(str(tmp_path)).execute("audit", {"url": URL})
    assert calls == ["example.org"]
    assert "operator blocked this site" not in result.content[0].text
    assert result.details["refused"]


async def test_keyless_firecrawl_enforces_the_same_final_policy_gate(monkeypatch):
    from misaka.core.web.backends import firecrawl
    configure(website_blocklist={"enabled": True, "domains": ["blocked.example.org"]},
              env={"FIRECRAWL_API_KEY": "fixture-paid-key"})
    website_policy.invalidate_cache()
    payload = {"success": True, "data": {"markdown": "blocked page body", "metadata": {
        "sourceURL": "https://blocked.example.org/page", "title": "Blocked page"}}}
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload)), **(kw | {"proxy": None})))
    monkeypatch.setattr(firecrawl, "vet_public_url", no_dns)
    paid = await firecrawl.FirecrawlWebSearchProvider().extract([URL], format="markdown")
    free = await keyless.firecrawl_extract_keyless([URL])
    assert paid[0]["blocked_by_policy"]
    assert free[0]["content"] == "" and free[0]["blocked_by_policy"]
    assert free[0]["metadata"]["sourceURL"] == "https://blocked.example.org/page"



@pytest.mark.parametrize("replacement", [None, "new-login"])
async def test_xai_rejected_refresh_rechecks_latest_credential(monkeypatch, tmp_path, replacement):
    from misaka.ai.utils.oauth import OAuthCredentials
    from misaka.core import auth_storage
    from misaka.core.web.backends import xai

    path = str(tmp_path / "auth.json")
    stale = auth_storage.AuthStorage.create(path)
    grant = {"type": "oauth", "access": "old-access", "refresh": "old-refresh", "expires": 9999999999999}
    stale.set("xai", grant)
    writer = auth_storage.AuthStorage.create(path)
    if replacement is None:
        writer.logout("xai")
    else:
        writer.set("xai", {**grant, "access": replacement, "refresh": "new-refresh"})
    calls = []
    async def refresh(credentials):
        calls.append(credentials)
        return OAuthCredentials(access="resurrected", refresh="rotated", expires=9999999999999)
    monkeypatch.setattr(auth_storage, "getOAuthProvider", lambda _: SimpleNamespace(
        refreshToken=refresh, getApiKey=lambda c: c.access))
    assert await xai._force_refresh_oauth_token(xai.OAuthAccount(stale, None), "old-access") == (replacement or "")
    assert calls == []
    assert stale.get("xai") == writer.get("xai")
    assert auth_storage.AuthStorage.create(path).get("xai") == writer.get("xai")


async def test_xai_concurrent_401s_refresh_once_under_store_lock(monkeypatch, tmp_path):
    from misaka.ai.utils.oauth import OAuthCredentials
    from misaka.core import auth_storage
    from misaka.core.web.backends import xai

    path = str(tmp_path / "auth.json")
    first = auth_storage.AuthStorage.create(path)
    first.set("xai", {"type": "oauth", "access": "old", "refresh": "refresh", "expires": 9999999999999})
    second = auth_storage.AuthStorage.create(path)
    calls = []
    async def refresh(credentials):
        calls.append(credentials.access)
        await asyncio.sleep(0.01)
        return OAuthCredentials(access="new", refresh="rotated", expires=9999999999999)
    monkeypatch.setattr(auth_storage, "getOAuthProvider", lambda _: SimpleNamespace(
        refreshToken=refresh, getApiKey=lambda c: c.access))
    results = await asyncio.gather(*(xai._force_refresh_oauth_token(xai.OAuthAccount(s, None), "old") for s in (first, second)))
    assert results == ["new", "new"] and calls == ["old"]


@pytest.mark.parametrize("stage", ["extract_dns", "fetch_connect", "download_connect"])
async def test_abort_interrupts_stalled_network_stages(monkeypatch, tmp_path, stage):
    entered, exited = asyncio.Event(), asyncio.Event()
    signal = Signal()
    async def stall(_url, *, proxy=None):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()
    @asynccontextmanager
    async def stream(url, **_kwargs):
        await stall(url)
        yield  # pragma: no cover
    if stage == "extract_dns":
        monkeypatch.setattr(extract, "vet_public_url", stall)
        work = extract.web_extract_tool([URL], signal=signal)
    else:
        module = web_fetch if stage == "fetch_connect" else download_file
        monkeypatch.setattr(module, "open_checked_stream", stream)
        definition = (web_fetch.create_web_fetch_tool_definition(str(tmp_path)) if module is web_fetch
                      else download_file.create_download_file_tool_definition(str(tmp_path)))
        work = definition.execute("abort", {"url": URL}, signal)
    task = asyncio.create_task(work)
    await entered.wait()
    signal.event.set()
    if stage == "extract_dns":
        assert json.loads(await asyncio.wait_for(task, 1))["error"] == "Interrupted"
    else:
        with pytest.raises(RuntimeError, match="Operation aborted"):
            await asyncio.wait_for(task, 1)
    assert exited.is_set()
    assert not list(tmp_path.rglob("*.part"))


async def test_aborted_fetch_follower_does_not_stop_leader(monkeypatch, tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    @asynccontextmanager
    async def stream(url, **_kwargs):
        calls.append(url)
        entered.set()
        await release.wait()
        yield httpx.Response(200, text="body", headers={"content-type": "text/plain"},
                             request=httpx.Request("GET", url))
    monkeypatch.setattr(web_fetch, "open_checked_stream", stream)
    definition = web_fetch.create_web_fetch_tool_definition(str(tmp_path))
    leader = asyncio.create_task(definition.execute("leader", {"url": URL}))
    await entered.wait()
    signal = Signal()
    follower = asyncio.create_task(definition.execute("follower", {"url": URL}, signal))
    await asyncio.sleep(0)
    signal.event.set()
    try:
        with pytest.raises(RuntimeError, match="Operation aborted"):
            await asyncio.wait_for(follower, 1)
        assert not leader.done()
    finally:
        release.set()
    assert (await leader).details["saved_path"]
    assert calls == [URL]


async def test_repeated_parent_cancellation_drains_owned_write(tmp_path):
    from misaka.core.tools._common import run_with_abort
    from misaka.utils.async_lifecycle import run_in_thread

    entered, release = threading.Event(), threading.Event()
    path = tmp_path / "finished.txt"
    def write():
        entered.set()
        assert release.wait(3)
        path.write_text("complete")
    parent = asyncio.create_task(run_with_abort(run_in_thread(write), Signal()))
    assert await asyncio.to_thread(entered.wait, 1)
    try:
        parent.cancel()
        await asyncio.sleep(0.01)
        parent.cancel()
        await asyncio.sleep(0.01)
        assert not parent.done() and not path.exists()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await parent
    assert path.read_text() == "complete"


@pytest.mark.parametrize("error", [None, "\x01" * 150000])
async def test_all_extract_fields_fit_even_with_json_escape_expansion(error):
    rows = [{"url": "\x01" * 150000, "title": "\x01" * 150000,
             "content": "\x01" * 150000, "error": error,
             "blocked_by_policy": {k: "\x01" * 150000 for k in ("host", "rule", "source")}}
            for _ in range(5)]
    rendered = await extract._render(rows, 500000, None, "exa")
    assert len(rendered) <= extract.MAX_RESULT_SIZE_CHARS
    assert len(json.loads(rendered)["results"]) == 5


async def test_publish_is_complete_when_name_becomes_visible(monkeypatch, tmp_path):
    part = tmp_path / ".partial-fixture"
    part.write_bytes(b"complete bytes")
    existing = tmp_path / "file.txt"
    existing.write_bytes(b"already here")
    link = download_file.os.link
    def observe(src, dst):
        link(src, dst)
        assert Path(dst).read_bytes() == b"complete bytes"
    monkeypatch.setattr(download_file.os, "link", observe)
    final = Path(download_file._publish(str(part), str(tmp_path), "file.txt"))
    assert final.name == "file-1.txt" and final.read_bytes() == b"complete bytes"
    assert existing.read_bytes() == b"already here" and not part.exists()


async def test_overlapping_cache_writers_never_pair_body_with_other_metadata(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = cache.atomic.write_text
    def write(path, text, **kw):
        original(path, text, **kw)
        # The first writer has published its data but has not yet published the index.
        if "body-A" in text:
            entered.set()
            assert release.wait(3)
    monkeypatch.setattr(cache.atomic, "write_text", write)
    first = asyncio.create_task(asyncio.to_thread(
        cache.extract_cache_put, URL, "body-A", title="title-A",
        metadata={"sourceURL": "https://example.org/A", "served_by": "exa"}))
    assert await asyncio.to_thread(entered.wait, 1)
    try:
        await asyncio.to_thread(cache.extract_cache_put, URL, "body-B", title="title-B",
            metadata={"sourceURL": "https://example.org/B", "served_by": "parallel"})
    finally:
        release.set()
    await first
    hit = cache.extract_cache_get(URL)
    assert (hit["content"], hit["title"], hit["metadata"]["served_by"]) in {
        ("body-A", "title-A", "exa"), ("body-B", "title-B", "parallel")}


async def test_download_abort_during_stream_close_removes_staging(monkeypatch, tmp_path):
    closing = asyncio.Event()
    signal = Signal()
    @asynccontextmanager
    async def stream(url, **_kwargs):
        yield httpx.Response(200, content=b"GIF89a" + b"x" * 40,
            headers={"content-type": "image/gif"}, request=httpx.Request("GET", url))
        closing.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(download_file, "open_checked_stream", stream)
    task = asyncio.create_task(download_file.create_download_file_tool_definition(str(tmp_path)).execute(
        "abort-close", {"url": "https://example.org/image.gif"}, signal))
    await closing.wait()
    assert list((tmp_path / "downloads").glob(".partial-*"))
    signal.event.set()
    with pytest.raises(RuntimeError, match="Operation aborted"):
        await asyncio.wait_for(task, 1)
    assert list((tmp_path / "downloads").iterdir()) == []


@pytest.mark.parametrize("operation", ["search", "extract"])
@pytest.mark.parametrize("outcome", ["ok", "error", "cancel"])
async def test_parallel_closes_native_client_on_every_exit(monkeypatch, operation, outcome):
    from misaka.core.web.backends.parallel import ParallelWebSearchProvider
    monkeypatch.setenv("PARALLEL_API_KEY", "fixture-key")
    entered, closed = asyncio.Event(), asyncio.Event()
    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            entered.set()
            assert request.headers["parallel-beta"] == "search-extract-2025-10-10"
            if outcome == "cancel":
                await asyncio.Event().wait()
            if outcome == "error":
                raise httpx.ConnectError("fixture outage")
            return httpx.Response(200, json={"results": [], "errors": []})
        async def aclose(self):
            closed.set()
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=Transport(), **(kw | {"proxy": None})))
    provider = ParallelWebSearchProvider()
    task = asyncio.create_task(provider.search("q") if operation == "search" else provider.extract([URL]))
    await entered.wait()
    if outcome == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif outcome == "error" and operation == "extract":
        with pytest.raises(httpx.ConnectError):
            await task
    else:
        await task
    assert closed.is_set()

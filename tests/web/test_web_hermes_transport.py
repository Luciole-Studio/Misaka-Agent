"""Pinned Hermes/SDK transport contracts; failures precede the provider result layer."""

import asyncio
import json

import httpx
import pytest
from webconf import write_web

from misaka.core.platform import budget
from misaka.core.web import bounded, config, registry, website_policy
from misaka.core.web.backends import firecrawl, parallel


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for key in ("MISAKA_USAGE_DB", "MISAKA_USAGE_TASK_ID", "MISAKA_USAGE_GENERATION"):
        monkeypatch.delenv(key, raising=False)
    for key in config._CREDENTIAL_VARS + config._ENDPOINT_VARS:
        monkeypatch.delenv(key, raising=False)
    website_policy.invalidate_cache()
    registry.reset_for_tests()
    yield
    website_policy.invalidate_cache()
    registry.reset_for_tests()


def setup_http(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **(kw | {"proxy": None})))


@pytest.mark.parametrize("vendor,status,headers,attempts", [
    ("parallel", 408, {}, 3), ("parallel", 409, {}, 3),
    ("parallel", 429, {}, 3), ("parallel", 500, {}, 3),
    ("parallel", 403, {"x-should-retry": "true"}, 3),
    ("parallel", 429, {"x-should-retry": "false"}, 1),
    ("parallel", 401, {}, 1), ("firecrawl", 502, {}, 3),
    ("firecrawl", 500, {}, 1), ("firecrawl", 429, {}, 1),
])
async def test_exact_retry_classification_and_attempt_accounting(monkeypatch, vendor, status, headers, attempts):
    sent, sleeps, ledger = [], [], []

    def handler(request):
        sent.append(request)
        return httpx.Response(status if len(sent) < 3 else 200, headers=headers,
                              json={"results": [], "success": True})

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    monkeypatch.setattr(budget, "record_external_call", lambda *a, **k: ledger.append((a, k)))
    setup_http(monkeypatch, handler)
    if vendor == "parallel":
        call = parallel._post("TOKEN", "search", {"objective": "q"})
    else:
        call = firecrawl._post("https://api.firecrawl.dev", {"Authorization": "Bearer TOKEN"},
                               "search", {"query": "q"}, sdk=True)
    if attempts == 1:
        with pytest.raises((httpx.HTTPStatusError, ValueError)):
            await call
    else:
        await call
    assert len(sent) == len(ledger) == attempts
    assert len(sleeps) == attempts - 1
    if vendor == "parallel":
        assert [r.headers["x-stainless-retry-count"] for r in sent] == [str(i) for i in range(attempts)]
    elif sleeps:
        assert sleeps == [0.5, 1.0]


@pytest.mark.parametrize("vendor", ["parallel", "firecrawl"])
async def test_exhausted_transport_failures_make_exactly_three_attempts(monkeypatch, vendor):
    sent = []

    def handler(request):
        sent.append(request)
        raise httpx.ConnectError("offline", request=request)

    async def sleep(_seconds):
        pass

    monkeypatch.setattr(asyncio, "sleep", sleep)
    setup_http(monkeypatch, handler)
    call = (parallel._post("TOKEN", "extract", {"urls": ["https://page.test"]}) if vendor == "parallel"
            else firecrawl._post("https://api.firecrawl.dev", {}, "scrape", {}, sdk=True))
    with pytest.raises(httpx.ConnectError):
        await call
    assert len(sent) == 3


@pytest.mark.parametrize("headers,expected", [
    ({"retry-after-ms": "1250", "retry-after": "9"}, 1.25),
    ({"retry-after": "2.5"}, 2.5), ({"retry-after-ms": "bad", "retry-after": "3"}, 3),
    ({"retry-after": "Wed, 09 Sep 2026 03:00:02 GMT"}, 2),
    ({"retry-after": "0"}, 0.5), ({"retry-after": "61"}, 0.5),
    ({"retry-after": "nonsense"}, 0.5),
])
def test_parallel_retry_after_matches_sdk(monkeypatch, headers, expected):
    monkeypatch.setattr(parallel.random, "random", lambda: 0)
    monkeypatch.setattr(parallel.time, "time", lambda: 1788922800)
    assert parallel._retry_delay(0, httpx.Headers(headers)) == expected


@pytest.mark.parametrize("sdk", [True, False])
async def test_firecrawl_business_failure_preserves_error_and_is_not_empty_success(monkeypatch, sdk):
    setup_http(monkeypatch, lambda r: httpx.Response(200, json={"success": False, "error": "vendor quota"}))
    with pytest.raises(ValueError, match="vendor quota"):
        await firecrawl._post("https://api.firecrawl.dev", {}, "search", {}, sdk=sdk)


@pytest.mark.parametrize("selection", ["backend", "search_backend", "extract_backend"])
async def test_explicit_anonymous_firecrawl_works_even_with_fallback_disabled(monkeypatch, selection):
    write_web({selection: "firecrawl", "keyless_fallback": False})
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"success": True, "data": {"web": []}})

    setup_http(monkeypatch, handler)
    assert registry.web_search_available()
    result = await firecrawl.FirecrawlWebSearchProvider().search("query")
    assert result["success"] and len(calls) == 1
    assert "authorization" not in calls[0].headers


async def test_firecrawl_credentials_win_free_tier_for_both_capabilities(monkeypatch):
    write_web({"provider_tier": {"firecrawl": "free"}})
    monkeypatch.setenv("FIRECRAWL_API_KEY", "TOKEN")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"success": True, "data": {"markdown": "page"}})

    async def vetted(url, *, proxy=None):
        return ("93.184.216.34",)

    setup_http(monkeypatch, handler)
    monkeypatch.setattr(firecrawl, "vet_public_url", vetted)
    provider = firecrawl.FirecrawlWebSearchProvider()
    assert (await provider.search("q"))["success"]
    assert (await provider.extract(["https://page.test"]))[0]["content"] == "page"
    assert len(calls) == 2 and all(r.headers["authorization"] == "Bearer TOKEN" for r in calls)
    assert json.loads(calls[1].content)["maxAge"] == 14_400_000


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ConnectTimeout])
async def test_direct_stream_retries_only_vetted_connect_candidates(monkeypatch, error_type):
    addresses = ("93.184.216.34", "23.192.228.80")
    sent = []

    async def vetted(url, *, proxy=None):
        return addresses

    def handler(request):
        sent.append(request.url.host)
        if len(sent) == 1:
            raise error_type("first address failed", request=request)
        return httpx.Response(200, text="page")

    monkeypatch.setattr(bounded, "vet_public_url", vetted)
    async with bounded.open_checked_stream("https://page.test/", transport=httpx.MockTransport(handler)) as response:
        assert await response.aread() == b"page"
        assert str(response.url) == "https://page.test/"
    assert sent == list(addresses)


async def test_consumer_connect_error_is_not_retried_after_stream_yield(monkeypatch):
    sent = []

    async def vetted(url, *, proxy=None):
        return ("93.184.216.34", "23.192.228.80")

    def handler(request):
        sent.append(request.url.host)
        return httpx.Response(200, text="page")

    monkeypatch.setattr(bounded, "vet_public_url", vetted)
    with pytest.raises(httpx.ConnectError, match="consumer"):
        async with bounded.open_checked_stream("https://page.test/", transport=httpx.MockTransport(handler)):
            raise httpx.ConnectError("consumer failed after using the body")
    assert len(sent) == 1


@pytest.mark.parametrize("data", [{}, {"success": None}, {"success": 0}, {"success": ""}, []])
async def test_firecrawl_sdk_route_requires_a_successful_envelope(monkeypatch, data):
    setup_http(monkeypatch, lambda r: httpx.Response(200, json=data))
    with pytest.raises(ValueError, match="Firecrawl"):
        await firecrawl._post("https://api.firecrawl.dev", {}, "search", {}, sdk=True)

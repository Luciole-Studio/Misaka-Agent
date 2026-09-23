"""The web search provider layer: backends, the keyless ring, selection, and dispatch.

Nothing here touches the network. Every backend's HTTP is stubbed at ``httpx.AsyncClient``
and the one backend that spawns a process (ddgs) gets a stub script instead of its worker,
so the tests exercise the real provider code -- normalizers, error branches, ring failover,
selection order -- rather than mocks of it.
"""

from __future__ import annotations

import json
import sys

import httpx
import pytest
from webconf import write_web

from misaka.config import home
from misaka.core.web import config, dispatch, keyless, registry
from misaka.core.web.backends import (
    brave_free,
    exa,
    firecrawl,
    keenable,
    parallel,
    searxng,
    tavily,
)
from misaka.core.web.backends import (
    ddgs as ddgs_backend,
)
from misaka.core.web.provider import WebSearchProvider

_VENDOR_ENV = (
    "BRAVE_SEARCH_API_KEY",
    "SEARXNG_URL",
    "TAVILY_API_KEY",
    "TAVILY_BASE_URL",
    "EXA_API_KEY",
    "PARALLEL_API_KEY",
    "PARALLEL_SEARCH_MODE",
    "PARALLEL_BASE_URL",
    "KEENABLE_API_KEY",
    "FIRECRAWL_API_KEY",
    "FIRECRAWL_API_URL",
)


@pytest.fixture(autouse=True)
def web_home(monkeypatch, tmp_path):
    """A throwaway web config and an empty vendor environment for every test."""
    for name in _VENDOR_ENV:
        monkeypatch.delenv(name, raising=False)
    path = home.path("settings")           # the "web" section lives here now
    registry.reset_for_tests()
    # The ring cursor is random per process; pin it so walk order is assertable.
    monkeypatch.setattr(keyless.current_scope(), "cursor", [0])
    yield path
    registry.reset_for_tests()


def write_config(_path, **keys) -> None:
    write_web(keys)


# ---------------------------------------------------------------------------
# Config: credentials and tier pins
# ---------------------------------------------------------------------------


def test_a_missing_config_file_is_an_empty_config(web_home):
    assert not web_home.exists()
    assert config.web_config() == {}
    assert config.keyless_tier_enabled() is True
    assert config.provider_tier("exa") == "auto"


def test_a_corrupt_config_file_is_an_empty_config(web_home):
    web_home.write_text("{not json", encoding="utf-8")
    assert config.web_config() == {}


def test_a_credential_can_live_in_the_config_file(web_home):
    write_config(web_home, env={"TAVILY_API_KEY": "from-file"})
    assert config.provider_env("TAVILY_API_KEY") == "from-file"
    assert config.has_env("TAVILY_API_KEY") is True


def test_the_process_environment_beats_the_config_file(monkeypatch, web_home):
    write_config(web_home, env={"TAVILY_API_KEY": "from-file"})
    monkeypatch.setenv("TAVILY_API_KEY", "from-env")
    assert config.provider_env("TAVILY_API_KEY") == "from-env"


def test_a_free_pin_takes_the_keyless_path_even_with_a_key(web_home):
    write_config(web_home, provider_tier={"exa": "free"})
    assert config.use_keyless("exa", "a-real-key") is True


def test_a_paid_pin_takes_the_keyed_path_even_without_a_key(web_home):
    write_config(web_home, provider_tier={"exa": "paid"})
    assert config.use_keyless("exa", "") is False


def test_auto_follows_the_key_and_then_the_tier(web_home):
    assert config.use_keyless("exa", "key") is False
    assert config.use_keyless("exa", "") is True
    write_config(web_home, keyless_fallback=False)
    assert config.use_keyless("exa", "") is False


# ---------------------------------------------------------------------------
# HTTP stubbing
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self, handler, calls):
        self._handler = handler
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        await self.aclose()
        return False

    async def aclose(self):
        pass

    async def _call(self, method, url, kwargs):
        self._calls.append({"method": method, "url": url, **kwargs})
        result = self._handler(method, url, kwargs)
        if isinstance(result, Exception):
            raise result
        return result

    async def get(self, url, **kwargs):
        return await self._call("GET", url, kwargs)

    async def post(self, url, **kwargs):
        return await self._call("POST", url, kwargs)


def stub_http(monkeypatch, handler):
    """Replace ``httpx.AsyncClient`` with a stub; returns the list of recorded calls."""
    calls: list[dict] = []

    def factory(*_args, **_kwargs):
        return _FakeClient(handler, calls)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return calls


def response(status=200, *, json_body=None, text=None, url="https://example.test/"):
    request = httpx.Request("POST", url)
    if json_body is not None:
        return httpx.Response(status, json=json_body, request=request)
    return httpx.Response(status, text=text or "", request=request)


def assert_contract(payload, expected_count=None):
    """Every success response must be exactly the shape the tool renders."""
    assert payload["success"] is True
    web = payload["data"]["web"]
    assert isinstance(web, list)
    if expected_count is not None:
        assert len(web) == expected_count
    for index, entry in enumerate(web, 1):
        assert set(entry) >= {"title", "url", "description", "position"}
        assert entry["position"] == index
        assert isinstance(entry["title"], str)
        assert isinstance(entry["url"], str)
        assert isinstance(entry["description"], str)


# ---------------------------------------------------------------------------
# Brave (free tier)
# ---------------------------------------------------------------------------


async def test_brave_returns_the_contract_shape(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-key")
    body = {
        "web": {
            "results": [
                {"title": "One", "url": "https://one.test", "description": "first"},
                {"title": "Two", "url": "https://two.test", "description": "second"},
            ]
        }
    }
    calls = stub_http(monkeypatch, lambda *_: response(json_body=body))

    result = await brave_free.BraveFreeWebSearchProvider().search("q", limit=5)

    assert_contract(result, 2)
    assert result["data"]["web"][1]["title"] == "Two"
    assert calls[0]["headers"]["X-Subscription-Token"] == "brave-key"
    assert calls[0]["params"] == {"q": "q", "count": 5}


async def test_brave_caps_count_at_twenty(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "k")
    calls = stub_http(monkeypatch, lambda *_: response(json_body={"web": {"results": []}}))

    result = await brave_free.BraveFreeWebSearchProvider().search("q", limit=100)

    assert calls[0]["params"]["count"] == 20
    assert_contract(result, 0)


async def test_brave_without_a_key_says_so():
    result = await brave_free.BraveFreeWebSearchProvider().search("q")
    assert result == {"success": False, "error": "BRAVE_SEARCH_API_KEY is not set"}


@pytest.mark.parametrize("status", [401, 429])
async def test_brave_reports_the_http_status(monkeypatch, status):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "k")
    stub_http(monkeypatch, lambda *_: response(status, text="nope"))

    result = await brave_free.BraveFreeWebSearchProvider().search("q")

    assert result["success"] is False
    assert result["error"] == f"Brave Search returned HTTP {status}"


async def test_brave_reports_an_unreachable_endpoint(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "k")
    stub_http(
        monkeypatch,
        lambda *_: httpx.ConnectTimeout("timed out", request=httpx.Request("GET", "https://b")),
    )

    result = await brave_free.BraveFreeWebSearchProvider().search("q")

    assert result["success"] is False
    assert result["error"].startswith("Could not reach Brave Search")


async def test_brave_reports_a_non_json_body(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "k")
    stub_http(monkeypatch, lambda *_: response(text="<html>hello</html>"))

    result = await brave_free.BraveFreeWebSearchProvider().search("q")

    assert result == {
        "success": False,
        "error": "Could not parse Brave Search response as JSON",
    }


# ---------------------------------------------------------------------------
# SearXNG
# ---------------------------------------------------------------------------


async def test_searxng_sorts_by_score_and_caps_to_limit(monkeypatch):
    monkeypatch.setenv("SEARXNG_URL", "http://localhost:8080/")
    body = {
        "results": [
            {"title": "low", "url": "https://low.test", "content": "c", "score": 0.1},
            {"title": "high", "url": "https://high.test", "content": "c", "score": 9.0},
            {"title": "mid", "url": "https://mid.test", "content": "c", "score": 1.0},
        ]
    }
    calls = stub_http(monkeypatch, lambda *_: response(json_body=body))

    result = await searxng.SearXNGWebSearchProvider().search("q", limit=2)

    assert_contract(result, 2)
    assert [e["title"] for e in result["data"]["web"]] == ["high", "mid"]
    assert calls[0]["url"] == "http://localhost:8080/search"


async def test_searxng_without_a_url_says_so():
    result = await searxng.SearXNGWebSearchProvider().search("q")
    assert result == {"success": False, "error": "SEARXNG_URL is not set"}


async def test_searxng_names_the_instance_it_could_not_reach(monkeypatch):
    monkeypatch.setenv("SEARXNG_URL", "http://localhost:8080")
    stub_http(
        monkeypatch,
        lambda *_: httpx.ConnectError("refused", request=httpx.Request("GET", "http://l")),
    )

    result = await searxng.SearXNGWebSearchProvider().search("q")

    assert "http://localhost:8080" in result["error"]
    assert result["success"] is False


async def test_searxng_reports_the_http_status(monkeypatch):
    monkeypatch.setenv("SEARXNG_URL", "http://localhost:8080")
    stub_http(monkeypatch, lambda *_: response(502, text="bad gateway"))

    result = await searxng.SearXNGWebSearchProvider().search("q")

    assert result["error"] == "SearXNG returned HTTP 502"


# ---------------------------------------------------------------------------
# Tavily
# ---------------------------------------------------------------------------


async def test_tavily_keyed_search(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    body = {"results": [{"title": "T", "url": "https://t.test", "content": "snippet"}]}
    calls = stub_http(monkeypatch, lambda *_: response(json_body=body))

    result = await tavily.TavilyWebSearchProvider().search("q", limit=3)

    assert_contract(result, 1)
    assert result["data"]["web"][0]["description"] == "snippet"
    assert calls[0]["headers"]["Authorization"] == "Bearer tvly-x"
    assert "X-Tavily-Access-Mode" not in calls[0]["headers"]


async def test_tavily_keyed_error_body_reaches_the_model(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    stub_http(monkeypatch, lambda *_: response(432, text="Usage limit exceeded"))

    result = await tavily.TavilyWebSearchProvider().search("q")

    assert result == {"success": False, "error": "Usage limit exceeded"}


async def test_tavily_without_a_key_calls_its_own_endpoint_not_the_ring(monkeypatch):
    """Tavily is not a ring member: an unkeyed call is one keyless request to Tavily."""
    calls = stub_http(monkeypatch, lambda *_: response(json_body={"results": []}))

    result = await tavily.TavilyWebSearchProvider().search("q", limit=7)

    assert result["success"] is True
    assert len(calls) == 1
    assert calls[0]["url"] == "https://api.tavily.com/search"
    assert calls[0]["headers"]["X-Tavily-Access-Mode"] == "keyless"
    assert "Authorization" not in calls[0]["headers"]
    assert calls[0]["json"]["max_results"] == 7
    assert calls[0]["json"]["include_raw_content"] is False
    assert calls[0]["json"]["include_images"] is False


async def test_tavily_keyless_caps_the_result_count(monkeypatch):
    """The cap is the keyed one: a bucketed limit of 100 must not be sent verbatim."""
    calls = stub_http(monkeypatch, lambda *_: response(json_body={"results": []}))

    await tavily.TavilyWebSearchProvider().search("q", limit=100)

    assert calls[0]["json"]["max_results"] == 20


async def test_tavily_keyless_honours_the_base_url_override(monkeypatch):
    monkeypatch.setenv("TAVILY_BASE_URL", "https://tavily.internal")
    calls = stub_http(monkeypatch, lambda *_: response(json_body={"results": []}))

    await tavily.TavilyWebSearchProvider().search("q")

    assert calls[0]["url"] == "https://tavily.internal/search"


async def test_tavily_paid_pin_without_a_key_refuses_before_any_request(monkeypatch, web_home):
    """No key and keyless shut off is a configuration error, not a silent keyless call."""
    write_config(web_home, provider_tier={"tavily": "paid"})
    provider = tavily.TavilyWebSearchProvider()
    calls = stub_http(monkeypatch, lambda *_: response(json_body={"results": []}))

    assert provider.is_keyless_available() is False
    result = await provider.search("q")

    assert calls == []
    assert result["success"] is False
    assert "TAVILY_API_KEY is not set" in result["error"]
    assert "provider_tier.tavily" in result["error"]


async def test_tavily_keyless_off_without_a_key_refuses(monkeypatch, web_home):
    write_config(web_home, keyless_fallback=False)
    calls = stub_http(monkeypatch, lambda *_: response(json_body={"results": []}))

    result = await tavily.TavilyWebSearchProvider().search("q")

    assert calls == []
    assert "TAVILY_API_KEY is not set" in result["error"]


async def test_tavily_free_pin_forces_keyless_even_with_a_key(monkeypatch, web_home):
    write_config(web_home, provider_tier={"tavily": "free"})
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-realkey")
    calls = stub_http(monkeypatch, lambda *_: response(json_body={"results": []}))

    await tavily.TavilyWebSearchProvider().search("q")

    assert calls[0]["headers"]["X-Tavily-Access-Mode"] == "keyless"
    assert "Authorization" not in calls[0]["headers"]


def test_tavily_is_not_a_member_of_the_keyless_ring():
    """Hermes removed it from the ring; a zero-credential install must never land here."""
    assert "tavily" not in keyless.KEYLESS_RING
    assert keyless.KEYLESS_RING == ("exa", "parallel", "firecrawl", "keenable")


# ---------------------------------------------------------------------------
# Exa
# ---------------------------------------------------------------------------


async def test_exa_keyed_search_joins_highlights(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "exa-key")
    body = {
        "results": [
            {"title": "E", "url": "https://e.test", "highlights": ["a", "b"]},
            {"title": "F", "url": "https://f.test"},
        ]
    }
    calls = stub_http(monkeypatch, lambda *_: response(json_body=body))

    result = await exa.ExaWebSearchProvider().search("q", limit=4)

    assert_contract(result, 2)
    assert result["data"]["web"][0]["description"] == "a b"
    assert result["data"]["web"][1]["description"] == ""
    assert calls[0]["headers"]["x-api-key"] == "exa-key"
    assert calls[0]["json"]["contents"] == {"highlights": True}


async def test_exa_paid_pin_without_a_key_names_the_variable(monkeypatch, web_home):
    write_config(web_home, provider_tier={"exa": "paid"})

    result = await exa.ExaWebSearchProvider().search("q")

    assert result["success"] is False
    assert "EXA_API_KEY environment variable not set" in result["error"]


async def test_exa_reports_a_rate_limit(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "k")
    stub_http(monkeypatch, lambda *_: response(429, text="rate limit exceeded"))

    result = await exa.ExaWebSearchProvider().search("q")

    assert result["success"] is False
    assert "rate limit exceeded" in result["error"]


async def test_exa_without_a_key_rides_the_ring(monkeypatch):
    async def fake_ring(name, query, limit):
        return {"success": True, "data": {"web": []}, "via": name}

    monkeypatch.setattr(exa, "search_with_failover", fake_ring)

    result = await exa.ExaWebSearchProvider().search("q")

    assert result["via"] == "exa"


# ---------------------------------------------------------------------------
# Parallel
# ---------------------------------------------------------------------------


async def test_parallel_keyed_search_works_without_sdk(monkeypatch):
    monkeypatch.setenv("PARALLEL_API_KEY", "p-key")
    monkeypatch.setitem(sys.modules, "parallel", None)
    calls = stub_http(monkeypatch, lambda *_args: response(json_body={"results": [
        {"title": "P", "url": "https://p.test", "excerpts": ["one", "two"]}
    ]}))
    result = await parallel.ParallelWebSearchProvider().search("q", 30)
    assert_contract(result, 1)
    assert result["data"]["web"][0]["description"] == "one two"
    assert calls[0]["url"] == "https://api.parallel.ai/v1beta/search"
    assert calls[0]["headers"]["x-api-key"] == "p-key"
    assert calls[0]["headers"]["parallel-beta"] == "search-extract-2025-10-10"
    assert calls[0]["json"] == {"search_queries": ["q"], "objective": "q", "mode": "agentic", "max_results": 20}


async def test_parallel_keyed_failure_is_reported(monkeypatch):
    monkeypatch.setenv("PARALLEL_API_KEY", "p-key")
    stub_http(monkeypatch, lambda *_args: response(401, text="bad key"))
    result = await parallel.ParallelWebSearchProvider().search("q")
    assert result["success"] is False
    assert "401" in result["error"]


def test_parallel_search_mode_is_validated(monkeypatch):
    monkeypatch.setenv("PARALLEL_SEARCH_MODE", "nonsense")
    assert parallel._resolve_search_mode() == "agentic"
    monkeypatch.setenv("PARALLEL_SEARCH_MODE", "FAST")
    assert parallel._resolve_search_mode() == "fast"


# ---------------------------------------------------------------------------
# Keenable
# ---------------------------------------------------------------------------


async def test_keenable_keyed_search(monkeypatch):
    monkeypatch.setenv("KEENABLE_API_KEY", "kn")
    body = {"results": [{"title": "K", "url": "https://k.test", "snippet": "s"}]}
    calls = stub_http(monkeypatch, lambda *_: response(json_body=body))

    result = await keenable.KeenableWebSearchProvider().search("q")

    assert_contract(result, 1)
    assert result["data"]["web"][0]["description"] == "s"
    assert calls[0]["headers"]["Authorization"] == "Bearer kn"
    assert calls[0]["headers"]["X-Keenable-Title"] == keyless.CLIENT_NAME


async def test_keenable_reports_the_error_body(monkeypatch):
    monkeypatch.setenv("KEENABLE_API_KEY", "kn")
    stub_http(monkeypatch, lambda *_: response(429, text="too many requests"))

    result = await keenable.KeenableWebSearchProvider().search("q")

    assert result["error"] == "Keenable search failed: too many requests"


# ---------------------------------------------------------------------------
# Firecrawl
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"data": [{"title": "F", "url": "https://f.test", "description": "d"}]},
        {"data": {"web": [{"title": "F", "url": "https://f.test", "description": "d"}]}},
        {"web": [{"title": "F", "url": "https://f.test", "description": "d"}]},
        {"results": [{"title": "F", "url": "https://f.test", "description": "d"}]},
    ],
)
async def test_firecrawl_normalizes_every_response_shape(monkeypatch, body):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-key")
    stub_http(monkeypatch, lambda *_: response(json_body={"success": True, **body}))

    result = await firecrawl.FirecrawlWebSearchProvider().search("q")

    assert_contract(result, 1)
    assert result["data"]["web"][0]["url"] == "https://f.test"


async def test_firecrawl_self_hosted_never_rides_the_ring(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_URL", "http://localhost:3002")
    calls = stub_http(monkeypatch, lambda *_: response(json_body={"success": True, "data": []}))

    async def unexpected(*_args):
        raise AssertionError("a self-hosted instance must not fall back to the ring")

    monkeypatch.setattr(firecrawl, "search_with_failover", unexpected)

    result = await firecrawl.FirecrawlWebSearchProvider().search("q")

    assert_contract(result, 0)
    assert calls[0]["url"] == "http://localhost:3002/v2/search"
    assert "Authorization" not in calls[0]["headers"]


async def test_firecrawl_paid_pin_without_credentials_names_the_variable(monkeypatch, web_home):
    write_config(web_home, provider_tier={"firecrawl": "paid"})

    result = await firecrawl.FirecrawlWebSearchProvider().search("q")

    # Both ways to configure it, because the branch fires only when neither is set.
    assert "FIRECRAWL_API_KEY is not set" in result["error"]
    assert "FIRECRAWL_API_URL" in result["error"]


# ---------------------------------------------------------------------------
# ddgs
# ---------------------------------------------------------------------------


def _stub_worker(monkeypatch, source):
    monkeypatch.setattr(ddgs_backend, "_worker_argv", lambda: [sys.executable, "-c", source])


async def test_ddgs_without_the_package_says_how_to_install_it(monkeypatch):
    provider = ddgs_backend.DDGSWebSearchProvider()
    monkeypatch.setattr(type(provider), "is_available", lambda _self: False)

    result = await provider.search("q")

    assert result == {
        "success": False,
        "error": "ddgs package is not installed - run `pip install ddgs`",
    }


async def test_ddgs_reads_the_worker_envelope(monkeypatch):
    provider = ddgs_backend.DDGSWebSearchProvider()
    monkeypatch.setattr(type(provider), "is_available", lambda _self: True)
    hit = {"title": "D", "url": "https://d.test", "description": "b", "position": 1}
    _stub_worker(
        monkeypatch,
        "import json,sys; sys.stdin.read(); "
        f"print(json.dumps({{'ok': True, 'results': [{hit!r}]}}))",
    )

    result = await provider.search("q")

    assert_contract(result, 1)


async def test_ddgs_surfaces_a_worker_error(monkeypatch):
    provider = ddgs_backend.DDGSWebSearchProvider()
    monkeypatch.setattr(type(provider), "is_available", lambda _self: True)
    _stub_worker(
        monkeypatch,
        "import json,sys; sys.stdin.read(); "
        "print(json.dumps({'ok': False, 'error': 'RuntimeError: boom'}))",
    )

    result = await provider.search("q")

    assert result == {
        "success": False,
        "error": "DuckDuckGo search failed: RuntimeError: boom",
    }


async def test_ddgs_kills_a_worker_that_overruns_its_deadline(monkeypatch):
    provider = ddgs_backend.DDGSWebSearchProvider()
    monkeypatch.setattr(type(provider), "is_available", lambda _self: True)
    monkeypatch.setattr(ddgs_backend, "operation_seconds", lambda _name: 0.2)
    _stub_worker(monkeypatch, "import sys,time; sys.stdin.read(); time.sleep(30)")

    result = await provider.search("q")

    assert result["success"] is False
    assert "timed out after 0.2s" in result["error"]


async def test_ddgs_rejects_a_worker_that_says_nothing(monkeypatch):
    provider = ddgs_backend.DDGSWebSearchProvider()
    monkeypatch.setattr(type(provider), "is_available", lambda _self: True)
    _stub_worker(monkeypatch, "import sys; sys.stdin.read()")

    result = await provider.search("q")

    assert "exited without a result" in result["error"]


# ---------------------------------------------------------------------------
# The keyless ring
# ---------------------------------------------------------------------------


def _mcp_body(text):
    return {"result": {"content": [{"type": "text", "text": text}]}}


async def test_exa_keyless_parses_the_formatted_payload(monkeypatch):
    text = (
        "Title: First\nURL: https://first.test\nPublished: today\n"
        "Highlights:\nhello\nworld\n---\nTitle: Second\nURL: https://second.test\n"
    )
    stub_http(monkeypatch, lambda *_: response(json_body=_mcp_body(text)))

    result = await keyless.exa_search_keyless("q", limit=5)

    assert_contract(result, 2)
    assert result["data"]["web"][0]["description"] == "hello world"
    assert result["data"]["web"][1]["title"] == "Second"


async def test_exa_keyless_reads_an_sse_stream(monkeypatch):
    payload = json.dumps(_mcp_body("Title: T\nURL: https://t.test\n"))
    stub_http(monkeypatch, lambda *_: response(text=f"event: message\ndata: {payload}\n\n"))

    result = await keyless.exa_search_keyless("q")

    assert_contract(result, 1)


async def test_exa_keyless_surfaces_a_tool_error(monkeypatch):
    body = {"result": {"isError": True, "content": [{"text": "rate limit reached"}]}}
    stub_http(monkeypatch, lambda *_: response(json_body=body))

    result = await keyless.exa_search_keyless("q")

    assert result["success"] is False
    assert "rate limit reached" in result["error"]
    assert "EXA_API_KEY" in result["error"]


async def test_parallel_keyless_maps_excerpts_and_honours_the_limit(monkeypatch):
    payload = {
        "results": [
            {"title": "A", "url": "https://a.test", "excerpts": ["x", "y"]},
            {"title": "B", "url": "https://b.test", "excerpts": []},
            {"title": "C", "url": "https://c.test"},
        ]
    }
    stub_http(monkeypatch, lambda *_: response(json_body=_mcp_body(json.dumps(payload))))

    result = await keyless.parallel_search_keyless("q", limit=2)

    assert_contract(result, 2)
    assert result["data"]["web"][0]["description"] == "x y"


async def test_parallel_keyless_sends_no_user_identifier(monkeypatch):
    calls = stub_http(
        monkeypatch, lambda *_: response(json_body=_mcp_body(json.dumps({"results": []})))
    )

    await keyless.parallel_search_keyless("q")

    arguments = calls[0]["json"]["params"]["arguments"]
    assert set(arguments) == {"objective", "search_queries", "session_id"}
    assert arguments["session_id"] == keyless._SESSION_ID


async def test_keenable_keyless_uses_the_public_endpoint(monkeypatch):
    body = {"results": [{"title": "K", "url": "https://k.test", "snippet": "s"}]}
    calls = stub_http(monkeypatch, lambda *_: response(json_body=body))

    result = await keyless.keenable_search_keyless("q")

    assert calls[0]["url"].endswith("/v1/search/public")
    assert_contract(result, 1)


async def test_firecrawl_keyless_sends_no_authorization(monkeypatch):
    calls = stub_http(monkeypatch, lambda *_: response(json_body={"data": []}))

    result = await keyless.firecrawl_search_keyless("q")

    assert "Authorization" not in calls[0]["headers"]
    assert_contract(result, 0)


def test_rate_limit_detection_covers_the_vendor_wordings():
    for message in ("HTTP 429: slow down", "Quota exceeded", "too many requests"):
        assert keyless.is_rate_limitish(message)
    assert not keyless.is_rate_limitish("invalid query syntax")


async def test_the_ring_fails_over_past_a_throttled_vendor(monkeypatch):
    async def throttled(_query, _limit):
        return {"success": False, "error": "rate limit"}

    async def healthy(_query, _limit):
        return {"success": True, "data": {"web": []}}

    monkeypatch.setitem(keyless._KEYLESS_SEARCHERS, "exa", throttled)
    monkeypatch.setitem(keyless._KEYLESS_SEARCHERS, "parallel", healthy)

    result = await keyless.search_with_failover("exa", "q")

    assert result["success"] is True
    assert result["data"]["served_by"] == "parallel"


async def test_the_ring_stops_at_an_error_that_is_not_throttling(monkeypatch):
    tried = []

    async def broken(_query, _limit):
        tried.append("exa")
        return {"success": False, "error": "malformed query"}

    async def never(_query, _limit):
        raise AssertionError("the walk must stop at a non-throttle error")

    monkeypatch.setitem(keyless._KEYLESS_SEARCHERS, "exa", broken)
    monkeypatch.setitem(keyless._KEYLESS_SEARCHERS, "parallel", never)

    result = await keyless.search_with_failover("exa", "q")

    assert tried == ["exa"]
    assert result["error"] == "malformed query"


async def test_the_ring_reports_when_every_vendor_is_throttled(monkeypatch):
    async def throttled(_query, _limit):
        return {"success": False, "error": "429"}

    for vendor in keyless.KEYLESS_RING:
        monkeypatch.setitem(keyless._KEYLESS_SEARCHERS, vendor, throttled)

    result = await keyless.search_with_failover("exa", "q")

    assert result["success"] is False
    assert "all keyless vendors throttled" in result["error"]


async def test_a_paid_pin_removes_a_vendor_from_the_ring(monkeypatch, web_home):
    write_config(web_home, provider_tier=dict.fromkeys(keyless.KEYLESS_RING, "paid"))

    result = await keyless.search_with_failover("exa", "q")

    assert result == {
        "success": False,
        "error": "All keyless web providers are disabled or pinned to paid tiers.",
    }


def test_an_unpinned_ring_rotates_per_request(monkeypatch):
    monkeypatch.setattr(keyless.current_scope(), "cursor", [0])
    first = keyless.ring_order("exa")
    second = keyless.ring_order("exa")
    assert first[0] == "exa"
    assert second[0] == "parallel"


def test_a_pinned_vendor_always_starts_the_walk(monkeypatch, web_home):
    write_config(web_home, backend="keenable")
    monkeypatch.setattr(keyless.current_scope(), "cursor", [0])
    assert keyless.ring_order("keenable")[0] == "keenable"
    assert keyless.ring_order("keenable")[0] == "keenable"


def test_resolution_peeks_at_the_cursor_without_turning_it(monkeypatch):
    monkeypatch.setattr(keyless.current_scope(), "cursor", [2])
    assert keyless.keyless_walk_order()[0] == "firecrawl"
    assert keyless.keyless_walk_order()[0] == "firecrawl"


def test_a_keyless_tavily_failure_is_rescue_eligible(web_home):
    """Tavily left the ring, so its failure has not already walked every free tier."""
    write_config(web_home, backend="tavily")
    assert dispatch.serves_keyless(tavily.TavilyWebSearchProvider()) is False
    assert dispatch.rescue_eligible(tavily.TavilyWebSearchProvider()) is True


def test_a_keyless_ring_vendor_failure_is_not_rescue_eligible(web_home):
    write_config(web_home, backend="exa")
    assert dispatch.serves_keyless(exa.ExaWebSearchProvider()) is True
    assert dispatch.rescue_eligible(exa.ExaWebSearchProvider()) is False


def test_rescue_eligibility_never_raises(monkeypatch, web_home):
    """A config layer that throws must not turn a search failure into a crash."""
    write_config(web_home, backend="searxng")

    def boom(_provider):
        raise RuntimeError("config exploded")

    monkeypatch.setattr(dispatch, "serves_keyless", boom)
    assert dispatch.rescue_eligible(searxng.SearXNGWebSearchProvider()) is False


# ---------------------------------------------------------------------------
# Registry: availability and selection
# ---------------------------------------------------------------------------


class _Fake(WebSearchProvider):
    def __init__(self, name, available=False, keyless_ok=False):
        self._name = name
        self._available = available
        self._keyless = keyless_ok

    @property
    def name(self):
        return self._name

    def is_available(self):
        if self._available == "raise":
            raise RuntimeError("broken probe")
        return bool(self._available)

    def is_keyless_available(self):
        return bool(self._keyless)

    async def search(self, query, limit=5):
        return {"success": True, "data": {"web": []}}


def test_registration_rejects_a_non_provider():
    with pytest.raises(TypeError):
        registry.register_provider(object())


def test_registration_rejects_a_blank_name():
    with pytest.raises(ValueError, match="non-empty string"):
        registry.register_provider(_Fake("  "))


def test_re_registration_replaces_the_earlier_instance():
    first, second = _Fake("dup"), _Fake("dup")
    registry.register_provider(first)
    registry.register_provider(second)
    assert registry.get_provider("dup") is second
    assert [p.name for p in registry.list_providers()] == ["dup"]


def test_every_bundled_backend_registers():
    registry.ensure_backends_registered()
    assert [p.name for p in registry.list_providers()] == [
        "brave-free",
        "ddgs",
        "exa",
        "firecrawl",
        "keenable",
        "nous",
        "parallel",
        "perplexity",
        "searxng",
        "tavily",
        "xai",
    ]


def test_every_bundled_backend_names_its_credential():
    registry.ensure_backends_registered()
    for provider in registry.list_providers():
        hint = provider.get_setup_schema()
        assert hint["name"]
        assert isinstance(hint["env_vars"], list)
        for entry in hint["env_vars"]:
            assert entry["key"].isupper()
            assert entry["url"].startswith("https://")


@pytest.mark.parametrize(
    "backend,variable",
    [
        ("exa", "EXA_API_KEY"),
        ("parallel", "PARALLEL_API_KEY"),
        ("keenable", "KEENABLE_API_KEY"),
        ("firecrawl", "FIRECRAWL_API_KEY"),
        ("searxng", "SEARXNG_URL"),
        ("brave-free", "BRAVE_SEARCH_API_KEY"),
    ],
)
def test_availability_follows_the_credential(monkeypatch, backend, variable):
    assert registry.is_backend_available(backend) is False
    monkeypatch.setenv(variable, "value")
    assert registry.is_backend_available(backend) is True


def test_tavily_counts_as_available_when_explicitly_chosen(web_home):
    assert registry.is_backend_available("tavily") is False
    write_config(web_home, backend="tavily")
    assert registry.is_backend_available("tavily") is True


async def test_aborting_a_search_kills_the_ddgs_worker(monkeypatch):
    """The reap on CancelledError is only reachable if something actually cancels.

    Nothing did until web_search raced its dispatch against the abort signal: the agent
    loop awaits a tool call rather than running it as a task it can cancel, so this
    handler sat unreachable and a Ctrl-C left the child searching for a session that had
    stopped listening.
    """
    import asyncio

    source = "import sys, time\nsys.stdin.buffer.read()\ntime.sleep(30)\n"
    monkeypatch.setattr(ddgs_backend, "_worker_argv", lambda: [sys.executable, "-c", source])
    reaped: list = []
    real_reap = ddgs_backend._terminate_and_reap

    async def watched(proc):
        reaped.append(proc)
        await real_reap(proc)

    monkeypatch.setattr(ddgs_backend, "_terminate_and_reap", watched)

    call = asyncio.ensure_future(ddgs_backend._run_ddgs_search_bounded("q", 5))
    await asyncio.sleep(0.4)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call

    assert reaped, "the worker was left running"
    assert reaped[0].returncode is not None


def test_ddgs_worker_env_carries_no_credentials(monkeypatch):
    """The child hands one query to a third-party library; it needs no keys to do that."""
    monkeypatch.setenv("EXA_API_KEY", "exa-secret")
    monkeypatch.setenv("TAVILY_BASE_URL", "https://user:pw@tavily.internal")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")

    env = ddgs_backend._worker_env()

    assert "EXA_API_KEY" not in env
    assert "TAVILY_BASE_URL" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    # Networking and path stay: an allowlist that forgot these would break real installs.
    assert env["PATH"] == "/usr/bin"
    assert env["HTTPS_PROXY"] == "http://proxy.internal:3128"
    assert "PYTHONPATH" in env


def test_without_credentials_keeps_ordinary_variables():
    kept = config.without_credentials(
        {"LANG": "en_US.UTF-8", "MONKEY_API_KEY": "x", "TMPDIR": "/tmp", "npm_token": "y"}
    )
    assert kept == {"LANG": "en_US.UTF-8", "TMPDIR": "/tmp"}


def test_ddgs_availability_follows_the_package(monkeypatch):
    monkeypatch.setattr(registry, "ddgs_package_importable", lambda: True)
    assert registry.is_backend_available("ddgs") is True
    monkeypatch.setattr(registry, "ddgs_package_importable", lambda: False)
    assert registry.is_backend_available("ddgs") is False


def test_an_unknown_backend_is_never_available():
    assert registry.is_backend_available("no-such-vendor") is False


def test_a_credential_free_install_still_reaches_the_ring():
    registry.ensure_backends_registered()
    assert registry.backend_name() in keyless.KEYLESS_RING
    assert registry.web_search_available() is True


def test_disabling_the_keyless_tier_leaves_a_bare_install_with_nothing(web_home):
    write_config(web_home, keyless_fallback=False)
    registry.ensure_backends_registered()
    assert registry.backend_name() == "firecrawl"
    assert registry.web_search_available() is False


def test_the_credential_ladder_prefers_paid_over_free(monkeypatch):
    registry.ensure_backends_registered()
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "b")
    assert registry.backend_name() == "brave-free"
    monkeypatch.setenv("SEARXNG_URL", "http://localhost:8080")
    assert registry.backend_name() == "searxng"
    monkeypatch.setenv("EXA_API_KEY", "e")
    assert registry.backend_name() == "exa"
    monkeypatch.setenv("TAVILY_API_KEY", "t")
    assert registry.backend_name() == "tavily"


def test_a_stored_selection_is_final_even_when_it_is_wrong(web_home):
    write_config(web_home, backend="typo-backend")
    assert registry.backend_name() == "typo-backend"
    assert registry.search_backend_name() == "typo-backend"


def test_the_per_capability_key_wins_over_the_shared_one(web_home):
    write_config(web_home, backend="tavily", search_backend="searxng")
    assert registry.backend_name() == "tavily"
    assert registry.search_backend_name() == "searxng"


def test_an_externally_registered_provider_can_win_the_ladder():
    registry.ensure_backends_registered()
    registry.register_provider(_Fake("house-index", available=True))
    assert registry.backend_name() == "house-index"


def test_resolution_returns_a_configured_provider_even_when_unavailable():
    registry.register_provider(_Fake("chosen", available=False))
    registry.register_provider(_Fake("other", available=True))
    assert registry.resolve_search_provider("chosen").name == "chosen"


def test_resolution_falls_back_when_the_configured_name_is_unregistered():
    registry.register_provider(_Fake("only", available=True))
    assert registry.resolve_search_provider("ghost").name == "only"


def test_resolution_walks_the_legacy_preference_order():
    registry.register_provider(_Fake("ddgs", available=True))
    registry.register_provider(_Fake("tavily", available=True))
    registry.register_provider(_Fake("searxng", available=True))
    assert registry.resolve_search_provider(None).name == "tavily"


def test_resolution_reaches_keyless_only_when_nothing_is_available():
    registry.register_provider(_Fake("exa", available=False, keyless_ok=True))
    registry.register_provider(_Fake("brave-free", available=False))
    assert registry.resolve_search_provider(None).name == "exa"


def test_resolution_skips_a_provider_whose_probe_raises():
    registry.register_provider(_Fake("brave-free", available="raise"))
    registry.register_provider(_Fake("searxng", available=True))
    assert registry.resolve_search_provider(None).name == "searxng"


def test_readiness_accepts_keyless_and_rejects_nothing_at_all():
    assert registry.provider_is_ready(None) is False
    assert registry.provider_is_ready(_Fake("x", available=False)) is False
    assert registry.provider_is_ready(_Fake("x", keyless_ok=True)) is True
    assert registry.provider_is_ready(_Fake("x", available="raise")) is False


# ---------------------------------------------------------------------------
# Dispatch and the one-shot rescue
# ---------------------------------------------------------------------------


class _Failing(_Fake):
    async def search(self, query, limit=5):
        return {"success": False, "error": "backend exploded"}


class _Raising(_Fake):
    async def search(self, query, limit=5):
        raise RuntimeError("connection reset")


async def test_dispatch_returns_what_the_backend_returned(web_home):
    write_config(web_home, backend="house")
    registry.register_provider(_Fake("house", available=True))

    result = await dispatch.web_search("q")

    assert result == {"success": True, "data": {"web": []}}


async def test_dispatch_names_a_selection_that_matches_nothing(web_home):
    write_config(web_home, backend="ghost-vendor")

    result = await dispatch.web_search("q")

    assert result["success"] is False
    assert "ghost-vendor" in result["error"]
    assert config.config_label() in result["error"]      # the file in effect, not a fixed spelling


async def test_dispatch_reports_when_nothing_can_serve(web_home, monkeypatch):
    # A stripped install: no backend registered at all, and no keyless tier to fall to.
    # Both entry points into registration are stubbed -- the dispatcher calls one and the
    # selection ladder calls the other, exactly as Hermes' `_ensure_web_plugins_loaded()`
    # is called from both places.
    write_config(web_home, keyless_fallback=False)
    monkeypatch.setattr(dispatch, "ensure_backends_registered", lambda: None)
    monkeypatch.setattr(registry, "ensure_backends_registered", lambda: None)

    result = await dispatch.web_search("q")

    assert result["success"] is False
    assert result["error"].startswith("No web search provider configured")


async def test_a_failed_keyed_backend_gets_one_ring_rescue(monkeypatch, web_home):
    write_config(web_home, backend="searxng")
    registry.register_provider(_Failing("searxng", available=True))

    async def ring(name, query, limit):
        return {"success": True, "data": {"web": [], "served_by": "exa"}}

    monkeypatch.setattr(dispatch, "search_with_failover", ring)

    result = await dispatch.web_search("q")

    assert result["success"] is True
    assert result["data"]["rescued_from"] == "searxng"
    assert "backend exploded" in result["data"]["backend_error"]


async def test_a_raised_failure_is_rescued_too(monkeypatch, web_home):
    write_config(web_home, backend="searxng")
    registry.register_provider(_Raising("searxng", available=True))

    async def ring(_name, _query, _limit):
        return {"success": True, "data": {"web": []}}

    monkeypatch.setattr(dispatch, "search_with_failover", ring)

    result = await dispatch.web_search("q")

    assert result["data"]["rescued_from"] == "searxng"
    assert "connection reset" in result["data"]["backend_error"]


async def test_a_failed_rescue_keeps_the_original_error(monkeypatch, web_home):
    write_config(web_home, backend="searxng")
    registry.register_provider(_Failing("searxng", available=True))

    async def ring(_name, _query, _limit):
        return {"success": False, "error": "everything throttled"}

    monkeypatch.setattr(dispatch, "search_with_failover", ring)

    result = await dispatch.web_search("q")

    assert result["success"] is False
    assert "backend exploded" in result["error"]
    assert "everything throttled" in result["error"]


async def test_a_keyless_ring_vendor_is_not_rescued_twice(monkeypatch, web_home):
    write_config(web_home, backend="exa")
    provider = exa.ExaWebSearchProvider()

    async def failed_ring(*_args):
        return {"success": False, "error": "backend exploded"}

    monkeypatch.setattr(provider, "search", failed_ring)
    registry.register_provider(provider)

    async def never(*_args):
        raise AssertionError("the ring was already walked; do not walk it again")

    monkeypatch.setattr(dispatch, "search_with_failover", never)

    result = await dispatch.web_search("q")

    assert result == {"success": False, "error": "backend exploded"}


async def test_a_keyed_ring_vendor_is_rescued(monkeypatch, web_home):
    write_config(web_home, backend="exa")
    monkeypatch.setenv("EXA_API_KEY", "e")
    registry.register_provider(_Failing("exa", available=True))

    async def ring(_name, _query, _limit):
        return {"success": True, "data": {"web": []}}

    monkeypatch.setattr(dispatch, "search_with_failover", ring)

    result = await dispatch.web_search("q")

    assert result["data"]["rescued_from"] == "exa"


async def test_the_rescue_can_be_turned_off(monkeypatch, web_home):
    write_config(web_home, backend="searxng", keyless_rescue=False)
    registry.register_provider(_Failing("searxng", available=True))

    async def never(*_args):
        raise AssertionError("keyless_rescue is off")

    monkeypatch.setattr(dispatch, "search_with_failover", never)

    result = await dispatch.web_search("q")

    assert result == {"success": False, "error": "backend exploded"}


async def test_disabling_the_keyless_tier_also_disables_the_rescue(monkeypatch, web_home):
    write_config(web_home, backend="searxng", keyless_fallback=False)
    registry.register_provider(_Failing("searxng", available=True))

    async def never(*_args):
        raise AssertionError("the keyless tier is off")

    monkeypatch.setattr(dispatch, "search_with_failover", never)

    assert (await dispatch.web_search("q"))["success"] is False

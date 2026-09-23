"""The five extract-capable backends: what they send, and what they hand back.

Nothing here touches the network or the resolver. Vendor HTTP is stubbed at
``httpx.AsyncClient`` the way :mod:`tests.test_web_providers` stubs it, Parallel's optional
SDK is a module planted in ``sys.modules``, and Firecrawl's SSRF gate is stubbed public by
default -- so what runs is the real provider code: the normalizers, the re-keying, the
policy gates and the per-URL error branches, rather than mocks of them.

The assertion every vendor repeats is the contract one: a batch answers with one entry per
requested URL, in the order it was asked for, and a page that failed is an entry carrying
``error`` rather than a hole. The tool above reassembles its argument list by position, so
a backend that drops or reorders an entry hands the model one page's text under another
page's address.
"""

from __future__ import annotations

import sys

import httpx
import pytest
from webconf import write_web

from misaka.config import home
from misaka.core.web import keyless, website_policy
from misaka.core.web.backends import exa, firecrawl, keenable, parallel, tavily
from misaka.core.web.bounded import UnsafeUrlError

_VENDOR_ENV = (
    "EXA_API_KEY",
    "FIRECRAWL_API_KEY",
    "FIRECRAWL_API_URL",
    "KEENABLE_API_KEY",
    "PARALLEL_API_KEY",
    "TAVILY_API_KEY",
    "TAVILY_BASE_URL",
)

THREE = ["https://a.test/one", "https://b.test/two", "https://c.test/three"]


@pytest.fixture(autouse=True)
def web_home(monkeypatch, tmp_path):
    """A throwaway web config and an empty vendor environment for every test."""
    for name in _VENDOR_ENV:
        monkeypatch.delenv(name, raising=False)
    path = home.path("settings")           # the "web" section lives here now
    # The blocklist is cached for 30s keyed on the config path; a test that writes one
    # must not inherit the previous test's answer, nor leave its own behind.
    website_policy.invalidate_cache()
    monkeypatch.setattr(keyless.current_scope(), "cursor", [0])
    yield path
    website_policy.invalidate_cache()


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    """Firecrawl's post-scrape SSRF gate, stubbed to "public" and resolving nothing.

    :func:`misaka.core.web.bounded.vet_public_url` calls ``getaddrinfo``. The one
    test that cares about the gate re-patches this with a raiser.
    """

    async def _vetted(url, *, proxy=None):
        return ("93.184.216.34",)

    monkeypatch.setattr(firecrawl, "vet_public_url", _vetted)


def write_config(_path, **keys) -> None:
    write_web(keys)


# ---------------------------------------------------------------------------
# HTTP stubbing (the shape tests/test_web_providers.py established)
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


def assert_positional(entries, urls):
    """Every extract answer must be one entry per requested URL, in order."""
    assert [entry["url"] for entry in entries] == list(urls)
    for entry in entries:
        assert set(entry) >= {"url", "title", "content", "raw_content", "metadata"}
        assert isinstance(entry["content"], str)
        assert isinstance(entry["raw_content"], str)
        assert entry["metadata"]["sourceURL"]


def keyless_spy(monkeypatch, module):
    """Record the ring hand-off *module* makes instead of performing one."""
    seen: dict = {}

    async def fake(name, urls):
        seen["call"] = (name, list(urls))
        return [{"url": url, "title": "", "content": "ring", "raw_content": "ring"} for url in urls]

    monkeypatch.setattr(module, "extract_with_failover", fake)
    return seen


# ---------------------------------------------------------------------------
# Exa -- POST /contents, batched
# ---------------------------------------------------------------------------


async def test_exa_extract_posts_the_url_list_to_contents(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "exa-key")
    body = {"results": [{"url": THREE[0], "title": "One", "text": "first page"}]}
    calls = stub_http(monkeypatch, lambda *_: response(json_body=body))

    out = await exa.ExaWebSearchProvider().extract([THREE[0]])

    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "https://api.exa.ai/contents"
    assert calls[0]["json"] == {"urls": [THREE[0]], "text": True}
    assert calls[0]["headers"]["x-api-key"] == "exa-key"
    assert calls[0]["headers"]["x-exa-integration"] == "misaka-agent"
    assert out == [
        {
            "url": THREE[0],
            "title": "One",
            # Hermes maps result.text to BOTH; Exa offers no second rendition.
            "content": "first page",
            "raw_content": "first page",
            "metadata": {"sourceURL": THREE[0], "title": "One"},
        }
    ]


async def test_exa_extract_keeps_a_missing_page_in_position(monkeypatch):
    """Exa answers short and out of order; the reply is re-keyed onto the request."""
    monkeypatch.setenv("EXA_API_KEY", "k")
    body = {
        "results": [
            {"url": THREE[2], "title": "C", "text": "third"},
            {"url": THREE[0], "title": "A", "text": "first"},
        ]
    }
    stub_http(monkeypatch, lambda *_: response(json_body=body))

    out = await exa.ExaWebSearchProvider().extract(THREE)

    assert_positional(out, THREE)
    assert out[0]["content"] == "first"
    assert out[1]["error"] == "no content returned"
    assert out[1]["content"] == ""
    assert out[2]["content"] == "third"
    assert "error" not in out[0] and "error" not in out[2]


async def test_exa_extract_preserves_a_page_without_a_request_mapping(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "k")
    body = {
        "results": [
            {"url": THREE[0], "title": "A", "text": "first"},
            {"url": "https://uninvited.test", "title": "X", "text": "spam"},
        ]
    }
    stub_http(monkeypatch, lambda *_: response(json_body=body))

    out = await exa.ExaWebSearchProvider().extract([THREE[0]])

    assert_positional(out[:1], [THREE[0]])
    assert len(out) == 2 and out[1]["requested_url"] is None
    assert out[1]["content"] == "spam"


async def test_exa_extract_raises_when_the_key_is_rejected(monkeypatch):
    """A rejected key is a whole-backend failure: raise, so the dispatcher can rescue."""
    monkeypatch.setenv("EXA_API_KEY", "bad")
    stub_http(monkeypatch, lambda *_: response(401, text="unauthorized"))

    with pytest.raises(ValueError, match="unauthorized"):
        await exa.ExaWebSearchProvider().extract([THREE[0]])


async def test_exa_extract_without_a_key_or_a_free_tier_refuses_every_url(web_home):
    write_config(web_home, keyless_fallback=False)

    out = await exa.ExaWebSearchProvider().extract(THREE)

    assert_positional(out, THREE)
    assert all("EXA_API_KEY" in entry["error"] for entry in out)


async def test_exa_extract_without_a_key_walks_the_ring(monkeypatch):
    seen = keyless_spy(monkeypatch, exa)

    out = await exa.ExaWebSearchProvider().extract([THREE[0]])

    assert seen["call"] == ("exa", [THREE[0]])
    assert out[0]["content"] == "ring"


async def test_exa_extract_honours_a_free_tier_pin_over_a_key(monkeypatch, web_home):
    """The routing decision search makes, made once, in :func:`config.use_keyless`."""
    monkeypatch.setenv("EXA_API_KEY", "a-real-key")
    write_config(web_home, provider_tier={"exa": "free"})
    seen = keyless_spy(monkeypatch, exa)

    await exa.ExaWebSearchProvider().extract([THREE[0]])

    assert seen["call"] == ("exa", [THREE[0]])


# ---------------------------------------------------------------------------
# Keenable -- GET /v1/fetch, per URL
# ---------------------------------------------------------------------------


def _keenable_pages(failures: dict[str, tuple[int, str]] | None = None):
    failures = failures or {}
    def handler(_method, _url, kwargs):
        target = kwargs["params"]["url"]
        if target in failures:
            status, text = failures[target]
            return response(status, text=text)
        return response(json_body={"url": target, "title": f"T {target}", "content": f"body {target}"})
    return handler


async def test_keenable_extract_fetches_each_url_in_turn(monkeypatch):
    monkeypatch.setenv("KEENABLE_API_KEY", "kee-key")
    calls = stub_http(monkeypatch, _keenable_pages())

    out = await keenable.KeenableWebSearchProvider().extract([THREE[0]])

    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == "https://api.keenable.ai/v1/fetch"
    assert calls[0]["params"] == {"url": THREE[0]}
    assert calls[0]["headers"]["Authorization"] == "Bearer kee-key"
    assert calls[0]["headers"]["X-Keenable-Title"] == "misaka-agent"
    assert out == [
        {
            "url": THREE[0],
            "title": f"T {THREE[0]}",
            "content": f"body {THREE[0]}",
            "raw_content": f"body {THREE[0]}",
            "metadata": {"sourceURL": THREE[0], "title": f"T {THREE[0]}"},
        }
    ]


async def test_keenable_extract_keeps_a_failed_page_in_position(monkeypatch):
    monkeypatch.setenv("KEENABLE_API_KEY", "k")
    calls = stub_http(monkeypatch, _keenable_pages({THREE[1]: (502, "upstream exploded")}))

    out = await keenable.KeenableWebSearchProvider().extract(THREE)

    assert_positional(out, THREE)
    assert len(calls) == 3  # one page failing does not stop the batch
    assert "upstream exploded" in out[1]["error"]
    assert out[0]["content"] == f"body {THREE[0]}"
    assert out[2]["content"] == f"body {THREE[2]}"


async def test_keenable_extract_files_a_redirect_under_the_requested_url(monkeypatch):
    """The divergence keyless_mcp's port already records: position beats the echo."""
    monkeypatch.setenv("KEENABLE_API_KEY", "k")
    stub_http(
        monkeypatch,
        lambda *_: response(
            json_body={"url": "https://elsewhere.test/moved", "title": "T", "content": "body"}
        ),
    )

    out = await keenable.KeenableWebSearchProvider().extract([THREE[0]])

    assert out[0]["url"] == THREE[0]


async def test_keenable_extract_without_a_key_walks_the_ring(monkeypatch):
    seen = keyless_spy(monkeypatch, keenable)

    await keenable.KeenableWebSearchProvider().extract([THREE[0]])

    assert seen["call"] == ("keenable", [THREE[0]])


# ---------------------------------------------------------------------------
# Tavily -- POST /extract, batched, three result lists
# ---------------------------------------------------------------------------


async def test_tavily_extract_posts_the_url_list(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-key")
    body = {"results": [{"url": THREE[0], "title": "A", "raw_content": "long", "content": "short"}]}
    calls = stub_http(monkeypatch, lambda *_: response(json_body=body))

    out = await tavily.TavilyWebSearchProvider().extract([THREE[0]])

    assert calls[0]["url"] == "https://api.tavily.com/extract"
    assert calls[0]["json"] == {"urls": [THREE[0]], "include_images": False}
    assert calls[0]["headers"]["Authorization"] == "Bearer tvly-key"
    # raw_content wins: it is the same page, untruncated.
    assert out[0]["content"] == "long"
    assert out[0]["raw_content"] == "long"
    assert out[0]["metadata"] == {"sourceURL": THREE[0], "title": "A"}


def test_tavily_normaliser_walks_all_three_lists():
    raw = {
        "results": [{"url": THREE[0], "title": "A", "content": "short", "raw_content": "long"}],
        "failed_results": [{"url": THREE[1], "error": "paywalled"}],
        "failed_urls": [THREE[2]],
    }

    docs = tavily.normalize_extract_documents(raw, THREE)

    assert_positional(docs, THREE)
    assert docs[0]["content"] == "long"
    assert docs[1]["error"] == "paywalled"
    assert docs[2]["error"] == "extraction failed"


def test_tavily_normaliser_backfills_a_url_no_list_named():
    docs = tavily.normalize_extract_documents({"results": []}, THREE)

    assert_positional(docs, THREE)
    assert all(doc["error"] == "no content returned" for doc in docs)


def test_tavily_normaliser_only_guesses_for_a_single_url_request():
    """Hermes' fallback_url, kept where it is defensible and dropped where it is not."""
    unlabelled = {"results": [{"title": "A", "content": "text"}]}

    alone = tavily.normalize_extract_documents(unlabelled, [THREE[0]])
    assert alone[0]["content"] == "text"

    batched = tavily.normalize_extract_documents(unlabelled, THREE)
    assert [doc["error"] for doc in batched[:3]] == ["no content returned"] * 3
    assert batched[3]["requested_url"] is None and batched[3]["content"] == "text"


async def test_tavily_extract_keeps_a_failed_page_in_position(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    body = {
        "results": [
            {"url": THREE[2], "title": "C", "content": "third"},
            {"url": THREE[0], "title": "A", "content": "first"},
        ],
        "failed_results": [{"url": THREE[1], "error": "robots.txt"}],
    }
    stub_http(monkeypatch, lambda *_: response(json_body=body))

    out = await tavily.TavilyWebSearchProvider().extract(THREE)

    assert_positional(out, THREE)
    assert out[0]["content"] == "first"
    assert out[1]["error"] == "robots.txt"
    assert out[2]["content"] == "third"


async def test_tavily_extract_keyless_uses_tavilys_own_endpoint(monkeypatch):
    """Tavily is not a ring member: keyless is its endpoint with one header changed."""
    calls = stub_http(monkeypatch, lambda *_: response(json_body={"results": []}))

    await tavily.TavilyWebSearchProvider().extract([THREE[0]])

    assert calls[0]["url"] == "https://api.tavily.com/extract"
    assert calls[0]["headers"]["X-Tavily-Access-Mode"] == "keyless"
    assert "Authorization" not in calls[0]["headers"]


async def test_tavily_extract_without_a_key_or_keyless_refuses_every_url(web_home):
    write_config(web_home, keyless_fallback=False)

    out = await tavily.TavilyWebSearchProvider().extract(THREE)

    assert_positional(out, THREE)
    assert all("TAVILY_API_KEY is not set" in entry["error"] for entry in out)


async def test_tavily_extract_raises_when_the_key_is_rejected(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "bad")
    stub_http(monkeypatch, lambda *_: response(401, text="invalid api key"))

    with pytest.raises(ValueError, match="invalid api key"):
        await tavily.TavilyWebSearchProvider().extract([THREE[0]])


# ---------------------------------------------------------------------------
# Parallel -- the REST contract
# ---------------------------------------------------------------------------


async def test_parallel_extract_requests_full_content_without_sdk(monkeypatch):
    monkeypatch.setenv("PARALLEL_API_KEY", "par-key")
    monkeypatch.setitem(sys.modules, "parallel", None)
    calls = stub_http(monkeypatch, lambda *_args: response(json_body={"results": [], "errors": []}))
    out = await parallel.ParallelWebSearchProvider().extract([THREE[0]])
    assert calls[0]["url"] == "https://api.parallel.ai/v1beta/extract"
    assert calls[0]["headers"]["x-api-key"] == "par-key"
    assert calls[0]["headers"]["parallel-beta"] == "search-extract-2025-10-10"
    assert calls[0]["json"] == {"urls": [THREE[0]], "full_content": True}
    assert out[0]["error"] == "no content returned"


async def test_parallel_extract_maps_full_content_then_excerpts(monkeypatch):
    monkeypatch.setenv("PARALLEL_API_KEY", "k")
    reply = {
        "results": [
            {"url": THREE[0], "title": "A", "full_content": "whole page", "excerpts": []},
            {"url": THREE[1], "title": "B", "full_content": "", "excerpts": ["one", "two"]},
        ],
        "errors": [],
    }
    monkeypatch.setattr(parallel, "_keyed_extract", lambda *_args: _resolved(reply))

    out = await parallel.ParallelWebSearchProvider().extract(THREE[:2])

    assert out[0]["content"] == "whole page"
    assert out[1]["content"] == "one\n\ntwo"


async def test_parallel_extract_reads_the_errors_list_into_position(monkeypatch):
    monkeypatch.setenv("PARALLEL_API_KEY", "k")
    reply = {
        "results": [
            {"url": THREE[2], "title": "C", "full_content": "third", "excerpts": []},
            {"url": THREE[0], "title": "A", "full_content": "first", "excerpts": []},
        ],
        "errors": [{"url": THREE[1], "content": "fetch refused", "error_type": "http_403"}],
    }
    monkeypatch.setattr(parallel, "_keyed_extract", lambda *_args: _resolved(reply))

    out = await parallel.ParallelWebSearchProvider().extract(THREE)

    assert_positional(out, THREE)
    assert out[0]["content"] == "first"
    assert out[1]["error"] == "fetch refused"
    assert out[2]["content"] == "third"


async def test_parallel_extract_falls_back_to_the_error_type(monkeypatch):
    monkeypatch.setenv("PARALLEL_API_KEY", "k")
    reply = {
        "results": [],
        "errors": [{"url": THREE[0], "content": "", "error_type": "timeout"}],
    }
    monkeypatch.setattr(parallel, "_keyed_extract", lambda *_args: _resolved(reply))

    out = await parallel.ParallelWebSearchProvider().extract([THREE[0]])

    assert out[0]["error"] == "timeout"


async def test_parallel_extract_http_failure_reaches_dispatch(monkeypatch):
    monkeypatch.setenv("PARALLEL_API_KEY", "k")
    stub_http(monkeypatch, lambda *_args: response(401, text="bad key"))
    with pytest.raises(httpx.HTTPStatusError):
        await parallel.ParallelWebSearchProvider().extract(THREE)


async def test_parallel_extract_without_a_key_walks_the_ring(monkeypatch):
    seen = keyless_spy(monkeypatch, parallel)

    await parallel.ParallelWebSearchProvider().extract([THREE[0]])

    assert seen["call"] == ("parallel", [THREE[0]])


async def _resolved(value):
    """Await-able constant: the test result for a patched ``_keyed_extract``."""
    return value


# ---------------------------------------------------------------------------
# Firecrawl -- POST /v2/scrape per URL, with both gates and the format selection
# ---------------------------------------------------------------------------


def _scraped(url="https://ok.test/page", *, markdown="MD", html="<p>HTML</p>", title="T"):
    """A cloud-shaped scrape reply: the page object nested under ``data``."""
    return {
        "success": True,
        "data": {
            "markdown": markdown,
            "html": html,
            "metadata": {"title": title, "sourceURL": url},
        },
    }


def _firecrawl_pages(failures=None):
    failures = failures or {}
    def handler(_method, _url, kwargs):
        target = kwargs["json"]["url"]
        if target in failures:
            return failures[target]
        return response(json_body=_scraped(target, markdown=f"md {target}"))
    return handler


async def test_firecrawl_extract_posts_url_and_formats(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-key")
    calls = stub_http(monkeypatch, _firecrawl_pages())

    out = await firecrawl.FirecrawlWebSearchProvider().extract([THREE[0]])

    assert calls[0]["url"] == "https://api.firecrawl.dev/v2/scrape"
    assert calls[0]["json"] == {"url": THREE[0], "formats": ["markdown", "html"], "maxAge": 14_400_000, "onlyMainContent": True, "mobile": False,
                                    "skipTlsVerification": True, "removeBase64Images": True,
                                    "fastMode": False, "blockAds": True, "storeInCache": True}
    assert calls[0]["headers"]["Authorization"] == "Bearer fc-key"
    assert out[0]["content"] == f"md {THREE[0]}"
    assert out[0]["raw_content"] == f"md {THREE[0]}"
    assert out[0]["metadata"]["sourceURL"] == THREE[0]


async def test_firecrawl_extract_uses_a_self_hosted_instance(monkeypatch):
    """A private instance is a deliberate setup: never traded for the public cloud."""
    monkeypatch.setenv("FIRECRAWL_API_URL", "http://localhost:3002/")
    calls = stub_http(monkeypatch, _firecrawl_pages())

    await firecrawl.FirecrawlWebSearchProvider().extract([THREE[0]])

    assert calls[0]["url"] == "http://localhost:3002/v2/scrape"
    assert "Authorization" not in calls[0]["headers"]


@pytest.mark.parametrize(
    ("requested", "sent", "markdown", "html", "expected"),
    [
        ("markdown", ["markdown"], "MD", "<p>HTML</p>", "MD"),
        ("html", ["html"], "MD", "<p>HTML</p>", "<p>HTML</p>"),
        (None, ["markdown", "html"], "MD", "<p>HTML</p>", "MD"),
        # None with nothing in markdown falls through to html.
        (None, ["markdown", "html"], "", "<p>HTML</p>", "<p>HTML</p>"),
        # Prefer markdown; keep usable HTML if the vendor returned only that.
        ("markdown", ["markdown"], "", "<p>HTML</p>", "<p>HTML</p>"),
        # An unrecognised format asks for both and prefers html, as in Hermes.
        ("pdf", ["markdown", "html"], "MD", "<p>HTML</p>", "<p>HTML</p>"),
    ],
)
async def test_firecrawl_format_selection(monkeypatch, requested, sent, markdown, html, expected):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "k")
    calls = stub_http(
        monkeypatch,
        lambda *_: response(json_body=_scraped(THREE[0], markdown=markdown, html=html)),
    )

    out = await firecrawl.FirecrawlWebSearchProvider().extract([THREE[0]], format=requested)

    assert calls[0]["json"]["formats"] == sent
    assert out[0]["content"] == expected


async def test_firecrawl_reads_a_payload_that_is_not_nested(monkeypatch):
    """Self-hosted builds answer with the page object at the top level."""
    monkeypatch.setenv("FIRECRAWL_API_KEY", "k")
    flat = {"success": True, "markdown": "plain", "metadata": {"title": "T", "sourceURL": THREE[0]}}
    stub_http(monkeypatch, lambda *_: response(json_body=flat))

    out = await firecrawl.FirecrawlWebSearchProvider().extract([THREE[0]])

    assert out[0]["content"] == "plain"
    assert out[0]["title"] == "T"


async def test_firecrawl_keeps_a_failed_scrape_in_position(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "k")
    calls = stub_http(
        monkeypatch, _firecrawl_pages({THREE[1]: response(500, text="render crashed")})
    )

    out = await firecrawl.FirecrawlWebSearchProvider().extract(THREE)

    assert_positional(out, THREE)
    assert len(calls) == 3
    assert "render crashed" in out[1]["error"]
    assert out[0]["content"] == f"md {THREE[0]}"
    assert out[2]["content"] == f"md {THREE[2]}"


@pytest.mark.parametrize(
    "raised",
    [
        httpx.ReadTimeout("slow", request=httpx.Request("POST", "https://api.firecrawl.dev")),
        TimeoutError(),
    ],
)
async def test_firecrawl_reports_its_per_url_timeout(monkeypatch, raised):
    """Both spellings of "too slow": httpx's own, and the one asyncio.timeout raises."""
    monkeypatch.setenv("FIRECRAWL_API_KEY", "k")
    stub_http(monkeypatch, _firecrawl_pages({THREE[1]: raised}))

    out = await firecrawl.FirecrawlWebSearchProvider().extract(THREE)

    assert_positional(out, THREE)
    assert out[1]["error"] == (
        "Scrape reached its configured HTTP or operation timeout -- page may be too large or unresponsive. "
        "Try web_fetch instead."
    )
    assert out[0]["content"] == f"md {THREE[0]}"


async def test_firecrawl_refuses_a_blocklisted_host_before_scraping(monkeypatch, web_home):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "k")
    write_config(web_home, website_blocklist={"enabled": True, "domains": ["b.test"]})
    website_policy.invalidate_cache()
    calls = stub_http(monkeypatch, _firecrawl_pages())

    out = await firecrawl.FirecrawlWebSearchProvider().extract(THREE)

    assert_positional(out, THREE)
    assert out[1]["blocked_by_policy"] == {
        "host": "b.test",
        "rule": "b.test",
        "source": "config",
    }
    assert "Blocked by website policy" in out[1]["error"]
    # A refusal must cost no vendor request.
    assert [call["json"]["url"] for call in calls] == [THREE[0], THREE[2]]


async def test_firecrawl_refuses_a_final_url_that_resolves_privately(monkeypatch):
    """The vendor followed the redirect on its own servers; we re-check where it landed."""
    monkeypatch.setenv("FIRECRAWL_API_KEY", "k")

    async def _unsafe(_url, *, proxy=None):
        raise UnsafeUrlError("resolves to a private address")

    monkeypatch.setattr(firecrawl, "vet_public_url", _unsafe)
    stub_http(
        monkeypatch,
        lambda *_: response(json_body=_scraped("http://169.254.169.254/latest/meta-data")),
    )

    out = await firecrawl.FirecrawlWebSearchProvider().extract([THREE[0]])

    assert out[0]["url"] == THREE[0]
    assert out[0]["content"] == ""
    assert "refused by the outbound URL check" in out[0]["error"]
    assert "resolves to a private address" in out[0]["error"]
    assert out[0]["metadata"]["sourceURL"] == "http://169.254.169.254/latest/meta-data"


async def test_firecrawl_refuses_a_final_url_the_blocklist_names(monkeypatch, web_home):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "k")
    write_config(web_home, website_blocklist={"enabled": True, "domains": ["blocked.test"]})
    website_policy.invalidate_cache()
    stub_http(monkeypatch, lambda *_: response(json_body=_scraped("https://blocked.test/moved")))

    out = await firecrawl.FirecrawlWebSearchProvider().extract(["https://allowed.test/start"])

    assert out[0]["url"] == "https://allowed.test/start"
    assert out[0]["blocked_by_policy"]["host"] == "blocked.test"
    assert out[0]["metadata"]["sourceURL"] == "https://blocked.test/moved"


async def test_firecrawl_extract_without_credentials_walks_the_ring(monkeypatch):
    seen = keyless_spy(monkeypatch, firecrawl)

    await firecrawl.FirecrawlWebSearchProvider().extract([THREE[0]])

    assert seen["call"] == ("firecrawl", [THREE[0]])


async def test_firecrawl_extract_with_the_ring_off_refuses_every_url(web_home):
    write_config(web_home, keyless_fallback=False)

    out = await firecrawl.FirecrawlWebSearchProvider().extract(THREE)

    assert_positional(out, THREE)
    assert all("FIRECRAWL_API_KEY" in entry["error"] for entry in out)


# ---------------------------------------------------------------------------
# All five, one assertion
# ---------------------------------------------------------------------------


def test_every_extract_backend_advertises_the_capability():
    providers = [
        exa.ExaWebSearchProvider(),
        firecrawl.FirecrawlWebSearchProvider(),
        keenable.KeenableWebSearchProvider(),
        parallel.ParallelWebSearchProvider(),
        tavily.TavilyWebSearchProvider(),
    ]
    assert all(provider.supports_extract() for provider in providers)
    assert all(provider.supports_search() for provider in providers)

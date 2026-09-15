"""The keyless ring's extract half: four vendors, one positional contract, one ring walk.

Nothing here touches the network -- ``httpx.AsyncClient`` is stubbed the way
tests/test_web_providers.py stubs it, so what runs is the real vendor code (request
shapes, payload normalizers, the back-fill, the failover rule) rather than mocks of it.

Ported from the extract cases in Hermes' ``tests/tools/test_web_keyless_fallback.py``
(``test_parallel_extract_covers_missing_urls``, ``test_exa_extract_per_url``,
``test_extract_fails_over_when_all_urls_throttled``,
``test_extract_partial_failure_stays_on_primary``), with the rest written for MISAKA's
own rules: order parity, the contract keys on every entry, and the all-throttled note.
"""

from __future__ import annotations

import json

import httpx
import pytest

from misaka.config.product import CFG
from misaka.core.tools._web import bounded
from misaka.core.web import keyless


@pytest.fixture(autouse=True)
def web_home(monkeypatch, tmp_path):
    """A throwaway web config and a pinned ring cursor for every test."""
    async def resolve(*_args):
        return ["93.184.216.34"]
    monkeypatch.setattr(bounded, "_resolve_host", resolve)
    path = tmp_path / "web.json"
    monkeypatch.setitem(CFG, "web_config", str(path))
    # The cursor is random per process; pin it so the walk order is assertable.
    monkeypatch.setattr(keyless.current_scope(), "cursor", [0])
    return path


def write_config(path, **keys) -> None:
    path.write_text(json.dumps(keys), encoding="utf-8")


# ---------------------------------------------------------------------------
# HTTP stubbing (the idiom of tests/test_web_providers.py, which owns the original)
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self, handler, calls):
        self._handler = handler
        self._calls = calls

    async def __aenter__(self):
        return self

    async def aclose(self):
        pass

    async def __aexit__(self, *_exc):
        return False

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


def mcp_body(text):
    return {"result": {"content": [{"type": "text", "text": text}]}}


def assert_entries(entries, urls):
    """Every extract reply is one entry per requested URL, in order, fully shaped."""
    assert [entry["url"] for entry in entries] == list(urls)
    for entry in entries:
        assert set(entry) >= {"url", "title", "content", "raw_content"}
        for key in ("url", "title", "content", "raw_content"):
            assert isinstance(entry[key], str)
        if entry.get("error"):
            assert entry["content"] == ""
            assert entry["raw_content"] == ""


def entry(url, *, title="T", content="body"):
    return {
        "url": url,
        "title": title,
        "content": content,
        "raw_content": content,
        "metadata": {"sourceURL": url, "title": title},
    }


def failed(url, error):
    return {
        "url": url,
        "title": "",
        "content": "",
        "raw_content": "",
        "error": error,
        "metadata": {"sourceURL": url},
    }


# ---------------------------------------------------------------------------
# Parallel: the one batched member
# ---------------------------------------------------------------------------

URLS = ["https://a.test", "https://b.test"]


async def test_parallel_extract_sends_one_batched_web_fetch(monkeypatch):
    payload = {"results": [{"url": URLS[0], "title": "A", "full_content": "body a"}]}
    calls = stub_http(monkeypatch, lambda *_: response(json_body=mcp_body(json.dumps(payload))))

    out = await keyless.parallel_extract_keyless([URLS[0]])

    assert len(calls) == 1
    params = calls[0]["json"]["params"]
    assert params["name"] == "web_fetch"
    assert params["arguments"] == {
        "urls": [URLS[0]],
        "objective": "Full page content",
        "session_id": keyless._SESSION_ID,
    }
    assert_entries(out, [URLS[0]])
    assert out[0]["title"] == "A"
    assert out[0]["content"] == "body a"
    assert out[0]["raw_content"] == "body a"
    assert out[0]["metadata"] == {"sourceURL": URLS[0], "title": "A", "content_kind": "page_text"}


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({"full_content": "full", "content": "short", "excerpts": ["x"]}, "full"),
        ({"content": "short", "excerpts": ["x"]}, "short"),
        ({"excerpts": ["one", "two"]}, "one\n\ntwo"),
        ({}, ""),
    ],
)
async def test_parallel_extract_prefers_the_fullest_content_field(monkeypatch, record, expected):
    payload = {"results": [{"url": URLS[0], **record}]}
    stub_http(monkeypatch, lambda *_: response(json_body=mcp_body(json.dumps(payload))))

    out = await keyless.parallel_extract_keyless([URLS[0]])

    assert out[0]["content"] == expected


async def test_parallel_extract_backfills_a_url_the_endpoint_dropped(monkeypatch):
    """Hermes' back-fill: a silently dropped URL still gets its own entry."""
    payload = {"results": [{"url": URLS[0], "title": "A", "content": "c"}]}
    stub_http(monkeypatch, lambda *_: response(json_body=mcp_body(json.dumps(payload))))

    out = await keyless.parallel_extract_keyless(URLS)

    assert_entries(out, URLS)
    assert out[0]["content"] == "c"
    assert out[1]["error"] == "no content returned"


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({"content": "403 forbidden"}, "403 forbidden"),
        ({"error_type": "timeout"}, "timeout"),
        ({}, "extraction failed"),
    ],
)
async def test_parallel_extract_maps_the_errors_array(monkeypatch, record, expected):
    payload = {"results": [], "errors": [{"url": URLS[0], **record}]}
    stub_http(monkeypatch, lambda *_: response(json_body=mcp_body(json.dumps(payload))))

    out = await keyless.parallel_extract_keyless([URLS[0]])

    assert_entries(out, [URLS[0]])
    assert out[0]["error"] == expected


async def test_parallel_extract_rekeys_a_reply_that_came_back_shuffled(monkeypatch):
    """Order parity is the contract: the caller pairs entries with URLs by position."""
    payload = {
        "results": [
            {"url": URLS[1], "title": "B", "content": "b"},
            {"url": URLS[0], "title": "A", "content": "a"},
        ]
    }
    stub_http(monkeypatch, lambda *_: response(json_body=mcp_body(json.dumps(payload))))

    out = await keyless.parallel_extract_keyless(URLS)

    assert_entries(out, URLS)
    assert [e["content"] for e in out] == ["a", "b"]


async def test_parallel_extract_preserves_unassociated_material(monkeypatch):
    payload = {
        "results": [
            {"url": URLS[0], "content": "a"},
            {"url": "https://uninvited.test", "content": "z"},
        ]
    }
    stub_http(monkeypatch, lambda *_: response(json_body=mcp_body(json.dumps(payload))))

    out = await keyless.parallel_extract_keyless([URLS[0]])

    assert_entries(out[:1], [URLS[0]])
    assert out[1]["requested_url"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"result": {"isError": True, "content": [{"text": "rate limit reached"}]}},
        mcp_body("not json at all"),
        mcp_body(json.dumps(["a list, not an object"])),
    ],
)
async def test_parallel_extract_turns_a_whole_call_failure_into_per_url_entries(monkeypatch, body):
    stub_http(monkeypatch, lambda *_: response(json_body=body))

    out = await keyless.parallel_extract_keyless(URLS)

    assert_entries(out, URLS)
    for item in out:
        assert "Keyless Parallel extract failed" in item["error"]
        assert "PARALLEL_API_KEY" in item["error"]


async def test_parallel_extract_reports_an_unreachable_endpoint(monkeypatch):
    stub_http(
        monkeypatch,
        lambda *_: httpx.ConnectTimeout("timed out", request=httpx.Request("POST", "https://p")),
    )

    out = await keyless.parallel_extract_keyless([URLS[0]])

    assert "request failed" in out[0]["error"]


# ---------------------------------------------------------------------------
# Exa: one call per URL, because the reply is one undivided text
# ---------------------------------------------------------------------------


async def test_exa_extract_calls_once_per_url_and_scrapes_the_title(monkeypatch):
    bodies = {URLS[0]: "# Page A\nbody a", URLS[1]: "Title: Page B\nbody b"}

    def handler(_method, _url, kwargs):
        asked = kwargs["json"]["params"]["arguments"]["urls"]
        assert len(asked) == 1
        return response(json_body=mcp_body(bodies[asked[0]]))

    calls = stub_http(monkeypatch, handler)

    out = await keyless.exa_extract_keyless(URLS)

    assert len(calls) == 2
    assert calls[0]["json"]["params"]["name"] == "web_fetch_exa"
    assert_entries(out, URLS)
    assert [e["title"] for e in out] == ["Page A", "Page B"]
    assert out[0]["content"] == "# Page A\nbody a"
    assert out[0]["raw_content"] == out[0]["content"]


@pytest.mark.parametrize(
    ("payload", "title"),
    [
        ("intro\n# Heading wins\nTitle: later", "Heading wins"),
        ("Title: Label wins\n# later", "Label wins"),
        ("just some prose", ""),
    ],
)
async def test_exa_extract_reads_the_first_title_line_only(monkeypatch, payload, title):
    stub_http(monkeypatch, lambda *_: response(json_body=mcp_body(payload)))

    out = await keyless.exa_extract_keyless([URLS[0]])

    assert out[0]["title"] == title


async def test_exa_extract_keeps_going_after_one_page_fails(monkeypatch):
    def handler(_method, _url, kwargs):
        asked = kwargs["json"]["params"]["arguments"]["urls"][0]
        if asked == URLS[0]:
            return response(
                json_body={"result": {"isError": True, "content": [{"text": "429 slow down"}]}}
            )
        return response(json_body=mcp_body("# B\nbody b"))

    stub_http(monkeypatch, handler)

    out = await keyless.exa_extract_keyless(URLS)

    assert_entries(out, URLS)
    assert "429 slow down" in out[0]["error"]
    assert "EXA_API_KEY" in out[0]["error"]
    assert out[1]["title"] == "B"


# ---------------------------------------------------------------------------
# Firecrawl: anonymous POST to the public cloud
# ---------------------------------------------------------------------------


async def test_firecrawl_extract_posts_anonymously_to_v2_scrape(monkeypatch):
    body = {"data": {"markdown": "# md", "metadata": {"title": "T", "sourceURL": "https://example.org/final"}}}
    calls = stub_http(monkeypatch, lambda *_: response(json_body=body))

    out = await keyless.firecrawl_extract_keyless([URLS[0]])

    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == f"{keyless.FIRECRAWL_API_URL}/v2/scrape"
    assert calls[0]["json"] == {"url": URLS[0], "formats": ["markdown"]}
    assert "Authorization" not in calls[0]["headers"]
    assert_entries(out, [URLS[0]])
    assert out[0]["title"] == "T"
    assert out[0]["content"] == "# md"
    # The entry names the URL that was asked for, not whatever the vendor echoed.
    assert out[0]["metadata"] == {"sourceURL": "https://example.org/final", "title": "T"}


@pytest.mark.parametrize(
    ("body", "content", "title"),
    [
        ({"markdown": "flat", "metadata": {"title": "F"}}, "flat", "F"),
        ({"data": {"html": "<p>x</p>"}}, "<p>x</p>", ""),
        ({"data": {"markdown": "m", "metadata": "not-a-dict"}}, "m", ""),
        ({"data": {}}, "", ""),
    ],
)
async def test_firecrawl_extract_normalizes_the_scrape_payload(monkeypatch, body, content, title):
    stub_http(monkeypatch, lambda *_: response(json_body=body))

    out = await keyless.firecrawl_extract_keyless([URLS[0]])

    assert out[0]["content"] == content
    assert out[0]["title"] == title


async def test_firecrawl_extract_makes_one_call_per_url(monkeypatch):
    calls = stub_http(monkeypatch, lambda *_: response(json_body={"data": {"markdown": "m"}}))

    out = await keyless.firecrawl_extract_keyless(URLS)

    assert [call["json"]["url"] for call in calls] == URLS
    assert_entries(out, URLS)


async def test_firecrawl_extract_turns_a_throttle_into_a_rate_limitish_entry(monkeypatch):
    stub_http(monkeypatch, lambda *_: response(429, text="slow down"))

    out = await keyless.firecrawl_extract_keyless([URLS[0]])

    assert_entries(out, [URLS[0]])
    assert "429" in out[0]["error"]
    assert keyless.is_rate_limitish(out[0]["error"])


# ---------------------------------------------------------------------------
# Keenable: GET the public fetch endpoint
# ---------------------------------------------------------------------------


async def test_keenable_extract_gets_the_public_endpoint(monkeypatch):
    body = {"url": URLS[0], "title": "K", "content": "markdown body"}
    calls = stub_http(monkeypatch, lambda *_: response(json_body=body))

    out = await keyless.keenable_extract_keyless([URLS[0]])

    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == f"{keyless.KEENABLE_API_URL}/v1/fetch/public"
    assert calls[0]["params"] == {"url": URLS[0]}
    assert calls[0]["headers"] == {"X-Keenable-Title": keyless.CLIENT_NAME}
    assert_entries(out, [URLS[0]])
    assert out[0]["title"] == "K"
    assert out[0]["content"] == "markdown body"


async def test_keenable_extract_files_a_redirect_under_the_requested_url(monkeypatch):
    body = {"url": "https://final.test/after-redirect", "title": "K", "content": "c"}
    stub_http(monkeypatch, lambda *_: response(json_body=body))

    out = await keyless.keenable_extract_keyless([URLS[0]])

    assert_entries(out, [URLS[0]])


async def test_keenable_extract_keeps_order_across_a_mixed_batch(monkeypatch):
    def handler(_method, _url, kwargs):
        if kwargs["params"]["url"] == URLS[0]:
            return response(json_body={"title": "A", "content": "a"})
        return response(404, text="not found")

    stub_http(monkeypatch, handler)

    out = await keyless.keenable_extract_keyless(URLS)

    assert_entries(out, URLS)
    assert out[0]["content"] == "a"
    assert "not found" in out[1]["error"]
    assert "KEENABLE_API_KEY" in out[1]["error"]


async def test_keenable_extract_survives_a_non_object_body(monkeypatch):
    stub_http(monkeypatch, lambda *_: response(json_body=["not", "an", "object"]))

    out = await keyless.keenable_extract_keyless([URLS[0]])

    assert_entries(out, [URLS[0]])
    assert "expected a JSON object" in out[0]["error"]


# ---------------------------------------------------------------------------
# The ring: same walk as search, different failover evidence
# ---------------------------------------------------------------------------


def stub_extractor(monkeypatch, vendor, results, seen=None):
    """Replace one ring member with a scripted extractor; records that it was called."""

    async def extractor(urls):
        if seen is not None:
            seen.append(vendor)
        assert urls == URLS
        return [dict(item) for item in results]

    monkeypatch.setitem(keyless._KEYLESS_EXTRACTORS, vendor, extractor)


def test_every_ring_member_has_an_extractor():
    assert tuple(keyless._KEYLESS_EXTRACTORS) == keyless.KEYLESS_RING


async def test_a_batch_wide_throttle_advances_to_the_next_vendor(monkeypatch):
    seen: list[str] = []
    stub_extractor(monkeypatch, "exa", [failed(u, "rate limit") for u in URLS], seen)
    stub_extractor(monkeypatch, "parallel", [entry(u) for u in URLS], seen)

    out = await keyless.extract_with_failover("exa", URLS)

    assert seen == ["exa", "parallel"]
    assert_entries(out, URLS)
    assert all(item["metadata"]["served_by"] == "parallel" for item in out)


async def test_one_failed_page_does_not_advance_the_walk(monkeypatch):
    """A page that failed is that page's problem; the vendor is still healthy."""
    seen: list[str] = []
    partial = [entry(URLS[0]), failed(URLS[1], "rate limit")]
    stub_extractor(monkeypatch, "exa", partial, seen)
    stub_extractor(monkeypatch, "parallel", [entry(u) for u in URLS], seen)

    out = await keyless.extract_with_failover("exa", URLS)

    assert seen == ["exa"]
    assert out == partial


async def test_an_error_that_is_not_throttling_stops_the_walk(monkeypatch):
    seen: list[str] = []
    refused = [failed(u, "invalid url") for u in URLS]
    stub_extractor(monkeypatch, "exa", refused, seen)
    stub_extractor(monkeypatch, "parallel", [entry(u) for u in URLS], seen)

    out = await keyless.extract_with_failover("exa", URLS)

    assert seen == ["exa"]
    assert out == refused


async def test_a_vendor_that_serves_the_request_is_not_annotated(monkeypatch):
    stub_extractor(monkeypatch, "exa", [entry(u) for u in URLS])

    out = await keyless.extract_with_failover("exa", URLS)

    assert all("served_by" not in item["metadata"] for item in out)


async def test_only_the_pages_that_were_read_are_annotated(monkeypatch):
    stub_extractor(monkeypatch, "exa", [failed(u, "429") for u in URLS])
    stub_extractor(monkeypatch, "parallel", [entry(URLS[0]), failed(URLS[1], "404")])

    out = await keyless.extract_with_failover("exa", URLS)

    assert out[0]["metadata"]["served_by"] == "parallel"
    assert "served_by" not in out[1]["metadata"]


async def test_every_vendor_throttled_says_the_whole_ring_was_walked(monkeypatch):
    seen: list[str] = []
    for vendor in keyless.KEYLESS_RING:
        stub_extractor(monkeypatch, vendor, [failed(u, "429 too many requests") for u in URLS], seen)

    out = await keyless.extract_with_failover("exa", URLS)

    assert seen == list(keyless.KEYLESS_RING)
    assert_entries(out, URLS)
    for item in out:
        assert item["error"].startswith("429 too many requests")
        assert "all keyless vendors throttled" in item["error"]
        for vendor in keyless.KEYLESS_RING:
            assert vendor in item["error"]


async def test_a_paid_pin_removes_a_vendor_from_the_extract_ring(web_home, monkeypatch):
    seen: list[str] = []
    for vendor in keyless.KEYLESS_RING:
        stub_extractor(monkeypatch, vendor, [entry(u) for u in URLS], seen)
    write_config(web_home, provider_tier=dict.fromkeys(keyless.KEYLESS_RING, "paid"))

    out = await keyless.extract_with_failover("exa", URLS)

    assert seen == []
    assert_entries(out, URLS)
    assert all(
        item["error"] == "All keyless web providers are disabled or pinned to paid tiers." for item in out
    )


async def test_extract_enters_the_ring_at_the_shared_cursor(monkeypatch):
    """Extract and search walk one ring off one cursor -- an unpinned name does not lead."""
    monkeypatch.setattr(keyless.current_scope(), "cursor", [1])
    seen: list[str] = []
    stub_extractor(monkeypatch, "parallel", [entry(u) for u in URLS], seen)

    await keyless.extract_with_failover("exa", URLS)

    assert seen == ["parallel"]


async def test_a_pinned_vendor_starts_the_extract_walk(web_home, monkeypatch):
    write_config(web_home, backend="keenable")
    monkeypatch.setattr(keyless.current_scope(), "cursor", [0])
    seen: list[str] = []
    stub_extractor(monkeypatch, "keenable", [entry(u) for u in URLS], seen)

    await keyless.extract_with_failover("keenable", URLS)

    assert seen == ["keenable"]


async def test_the_extract_backend_key_pins_the_extract_walk(web_home, monkeypatch):
    """``extract_backend`` alone is a pin -- the key that reaches this walk by name.

    The cursor sits on ``exa`` and the pinned vendor is last in the ring, so an ignored
    pin is visible twice over: the batch would be fetched by a vendor the install did not
    choose, and the request would count as unpinned and turn the cursor.
    """
    write_config(web_home, extract_backend="keenable")
    monkeypatch.setattr(keyless.current_scope(), "cursor", [0])
    seen: list[str] = []
    stub_extractor(monkeypatch, "keenable", [entry(u) for u in URLS], seen)

    out = await keyless.extract_with_failover("keenable", URLS)

    assert seen == ["keenable"]
    assert_entries(out, URLS)
    assert keyless.current_scope().cursor[0] == 0


async def test_an_empty_batch_is_nobodys_throttle(monkeypatch):
    seen: list[str] = []

    async def empty(urls):
        seen.append("exa")
        assert urls == []
        return []

    monkeypatch.setitem(keyless._KEYLESS_EXTRACTORS, "exa", empty)

    assert await keyless.extract_with_failover("exa", []) == []
    assert seen == ["exa"]

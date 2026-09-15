"""Every outbound web call leaves a row in the spend ledger, and none of them leaves a URL.

A run's cost is not only its model tokens: the searches, the page fetches and the
downloads are the other half of the bill, and until they are on the ledger a finished
run's real spend cannot be reconstructed. Nothing here touches the network -- the
transports are stubbed and the ledger is a throwaway board under ``tmp_path``.
"""

from __future__ import annotations

import functools
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from misaka.config.product import CFG
from misaka.core.platform import tasks
from misaka.core.tools import download_file, web_fetch
from misaka.core.tools._web import bounded, negative_cache
from misaka.core.tools.download_file import create_download_file_tool_definition
from misaka.core.tools.web_fetch import create_web_fetch_tool_definition
from misaka.core.web import cache, registry, tool

PUBLIC = "93.184.216.34"
PAGE = (
    b"<!doctype html><html><head><title>Report</title></head>"
    b"<body><p>Shipments rose to 1,240 units.</p></body></html>"
)
PDF = b"%PDF-1.7\n" + b"x" * 200

# A query and two URLs written the way the dangerous ones are: the first carries what the
# user typed, the other two carry a live credential in plain sight.
QUERY = "acetaminophen hepatotoxicity threshold"
PAGE_URL = "https://example.com/report?session=secret-token"
FILE_URL = "https://files.example/paper.pdf?X-Amz-Signature=deadbeefcafe"


@pytest.fixture(autouse=True)
def _offline(monkeypatch, tmp_path):
    """No hostname resolves, no vendor config is read, and both caches start empty."""
    negative_cache.clear()
    cache.search_memo.clear()
    registry.reset_for_tests()
    monkeypatch.setitem(CFG, "web_config", str(tmp_path / "web.json"))

    async def _resolve_host(_host, _port):
        return [PUBLIC]

    monkeypatch.setattr(bounded, "_resolve_host", _resolve_host)
    yield
    negative_cache.clear()
    cache.search_memo.clear()
    registry.reset_for_tests()


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """Point the three ``MISAKA_USAGE_*`` variables at a throwaway board, and read it back.

    Those three are what a worker already exports so a turn's tokens land on the right
    card; an external call rides the same address rather than inventing a second one.
    """
    path = tmp_path / "ledger.db"
    monkeypatch.setenv("MISAKA_USAGE_DB", str(path))
    monkeypatch.setenv("MISAKA_USAGE_TASK_ID", "t_acct")
    monkeypatch.setenv("MISAKA_USAGE_GENERATION", "4")

    def rows():
        con = tasks.connect(str(path))
        try:
            return [
                {"task_id": row["task_id"], "generation": row["generation"],
                 **json.loads(row["payload"])}
                for row in con.execute(
                    "SELECT task_id,generation,payload FROM events "
                    "WHERE kind='external_call' ORDER BY id"
                )
            ]
        finally:
            con.close()

    return SimpleNamespace(path=path, rows=rows)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _static(body: bytes, content_type: str):
    def handler(_request):
        return httpx.Response(200, content=body, headers={"content-type": content_type})

    return handler


def _serve(monkeypatch, module, handler):
    """Give one tool module a stubbed transport, leaving the SSRF vetting real code."""
    monkeypatch.setattr(
        module,
        "open_checked_stream",
        functools.partial(bounded.open_checked_stream, transport=httpx.MockTransport(handler)),
    )


def _hit(count: int) -> dict:
    return {
        "success": True,
        "data": {
            "web": [
                {"title": f"t{i}", "url": f"https://example.com/{i}",
                 "description": f"d{i}", "position": i}
                for i in range(1, count + 1)
            ]
        },
    }


def _stub_search(monkeypatch, response, *, backend="searxng"):
    """Exercise the actual provider and account at its HTTP request, not a tool stub."""
    from misaka.core.web.backends.searxng import SearXNGWebSearchProvider

    calls = []
    monkeypatch.setenv("SEARXNG_URL", "https://search.example")
    provider = SearXNGWebSearchProvider()
    monkeypatch.setattr(tool, "resolve_provider", lambda: (provider, backend, ""))
    monkeypatch.setattr(tool, "dispatch_search", provider.search)
    real_client = httpx.AsyncClient
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"results": response["data"]["web"]})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(
        **({"transport": httpx.MockTransport(handle)} | kw | {"proxy": None})))
    return calls


async def _fetch(url=PAGE_URL):
    return await create_web_fetch_tool_definition().execute("call-1", {"url": url}, None, None, None)


async def _download(workspace, url=FILE_URL):
    definition = create_download_file_tool_definition(str(workspace))
    return await definition.execute("call-1", {"url": url}, None, None, None)


# --- one row per paid call ----------------------------------------------------------


async def test_search_records_backend_query_hash_and_attempt(monkeypatch, ledger):
    _stub_search(monkeypatch, _hit(3))

    result = json.loads(await tool.web_search_tool(QUERY, limit=3))

    assert result["success"] is True
    assert ledger.rows() == [{
        "task_id": "t_acct",
        "generation": 4,
        "service": "web_search",
        "backend": "searxng",
        "unit": "http_request",
        "subject_sha256": _sha(QUERY),
    }]


async def test_a_memo_hit_is_not_a_second_external_call(monkeypatch, ledger):
    """The ledger counts what was paid for, not what the model asked for: the second
    identical search is served from the memo and never reaches a vendor."""
    calls = _stub_search(monkeypatch, _hit(2))

    await tool.web_search_tool(QUERY, limit=3)
    await tool.web_search_tool(QUERY, limit=3)

    assert len(calls) == 1
    assert len(ledger.rows()) == 1


async def test_fetch_records_the_url_hash_and_attempt(monkeypatch, ledger):
    _serve(monkeypatch, web_fetch, _static(PAGE, "text/html; charset=utf-8"))

    result = await _fetch()

    assert "Shipments rose to 1,240 units." in result.content[0].text
    assert ledger.rows() == [{
        "task_id": "t_acct",
        "generation": 4,
        "service": "web_fetch",
        "backend": "direct",
        "unit": "http_request",
        "subject_sha256": _sha(PAGE_URL),
    }]


async def test_download_records_the_url_hash_and_attempt(
    monkeypatch, ledger, workspace
):
    _serve(monkeypatch, download_file, _static(PDF, "application/pdf"))

    result = await _download(workspace)

    assert (workspace / "downloads" / "paper.pdf").read_bytes() == PDF
    assert result.details["bytes"] == len(PDF)
    assert ledger.rows() == [{
        "task_id": "t_acct",
        "generation": 4,
        "service": "download_file",
        "backend": "direct",
        "unit": "http_request",
        "subject_sha256": _sha(FILE_URL),
    }]


# --- what the ledger must never hold, and what it must never break ------------------


async def test_the_ledger_holds_no_query_and_no_url_in_the_clear(
    monkeypatch, ledger, workspace
):
    """A ledger row outlives the turn and gets exported, so the one thing it may not
    carry is the text that identifies a person or authenticates a request."""
    _stub_search(monkeypatch, _hit(1))
    await tool.web_search_tool(QUERY, limit=3)
    _serve(monkeypatch, web_fetch, _static(PAGE, "text/html; charset=utf-8"))
    await _fetch()
    _serve(monkeypatch, download_file, _static(PDF, "application/pdf"))
    await _download(workspace)

    written = json.dumps(ledger.rows())

    assert len(ledger.rows()) == 3
    for leak in (QUERY, "acetaminophen", PAGE_URL, FILE_URL, "secret-token",
                 "deadbeefcafe", "example.com"):
        assert leak not in written


async def test_a_broken_ledger_never_fails_a_tool(monkeypatch, ledger, workspace):
    """Bookkeeping that can fail a tool call is worse than no bookkeeping."""

    def explode(_path):
        raise RuntimeError("the board is on fire")

    _stub_search(monkeypatch, _hit(2))
    _serve(monkeypatch, web_fetch, _static(PAGE, "text/html; charset=utf-8"))
    monkeypatch.setattr(tasks, "connect", explode)

    searched = json.loads(await tool.web_search_tool(QUERY, limit=3))
    fetched = await _fetch()
    _serve(monkeypatch, download_file, _static(PDF, "application/pdf"))
    downloaded = await _download(workspace)

    assert searched["success"] is True and len(searched["data"]["web"]) == 2
    assert "Shipments rose to 1,240 units." in fetched.content[0].text
    assert downloaded.details["bytes"] == len(PDF)


async def test_a_session_with_no_ledger_records_nothing(monkeypatch, ledger):
    """An interactive session is charged for no tokens either: with no card to bill,
    there is nothing to open and no board to create."""
    for name in ("MISAKA_USAGE_DB", "MISAKA_USAGE_TASK_ID", "MISAKA_USAGE_GENERATION"):
        monkeypatch.delenv(name, raising=False)
    _serve(monkeypatch, web_fetch, _static(PAGE, "text/html; charset=utf-8"))

    result = await _fetch()

    assert "Shipments rose to 1,240 units." in result.content[0].text
    assert not ledger.path.exists()


def _vendor_wire(monkeypatch, handler):
    client = httpx.AsyncClient
    calls = []
    def record(request):
        calls.append(request)
        return handler(request)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(
        **{**kw, "transport": httpx.MockTransport(record), "proxy": None}))
    return calls


async def test_batched_extract_costs_one_request_and_cache_costs_zero(monkeypatch, ledger, tmp_path):
    from misaka.core.web import extract
    monkeypatch.setitem(CFG, "web_cache", str(tmp_path / "cache"))
    Path(CFG["web_config"]).write_text(json.dumps({"extract_backend": "exa"}))
    monkeypatch.setenv("EXA_API_KEY", "fixture-exa-key")
    urls = [f"https://example.com/{i}" for i in range(5)]
    calls = _vendor_wire(monkeypatch, lambda _: httpx.Response(200, json={
        "results": [{"url": url, "text": "page"} for url in urls]}))
    for _ in range(2):
        result = json.loads(await extract.web_extract_tool(urls, cwd=str(tmp_path)))
        assert len(result["results"]) == 5
    assert len(calls) == len(ledger.rows()) == 1
    assert ledger.rows()[0]["backend"] == "exa"


async def test_failed_vendor_and_rescue_are_separate_attempts(monkeypatch, ledger, tmp_path):
    from misaka.core.web import extract, keyless
    Path(CFG["web_config"]).write_text(json.dumps({"extract_backend": "tavily", "cache_enabled": False}))
    monkeypatch.setenv("TAVILY_API_KEY", "fixture-key")
    urls = ["https://example.com/a", "https://example.com/b"]
    def handle(request):
        if request.url.host == "api.tavily.com":
            return httpx.Response(503, text="outage")
        if str(request.url) == keyless.EXA_MCP_URL:
            return httpx.Response(429, text="rate limit")
        assert str(request.url) == keyless.PARALLEL_MCP_URL
        text = json.dumps({"results": [{"url": url, "full_content": "rescued"} for url in urls]})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
            "result": {"content": [{"type": "text", "text": text}]}})
    calls = _vendor_wire(monkeypatch, handle)
    result = json.loads(await extract.web_extract_tool(urls, cwd=str(tmp_path)))
    assert all(row["content"] == "rescued" for row in result["results"])
    assert len(calls) == 4  # Tavily batch, two Exa URL attempts, Parallel batch
    assert [row["backend"] for row in ledger.rows()] == ["tavily", "exa", "exa", "parallel"]


async def test_failed_http_and_each_redirect_are_counted(monkeypatch, ledger):
    seen = []
    def handle(request):
        seen.append(str(request.url))
        if len(seen) == 1:
            return httpx.Response(302, headers={"Location": "https://example.com/gone"})
        return httpx.Response(404)
    _serve(monkeypatch, web_fetch, handle)
    result = await _fetch()
    assert result.details["status"] == 404
    assert len(seen) == len(ledger.rows()) == 2
    assert [row["subject_sha256"] for row in ledger.rows()] == [_sha(PAGE_URL), _sha("https://example.com/gone")]


async def test_brave_attempt_uses_the_registered_backend_name(monkeypatch, ledger):
    from misaka.core.web.backends.brave_free import BraveFreeWebSearchProvider
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "fixture-brave-key")
    _vendor_wire(monkeypatch, lambda _: httpx.Response(401))
    provider = BraveFreeWebSearchProvider()
    await provider.search("q")
    assert ledger.rows()[0]["backend"] == provider.name == "brave-free"

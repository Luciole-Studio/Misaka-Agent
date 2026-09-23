"""The web_extract tool: gates, order, cache, budget, evidence and the fence.

Nothing here touches the network or DNS. The provider layer is stubbed at
``extract.dispatch_extract`` for everything above it, and the address vet at
``extract.vet_public_url`` -- the two seams that would otherwise reach outside the process.
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest
from webconf import write_web

from misaka.config import home
from misaka.config.product import CFG
from misaka.core.platform import budget
from misaka.core.platform.prompt_guard import MARKER
from misaka.core.web import cache, extract, registry, website_policy

HERMES_WEB_TOOLS = Path(__file__).parent / "fixtures/hermes_990473a/tools/web_tools.py"

_VENDOR_ENV = (
    "TAVILY_API_KEY",
    "EXA_API_KEY",
    "PARALLEL_API_KEY",
    "KEENABLE_API_KEY",
    "FIRECRAWL_API_KEY",
    "FIRECRAWL_API_URL",
    "XAI_API_KEY",
)


@pytest.fixture(autouse=True)
def web_home(tmp_path, monkeypatch):
    """A throwaway web.json and cache home, with no vendor credentials in the way."""
    for name in _VENDOR_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(CFG, "web_cache", str(tmp_path / "cache" / "web"))
    registry.reset_for_tests()
    website_policy.invalidate_cache()
    yield home.path("settings")            # the "web" section lives here now
    registry.reset_for_tests()
    website_policy.invalidate_cache()


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    """Every URL vets clean unless a test says otherwise; no resolver is ever reached."""

    async def _vet(url, *, proxy=None):
        return ("93.184.216.34",)

    monkeypatch.setattr(extract, "vet_public_url", _vet)


class _Provider:
    """The smallest thing resolve_extractor can return."""

    def __init__(self, name="tavily", extracts=True):
        self.name = name
        self.display_name = name.title()
        self._extracts = extracts

    def supports_extract(self):
        return self._extracts


def stub(monkeypatch, results, *, provider="tavily", rescued=False, calls=None):
    """Pin the resolved provider and make the dispatch layer answer with *results*."""
    monkeypatch.setattr(
        extract, "resolve_extractor", lambda: (_Provider(provider), provider, "")
    )

    async def fake_extract(prov, urls, *, format=None):
        if calls is not None:
            calls.append((prov.name, list(urls), format))
        return (results(urls) if callable(results) else results), rescued

    monkeypatch.setattr(extract, "dispatch_extract", fake_extract)


def page(url, content, title="T"):
    return {"url": url, "title": title, "content": content, "raw_content": content}


async def run(urls, char_limit=None, *, cwd=None, signal=None):
    return await extract.web_extract_tool(
        urls, "markdown", char_limit, signal=signal, cwd=cwd
    )


def body(rendered):
    """The tool's own JSON. The fence is added by the registered wrapper, not here."""
    return json.loads(rendered)


# ---------------------------------------------------------------------------
# Schema: every character of it came from Hermes
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HERMES_WEB_TOOLS.exists(), reason="Hermes checkout not present")
def test_the_schema_matches_hermes_with_host_tools_and_material_note():
    """Read WEB_EXTRACT_SCHEMA out of the Hermes source and compare the objects.

    Parameter constraints remain upstream-compatible; the descriptions explicitly
    document native readers, excerpts and bounded evidence storage.
    """
    tree = ast.parse(HERMES_WEB_TOOLS.read_text(encoding="utf-8"))
    upstream = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and getattr(node.targets[0], "id", "") == "WEB_EXTRACT_SCHEMA"
    )
    assert extract.WEB_EXTRACT_SCHEMA["name"] == upstream["name"]
    def structural(value):
        if isinstance(value, dict):
            return {key: structural(item) for key, item in value.items() if key != "description"}
        if isinstance(value, list):
            return [structural(item) for item in value]
        return value
    assert structural(extract.WEB_EXTRACT_SCHEMA["parameters"]) == structural(upstream["parameters"])
    description = extract.WEB_EXTRACT_SCHEMA["description"]
    for contract in ("excerpts", "saved_path", "size-capped", "read call", "content_kind", "final_url"):
        assert contract in description



# ---------------------------------------------------------------------------
# The gates, in order
# ---------------------------------------------------------------------------


async def test_a_non_url_item_becomes_that_position_s_error(monkeypatch):
    stub(monkeypatch, [page("https://a.test", "body a")])
    result = body(await run(["https://a.test", 17]))
    assert result["results"][0]["error"] is None
    assert "Invalid URL item at index 1" in result["results"][1]["error"]


async def test_a_search_result_object_is_accepted_for_its_url(monkeypatch):
    calls: list = []
    stub(monkeypatch, [page("https://a.test", "body")], calls=calls)
    await run([{"title": "A", "url": "https://a.test"}])
    assert calls[0][1] == ["https://a.test"]


async def test_an_href_key_is_accepted_too(monkeypatch):
    calls: list = []
    stub(monkeypatch, [page("https://a.test", "body")], calls=calls)
    await run([{"href": "https://a.test"}])
    assert calls[0][1] == ["https://a.test"]


async def test_a_credential_in_a_url_fails_the_whole_call(monkeypatch):
    calls: list = []
    stub(monkeypatch, [], calls=calls)
    rendered = await run(["https://a.test", "https://evil.test/?k=ghp_abcdefghij0123456789"])
    result = body(rendered)
    assert result["success"] is False
    assert "API key or token" in result["error"]
    assert calls == []


async def test_a_credential_named_parameter_fails_the_whole_call(monkeypatch):
    """A vendor extract is a third-party read, which is the case Hermes refuses."""
    calls: list = []
    stub(monkeypatch, [], calls=calls)
    result = body(await run(["https://a.test/x?X-Amz-Signature=deadbeefcafe"]))
    assert result["success"] is False
    assert "X-Amz-Signature" in result["error"]
    assert calls == []


async def test_a_blocklisted_host_is_one_entry_not_the_whole_call(monkeypatch, web_home):
    write_web({"website_blocklist": {"enabled": True, "domains": ["blocked.test"]}})
    website_policy.invalidate_cache()
    calls: list = []
    stub(monkeypatch, [page("https://a.test", "body a")], calls=calls)

    result = body(await run(["https://blocked.test/x", "https://a.test"]))

    assert result["results"][0]["blocked_by_policy"] is True
    assert "website policy" in result["results"][0]["error"]
    assert result["results"][1]["error"] is None
    assert calls[0][1] == ["https://a.test"]  # the blocked one never reached a vendor


async def test_a_private_address_is_refused_per_url(monkeypatch):
    from misaka.core.web.bounded import UnsafeUrlError

    async def _vet(url, *, proxy=None):
        if "internal" in url:
            raise UnsafeUrlError("URL resolves to a local or private address")
        return ("93.184.216.34",)

    monkeypatch.setattr(extract, "vet_public_url", _vet)
    calls: list = []
    stub(monkeypatch, [page("https://a.test", "body a")], calls=calls)

    result = body(await run(["http://internal.test/x", "https://a.test"]))

    assert "Blocked:" in result["results"][0]["error"]
    assert "private" in result["results"][0]["error"]
    assert calls[0][1] == ["https://a.test"]


async def test_more_than_five_urls_are_sliced(monkeypatch):
    calls: list = []
    stub(monkeypatch, lambda urls: [page(u, "x") for u in urls], calls=calls)
    result = body(await run([f"https://{i}.test" for i in range(9)]))
    assert len(calls[0][1]) == extract.MAX_URLS
    assert len(result["results"]) == extract.MAX_URLS


# ---------------------------------------------------------------------------
# Backend selection errors
# ---------------------------------------------------------------------------


async def test_a_search_only_backend_says_so_by_name(monkeypatch, web_home):
    write_web({"extract_backend": "ddgs"})
    result = body(await run(["https://a.test"]))
    assert result["success"] is False
    assert "search-only backend" in result["error"]
    assert "Ddgs" in result["error"] or "ddgs" in result["error"]


async def test_an_unknown_backend_is_reported_as_the_typo_it_is(monkeypatch, web_home):
    write_web({"extract_backend": "tavly"})
    result = body(await run(["https://a.test"]))
    assert result["success"] is False
    assert "tavly" in result["error"]
    assert "no registered web extract provider" in result["error"]


async def test_no_provider_at_all_says_how_to_get_one(monkeypatch):
    """Unreachable with the shipped backends -- firecrawl is the default and it extracts.

    It is reachable the way Hermes' own branch is: with nothing registered that can serve
    the capability. Driven through the two seams rather than by emptying the registry,
    which ``resolve_extractor`` would just refill.
    """
    from misaka.core.web import dispatch

    monkeypatch.setattr(dispatch, "get_provider", lambda _name: None)
    monkeypatch.setattr(dispatch, "active_extract_provider", lambda: None)
    monkeypatch.setattr(dispatch, "selection_stored", lambda: False)

    result = body(await run(["https://a.test"]))
    assert result["success"] is False
    assert "No web extract provider configured" in result["error"]


# ---------------------------------------------------------------------------
# Budget, evidence and the footer
# ---------------------------------------------------------------------------


async def test_a_short_page_comes_back_whole_and_lands_on_disk(monkeypatch, tmp_path):
    stub(monkeypatch, [page("https://a.test", "the whole page")])
    result = body(await run(["https://a.test"], cwd=str(tmp_path)))
    entry = result["results"][0]
    assert entry["content"] == "the whole page"
    assert "[TRUNCATED]" not in entry["content"]
    saved = tmp_path / entry["saved_path"]
    assert saved.read_text(encoding="utf-8").endswith("the whole page\n")


async def test_a_long_page_is_head_and_tail_with_a_readable_footer(monkeypatch, tmp_path):
    lines = [f"line {i:04d} " + "x" * 40 for i in range(400)]
    content = "\n".join(lines)
    stub(monkeypatch, [page("https://a.test", content)])

    result = body(await run(["https://a.test"], char_limit=4000, cwd=str(tmp_path)))
    entry = result["results"][0]

    assert "[TRUNCATED]" in entry["content"]
    assert entry["content"].startswith("line 0000")
    assert "middle omitted" in entry["content"]
    assert 'read path="' in entry["content"]
    assert len(entry["content"]) < len(content)


async def test_the_footer_offset_lands_in_the_page_not_the_frontmatter(monkeypatch, tmp_path):
    """The whole reason the writer owns the line count: read is 1-indexed over the file."""
    lines = [f"line {i:04d} " + "y" * 40 for i in range(400)]
    stub(monkeypatch, [page("https://a.test", "\n".join(lines))])

    result = body(await run(["https://a.test"], char_limit=4000, cwd=str(tmp_path)))
    entry = result["results"][0]

    offset = int(entry["content"].split("offset=")[1].split()[0])
    saved = (tmp_path / entry["saved_path"]).read_text(encoding="utf-8").split("\n")
    landed = saved[offset - 1]
    assert landed.startswith("line ")
    # And it is past what the head already showed.
    head = entry["content"].split("\n\n[... middle omitted")[0]
    assert landed not in head


async def test_the_whole_document_stays_under_the_result_ceiling(monkeypatch, tmp_path):
    """Five very large pages shrink the per-page budget; none of them is dropped."""
    huge = "z" * 400_000
    stub(monkeypatch, lambda urls: [page(u, huge) for u in urls])

    rendered = await run([f"https://{i}.test" for i in range(5)], char_limit=500_000, cwd=str(tmp_path))
    result = body(rendered)

    assert len(rendered) <= extract.MAX_RESULT_SIZE_CHARS + 2000  # + the fence's own text
    assert len(result["results"]) == 5
    assert all("[TRUNCATED]" in entry["content"] for entry in result["results"])
    assert all(entry["saved_path"] for entry in result["results"])


async def test_a_base64_image_becomes_a_placeholder_and_a_real_link_survives(monkeypatch):
    content = (
        "![a chart](data:image/png;base64,AAAABBBBCCCCDDDD)\n"
        "![a photo](https://cdn.test/p.png)\n"
        "data:image/gif;base64,ZZZZ"
    )
    stub(monkeypatch, [page("https://a.test", content)])
    entry = body(await run(["https://a.test"]))["results"][0]
    assert "[IMAGE: a chart]" in entry["content"]
    assert "![a photo](https://cdn.test/p.png)" in entry["content"]
    assert "base64" not in entry["content"]


async def test_the_char_limit_comes_from_config_when_unset(monkeypatch, web_home):
    write_web({"extract_char_limit": 2500})
    assert extract.extract_char_limit() == 2500
    write_web({"extract_char_limit": 10})
    assert extract.extract_char_limit() == 2000  # floored
    write_web({"extract_char_limit": "nonsense"})
    assert extract.extract_char_limit() == extract.DEFAULT_EXTRACT_CHAR_LIMIT


# ---------------------------------------------------------------------------
# Cache and ledger
# ---------------------------------------------------------------------------


async def test_a_cached_page_is_not_fetched_again(monkeypatch, tmp_path):
    calls: list = []
    stub(monkeypatch, [page("https://a.test", "first body")], calls=calls)
    await run(["https://a.test"], cwd=str(tmp_path))
    assert len(calls) == 1

    await run(["https://a.test"], cwd=str(tmp_path))
    assert len(calls) == 1  # served from the disk cache, no second vendor call


async def test_a_rescued_batch_is_never_cached(monkeypatch, tmp_path):
    calls: list = []
    stub(monkeypatch, [page("https://a.test", "ring body")], rescued=True, calls=calls)
    await run(["https://a.test"], cwd=str(tmp_path))
    await run(["https://a.test"], cwd=str(tmp_path))
    assert len(calls) == 2  # the chosen backend is attempted again next call


async def test_a_failed_page_is_not_cached(monkeypatch, tmp_path):
    calls: list = []
    stub(
        monkeypatch,
        [{"url": "https://a.test", "title": "", "content": "", "error": "504"}],
        calls=calls,
    )
    await run(["https://a.test"], cwd=str(tmp_path))
    await run(["https://a.test"], cwd=str(tmp_path))
    assert len(calls) == 2


async def test_dispatch_stub_and_cache_do_not_fabricate_outbound_rows(monkeypatch, tmp_path):
    rows: list = []
    monkeypatch.setattr(
        budget,
        "record_external_call",
        lambda service, **facts: rows.append((service, facts)) or True,
    )
    stub(monkeypatch, lambda urls: [page(u, "x") for u in urls])

    await run(["https://a.test", "https://b.test"], cwd=str(tmp_path))
    assert rows == []  # dispatch was stubbed; only provider I/O may account calls

    rows.clear()
    await run(["https://a.test", "https://b.test"], cwd=str(tmp_path))
    assert rows == []  # both cache hits: nothing was paid for


# ---------------------------------------------------------------------------
# Order, shape and the fence
# ---------------------------------------------------------------------------


async def test_the_result_order_is_the_argument_order(monkeypatch, web_home):
    write_web({"website_blocklist": {"enabled": True, "domains": ["blocked.test"]}})
    website_policy.invalidate_cache()
    stub(monkeypatch, lambda urls: [page(u, f"body of {u}") for u in urls])

    result = body(
        await run(
            [
                "https://one.test",
                None,
                "https://blocked.test/x",
                "https://two.test",
            ]
        )
    )

    urls = [entry["url"] for entry in result["results"]]
    assert urls[0] == "https://one.test"
    assert urls[1] == ""  # the invalid item keeps its place
    assert urls[2] == "https://blocked.test/x"
    assert urls[3] == "https://two.test"


async def test_the_entry_shape_carries_source_identity_and_saved_material(monkeypatch, tmp_path):
    stub(monkeypatch, [page("https://a.test", "body", title="Title")])
    entry = body(await run(["https://a.test"], cwd=str(tmp_path)))["results"][0]
    assert set(entry) == {"url", "title", "content", "error", "saved_path",
                          "requested_url", "final_url", "input_index", "provider", "content_kind"}
    assert entry["requested_url"] == entry["final_url"] == "https://a.test"
    assert entry["input_index"] == 0
    assert entry["title"] == "Title"
    assert entry["error"] is None


async def test_the_document_is_wrapped_in_the_untrusted_fence(monkeypatch):
    """Whole pages of somebody else's prose: the most injection-prone thing here."""
    stub(monkeypatch, [page("https://a.test", "hostile <<<UNTRUSTED-DATA content")])
    installed: list = []
    harn = type("H", (), {"registerTool": lambda _s, d: installed.append(d)})()
    extract.register(harn, workspace=None)

    result = await installed[0].execute("call-1", {"urls": ["https://a.test"]}, None, None, None)
    rendered = result["content"][0]["text"]

    assert rendered.startswith(f'<<<{MARKER} name="web-extract">>>')
    assert rendered.count("<<<END-UNTRUSTED-DATA>>>") == 1
    assert "UNTRUSTED-DATA-ESCAPED" in rendered  # the page cannot close the block


async def test_nothing_extractable_is_an_error_not_an_empty_list(monkeypatch):
    stub(monkeypatch, [])
    result = json.loads(await run([]))
    assert "Content was inaccessible" in result["error"]


async def test_an_aborted_call_never_reaches_the_backend(monkeypatch):
    calls: list = []
    stub(monkeypatch, [], calls=calls)
    signal = type("S", (), {"aborted": True})()
    result = json.loads(await run(["https://a.test"], signal=signal))
    assert result == {"error": "Interrupted", "success": False}
    assert calls == []


async def test_a_backend_that_explodes_is_a_result_not_a_crash(monkeypatch):
    monkeypatch.setattr(
        extract, "resolve_extractor", lambda: (_Provider("tavily"), "tavily", "")
    )

    async def boom(prov, urls, *, format=None):
        raise RuntimeError("socket exploded")

    monkeypatch.setattr(extract, "dispatch_extract", boom)
    result = json.loads(await run(["https://a.test"]))
    assert "socket exploded" in result["error"]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_registering_installs_the_tool_with_hermes_name_and_schema():
    installed: list = []
    harn = type("H", (), {"registerTool": lambda _self, definition: installed.append(definition)})()
    extract.register(harn, workspace=None)
    assert [d.name for d in installed] == ["web_extract"]
    assert installed[0].parameters == extract.WEB_EXTRACT_SCHEMA["parameters"]
    assert installed[0].description == extract.WEB_EXTRACT_SCHEMA["description"]


def test_the_cache_key_carries_the_format_so_two_renderings_cannot_collide():
    cache.extract_cache_put("https://a.test", "markdown body", format="markdown", provider="tavily")
    assert cache.extract_cache_get("https://a.test", format="markdown", provider="tavily")
    assert cache.extract_cache_get("https://a.test", format="html", provider="tavily") is None
    assert cache.extract_cache_get("https://a.test", format="markdown", provider="exa") is None


# ---------------------------------------------------------------------------
# The reason every page is stored: the research ledger has to be able to quote it
# ---------------------------------------------------------------------------


async def test_an_extracted_page_keeps_its_full_text_and_provenance(monkeypatch, tmp_path):
    import hashlib
    content = "Shipments rose to 1,240 units.\n\nThe rest follows."
    stub(monkeypatch, [page("https://a.test/report", content)])
    entry = body(await run(["https://a.test/report"], cwd=str(tmp_path)))["results"][0]
    saved = (tmp_path / entry["saved_path"]).read_text(encoding="utf-8")
    header, text = saved.split("---\n", 2)[1:]
    assert 'source_url:' in header and content in text
    assert hashlib.sha256(text.removeprefix("\n").removesuffix("\n").encode()).hexdigest() in header


async def test_the_evidence_header_names_the_vendor_that_answered(monkeypatch, tmp_path):
    """A rescued page was served by a ring member; naming the chosen backend would lie."""
    served = dict(
        page("https://a.test", "ring body"),
        metadata={"served_by": "exa", "rescued_from": "tavily"},
    )
    stub(monkeypatch, [served], provider="tavily", rescued=True)

    entry = body(await run(["https://a.test"], cwd=str(tmp_path)))["results"][0]
    header = (tmp_path / entry["saved_path"]).read_text(encoding="utf-8").split("---")[1]

    assert '"exa"' in header
    assert '"tavily"' not in header


async def test_the_evidence_header_names_the_chosen_backend_when_it_served(monkeypatch, tmp_path):
    stub(monkeypatch, [page("https://a.test", "body")], provider="tavily")
    entry = body(await run(["https://a.test"], cwd=str(tmp_path)))["results"][0]
    header = (tmp_path / entry["saved_path"]).read_text(encoding="utf-8").split("---")[1]
    assert '"tavily"' in header


# ---------------------------------------------------------------------------
# The abort race, the stored-copy ceiling, and the two vendor-written fields
# ---------------------------------------------------------------------------


class _LiveSignal:
    """An abort signal shaped like the real one: a flag AND an awaitable wait()."""

    def __init__(self):
        self._event = asyncio.Event()

    @property
    def aborted(self):
        return self._event.is_set()

    def wait(self):
        return self._event.wait()

    def abort(self):
        self._event.set()


async def test_an_abort_mid_flight_cancels_the_vendor_call(monkeypatch):
    """The agent loop cannot cancel a tool call, so the tool has to race its own work.

    Without the race a five-page extract through a backend that allows sixty seconds each
    is five minutes of a session that will not answer Ctrl-C.
    """
    signal = _LiveSignal()
    cancelled = asyncio.Event()

    monkeypatch.setattr(
        extract, "resolve_extractor", lambda: (_Provider("tavily"), "tavily", "")
    )

    async def never_answers(prov, urls, *, format=None):
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return [], False

    monkeypatch.setattr(extract, "dispatch_extract", never_answers)

    async def abort_soon():
        await asyncio.sleep(0.05)
        signal.abort()

    task = asyncio.ensure_future(run(["https://a.test"], signal=signal))
    await abort_soon()
    result = json.loads(await task)

    assert result == {"error": "Interrupted", "success": False}
    assert cancelled.is_set()  # the vendor call was really stopped, not left running


async def test_a_multi_megabyte_page_is_capped_before_it_reaches_the_disk(monkeypatch, tmp_path):
    """Some backends answer a long page with megabytes of markdown."""
    huge = "q" * (cache.MAX_STORED_TEXT_CHARS + 50_000)
    stub(monkeypatch, [page("https://a.test", huge)])

    entry = body(await run(["https://a.test"], cwd=str(tmp_path)))["results"][0]
    saved = (tmp_path / entry["saved_path"]).read_text(encoding="utf-8")

    assert len(saved) < cache.MAX_STORED_TEXT_CHARS + 2000
    assert "stored copy truncated at" in saved

    # The digest identifies the complete saved representation, not its short preview.
    import hashlib
    header, text = saved.split("---\n", 2)[1:]
    assert hashlib.sha256(text.removeprefix("\n").removesuffix("\n").encode()).hexdigest() in header


async def test_an_unbounded_vendor_error_cannot_carry_the_document_past_the_ceiling(monkeypatch):
    """The per-page budget governs content; an error field is whatever the endpoint sent."""
    failures = [
        {"url": f"https://{i}.test", "title": "", "content": "", "error": "x" * 60_000}
        for i in range(5)
    ]
    stub(monkeypatch, failures)

    rendered = await run([f"https://{i}.test" for i in range(5)])

    assert len(rendered) < extract.MAX_RESULT_SIZE_CHARS
    for entry in body(rendered)["results"]:
        assert len(entry["error"]) < 2200
        assert entry["error"].endswith("… [truncated]")


async def test_an_unbounded_vendor_title_is_cut_too(monkeypatch):
    stub(monkeypatch, [page("https://a.test", "body", title="T" * 5000)])
    entry = body(await run(["https://a.test"]))["results"][0]
    assert len(entry["title"]) < 600
    assert entry["title"].endswith("… [truncated]")


async def test_text_that_cannot_be_encoded_costs_the_evidence_file_not_the_extraction(
    monkeypatch, tmp_path
):
    """json.loads accepts a lone surrogate; encoding one raises, and that must not escape."""
    stub(monkeypatch, [page("https://a.test", "before \ud83d after")])

    entry = body(await run(["https://a.test"], cwd=str(tmp_path)))["results"][0]

    assert entry["error"] is None
    assert "before" in entry["content"]
    assert "saved_path" not in entry  # storage failed, the extraction did not


async def test_a_presigned_vendor_final_url_is_not_written_into_the_header(monkeypatch, tmp_path):
    served = dict(
        page("https://a.test", "body"),
        metadata={"sourceURL": "https://cdn.test/f?X-Amz-Signature=deadbeefcafe"},
    )
    stub(monkeypatch, [served])

    entry = body(await run(["https://a.test"], cwd=str(tmp_path)))["results"][0]
    header = (tmp_path / entry["saved_path"]).read_text(encoding="utf-8").split("---")[1]

    assert "X-Amz-Signature" not in header
    assert "https://cdn.test/f" in header

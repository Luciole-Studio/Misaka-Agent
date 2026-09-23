"""URL identity must agree between input normalization, policy rules and the HTTP client."""
import json

import httpx
import pytest
from webconf import write_web

from misaka.config.product import CFG
from misaka.core.tools import download_file, web_fetch
from misaka.core.web import bounded, cache, config, extract, website_policy
from misaka.core.web.url_safety import normalize_url_for_request

URL = "https://source.example.org/paper"


@pytest.fixture(autouse=True)
def isolated_web(monkeypatch, tmp_path):
    monkeypatch.setitem(CFG, "web_cache", str(tmp_path / "cache"))
    for key in ("MISAKA_USAGE_DB", "MISAKA_USAGE_TASK_ID", "MISAKA_USAGE_GENERATION"):
        monkeypatch.delenv(key, raising=False)
    for name in config._CREDENTIAL_VARS + config._ENDPOINT_VARS:
        monkeypatch.delenv(name, raising=False)
    website_policy.invalidate_cache()
    yield
    website_policy.invalidate_cache()


def configure(**values):
    write_web(values)
    website_policy.invalidate_cache()


@pytest.mark.parametrize("url", ["https://faß.example/paper", "https://FAẞ.EXAMPLE/paper"])
def test_normalization_keeps_the_http_clients_host_identity(url):
    normalized = normalize_url_for_request(url)
    assert normalized.isascii()
    assert httpx.URL(normalized).raw_host == httpx.URL(url).raw_host


@pytest.mark.parametrize("operation", ["fetch", "extract", "download"])
async def test_each_url_tool_dials_the_requested_idn_not_another_domain(monkeypatch, tmp_path, operation):
    configure(backend="firecrawl", cache_enabled=False, keyless_rescue=False,
              env={"FIRECRAWL_API_KEY": "fixture-key"})
    resolved, sent = [], []

    async def resolve(host, _port):
        resolved.append(host)
        return ["93.184.216.34"]

    def handle(request):
        sent.append(request)
        if request.url.path == "/v2/scrape":
            url = json.loads(request.content)["url"]
            return httpx.Response(200, json={"success": True, "data": {"markdown": "fixture text", "metadata": {"sourceURL": url}}})
        return httpx.Response(200, text="fixture text", headers={"content-type": "text/plain"})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(
        **{**kw, "transport": httpx.MockTransport(handle), "proxy": None}))
    monkeypatch.setattr(bounded, "_resolve_host", resolve)
    url = "https://faß.example/paper"
    if operation == "extract":
        out = json.loads(await extract.web_extract_tool([url], cwd=str(tmp_path)))
        assert out["results"][0]["saved_path"]
    elif operation == "fetch":
        out = await web_fetch.create_web_fetch_tool_definition(str(tmp_path)).execute("fixture", {"url": url})
        assert out.details["saved_path"]
    else:
        out = await download_file.create_download_file_tool_definition(str(tmp_path)).execute("fixture", {"url": url, "path": "paper.txt"})
        assert out.details["path"]
    assert sent
    assert set(resolved) == {"xn--fa-hia.example"}, resolved


@pytest.mark.parametrize(("rule", "url", "blocked"), [
    ("blocked.example.org", "https://blocked.example.org/paper", True),
    ("https://blocked.example.org:443/from-bookmark", "https://blocked.example.org/paper", True),
    ("例子.测试", "https://xn--fsqu00a.xn--0zwm56d/paper", True),
    ("xn--fsqu00a.xn--0zwm56d", "https://例子.测试/paper", True),
    ("*.例子.测试", "https://sub.xn--fsqu00a.xn--0zwm56d/paper", True),
    ("*.例子.测试", "https://xn--fsqu00a.xn--0zwm56d/paper", False),
    ("例子.测试", "https://notxn--fsqu00a.xn--0zwm56d/paper", False),
    ("faß.example", "https://xn--fa-hia.example/paper", True),
    ("faß.example", "https://fass.example/paper", False),
    ("*.cdn[12].example", "https://a.cdn1.example/paper", True),
])
def test_rules_and_url_hosts_have_the_same_identity(rule, url, blocked):
    configure(website_blocklist={"enabled": True, "domains": [rule]})
    assert bool(website_policy.check_website_access(url)) is blocked


@pytest.mark.parametrize("cached", [False, True], ids=["fresh", "cached"])
async def test_reported_idn_redirect_obeys_the_same_rule_on_both_paths(monkeypatch, tmp_path, cached):
    configure(backend="firecrawl", keyless_rescue=False,
              env={"FIRECRAWL_API_KEY": "fixture-key"},
              website_blocklist={"enabled": True, "domains": ["xn--fsqu00a.xn--0zwm56d"]})
    final = "https://例子.测试/paper"
    if cached:
        cache.extract_cache_put(URL, "blocked fixture text", provider="firecrawl", metadata={"sourceURL": final})
    calls = []

    async def resolve(*_args):
        return ["93.184.216.34"]

    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"success": True, "data": {"markdown": "blocked fixture text", "metadata": {"sourceURL": final}}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(
        **{**kw, "transport": httpx.MockTransport(handle), "proxy": None}))
    monkeypatch.setattr(bounded, "_resolve_host", resolve)
    out = json.loads(await extract.web_extract_tool([URL], cwd=str(tmp_path)))
    page = out["results"][0]
    assert page.get("blocked_by_policy")
    assert page["content"] == "" and "saved_path" not in page
    assert len(calls) == (0 if cached else 1)


async def test_aborting_the_fetch_leader_does_not_abort_its_follower(monkeypatch, tmp_path):
    import asyncio

    entered, joined, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    abort = asyncio.Event()
    requests = []

    class Signal:
        @property
        def aborted(self):
            return abort.is_set()

        async def wait(self):
            await abort.wait()

    async def resolve(*_args):
        return ["93.184.216.34"]

    async def handle(request):
        requests.append(request)
        if len(requests) == 1:
            entered.set()
            try:
                await asyncio.Future()
            finally:
                closed.set()
        return httpx.Response(200, text="follower's independent result",
                              headers={"content-type": "text/plain"})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original_client(
        **{**kw, "transport": httpx.MockTransport(handle), "proxy": None}))
    monkeypatch.setattr(bounded, "_resolve_host", resolve)
    original_flight = web_fetch.single_flight
    callers = 0

    async def observe_flight(key, fn):
        nonlocal callers
        callers += 1
        if callers == 2:
            joined.set()
        return await original_flight(key, fn)

    monkeypatch.setattr(web_fetch, "single_flight", observe_flight)
    fetch = web_fetch.create_web_fetch_tool_definition(str(tmp_path))
    async with asyncio.timeout(2):
        leader = asyncio.create_task(fetch.execute("leader", {"url": URL}, Signal()))
        await entered.wait()
        follower = asyncio.create_task(fetch.execute("follower", {"url": URL}))
        await joined.wait()
        abort.set()
        with pytest.raises(RuntimeError, match="Operation aborted"):
            await leader
        out = await follower
    assert closed.is_set()
    assert len(requests) == 2 and "follower's independent result" in out.content[0].text
    assert (tmp_path / out.details["saved_path"]).is_file()


def provider_reply(monkeypatch, provider, documents):
    """Use the real wrapper/provider, replacing only DNS and HTTP transport."""
    from misaka.core.web import registry

    vendor = provider.removesuffix('-keyless')
    keyless = provider.endswith('-keyless')
    configure(backend=vendor, keyless_rescue=False,
              env={} if keyless else {f"{vendor.upper()}_API_KEY": "TOKEN"})
    registry.reset_for_tests()
    calls = []

    async def resolve(*_args):
        return ["93.184.216.34"]

    def handle(request):
        calls.append(request)
        rows = [{**row, "text": row.get("content", ""), "full_content": row.get("content", "")}
                for row in documents]
        data = rows[0] if vendor == 'keenable' else {"results": rows}
        if provider == 'parallel-keyless':
            data = {"jsonrpc": "2.0", "id": 1, "result": {
                "content": [{"type": "text", "text": json.dumps(data)}]}}
        return httpx.Response(200, json=data)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handle), **(kw | {"proxy": None})))
    monkeypatch.setattr(bounded, "_resolve_host", resolve)
    return calls


@pytest.mark.parametrize("provider", ['parallel', 'exa', 'tavily', 'keenable', 'parallel-keyless', 'keenable-keyless'])
async def test_canonical_url_preserves_body_identity_cache_and_saved_page(monkeypatch, tmp_path, provider):
    import yaml

    final = 'https://source.example.org/canonical'
    calls = provider_reply(monkeypatch, provider, [{"url": final, "title": "Page", "content": "returned body"}])
    one = json.loads(await extract.web_extract_tool([URL], cwd=str(tmp_path)))['results'][0]
    two = json.loads(await extract.web_extract_tool([URL], cwd=str(tmp_path)))['results'][0]
    assert len(calls) == 1, 'A cache hit must not repeat the vendor request'
    assert one == two
    assert one['requested_url'] == URL and one['final_url'] == final and one['input_index'] == 0
    assert one['provider'] == provider.removesuffix('-keyless')
    saved = (tmp_path / one['saved_path']).read_text()
    meta = yaml.safe_load(saved.split('---', 2)[1])
    assert meta['source_url'] == URL and meta['final_url'] == final and 'returned body' in saved


async def test_multi_url_canonical_results_without_ids_are_saved_not_guessed_or_refetched(monkeypatch, tmp_path):
    requested = [URL, 'https://source.example.org/another']
    rows = [{"url": f'https://source.example.org/canonical-{i}', "content": f'body {i}'} for i in range(2)]
    calls = provider_reply(monkeypatch, 'parallel', rows)
    for _ in range(2):
        out = json.loads(await extract.web_extract_tool(requested, cwd=str(tmp_path)))['results']
        assert len(out) == 4
        assert all(entry['error'] and not entry['content'] for entry in out[:2])
        for i, entry in enumerate(out[2:]):
            assert entry['requested_url'] is None and entry['input_index'] is None
            assert entry['association'] == 'unresolved' and entry['final_url'] == rows[i]['url']
            saved = (tmp_path / entry['saved_path']).read_text()
            assert f'body {i}' in saved and 'source_url: null' in saved
    assert len(calls) == 2, 'One batch per call, no rescue of already returned unassociated material'
    assert all(cache.extract_cache_get(url, provider='parallel') is None for url in requested)


async def test_vendor_id_maps_a_canonical_result_to_duplicate_inputs(monkeypatch, tmp_path):
    other = 'https://source.example.org/omitted'
    calls = provider_reply(monkeypatch, 'exa', [
        {'url': 'https://source.example.org/canonical', 'id': URL, 'content': 'same returned page'},
    ])
    out = json.loads(await extract.web_extract_tool([URL, URL, other], cwd=str(tmp_path)))['results']
    assert len(calls) == 1 and len(out) == 3
    assert [entry['requested_url'] for entry in out] == [URL, URL, other]
    assert [entry['input_index'] for entry in out] == [0, 1, 2]
    assert out[0]['content'] == out[1]['content'] == 'same returned page'
    assert out[0]['saved_path'] == out[1]['saved_path']
    assert out[2]['error'] and 'saved_path' not in out[2]


async def test_excess_unassociated_records_use_one_bounded_saved_json_document(monkeypatch, tmp_path):
    rows = [{'url': f'https://source.example.org/extra-{i}', 'content': f'body {i}'} for i in range(200)]
    calls = provider_reply(monkeypatch, 'parallel', rows)
    raw = await extract.web_extract_tool([URL], cwd=str(tmp_path))
    out = json.loads(raw)['results']
    assert len(calls) == 1 and len(raw) < extract.MAX_RESULT_SIZE_CHARS
    assert len(out) == 2 and out[1]['association'] == 'unresolved'
    saved = (tmp_path / out[1]['saved_path']).read_text()
    assert all(f'body {i}' in saved for i in range(200))


async def test_parallel_excerpts_stay_labelled_through_cache_and_evidence(monkeypatch, tmp_path):
    calls = provider_reply(monkeypatch, 'parallel', [{'url': URL, 'excerpts': ['only this passage']}])
    for _ in range(2):
        entry = json.loads(await extract.web_extract_tool([URL], cwd=str(tmp_path)))['results'][0]
        assert entry['content_kind'] == 'excerpts' and entry['content'] == 'only this passage'
        assert 'content_kind: "excerpts"' in (tmp_path / entry['saved_path']).read_text()
    assert len(calls) == 1


async def test_duplicate_input_urls_publish_each_saved_artifact_once(monkeypatch, tmp_path):
    calls = provider_reply(monkeypatch, 'parallel', [{'url': URL, 'content': 'page'}])
    tools = []
    extract.register(type('Collector', (), {'registerTool': lambda self, tool: tools.append(tool)})(), str(tmp_path))
    result = await tools[0].execute('call', {'urls': [URL, URL]}, None, None, None)
    assert len(calls) == 1 and len(result['details']['saved_paths']) == 1
    assert (tmp_path / result['details']['saved_paths'][0]).is_file()

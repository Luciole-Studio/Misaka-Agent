"""Regression tests for the September Web audit; no network or real accounts."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from webconf import write_web

from misaka.config import home
from misaka.core.web import cache, extract, keyless, x_search
from misaka.core.web.scope import WebScope


@pytest.fixture
def scope(tmp_path, monkeypatch):
    from misaka.config.product import CFG
    monkeypatch.setitem(CFG, 'web_cache', str(tmp_path / 'cache'))
    with WebScope(str(tmp_path)).activate() as view:
        view.config = {'env': {'EXA_API_KEY': 'fixture-super-secret-123456'}}
        view.environment = {}
        yield view

class Harness:
    def __init__(self): self.tools = {}
    def registerTool(self, definition): self.tools[definition.name] = definition

async def test_x_search_abort_has_no_successful_artifact(scope, tmp_path, monkeypatch):
    async def search(args):
        await asyncio.Event().wait()
    monkeypatch.setattr(x_search, 'search', search)
    signal = SimpleNamespace(aborted=True)
    harness = Harness()
    x_search.register(harness, str(tmp_path))
    result = await harness.tools['x_search'].execute('id', {'query': 'fixture'}, signal, None, None)
    assert result.get('isError'), result
    assert not result['details'].get('saved_path')
    assert not (tmp_path / 'downloads').exists()

async def test_browser_truncation_keeps_dialog_redacted(scope, tmp_path, monkeypatch):
    from misaka.core.web.browser import tools
    secret = 'fixture-super-secret-123456'
    async def perform(*args):
        return {'success': True, 'snapshot': 'x' * 100_001,
                'pending_dialogs': [{'id': 'fixture-dialog', 'message': secret}]}
    monkeypatch.setattr(tools, 'current_runtime', lambda: SimpleNamespace(browser=SimpleNamespace(perform=perform)))
    harness = Harness(); tools.register(harness, str(tmp_path))
    result = await harness.tools['browser_snapshot'].execute('id', {}, None, None, None)
    assert secret not in result['content'][0]['text'], result
    stored = (tmp_path / result['details']['saved_paths'][0]).read_text()
    assert secret not in stored and '<redacted>' in stored

async def test_browser_dialog_obeys_result_budget(scope, tmp_path, monkeypatch):
    from misaka.core.web.browser import tools
    async def perform(*args):
        return {'success': True, 'snapshot': '', 'pending_dialogs': [{'message': 'x' * 300_000}]}
    monkeypatch.setattr(tools, 'current_runtime', lambda: SimpleNamespace(browser=SimpleNamespace(perform=perform)))
    harness = Harness(); tools.register(harness, str(tmp_path))
    result = await harness.tools['browser_snapshot'].execute('id', {}, None, None, None)
    assert len(result['content'][0]['text']) <= 100_000

@pytest.mark.parametrize('backend', ['firecrawl-keyless', 'tavily', 'exa', 'parallel-keyless', 'keenable-keyless'])
async def test_structured_vendor_failure_is_not_cacheable_success(scope, monkeypatch, backend):
    async def handler(request):
        return httpx.Response(200, json={'success': False, 'error': 'quota exceeded'})
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: real(transport=httpx.MockTransport(handler)))
    if backend == 'firecrawl-keyless':
        result = await keyless.firecrawl_search_keyless('fixture')
    elif backend == 'parallel-keyless':
        async def call(*a, **kw): return json.dumps({'success': False, 'error': 'quota exceeded'})
        monkeypatch.setattr(keyless, 'mcp_call', call)
        result = await keyless.parallel_search_keyless('fixture')
    elif backend == 'keenable-keyless':
        result = await keyless.keenable_search_keyless('fixture')
    elif backend == 'tavily':
        from misaka.core.web.backends.tavily import TavilyWebSearchProvider
        result = await TavilyWebSearchProvider().search('fixture')
    else:
        from misaka.core.web.backends.exa import ExaWebSearchProvider
        result = await ExaWebSearchProvider().search('fixture')
    assert result.get('success') is False and 'quota exceeded' in result['error'], result
    memo = cache.SearchMemo(); memo.store(backend, 'fixture', 5, result)
    assert memo.lookup(backend, 'fixture', 5) is None

async def test_extract_keeps_restricted_original_and_redacted_evidence(scope, tmp_path):
    secret = 'fixture-super-secret-123456'
    rendered = json.loads(await extract._render([{'url': 'https://example.test/', 'content': secret}], None, str(tmp_path), 'fixture'))
    row = rendered['results'][0]
    assert secret not in row['content']
    path = tmp_path / row['saved_path']
    assert secret not in path.read_text()
    originals = list((home.path('web_evidence', tmp_path) / 'originals').glob('*.md'))
    assert len(originals) == 1 and secret in originals[0].read_text()
    assert originals[0].stat().st_mode & 0o777 == 0o600
    assert originals[0].parent.stat().st_mode & 0o777 == 0o700

async def test_controller_uses_default_deadline_not_requested_operation(scope, tmp_path, monkeypatch):
    # serve() forwards owner.run(call) without _tool_name, demonstrated separately in source.
    from misaka.core.web.runtime import WebRuntime
    from misaka.core.web.timeouts import WebOperationTimeout
    write_web({'operation_timeout': {'default': 0.01, 'browser_exec': 1}}, profile=tmp_path)
    owner = WebRuntime(scope)
    async def call(): await asyncio.sleep(.025); return 'done'
    try:
        with pytest.raises(WebOperationTimeout, match='default timed out'):
            await owner.run(call)
        assert await owner.run(call, _tool_name='browser_exec') == 'done'
    finally:
        await owner.close()

async def test_fresh_and_cached_sources_share_the_same_gate(scope, tmp_path, monkeypatch):
    from misaka.core.web.bounded import UnsafeUrlError
    from misaka.core.web.provider import WebSearchProvider
    class Provider(WebSearchProvider):
        name = 'fixture'
        def is_available(self): return True
        def supports_extract(self): return True
        async def extract(self, urls, **kw):
            return [{'url': url, 'content': 'fixture private material', 'metadata': {'sourceURL': 'http://127.0.0.1/private'}} for url in urls]
    monkeypatch.setattr(extract, 'resolve_extractor', lambda: (Provider(), 'fixture', ''))
    async def vet(url, **kw):
        if '127.0.0.1' in url: raise UnsafeUrlError('private address')
        return ('93.184.216.34',)
    monkeypatch.setattr(extract, 'vet_public_url', vet)
    first = json.loads(await extract.web_extract_tool(['https://example.test/'], cwd=str(tmp_path)))['results'][0]
    second = json.loads(await extract.web_extract_tool(['https://example.test/'], cwd=str(tmp_path)))['results'][0]
    assert first['content'] == second['content'] == ''
    assert 'Blocked source' in first['error'] and 'Blocked source' in second['error']
    assert not first.get('saved_path') and not second.get('saved_path')
    assert cache.extract_cache_get('https://example.test/', provider='fixture') is None

@pytest.mark.parametrize('name,suffix', [('web_fetch', '/page'), ('download_file', '/file.pdf')])
async def test_direct_tool_http_failure_is_error_in_agent_loop(scope, tmp_path, monkeypatch, name, suffix):
    import functools

    from misaka.agent.agent_loop import execute_prepared_tool_call
    from misaka.core.tools import download_file, web_fetch
    from misaka.core.web import WebPart, bounded
    async def resolve(*args): return ['93.184.216.34']
    monkeypatch.setattr(bounded, '_resolve_host', resolve)
    transport = httpx.MockTransport(lambda req: httpx.Response(503, text='fixture unavailable'))
    module = web_fetch if name == 'web_fetch' else download_file
    monkeypatch.setattr(module, 'open_checked_stream', functools.partial(bounded.open_checked_stream, transport=transport))
    write_web({'browser': {'enabled': False}}, profile=tmp_path)
    part = WebPart(SimpleNamespace(profile_dir=str(tmp_path), workspace=str(tmp_path)))
    prepared = SimpleNamespace(tool=next(t for t in part.tools if t.name == name),
        toolCall=SimpleNamespace(id='fixture', name=name, arguments={}), args={'url': 'https://93.184.216.34' + suffix})
    try:
        outcome = await execute_prepared_tool_call(prepared, None, lambda event: None)
        assert '503' in outcome.result.content[0].text
        assert outcome.isError is True
    finally:
        await part.runtime.close()

async def test_web_fetch_hides_server_presigned_redirect(scope, tmp_path, monkeypatch):
    import functools

    from misaka.core.tools import web_fetch
    from misaka.core.web import bounded
    signed = 'https://93.184.216.34/final?X-Amz-Signature=fixture-only-signature'
    def handler(request):
        if request.url.path == '/start': return httpx.Response(302, headers={'location': signed})
        return httpx.Response(200, text='fixture text', headers={'content-type': 'text/plain'})
    async def resolve(*args): return ['93.184.216.34']
    monkeypatch.setattr(bounded, '_resolve_host', resolve)
    monkeypatch.setattr(web_fetch, 'open_checked_stream', functools.partial(bounded.open_checked_stream, transport=httpx.MockTransport(handler)))
    result = await web_fetch.create_web_fetch_tool_definition(str(tmp_path)).execute('fixture', {'url': 'https://93.184.216.34/start'})
    assert signed not in result.content[0].text
    assert 'X-Amz-Signature' not in json.dumps(result.details)
    assert 'X-Amz-Signature' not in (tmp_path / result.details['saved_path']).read_text()

async def test_actual_reference_controller_honors_browser_operation_deadline(scope, tmp_path):
    import sys

    from misaka.core.web.browser.controller import Controller
    fixture = tmp_path / 'controller_fixture.py'
    import misaka
    fixture.write_text('import sys\nsys.path.insert(0, ' + repr(str(Path(misaka.__file__).parent.parent)) + ')\n' + '''import asyncio
from misaka.core.web.browser import BrowserManager, settings
from misaka.core.web.browser.controller import serve
settings.available_tools = lambda **kwargs: {'browser_exec'}
async def action(self, *args):
    await asyncio.sleep(.1)
    return {'success': True}
BrowserManager.perform = action
asyncio.run(serve())
''')
    cfg = {'controller_command': [sys.executable, str(fixture)]}
    write_web({'operation_timeout': {'default': .03, 'browser_exec': 2}, 'browser': cfg}, profile=tmp_path)
    controller = Controller(str(tmp_path), cfg)
    try:
        result = await controller.perform('browser_exec', {'code': 'pass', 'timeout_s': 5}, 'fixture')
        assert result['success']
    finally:
        await controller.close()

async def test_removed_oopif_leaves_frame_tree(scope):
    from misaka.core.web.browser.cdp import Supervisor
    supervisor = Supervisor(); supervisor.root = 'root'
    supervisor.sessions = {'root': 'root-session', 'child': 'child-session'}
    supervisor.frames = {'root': {'id': 'root'}, 'child': {'id': 'child', 'parentId': 'root'}}
    class Socket:
        def __aiter__(self):
            async def messages():
                for message in [
                    {'method': 'Page.frameDetached', 'sessionId': 'root-session', 'params': {'frameId': 'child', 'reason': 'remove'}},
                    {'method': 'Target.detachedFromTarget', 'sessionId': 'root-session', 'params': {'sessionId': 'child-session'}},
                ]: yield json.dumps(message)
            return messages()
    supervisor.ws = Socket()
    await supervisor._read()
    assert 'child' not in supervisor.sessions and 'child' not in supervisor.frames
    assert supervisor.frame_tree()[0]['childFrames'] == []


def test_extract_index_eviction_removes_body_file(scope, monkeypatch):
    monkeypatch.setattr(cache, '_INDEX_MAX_ENTRIES', 1)
    first, second = 'https://example.test/first', 'https://example.test/second'
    cache.extract_cache_put(first, 'one', provider='fixture')
    first_path = cache._entry_file_path(first, None, 'fixture')
    cache.extract_cache_put(second, 'two', provider='fixture')
    assert len(cache._load_index()) == 1
    assert not first_path.exists() and cache.extract_cache_get(first, provider='fixture') is None

def upstream_symbols(path, names, namespace=None):
    import ast
    source = Path(__file__).parent / 'fixtures/hermes_990473a' / path
    tree = ast.parse(source.read_text())
    selected = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
    assert {node.name for node in selected} == set(names)
    prefix = ast.parse('from __future__ import annotations').body
    ns = {} if namespace is None else namespace
    exec(compile(ast.Module(body=prefix + selected, type_ignores=[]), str(source), 'exec'), ns)  # noqa: S102 - pinned local source fixture
    return ns


def test_autodetect_perplexity_priority_matches_pinned_hermes(scope, monkeypatch):
    from misaka.core.web import registry
    scope.config['env']['PERPLEXITY_API_KEY'] = 'fixture-perplexity-key'
    monkeypatch.setattr(registry, 'ddgs_package_importable', lambda: False)
    ns = upstream_symbols('tools/web_tools.py', {'_get_backend'}, {
        '_configured_backend': lambda: None, 'selection_exists': lambda name: False,
        '_has_env': lambda key: bool(scope.config['env'].get(key)),
        '_is_tool_gateway_ready': lambda: False, '_ddgs_package_importable': lambda: False,
    })
    registry.ensure_backends_registered()
    assert ns['_get_backend']() == 'perplexity'
    assert registry.backend_name() == 'perplexity'
    assert registry.resolve_search_provider().name == 'perplexity'
    import ast
    source = Path(__file__).parent / 'fixtures/hermes_990473a/agent/web_search_registry.py'
    tree = ast.parse(source.read_text())
    preference = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign) and getattr(n.targets[0], 'id', '') == '_LEGACY_PREFERENCE')
    assert preference.index('perplexity') < preference.index('exa')
    assert registry._LEGACY_PREFERENCE.index('perplexity') < registry._LEGACY_PREFERENCE.index('exa')


def test_pinned_hermes_also_accepts_firecrawl_200_error(monkeypatch):
    ns = upstream_symbols('plugins/web/firecrawl/provider.py',
        {'_KeylessFirecrawlClient', '_to_plain_object', '_normalize_result_list', '_extract_web_search_results'},
        {'httpx': httpx, '_FIRECRAWL_CLOUD_API_URL': 'https://fixture.invalid'})
    monkeypatch.setattr(httpx, 'post', lambda *args, **kwargs: httpx.Response(200,
        json={'success': False, 'error': 'quota exceeded'}, request=httpx.Request('POST', 'https://fixture.invalid')))
    data = ns['_KeylessFirecrawlClient']().search(query='fixture')
    assert data['success'] is False and ns['_extract_web_search_results'](data) == []


@pytest.mark.parametrize('kind', ['async', 'sync', 'swallows_cancel'])
@pytest.mark.parametrize('rescue', [False, True])
async def test_extract_dispatch_timeout_drains_before_rescue(scope, monkeypatch, kind, rescue):
    import threading

    from misaka.core.web import dispatch

    cleaned = threading.Event()
    scope.config['extract_timeout'] = .01
    async def extract_async(urls, **kwargs):
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            if kind != 'swallows_cancel':
                raise
            return [{'url': urls[0], 'content': 'late success'}]
        finally:
            await asyncio.sleep(.01)
            cleaned.set()
    def extract_sync(urls, **kwargs):
        cleaned.wait(.03)
        cleaned.set()
        return [{'url': urls[0], 'content': 'late success'}]
    async def rescue_extract(name, urls, failed):
        assert cleaned.is_set() and 'timed out' in failed[0]['error']
        return [{'url': urls[0], 'content': 'rescued'}]
    monkeypatch.setattr(dispatch, 'rescue_eligible', lambda provider: rescue)
    monkeypatch.setattr(dispatch, 'rescue_extract', rescue_extract)
    provider = SimpleNamespace(name='fixture', extract=extract_sync if kind == 'sync' else extract_async)
    results, rescued = await dispatch.web_extract(provider, ['https://example.test/'])
    assert cleaned.is_set() and rescued is rescue
    assert results[0]['content'] == ('rescued' if rescue else '')


@pytest.mark.parametrize('timeout', [0, -1])
async def test_disabling_extract_inner_timeout_keeps_success(scope, timeout):
    from misaka.core.web import dispatch

    scope.config['extract_timeout'] = timeout
    async def extract_async(urls, **kwargs):
        await asyncio.sleep(.01)
        return [{'url': urls[0], 'content': 'fixture'}]
    result, rescued = await dispatch.web_extract(SimpleNamespace(name='fixture', extract=extract_async),
                                                ['https://example.test/'])
    assert not rescued and result[0]['content'] == 'fixture'


async def test_outer_extract_cancellation_does_not_start_rescue(scope, monkeypatch):
    from misaka.core.web import dispatch

    entered, cleaned = asyncio.Event(), asyncio.Event()
    async def extract_async(urls, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(.01)
            cleaned.set()
    async def rescue_extract(*args):
        pytest.fail('caller cancellation must not dispatch rescue')
    monkeypatch.setattr(dispatch, 'rescue_extract', rescue_extract)
    task = asyncio.create_task(dispatch.web_extract(SimpleNamespace(name='fixture', extract=extract_async),
                                                    ['https://example.test/']))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()

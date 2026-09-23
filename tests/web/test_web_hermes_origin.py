"""MISAKA-only Web repairs; shared Hermes quirks are deliberately retained.

Origin and scope: docs/audits/web-sweep-2026-09-10/upstream-origin.md.
Only HTTP transport / terminal engine are stubbed; SDK and lifecycle paths are real.
"""
import asyncio
import json
import os
import sys
from types import SimpleNamespace

import httpx
import pytest
from webconf import write_web

from misaka.core.web import WebPart, config, keyless, x_search
from misaka.core.web.browser import BrowserManager, settings
from misaka.core.web.browser.session import BrowserSession
from misaka.core.web.runtime import WebRuntime
from misaka.core.web.scope import WebScope
from misaka.core.wiring import SessionSpec


@pytest.fixture
def scope(tmp_path, monkeypatch):
    for key in config.provider_variables() | {'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
            'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy', 'SSL_CERT_FILE', 'SSL_CERT_DIR'}:
        monkeypatch.delenv(key, raising=False)
    with WebScope(str(tmp_path)).activate():
        yield tmp_path


async def test_keyless_firecrawl_uses_profile_network_without_changing_hermes_client_lifetime(scope, monkeypatch):
    route = 'http://proxy.example.test:8080'
    write_web({'env': {'HTTPS_PROXY': route}}, profile=scope)
    constructed = []
    real_client = httpx.AsyncClient
    def client(**kwargs):
        constructed.append(kwargs)
        # Capture production constructor contract; every exchange stays in memory.
        return real_client(transport=httpx.MockTransport(lambda req: httpx.Response(200,
            json={'success': True, 'data': {'web': [{'url': 'https://page.test/', 'title': 'Fixture', 'description': 'Material'}]}}, request=req)), trust_env=False)
    monkeypatch.setattr(httpx, 'AsyncClient', client)
    runtime = WebRuntime(WebScope(str(scope)))
    try:
        for _ in range(2):
            result = await runtime.run(keyless.firecrawl_search_keyless, 'fixture', _tool_name='web_search')
            assert result['success'], result
        assert len(constructed) == 2 and all(row.get('trust_env') is False and row.get('proxy') == route for row in constructed), constructed
    finally:
        await runtime.close()


@pytest.mark.parametrize('provider,operation', [('brave-free', 'search'), ('searxng', 'search'), ('tavily', 'search'), ('tavily', 'extract')])
async def test_other_api_routes_use_profile_network_without_changing_hermes_client_lifetime(scope, monkeypatch, provider, operation):
    from misaka.core.web.backends.brave_free import BraveFreeWebSearchProvider
    from misaka.core.web.backends.searxng import SearXNGWebSearchProvider
    from misaka.core.web.backends.tavily import TavilyWebSearchProvider
    route = 'http://proxy.example.test:8080'
    write_web({'env': {'HTTPS_PROXY': route}}, profile=scope)
    monkeypatch.setenv('BRAVE_SEARCH_API_KEY', 'fixture-brave-key-12345')
    monkeypatch.setenv('SEARXNG_URL', 'https://searx.example.test')
    monkeypatch.setenv('TAVILY_API_KEY', 'fixture-tavily-key-12345')
    instance = {'brave-free': BraveFreeWebSearchProvider, 'searxng': SearXNGWebSearchProvider, 'tavily': TavilyWebSearchProvider}[provider]()
    constructed = []
    real_client = httpx.AsyncClient
    row = {'url': 'https://page.test/', 'title': 'Fixture', 'description': 'Material', 'content': 'Material'}
    def client(**kwargs):
        constructed.append(kwargs)
        return real_client(transport=httpx.MockTransport(lambda req: httpx.Response(200,
            json={'web': {'results': [row]}, 'results': [row]}, request=req)), trust_env=False)
    monkeypatch.setattr(httpx, 'AsyncClient', client)
    runtime = WebRuntime(WebScope(str(scope)))
    try:
        for _ in range(2):
            result = await runtime.run(getattr(instance, operation), ['https://page.test/'] if operation == 'extract' else 'fixture', _tool_name='web_' + operation)
            assert (result[0].get('content') if operation == 'extract' else result['success']), result
        assert len(constructed) == 2 and all(row.get('trust_env') is False and row.get('proxy') == route for row in constructed), constructed
    finally:
        await runtime.close()


@pytest.mark.parametrize('encoding', ['utf-8-sig', 'utf-16', 'utf-32'])
async def test_download_keeps_valid_unicode_dataset(scope, monkeypatch, encoding):
    import functools

    from misaka.core.tools import download_file
    from misaka.core.web import bounded
    content = 'country\tvalue\n日本\t12\n中国\t13\n'.encode(encoding)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=content,
        headers={'content-type': 'text/tab-separated-values; charset=' + encoding}, request=request))
    monkeypatch.setattr(download_file, 'open_checked_stream', functools.partial(bounded.open_checked_stream, transport=transport))
    tool = download_file.create_download_file_tool_definition(str(scope))
    result = await tool.execute('unicode-dataset', {'url': 'https://93.184.216.34/data.tsv'})
    path = scope / 'downloads' / 'data.tsv'
    assert path.is_file(), result.content[0].text
    assert path.read_bytes() == content


async def test_xai_web_refresh_uses_profile_network(scope, monkeypatch):
    from misaka.core.web.backends.xai import _resolve_credentials
    route = 'http://proxy.example.test:8080'
    write_web({'env': {'HTTPS_PROXY': route}}, profile=scope)
    (scope / 'auth.json').write_text(json.dumps({'xai': {'type': 'oauth', 'access': 'fixture-old-access', 'refresh': 'fixture-old-refresh', 'expires': 0}}))
    constructed = []
    real_client = httpx.AsyncClient
    def client(**kwargs):
        constructed.append(kwargs)
        return real_client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={
            'access_token': 'fixture-new-access', 'refresh_token': 'fixture-new-refresh', 'expires_in': 3600}, request=req)), trust_env=False)
    monkeypatch.setattr(httpx, 'AsyncClient', client)
    runtime = WebRuntime(WebScope(str(scope)))
    try:
        token, account = await runtime.run(_resolve_credentials, _tool_name='web_search')
        assert token == 'fixture-new-access' and account is not None
        assert constructed and constructed[0].get('trust_env') is False and constructed[0].get('proxy') == route, constructed
    finally:
        await runtime.close()


async def test_xai_login_abort_drains_owned_request(scope, monkeypatch):
    import asyncio

    from misaka.ai.utils.abort import AbortController
    from misaka.ai.utils.oauth.xai import _http_post_form
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    actual_clients = []
    real_client = httpx.AsyncClient
    async def handler(request):
        entered.set()
        try:
            await release.wait()
            return httpx.Response(200, json={}, request=request)
        finally:
            finished.set()
    def client(**kwargs):
        value = real_client(transport=httpx.MockTransport(handler), trust_env=False)
        actual_clients.append(value)
        return value
    monkeypatch.setattr(httpx, 'AsyncClient', client)
    signal = AbortController()
    task = asyncio.create_task(_http_post_form('https://auth.example.test/token', {'device_code': 'fixture'}, signal))
    try:
        await entered.wait()
        signal.abort()
        with pytest.raises(RuntimeError, match='cancelled'):
            await task
        assert finished.is_set() and all(client.is_closed for client in actual_clients), 'Login returned cancelled while its owned HTTP operation/client were still alive'
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 2)
        for _ in range(20):
            if all(client.is_closed for client in actual_clients):
                break
            await asyncio.sleep(0.01)
        assert all(client.is_closed for client in actual_clients), 'Fixture teardown did not drain the released request'


@pytest.mark.parametrize('base_href', ['', 'https://archive.example.test/collection/'])
async def test_web_fetch_preserves_document_base_links(scope, monkeypatch, base_href):
    import functools

    from misaka.core.tools import web_fetch
    from misaka.core.web import bounded
    html = ('<html><head>' + (f'<base href="{base_href}">' if base_href else '')
            + '</head><body><p>Read the <a href="paper.pdf">full paper</a>.</p></body></html>')
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=html,
        headers={'content-type': 'text/html; charset=utf-8'}, request=request))
    monkeypatch.setattr(web_fetch, 'open_checked_stream', functools.partial(bounded.open_checked_stream, transport=transport))
    result = await web_fetch.create_web_fetch_tool_definition(str(scope)).execute('base-link', {'url': 'https://93.184.216.34/start'})
    expected = (base_href or 'https://93.184.216.34/') + 'paper.pdf'
    assert expected in result.content[0].text, result.content[0].text


@pytest.fixture
async def web_session(scope, monkeypatch):
    import sys

    from misaka.core.auth_storage import AuthStorage
    from misaka.core.model_registry import ModelRegistry
    from misaka.core.resource_loader import DefaultResourceLoader
    from misaka.core.sdk import create_agent_session
    from misaka.core.session_manager import SessionManager
    from misaka.core.web import WebPart
    from misaka.core.web.browser import settings
    from misaka.core.wiring import SessionSpec
    monkeypatch.setattr(settings, 'executable', lambda _: sys.executable)
    monkeypatch.setenv('XAI_API_KEY', 'fixture-xai-key-only-for-tool-registration')
    write_web({'browser': {'backend': 'agent-browser'}}, profile=scope)
    owner = WebPart(SessionSpec(str(scope), 'sister', str(scope), 'bare'))
    auth = AuthStorage.inMemory()
    loader = DefaultResourceLoader({'cwd': str(scope), 'agentDir': str(scope), 'noExtensions': True,
                                    'noContextFiles': True, 'noPromptTemplates': True, 'noThemes': True})
    await loader.reload()
    session = (await create_agent_session({'cwd': str(scope), 'agentDir': str(scope), 'authStorage': auth,
        'modelRegistry': ModelRegistry.inMemory(auth), 'resourceLoader': loader, 'parts': [owner],
        'customTools': list(owner.tools), 'sessionManager': SessionManager.inMemory(str(scope))}))['session']
    try:
        yield session
    finally:
        session.dispose()
        await owner.runtime.close()


async def _tool_batch(session, calls, execution="sequential"):
    from misaka.agent.agent_loop import execute_tool_calls
    from misaka.agent.types import AgentContext, AgentLoopConfig
    from misaka.ai.types import AssistantMessage, Usage
    message = AssistantMessage(content=calls, api='test', provider='test', model='test',
        usage=Usage(input=0, output=0, cacheRead=0, cacheWrite=0, totalTokens=0,
                    cost={'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0, 'total': 0}),
        stopReason='toolUse', timestamp=0)
    context = AgentContext(systemPrompt='', messages=[], tools=session.agent.state.tools)
    cfg = AgentLoopConfig(model=None, convertToLlm=lambda messages: messages, toolExecution=execution,
                          beforeToolCall=session.agent.beforeToolCall, afterToolCall=session.agent.afterToolCall)
    events = []
    return await execute_tool_calls(context, message, cfg, None, events.append), events


@pytest.mark.parametrize('tool_name', ['browser_click', 'x_search'])
async def test_web_failure_remains_error_at_real_host_boundary(web_session, monkeypatch, tool_name):
    from misaka.ai.types import ToolCall
    from misaka.core.web import x_search
    from misaka.core.web.browser import BrowserManager
    async def fail(*args, **kwargs):
        raise ValueError('fixture web action failed')
    monkeypatch.setattr(BrowserManager, 'perform', fail)
    monkeypatch.setattr(x_search, 'post_responses', fail)
    arguments = {'ref': '@e1'} if tool_name == 'browser_click' else {'query': 'fixture'}
    batch, events = await _tool_batch(web_session, [ToolCall(id='web-failed-once', name=tool_name, arguments=arguments)])
    assert len(batch.messages) == 1 and 'fixture web action failed' in batch.messages[0].content[0].text
    assert batch.messages[0].isError is True
    assert [e.isError for e in events if e.type == 'tool_execution_end'] == [True]


@pytest.mark.parametrize('execution', ['sequential', 'parallel'])
async def test_duplicate_id_is_renamed_without_dropping_calls(web_session, monkeypatch, execution):
    from misaka.ai.types import ToolCall
    from misaka.core.web.browser import BrowserManager
    actions = []
    async def perform(self, name, args, call_id=''):
        actions.append(call_id)
        return {'success': True, 'clicked': True}
    monkeypatch.setattr(BrowserManager, 'perform', perform)
    call = ToolCall(id='same-logical-browser-click', name='browser_click', arguments={'ref': '@e1'})
    batch, events = await _tool_batch(web_session, [call, call.model_copy(deep=True)], execution)
    terminals = [e for e in events if e.type == 'tool_execution_end']
    assert len(actions) == len(batch.messages) == len(terminals) == 2
    expected = ['same-logical-browser-click', 'same-logical-browser-click_d2']
    assert sorted(actions) == expected
    assert [message.toolCallId for message in batch.messages] == expected
    assert [event.toolCallId for event in terminals] == expected


async def test_disabling_browser_blocks_existing_runtime(scope, monkeypatch):
    cfg = {'command': sys.executable, 'backend': 'agent-browser', 'cloud_provider': 'local'}
    write_web({'browser': cfg}, profile=scope)
    part = WebPart(SessionSpec(str(scope), 'sister', str(scope), 'bare'))
    tool = next(t for t in part.tools if t.name == 'browser_snapshot')
    calls = []
    async def perform(self, name, args):
        calls.append(name)
        return {'success': True, 'snapshot': 'fixture'}
    monkeypatch.setattr(BrowserSession, 'perform', perform)
    config.update_config({'browser.enabled': 'false'})
    try:
        with pytest.raises(RuntimeError, match='disabled'):
            await tool.execute('disabled-browser-call', {}, None, None, SimpleNamespace())
        assert not calls
    finally:
        await part.runtime.close()


async def test_browser_snapshot_failed_save_does_not_invent_read_pointer(scope, monkeypatch):
    write_web({'browser': {'command': sys.executable, 'backend': 'agent-browser'}}, profile=scope)
    part = WebPart(SessionSpec(str(scope), 'sister', str(scope), 'bare'))
    tool = next(t for t in part.tools if t.name == 'browser_snapshot')
    async def perform(self, name, args, call_id=''):
        return {'success': True, 'snapshot': 'material\n' * 5000}
    monkeypatch.setattr(BrowserManager, 'perform', perform)
    (scope / 'downloads').write_text('Fixture: downloads is a file, so saving fails')
    try:
        result = await tool.execute('snapshot-storage-failure', {}, None, None, SimpleNamespace())
        text = result['content'][0]['text']
        assert '"read"' not in text and '"storage_error"' in text, text[:300]
    finally:
        await part.runtime.close()


async def test_x_search_failed_save_does_not_invent_read_pointer(scope, monkeypatch):
    definitions = []
    x_search.register(SimpleNamespace(registerTool=definitions.append), str(scope))
    async def search(args):
        return {'success': True, 'answer': 'material\n' * 15000}
    monkeypatch.setattr(x_search, 'search', search)
    (scope / 'downloads').write_text('Fixture: downloads is a file, so saving fails')
    result = await definitions[0].execute('x-storage-failure', {'query': 'fixture'}, None, None, SimpleNamespace())
    text = result['content'][0]['text']
    assert '"read"' not in text and '"storage_error"' in text, text[:300]


def test_public_gateway_domain_is_not_redacted_as_a_secret(scope, monkeypatch):
    monkeypatch.setenv('TOOL_GATEWAY_DOMAIN', 'gateway.example.test')
    citation = 'https://gateway.example.test/paper'
    assert config.redact_secrets(citation) == citation


@pytest.mark.parametrize('failure', ['nonzero', 'missing_executable'])
async def test_failed_browser_use_reload_still_reaps_owned_harness(scope, monkeypatch, failure):
    import psutil

    from misaka.core.web.browser.ownership import alive
    # Real subprocess, PID+birth-time ownership; only the shutdown CLI is a failing fixture.
    cli = scope / 'browser-use-failing-reload'
    cli.write_text(f'#!{sys.executable}\nimport sys\nsys.exit(1)\n')
    cli.chmod(0o700)
    monkeypatch.setattr(settings, 'executable', lambda name: str(cli if failure == 'nonzero' else scope / 'absent-fixture'))
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time; time.sleep(60)', start_new_session=True)
    session = BrowserSession(str(scope), {}, 'local')
    session.root = scope / 'owned-session'
    session.root.mkdir()
    session.exec_env = {'PATH': os.defpath}
    session.harness_identity = (process.pid, psutil.Process(process.pid).create_time())
    try:
        with pytest.raises(RuntimeError, match='cleanup errors'):
            await session.close()
        assert not alive(session.harness_identity), 'Failed --reload skipped the existing PID-fenced harness reaper'
    finally:
        if process.returncode is None:
            process.terminate()
        await process.wait()



@pytest.mark.parametrize('suffix', ['.txt', '.csv', '.tsv'])
@pytest.mark.parametrize('encoding', ['utf-16-le', 'utf-16-be', 'utf-32-le', 'utf-32-be'])
def test_unicode_signature_prefix_is_not_binary(suffix, encoding):
    import codecs

    from misaka.core.tools.download_file import _magic_mismatch
    bom = getattr(codecs, 'BOM_' + encoding.replace('-', '', 1).replace('-', '_').upper())
    head = bom + 'country\t日本\t\U0001f340\n'.encode(encoding)
    assert not _magic_mismatch(suffix, head)
    # The sampled prefix is allowed to stop inside a codepoint.
    assert not _magic_mismatch(suffix, head[:-1])
    assert _magic_mismatch(suffix, bom + 'header\x00'.encode(encoding))
    assert _magic_mismatch('.pdf', head)  # No relaxation of PDF magic checks.


@pytest.mark.parametrize('head', [b'PK\x00\x00payload', b'\xff\xfe\x00\xdc', b'\xff\xfe\x00\x00\x00\x00\x11\x00'])
def test_binary_or_malformed_unicode_is_still_rejected(head):
    from misaka.core.tools.download_file import _magic_mismatch
    assert _magic_mismatch('.csv', head)


@pytest.mark.parametrize('prefix,expected', [
    ('<base href="/collection/"><base href="https://ignored.example/">', 'https://page.example/collection/paper.pdf'),
    ('<base target="_blank"><base href="../archive/">', 'https://page.example/archive/paper.pdf'),
    ('<base href=""><base href="https://ignored.example/">', 'https://page.example/start/paper.pdf'),
    ('<template><base href="https://hidden.example/"></template>', 'https://page.example/start/paper.pdf'),
    ('<base href="javascript:alert(1)"><base href="https://ignored.example/">', 'https://page.example/start/paper.pdf'),
])
def test_document_base_first_href_and_hidden_elements(prefix, expected):
    from misaka.core.documents.htmltext import readable
    text, _ = readable(prefix + '<a href="paper.pdf">Paper</a>', 'https://page.example/start/index.html')
    assert text == f'[Paper]({expected})'


def test_controller_gets_only_browser_accounts_and_explicit_profile_route(scope, monkeypatch):
    monkeypatch.setenv('BROWSERBASE_API_KEY', 'fixture-browserbase-secret')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'fixture-unrelated-secret')
    monkeypatch.setenv('AGENT_BROWSER_AUTO_CONNECT', '1')
    config.update_config({'env.BROWSERBASE_PROJECT_ID': 'fixture-project',
                          'env.HTTPS_PROXY': 'http://proxy.example.test:8080'})
    engine = settings.subprocess_env()
    child = engine | settings.provider_environment()
    assert child['BROWSERBASE_API_KEY'] == 'fixture-browserbase-secret'
    assert child['BROWSERBASE_PROJECT_ID'] == 'fixture-project'
    assert child['HTTPS_PROXY'] == 'http://proxy.example.test:8080'
    assert 'BROWSERBASE_API_KEY' not in engine
    assert 'ANTHROPIC_API_KEY' not in child and 'AGENT_BROWSER_AUTO_CONNECT' not in child


async def test_failure_envelope_not_only_exceptions_reaches_pi(web_session, monkeypatch):
    from misaka.ai.types import ToolCall
    async def perform(*args, **kwargs):
        return {'success': False, 'error': 'fixture failed without raising'}
    monkeypatch.setattr(BrowserManager, 'perform', perform)
    batch, events = await _tool_batch(web_session, [ToolCall(id='failed-envelope', name='browser_click', arguments={'ref': '@e1'})])
    assert batch.messages[0].isError is True
    assert [e.isError for e in events if e.type == 'tool_execution_end'] == [True]
    assert 'fixture failed without raising' in batch.messages[0].content[0].text


@pytest.mark.parametrize('terminal', [False, True])
async def test_duplicate_ids_are_normalized_before_final_message_is_persisted(terminal):
    from test_agent_loop_truncated_tools import _assistant

    from misaka.agent.agent import Agent
    from misaka.agent.agent_loop import stream_assistant_response
    from misaka.agent.types import AgentContext, AgentLoopConfig
    from misaka.ai.types import ToolCall
    from misaka.ai.utils.event_stream import EventStream
    ids = ['call|item_a', 'call_d2|item_b', 'call|item_c', 'call|item_d']
    message = _assistant([ToolCall(id=value, name='browser_click', arguments={'ref': '@e1'}) for value in ids], 'toolUse')
    stream = EventStream(lambda e: e.type == 'done', lambda _: message)
    if terminal:
        stream.push(SimpleNamespace(type='done'))
    else:
        stream.end(message)
    context = AgentContext(systemPrompt='', messages=[], tools=[])
    events = []
    result = await stream_assistant_response(context,
        AgentLoopConfig(model=Agent().state.model, convertToLlm=lambda m: m), None, events.append,
        lambda *_: stream)
    expected = ['call|item_a', 'call_d2|item_b', 'call_d3|item_c', 'call_d4|item_d']
    assert [block.id for block in result.content] == expected
    assert [block.id for block in context.messages[-1].content] == expected
    assert [block.id for block in events[-1].message.content] == expected


async def test_xai_request_task_cancellation_drains_client(scope, monkeypatch):
    from misaka.ai.utils.oauth.xai import _http_post_form
    entered, finished = asyncio.Event(), asyncio.Event()
    real_client = httpx.AsyncClient
    clients = []
    async def handler(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()
    def client(**kwargs):
        value = real_client(transport=httpx.MockTransport(handler), trust_env=False)
        clients.append(value)
        return value
    monkeypatch.setattr(httpx, 'AsyncClient', client)
    task = asyncio.create_task(_http_post_form('https://auth.example.test/token', {}))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set() and all(c.is_closed for c in clients)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_oauth_profile_route_is_context_local_and_not_left_on_other_logins(scope, monkeypatch):
    from misaka.ai.utils.oauth.xai import _http_post_form, with_http_options
    from misaka.core.web.network import api_network_options
    route = 'http://proxy.example.test:8080'
    write_web({'env': {'HTTPS_PROXY': route}}, profile=scope)
    seen = []
    real_client = httpx.AsyncClient
    def client(**kwargs):
        seen.append(kwargs)
        return real_client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={}, request=req)), trust_env=False)
    monkeypatch.setattr(httpx, 'AsyncClient', client)
    with with_http_options(api_network_options):
        await _http_post_form('https://auth.example.test/token', {})
    await _http_post_form('https://auth.example.test/token', {})
    assert seen[0]['proxy'] == route and seen[0]['trust_env'] is False
    assert seen[1] == {'timeout': 30}


@pytest.mark.parametrize('name', ['web_search', 'web_extract'])
@pytest.mark.parametrize('failure', [True, False])
async def test_json_web_error_contract_reaches_host_without_judging_prose(web_session, monkeypatch, name, failure):
    from misaka.ai.types import ToolCall
    from misaka.core.web import extract, tool
    payload = ({'success': False, 'error': 'fixture structured failure'} if failure else
               {'success': True, 'data': {'web': []}, 'results': [{'content': 'A page quoting "error" and "failed".', 'error': None}], 'quote': '"error" and "failed" are words in a document'})
    async def respond(*args, **kwargs):
        return json.dumps(payload)
    monkeypatch.setattr(tool if name == 'web_search' else extract, name + '_tool', respond)
    args = {'query': 'fixture'} if name == 'web_search' else {'urls': ['https://page.example/']}
    batch, events = await _tool_batch(web_session, [ToolCall(id='json-result', name=name, arguments=args)])
    assert batch.messages[0].isError is failure
    assert [event.isError for event in events if event.type == 'tool_execution_end'] == [failure]


def test_id_normalization_matches_hermes_whitespace_and_preserves_arguments():
    from test_agent_loop_truncated_tools import _assistant

    from misaka.agent.agent_loop import _uniquify_tool_call_ids
    from misaka.ai.types import ToolCall
    message = _assistant([ToolCall(id=value, name='browser_type', arguments={'text': 'same content'})
                          for value in [' call ', 'call', 'call_d2', '', 'call| item ']], 'toolUse')
    _uniquify_tool_call_ids(message)
    assert [block.id for block in message.content] == [' call ', 'call_d2', 'call_d2_d2', '', 'call_d3| item ']
    assert all(block.arguments == {'text': 'same content'} for block in message.content)


async def test_api_and_forced_oauth_refresh_honor_profile_ca_and_no_proxy(scope, monkeypatch):
    import ssl

    import certifi

    from misaka.core.auth_storage import AuthStorage
    from misaka.core.web.backends.brave_free import BraveFreeWebSearchProvider
    from misaka.core.web.backends.xai import OAuthAccount, _force_refresh_oauth_token
    write_web({'env': {'HTTPS_PROXY': 'http://proxy.example.test:8080',
        'NO_PROXY': 'auth.x.ai,api.search.brave.com', 'SSL_CERT_FILE': certifi.where()}}, profile=scope)
    monkeypatch.setenv('BRAVE_SEARCH_API_KEY', 'fixture-brave-secret')
    seen = []
    real_client = httpx.AsyncClient
    def client(**kwargs):
        seen.append(kwargs)
        return real_client(transport=httpx.MockTransport(lambda req: httpx.Response(200, request=req, json={
            'web': {'results': []}, 'access_token': 'fixture-new-access', 'refresh_token': 'fixture-new-refresh', 'expires_in': 3600})), trust_env=False)
    monkeypatch.setattr(httpx, 'AsyncClient', client)
    storage = AuthStorage.inMemory({'xai:named': {'type': 'oauth', 'access': 'fixture-old-access',
        'refresh': 'fixture-old-refresh', 'expires': 9_999_999_999_999}})
    runtime = WebRuntime(WebScope(str(scope)))
    try:
        result = await runtime.run(BraveFreeWebSearchProvider().search, 'fixture')
        assert result['success']
        token = await runtime.run(_force_refresh_oauth_token, OAuthAccount(storage, 'named'), 'fixture-old-access')
        assert token == 'fixture-new-access'
        assert len(seen) == 2
        assert all(row['proxy'] is None and row['trust_env'] is False and
                   isinstance(row['verify'], ssl.SSLContext) and row['verify'].verify_mode == ssl.CERT_REQUIRED for row in seen)
    finally:
        await runtime.close()

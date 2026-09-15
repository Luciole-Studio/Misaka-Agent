"""Browser/gateway protocol and permission contracts, with no real credentials."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from misaka.config.product import CFG
from misaka.core.subagent.policy import _permission_action
from misaka.core.web import WebPart, config, dispatch, gateway, registry
from misaka.core.web.browser import BrowserManager, settings
from misaka.core.web.browser.providers import CloudProvider
from misaka.core.web.browser.session import BrowserSession
from misaka.core.web.runtime import WebRuntime
from misaka.core.web.scope import WebScope
from misaka.core.wiring import SessionSpec


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setitem(CFG, 'web_config', str(tmp_path / 'shared.json'))
    for key in config.provider_variables() | {'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
            'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy', 'SSL_CERT_FILE', 'SSL_CERT_DIR'}:
        monkeypatch.delenv(key, raising=False)
    with WebScope(str(tmp_path)).activate():
        yield tmp_path


def fake_network(monkeypatch, handler):
    real = httpx.AsyncClient
    requests, clients = [], []
    def respond(request):
        requests.append(request)
        return handler(request)
    def factory(**kw):
        client = real(**(kw | {'proxy': None, 'transport': httpx.MockTransport(respond)}))
        clients.append(client)
        return client
    monkeypatch.setattr(httpx, 'AsyncClient', factory)
    return requests, clients


@pytest.mark.parametrize('vendor,env,create_path,release_method,release_body', [
    ('browserbase', {'BROWSERBASE_API_KEY': 'key', 'BROWSERBASE_PROJECT_ID': 'project'}, '/v1/sessions', 'POST', {'projectId': 'project', 'status': 'REQUEST_RELEASE'}),
    ('browser-use', {'BROWSER_USE_API_KEY': 'key'}, '/api/v3/browsers', 'PATCH', {'action': 'stop'}),
    ('firecrawl', {'FIRECRAWL_API_KEY': 'key'}, '/v2/browser', 'DELETE', None),
])
async def test_cloud_create_and_close_exactly_once(monkeypatch, vendor, env, create_path, release_method, release_body):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    requests, clients = fake_network(monkeypatch, lambda r: httpx.Response(200, json={
        'id': 'owned', 'cdpUrl': 'wss://browser.test/devtools/browser/owned', 'timeoutAt': '2099-01-01T00:00:00Z'}))
    runtime = WebRuntime()
    async def run():
        lease = await CloudProvider(vendor).create_session('owner')
        assert lease.expires_at and lease.cdp_url
        await lease.close()
        await lease.close()
    try:
        await runtime.run(run)
    finally:
        await runtime.close()
    assert [(r.method, r.url.path) for r in requests] == [('POST', create_path), (release_method, create_path + '/owned')]
    assert (json.loads(requests[1].content) if requests[1].content else None) == release_body
    assert all(c.is_closed for c in clients)


@pytest.mark.parametrize('status', [400, 401, 403, 409, 429, 500, 502])
async def test_failed_cloud_create_not_replayed_or_local_fallback(monkeypatch, isolated, status):
    monkeypatch.setenv('BROWSER_USE_API_KEY', 'key')
    monkeypatch.setattr(settings, 'executable', lambda _: sys.executable)
    requests, _ = fake_network(monkeypatch, lambda r: httpx.Response(status, json={'error': 'fixture'}))
    session = BrowserSession(str(isolated), {}, 'browser-use')
    try:
        for _ in range(2):
            with pytest.raises(httpx.HTTPStatusError):
                await session.start()
    finally:
        await session.close()
    assert len(requests) == 1
    assert not session.root.exists()


async def test_browserbase_only_explicit_402_feature_downgrade(monkeypatch):
    monkeypatch.setenv('BROWSERBASE_API_KEY', 'key')
    monkeypatch.setenv('BROWSERBASE_PROJECT_ID', 'project')
    bodies = []
    def reply(r):
        bodies.append(json.loads(r.content))
        return httpx.Response(402 if len(bodies) < 3 else 200, json={'id': 'one', 'connectUrl': 'wss://browser.test/socket'})
    fake_network(monkeypatch, reply)
    lease = await CloudProvider('browserbase').create_session('owner')
    assert bodies == [{'projectId': 'project', 'keepAlive': True, 'proxies': True},
                      {'projectId': 'project', 'proxies': True}, {'projectId': 'project'}]
    assert not lease.features['proxies'] and not lease.features['keep_alive']


async def test_close_failure_retains_owner_for_retry(monkeypatch, isolated):
    from misaka.core.web.browser.providers import CloudLease
    codes = iter([503, 200])
    requests, _ = fake_network(monkeypatch, lambda r: httpx.Response(next(codes), json={}))
    owner = BrowserManager(str(isolated))
    session = BrowserSession(str(isolated), {}, 'local')
    session.lease = CloudLease('one', '', {}, None, 'https://cloud.test/one', {}, 'DELETE', None, 'fixture')
    owner.sessions['one'] = session
    with pytest.raises(RuntimeError, match='cleanup failed'):
        await owner.close()
    assert 'one' in owner.sessions and not session.closed
    await owner.close()
    assert not owner.sessions and session.closed and len(requests) == 2


async def test_camofox_scoped_user_and_only_own_tab_cleanup(monkeypatch, isolated):
    monkeypatch.setenv('CAMOFOX_URL', 'http://127.0.0.1:9377')
    monkeypatch.setenv('CAMOFOX_API_KEY', 'private-key')
    monkeypatch.setattr('misaka.core.web.browser.session.check_url', async_identity)
    def reply(r):
        if r.url.path == '/tabs':
            return httpx.Response(200, json={'tabId': 'one', 'url': 'https://page.test/'})
        if r.url.path.endswith('/snapshot'):
            return httpx.Response(200, json={'snapshot': '- textbox "Name" [ref=e1]', 'url': 'https://page.test/', 'refsCount': 1})
        return httpx.Response(200, json={'success': True})
    requests, _ = fake_network(monkeypatch, reply)
    session = BrowserSession(str(isolated), {}, 'camofox')
    try:
        await session.perform('browser_navigate', {'url': 'https://page.test/'})
        await session.perform('browser_type', {'ref': '@e1', 'text': 'raw-form-value'})
        with pytest.raises(ValueError, match='not console'):
            await session.perform('browser_console', {})
    finally:
        await session.close()
    assert requests[0].headers['authorization'] == 'Bearer private-key'
    assert json.loads(next(r for r in requests if r.url.path.endswith('/type')).content)['text'] == 'raw-form-value'
    assert requests[-1].method == 'DELETE' and requests[-1].url.path == '/tabs/one'
    assert all('/sessions/' not in r.url.path for r in requests)


async def async_identity(value):
    return value


@pytest.mark.parametrize('mode', ['plan', 'dontAsk', 'default', 'auto', 'acceptEdits'])
def test_browser_exec_does_not_gain_permission_from_tool_name(mode, isolated):
    action, _ = _permission_action(mode, (), 'browser_exec', {'code': 'print(1)'}, str(isolated), frozenset({'browser_exec', 'bash'}))
    assert action != 'allow'
    expected = 'deny' if mode == 'plan' else 'allow'
    action, _ = _permission_action(mode, (('Bash',),), 'browser_exec', {'code': 'print(1)'}, str(isolated), frozenset({'bash'}))
    assert action == expected


def test_availability_missing_dependency_is_cheap_and_honest(monkeypatch):
    monkeypatch.setattr(settings, 'executable', lambda _: '/fixture/browser')
    monkeypatch.setattr('importlib.util.find_spec', lambda _: None)
    assert not settings.available_tools()
    config.update_config({'browser.controller_command': '["controller"]', 'browser.controller_capabilities': '["browser_snapshot"]'})
    assert settings.available_tools() == {'browser_snapshot'}


def test_webpart_profile_schema_and_real_registry(monkeypatch, isolated):
    monkeypatch.setattr(settings, 'executable', lambda _: '/fixture/browser')
    config.update_config({'browser.use_real_profile': 'true', 'browser.backend': 'browser-use'})
    part = WebPart(SessionSpec(str(isolated), 'sister', str(isolated), 'bare'))
    tools = {tool.name: tool for tool in part.tools}
    assert {'browser_exec', 'browser_cdp', 'browser_dialog'} <= tools.keys()
    assert 'browser_click' in tools  # Registry fallback, filtered by the final session ceiling.
    assert 'local' in tools['browser_exec'].parameters['properties']
    assert all(not {'oneOf', 'allOf', 'anyOf'} & tool.parameters.keys() for tool in tools.values() if isinstance(tool.parameters, dict))


async def test_nous_selection_entitlement_forwarding_and_account_cache(monkeypatch):
    monkeypatch.setenv('TOOL_GATEWAY_USER_TOKEN', 'gateway-secret-token')
    config.update_config({'search_backend': 'nous'})
    def reply(r):
        if r.url.path == '/api/oauth/account':
            return httpx.Response(200, json={'paid_service_access': {'allowed': False},
                'tool_access': {'enabled': True, 'coverage': {'firecrawl': True, 'browser-use': False}}})
        assert r.url.host == 'firecrawl-gateway.nousresearch.com'
        assert r.headers['authorization'] == 'Bearer gateway-secret-token'
        return httpx.Response(200, json={'success': True, 'data': {'web': [{'title': 'fixture', 'url': 'https://page.test'}]}},
                              headers={'x-external-call-id': 'receipt-one'})
    requests, _ = fake_network(monkeypatch, reply)
    registry.ensure_backends_registered()
    provider, backend, error = dispatch.resolve_provider()
    assert backend == 'nous' and not error and not dispatch.serves_keyless(provider)
    assert not dispatch.rescue_eligible(provider)
    assert (await provider.search('first'))['success']
    assert (await provider.search('second'))['success']
    assert len(requests) == 3
    with pytest.raises(ValueError, match='entitlement'):
        await gateway.resolve('browser-use')
    assert 'gateway-secret-token' not in config.redact_secrets('gateway-secret-token')


async def test_nous_rejection_and_explicit_vendor_are_not_billed_elsewhere(monkeypatch):
    monkeypatch.setenv('TOOL_GATEWAY_USER_TOKEN', 'gateway-secret-token')
    monkeypatch.setenv('FIRECRAWL_API_KEY', 'direct-key')
    registry.ensure_backends_registered()
    assert registry.backend_name() == 'firecrawl'
    config.update_config({'search_backend': 'nous'})
    requests, _ = fake_network(monkeypatch, lambda r: httpx.Response(200, json={'paid_service_access': {'allowed': False}}))
    result = await registry.get_provider('nous').search('query')
    assert result['success'] is False and 'entitlement' in result['error']
    assert len(requests) == 1 and requests[0].url.path == '/api/oauth/account'


async def test_nous_refresh_stays_on_login_portal_and_logout_wins(monkeypatch, isolated):
    auth = gateway.storage()
    auth.set('nous', {'type': 'oauth', 'access': 'old-token', 'refresh': 'old-refresh', 'expires': 1,
                      'portal_base_url': 'https://portal.test'})
    requests, _ = fake_network(monkeypatch, lambda r: httpx.Response(200, json={
        'access_token': 'new-token', 'refresh_token': 'new-refresh', 'expires_in': 3600}))
    access, base = await gateway.token()
    assert access == 'new-token' and base == 'https://portal.test'
    assert requests[0].headers['x-nous-refresh-token'] == 'old-refresh'
    assert b'refresh_token=old-refresh' not in requests[0].content
    gateway.storage().remove('nous')
    with pytest.raises(ValueError, match='missing'):
        await gateway.token(rejected=access)
    assert len(requests) == 1


async def test_nous_device_code_real_auth_store(monkeypatch, isolated):
    responses = iter([
        {'device_code': 'device-one', 'user_code': 'ABCD', 'verification_uri': 'https://portal.nousresearch.com/device', 'expires_in': 60, 'interval': 0.01},
        {'access_token': 'new-token', 'refresh_token': 'refresh-token', 'expires_in': 3600},
    ])
    requests, _ = fake_network(monkeypatch, lambda r: httpx.Response(200, json=next(responses)))
    shown = []
    auth = gateway.storage()
    await auth.login('nous', SimpleNamespace(onDeviceCode=shown.append, signal=None))
    assert shown[0].userCode == 'ABCD'
    assert auth.get('nous')['access'] == 'new-token'
    assert [r.url.path for r in requests] == ['/api/oauth/device/code', '/api/oauth/token']
    assert b'client_id=hermes-cli' in requests[0].content


async def test_managed_browser_creation_has_stable_idempotency_and_short_lifetime(monkeypatch):
    monkeypatch.setenv('TOOL_GATEWAY_USER_TOKEN', 'gateway-secret')
    def reply(r):
        if r.url.path == '/api/oauth/account':
            return httpx.Response(200, json={'paid_service_access': {'allowed': True}})
        return httpx.Response(200, json={'id': 'one', 'cdpUrl': 'wss://browser.test/socket'}, headers={'x-external-call-id': 'receipt-one'})
    requests, _ = fake_network(monkeypatch, reply)
    lease = await CloudProvider('nous').create_session('owned')
    await lease.close()
    assert requests[1].headers['x-idempotency-key'] == 'browser-use-session-create:owned'
    assert json.loads(requests[1].content) == {'timeout': 5, 'proxyCountryCode': 'us'}
    assert requests[1].url.host == 'browser-use-gateway.nousresearch.com'
    assert lease.external_call_id == 'receipt-one'


async def test_runtime_retries_failed_browser_release_in_its_own_profile(monkeypatch, isolated):
    from misaka.core.web.scope import current_scope
    runtime = WebRuntime(current_scope())
    seen = []
    class Browser:
        async def close(self):
            seen.append(current_scope().profile_dir)
            if len(seen) == 1:
                raise RuntimeError('release rejected')
    runtime.browser = Browser()
    with WebScope(str(isolated / 'other')).activate():
        with pytest.raises(RuntimeError, match='rejected'):
            await runtime.close()
        await runtime.close()
    assert seen == [str(isolated), str(isolated)]


def test_orphan_discovery_excludes_live_and_other_profile_owners(monkeypatch, isolated):
    import os
    import shutil
    import tempfile

    import psutil

    from misaka.core.web.browser import ownership
    root = Path(tempfile.mkdtemp(prefix=ownership.prefix(), dir='/tmp'))
    try:
        (root / 'owner.json').write_text(json.dumps({'parent': ownership.parent()}))
        assert root not in [p for p, _ in ownership.stale()]
        process = psutil.Process(os.getpid())
        (root / 'owner.json').write_text(json.dumps({'parent': [process.pid, process.create_time() - 100]}))
        assert root in [p for p, _ in ownership.stale()]
        with WebScope(str(isolated / 'other')).activate():
            assert root not in [p for p, _ in ownership.stale()]
    finally:
        shutil.rmtree(root)


def test_recording_retention_excludes_active_and_unrelated_files(isolated):
    session = BrowserSession(str(isolated), {'recording_retention': 1}, 'local')
    session.recording_path = isolated / f'recording-{session.id}.partial.webm'
    (isolated / 'recording-old.webm').write_bytes(b'old')
    (isolated / 'recording-other.partial.webm').write_bytes(b'active')
    (isolated / 'user.webm').write_bytes(b'user')
    session.recording_path.write_bytes(b'done')
    session._finish_recording()
    assert not (isolated / 'recording-old.webm').exists()
    assert (isolated / 'recording-other.partial.webm').exists() and (isolated / 'user.webm').exists()


def test_cli_numeric_settings_have_real_consumers():
    config.update_config({'x_search.retries': '3', 'x_search.timeout_seconds': '210', 'browser.command_timeout': '20'})
    assert config.web_config()['x_search'] == {'retries': 3, 'timeout_seconds': 210}
    assert settings.config()['command_timeout'] == 20
    with pytest.raises(ValueError):
        config.update_config({'browser.command_timeout': 'false'})


def test_browser_plugin_metadata_drives_redaction_and_validation(monkeypatch):
    from misaka.core.web.browser.providers import BrowserProvider, validate_provider
    from misaka.core.web.scope import current_scope
    class Plugin(BrowserProvider):
        name = 'fixture-browser'
        def is_available(self): return True
        async def create_session(self, owner_id): raise AssertionError('discovery must stay local')
        def get_setup_schema(self):
            return {'env_vars': [{'key': 'FIXTURE_LOGIN'}]}
    provider = Plugin()
    validate_provider(provider)
    current_scope().browser_providers[provider.name] = provider
    monkeypatch.setenv('FIXTURE_LOGIN', 'private-plugin-credential')
    assert 'FIXTURE_LOGIN' not in config.without_credentials({'FIXTURE_LOGIN': 'private-plugin-credential'})
    assert config.redact_secrets('private-plugin-credential') == '<redacted>'
    monkeypatch.setattr(provider, 'get_setup_schema', lambda: {'env_vars': [{'key': 'not a key'}]})
    with pytest.raises(ValueError, match='ASCII'):
        validate_provider(provider)


def test_auth_rotation_changes_extract_cache_namespace(isolated):
    from misaka.core.web.cache import _url_digest
    from misaka.core.web.scope import cache_namespace
    before = cache_namespace(), _url_digest('https://page.test', 'markdown', 'nous')
    (isolated / 'auth.json').write_text(json.dumps({'nous': {'type': 'oauth', 'access': 'changed-token'}}))
    assert before[0] != cache_namespace()
    assert before[1] != _url_digest('https://page.test', 'markdown', 'nous')


async def test_browser_endpoint_rotation_closes_old_bound_owner(monkeypatch, isolated):
    import asyncio
    import time

    from misaka.core.web import browser
    instances = []
    class Session:
        def __init__(self, *args):
            self.closed = False; self.last_used = time.monotonic()
            self.lock = asyncio.Lock(); self.pending_action = None
            instances.append(self)
        async def perform(self, *_args): return {'success': True}
        async def close(self): self.closed = True
    monkeypatch.setattr(browser, 'BrowserSession', Session)
    monkeypatch.setenv('BROWSER_CDP_URL', 'wss://fixture.test/one')
    manager = BrowserManager(str(isolated))
    try:
        await manager.perform('browser_snapshot', {})
        monkeypatch.setenv('BROWSER_CDP_URL', 'wss://fixture.test/two')
        await manager.perform('browser_snapshot', {})
        assert len(instances) == 2 and instances[0].closed and not instances[1].closed
    finally:
        await manager.close()


async def test_camofox_adopted_tab_checked_before_mutation(monkeypatch, isolated):
    monkeypatch.setenv('CAMOFOX_URL', 'https://camofox.test')
    def reply(r):
        if r.url.path == '/tabs':
            return httpx.Response(200, json={'tabs': [{'listItemId': 'mine', 'tabId': 'owned'}, {'listItemId': 'other', 'tabId': 'foreign'}]})
        if r.url.path.endswith('/snapshot'):
            return httpx.Response(200, json={'url': 'http://127.0.0.1/private', 'snapshot': 'private state'})
        raise AssertionError('private tab was acted on or deleted')
    requests, _ = fake_network(monkeypatch, reply)
    session = BrowserSession(str(isolated), {'camofox_managed_persistence': True, 'camofox_adopt_existing_tab': True,
        'camofox_session_key': 'mine'}, 'camofox')
    try:
        with pytest.raises(ValueError):
            await session.perform('browser_click', {'ref': '@e1'})
        assert [r.url.path for r in requests] == ['/tabs', '/tabs/owned/snapshot']
    finally:
        await session.close()


async def test_camofox_http_200_error_is_not_success(monkeypatch, isolated):
    monkeypatch.setenv('CAMOFOX_URL', 'https://camofox.test')
    fake_network(monkeypatch, lambda r: httpx.Response(200, json={'success': False, 'error': 'missing tab'}))
    session = BrowserSession(str(isolated), {}, 'camofox')
    with pytest.raises(ValueError, match='missing tab'):
        await session.rest('GET', '/tabs')


def test_malformed_process_receipts_never_signal_any_pid(monkeypatch):
    from misaka.core.web.browser import ownership
    monkeypatch.setattr(ownership.psutil, 'Process', lambda *_: pytest.fail('unproven process identity'))
    for value in [None, [True, 1], [-1, 1], [1, float('nan')], ['1', 1], [1, True], [1]]:
        assert ownership.alive(value)
        ownership.reap_process(value)


async def test_cloud_creation_cancellation_drains_ack_and_releases_once(monkeypatch, isolated):
    import asyncio

    from misaka.core.web.browser.providers import BrowserProvider
    from misaka.core.web.scope import current_scope
    entered, release = asyncio.Event(), asyncio.Event()
    closed = []
    class Lease:
        cdp_url = 'wss://fixture.test/owned'
        async def close(self): closed.append(True)
    class Provider(BrowserProvider):
        name = 'fixture'
        def is_available(self): return True
        async def create_session(self, owner_id):
            entered.set(); await release.wait(); return Lease()
    current_scope().browser_providers['fixture'] = Provider()
    monkeypatch.setattr(settings, 'executable', lambda _: sys.executable)
    config.update_config({'browser.cloud_provider': 'fixture'})
    manager = BrowserManager(str(isolated))
    task = asyncio.create_task(manager.perform('browser_snapshot', {}))
    await entered.wait(); task.cancel(); await asyncio.sleep(.01)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await manager.close()
    assert closed == [True]


async def test_real_session_empty_ceiling_and_browser_use_readonly_fallback(monkeypatch, isolated):
    from misaka.core.auth_storage import AuthStorage
    from misaka.core.model_registry import ModelRegistry
    from misaka.core.resource_loader import DefaultResourceLoader
    from misaka.core.sdk import create_agent_session
    from misaka.core.session_manager import SessionManager
    monkeypatch.setattr(settings, 'executable', lambda _: '/fixture/browser')
    config.update_config({'browser.backend': 'browser-use'})
    owner = WebPart(SessionSpec(str(isolated), 'sister', str(isolated), 'bare'))
    auth = AuthStorage.inMemory()
    loader = DefaultResourceLoader({'cwd': str(isolated), 'agentDir': str(isolated), 'noExtensions': True,
                                    'noContextFiles': True, 'noPromptTemplates': True, 'noThemes': True})
    await loader.reload()
    session = (await create_agent_session({'cwd': str(isolated), 'agentDir': str(isolated), 'authStorage': auth,
        'modelRegistry': ModelRegistry.inMemory(auth), 'resourceLoader': loader, 'parts': [owner],
        'customTools': list(owner.tools), 'sessionManager': SessionManager.inMemory(str(isolated))}))['session']
    try:
        assert 'browser_exec' in session.getActiveToolNames() and 'browser_click' not in session.getActiveToolNames()
        session.setActiveToolsByName(['read', 'browser_exec', 'browser_snapshot', 'browser_navigate'])
        assert set(session.getActiveToolNames()) == {'read', 'browser_snapshot', 'browser_navigate'}
        session.setActiveToolsByName([])
        assert session.getActiveToolNames() == []
        session.setDisallowedToolsByName(['bash'])
        session.setActiveToolsByName(['browser_exec', 'browser_snapshot', 'bash'])
        assert session.getActiveToolNames() == ['browser_snapshot']
    finally:
        session.dispose()
        await owner.runtime.close()


async def test_auxiliary_vision_exactly_once_and_native_zero(monkeypatch):
    import base64

    from misaka.ai.models import get_model
    from misaka.core.web.browser.tools import image_content
    from misaka.utils.image_process import process_image
    body = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jE1sAAAAASUVORK5CYII=')
    assert (await process_image(body, 'image/png')).ok
    calls = []
    model = get_model('openai', 'gpt-4o')
    async def complete(*_args):
        calls.append(True); return SimpleNamespace(content=[SimpleNamespace(text='fixture image analysis')])
    async def auth(_):
        return SimpleNamespace(auth=SimpleNamespace(baseUrl=None, apiKey='fixture-key', headers={}), env={})
    import importlib
    monkeypatch.setattr(importlib.import_module('misaka.ai.stream'), 'complete_simple', complete)
    config.update_config({'browser.vision_model': 'openai/gpt-4o'})
    registry = SimpleNamespace(find=lambda *_: model, getAuth=auth)
    assert (await image_content(body, 'question', SimpleNamespace(model=model, modelRegistry=registry)))[0]['type'] == 'image'
    assert not calls
    text_model = SimpleNamespace(input=['text'])
    result = await image_content(body, 'question', SimpleNamespace(model=text_model, modelRegistry=registry))
    assert result == [{'type': 'text', 'text': 'fixture image analysis'}] and calls == [True]


def test_cdp_redaction_preserves_only_protocol_declared_binary(monkeypatch):
    from misaka.core.web.browser.tools import redact_result
    monkeypatch.setenv('XAI_API_KEY', 'fixtureSecret')
    payload = {'result': {'data': 'AAfixtureSecretBB', 'message': 'fixtureSecret'}}
    assert redact_result(payload, 'Page.captureScreenshot')['result'] == {'data': 'AAfixtureSecretBB', 'message': '<redacted>'}
    assert redact_result(payload, 'Runtime.evaluate')['result']['data'] == 'AA<redacted>BB'
    fake_binary = {'result': {'body': 'AAfixtureSecretBB', 'base64Encoded': True}}
    assert redact_result(fake_binary, 'Runtime.evaluate')['result']['body'] == 'AA<redacted>BB'
    assert redact_result(fake_binary, 'Network.getResponseBody')['result']['body'] == 'AAfixtureSecretBB'
    assert redact_result({'result': {'fixtureSecret': 'value'}}, 'Runtime.evaluate')['result'] == {'<redacted>': 'value'}


def test_real_cli_browser_setup_and_explicit_install_flags(monkeypatch, isolated):
    from misaka.cli import app, web, web_browser
    async def discover(_):
        return SimpleNamespace(extensions=[], errors=[], runtime=SimpleNamespace(invalidate=lambda: None))
    monkeypatch.setattr(web, '_discover', discover)
    monkeypatch.setattr(web_browser, 'status', lambda: None)
    monkeypatch.setattr(web_browser.shutil, 'which', lambda name: '/fixture/' + name)
    commands = []
    monkeypatch.setattr(web_browser.subprocess, 'run', lambda argv, **kw: commands.append(argv))
    app.main(['web', 'browser-setup', 'local', '--yes', '--profile', str(isolated)])
    assert json.loads((isolated / 'web.json').read_text())['browser']['cloud_provider'] == 'local'
    app.main(['web', 'browser-install', 'agent-browser', '--yes', '--profile', str(isolated)])
    assert commands[0][-1] == 'agent-browser@0.26.0'
    assert commands[1][-1] == 'install'
    assert str(isolated) in commands[1][0]


async def test_camofox_managed_state_requires_reusable_identity(monkeypatch, isolated):
    monkeypatch.setenv('CAMOFOX_URL', 'https://camofox.test')
    session = BrowserSession(str(isolated), {'camofox_managed_persistence': True}, 'camofox')
    try:
        with pytest.raises(ValueError, match='stable'):
            await session.start()
    finally:
        await session.close()


@pytest.mark.parametrize('env', [
    {'HTTP_PROXY': 'http://proxy.test:8123'},
    {'ALL_PROXY': 'http://proxy.test:8123', 'NO_PROXY': 'localhost'},
    {'HTTP_PROXY': 'http://one.test:8123', 'HTTPS_PROXY': 'http://two.test:8123'},
])
async def test_lightpanda_does_not_silently_ignore_network_routes(monkeypatch, isolated, env):
    from misaka.core.web.browser.lightpanda import launch
    for key, value in env.items(): monkeypatch.setenv(key, value)
    session = BrowserSession(str(isolated), {'lightpanda_path': '/fixture/lightpanda'}, 'local')
    with pytest.raises(ValueError, match='one all-request proxy'):
        await launch(session)

"""Hermes remainder contracts: real registrations and wire payloads, no provider credentials."""
import asyncio
import json
import threading
import time

import httpx
import pytest

from misaka.config.product import CFG
from misaka.core.auth_storage import AuthStorage
from misaka.core.tools._web import bounded
from misaka.core.web import config, registry, x_search
from misaka.core.web.backends import ddgs, xai
from misaka.core.web.backends.perplexity import (
    PerplexityWebSearchProvider,
    _query_for_urls,
)
from misaka.core.web.runtime import WebRuntime
from misaka.core.web.scope import WebScope


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setitem(CFG, 'web_config', str(tmp_path / 'shared.json'))
    for key in config.provider_variables() | {'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
                                             'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy',
                                             'SSL_CERT_FILE', 'SSL_CERT_DIR'}:
        monkeypatch.delenv(key, raising=False)
    with WebScope(str(tmp_path)).activate():
        yield tmp_path


def net(monkeypatch, handler):
    original = httpx.AsyncClient
    requests, clients = [], []
    def record(request):
        requests.append(request)
        return handler(request)
    def factory(**kwargs):
        client = original(**(kwargs | {'proxy': None, 'transport': httpx.MockTransport(record)}))
        clients.append((kwargs, client))
        return client
    monkeypatch.setattr(httpx, 'AsyncClient', factory)
    return requests, clients


def oauth(home, **accounts):
    data = {('xai' if key == 'default' else 'xai:' + key): {
        'type': 'oauth', 'access': value, 'refresh': 'refresh-' + key,
        'expires': int((time.time() + 3600) * 1000),
    } for key, value in accounts.items()}
    (home / 'auth.json').write_text(json.dumps(data))


async def test_perplexity_endpoints_order_duplicates_snippet_kind(monkeypatch):
    monkeypatch.setenv('PERPLEXITY_API_KEY', 'perplexity-fixture')
    monkeypatch.setenv('PERPLEXITY_BASE_URL', 'https://pplx.test/')
    requests, clients = net(monkeypatch, lambda r: httpx.Response(200, json={'results': (
        [{'title': 'Title', 'url': 'https://a.test/word-2026', 'snippet': 'summary'}]
        if r.url.path == '/search' else [{'url': 'https://a.test/word-2026', 'text': 'passage'}])}))
    p = PerplexityWebSearchProvider()
    runtime = WebRuntime()
    try:
        result = await runtime.run(p.search, 'query', 100)
        docs = await runtime.run(p.extract, ['https://a.test/word-2026', 'https://b.test/', 'https://a.test/word-2026'])
    finally:
        await runtime.close()
    assert json.loads(requests[0].content) == {'query': 'query', 'max_results': 20, 'search_context_size': 'low'}
    assert result['data']['web'][0]['description'] == 'summary'
    assert requests[1].url.path == '/sdk/content/snippets'
    assert json.loads(requests[1].content)['max_tokens_per_page'] == 4096
    assert docs[0] == docs[2]
    assert docs[0]['metadata']['content_kind'] == 'snippets'
    assert docs[1]['error'] == 'no content returned'
    assert len(clients) == 1 and clients[0][1].is_closed
    registry.ensure_backends_registered()
    assert registry.is_backend_available('perplexity')
    assert not p.is_keyless_available()
    assert _query_for_urls(['https://a.test/2026/World_Test-world']) == 'world test'


async def test_perplexity_per_url_errors_and_no_hidden_retry(monkeypatch):
    requests, _ = net(monkeypatch, lambda r: httpx.Response(503))
    provider = PerplexityWebSearchProvider()
    assert not (await provider.search('q'))['success']
    assert requests == []
    monkeypatch.setenv('PERPLEXITY_API_KEY', 'key')
    assert all(row['error'] for row in await provider.extract(['https://a.test', 'https://a.test']))
    assert len(requests) == 1


@pytest.mark.parametrize('args', [
    {'allowed_x_handles': ['a'], 'excluded_x_handles': ['b']},
    {'allowed_x_handles': ['a'] * 11}, {'from_date': '2026-1-01'},
    {'from_date': '2026-02-30'}, {'from_date': '2099-01-01'},
    {'from_date': '2026-02-02', 'to_date': '2026-01-01'}, {'enable_image_understanding': 'false'},
])
def test_x_parameters_reject_invalid_protocol_inputs(args):
    with pytest.raises(ValueError):
        x_search.tool_parameters(args)


async def test_x_is_separate_key_first_with_factual_citations_metadata(monkeypatch, isolated):
    oauth(isolated, default='oauth-fixture')
    monkeypatch.setenv('XAI_API_KEY', 'api-fixture')
    requests, _ = net(monkeypatch, lambda r: httpx.Response(200, json={'output_text': 'An answer', 'citations': []}))
    result = await x_search.search({'query': ' q ', 'allowed_x_handles': [' @one '],
                                  'enable_image_understanding': False, 'enable_video_understanding': True})
    body = json.loads(requests[0].content)
    assert requests[0].headers['authorization'] == 'Bearer api-fixture'
    assert body['store'] is False and body['model'] == 'grok-4.5'
    assert body['tools'] == [{'type': 'x_search', 'allowed_x_handles': ['one'], 'enable_video_understanding': True}]
    assert result['answer'] == 'An answer' and result['citations_missing'] is True
    assert 'degraded' not in result and 'degraded_reason' not in result
    assert result['active_filters'] == ['allowed_x_handles']


@pytest.mark.parametrize('status,attempts', [(400, 1), (401, 1), (429, 1), (500, 3)])
async def test_x_retry_contract(monkeypatch, status, attempts):
    monkeypatch.setenv('XAI_API_KEY', 'TOKEN')
    requests, _ = net(monkeypatch, lambda r: httpx.Response(status, text='error'))
    sleeps = []
    async def sleep(seconds):
        sleeps.append(seconds)
    monkeypatch.setattr(xai.asyncio, 'sleep', sleep)
    with pytest.raises(ValueError):
        await x_search.search({'query': 'q'})
    assert len(requests) == attempts
    assert sleeps == ([1.5, 3.0] if attempts == 3 else [])


async def test_rejected_named_account_refreshes_only_its_grant_and_rotates(monkeypatch, isolated):
    oauth(isolated, A='account-A', B='account-B')
    seen_refreshes = []
    from misaka.ai.utils.oauth import xai as oauth_module
    async def fail(refresh, signal=None, **kw):
        seen_refreshes.append(refresh)
        raise RuntimeError('invalid grant')
    monkeypatch.setattr(oauth_module, 'refresh_xai_token', fail)
    requests, clients = net(monkeypatch, lambda r: httpx.Response(
        401 if r.headers['authorization'] == 'Bearer account-A' else 200,
        json={'output': [], 'citations': ['https://x.com/a/status/1']}))
    runtime = WebRuntime()
    try:
        for query in ('first', 'second'):
            await runtime.run(x_search.search, {'query': query})
    finally:
        await runtime.close()
    assert seen_refreshes == ['refresh-A']
    assert [r.headers['authorization'] for r in requests] == ['Bearer account-A', 'Bearer account-B', 'Bearer account-B']
    assert all(c.is_closed for _, c in clients)
    assert len(clients) == 2


async def test_logout_wins_before_rejected_refresh(monkeypatch, isolated):
    oauth(isolated, A='account-A')
    token, account = await xai._resolve_credentials()
    AuthStorage.create(str(isolated / 'auth.json')).remove('xai:A')
    assert await xai._force_refresh_oauth_token(account, token) == ''
    assert json.loads((isolated / 'auth.json').read_text()) == {}


async def test_profile_proxy_rotation_uses_snapshot_and_retires_pool(monkeypatch, isolated):
    path = isolated / 'web.json'
    path.write_text(json.dumps({'env': {'HTTPS_PROXY': 'http://proxy-a.test:8123', 'PERPLEXITY_API_KEY': 'KEY'}}))
    _requests, clients = net(monkeypatch, lambda r: httpx.Response(200, json={'results': []}))
    runtime = WebRuntime()
    try:
        await runtime.run(PerplexityWebSearchProvider().search, 'a')
        path.write_text(json.dumps({'env': {'HTTPS_PROXY': 'http://proxy-b.test:8123', 'PERPLEXITY_API_KEY': 'KEY'}}))
        await runtime.run(PerplexityWebSearchProvider().search, 'b')
        assert clients[0][1].is_closed and not clients[1][1].is_closed
    finally:
        await runtime.close()
    assert [c[0]['proxy'] for c in clients] == ['http://proxy-a.test:8123', 'http://proxy-b.test:8123']
    assert all(c[0]['trust_env'] is False for c in clients)


def test_ddgs_receives_profile_network_but_no_model_tokens(monkeypatch, isolated):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'private-model-key')
    (isolated / 'web.json').write_text(json.dumps({'env': {'https_proxy': 'http://user:pass@proxy.test:8080',
                                                         'SSL_CERT_FILE': '/fixture/ca.pem', 'DDGS_PROXY': 'socks5h://proxy.test:1234'}}))
    env = ddgs._worker_env()
    assert 'ANTHROPIC_API_KEY' not in env
    assert env['HTTPS_PROXY'] == 'http://user:pass@proxy.test:8080'
    assert env['SSL_CERT_FILE'] == '/fixture/ca.pem'
    assert env['DDGS_PROXY'] == 'socks5h://proxy.test:1234'


async def test_dns_cancellation_does_not_abandon_owned_resolver(monkeypatch):
    entered, released = threading.Event(), threading.Event()
    def lookup(*args, **kwargs):
        entered.set()
        released.wait(3)
        return [(2, 1, 6, '', ('8.8.8.8', 443))]
    monkeypatch.setattr(bounded.socket, 'getaddrinfo', lookup)
    task = asyncio.create_task(bounded._resolve_host('fixture', 443))
    while not entered.is_set():
        await asyncio.sleep(.001)
    task.cancel()
    await asyncio.sleep(.01)
    assert not task.done()
    released.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize('payload,success', [({'results': None}, True), ({'success': False, 'error': 'fixture rejected'}, False), ({'error': 'fixture rejected'}, False)])
async def test_perplexity_empty_and_error_envelopes_are_distinct(monkeypatch, payload, success):
    monkeypatch.setenv('PERPLEXITY_API_KEY', 'fixture-key')
    net(monkeypatch, lambda r: httpx.Response(200, json=payload))
    result = await PerplexityWebSearchProvider().search('fixture')
    assert result['success'] is success

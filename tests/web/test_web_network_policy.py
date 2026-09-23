"""Actual URL routes, not environment-variable presence masquerading as proxy use."""

import asyncio
import json
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from webconf import write_web

from misaka.config.product import CFG
from misaka.core.web import bounded, cache, config, registry

PROXY_VARS = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY')


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in (*PROXY_VARS, *(v.lower() for v in PROXY_VARS), 'REQUEST_METHOD',
                 'MISAKA_ALLOW_PRIVATE_URLS', 'WEB_TOOLS_DEBUG', 'MISAKA_USAGE_DB', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
                 'MISAKA_USAGE_TASK_ID', 'MISAKA_USAGE_GENERATION',
                 *config._CREDENTIAL_VARS, *config._ENDPOINT_VARS):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(CFG, 'web_cache', str(tmp_path / 'cache'))
    monkeypatch.setenv('MISAKA_HOME', str(tmp_path))
    monkeypatch.chdir(tmp_path)
    write()
    cache.search_memo.clear()
    registry.reset_for_tests()
    yield
    cache.search_memo.clear()
    registry.reset_for_tests()


def write(**values):
    write_web(values)


def resolve(monkeypatch, addresses=None, error=None):
    asked = []

    async def lookup(host, port):
        asked.append((host, port))
        if error is not None:
            raise error
        return ['93.184.216.34'] if addresses is None else addresses

    monkeypatch.setattr(bounded, '_resolve_host', lookup)
    return asked


@asynccontextmanager
async def server(handler, context=None):
    tasks = set()

    async def connection(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            await handler(reader, writer)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            finally:
                tasks.discard(task)

    listener = await asyncio.start_server(connection, '127.0.0.1', 0, ssl=context)
    try:
        yield listener.sockets[0].getsockname()[1]
    finally:
        listener.close()
        await listener.wait_closed()
        pending = tuple(tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


def responder(seen, *, status=200, body=b'fixture body', headers='', mime='text/plain'):
    async def reply(reader, writer):
        seen.append((await reader.readuntil(b'\r\n\r\n')).decode())
        writer.write(f'HTTP/1.1 {status} Fixture\r\nContent-Length: {len(body)}\r\n'
                     f'Content-Type: {mime}\r\nConnection: close\r\n{headers}\r\n'.encode() + body)
        await writer.drain()
    return reply


async def test_explicit_proxy_dns_reaches_actual_proxy_when_local_dns_fails(monkeypatch):
    write(proxy_dns=True)
    asked = resolve(monkeypatch, error=socket.gaierror('fixture DNS unavailable'))
    seen = []
    async with server(responder(seen)) as port:
        monkeypatch.setenv('HTTP_PROXY', f'http://127.0.0.1:{port}')
        async with bounded.open_checked_stream('http://page.invalid/report') as response:
            assert await response.aread() == b'fixture body'
            assert str(response.url) == 'http://page.invalid/report'
    assert asked == [('page.invalid', 80)]
    assert len(seen) == 1 and seen[0].startswith('GET http://page.invalid/report HTTP/1.1')


async def test_exact_https_host_grant_reaches_private_answer(monkeypatch):
    write(trusted_private_hosts=['multimedia.nt.qq.com.cn'])
    resolve(monkeypatch, ['198.18.0.23'])
    assert await bounded.vet_public_url('https://multimedia.nt.qq.com.cn/file') == ('198.18.0.23',)


async def test_redirect_rechecks_credential_prefix_before_next_request(monkeypatch):
    resolve(monkeypatch)
    seen = []

    def reply(request):
        seen.append(request)
        return httpx.Response(302, headers={'location': 'https://other.invalid/sk-12345678901234567890123'})

    with pytest.raises(bounded.UnsafeUrlError, match='API key or token'):
        async with bounded.open_checked_stream('https://page.invalid/', transport=httpx.MockTransport(reply)):
            pytest.fail('credential redirect was accepted')
    assert len(seen) == 1


def test_new_network_route_does_not_inherit_another_routes_ban():
    from misaka.core.web import negative_cache as bans

    url = 'https://page.invalid/'
    bans.clear()
    bans.record_failure(url, 403)
    assert bans.skip_reason(url)
    write(proxy_dns=True, env={'HTTPS_PROXY': 'http://proxy.invalid:3128'})
    assert bans.skip_reason(url) is None


@pytest.mark.parametrize('env,url,selected', [
    ({'HTTP_PROXY': 'proxy.invalid:3128'}, 'http://page.invalid/', True),
    ({'HTTPS_PROXY': 'http://proxy.invalid:3128'}, 'http://page.invalid/', False),
    ({'ALL_PROXY': 'http://proxy.invalid:3128'}, 'https://page.invalid/', True),
    ({'HTTP_PROXY': 'http://proxy.invalid:3128', 'NO_PROXY': '*'}, 'http://page.invalid/', False),
    ({'HTTP_PROXY': 'http://proxy.invalid:3128', 'NO_PROXY': 'page.invalid'}, 'http://sub.page.invalid/', False),
    ({'HTTP_PROXY': 'http://proxy.invalid:3128', 'NO_PROXY': '.page.invalid'}, 'http://page.invalid/', True),
    ({'HTTP_PROXY': 'http://proxy.invalid:3128', 'NO_PROXY': '.page.invalid'}, 'http://sub.page.invalid/', False),
    ({'HTTP_PROXY': 'http://proxy.invalid:3128', 'NO_PROXY': 'page.invalid'}, 'http://notpage.invalid/', True),
    ({'HTTP_PROXY': 'http://proxy.invalid:3128', 'NO_PROXY': 'page.invalid:81'}, 'http://page.invalid:81/', False),
    ({'HTTP_PROXY': 'http://proxy.invalid:3128', 'NO_PROXY': 'page.invalid:81'}, 'http://page.invalid:82/', True),
    ({'HTTP_PROXY': 'http://proxy.invalid:3128', 'NO_PROXY': 'http://page.invalid'}, 'http://sub.page.invalid/', True),
    ({'HTTPS_PROXY': 'http://proxy.invalid:3128', 'NO_PROXY': 'http://page.invalid'}, 'https://page.invalid/', True),
    ({'HTTP_PROXY': 'http://proxy.invalid:3128', 'NO_PROXY': '::1'}, 'http://[::1]/', False),
    ({'HTTP_PROXY': 'http://proxy.invalid:3128', 'NO_PROXY': '127.0.0.1'}, 'http://127.0.0.1/', False),
    ({'HTTP_PROXY': 'http://wrong.invalid', 'http_proxy': 'http://proxy.invalid:3128'}, 'http://page.invalid/', True),
    ({'HTTP_PROXY': 'http://proxy.invalid:3128', 'http_proxy': ''}, 'http://page.invalid/', False),
    ({'HTTP_PROXY': 'http://proxy.invalid:3128', 'REQUEST_METHOD': 'GET'}, 'http://page.invalid/', False),
    ({'http_proxy': 'http://proxy.invalid:3128', 'REQUEST_METHOD': 'GET'}, 'http://page.invalid/', True),
])
def test_proxy_selection_matches_httpx_not_any_proxy_variable(env, url, selected, monkeypatch):
    from httpx._utils import URLPattern, get_environment_proxies

    from misaka.core.web.network import proxy_for_url

    write(proxy_dns=True)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    # The official installed implementation is the route oracle, not a duplicate
    # hand-built expected matcher. An explicit proxy prevents platform fallback.
    mounts = sorted((URLPattern(key), value) for key, value in get_environment_proxies().items())
    upstream = next((value for pattern, value in mounts if pattern.matches(httpx.URL(url))), None)
    actual = proxy_for_url(url)
    assert (actual is not None) is selected
    assert (httpx.URL(actual) if actual else None) == (httpx.URL(upstream) if upstream else None)


def test_profile_proxy_precedence_snapshot_and_no_material_cache_invalidation(tmp_path, monkeypatch):
    from misaka.core.web.network import policy_key, proxy_for_url
    from misaka.core.web.scope import WebScope, cache_namespace

    profile = tmp_path / 'profile'
    profile.mkdir()
    write(proxy_dns=True, env={'http_proxy': 'http://shared.invalid:3128'})
    write_web({'env': {'http_proxy': 'http://profile.invalid:3128'}}, profile=profile)
    scope = WebScope(str(profile))
    with scope.activate(snapshot=True):
        namespace, policy = cache_namespace(), policy_key()
        assert proxy_for_url('http://page.invalid/') == 'http://profile.invalid:3128'
        monkeypatch.setenv('HTTP_PROXY', 'http://process.invalid:3128')
        assert proxy_for_url('http://page.invalid/') == 'http://profile.invalid:3128'
    with scope.activate(snapshot=True):
        assert proxy_for_url('http://page.invalid/') == 'http://process.invalid:3128'
        assert policy_key() != policy and cache_namespace() == namespace
    monkeypatch.setenv('HTTP_PROXY', '')
    with scope.activate(snapshot=True):
        assert proxy_for_url('http://page.invalid/') is None
        assert cache_namespace() == namespace
    monkeypatch.delenv('HTTP_PROXY')
    write_web({'proxy_dns': False, 'trusted_private_hosts': ['page.invalid']}, profile=profile)
    with scope.activate(snapshot=True):
        assert proxy_for_url('http://page.invalid/') is None
        assert cache_namespace() == namespace and policy_key() != policy


@pytest.mark.parametrize('url', [
    'http://metadata.google.internal/', 'http://169.254.169.254/', 'http://169.254.170.2/',
    'http://100.100.100.200/', 'http://[fd00:ec2::254]/',
    'http://[::ffff:169.254.169.254]/', 'http://2852039166/', 'http://0xa9fea9fe/',
    'http://0251.0376.0251.0376/', 'http://169.254.43518/',
    'http://localhost/', 'http://127.1/', 'http://user:pass@page.invalid/',
    'file:///etc/passwd', 'https://page.invalid/sk-12345678901234567890',
])
async def test_proxy_opt_in_never_delegates_metadata_literals_or_credentials(url, monkeypatch):
    write(proxy_dns=True)
    resolve(monkeypatch, error=socket.gaierror('no DNS'))
    seen = []
    async with server(responder(seen)) as port:
        monkeypatch.setenv('ALL_PROXY', f'http://127.0.0.1:{port}')
        with pytest.raises(bounded.UnsafeUrlError):
            async with bounded.open_checked_stream(url):
                pytest.fail('unsafe URL admitted')
    assert not seen


@pytest.mark.parametrize('addresses,error', [
    ([], None), (['93.184.216.34', '10.0.0.7'], None),
    (['not-an-address'], None), (None, OSError('not a DNS error')),
    (None, UnicodeError('bad host')), (None, socket.gaierror('literal DNS error')),
])
async def test_empty_unsafe_or_non_dns_failures_do_not_become_proxy_permission(addresses, error, monkeypatch):
    write(proxy_dns=True)
    resolve(monkeypatch, addresses, error)
    url = 'http://93.184.216.34/' if isinstance(error, socket.gaierror) else 'http://page.invalid/'
    seen = []
    async with server(responder(seen)) as port:
        monkeypatch.setenv('HTTP_PROXY', f'http://127.0.0.1:{port}')
        with pytest.raises(ValueError):
            async with bounded.open_checked_stream(url):
                pytest.fail('failed DNS admitted')
    assert not seen


@pytest.mark.parametrize('url,allowed', [
    ('https://Media.Example./', True), ('https://media.example:8443/', True),
    ('http://media.example/', False), ('https://sub.media.example/', False),
    ('https://notmedia.example/', False), ('https://例子.测试/', True),
])
async def test_host_grants_are_exact_https_idna_aware_and_not_wildcards(url, allowed, monkeypatch):
    write(trusted_private_hosts=['MEDIA.EXAMPLE.', '例子.测试'])
    resolve(monkeypatch, ['198.18.0.23'])
    if allowed:
        assert await bounded.vet_public_url(url) == ('198.18.0.23',)
    else:
        with pytest.raises(bounded.UnsafeUrlError):
            await bounded.vet_public_url(url)


@pytest.mark.parametrize('address', ['169.254.169.254', '100.100.100.200', 'fd00:ec2::254', '::ffff:169.254.170.2'])
async def test_host_grants_and_global_opt_out_preserve_the_metadata_floor(address, monkeypatch):
    write(trusted_private_hosts=['page.invalid'], allow_private_urls=True, proxy_dns=True)
    resolve(monkeypatch, [address])
    with pytest.raises(bounded.UnsafeUrlError):
        await bounded.vet_public_url('https://page.invalid/', proxy='http://proxy.invalid')


@pytest.mark.parametrize('setting,value', [
    ('proxy_dns', None), ('proxy_dns', 'false'), ('proxy_dns', 1),
    ('trusted_private_hosts', None), ('trusted_private_hosts', 'page.invalid'),
    ('trusted_private_hosts', ['*.example']), ('trusted_private_hosts', ['https://page.invalid']),
    ('trusted_private_hosts', ['page.invalid:8443']), ('trusted_private_hosts', ['user@page.invalid']),
    ('trusted_private_hosts', ['127.1']), ('trusted_private_hosts', ['0xa9fea9fe']),
    ('trusted_private_hosts', ['metadata.google.internal']), ('trusted_private_hosts', ['bad host']),
])
def test_invalid_policy_is_reported_by_the_real_status_parser(setting, value):
    from misaka.core.web import network

    write(**{setting: value})
    with pytest.raises(ValueError):
        network.status()


async def test_no_proxy_uses_direct_pinned_route_and_proxy_failure_never_falls_back(monkeypatch):
    write(proxy_dns=True, allow_private_urls=True)
    resolve(monkeypatch, ['127.0.0.1'])
    direct, proxied = [], []
    async with server(responder(direct)) as target, server(responder(proxied)) as proxy:
        monkeypatch.setenv('HTTP_PROXY', f'http://127.0.0.1:{proxy}')
        monkeypatch.setenv('NO_PROXY', f'page.invalid:{target}')
        async with bounded.open_checked_stream(f'http://page.invalid:{target}/report') as response:
            assert await response.aread() == b'fixture body'
    assert not proxied and len(direct) == 1
    assert direct[0].startswith('GET /report HTTP/1.1') and f'Host: page.invalid:{target}' in direct[0]
    monkeypatch.delenv('NO_PROXY')
    # The just-closed local port fails connect. Two vetted target addresses must
    # not cause two proxy attempts or a direct fallback.
    resolve(monkeypatch, ['93.184.216.34', '23.192.228.80'])
    monkeypatch.setattr(bounded, 'pin_to_address', lambda *_: pytest.fail('proxy fell back to direct'))
    with pytest.raises(httpx.HTTPError):
        async with bounded.open_checked_stream('http://page.invalid/'):
            pytest.fail('closed proxy answered')


async def test_redirect_changes_proxy_route_and_strips_origin_credentials(monkeypatch):
    write(proxy_dns=True, allow_private_urls=True)
    resolve(monkeypatch, ['127.0.0.1'])
    direct, proxied = [], []
    async with server(responder(direct)) as target:
        headers = f'Location: http://second.invalid:{target}/final\r\n'
        async with server(responder(proxied, status=302, headers=headers)) as proxy:
            monkeypatch.setenv('HTTP_PROXY', f'http://proxy-user:proxy-password@127.0.0.1:{proxy}')
            monkeypatch.setenv('NO_PROXY', 'second.invalid')
            async with bounded.open_checked_stream('http://first.invalid/', headers={
                'Authorization': 'Bearer fixture-auth', 'Cookie': 'fixture-cookie', 'X-Trace': 'kept',
            }) as response:
                assert await response.aread() == b'fixture body'
                assert str(response.url) == f'http://second.invalid:{target}/final'
    assert len(proxied) == len(direct) == 1
    assert 'Proxy-Authorization: Basic ' in proxied[0] and 'Bearer fixture-auth' in proxied[0]
    assert 'authorization' not in direct[0].lower() and 'cookie:' not in direct[0].lower()
    assert 'X-Trace: kept' in direct[0]


@pytest.mark.parametrize('tool_name', ['web_fetch', 'download_file'])
async def test_actual_proxy_tools_join_one_debug_trace_and_one_ledger_attempt(tool_name, tmp_path, monkeypatch):
    from misaka.core.platform import budget, tasks
    from misaka.core.web import WebPart
    from misaka.core.wiring import SessionSpec

    write(proxy_dns=True, debug_enabled=True)
    resolve(monkeypatch, error=socket.gaierror('DNS unavailable'))
    body = b'fixture research body' if tool_name == 'web_fetch' else b'PK\x03\x04fixture archive'
    mime = 'text/plain' if tool_name == 'web_fetch' else 'application/zip'
    seen = []
    profile = tmp_path / 'profile'
    part = WebPart(SessionSpec(str(profile), 'sister', str(tmp_path), 'bare'))
    try:
        async with server(responder(seen, body=body, mime=mime)) as port:
            monkeypatch.setenv('HTTP_PROXY', f'http://user:fixture-proxy-password@127.0.0.1:{port}')
            tool = next(t for t in part.tools if t.name == tool_name)
            with budget.usage_context(str(tmp_path / 'ledger.db'), 'fixture', 1):
                result = await tool.execute('proxy-call', {'url': 'http://page.invalid/report.zip'}, None, None, None)
    finally:
        await part.session_shutdown({}, None)
    row, = [json.loads(p.read_text()) for p in (profile / 'logs/web').glob('web-debug-*.json')]
    con = tasks.connect(str(tmp_path / 'ledger.db'))
    try:
        ledger = [json.loads(r[0]) for r in con.execute("SELECT payload FROM events WHERE kind='external_call'")]
    finally:
        con.close()
    assert row['attempt_count'] == len(seen) == len(ledger) == 1
    assert ledger[0]['web_trace_id'] == row['trace_id'] and ledger[0]['web_attempt'] == 1
    assert any(e['kind'] == 'route' and e['transport'] == 'proxy' and e['dns_delegated'] for e in row['events'])
    assert 'fixture-proxy-password' not in json.dumps(row) and 'fixture research body' not in json.dumps(row)
    if tool_name == 'download_file':
        assert Path(result.details['path']).read_bytes() == body
    else:
        assert 'fixture research body' in result.content[0].text


@pytest.mark.parametrize('backend', ['parallel', 'firecrawl'])
async def test_real_extract_and_final_url_checks_allow_explicit_remote_dns(backend, tmp_path, monkeypatch):
    from misaka.core.web import WebPart
    from misaka.core.wiring import SessionSpec

    source, final = 'https://page.invalid/source', 'https://canonical.invalid/report'
    write(proxy_dns=True, extract_backend=backend, keyless_rescue=False,
          env={'HTTPS_PROXY': 'http://proxy.invalid:3128', f'{backend.upper()}_API_KEY': 'fixture-api-key'})
    asked = resolve(monkeypatch, error=socket.gaierror('local DNS unavailable'))
    calls = []
    real = httpx.AsyncClient

    def reply(request):
        calls.append(request)
        if backend == 'firecrawl':
            return httpx.Response(200, json={'success': True, 'data': {
                'markdown': 'fixture extracted body', 'metadata': {'sourceURL': final},
            }})
        return httpx.Response(200, json={'results': [{'url': final, 'full_content': 'fixture extracted body'}]})

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: real(**{**kw, 'proxy': None, 'transport': httpx.MockTransport(reply)}))
    part = WebPart(SessionSpec(str(tmp_path / 'profile'), 'sister', str(tmp_path), 'bare'))
    try:
        tool = next(t for t in part.tools if t.name == 'web_extract')
        result = await tool.execute('extract', {'urls': [source]}, None, None, None)
    finally:
        await part.session_shutdown({}, None)
    assert len(calls) == 1 and 'fixture extracted body' in result['content'][0]['text']
    assert asked[0][0] == 'page.invalid'
    if backend == 'firecrawl':
        assert asked[-1][0] == 'canonical.invalid'


def test_cli_persists_and_validates_real_network_settings_without_printing_proxy_auth(tmp_path, capsys):
    import base64

    from misaka.cli import app
    from misaka.core.web import network

    profile = str(tmp_path / 'profile')
    app.main(['web', 'set', 'proxy_dns', 'true', '--profile', profile])
    app.main(['web', 'set', 'trusted_private_hosts', '例子.测试,MEDIA.EXAMPLE.', '--profile', profile])
    app.main(['web', 'set', 'env.HTTPS_PROXY', 'http://proxy-user:fixture-password@proxy.invalid:3128', '--profile', profile])
    app.main(['web', 'status', '--profile', profile])
    output = capsys.readouterr().out
    assert 'URL proxy DNS: on' in output and 'media.example' in output and 'fixture-password' not in output
    original = (Path(profile) / 'settings.json').read_bytes()
    with pytest.raises(SystemExit):
        app.main(['web', 'set', 'trusted_private_hosts', '*.example', '--profile', profile])
    assert (Path(profile) / 'settings.json').read_bytes() == original
    assert not (Path(profile) / 'logs').exists()
    write(proxy_dns=True, env={'HTTPS_PROXY': 'http://proxy-user:fixture-password@proxy.invalid:3128'})
    auth = 'Basic ' + base64.b64encode(b'proxy-user:fixture-password').decode()
    redacted = config.redact_secrets(f'proxy-user:fixture-password {auth}')
    assert 'fixture-password' not in redacted and auth not in redacted
    assert network.proxy_for_url('https://page.invalid/') is not None
    app.main(['web', 'unset', 'proxy_dns', '--profile', profile])


@pytest.fixture
def tls_material(tmp_path):
    import shutil
    import ssl
    import subprocess

    executable = shutil.which('openssl')
    if executable is None:
        pytest.skip('local TLS fixture generation requires openssl')
    cert, key = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    subprocess.run([executable, 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256',
                    '-nodes', '-keyout', str(key), '-out', str(cert), '-days', '1',
                    '-subj', '/CN=page.invalid', '-addext', 'subjectAltName=DNS:page.invalid,DNS:localhost'],
                   check=True, capture_output=True)
    cert_hash = subprocess.check_output([executable, 'x509', '-in', str(cert), '-hash', '-noout'], text=True).strip()
    (tmp_path / f'{cert_hash}.0').symlink_to(cert)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    return cert, context


@pytest.mark.parametrize('matching_name', [True, False])
async def test_real_connect_tunnel_uses_configured_ca_and_verifies_logical_hostname(tls_material, matching_name, monkeypatch):
    cert, context = tls_material
    write(proxy_dns=True, env={'SSL_CERT_FILE': str(cert)})
    resolve(monkeypatch, error=socket.gaierror('DNS belongs to proxy'))
    seen, tunnels = [], []
    async with server(responder(seen), context) as target:
        async def tunnel(reader, writer):
            tunnels.append((await reader.readuntil(b'\r\n\r\n')).decode())
            upstream_reader, upstream_writer = await asyncio.open_connection('127.0.0.1', target)
            try:
                writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                await writer.drain()

                async def pump(source, destination):
                    while chunk := await source.read(16384):
                        destination.write(chunk)
                        await destination.drain()
                pumps = [asyncio.create_task(pump(reader, upstream_writer)),
                         asyncio.create_task(pump(upstream_reader, writer))]
                try:
                    await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in pumps:
                        task.cancel()
                    await asyncio.gather(*pumps, return_exceptions=True)
            finally:
                upstream_writer.close()
                await upstream_writer.wait_closed()
        async with server(tunnel) as proxy:
            monkeypatch.setenv('HTTPS_PROXY', f'http://127.0.0.1:{proxy}')
            name = 'page.invalid' if matching_name else 'wrong.invalid'
            if matching_name:
                async with bounded.open_checked_stream(f'https://{name}/report') as response:
                    assert await response.aread() == b'fixture body'
            else:
                with pytest.raises(httpx.ConnectError, match='certificate'):
                    async with bounded.open_checked_stream(f'https://{name}/report'):
                        pytest.fail('TLS hostname mismatch was accepted')
    assert len(tunnels) == 1 and tunnels[0].startswith(f'CONNECT {name}:443 HTTP/1.1')
    assert len(seen) == int(matching_name)
    if seen:
        assert 'Host: page.invalid' in seen[0]


async def test_https_proxy_itself_uses_explicit_ca(tls_material, monkeypatch):
    cert, context = tls_material
    write(proxy_dns=True, env={'SSL_CERT_FILE': str(cert)})
    resolve(monkeypatch, error=socket.gaierror('no local DNS'))
    seen = []
    async with server(responder(seen), context) as port:
        monkeypatch.setenv('HTTP_PROXY', f'https://localhost:{port}')
        async with bounded.open_checked_stream('http://page.invalid/report') as response:
            assert await response.aread() == b'fixture body'
    assert len(seen) == 1 and seen[0].startswith('GET http://page.invalid/report ')


async def test_direct_pinning_uses_explicit_ca_directory_and_retains_sni(tls_material, tmp_path, monkeypatch):
    _cert, context = tls_material
    write(trusted_private_hosts=['page.invalid'], env={'SSL_CERT_DIR': str(tmp_path)})
    resolve(monkeypatch, ['127.0.0.1'])
    seen = []
    async with (server(responder(seen), context) as port,
                bounded.open_checked_stream(f'https://page.invalid:{port}/report') as response):
        assert await response.aread() == b'fixture body'
    assert len(seen) == 1 and f'Host: page.invalid:{port}' in seen[0]


def test_ca_file_precedence_and_policy_identity_do_not_reuse_a_failed_route(tmp_path, monkeypatch):
    from misaka.core.web import network
    from misaka.core.web.scope import cache_namespace

    namespace, key = cache_namespace(), network.policy_key()
    write(env={'SSL_CERT_FILE': str(tmp_path / 'missing.pem'), 'SSL_CERT_DIR': str(tmp_path)})
    assert network.policy_key() != key and cache_namespace() == namespace
    with pytest.raises(FileNotFoundError):
        network.tls_verify()
    monkeypatch.setenv('SSL_CERT_FILE', '')
    assert network.tls_verify().check_hostname


async def test_subagent_http_hook_remains_direct_when_web_proxy_dns_is_enabled(monkeypatch):
    from misaka.core.subagent import hooks

    write(proxy_dns=True, env={'HTTP_PROXY': 'http://proxy.invalid:3128'})
    resolve(monkeypatch, error=socket.gaierror('no local DNS'))
    async def post(*_args, **_kwargs):
        pytest.fail('hook unexpectedly delegated through proxy')
    monkeypatch.setattr(hooks, '_post_http', post)
    result = await hooks._http_hook({'url': 'http://page.invalid/'}, {'hook_event_name': 'Stop'}, {})
    assert 'host resolution failed' in str(result)


async def test_concurrent_same_owner_keeps_each_call_proxy_snapshot_and_flight(tmp_path, monkeypatch):
    from misaka.core.web import WebPart
    from misaka.core.wiring import SessionSpec

    write(proxy_dns=True)
    resolve(monkeypatch, error=socket.gaierror('no local DNS'))
    started, release = asyncio.Event(), asyncio.Event()
    first_seen, second_seen = [], []
    async def first_reply(reader, writer):
        first_seen.append(await reader.readuntil(b'\r\n\r\n'))
        started.set()
        await release.wait()
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 11\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\nfirst route')
        await writer.drain()
    part = WebPart(SessionSpec(str(tmp_path / 'profile'), 'sister', str(tmp_path), 'bare'))
    first = None
    try:
        async with server(first_reply) as one, server(responder(second_seen, body=b'second route')) as two:
            monkeypatch.setenv('HTTP_PROXY', f'http://127.0.0.1:{one}')
            tool = next(t for t in part.tools if t.name == 'web_fetch')
            args = {'url': 'http://page.invalid/report'}
            first = asyncio.create_task(tool.execute('same-id', args, None, None, None))
            await asyncio.wait_for(started.wait(), 2)
            monkeypatch.setenv('HTTP_PROXY', f'http://127.0.0.1:{two}')
            second = await asyncio.wait_for(tool.execute('same-id', args, None, None, None), 2)
            assert 'second route' in second.content[0].text and not first.done()
            release.set()
            assert 'first route' in (await first).content[0].text
    finally:
        release.set()
        await part.session_shutdown({}, None)
        if first is not None:
            await asyncio.gather(first, return_exceptions=True)
    assert len(first_seen) == len(second_seen) == 1


@pytest.mark.parametrize('mode', ['timeout', 'cancel'])
@pytest.mark.parametrize('name', ['web_fetch', 'download_file'])
async def test_proxy_stream_cleanup_is_owned_through_timeout_and_repeated_cancel(mode, name, tmp_path, monkeypatch):
    from misaka.core.web import WebPart
    from misaka.core.wiring import SessionSpec

    write(proxy_dns=True, debug_enabled=True, operation_timeout={name: 0.2 if mode == 'timeout' else 10})
    resolve(monkeypatch, error=socket.gaierror('no local DNS'))
    started, disconnected = asyncio.Event(), asyncio.Event()
    async def stall(reader, writer):
        await reader.readuntil(b'\r\n\r\n')
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 1000\r\nContent-Type: application/zip\r\n\r\nPK\x03\x04'
                     if name == 'download_file' else
                     b'HTTP/1.1 200 OK\r\nContent-Length: 1000\r\nContent-Type: text/plain\r\n\r\ntext')
        await writer.drain()
        started.set()
        await reader.read()
        disconnected.set()
    profile = tmp_path / 'profile'
    part = WebPart(SessionSpec(str(profile), 'sister', str(tmp_path), 'bare'))
    try:
        async with server(stall) as port:
            monkeypatch.setenv('HTTP_PROXY', f'http://127.0.0.1:{port}')
            tool = next(t for t in part.tools if t.name == name)
            task = asyncio.create_task(tool.execute('stop', {'url': 'http://page.invalid/report.zip'}, None, None, None))
            await asyncio.wait_for(started.wait(), 2)
            if mode == 'cancel':
                task.cancel()
                asyncio.get_running_loop().call_soon(task.cancel)
            with pytest.raises(TimeoutError if mode == 'timeout' else asyncio.CancelledError):
                await task
            await asyncio.wait_for(disconnected.wait(), 2)
    finally:
        await part.session_shutdown({}, None)
    assert not list(tmp_path.rglob('*.part'))
    row, = [json.loads(p.read_text()) for p in (profile / 'logs/web').glob('web-debug-*.json')]
    assert row['attempt_count'] == 1 and row['execution_outcome'] == ('timeout' if mode == 'timeout' else 'cancelled')


@pytest.mark.parametrize('raw', [
    'ftp://user:fixture-password@proxy.invalid', 'http://user:fixture-password@proxy.invalid/private',
    'http://user:fixture-password@proxy.invalid/?token=fixture-token', 'http://metadata.google.internal',
    'http://2852039166', 'http://proxy.invalid:0',
])
def test_invalid_or_metadata_proxy_settings_are_not_logged(raw):
    from misaka.core.web import network

    write(proxy_dns=True, env={'HTTPS_PROXY': raw})
    with pytest.raises(ValueError) as caught:
        network.status()
    assert 'fixture-password' not in str(caught.value) and 'fixture-token' not in str(caught.value)


async def test_removing_private_host_grant_rechecks_cached_canonical_source_without_refetch(tmp_path, monkeypatch):
    from misaka.core.web import WebPart
    from misaka.core.wiring import SessionSpec

    settings = {'extract_backend': 'firecrawl', 'keyless_rescue': False,
                'env': {'FIRECRAWL_API_KEY': 'fixture-key'}}
    write(**settings, trusted_private_hosts=['canonical.invalid'])
    async def lookup(host, _port):
        return ['198.18.0.23'] if host == 'canonical.invalid' else ['93.184.216.34']
    monkeypatch.setattr(bounded, '_resolve_host', lookup)
    calls = []
    def reply(request):
        calls.append(request)
        return httpx.Response(200, json={'success': True, 'data': {
            'markdown': 'fixture private material', 'metadata': {'sourceURL': 'https://canonical.invalid/page'},
        }})
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: real(**{**kw, 'proxy': None, 'transport': httpx.MockTransport(reply)}))
    part = WebPart(SessionSpec(str(tmp_path / 'profile'), 'sister', str(tmp_path), 'bare'))
    try:
        tool = next(t for t in part.tools if t.name == 'web_extract')
        args = {'urls': ['https://public.invalid/page']}
        first = await tool.execute('first', args, None, None, None)
        assert 'fixture private material' in first['content'][0]['text']
        saved = {p: p.read_bytes() for p in tmp_path.rglob('*.md')}
        write(**settings)
        with pytest.raises(RuntimeError, match='private address') as caught:
            await tool.execute('second', args, None, None, None)
    finally:
        await part.session_shutdown({}, None)
    assert len(calls) == 1, 'A policy change paid for already-cached material'
    assert 'fixture private material' not in str(caught.value)
    assert 'Blocked source:' in str(caught.value)
    assert saved and all(p.read_bytes() == data for p, data in saved.items())

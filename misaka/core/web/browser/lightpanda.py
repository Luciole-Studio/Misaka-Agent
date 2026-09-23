"""Launch pinned Lightpanda directly; agent-browser 0.26 passes removed CLI flags.

Keep its CDP actions, not a wrapper translating historical executable arguments.
"""
import asyncio
import socket
from pathlib import Path

import httpx

from misaka.core.web.config import provider_env
from misaka.core.web.network import proxy_environment, proxy_for_url
from misaka.core.web.url_safety import allow_private_urls
from misaka.utils.async_lifecycle import settle


async def launch(session):
    executable = session.cfg.get('lightpanda_path')
    if not executable:
        raise ValueError('Set browser.lightpanda_path to a Lightpanda 0.4.0 executable')
    if session.cfg.get('headed') or session.cfg.get('use_real_profile'):
        raise ValueError('Lightpanda is headless and does not read Chromium profiles')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    argv = [str(Path(executable).expanduser()), 'serve', '--host', '127.0.0.1', '--port', str(port),
            '--http-connect-timeout', '8000', '--http-timeout', '30000']
    if not allow_private_urls():
        argv.append('--block-private-networks')
    proxy = proxy_environment()
    http, https = proxy['HTTP_PROXY'] or proxy['ALL_PROXY'], proxy['HTTPS_PROXY'] or proxy['ALL_PROXY']
    if (http or https) and proxy['NO_PROXY'] != '*':
        if http != https or proxy['NO_PROXY']:
            raise ValueError('Lightpanda 0.4.0 supports one all-request proxy, not per-scheme routes or NO_PROXY; select Chrome for that policy')
        argv.extend(['--http-proxy', proxy_for_url('https://example.org', api=True)])
    for env, option in [('SSL_CERT_FILE', '--ca-cert'), ('SSL_CERT_DIR', '--ca-path')]:
        if value := provider_env(env):
            argv.extend([option, value])
            break  # Same PEM-file-over-directory precedence as the API transport.
    with (session.root / 'lightpanda.log').open('wb') as output:
        session.native_process, cancelled = await settle(asyncio.create_task(asyncio.create_subprocess_exec(
            *argv, env=session.env, cwd=str(session.root), stdout=output, stderr=output, start_new_session=True)))
    from misaka.core.web.browser.ownership import receipt
    from misaka.utils.async_lifecycle import run_in_thread
    await run_in_thread(receipt, session)
    if cancelled:
        raise cancelled
    # Owner-created loopback discovery deliberately never traverses a user HTTP proxy.
    async with httpx.AsyncClient(timeout=0.5, trust_env=False) as client:
        async with asyncio.timeout(15):
            while session.native_process.returncode is None:
                try:
                    response = await client.get(f'http://127.0.0.1:{port}/json/version')
                    response.raise_for_status()
                    return response.json()['webSocketDebuggerUrl']
                except (httpx.TransportError, httpx.HTTPStatusError):
                    await asyncio.sleep(0.05)
    raise RuntimeError('Lightpanda exited before CDP was ready; see its private owner log')

"""Session-owned browser adapters. CLI, REST and CDP have one lifecycle owner."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import httpx

from misaka.core.tools._web.bounded import vet_public_url
from misaka.core.tools._web.screening import screen_url
from misaka.core.web.accounting import account_call
from misaka.core.web.browser import settings
from misaka.core.web.browser.cdp import Supervisor
from misaka.core.web.browser.process import command
from misaka.core.web.browser.providers import providers
from misaka.core.web.config import provider_env
from misaka.core.web.network import proxy_for_url
from misaka.core.web.runtime import api_client
from misaka.core.web.scope import current_scope
from misaka.utils.async_lifecycle import run_in_thread, settle


async def check_url(url):
    screened = screen_url(url)
    if screened.refusal:
        raise ValueError(screened.refusal)
    await vet_public_url(screened.url, proxy=proxy_for_url(screened.url))
    return screened.url


def _scratch():
    from misaka.core.web.browser.ownership import prefix
    root = Path(tempfile.mkdtemp(prefix=prefix(), dir='/tmp' if os.name == 'posix' else None))
    (root / 'config.json').write_text('{}')
    return root


def _copy_profile(source, destination):
    source = Path(source).expanduser().resolve(strict=True)
    if not source.is_dir():
        raise ValueError('browser.real_profile_path must name an explicit Chromium user-data directory')
    # Copy only on explicit opt-in. Never launch with or modify the source directory.
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(
        'Singleton*', 'DevToolsActivePort', 'LOCK', 'lockfile', 'Cache', 'Code Cache', 'GPUCache', 'Crashpad'))


class BrowserSession:
    def __init__(self, cwd, cfg, kind, *, force_chrome=False):
        self.cwd, self.cfg, self.kind = cwd, dict(cfg), kind
        self.id = uuid.uuid4().hex
        self.root = None
        self.env = None
        self.prefix = None
        self.started = False
        self.closed = False
        self.supervisor = None
        self.lease = None
        self.external_tab = False
        self.pending_action = None
        self.cdp_url = None
        self.url = ''
        self.force_chrome = force_chrome
        self.lock = asyncio.Lock()
        self.last_used = time.monotonic()
        self.camofox_tab = None
        self.camofox_managed = bool(self.cfg.get('camofox_managed_persistence'))
        self.camofox_session_key = provider_env('CAMOFOX_SESSION_KEY') or self.cfg.get('camofox_session_key') or self.id
        self.camofox_user = None
        self.camofox_base = provider_env('CAMOFOX_URL').rstrip('/')
        self.camofox_key = provider_env('CAMOFOX_API_KEY')
        self.recording = False
        self.recording_path = None
        self.daemon_identity = None
        self.harness_identity = None
        self.native_process = None
        self.exec_env = None
        self.close_task = None
        self.closing = False
        self.creation_error = None
        self.start_task = None

    async def start(self):
        if self.start_task is None:
            self.start_task = asyncio.create_task(self._start())
        await asyncio.shield(self.start_task)

    async def _start(self):
        if self.creation_error:
            raise RuntimeError(self.creation_error)
        if self.started:
            return
        if self.closed or self.closing:
            raise RuntimeError('This browser session is closed')
        from misaka.core.web.browser import ownership
        await ownership.reap()
        self.root = _scratch()
        await run_in_thread(ownership.receipt, self)
        if self.kind == 'camofox':
            if self.camofox_managed and self.camofox_session_key == self.id:
                raise ValueError('Managed Camofox persistence requires an explicit stable camofox_session_key')
            self.camofox_user = provider_env('CAMOFOX_USER_ID') or (
                'misaka-' + hashlib.sha256(str(current_scope().profile_dir).encode()).hexdigest()[:20]
                if self.cfg.get('camofox_managed_persistence') else 'misaka-' + self.id)
            if self.cfg.get('camofox_adopt_existing_tab'):
                if self.camofox_session_key == self.id or not self.camofox_managed:
                    raise ValueError('Camofox adoption requires managed persistence and an explicit camofox_session_key')
                tabs = (await self.rest('GET', '/tabs')).json().get('tabs', [])
                matches = [tab for tab in tabs if tab.get('listItemId') == self.camofox_session_key]
                if matches:
                    self.camofox_tab = quote(str(matches[-1]['tabId']), safe='')
            self.started = True
            return
        self.env = settings.subprocess_env()
        self.env.update(AGENT_BROWSER_SOCKET_DIR=str(self.root), AGENT_BROWSER_IDLE_TIMEOUT_MS='300000',
                        AGENT_BROWSER_DEFAULT_TIMEOUT=str(int(float(self.cfg.get('command_timeout', 30)) * 1000)),
                        AGENT_BROWSER_NO_AUTO_DIALOG='1')
        executable = settings.executable('agent-browser')
        if not executable:
            raise ValueError('agent-browser is not installed; run misaka web browser-install')
        self.prefix = [executable, '--config', str(self.root / 'config.json'), '--session', self.id, '--json']
        engine = 'chrome' if self.force_chrome else self.cfg.get('engine', 'auto')
        if engine not in {'auto', 'chrome', 'lightpanda'}:
            raise ValueError('browser.engine takes auto, chrome or lightpanda')
        cdp = provider_env('BROWSER_CDP_URL') or self.cfg.get('cdp_url') if self.kind == 'cdp' else None
        if self.kind == 'cdp' and not cdp:
            raise ValueError('Set an explicit CDP endpoint with misaka web browser-connect')
        if self.kind not in {'cdp', 'local'}:
            try:
                # A timeout/malformed receipt may represent a real paid creation.
                # Latch the uncertain outcome: no automatic second creation or local fallback.
                self.lease, cancelled = await settle(asyncio.create_task(providers()[self.kind].create_session(self.id)))
                await run_in_thread(ownership.receipt, self)
                if cancelled:
                    raise cancelled
                cdp = self.lease.cdp_url
                if not cdp:
                    raise ValueError('Cloud session returned no CDP URL')
            except BaseException:
                self.creation_error = 'Cloud creation/attachment did not complete; no second session was created automatically'
                raise
        if self.kind == 'local' and engine == 'lightpanda':
            from misaka.core.web.browser.lightpanda import launch
            cdp = await launch(self)
        if cdp:
            self.cdp_url = str(cdp)
            from misaka.core.web.config import remember_secret
            remember_secret(self.cdp_url)
            self.supervisor = Supervisor(check_url=check_url)
            target = await self.supervisor.connect(self.cdp_url, own_tab=True, direct=self.kind == 'local')
            self.external_tab = self.kind != 'local'
            marker = 'about:blank#misaka-' + self.id
            await self.supervisor.command('Page.navigate', {'url': marker}, target_id=target)
            if self.native_process:
                self.cdp_url = await self.supervisor.share()
            targets = (await self.supervisor.call('Target.getTargets'))['targetInfos']
            marker = next(row['url'] for row in targets if row['targetId'] == target)
            self.prefix.extend(['--cdp', self.cdp_url])
            self.started = True
            tabs = await self.raw('tab')
            selected = next((row['tabId'] for row in tabs.get('tabs', []) if row.get('url') == marker), None)
            if not selected:
                raise RuntimeError('Owned CDP tab was not discovered; no existing user tab was selected')
            await self.raw('tab', selected)
        else:
            if engine != 'auto':
                self.prefix.extend(['--engine', engine])
            if self.cfg.get('headed', False):
                self.prefix.append('--headed')
            executable_path = self.cfg.get('lightpanda_path') if engine == 'lightpanda' else self.cfg.get('executable_path')
            if executable_path:
                self.env['AGENT_BROWSER_EXECUTABLE_PATH'] = str(Path(executable_path).expanduser())
            if self.cfg.get('use_real_profile', False):
                if not self.cfg.get('real_profile_path'):
                    raise ValueError('Set browser.real_profile_path before enabling real-profile browsing')
                await run_in_thread(_copy_profile, self.cfg['real_profile_path'], self.root / 'profile')
                self.prefix.extend(['--profile', str(self.root / 'profile')])
            self.started = True  # Partial CLI startup is owned too.
            result = await self.raw('get', 'cdp-url', timeout=max(60, float(self.cfg.get('open_timeout', 60))))
            self.cdp_url = result.get('cdpUrl')
            if not self.cdp_url:
                raise RuntimeError('agent-browser returned no CDP endpoint')
            self.supervisor = Supervisor(check_url=check_url)
            await self.supervisor.connect(self.cdp_url, own_tab=True, direct=True)
            if engine != 'lightpanda':
                marker = 'about:blank#misaka-' + self.id
                await self.supervisor.command('Page.navigate', {'url': marker})
                tabs = await self.raw('tab')
                selected = next((row['tabId'] for row in tabs.get('tabs', []) if row.get('url') == marker), None)
                if not selected:
                    raise RuntimeError('Owned local tab was not discovered')
                await self.raw('tab', selected)
        await run_in_thread(self._capture_daemon)
        await run_in_thread(ownership.receipt, self)
        if self.cfg.get('record_sessions'):
            directory = Path(self.cwd) / 'downloads' / 'browser'
            if not directory.resolve().is_relative_to(Path(self.cwd).resolve()):
                raise ValueError('Browser recording directory points outside the workspace')
            await run_in_thread(directory.mkdir, parents=True, exist_ok=True)
            path = directory / f'recording-{self.id}.partial.webm'
            self.recording_path = path
            await self.raw('record', 'start', str(path))
            self.recording = True

    def _capture_daemon(self):
        import psutil
        path = self.root / (self.id + '.pid')
        try:
            pid = int(path.read_text().strip())
            process = psutil.Process(pid)
            self.daemon_identity = (pid, process.create_time())
        except (OSError, ValueError, psutil.Error):
            pass

    def _reap_daemon(self):
        from misaka.core.web.browser.ownership import reap_process
        if not self.daemon_identity:
            self._capture_daemon()
        reap_process(self.daemon_identity)

    async def raw(self, *args, timeout=None):
        seconds = float(self.cfg.get('command_timeout', 30)) if timeout is None else timeout
        rc, out, err = await command([*self.prefix, *args], self.env, cwd=str(self.root), timeout=seconds)
        try:
            result = json.loads(out)
        except ValueError:
            raise RuntimeError(f'agent-browser exited {rc} without a JSON result: {err[:1000]}') from None
        if not result.get('success'):
            raise RuntimeError(str(result.get('error') or err or 'Browser command failed'))
        return result.get('data') or {}

    async def rest(self, method, suffix, body=None):
        endpoint = self.camofox_base + suffix
        headers = {'Authorization': 'Bearer ' + self.camofox_key} if self.camofox_key else {}
        async with api_client('camofox', endpoint, self.camofox_key, timeout=float(self.cfg.get('command_timeout', 30))) as client, account_call('browser', 'camofox', suffix):
            response = await client.request(method, endpoint, headers=headers,
                                            **({'json': {'userId': self.camofox_user, **body}} if body is not None
                                               else {'params': {'userId': self.camofox_user}}))
            response.raise_for_status()
            if response.headers.get("content-type", "").startswith("application/json"):
                data = response.json()
                if not isinstance(data, dict) or data.get("error") or data.get("success") is False:
                    raise ValueError("Camofox returned an invalid or rejected action: " + str(data))
            return response

    async def _check_camofox_url(self, url):
        if self.cfg.get('camofox_rewrite_loopback_urls') and urlsplit(url).hostname == 'host.docker.internal':
            # This alias resolves inside Docker, not on the client. The original
            # loopback URL already passed the explicit private-URL policy.
            return await check_url(url.replace('host.docker.internal', '127.0.0.1', 1))
        return await check_url(url)

    async def _camofox(self, name, args):
        if name == 'browser_navigate':
            original = args['url']
            if self.cfg.get('camofox_rewrite_loopback_urls'):
                parts = urlsplit(original)
                if parts.hostname in {'localhost', '127.0.0.1', '::1'}:
                    alias = self.cfg.get('camofox_loopback_host_alias', 'host.docker.internal')
                    if not re.fullmatch(r'[A-Za-z0-9.-]+', alias):
                        raise ValueError('Camofox loopback alias must be a hostname without port or credentials')
                    rewritten = urlunsplit((parts.scheme, alias + (f':{parts.port}' if parts.port else ''), parts.path, parts.query, parts.fragment))
                    if alias != 'host.docker.internal':
                        await check_url(rewritten)
                    args = args | {'url': rewritten}
            if self.camofox_tab:
                try:
                    response = await self.rest('POST', f'/tabs/{self.camofox_tab}/navigate', {'url': args['url']})
                except httpx.HTTPStatusError as error:
                    if error.response.status_code != 404:
                        raise
                    self.camofox_tab = None
                    return await self._camofox(name, args)  # Explicitly missing tab; no uncertain action replay.
                data = response.json()
            else:
                response = await self.rest('POST', '/tabs', {'url': args['url'], 'listItemId': self.camofox_session_key})
                data = response.json()
                self.camofox_tab = quote(str(data['tabId']), safe='')
                if not self.camofox_managed:
                    from misaka.core.web.browser.ownership import receipt
                    from misaka.core.web.browser.providers import CloudLease
                    endpoint = self.camofox_base + '/tabs/' + self.camofox_tab + '?' + urlencode({'userId': self.camofox_user})
                    headers = {'Authorization': 'Bearer ' + self.camofox_key} if self.camofox_key else {}
                    self.lease = CloudLease(self.camofox_tab, '', {}, None, endpoint, headers, 'DELETE', None, 'camofox')
                    await run_in_thread(receipt, self)
            self.url = data.get('url') or args['url']
            await self._check_camofox_url(self.url)
            return {'success': True, 'url': self.url, 'requested_url': original,
                    'loopback_rewritten': original != args['url'], **await self._camofox('browser_snapshot', {})}
        if not self.camofox_tab:
            raise ValueError('Call browser_navigate first')
        base = f'/tabs/{self.camofox_tab}'
        if name == 'browser_snapshot':
            data = (await self.rest('GET', base + '/snapshot')).json()
            if data.get('url'):
                await self._check_camofox_url(data['url'])
                self.url = data['url']
            return {'snapshot': data.get('snapshot', ''), 'element_count': data.get('refsCount', 0)}
        # Adopted or user-navigated tabs may have changed origin since the last call.
        # Snapshot verifies the current page before any read or mutation.
        data = await self._camofox('browser_snapshot', {})
        if name == 'browser_get_images':
            rows = re.findall(r'img\s+"([^"]*)"[^\n]*\n\s*/url:\s*(\S+)', data['snapshot'])
            return {'images': [{'src': src, 'alt': alt} for alt, src in rows]}
        if name == 'browser_vision':
            if args.get('annotate'):
                raise ValueError('Camofox screenshots have no coordinate/ref annotation support')
            return {'image_bytes': (await self.rest('GET', base + '/screenshot')).content}
        if name == 'browser_console':
            if 'expression' not in args:
                raise ValueError('Camofox REST exposes evaluation, not console/error buffers')
            return (await self.rest('POST', base + '/evaluate', {'expression': args['expression']})).json()
        mapping = {'browser_click': ('click', {'ref': str(args.get('ref', '')).lstrip('@')}),
                   'browser_type': ('type', {'ref': str(args.get('ref', '')).lstrip('@'), 'text': args.get('text', '')}),
                   'browser_scroll': ('scroll', {'direction': args.get('direction')}),
                   'browser_back': ('back', {}), 'browser_press': ('press', {'key': args.get('key')})}
        if name not in mapping:
            raise ValueError('This Camofox REST backend has no CDP or Python execution capability')
        action, payload = mapping[name]
        data = (await self.rest('POST', base + '/' + action, payload)).json()
        if data.get('url'):
            await self._check_camofox_url(data['url'])
            self.url = data['url']
        return {'success': True, 'url': self.url}

    async def perform(self, name, args):
        if name == 'browser_navigate':
            args = {**args, 'url': await check_url(args['url'])}
        if self.closed or self.closing:
            raise RuntimeError('This browser session is closed')
        await self.start()
        self.last_used = time.monotonic()
        if self.lease and self.lease.expires_at:
            expires = datetime.fromisoformat(self.lease.expires_at)
            if expires.tzinfo is None:
                raise ValueError('Cloud lifetime receipt requires a timezone')
            if expires <= datetime.now(UTC):
                raise RuntimeError('Cloud browser lifetime expired; no action was replayed')
        if name == 'browser_dialog':
            result = await self.supervisor.dialog(**{key: value for key, value in args.items() if key != 'session'})
            if self.pending_action is not None:
                completed = await self._wait_action(self.pending_action)
                if completed.get('action_pending'):
                    result.update(completed)
                else:
                    self.pending_action = None
                    if 'image_bytes' in completed:
                        result['image_bytes'] = completed.pop('image_bytes')
                    result['completed_action'] = completed
            return result
        if name == 'browser_snapshot' and self.supervisor and self.supervisor.dialogs:
            return {'success': True, 'snapshot': '', **self.supervisor.state()}
        async with self.lock:
            if self.pending_action is not None:
                if not self.pending_action.done():
                    raise RuntimeError('An earlier action is waiting for its dialog; handle it before another action')
                try:
                    await self.pending_action
                finally:
                    self.pending_action = None
            if self.kind == 'camofox':
                return await self._camofox(name, args)
            return await self._wait_action(asyncio.create_task(self._action(name, args)))

    async def _action(self, name, args):
        if name == '_vault_focus':
            result = await self.supervisor.focus_page(args['origin'], accept=args.get('accept'),
                                                       owned_browser=not self.external_tab)
            if result.get('ok'):
                self.url = await check_url(result['url'])
            return result
        if name == 'browser_cdp':
            method, params = args['method'], args.get('params') or {}
            if not isinstance(params, dict):
                raise ValueError('CDP params must be an object')
            # Browser-wide mutations on a shared user browser are not tab-owned.
            if self.external_tab and method.split('.')[0] in {'Browser', 'Target', 'Storage'} and method not in {
                'Browser.getVersion', 'Target.getTargetInfo'}:
                raise ValueError('This method affects a shared browser, not this session-owned tab')
            if method == 'Page.navigate':
                await check_url(params.get('url', ''))
            result = await self.supervisor.command(method, params, target_id=args.get('target_id'),
                frame_id=args.get('frame_id'), timeout=max(1, min(300, float(args.get('timeout', 30)))))
            return {'success': True, 'method': method, 'result': result}
        if name == 'browser_exec':
            return await self.exec(args)
        return await self._agent_action(name, args)

    async def _wait_action(self, action):
        waiter = asyncio.create_task(self.supervisor.dialog_ready.wait())
        try:
            done, _ = await asyncio.wait([action, waiter], return_when=asyncio.FIRST_COMPLETED)
            if action in done:
                return await action
            self.pending_action = action
            return {'success': True, 'action_pending': True, **self.supervisor.state()}
        except BaseException:
            action.cancel()
            await settle(action)
            raise
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    async def _agent_action(self, name, args):
        if name == 'browser_navigate':
            result = await self.raw('open', args['url'], timeout=float(self.cfg.get('open_timeout', 60)))
            self.url = result.get('url') or args['url']
            await check_url(self.url)
            snapshot = await self.raw('snapshot', '-i', '-c')
            return {'success': True, **result, **snapshot, **self.supervisor.state()}
        if not self.url:
            raise ValueError('Call browser_navigate first')
        current = await self.raw('get', 'url')
        self.url = current.get('url') or self.url
        await check_url(self.url)
        if name == 'browser_snapshot':
            result = await self.raw('snapshot', *([] if args.get('full', False) else ['-i', '-c']))
            return {'success': True, **result, **self.supervisor.state()}
        if name == 'browser_console':
            if 'expression' in args:
                result = await self.raw('eval', args['expression'])
            else:
                flag = ['--clear'] if args.get('clear', False) else []
                result = {'console': await self.raw('console', *flag), 'errors': await self.raw('errors', *flag)}
        elif name == 'browser_get_images':
            result = await self.raw('eval', "[...document.images].map(i=>({src:i.src,alt:i.alt,width:i.naturalWidth,height:i.naturalHeight})).filter(i=>!i.src.startsWith('data:'))")
        elif name == 'browser_vision':
            path = self.root / ('screenshot-' + uuid.uuid4().hex + '.png')
            result = await self.raw('screenshot', '--full', *(['--annotate'] if args.get('annotate') else []), str(path))
            return {'success': True, **result, 'image_bytes': await run_in_thread(path.read_bytes)}
        else:
            argv = {'browser_click': ['click', args.get('ref', '')],
                    'browser_type': ['fill', args.get('ref', ''), args.get('text', '')],
                    'browser_scroll': ['scroll', args.get('direction', '')],
                    'browser_back': ['back'], 'browser_press': ['press', args.get('key', '')]}.get(name)
            if argv is None:
                raise ValueError('Unknown browser action')
            result = await self.raw(*argv)
            if name == 'browser_type':
                result = {'typed': True, 'ref': args['ref']}  # Never echo form values.
        current = await self.raw('get', 'url')
        self.url = current.get('url') or self.url
        await check_url(self.url)
        return {'success': True, **result, 'url': self.url}

    async def exec(self, args):
        executable = settings.executable('browser-use')
        if not executable:
            raise ValueError('browser-use CLI is not installed; install it explicitly before selecting this backend')
        code = args.get('code')
        if not isinstance(code, str) or not code.strip():
            raise ValueError('browser_exec requires Python code')
        if args.get('local') and not self.cfg.get('use_real_profile'):
            raise ValueError('local=true requires explicit browser.use_real_profile and real_profile_path')
        workspace = Path(self.cwd) / 'downloads' / 'browser' / self.id
        if not workspace.resolve().is_relative_to(Path(self.cwd).resolve()):
            raise ValueError('Browser workspace points outside the project')
        await run_in_thread(workspace.mkdir, parents=True, exist_ok=True, mode=0o700)
        env = {**self.env, 'BU_NAME': self.id, 'BU_CDP_WS': self.cdp_url, 'BU_AUTOSPAWN': '0',
               'BH_AGENT_WORKSPACE': str(workspace), 'BH_RUNTIME_DIR': str(self.root),
               'BH_TMP_DIR': str(workspace), 'BH_CONFIG_DIR': str(self.root / 'harness-config'),
               'BH_TELEMETRY': 'off', 'BH_UPDATE_CHECK': 'off', 'BH_RECORD': '0',
               'BROWSER_USE_SETUP_LOGGING': 'false'}
        # Pin the actual already-owned tab, not an existing user's tab selected by a daemon.
        code = f"switch_tab({self.supervisor.root!r})\n" + code
        self.exec_env = env
        try:
            rc, out, err = await command([executable], env, cwd=str(workspace), timeout=max(5, min(1800, int(args.get('timeout_s', 300)))),
                                         input_bytes=code.encode())
        finally:
            await run_in_thread(self._capture_harness)
            from misaka.core.web.browser.ownership import receipt
            await run_in_thread(receipt, self)
        result = {'success': rc == 0, 'exit_code': rc, 'output': out, 'stderr': err,
                  'workspace': str(workspace), 'session': args.get('session', '')}
        # The CLI's printed screenshot path must belong to this owner workspace.
        for raw in reversed(re.findall(r'(/[^\s\"\']+\.(?:png|jpe?g|webp))', out)):
            path = Path(raw).resolve()
            if path.is_relative_to(workspace.resolve()) and path.is_file():
                result['image_bytes'] = await run_in_thread(path.read_bytes)
                break
        return result

    def _finish_recording(self):
        if self.recording_path and self.recording_path.is_file():
            final = self.recording_path.with_name(f'recording-{self.id}.webm')
            self.recording_path.replace(final)
            files = sorted((path for path in final.parent.glob('recording-*.webm')
                            if not path.name.endswith('.partial.webm') and not path.is_symlink()),
                           key=lambda path: path.stat().st_mtime, reverse=True)
            keep = max(1, min(100, int(self.cfg.get('recording_retention', 10))))
            for path in files[keep:]:
                path.unlink(missing_ok=True)

    def _capture_harness(self):
        import psutil
        try:
            record = json.loads((self.root / 'bu.pid').read_text())
            pid = record['pid'] if isinstance(record, dict) else record
            proc = psutil.Process(pid)
            self.harness_identity = (pid, proc.create_time())
        except (OSError, ValueError, psutil.Error):
            pass

    async def close(self):
        if self.closed:
            return
        self.closing = True
        if self.close_task is None or (self.close_task.done() and self.close_task.exception()):
            self.close_task = asyncio.create_task(self._close())
        _, cancelled = await settle(self.close_task)
        if cancelled is not None:
            raise cancelled

    async def _close(self):
        errors = []
        if self.start_task and not self.start_task.done():
            self.start_task.cancel()
            await asyncio.gather(self.start_task, return_exceptions=True)
        if self.pending_action:
            self.pending_action.cancel()
            await asyncio.gather(self.pending_action, return_exceptions=True)
            self.pending_action = None
        if self.exec_env:
            try:
                rc, _, _ = await command([settings.executable('browser-use'), '--reload'], self.exec_env,
                                        cwd=str(self.root), timeout=30)
                if rc:
                    raise RuntimeError('Browser Use daemon shutdown failed')
                self.exec_env = None
            except Exception as error:  # noqa: BLE001 - finish independent cleanup and report failures
                errors.append(type(error).__name__)
        # Independent fallback: a failed shutdown CLI must not skip owned cleanup.
        if self.harness_identity:
            try:
                from misaka.core.web.browser.ownership import reap_process
                await run_in_thread(reap_process, self.harness_identity)
            except Exception as error:  # noqa: BLE001 - finish independent cleanup and report failures
                errors.append(type(error).__name__)
        if self.prefix and self.started and self.recording:
            try:
                await self.raw('record', 'stop', timeout=10)
                await run_in_thread(self._finish_recording)
                self.recording = False
            except Exception as error:  # noqa: BLE001 - finish independent cleanup and report failures
                errors.append(type(error).__name__)
        try:
            if self.supervisor:
                await self.supervisor.close(close_tab=self.external_tab)
                self.supervisor = None
        except Exception as error:  # noqa: BLE001 - finish independent cleanup and report failures
            errors.append(type(error).__name__)
        if self.prefix and self.started:
            try:
                await self.raw('close', timeout=10)
            except Exception as error:  # noqa: BLE001 - finish independent cleanup and report failures
                errors.append(type(error).__name__)
            finally:
                await run_in_thread(self._reap_daemon)
                self.started = False
        if self.native_process:
            from misaka.core.web.browser.process import terminate
            await terminate(self.native_process)
        if self.lease:
            try:
                await self.lease.close()
            except Exception as error:  # noqa: BLE001 - finish independent cleanup and report failures
                errors.append(type(error).__name__)
        if errors:
            raise RuntimeError('Browser cleanup errors: ' + ', '.join(errors))
        if self.root:
            await run_in_thread(shutil.rmtree, self.root, True)
        self.closed = True

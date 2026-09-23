"""Browser resources belong to WebRuntime, not a process-global session registry."""
import asyncio
import ipaddress
import logging
import re
import time

from misaka.core.web.bounded import vet_public_url
from misaka.core.web.browser import settings
from misaka.core.web.browser.session import BrowserSession, check_url
from misaka.core.web.network import proxy_for_url
from misaka.utils.async_lifecycle import settle


class BrowserManager:
    def __init__(self, cwd):
        self.cwd = cwd
        self.sessions = {}
        self.selection = None
        self.lock = asyncio.Lock()
        self.closed = False
        self.active = 0
        self.sidecars = set()
        self.idle_task = None

    async def perform(self, name, args, call_id=''):
        cfg = settings.config()
        if cfg.get('enabled', True) is False:
            raise ValueError('Browser tools are disabled by browser.enabled')
        kind = settings.route()
        session_name = args.get('session', '') if name in {'browser_exec', 'browser_cdp', 'browser_dialog', '_vault_focus'} else ''
        if not isinstance(session_name, str) or (session_name and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', session_name)):
            raise ValueError('Browser session names take up to 64 letters, digits, underscores or hyphens')
        if args.get('local'):
            if not cfg.get('use_real_profile'):
                raise ValueError('local=true requires explicit real-profile opt-in')
            kind = 'local'
        primary_kind = kind
        selection = (kind, settings.identity(cfg))
        async with self.lock:
            if self.closed:
                raise RuntimeError('This browser owner is closed')
            if self.selection != selection:
                if self.active:
                    raise RuntimeError('Browser configuration changed while actions are running; retry after they settle')
                await self._close_sessions()
                self.selection = selection
                self.sidecars.clear()
            if name == 'browser_navigate' and kind not in {'local', 'cdp', 'controller', 'camofox'}:
                url = await check_url(args['url'])
                addresses = await vet_public_url(url, proxy=proxy_for_url(url))
                if addresses and any(not ipaddress.ip_address(address).is_global for address in addresses):
                    self.sidecars.add(session_name)
                else:
                    self.sidecars.discard(session_name)
            if session_name in self.sidecars:
                kind = 'local'
                cfg = cfg | {'use_real_profile': False}
            session_key = (session_name, kind)
            for key, old in tuple(self.sessions.items()):
                if old.closed or (time.monotonic() - old.last_used > 300 and old.pending_action is None and not old.lock.locked()):
                    await old.close()
                    del self.sessions[key]
            if session_key not in self.sessions:
                if kind == 'controller':
                    from misaka.core.web.browser.controller import Controller
                    self.sessions[session_key] = Controller(self.cwd, cfg)
                else:
                    self.sessions[session_key] = BrowserSession(self.cwd, cfg, kind)
            session = self.sessions[session_key]
            self.active += 1
            if self.idle_task is None:
                self.idle_task = asyncio.create_task(self._idle())
        try:
            if kind == 'controller':
                return await session.perform(name, args, call_id)
            if name == 'browser_vision' and cfg.get('engine') == 'lightpanda':
                # A fresh Chrome page is a different state. Never replay click/type/press.
                if not session.url:
                    raise ValueError('Call browser_navigate first')
                temporary = BrowserSession(self.cwd, cfg | {'use_real_profile': False, 'record_sessions': False},
                                           'local', force_chrome=True)
                try:
                    await temporary.perform('browser_navigate', {'url': session.url})
                    result = await temporary.perform(name, args)
                    result.update(fallback_from='lightpanda', backend='chrome', state_preserved=False,
                                  warning='Screenshot uses a new Chrome page; Lightpanda DOM/cookies are not transferred')
                    return result
                finally:
                    _, cancelled = await settle(asyncio.create_task(temporary.close()))
                    if cancelled:
                        raise cancelled
            result = await session.perform(name, args)
            if kind != primary_kind:
                result.update(backend='local', routed_from=primary_kind, state_preserved=False,
                              warning='Private destination uses a separate session-owned local browser; cloud state is not copied')
            return result
        except (asyncio.CancelledError, TimeoutError):
            # A killed CLI waiter does not stop a command inside its daemon.
            # Stop the owned browser/tab before reporting cancellation.
            _, cancelled = await settle(asyncio.create_task(session.close()))
            if cancelled:
                raise cancelled
            raise
        finally:
            self.active -= 1

    async def _close_sessions(self):
        sessions = tuple(self.sessions.values())
        results = await asyncio.gather(*(session.close() for session in sessions), return_exceptions=True)
        errors = [type(result).__name__ for result in results if isinstance(result, BaseException)]
        self.sessions = {key: session for key, session in self.sessions.items() if not session.closed}
        if errors:
            raise RuntimeError('Browser owner cleanup failed: ' + ', '.join(errors))

    async def _idle(self):
        while True:
            await asyncio.sleep(30)
            async with self.lock:
                for key, session in tuple(self.sessions.items()):
                    if time.monotonic() - session.last_used > 300 and not session.lock.locked() and session.pending_action is None:
                        try:
                            await session.close()
                        except Exception:
                            logging.getLogger(__name__).exception('Idle browser cleanup failed; owner retained')
                        else:
                            self.sessions.pop(key, None)

    async def close(self):
        self.closed = True
        if self.idle_task:
            self.idle_task.cancel()
            await asyncio.gather(self.idle_task, return_exceptions=True)
        await self._close_sessions()

"""Session-owned Web calls and HTTP pools; standalone calls own temporary pools."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass

import httpx

from misaka.core.tools._common import run_with_abort
from misaka.core.web import debug
from misaka.core.web.network import api_network_key, api_network_options
from misaka.core.web.scope import current_scope
from misaka.core.web.timeouts import http_timeout, operation_deadline
from misaka.utils.async_lifecycle import settle

logger = logging.getLogger(__name__)
_current: ContextVar[WebRuntime | None] = ContextVar("web_runtime", default=None)


async def _close_client(client):
    try:
        await client.aclose()
    except Exception as error:  # noqa: BLE001 - teardown must preserve the request outcome
        logger.warning("Web HTTP client cleanup failed (%s)", type(error).__name__)


@dataclass
class _Client:
    client: httpx.AsyncClient
    borrowers: int = 0


class WebRuntime:
    """One owner and one event loop. No I/O occurs until a tool is called."""

    def __init__(self, scope=None):
        self.scope = current_scope() if scope is None else scope
        self._loop = None
        self._calls = set()
        self._clients = {}
        self._latest = {}
        self._closing = None
        self.closed = False
        self.debug_id = None
        self.browser = None

    def _check(self):
        if self.closed:
            raise RuntimeError("This Web session has been closed")
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("A Web session belongs to one event loop")
        self._loop = loop

    async def run(self, execute, *args, _tool_name="default", _tool_call=False, **kwargs):
        self._check()

        async def invoke():
            token = _current.set(self)
            try:
                with self.scope.activate(snapshot=True):
                    async with (debug.call(self, _tool_name, execute, args, kwargs, tool_call=_tool_call),
                                operation_deadline(_tool_name)):
                        result = await execute(*args, **kwargs)
                        debug.result(result)
                        return result
            finally:
                _current.reset(token)

        task = asyncio.create_task(invoke())
        self._calls.add(task)
        try:
            result, _ = await run_with_abort(task, None)
            return result
        finally:
            self._calls.discard(task)

    async def close(self):
        if asyncio.current_task() in self._calls:
            raise RuntimeError("Close Web resources from the lifecycle owner, not an active tool")
        if self._loop is not None and self._loop is not asyncio.get_running_loop():
            raise RuntimeError("Close a Web session on its owning event loop")
        if self._closing is None or (self._closing.done() and not self._closing.cancelled() and self._closing.exception()):
            self.closed = True
            self._closing = asyncio.create_task(self._finish())
        _, cancelled = await settle(self._closing)
        if cancelled is not None:
            raise cancelled

    async def _finish(self):
        token = _current.set(None)
        try:
            await self._finish_scoped()
        finally:
            _current.reset(token)

    async def _finish_scoped(self):
        with self.scope.activate(snapshot=True):
            calls = tuple(self._calls)
            for task in calls:
                task.cancel()
            await asyncio.gather(*calls, return_exceptions=True)
            browser_error = None
            if self.browser is not None:
                try:
                    await self.browser.close()
                except Exception as error:  # noqa: BLE001 - finish independent cleanup and report failures
                    browser_error = error
            clients, self._clients = tuple(self._clients.values()), {}
            self._latest.clear()
            await asyncio.gather(*(_close_client(entry.client) for entry in clients), return_exceptions=True)
            if browser_error is not None:
                raise browser_error

    @asynccontextmanager
    async def client(self, provider, endpoint, credential, *, timeout, follow_redirects):
        self._check()
        # Credentials identify a pool, but never appear in diagnostics or public cache keys.
        origin = httpx.URL(endpoint)
        origin_key = (origin.scheme, origin.host, origin.port)
        # Endpoint Basic Auth also owns cookies: changing that login must retire its pool.
        identity = hashlib.sha256(repr((credential, origin.username, origin.password)).encode()).digest()
        key = (provider, origin_key, identity,
               tuple(httpx.Timeout(timeout).as_dict().items()), follow_redirects, api_network_key())
        if key not in self._clients:
            async def guard_redirect(request):
                target = request.url
                if (origin.scheme, origin.host, origin.port) != (target.scheme, target.host, target.port):
                    # API payloads can also contain credentials. Do not forward their
                    # bodies to a new origin merely because a vendor returned Location.
                    raise httpx.RequestError("Cross-origin Web API redirect rejected", request=request)

            self._clients[key] = _Client(httpx.AsyncClient(
                timeout=timeout, follow_redirects=follow_redirects,
                event_hooks={"request": [guard_redirect]} if follow_redirects else {},
                **api_network_options(endpoint),
            ))
        entry = self._clients[key]
        self._latest[provider] = key
        entry.borrowers += 1
        try:
            # Retire an idle pool on config/credential rotation. Busy pools are
            # released by their last borrower, never underneath an active request.
            for old_key, old in tuple(self._clients.items()):
                if old_key[0] == provider and old_key != key and old.borrowers == 0:
                    del self._clients[old_key]
                    _, cancelled = await settle(asyncio.create_task(_close_client(old.client)))
                    if cancelled is not None:
                        raise cancelled
            yield entry.client
        finally:
            entry.borrowers -= 1
            if not entry.borrowers and self._latest.get(provider) != key:
                self._clients.pop(key, None)
                _, cancelled = await settle(asyncio.create_task(_close_client(entry.client)))
                if cancelled is not None:
                    raise cancelled


@asynccontextmanager
async def api_client(provider, endpoint, credential="", *, timeout=60.0, follow_redirects=False,
                     timeout_provider=None):
    """Borrow this tool's pool, or close a temporary standalone pool on every exit."""
    owner = _current.get()
    timeout = http_timeout(provider if timeout_provider is None else timeout_provider, timeout)
    temporary = owner is None
    if temporary:
        owner = WebRuntime()
    try:
        async with owner.client(provider, endpoint, credential, timeout=timeout, follow_redirects=follow_redirects) as client:
            yield client
    finally:
        if temporary:
            await owner.close()


def current_runtime():
    owner = _current.get()
    if owner is None:
        raise RuntimeError("Browser tools require a session-owned WebRuntime")
    return owner

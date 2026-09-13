"""MCP network transports through the official SDK, not a second protocol stack.

CCB services/mcp/client.ts selects the SDK SSE/HTTP/WebSocket transports. This
adapter performs that selection in Python and keeps each SDK task group owned
by one coroutine. MISAKA's existing role-owned stdio client is unchanged.
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any

import httpx

from misaka.core.mcp import (
    CALL_TIMEOUT,
    INIT_TIMEOUT,
    MAX_LIST_PAGES,
    McpClient,
    McpRoleContext,
)
from misaka.core.tools._common import abort_race
from misaka.utils.async_lifecycle import settle


class NetworkMcpClient(McpClient):
    def __init__(self, name, cfg, role_context=None):
        super().__init__(name, cfg, role_context or McpRoleContext.capture())
        self._owner: asyncio.Task | None = None
        self._connected: asyncio.Future | None = None
        self._closing = asyncio.Event()
        self._session: Any = None
        self.capabilities: dict[str, Any] = {}
        self.http_client_factory = httpx.AsyncClient
        self._login_callback = None
        self.auth = None
        self._stop_job = None
        self.auth_required = False

    async def _serve(self):
        try:
            from mcp import ClientSession, types
            from mcp.client.sse import sse_client
            from mcp.client.streamable_http import streamable_http_client

            kind = self.cfg.get("type") or "http"
            from misaka.core.subagent.mcp_headers import server_headers

            headers = await server_headers(self.name, self.cfg)
            self.auth = self.cfg.get("_auth")
            if self.auth is None and kind in {"http", "sse"}:
                from misaka.core.subagent.mcp_auth import McpOAuthProvider

                self.auth = McpOAuthProvider(self.name, self.cfg, self.role_context, callback=self._login_callback,
                                             refresh_client_factory=self.http_client_factory)
            async with AsyncExitStack() as stack:
                if kind in {"sse", "sse-ide"}:
                    transport = sse_client(self.cfg["url"], headers=headers, timeout=INIT_TIMEOUT, sse_read_timeout=86400, auth=self.auth, httpx_client_factory=self.http_client_factory)
                elif kind in {"ws", "ws-ide"}:
                    from misaka.core.subagent.mcp_websocket import websocket_client

                    if kind == "ws-ide" and self.cfg.get("authToken"):
                        headers["X-Claude-Code-Ide-Authorization"] = self.cfg["authToken"]
                    transport = websocket_client(self.cfg["url"], headers=headers)
                elif kind == "http":
                    client = await stack.enter_async_context(self.http_client_factory(
                        headers=headers, auth=self.auth,
                        timeout=httpx.Timeout(CALL_TIMEOUT, read=None), follow_redirects=False,
                    ))
                    transport = streamable_http_client(self.cfg["url"], http_client=client)
                else:
                    raise ValueError(f"MCP transport {kind!r} requires its owning host connection")
                streams = await stack.enter_async_context(transport)
                self._session = await stack.enter_async_context(ClientSession(
                    *streams[:2], read_timeout_seconds=timedelta(seconds=CALL_TIMEOUT + (300 if self._login_callback else 0)),
                    client_info=types.Implementation(name="misaka", version="1.0"),
                ))
                result = await self._session.initialize()
                self.capabilities = result.capabilities.model_dump(mode="json", exclude_none=True)
                self.tools = []
                cursor = None
                if result.capabilities.tools is not None:
                    for _ in range(MAX_LIST_PAGES):
                        page = await self._session.list_tools(cursor=cursor)
                        self.tools.extend(item.model_dump(mode="json", by_alias=True, exclude_none=True) for item in page.tools)
                        cursor = page.nextCursor
                        if not cursor:
                            break
                self._ready = True
                self.auth_required = False
                if not self._connected.done():
                    self._connected.set_result(None)
                await self._closing.wait()
        except BaseException as error:
            from misaka.core.subagent.mcp_auth import McpAuthRequired

            pending = [error]
            public_error = error
            while pending:
                failure = pending.pop()
                if isinstance(failure, McpAuthRequired):
                    self.auth_required = True
                    public_error = failure
                    break
                if isinstance(failure, BaseExceptionGroup):
                    pending.extend(failure.exceptions)
            if self._connected is not None and not self._connected.done():
                self._connected.set_exception(public_error)
            raise
        finally:
            self._ready = False
            self._session = None

    async def start(self, *, timeout=INIT_TIMEOUT):
        await self.stop()
        self._closing = asyncio.Event()
        self._connected = asyncio.get_running_loop().create_future()
        self._owner = asyncio.create_task(self._serve())
        self._owner.add_done_callback(lambda job: None if job.cancelled() else job.exception())
        try:
            await asyncio.wait_for(asyncio.shield(self._connected), timeout)
        except BaseException:
            await self.stop()
            raise
        return self.tools

    async def authenticate(self, notify):
        from misaka.core.subagent.mcp_auth import (
            AUTH_TIMEOUT,
            McpTokenStorage,
            OAuthCallback,
            clear_server_tokens,
        )

        if (self.cfg.get("type") or "http") not in {"http", "sse"}:
            raise ValueError("This MCP transport uses its owning host's authentication")
        async with self._start_lock:
            await self.stop()
            storage = McpTokenStorage(self.name, self.cfg, self.role_context)
            await clear_server_tokens(storage, self.http_client_factory, preserve_scope=True)
            port = (self.cfg.get("oauth") or {}).get("callbackPort")
            async with OAuthCallback(notify, port=port) as callback:
                self._login_callback = callback
                try:
                    return await self.start(timeout=AUTH_TIMEOUT + INIT_TIMEOUT)
                finally:
                    self._login_callback = None
                    # The returned client may later receive a step-up challenge;
                    # the expired callback must not remain its interactive handler.
                    if self.auth is not None and hasattr(self.auth, "callback"):
                        self.auth.callback = None

    async def logout(self):
        from misaka.core.subagent.mcp_auth import McpTokenStorage, clear_server_tokens

        async with self._start_lock:
            await self.stop()
            return await clear_server_tokens(McpTokenStorage(self.name, self.cfg, self.role_context), self.http_client_factory)

    def _usable(self):
        return self._ready and self._owner is not None and not self._owner.done()

    async def _request(self, method, params, timeout=None, signal=None):
        from mcp import types

        result_types = {
            "tools/call": types.CallToolResult, "tools/list": types.ListToolsResult,
            "resources/list": types.ListResourcesResult, "resources/read": types.ReadResourceResult,
            "resources/templates/list": types.ListResourceTemplatesResult,
        }
        request = types.ClientRequest.model_validate({"method": method, "params": params})
        job = asyncio.create_task(self._session.send_request(
            request, result_types[method], request_read_timeout_seconds=timedelta(seconds=timeout or CALL_TIMEOUT),
        ))
        try:
            async with abort_race(signal) as aborting:
                if aborting is not None:
                    done, _ = await asyncio.wait({job, aborting}, return_when=asyncio.FIRST_COMPLETED)
                    if job not in done:
                        raise RuntimeError("Operation aborted")
                return (await job).model_dump(mode="json", by_alias=True, exclude_none=True)
        finally:
            if not job.done():
                job.cancel()
            await asyncio.gather(job, return_exceptions=True)

    async def stop(self):
        if self._stop_job is None or self._stop_job.done():
            self._ready = False
            self._closing.set()
            owner, self._owner = self._owner, None
            connected = self._connected
            async def drain():
                if owner is not None:
                    if connected is None or not connected.done():
                        owner.cancel()
                    await asyncio.gather(owner, return_exceptions=True)
                if connected is not None and connected.done() and not connected.cancelled():
                    connected.exception()
            self._stop_job = asyncio.create_task(drain())
        _, cancelled = await settle(self._stop_job)
        if cancelled is not None:
            raise cancelled

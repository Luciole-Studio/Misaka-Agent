"""The part of the MCP integration that talks to servers: connections, transports, and OAuth
sign-in. It pulls in the MCP client, so ``__init__.py`` loads it through ``runtime_lazy.py`` only when a
server is configured.

Translated from pi's ``coding-agent/src/extensions/mcp/runtime.ts``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any, Literal, NotRequired, TypedDict

from misaka.config.engine import VERSION
from misaka.core.resolve_config_value import (
    resolveConfigValueOrThrow,
    resolveHeadersOrThrow,
)
from misaka.mcp import (
    JSON_RPC_ERROR_CODES,
    AuthProvider,
    CallToolResult,
    ListResourcesResult,
    ListResourceTemplatesResult,
    McpAuthRequiredError,
    McpClient,
    McpError,
    McpHttpError,
    McpRequestOptions,
    McpSessionExpiredError,
    McpTransport,
    ReadResourceResult,
    Resource,
    ResourceTemplate,
    StdioTransport,
    StreamableHttpTransport,
)
from misaka.mcp import Tool as McpTool
from misaka.mcp.oauth import McpOAuthAuthorizationRequiredError, OAuthChallenge
from misaka.ui.tui.terminal_image import pathToFileURL
from misaka.utils import node_path
from misaka.utils.js import (
    AbortController,
    eager,
    errorMessage,
    jsSlice,
    jsTrim,
    jsTruthy,
    raceAbort,
    spawn,
    startTask,
)
from misaka.utils.whatwg_url import URL

from .config import McpServerEntry
from .environment import inheritedEnvironment
from .log import McpServerLog
from .oauth import (
    McpAuthProvider,
    McpOAuthCredentialStore,
    McpOAuthSettings,
    McpSignInCancelledError,
    createMcpAuthProvider,
    signInMcpServer,
)

DEFAULT_TIMEOUT_SECONDS = 60
STDERR_TAIL_CHARS = 2_000
# Delays between attempts to connect to an HTTP server that failed with a transient error.
CONNECT_RETRY_DELAYS_MS = [250, 1_000]

# `disconnected`: the connection dropped (for example the stdio server exited); the next call
# reconnects.
type ServerState = Literal["connecting", "connected", "disconnected", "needs-auth", "failed", "closed"]

type McpTransportFactory = Callable[[McpServerEntry, str, AuthProvider | None], McpTransport]


def isTransientError(error: Any) -> bool:
    """Network failures and overloaded or restarting servers, which are worth another attempt."""
    if isinstance(error, McpHttpError):
        return error.status == 408 or error.status == 429 or (error.status >= 500 and error.status != 501)
    return isinstance(error, TypeError)


def signInRequiredMessage(entry: McpServerEntry) -> str:
    provider = (entry["config"].get("auth") or {}).get("provider") if "url" in entry["config"] else None  # type: ignore[typeddict-item]
    return f'MCP server "{entry["name"]}" requires sign-in. Run {f"/login {provider}" if provider else "/mcp"} to sign in.'


def usesOAuth(entry: McpServerEntry) -> bool:
    """HTTP servers authenticate with OAuth unless the config supplies an ``Authorization`` header or ``auth``."""
    config = entry["config"]
    if "url" not in config or config.get("auth") is not None:  # type: ignore[typeddict-item]
        return False
    return not any(header.lower() == "authorization" for header in (config.get("headers") or {}))  # type: ignore[typeddict-item]


def expandHome(value: str) -> str:
    """``~`` and ``~/…`` (also ``~\\…`` on Windows) name the home directory, like in a shell."""
    if value == "~":
        return os.path.expanduser("~")
    if value.startswith("~/") or (sys.platform == "win32" and value.startswith("~\\")):
        return node_path.join(os.path.expanduser("~"), value[2:])
    return value


def createDefaultTransport(entry: McpServerEntry, cwd: str, authProvider: AuthProvider | None) -> McpTransport:
    config, name = entry["config"], entry["name"]
    if "url" in config:
        return StreamableHttpTransport({
            "url": config["url"],  # type: ignore[typeddict-item]
            "headers": resolveHeadersOrThrow(config.get("headers"), f'MCP server "{name}"'),  # type: ignore[typeddict-item]
            **({"authProvider": authProvider} if authProvider is not None else {}),
        })
    env: dict[str, str] = {}
    for key, value in (config.get("env") or {}).items():  # type: ignore[typeddict-item]
        env[key] = resolveConfigValueOrThrow(value, f'MCP server "{name}" env "{key}"')
    args = config.get("args")  # type: ignore[typeddict-item]
    configCwd = config.get("cwd")  # type: ignore[typeddict-item]
    return StdioTransport({
        "command": expandHome(config["command"]),  # type: ignore[typeddict-item]
        **({"args": [expandHome(arg) for arg in args]} if args is not None else {}),
        "cwd": node_path.resolve(cwd, expandHome(configCwd if configCwd is not None else ".")),
        # MISAKA fork: a stdio server inherits only the names it needs from MISAKA's environment,
        # which holds API keys (issue #10; see .environment); what else it needs is named in its env.
        "env": {**inheritedEnvironment(), **env},
        "inheritEnv": False,
        "stderr": "pipe",
    })


async def withoutTemplates[T](list: Callable[[], Awaitable[T]], empty: T) -> T:
    """Servers that do not implement ``resources/templates/list`` have no templates."""
    try:
        return await list()
    except McpError as error:
        if error.code == JSON_RPC_ERROR_CODES.methodNotFound:
            return empty
        raise


def listTemplates(client: McpClient, options: McpRequestOptions | None = None) -> Awaitable[list[ResourceTemplate]]:
    return withoutTemplates(lambda: client.listResourceTemplates(options if options is not None else {}), [])


class McpServerConnectionOptions(TypedDict):
    entry: McpServerEntry
    cwd: str
    createTransport: McpTransportFactory
    credentials: McpOAuthCredentialStore
    # The current token of a provider, for servers with `auth.provider`.
    providerToken: NotRequired[Callable[[str], Awaitable[str | None]]]
    onTools: Callable[[McpServerConnection], None]
    # Called when `state`, `error`, or `tools` change.
    onChange: NotRequired[Callable[[McpServerConnection], None]]
    # Receives the server's log messages (`notifications/message`).
    log: NotRequired[McpServerLog]


async def _closeQuietly(client: McpClient) -> None:
    """`client.close().catch(() => undefined)`."""
    with contextlib.suppress(Exception):
        await client.close()


async def _settleQuietly(operation: Awaitable[Any]) -> None:
    """`promise.catch(() => undefined)`."""
    with contextlib.suppress(Exception):
        await operation


def _resolved[T](value: T) -> asyncio.Future[T]:
    future: asyncio.Future[T] = asyncio.get_running_loop().create_future()
    future.set_result(value)
    return future


class McpServerConnection:
    """One configured server. Reconnects lazily when a call finds the connection gone."""

    def __init__(self, options: McpServerConnectionOptions) -> None:
        self.entry: McpServerEntry = options["entry"]
        self.state: ServerState = "connecting"
        self.error: str | None = None
        self.tools: list[McpTool] = []
        # Whether the server offers resources, which the resource tools list and read on demand. They are
        # not listed at connect: listing every resource can take long (a large drive, say).
        self.hasResources = False
        # Server instructions from `initialize`, describing its tools as a group.
        self.instructions: str | None = None
        # Last OAuth challenge from the server; sign-in uses its resource metadata URL and scope.
        self.challenge: OAuthChallenge | None = None
        self._client: McpClient | None = None
        self._opening: asyncio.Task[McpClient] | None = None
        # Aborted by `close()`; cancels a connect in progress, including the wait between retries.
        self._shutdown = AbortController()
        # Stderr of the last stdio server that failed to connect.
        self._stderrTail: str | None = None
        self._cwd = options["cwd"]
        self._createTransport = options["createTransport"]
        self._onTools = options["onTools"]
        self._onChange = options.get("onChange")
        self._log = options.get("log")
        # MISAKA fork: asyncio holds tasks weakly; refreshes and the opening connect live here.
        self._tasks: set[asyncio.Task[Any]] = set()
        url = self.oauthUrl
        provider = (self.entry["config"].get("auth") or {}).get("provider") if "url" in self.entry["config"] else None  # type: ignore[typeddict-item]
        self._authProvider: McpAuthProvider | Any | None
        if url:

            def onChallenge(challenge: OAuthChallenge) -> None:
                self.challenge = challenge

            self._authProvider = createMcpAuthProvider({
                "serverUrl": url,
                "store": options["credentials"].forServer(self.entry["name"], url),
                "settings": lambda: self.oauthSettings(),
                "onChallenge": onChallenge,
            })
        elif provider:
            providerToken = options.get("providerToken")

            async def token() -> str | None:
                return await providerToken(provider) if providerToken is not None else None

            async def settled() -> None:
                pass

            # Read on every request, so the provider's refreshes apply; MCP stores no copy.
            self._authProvider = SimpleNamespace(token=token, settled=settled)
        else:
            self._authProvider = None

    @property
    def name(self) -> str:
        return self.entry["name"]

    @property
    def _closed(self) -> bool:
        return self._shutdown.signal.aborted

    @property
    def timeoutMs(self) -> float:
        timeout = self.entry["config"].get("timeout")
        return (timeout if timeout is not None else DEFAULT_TIMEOUT_SECONDS) * 1000

    @property
    def oauthUrl(self) -> str | None:
        """Server URL when the server authenticates with OAuth."""
        return self.entry["config"]["url"] if usesOAuth(self.entry) and "url" in self.entry["config"] else None  # type: ignore[typeddict-item]

    def oauthSettings(self) -> McpOAuthSettings:
        oauth = self.entry["config"].get("oauth") if "url" in self.entry["config"] else None  # type: ignore[typeddict-item]
        if not oauth:
            return {}
        return {  # type: ignore[typeddict-item]
            "clientId": oauth.get("clientId"),
            "clientSecret": (
                None
                if oauth.get("clientSecret") is None
                else resolveConfigValueOrThrow(oauth["clientSecret"], f'MCP server "{self.entry["name"]}" oauth.clientSecret')
            ),
            "callbackPort": oauth.get("callbackPort"),
            "callbackUrl": oauth.get("callbackUrl"),
            "scope": oauth.get("scope"),
            "clientName": oauth.get("clientName"),
            "clientRegistration": oauth.get("clientRegistration"),
            "authServerMetadataUrl": URL(oauth["authServerMetadataUrl"]) if oauth.get("authServerMetadataUrl") else None,
        }

    def getClient(self) -> asyncio.Future[McpClient]:
        if self._closed:
            failed: asyncio.Future[McpClient] = asyncio.get_running_loop().create_future()
            failed.set_exception(RuntimeError(f'MCP server "{self.entry["name"]}" is shut down'))
            return failed
        if self._client is not None and self._client.connectionState == "connected":
            return _resolved(self._client)
        if self._opening is None:
            opening = spawn(self._open(), self._tasks)

            def clear(_task: asyncio.Task[McpClient]) -> None:
                self._opening = None

            opening.add_done_callback(clear)
            self._opening = opening
        # MISAKA fork: shielded, so one caller giving up does not cancel the connect the others share.
        return asyncio.shield(self._opening)

    def callTool(self, name: str, args: dict[str, Any], options: McpRequestOptions) -> asyncio.Future[CallToolResult]:
        return self._withClient(lambda client: client.callTool(name, args, options))

    def readResource(self, uri: str, options: McpRequestOptions) -> asyncio.Future[ReadResourceResult]:
        return self._withClient(lambda client: client.readResource(uri, options), True)

    def resourcesPage(self, cursor: str | None, options: McpRequestOptions) -> asyncio.Future[ListResourcesResult]:
        return self._withClient(lambda client: client.listResourcesPage(cursor, options), True)

    def resourceTemplatesPage(
        self, cursor: str | None, options: McpRequestOptions
    ) -> asyncio.Future[ListResourceTemplatesResult]:
        return self._withClient(
            lambda client: withoutTemplates(lambda: client.listResourceTemplatesPage(cursor, options), {"resourceTemplates": []}),
            True,
        )

    def allResources(self, options: McpRequestOptions) -> asyncio.Future[list[Resource]]:
        return self._withClient(lambda client: client.listResources(options), True)

    def allResourceTemplates(self, options: McpRequestOptions) -> asyncio.Future[list[ResourceTemplate]]:
        return self._withClient(lambda client: listTemplates(client, options), True)

    @eager
    async def _withClient[T](self, run: Callable[[McpClient], Awaitable[T]], readOnly: bool = False) -> T:
        """Run a request, reconnecting when needed. ``readOnly`` requests are retried once after a transient
        HTTP error; tool calls are not, since they may have run."""
        attempt = 1
        while True:
            client = await self.getClient()
            try:
                return await run(client)
            except Exception as error:
                if readOnly and attempt == 1 and isinstance(error, McpHttpError) and isTransientError(error):
                    await asyncio.sleep(CONNECT_RETRY_DELAYS_MS[0] / 1000)
                    attempt += 1
                    continue
                if isinstance(error, McpSessionExpiredError) and attempt == 1:
                    # The server no longer knows the session (restart, deploy), so it did not run the request.
                    # Retry once on a new session. The old client is detached but not closed: closing would
                    # fail its other in-flight calls, which instead get the same 404 and retry the same way.
                    if self._client is client:
                        self._client = None
                    attempt += 1
                    continue
                if not self._needsSignIn(error):
                    raise
                await self._dropClient(client)
                self._markNeedsAuth()
                raise RuntimeError(signInRequiredMessage(self.entry))

    async def reconnect(self) -> None:
        """Connect again with fresh credentials, for example after signing in."""
        if self._opening is not None:
            await _settleQuietly(asyncio.shield(self._opening))
        if self._client is not None:
            await self._dropClient(self._client)
        await self.getClient()

    async def signOut(self) -> None:
        """Disconnect after the stored credentials were removed."""
        if self._opening is not None:
            await _settleQuietly(asyncio.shield(self._opening))
        if self._client is not None:
            await self._dropClient(self._client)
        if not self._closed:
            self._markNeedsAuth()

    def _needsSignIn(self, error: Any) -> bool:
        """OAuth servers that still reject the request after a refresh need the user to sign in again."""
        return isinstance(error, McpOAuthAuthorizationRequiredError) or (
            self._authProvider is not None and isinstance(error, McpAuthRequiredError)
        )

    def _markNeedsAuth(self) -> None:
        self.state = "needs-auth"
        self.error = None
        self._changed()

    def _changed(self) -> None:
        if self._onChange is not None:
            self._onChange(self)

    async def _dropClient(self, client: McpClient) -> None:
        if self._client is client:
            self._client = None
        await _closeQuietly(client)

    async def _open(self) -> McpClient:
        self.state = "connecting"
        self._changed()
        retries = CONNECT_RETRY_DELAYS_MS if "url" in self.entry["config"] else []
        attempt = 0
        while True:
            self._stderrTail = None
            try:
                return await self._connectOnce()
            except Exception as error:  # noqa: BLE001 - upstream `catch (error)`: rethrown as the connect failure
                delay = retries[attempt] if attempt < len(retries) else None
                if self._closed or delay is None or not isTransientError(error):
                    raise self._connectFailed(error)
                # MISAKA fork: `sleep(delay, undefined, { signal })` from node:timers/promises, whose
                # rejection on abort is ignored.
                with contextlib.suppress(Exception):
                    await raceAbort(asyncio.sleep(delay / 1000), self._shutdown.signal)
                if self._closed:
                    raise self._connectFailed(error)
            attempt += 1

    async def _connectOnce(self) -> McpClient:
        client = McpClient({
            # MISAKA fork: misaka's own name and version, where pi sends `pi`.
            "name": "misaka",
            "version": VERSION,
            "requestTimeoutMs": self.timeoutMs,
            "roots": [{"uri": pathToFileURL(self._cwd), "name": node_path.basename(self._cwd)}],
        })
        log = self._log
        if log is not None:
            client.onNotification("notifications/message", lambda params: log.write(self.entry["name"], params))
        # Closing the client fails its pending initialize or discovery requests and closes the transport.
        # Memoized: a second `client.close()` returns before the first finished closing the transport.
        closing: asyncio.Future[None] | None = None

        def closeClient() -> asyncio.Future[None]:
            nonlocal closing
            if closing is None:
                closing = startTask(_closeQuietly(client))
            return closing

        self._shutdown.signal.addEventListener("abort", closeClient, once=True)
        transport: McpTransport | None = None
        try:
            transport = self._createTransport(self.entry, self._cwd, self._authProvider)
            await client.connect(transport)
            client.onNotification(
                "notifications/tools/list_changed", lambda _params: spawn(self._refreshTools(client), self._tasks)
            )
            stdio = transport if isinstance(transport, StdioTransport) else None
            client.onClose(lambda: self._handleClientClose(client, stdio))
            # Servers without the tools capability (prompts or resources only) do not answer tools/list.
            tools = (
                await client.listTools()
                if client.serverCapabilities is not None and jsTruthy(client.serverCapabilities.get("tools"))
                else []
            )
            if self._closed:
                raise RuntimeError("shut down while connecting")
            if client.connectionState != "connected":
                raise RuntimeError("connection closed during setup")
            self._client = client
            self.tools = tools
            self.hasResources = client.serverCapabilities is not None and "resources" in client.serverCapabilities
            self.instructions = jsTrim(client.instructions or "") or None
            self.state = "connected"
            self.error = None
            self._onTools(self)
            self._changed()
            return client
        except BaseException:
            await closeClient()
            if isinstance(transport, StdioTransport):
                self._stderrTail = jsSlice(jsTrim(transport.stderr), -STDERR_TAIL_CHARS) or None
            raise
        finally:
            self._shutdown.signal.removeEventListener("abort", closeClient)

    def _connectFailed(self, error: Any) -> Exception:
        if self._needsSignIn(error) and not self._closed:
            self._markNeedsAuth()
            return RuntimeError(signInRequiredMessage(self.entry))
        self.state = "closed" if self._closed else "failed"
        self.error = f"{errorMessage(error)}\n{self._stderrTail}" if self._stderrTail else errorMessage(error)
        self._changed()
        return RuntimeError(f'MCP server "{self.entry["name"]}" failed to connect: {self.error}')

    def _handleClientClose(self, client: McpClient, stdio: StdioTransport | None) -> None:
        """The transport dropped. The next call reconnects; until then the status shows why."""
        if self._client is not client or self._closed:
            return
        self._client = None
        self.state = "disconnected"
        stderr = jsSlice(jsTrim(stdio.stderr), -STDERR_TAIL_CHARS) if stdio is not None else None
        self.error = f"Connection closed\n{stderr}" if stderr else "Connection closed"
        self._changed()

    async def _refreshTools(self, client: McpClient) -> None:
        try:
            tools = await client.listTools()
            if self._client is not client or self._closed:
                return
            self.tools = tools
            self._onTools(self)
        except Exception as error:  # noqa: BLE001 - upstream `catch (error)`: recorded as the connection's error
            self.error = f"Failed to refresh tools: {errorMessage(error)}"
        self._changed()

    async def close(self) -> None:
        """Resolves once no transport of this server is open, including one that was still connecting."""
        self._shutdown.abort()
        self.state = "closed"
        self._changed()
        client = self._client
        self._client = None
        await asyncio.gather(
            _closeQuietly(client) if client is not None else _resolved(None),
            # MISAKA fork: shielded, as every waiter of the shared connect is.
            _settleQuietly(asyncio.shield(self._opening)) if self._opening is not None else _resolved(None),
        )
        # A refresh the server already answered may have rotated the refresh token; exiting before the
        # new tokens are saved would lose the grant.
        if self._authProvider is not None:
            await self._authProvider.settled()


__all__ = [
    "McpOAuthCredentialStore",
    "McpServerConnection",
    "McpServerLog",
    "McpSignInCancelledError",
    "McpTransportFactory",
    "createDefaultTransport",
    "signInMcpServer",
]

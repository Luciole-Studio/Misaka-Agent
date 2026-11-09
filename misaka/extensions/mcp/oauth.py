"""OAuth sign-in for remote MCP servers.

Connections never start a browser flow on their own. They send the stored access token and, after
a 401, try the stored refresh token. When that is not possible they fail with
``McpOAuthAuthorizationRequiredError``, and the user signs in through ``/mcp``, which runs
the authorization code flow (PKCE, a Client ID Metadata Document or dynamic client registration)
against a loopback callback.

Credentials live in ``credentials/mcp-auth.json`` under the home, keyed by server name and URL.

Translated from pi's ``coding-agent/src/extensions/mcp/oauth.ts``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, NotRequired, Protocol, TypedDict

from misaka.ai.utils.oauth_page import oauthErrorHtml, oauthSuccessHtml
from misaka.config import home
from misaka.config.engine import APP_NAME
from misaka.core.auth_storage import (
    AuthStorageBackend,
    FileAuthStorageBackend,
    LockResult,
)
from misaka.core.mcp_servers import mcpNamespace
from misaka.mcp.auth_provider import AuthProvider, McpFetch, UnauthorizedContext
from misaka.mcp.oauth import (
    AuthorizationServerMetadata,
    McpOAuthAuthorizationRequiredError,
    McpOAuthProvider,
    McpOAuthState,
    McpOAuthStateStore,
    OAuthCallback,
    OAuthCallbackPage,
    OAuthCallbackServer,
    OAuthChallenge,
    OAuthClientInformationMixed,
    OAuthClientMetadataDocument,
    authorizeMcp,
    parseWwwAuthenticate,
    stepUpScope,
)
from misaka.utils import lockfile
from misaka.utils import whatwg_fetch as _fetch
from misaka.utils.js import (
    AbortController,
    AbortSignal,
    defined,
    jsNumber,
    jsonParse,
    jsonStringify,
    jsSplitWhitespace,
    jsString,
    jsTrim,
    startTask,
)
from misaka.utils.values import maybe_await, read_field, signal_aborted
from misaka.utils.whatwg_url import URL

CALLBACK_HOST = "127.0.0.1"
CALLBACK_PATH = "/callback"
# Where pi.dev serves pi's Client ID Metadata Documents: `client.json` and `<callback ID>/client.json`.
# MISAKA fork: unreached, since the config validation rejects `clientRegistration: "cimd"`.
CLIENT_METADATA_BASE_URL = "https://pi.dev/oauth"
# Redirect URI for refreshes when none is stored. Refreshing never redirects the user.
FALLBACK_REDIRECT_URL = f"http://{CALLBACK_HOST}{CALLBACK_PATH}"
# Access tokens this close to expiry are refreshed before they are sent.
REFRESH_SKEW_MS = 30_000
# Bounds each request to the authorization server, so an unresponsive one cannot hold the refresh lock,
# delay shutdown, or keep a sign-in waiting for long.
OAUTH_REQUEST_TIMEOUT_MS = 15_000
# A refresh lock that its holder stopped renewing (the process was killed) is taken over after this.
REFRESH_LOCK_STALE_MS = 20_000
# How long to wait for another process's refresh: longer than a stale lock lives.
REFRESH_LOCK_WAIT_MS = 25_000
REFRESH_LOCK_RETRY_MS = 100


class McpOAuthSettings(TypedDict, total=False):
    clientId: str
    # Already resolved.
    clientSecret: str
    callbackPort: int
    # Loopback redirect URI; see `McpOAuthConfig.callbackUrl`.
    callbackUrl: str
    # Scopes to request, separated by spaces.
    scope: str
    # `client_name` for dynamic client registration. Default: `APP_NAME`.
    clientName: str
    # See `McpOAuthConfig.clientRegistration`.
    clientRegistration: Literal["dcr", "cimd"]
    # See `McpOAuthConfig.authServerMetadataUrl`.
    authServerMetadataUrl: URL


@dataclass(slots=True)
class CallbackSettings:
    """Where the loopback callback server listens and the redirect URI it serves."""

    # Address to listen on.
    host: str
    # Host name in the redirect URI.
    redirectHost: str
    port: int | None
    path: str
    # The exact redirect URI, when the port is fixed.
    fixedRedirectUrl: str | None


def callbackSettings(settings: McpOAuthSettings) -> CallbackSettings:
    callbackUrl = settings.get("callbackUrl")
    url = URL(callbackUrl if callbackUrl is not None else f"http://{CALLBACK_HOST}{CALLBACK_PATH}")
    address = re.sub(r"^\[|\]$", "", url.hostname)
    port = int(url.port) if url.port else settings.get("callbackPort")
    # MISAKA fork: JSON's `8080.0` is JavaScript's 8080, but Python reads it as a float, which no socket binds.
    if isinstance(port, float):
        port = int(port)
    fixedRedirectUrl: str | None = None
    # A configured URI with a port is sent exactly as written, since servers compare it as a string.
    if url.port:
        fixedRedirectUrl = callbackUrl
    elif port is not None:
        url.port = jsString(port)
        fixedRedirectUrl = url.href
    return CallbackSettings(
        # `localhost` is served on 127.0.0.1; browsers fall back to it when ::1 refuses.
        host=CALLBACK_HOST if address == "localhost" else address,
        redirectHost=address,
        port=port,
        path=url.pathname,
        fixedRedirectUrl=fixedRedirectUrl,
    )


def mergeScopes(*scopes: str | None) -> str | None:
    """Scopes of both lists, each once."""
    merged = list(dict.fromkeys(
        item for scope in scopes for item in (jsSplitWhitespace(scope) if scope else []) if item
    ))
    return " ".join(merged) if len(merged) > 0 else None


type _StoredStates = dict[str, McpOAuthState]


def parseStates(content: str | None) -> _StoredStates:
    if not content or not jsTrim(content):
        return {}
    parsed: Any = jsonParse(content)
    return parsed if isinstance(parsed, dict) else {}


def serializeStates(states: _StoredStates) -> str:
    return f"{jsonStringify(states, 2)}\n"


def storeKeys(name: str, serverUrl: str) -> tuple[str, str]:
    """Keys of a server's state: by name and URL, so servers sharing a URL keep separate accounts, and the
    legacy key by URL alone, written by older versions."""
    legacyKey = jsString(URL(serverUrl))
    return f"{mcpNamespace(name)}|{legacyKey}", legacyKey


class McpOAuthServerStore(McpOAuthStateStore, Protocol):
    async def withRefreshLock(self, fn: Callable[[], Awaitable[Any]]) -> Any:
        """Run ``fn`` while no other process refreshes the server's tokens."""
        ...


class _ServerStore:
    """The ``McpOAuthServerStore`` of one server: ``load`` and ``save`` are synchronous, as the backend is."""

    def __init__(self, store: McpOAuthCredentialStore, key: str, legacyKey: str) -> None:
        self._store = store
        self._key = key
        self._legacyKey = legacyKey

    def load(self) -> McpOAuthState | None:
        key, legacyKey = self._key, self._legacyKey

        # The first server to load legacy state takes it over; others with the same URL sign in again.
        def takeOver(current: str | None) -> LockResult:
            states = parseStates(current)
            # `states[key]` as a JavaScript condition: an object is truthy, a missing or null entry is not.
            if states.get(key) is not None or states.get(legacyKey) is None:
                return LockResult(result=states.get(key))
            states[key] = states[legacyKey]
            del states[legacyKey]
            return LockResult(result=states[key], next=serializeStates(states))

        return self._store._backend.withLock(takeOver)

    def save(self, state: McpOAuthState) -> None:
        def update(states: _StoredStates) -> None:
            states[self._key] = state

        self._store._write(update)

    async def withRefreshLock(self, fn: Callable[[], Awaitable[Any]]) -> Any:
        return await self._store._withRefreshLock(self._key, fn)


class McpOAuthCredentialStore:
    """Per-server OAuth state (client registration, tokens, pending PKCE verifier) in ``mcp-auth.json``."""

    def __init__(self, backend: AuthStorageBackend | None = None, lockDir: str | None = None) -> None:
        # MISAKA fork: the file sits with the other credentials and the lock files under `run/locks`,
        # where pi uses `<agent-dir>/mcp-auth.json` and the agent directory.
        self._backend: AuthStorageBackend = (
            backend if backend is not None else FileAuthStorageBackend(str(home.path("mcp_auth")))
        )
        # Directory for the refresh lock files. Without one, refreshes are only serialized in this process.
        self._lockDir: str | None = lockDir if backend is not None else str(home.path("locks"))

    def forServer(self, name: str, serverUrl: str) -> McpOAuthServerStore:
        key, legacyKey = storeKeys(name, serverUrl)
        return _ServerStore(self, key, legacyKey)

    async def _withRefreshLock(self, key: str, fn: Callable[[], Awaitable[Any]]) -> Any:
        """A lock file per server. When the process exits, proper-lockfile removes the locks it holds; when
        it is killed, the lock goes stale because it is no longer renewed, and the next process takes it over."""
        if not self._lockDir:
            return await fn()
        os.makedirs(self._lockDir, mode=0o700, exist_ok=True)
        hash = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        release = await lockfile.lock(
            os.path.join(self._lockDir, f"mcp-auth-refresh-{hash}"),
            {
                "realpath": False,
                "stale": REFRESH_LOCK_STALE_MS,
                "retries": {
                    "retries": REFRESH_LOCK_WAIT_MS / REFRESH_LOCK_RETRY_MS,
                    "factor": 1,
                    "minTimeout": REFRESH_LOCK_RETRY_MS,
                    "maxTimeout": REFRESH_LOCK_RETRY_MS,
                },
                # The default throws from a timer. A lost lock at worst lets two refreshes overlap.
                "onCompromised": lambda _error: None,
            },
        )
        try:
            return await fn()
        finally:
            with contextlib.suppress(Exception):
                await release()

    def tokens(self, name: str, serverUrl: str) -> Any:
        """The stored tokens of a server, for noticing sign-ins done by another process. Does not take over
        legacy state."""
        key, legacyKey = storeKeys(name, serverUrl)
        states: _StoredStates = self._backend.withLock(lambda current: LockResult(result=parseStates(current)))
        state = states.get(key) if states.get(key) is not None else states.get(legacyKey)
        return read_field(state, "tokens")

    def remove(self, name: str, serverUrl: str) -> bool:
        """Returns whether credentials were stored for the server. Removes legacy state the server would
        take over."""
        key, legacyKey = storeKeys(name, serverUrl)

        def removeStored(current: str | None) -> LockResult:
            states = parseStates(current)
            stored = key if key in states else (legacyKey if legacyKey in states else None)
            if not stored:
                return LockResult(result=False)
            del states[stored]
            return LockResult(result=True, next=serializeStates(states))

        return self._backend.withLock(removeStored)

    def _write(self, update: Callable[[_StoredStates], None]) -> None:
        def write(current: str | None) -> LockResult:
            states = parseStates(current)
            update(states)
            return LockResult(result=None, next=serializeStates(states))

        self._backend.withLock(write)


def timedFetch(fetch: McpFetch | None = None) -> McpFetch:
    """``fetch`` with ``OAUTH_REQUEST_TIMEOUT_MS`` per request, on top of the request's own signal."""
    resolved = fetch if fetch is not None else _fetch.fetch

    async def timed(input: Any, init: Any = None) -> _fetch.Response:
        timeout = AbortSignal.timeout(OAUTH_REQUEST_TIMEOUT_MS)
        signal = init.get("signal") if init is not None else None
        return await resolved(
            input,
            {**(init if init is not None else {}), "signal": AbortSignal.any([signal, timeout]) if signal else timeout},
        )

    return timed


def registeredRedirectUrls(client: OAuthClientInformationMixed | None) -> list[str]:
    return client["redirect_uris"] if client is not None and "redirect_uris" in client else []  # type: ignore[typeddict-item]


def callbackId(serverUrl: str) -> str:
    """12 characters identifying an MCP server URL in callback paths, computed like Codex does."""
    url = URL(serverUrl)
    url.hash = ""
    return base64.urlsafe_b64encode(hashlib.sha256(url.href.encode("utf-8")).digest()[:9]).decode("ascii")


def clientMetadataDocument(
    serverUrl: str, redirectUrl: str, metadata: AuthorizationServerMetadata | None
) -> OAuthClientMetadataDocument:
    """pi's Client ID Metadata Document, for ``clientRegistration: "cimd"``, chosen like Codex chooses its own.
    The configuration ensures the default callback path. Without the ``iss`` parameter in authorization
    responses (RFC 9207), the redirect URI and the document are specific to the MCP server, so a response
    cannot be mixed up with one from another authorization server (RFC 9700 section 4.4.2.2).
    """
    if (
        metadata is None
        or not metadata.get("client_id_metadata_document_supported")
        or "none" not in (metadata.get("token_endpoint_auth_methods_supported") or [])
    ):
        raise RuntimeError(
            'The authorization server does not support Client ID Metadata Documents for public clients; remove oauth.clientRegistration "cimd"'
        )
    if metadata.get("authorization_response_iss_parameter_supported"):
        return {"url": f"{CLIENT_METADATA_BASE_URL}/client.json", "redirectUrl": redirectUrl}
    id = callbackId(serverUrl)
    redirect = URL(redirectUrl)
    redirect.pathname = f"{CALLBACK_PATH}/{id}"
    return {"url": f"{CLIENT_METADATA_BASE_URL}/{id}/client.json", "redirectUrl": redirect.href}


def createProvider(
    serverUrl: str,
    store: McpOAuthStateStore,
    settings: McpOAuthSettings,
    redirectUrl: str,
    onRedirect: Callable[[URL], Any],
) -> McpOAuthProvider:
    clientName = settings.get("clientName")
    return McpOAuthProvider(
        defined({  # type: ignore[arg-type]
            "serverUrl": serverUrl,
            "redirectUrl": redirectUrl,
            "clientMetadata": {"client_name": clientName if clientName is not None else APP_NAME},
            "clientMetadataDocument": (
                (lambda metadata: clientMetadataDocument(serverUrl, redirectUrl, metadata))
                if settings.get("clientRegistration") == "cimd"
                else None
            ),
            "clientId": settings.get("clientId"),
            "clientSecret": settings.get("clientSecret"),
            "store": store,
            "onRedirect": onRedirect,
        })
    )


class McpAuthProvider(AuthProvider, Protocol):
    async def onUnauthorized(self, context: UnauthorizedContext) -> None: ...

    async def settled(self) -> None:
        """Resolves when no refresh is running, so shutdown does not drop rotated tokens before they are saved."""
        ...


class CreateMcpAuthProviderOptions(TypedDict):
    serverUrl: str
    store: McpOAuthServerStore
    settings: Callable[[], McpOAuthSettings]
    onChallenge: Callable[[OAuthChallenge], None]


class _McpAuthProvider:
    def __init__(self, options: CreateMcpAuthProviderOptions) -> None:
        self._options = options
        self._serverUrl, self._store = options["serverUrl"], options["store"]
        self._refreshing: asyncio.Future[None] | None = None

    def _refresh(
        self, staleToken: str | None, fetch: McpFetch | None = None, challenge: OAuthChallenge | None = None
    ) -> asyncio.Future[None]:
        """Replace ``staleToken``, the access token that expired or was rejected."""
        serverUrl, store = self._serverUrl, self._store

        async def refresh() -> None:
            state = await maybe_await(store.load())
            tokens = read_field(state, "tokens")
            if read_field(tokens, "access_token") != staleToken:
                return
            if not (tokens is not None and tokens.get("refresh_token")):
                raise McpOAuthAuthorizationRequiredError()
            settings = self._options["settings"]()
            redirectUrl = callbackSettings(settings).fixedRedirectUrl
            if redirectUrl is None:
                registered = registeredRedirectUrls(read_field(state, "clientInformation"))
                redirectUrl = registered[0] if len(registered) > 0 else FALLBACK_REDIRECT_URL
            provider = createProvider(serverUrl, store, settings, redirectUrl, lambda _url: None)
            # Refreshes the tokens, or reports that a new sign-in is needed. Not cancellable: a refresh the
            # server answered may have rotated the refresh token, so its answer must be saved.
            result = await authorizeMcp(
                provider,
                defined({  # type: ignore[arg-type]
                    "serverUrl": serverUrl,
                    "resourceMetadataUrl": challenge.get("resourceMetadataUrl") if challenge is not None else None,
                    "authorizationServerMetadataUrl": settings.get("authServerMetadataUrl"),
                    "scope": challenge.get("scope") if challenge is not None else None,
                    "fetch": timedFetch(fetch),
                }),
            )
            if result == "REDIRECT":
                raise McpOAuthAuthorizationRequiredError()

        if self._refreshing is None:
            # MISAKA fork: one task stands in for the shared promise, started at once as the promise is;
            # waiters shield it and its failure is always observed. `.finally` clears it once it settles.
            refreshing = startTask(store.withRefreshLock(refresh))

            def settle(done: asyncio.Future[None]) -> None:
                if not done.cancelled():
                    done.exception()
                if self._refreshing is done:
                    self._refreshing = None

            refreshing.add_done_callback(settle)
            self._refreshing = refreshing
        return self._refreshing

    async def token(self) -> str | None:
        store = self._store
        if self._refreshing is not None:
            with contextlib.suppress(Exception):
                await asyncio.shield(self._refreshing)
        state = await maybe_await(store.load())
        tokens = read_field(state, "tokens")
        token = read_field(tokens, "access_token")
        # `state?.tokensExpireAt !== undefined`: a stored null counts, and reads as 0.
        expired = (
            state is not None
            and "tokensExpireAt" in state
            and jsNumber(state["tokensExpireAt"]) - REFRESH_SKEW_MS <= time.time() * 1000
        )
        if not expired or not (tokens is not None and tokens.get("refresh_token")):
            return token
        # Failures fall through: the request goes out with the old token and a 401 decides what happens.
        with contextlib.suppress(Exception):
            await asyncio.shield(self._refresh(token))
        state = await maybe_await(store.load())
        tokens = read_field(state, "tokens")
        return read_field(tokens, "access_token")

    async def onUnauthorized(self, context: UnauthorizedContext) -> None:
        challenge = parseWwwAuthenticate(context["response"].headers.get("www-authenticate"))
        self._options["onChallenge"](challenge)
        # A refresh keeps the granted scope, so more scope needs a new sign-in.
        if challenge.get("error") == "insufficient_scope":
            raise McpOAuthAuthorizationRequiredError()
        await asyncio.shield(self._refresh(context.get("token"), context["fetch"], challenge))

    async def settled(self) -> None:
        if self._refreshing is not None:
            with contextlib.suppress(Exception):
                await asyncio.shield(self._refreshing)


def createMcpAuthProvider(options: CreateMcpAuthProviderOptions) -> McpAuthProvider:
    """Auth provider for MCP connections: sends the stored access token and refreshes it when it is about
    to expire or after a 401. Throws ``McpOAuthAuthorizationRequiredError`` when the user has to sign in,
    including when the server asks for more scope (``insufficient_scope``). ``onChallenge`` receives the
    server's ``WWW-Authenticate`` challenge so sign-in can use its resource metadata URL and scope.
    ``settings`` is only called when a refresh is needed, so a secret that fails to resolve fails the
    refresh instead of the whole connection setup.

    Many servers rotate refresh tokens, so two refreshes with the same refresh token lose the grant.
    Requests in this process share one refresh, and other processes are kept out by the store's
    refresh lock, held from reading the tokens to saving new ones. Tokens that changed meanwhile
    (another process refreshed them, or the user signed in) are used without refreshing.
    """
    return _McpAuthProvider(options)


class McpSignInPrompt(Protocol):
    def showAuthorizationUrl(self, url: URL) -> None:
        """Show the authorization URL to the user and open it in a browser."""
        ...

    def promptForRedirectUrl(self, signal: Any) -> Awaitable[str | None]:
        """Ask for the redirect URL from the browser address bar, for when the browser cannot reach the
        loopback callback (for example over SSH). Aborted once the callback arrives or the sign-in is
        cancelled. Resolves to ``None`` or an empty string when the user cancels."""
        ...


class McpSignInCancelledError(Exception):
    def __init__(self) -> None:
        super().__init__("Sign-in cancelled")
        self.message = "Sign-in cancelled"
        self.name = "McpSignInCancelledError"


class AuthorizationResponse(TypedDict):
    code: str
    iss: NotRequired[str]


def responseFromRedirectUrl(input: str, state: str, redirectUrl: URL) -> AuthorizationResponse:
    try:
        url = URL(jsTrim(input))
    except Exception:  # noqa: BLE001 - upstream `catch {}`
        raise RuntimeError("Expected the full redirect URL from the browser address bar") from None
    # A server-specific redirect URI tells authorization servers apart, so it must match exactly.
    if url.origin != redirectUrl.origin or url.pathname != redirectUrl.pathname:
        raise RuntimeError("The redirect URL does not match this sign-in's redirect URI")
    error = url.searchParams.get("error")
    if error:
        description = url.searchParams.get("error_description")
        raise RuntimeError(description if description is not None else error)
    if url.searchParams.get("state") != state:
        raise RuntimeError("The redirect URL belongs to a different sign-in")
    code = url.searchParams.get("code")
    if not code:
        raise RuntimeError("The redirect URL does not contain an authorization code")
    iss = url.searchParams.get("iss")
    response: AuthorizationResponse = {"code": code}
    if iss is not None:
        response["iss"] = iss
    return response


async def waitForAuthorizationResponse(
    callback: OAuthCallbackServer,
    state: str,
    redirectUrl: URL,
    prompt: McpSignInPrompt,
    signal: Any,
) -> AuthorizationResponse | OAuthCallback:
    """Wait for the browser callback or a pasted redirect URL, whichever comes first. Aborting ``signal``
    aborts the prompt, which then cancels the sign-in."""
    controller = AbortController()
    promptSignal = AbortSignal.any([controller.signal, signal]) if signal is not None else controller.signal
    fromBrowser = callback.waitForCallback(state, redirectUrl.pathname)
    asked = prompt.promptForRedirectUrl(promptSignal)

    async def fromUserInput() -> AuthorizationResponse:
        input = await maybe_await(asked)
        if not input or not jsTrim(input):
            raise McpSignInCancelledError()
        return responseFromRedirectUrl(input, state, redirectUrl)

    fromUser = startTask(fromUserInput())
    try:
        done, _pending = await asyncio.wait({fromBrowser, fromUser}, return_when=asyncio.FIRST_COMPLETED)
        # `Promise.race([fromBrowser, fromUser])`: of two settled at once, the first listed wins.
        winner = fromBrowser if fromBrowser in done else fromUser
        return winner.result()
    finally:
        # The losing side rejects once the prompt is aborted or the callback server closes.
        controller.abort()
        fromBrowser.add_done_callback(lambda future: future.cancelled() or future.exception())
        fromUser.add_done_callback(lambda future: future.cancelled() or future.exception())


async def listenForCallback(
    settings: CallbackSettings, extraPaths: list[str], port: int | None, required: bool
) -> OAuthCallbackServer:
    """Listen on ``port``, or on a free port when it is taken and not ``required``."""

    def renderPage(page: OAuthCallbackPage) -> str:
        if page["ok"]:
            return oauthSuccessHtml("Signed in to the MCP server. You may now close this page.")
        return oauthErrorHtml(page["message"], page.get("details"))

    options: dict[str, Any] = {
        "host": settings.host,
        "redirectHost": settings.redirectHost,
        "path": settings.path,
        "extraPaths": extraPaths,
        "renderPage": renderPage,
    }
    try:
        return await OAuthCallbackServer.listen({**options, "port": port if port is not None else 0})  # type: ignore[typeddict-item]
    except Exception:
        if required or port is None:
            raise
        return await OAuthCallbackServer.listen(options)  # type: ignore[arg-type]


class SignInMcpServerOptions(TypedDict):
    serverUrl: str
    store: McpOAuthStateStore
    settings: McpOAuthSettings
    challenge: NotRequired[OAuthChallenge | None]
    prompt: McpSignInPrompt
    signal: NotRequired[Any]


async def signInMcpServer(options: SignInMcpServerOptions) -> None:
    """Sign in to an MCP server. Uses the stored refresh token when possible; otherwise runs the browser
    authorization code flow. Tokens are saved to ``store``. Aborting ``signal`` stops the sign-in at any
    step with ``McpSignInCancelledError``; requests to the authorization server are also time-limited."""
    serverUrl, store, settings = options["serverUrl"], options["store"], options["settings"]
    signal = options.get("signal")
    if signal_aborted(signal):
        raise McpSignInCancelledError()
    stored = await maybe_await(store.load())
    challenge = options.get("challenge")
    stepUp = challenge is not None and challenge.get("error") == "insufficient_scope"
    callbackOptions = callbackSettings(settings)
    # Reuse the port of the registered redirect URI so the registered client stays valid.
    registeredUrls = registeredRedirectUrls(read_field(stored, "clientInformation"))
    registered = registeredUrls[0] if len(registeredUrls) > 0 else None
    preferredPort = (
        callbackOptions.port
        if callbackOptions.port is not None
        else ((jsNumber(URL(registered).port) or None) if registered else None)
    )
    cimd = settings.get("clientRegistration") == "cimd"
    callback = await listenForCallback(
        callbackOptions,
        # The redirect URI of a server-specific Client ID Metadata Document.
        [f"{CALLBACK_PATH}/{callbackId(serverUrl)}"] if cimd else [],
        preferredPort,
        callbackOptions.port is not None,
    )
    redirectUrl = callbackOptions.fixedRedirectUrl if callbackOptions.fixedRedirectUrl is not None else callback.redirectUrl
    try:
        if stored is not None:
            next: McpOAuthState = {**stored}
            # Every sign-in gets a fresh `state` parameter.
            next.pop("oauthState", None)
            # A registered client cannot use another redirect URI, and its tokens belong to it. A Client ID
            # Metadata Document is not stored, so with one, a stored client was registered before and is replaced.
            keepClient = settings.get("clientId") or (
                read_field(stored, "clientInformation") is None
                if cimd
                else redirectUrl in registeredRedirectUrls(read_field(stored, "clientInformation"))
            )
            if not keepClient:
                next.pop("clientInformation", None)
                next.pop("tokens", None)
                next.pop("tokensExpireAt", None)
            await maybe_await(store.save(next))

        authorizationUrl: URL | None = None

        def onRedirect(url: URL) -> None:
            nonlocal authorizationUrl
            authorizationUrl = url

        provider = createProvider(serverUrl, store, settings, redirectUrl, onRedirect)
        storedTokens = read_field(stored, "tokens")
        flow = defined({
            "serverUrl": serverUrl,
            "fetch": timedFetch(),
            "signal": signal,
            "resourceMetadataUrl": challenge.get("resourceMetadataUrl") if challenge is not None else None,
            "authorizationServerMetadataUrl": settings.get("authServerMetadataUrl"),
            # A server asking for more scope gets it on top of the configured scope and, since the challenge
            # may list only the missing scopes, on top of the scope granted so far.
            "scope": mergeScopes(
                settings.get("scope"),
                stepUpScope(
                    storedTokens.get("scope") if storedTokens is not None else None,
                    challenge.get("scope") if challenge is not None else None,
                )
                if stepUp
                else (challenge.get("scope") if challenge is not None else None),
            ),
        })
        # A refresh keeps the granted scope; a server asking for more needs the browser flow.
        skipRefresh = stepUp
        if await authorizeMcp(provider, {**flow, "skipRefresh": skipRefresh}) == "AUTHORIZED":  # type: ignore[arg-type]
            return
        if authorizationUrl is None:
            raise RuntimeError("OAuth flow did not produce an authorization URL")

        state = await provider.state()
        # The flow picks the redirect URI, which may be specific to the MCP server.
        redirectUri = authorizationUrl.searchParams.get("redirect_uri")
        authorizationRedirectUrl = URL(redirectUri if redirectUri is not None else redirectUrl)
        options["prompt"].showAuthorizationUrl(authorizationUrl)
        response = await waitForAuthorizationResponse(
            callback,
            state,
            authorizationRedirectUrl,
            options["prompt"],
            signal,
        )
        await authorizeMcp(
            provider,
            defined({**flow, "authorizationCode": response["code"], "iss": response.get("iss")}),  # type: ignore[arg-type]
        )
    except Exception:
        # Aborted requests fail with the signal's reason; report them as the cancellation they are.
        if signal_aborted(signal):
            raise McpSignInCancelledError() from None
        raise
    finally:
        await callback.close()


__all__ = [
    "CALLBACK_HOST",
    "CALLBACK_PATH",
    "CLIENT_METADATA_BASE_URL",
    "FALLBACK_REDIRECT_URL",
    "OAUTH_REQUEST_TIMEOUT_MS",
    "REFRESH_LOCK_RETRY_MS",
    "REFRESH_LOCK_STALE_MS",
    "REFRESH_LOCK_WAIT_MS",
    "REFRESH_SKEW_MS",
    "CreateMcpAuthProviderOptions",
    "McpAuthProvider",
    "McpOAuthCredentialStore",
    "McpOAuthServerStore",
    "McpOAuthSettings",
    "McpSignInCancelledError",
    "McpSignInPrompt",
    "SignInMcpServerOptions",
    "createMcpAuthProvider",
    "signInMcpServer",
]

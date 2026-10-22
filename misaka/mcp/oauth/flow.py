"""The OAuth authorization-code flow with PKCE: discovery, dynamic client registration, the
authorization redirect, code exchange, refresh and step-up, and the auth provider that drives it
from a transport's 401.

Translated from pi's ``packages/mcp/src/oauth/flow.ts``, whose header reads:

    Adapted from modelcontextprotocol/typescript-sdk v1.29.0 src/client/auth.ts.
    Copyright (c) 2024 Anthropic, PBC. Licensed under MIT; see LICENSES/.
    Modified to remove SDK/Zod dependencies and use WebCrypto for PKCE.

The MIT text is ``LICENSES/modelcontextprotocol-typescript-sdk.txt`` (indexed in THIRD_PARTY_NOTICES.md).

Provider members typed ``T | Promise<T>`` upstream are called through ``maybe_await``, so a provider
may be synchronous or asynchronous member by member.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol, Required, TypedDict

import httpx

from misaka.mcp.auth_provider import AuthProvider, McpFetch, UnauthorizedContext
from misaka.mcp.oauth.discovery import (
    discoverAuthorizationServerMetadata,
    discoverOAuthServerInfo,
    parseWwwAuthenticate,
    selectResource,
)
from misaka.mcp.oauth.errors import (
    McpOAuthAuthorizationRequiredError,
    OAuthError,
    OAuthInsecureEndpointError,
    OAuthIssuerMismatchError,
    OAuthRegistrationError,
)
from misaka.mcp.oauth.types import (
    AuthorizationServerMetadata,
    OAuthClientInformation,
    OAuthClientInformationFull,
    OAuthClientInformationMixed,
    OAuthClientMetadata,
    OAuthServerInfo,
    OAuthTokens,
    parseClientInformation,
    parseOAuthTokens,
)
from misaka.mcp.protocol.jsonrpc import isObject
from misaka.utils import whatwg_fetch as _fetch
from misaka.utils.js import (
    UNDEFINED,
    defined,
    jsonParse,
    jsonStringify,
    jsSplitWhitespace,
    jsString,
    jsTruthy,
    nullishCoalesce,
    toWellFormed,
)
from misaka.utils.values import maybe_await, signal_aborted
from misaka.utils.whatwg_url import URL, URLSearchParams

# Mutates `headers` (an `httpx.Headers`, where `headers[name] = value` is `Headers.set`) and `params`
# in place; may be asynchronous.
type AddClientAuthentication = Callable[
    [httpx.Headers, URLSearchParams, str | URL, AuthorizationServerMetadata | None], Any
]


class OAuthClientMetadataDocument(TypedDict):
    """A Client ID Metadata Document: an https URL used as `client_id`, and a redirect URI it lists."""

    url: str
    redirectUrl: str


class OAuthClientProvider(Protocol):
    """What the flow reads and writes through. Optional members, looked up with ``getattr``:
    ``clientMetadataDocument(metadata)`` -- the Client ID Metadata Document to identify as instead of
    registering dynamically, or ``None`` to register; called when no client information is stored, and the
    document is not stored (``metadata`` is ``None`` when the authorization server has none; check
    ``client_id_metadata_document_supported``) --, ``state()``, ``saveClientInformation(information)``,
    ``addClientAuthentication``, ``invalidateCredentials(kind)`` with ``kind`` one of ``"all"``,
    ``"client"``, ``"tokens"``, ``"verifier"`` and ``"discovery"``, ``saveDiscoveryState(state)``
    and ``discoveryState()``."""

    @property
    def redirectUrl(self) -> str | URL: ...

    @property
    def clientMetadata(self) -> OAuthClientMetadata: ...

    def clientInformation(
        self,
    ) -> OAuthClientInformationMixed | None | Awaitable[OAuthClientInformationMixed | None]: ...

    def tokens(self) -> OAuthTokens | None | Awaitable[OAuthTokens | None]: ...

    def saveTokens(self, tokens: OAuthTokens) -> None | Awaitable[None]: ...

    def redirectToAuthorization(self, url: URL) -> None | Awaitable[None]: ...

    def saveCodeVerifier(self, verifier: str) -> None | Awaitable[None]: ...

    def codeVerifier(self) -> str | Awaitable[str]: ...


class OAuthFlowOptions(TypedDict, total=False):
    serverUrl: Required[str | URL]
    authorizationCode: str
    # `iss` parameter of the authorization response that delivered `authorizationCode` (RFC 9207).
    iss: str
    scope: str
    resourceMetadataUrl: URL
    # Authorization server metadata document to use instead of discovery, for servers that advertise a
    # wrong authorization server or none. It is trusted as configured. Must use https, except on loopback.
    authorizationServerMetadataUrl: URL
    fetch: McpFetch
    # Aborts every request of the flow. Requests have no time limit of their own; combine with a timeout as needed.
    signal: Any
    skipIssuerValidation: bool
    # Go straight to the authorization redirect instead of refreshing stored tokens, for example when the
    # server asks for scopes the current grant lacks (a refresh keeps the old scope).
    skipRefresh: bool


type OAuthFlowResult = Literal["AUTHORIZED", "REDIRECT"]
type _ClientAuthMethod = Literal["client_secret_basic", "client_secret_post", "none"]


class TokenRequestOptions(TypedDict, total=False):
    metadata: AuthorizationServerMetadata
    clientInformation: Required[OAuthClientInformationMixed]
    resource: str
    addClientAuthentication: AddClientAuthentication
    fetch: McpFetch
    signal: Any


class StartAuthorizationOptions(TypedDict, total=False):
    metadata: AuthorizationServerMetadata
    clientInformation: Required[OAuthClientInformationMixed]
    redirectUrl: Required[str | URL]
    scope: str
    state: str
    resource: str


class StartAuthorizationResult(TypedDict):
    authorizationUrl: URL
    codeVerifier: str


class RegisterClientOptions(TypedDict, total=False):
    metadata: AuthorizationServerMetadata
    clientMetadata: Required[OAuthClientMetadata]
    scope: str
    fetch: McpFetch
    signal: Any


class ExchangeAuthorizationCodeOptions(TokenRequestOptions, total=False):
    code: Required[str]
    codeVerifier: Required[str]
    redirectUrl: Required[str | URL]


class RefreshAuthorizationOptions(TokenRequestOptions, total=False):
    refreshToken: Required[str]


def loopback(hostname: str) -> bool:
    return hostname == "localhost" or hostname == "127.0.0.1" or hostname == "[::1]" or hostname == "::1"


def applicationType(redirectUris: list[str]) -> Literal["native", "web"]:
    """The OpenID Connect `application_type` for `redirect_uris` (MCP SEP-837). Without one, OpenID Connect servers
    assume `web`, which rejects http loopback redirect URIs. Loopback hosts and custom schemes are native (RFC 8252).
    """

    def native(uri: str) -> bool:
        if not URL.canParse(uri):
            return False
        url = URL(uri)
        return (url.protocol != "http:" and url.protocol != "https:") or loopback(url.hostname)

    return "native" if any(native(uri) for uri in redirectUris) else "web"


def secureEndpoint(value: str | URL) -> URL:
    url = URL(value)
    if url.protocol != "https:" and not loopback(url.hostname):
        raise OAuthInsecureEndpointError(url.href)
    return url


def selectClientAuthMethod(information: OAuthClientInformationMixed, supported: list[str]) -> _ClientAuthMethod:
    hinted = information.get("token_endpoint_auth_method") if "token_endpoint_auth_method" in information else None
    if (
        hinted
        and hinted in ("client_secret_basic", "client_secret_post", "none")
        and (len(supported) == 0 or hinted in supported)
    ):
        return hinted  # type: ignore[return-value]
    if len(supported) == 0:
        return "client_secret_basic" if information.get("client_secret") else "none"
    if information.get("client_secret") and "client_secret_basic" in supported:
        return "client_secret_basic"
    if information.get("client_secret") and "client_secret_post" in supported:
        return "client_secret_post"
    if "none" in supported:
        return "none"
    return "client_secret_post" if information.get("client_secret") else "none"


def applyClientAuthentication(
    method: _ClientAuthMethod,
    information: OAuthClientInformation,
    headers: httpx.Headers,
    params: URLSearchParams,
) -> None:
    if method == "client_secret_basic":
        if not information.get("client_secret"):
            raise RuntimeError("client_secret_basic requires a client secret")
        credentials = toWellFormed(f"{information['client_id']}:{information['client_secret']}").encode("utf-8")
        headers["Authorization"] = f"Basic {base64.b64encode(credentials).decode('ascii')}"
    else:
        params.set("client_id", information["client_id"])
        if method == "client_secret_post" and information.get("client_secret"):
            params.set("client_secret", information["client_secret"])


async def pkce() -> dict[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return {"verifier": verifier, "challenge": base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")}


async def startAuthorization(
    authorizationServerUrl: str | URL, options: StartAuthorizationOptions
) -> StartAuthorizationResult:
    metadata = options.get("metadata")
    if metadata is not None:
        responseTypes = metadata.get("response_types_supported")
        if responseTypes is None:
            # `metadata.response_types_supported.includes` throws a TypeError when the field is missing.
            raise TypeError("Cannot read properties of undefined (reading 'includes')")
        if "code" not in responseTypes:
            raise RuntimeError("Authorization server does not support authorization codes")
    if (
        metadata is not None
        and jsTruthy(metadata.get("code_challenge_methods_supported"))
        and "S256" not in metadata["code_challenge_methods_supported"]
    ):
        raise RuntimeError("Authorization server does not support PKCE S256")
    authorizationEndpoint = metadata.get("authorization_endpoint") if metadata is not None else None
    url = URL(authorizationEndpoint if authorizationEndpoint is not None else URL("/authorize", authorizationServerUrl))
    generated = await pkce()
    verifier, challenge = generated["verifier"], generated["challenge"]
    url.searchParams.set("response_type", "code")
    url.searchParams.set("client_id", options["clientInformation"]["client_id"])
    url.searchParams.set("code_challenge", challenge)
    url.searchParams.set("code_challenge_method", "S256")
    url.searchParams.set("redirect_uri", jsString(options["redirectUrl"]))
    if options.get("state"):
        url.searchParams.set("state", options["state"])
    scope = options.get("scope")
    if scope:
        url.searchParams.set("scope", scope)
    if scope is not None and "offline_access" in jsSplitWhitespace(scope):
        url.searchParams.set("prompt", "consent")
    if options.get("resource"):
        url.searchParams.set("resource", options["resource"])
    return {"authorizationUrl": url, "codeVerifier": verifier}


async def tokenRequest(
    authorizationServerUrl: str | URL, options: TokenRequestOptions, params: URLSearchParams
) -> OAuthTokens:
    metadata = options.get("metadata")
    tokenEndpoint = metadata.get("token_endpoint") if metadata is not None else None
    url = secureEndpoint(tokenEndpoint if tokenEndpoint is not None else URL("/token", authorizationServerUrl))
    headers = httpx.Headers({"Accept": "application/json", "content-type": "application/x-www-form-urlencoded"})
    if options.get("resource"):
        params.set("resource", options["resource"])
    addClientAuthentication = options.get("addClientAuthentication")
    if addClientAuthentication is not None:
        await maybe_await(addClientAuthentication(headers, params, url, metadata))
    else:
        supported = metadata.get("token_endpoint_auth_methods_supported") if metadata is not None else None
        applyClientAuthentication(
            selectClientAuthMethod(options["clientInformation"], supported if supported is not None else []),
            options["clientInformation"],
            headers,
            params,
        )
    fetch = options.get("fetch")
    response = await (fetch if fetch is not None else _fetch.fetch)(
        url, defined({"method": "POST", "headers": headers, "body": params, "signal": options.get("signal")})  # type: ignore[arg-type]
    )
    text = await response.text()
    value: Any = None
    try:
        value = jsonParse(text)
    except Exception:  # noqa: BLE001, S110 - upstream `catch {}`
        pass
    # Servers may report OAuth errors with any status, so check the body before the status.
    if isObject(value) and isinstance(value.get("error"), str):
        raise OAuthError(
            value["error"],
            value["error_description"] if isinstance(value.get("error_description"), str) else value["error"],
            value["error_uri"] if isinstance(value.get("error_uri"), str) else None,
        )
    if not response.ok:
        raise OAuthError("server_error", f"HTTP {response.status}: {text}")
    return parseOAuthTokens(value)


async def registerClient(
    authorizationServerUrl: str | URL, options: RegisterClientOptions
) -> OAuthClientInformationFull:
    metadata = options.get("metadata")
    endpoint = metadata.get("registration_endpoint") if metadata is not None else None
    if metadata is not None and not endpoint:
        raise RuntimeError("Authorization server does not support dynamic client registration")
    fetch = options.get("fetch")
    response = await (fetch if fetch is not None else _fetch.fetch)(
        URL(endpoint if endpoint else URL("/register", authorizationServerUrl)),
        defined({  # type: ignore[arg-type]
            "method": "POST",
            "headers": {"Accept": "application/json", "content-type": "application/json"},
            "body": jsonStringify({
                **options["clientMetadata"],
                "application_type": (
                    options["clientMetadata"]["application_type"]
                    if options["clientMetadata"].get("application_type") is not None
                    else applicationType(options["clientMetadata"]["redirect_uris"])
                ),
                **({"scope": options["scope"]} if options.get("scope") else {}),
            }),
            "signal": options.get("signal"),
        }),
    )
    if not response.ok:
        raise OAuthRegistrationError(response.status, await response.text())
    return parseClientInformation(await response.json())


async def exchangeAuthorizationCode(
    authorizationServerUrl: str | URL, options: ExchangeAuthorizationCodeOptions
) -> OAuthTokens:
    return await tokenRequest(
        authorizationServerUrl,
        options,
        URLSearchParams([
            ("grant_type", "authorization_code"),
            ("code", options["code"]),
            ("code_verifier", options["codeVerifier"]),
            ("redirect_uri", jsString(options["redirectUrl"])),
        ]),
    )


async def refreshAuthorization(
    authorizationServerUrl: str | URL, options: RefreshAuthorizationOptions
) -> OAuthTokens:
    tokens = await tokenRequest(
        authorizationServerUrl,
        options,
        URLSearchParams([("grant_type", "refresh_token"), ("refresh_token", options["refreshToken"])]),
    )
    return {"refresh_token": options["refreshToken"], **tokens}


def withScope(tokens: OAuthTokens, scope: str | None) -> OAuthTokens:
    return {**tokens, "scope": scope} if "scope" not in tokens and scope else tokens


def stepUpScope(granted: str | None, challenged: str | None) -> str | None:
    """Scopes for a step-up authorization: the challenged scopes plus the ones granted so far, since a
    challenge may list only the missing scopes and a token with just those would lose access the old
    one had (SEP-2350). Without challenged scopes, ``None`` lets the flow pick its default."""
    if not challenged:
        return None
    scopes = [item for scope in (granted, challenged) for item in (jsSplitWhitespace(scope) if scope else []) if item]
    return " ".join(dict.fromkeys(scopes))


async def runFlow(provider: OAuthClientProvider, options: OAuthFlowOptions) -> OAuthFlowResult:
    authorizationServerMetadataUrl = options.get("authorizationServerMetadataUrl")
    metadataUrl = secureEndpoint(authorizationServerMetadataUrl) if authorizationServerMetadataUrl else None
    # With a configured metadata URL, discovery is not cached, so changing the URL applies at once.
    discoveryState = getattr(provider, "discoveryState", None)
    cached = None if metadataUrl else (await maybe_await(discoveryState()) if discoveryState is not None else None)
    discovered: OAuthServerInfo
    if cached is not None and cached.get("authorizationServerUrl"):
        authorizationServerMetadata = cached.get("authorizationServerMetadata")
        if authorizationServerMetadata is None:
            authorizationServerMetadata = await discoverAuthorizationServerMetadata(
                cached["authorizationServerUrl"],
                defined({  # type: ignore[arg-type]
                    "fetch": options.get("fetch"),
                    "skipIssuerValidation": options.get("skipIssuerValidation"),
                    "signal": options.get("signal"),
                }),
            )
        discovered = defined({  # type: ignore[assignment]
            "authorizationServerUrl": cached["authorizationServerUrl"],
            "authorizationServerMetadata": authorizationServerMetadata,
            "resourceMetadata": cached.get("resourceMetadata"),
        })
    else:
        discovered = await discoverOAuthServerInfo(
            options["serverUrl"],
            defined({  # type: ignore[arg-type]
                "resourceMetadataUrl": options.get("resourceMetadataUrl"),
                "authorizationServerMetadataUrl": metadataUrl,
                "fetch": options.get("fetch"),
                "skipIssuerValidation": options.get("skipIssuerValidation"),
                "signal": options.get("signal"),
            }),
        )
    if not metadataUrl:
        saveDiscoveryState = getattr(provider, "saveDiscoveryState", None)
        if saveDiscoveryState is not None:
            resourceMetadataUrl = options.get("resourceMetadataUrl")
            await maybe_await(
                saveDiscoveryState({
                    **discovered,
                    **({"resourceMetadataUrl": resourceMetadataUrl.href} if resourceMetadataUrl else {}),
                })
            )
    metadata = discovered.get("authorizationServerMetadata")
    resourceMetadata = discovered.get("resourceMetadata")
    resource = selectResource(options["serverUrl"], resourceMetadata)
    # `||`, not `??`: an empty scope (for example from `scopes_supported: []`) falls through to the next source.
    scopesSupported = resourceMetadata.get("scopes_supported") if resourceMetadata is not None else None
    scope = (
        options.get("scope")
        or (" ".join(scopesSupported) if scopesSupported is not None else None)
        or provider.clientMetadata.get("scope")
    )
    stored = await maybe_await(provider.clientInformation())
    # MISAKA fork: an optional member is looked up with `getattr`, and a provider may hold it as None.
    clientMetadataDocument = getattr(provider, "clientMetadataDocument", None)
    clientDocument = (
        None if stored is not None else (clientMetadataDocument(metadata) if clientMetadataDocument else None)
    )
    if clientDocument is not None:
        url = URL(clientDocument["url"])
        if url.protocol != "https:" or url.pathname == "/":
            raise RuntimeError("Invalid OAuth client metadata URL")
    client = stored if stored is not None else ({"client_id": clientDocument["url"]} if clientDocument else None)
    if client is None:
        if options.get("authorizationCode"):
            raise RuntimeError("OAuth client information is missing during code exchange")
        saveClientInformation = getattr(provider, "saveClientInformation", None)
        if saveClientInformation is None:
            raise RuntimeError("OAuth client information cannot be persisted")
        client = await registerClient(
            discovered["authorizationServerUrl"],
            defined({  # type: ignore[arg-type]
                "metadata": metadata,
                "clientMetadata": provider.clientMetadata,
                "scope": scope,
                "fetch": options.get("fetch"),
                "signal": options.get("signal"),
            }),
        )
        await maybe_await(saveClientInformation(client))
    # The document's redirect URI may differ from the provider's, for example by a server-specific path.
    redirectUrl = nullishCoalesce(
        clientDocument.get("redirectUrl") if clientDocument is not None else None, provider.redirectUrl
    )
    tokenOptions: TokenRequestOptions = defined({  # type: ignore[assignment]
        "metadata": metadata,
        "clientInformation": client,
        "resource": resource,
        "addClientAuthentication": getattr(provider, "addClientAuthentication", None),
        "fetch": options.get("fetch"),
        "signal": options.get("signal"),
    })
    if options.get("authorizationCode"):
        # RFC 9207: never send a code from another authorization server to this one. Without metadata there
        # is no issuer to compare with, so any `iss` is rejected.
        iss = options.get("iss", UNDEFINED)
        if iss is not UNDEFINED or (metadata is not None and metadata.get("authorization_response_iss_parameter_supported")):
            issuer = metadata.get("issuer", UNDEFINED) if metadata is not None else UNDEFINED
            if iss != issuer:
                raise OAuthIssuerMismatchError(
                    None if issuer is UNDEFINED else issuer, None if iss is UNDEFINED else iss
                )
        tokens = await exchangeAuthorizationCode(
            discovered["authorizationServerUrl"],
            {
                **tokenOptions,
                "code": options["authorizationCode"],
                "codeVerifier": await maybe_await(provider.codeVerifier()),
                "redirectUrl": redirectUrl,
            },
        )
        # A response without `scope` grants the requested scope (RFC 6749 §5.1). Recorded so a step-up can
        # keep it. Callers pass the options of the authorization request, so `scope` is what was requested.
        await maybe_await(provider.saveTokens(withScope(tokens, scope)))
        return "AUTHORIZED"
    existing = None if options.get("skipRefresh") else await maybe_await(provider.tokens())
    if existing is not None and existing.get("refresh_token"):
        try:
            tokens = await refreshAuthorization(
                discovered["authorizationServerUrl"],
                {**tokenOptions, "refreshToken": existing["refresh_token"]},
            )
            # A refresh without `scope` keeps the scope of the grant (RFC 6749 §6).
            await maybe_await(provider.saveTokens(withScope(tokens, existing.get("scope"))))
            return "AUTHORIZED"
        except Exception as error:
            if signal_aborted(options.get("signal")) or isinstance(error, OAuthInsecureEndpointError):
                raise
            if isinstance(error, OAuthError) and error.code != "server_error":
                raise
            # Any other failure falls through to the authorization redirect.
    state = getattr(provider, "state", None)
    authorization = await startAuthorization(
        discovered["authorizationServerUrl"],
        defined({  # type: ignore[arg-type]
            "metadata": metadata,
            "clientInformation": client,
            "redirectUrl": redirectUrl,
            "scope": scope,
            "state": await maybe_await(state()) if state is not None else None,
            "resource": resource,
        }),
    )
    await maybe_await(provider.saveCodeVerifier(authorization["codeVerifier"]))
    await maybe_await(provider.redirectToAuthorization(authorization["authorizationUrl"]))
    return "REDIRECT"


async def authorizeMcp(provider: OAuthClientProvider, options: OAuthFlowOptions) -> OAuthFlowResult:
    try:
        return await runFlow(provider, options)
    except OAuthError as error:
        invalidateCredentials = getattr(provider, "invalidateCredentials", None)
        if error.code in ("invalid_client", "unauthorized_client"):
            if invalidateCredentials is not None:
                await maybe_await(invalidateCredentials("all"))
            return await runFlow(provider, options)
        if error.code == "invalid_grant":
            if invalidateCredentials is not None:
                await maybe_await(invalidateCredentials("tokens"))
            return await runFlow(provider, options)
        raise


class _OAuthAuthProvider:
    """The ``AuthProvider`` ``adaptOAuthProvider`` returns. MISAKA fork: a class for upstream's object literal
    over closures, so the shared refresh (``inFlight``) has a home; ``ext/oauth.py``'s provider is built alike."""

    def __init__(self, provider: OAuthClientProvider) -> None:
        self._provider = provider
        self._inFlight: asyncio.Task[None] | None = None

    async def token(self) -> str | None:
        tokens = await maybe_await(self._provider.tokens())
        return tokens.get("access_token") if tokens is not None else None

    async def onUnauthorized(self, context: UnauthorizedContext) -> None:
        provider = self._provider
        challenge = parseWwwAuthenticate(context["response"].headers.get("www-authenticate"))
        insufficientScope = challenge.get("error") == "insufficient_scope"
        if not insufficientScope and self._inFlight is None and context.get("token") is not None:
            tokens = await maybe_await(provider.tokens())
            current = tokens.get("access_token") if tokens is not None else None
            if current is not None and current != context["token"]:
                return
        if self._inFlight is None:

            async def run() -> None:
                try:
                    granted = await maybe_await(provider.tokens()) if insufficientScope else None
                    result = await authorizeMcp(
                        provider,
                        defined({  # type: ignore[arg-type]
                            "serverUrl": context["serverUrl"],
                            "resourceMetadataUrl": challenge.get("resourceMetadataUrl"),
                            "scope": stepUpScope(granted.get("scope") if granted is not None else None, challenge.get("scope"))
                            if insufficientScope
                            else challenge.get("scope"),
                            "fetch": context["fetch"],
                            "skipRefresh": insufficientScope,
                        }),
                    )
                    if result == "REDIRECT":
                        raise McpOAuthAuthorizationRequiredError()
                finally:
                    self._inFlight = None

            # MISAKA fork: one task stands in for the shared promise; waiters shield it, so a cancelled
            # waiter does not cancel the refresh the others share, and its failure is always observed.
            self._inFlight = asyncio.ensure_future(run())
            self._inFlight.add_done_callback(lambda done: done.cancelled() or done.exception())
        await asyncio.shield(self._inFlight)


def adaptOAuthProvider(provider: OAuthClientProvider) -> AuthProvider:
    """Auth provider for ``StreamableHttpTransport``. After a 401 it refreshes the tokens, or raises
    ``McpOAuthAuthorizationRequiredError`` when the user has to authorize (again). Concurrent 401s share
    one refresh, and a request whose token was already replaced is just retried: with rotating refresh
    tokens, a second refresh with the old refresh token would fail and discard the new grant."""
    return _OAuthAuthProvider(provider)


__all__ = [
    "AddClientAuthentication",
    "ExchangeAuthorizationCodeOptions",
    "OAuthClientMetadataDocument",
    "OAuthClientProvider",
    "OAuthFlowOptions",
    "OAuthFlowResult",
    "RefreshAuthorizationOptions",
    "RegisterClientOptions",
    "StartAuthorizationOptions",
    "StartAuthorizationResult",
    "TokenRequestOptions",
    "adaptOAuthProvider",
    "authorizeMcp",
    "exchangeAuthorizationCode",
    "refreshAuthorization",
    "registerClient",
    "startAuthorization",
    "stepUpScope",
]

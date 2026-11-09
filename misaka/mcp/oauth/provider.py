"""The default stateful OAuth client provider: one MCP server URL, credentials in a pluggable store.

Translated from pi's ``packages/mcp/src/oauth/provider.ts``.
"""

from __future__ import annotations

import asyncio
import copy
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal, Protocol, Required, TypedDict, cast

from misaka.mcp.oauth.types import (
    AuthorizationServerMetadata,
    OAuthClientInformationMixed,
    OAuthClientMetadata,
    OAuthDiscoveryState,
    OAuthTokens,
)
from misaka.utils.js import jsString
from misaka.utils.values import maybe_await
from misaka.utils.whatwg_url import URL

if TYPE_CHECKING:
    from misaka.mcp.oauth.flow import OAuthClientMetadataDocument


class McpOAuthState(TypedDict, total=False):
    serverUrl: Required[str]
    clientInformation: OAuthClientInformationMixed
    tokens: OAuthTokens
    # When the access token expires, in milliseconds since the epoch, from `expires_in` at the time it was saved.
    tokensExpireAt: int | float
    codeVerifier: str
    oauthState: str
    discovery: OAuthDiscoveryState


class McpOAuthStateStore(Protocol):
    def load(self) -> McpOAuthState | None | Awaitable[McpOAuthState | None]: ...

    def save(self, state: McpOAuthState) -> None | Awaitable[None]: ...


class McpOAuthProviderOptions(TypedDict, total=False):
    serverUrl: Required[str | URL]
    redirectUrl: Required[str | URL]
    # `OAuthClientMetadata` whose `redirect_uris` may be left out (it defaults to `[redirectUrl]`).
    clientMetadata: Required[dict[str, Any]]
    # See `OAuthClientProvider.clientMetadataDocument`.
    clientMetadataDocument: Callable[[AuthorizationServerMetadata | None], OAuthClientMetadataDocument | None]
    clientId: str
    clientSecret: str
    store: McpOAuthStateStore
    onRedirect: Required[Callable[[URL], Any]]


class MemoryOAuthStateStore:
    def __init__(self) -> None:
        self._value: McpOAuthState | None = None

    def load(self) -> McpOAuthState | None:
        return None if self._value is None else copy.deepcopy(self._value)

    def save(self, state: McpOAuthState) -> None:
        self._value = copy.deepcopy(state)


class McpOAuthProvider:
    """Default stateful provider for one exact MCP server URL. Applications inject durable storage if needed."""

    def __init__(self, options: McpOAuthProviderOptions) -> None:
        self._serverUrl = jsString(URL(options["serverUrl"]))
        self.redirectUrl: str = jsString(options["redirectUrl"])
        clientMetadata = options["clientMetadata"]
        self.clientMetadata: OAuthClientMetadata = cast(OAuthClientMetadata, {
            **clientMetadata,
            "redirect_uris": (
                clientMetadata["redirect_uris"] if clientMetadata.get("redirect_uris") is not None else [self.redirectUrl]
            ),
            "grant_types": (
                clientMetadata["grant_types"]
                if clientMetadata.get("grant_types") is not None
                else ["authorization_code", "refresh_token"]
            ),
            "response_types": (
                clientMetadata["response_types"] if clientMetadata.get("response_types") is not None else ["code"]
            ),
            "token_endpoint_auth_method": (
                clientMetadata["token_endpoint_auth_method"]
                if clientMetadata.get("token_endpoint_auth_method") is not None
                else ("client_secret_post" if options.get("clientSecret") else "none")
            ),
        })
        self.clientMetadataDocument: (
            Callable[[AuthorizationServerMetadata | None], OAuthClientMetadataDocument | None] | None
        ) = options.get("clientMetadataDocument")
        self._configuredClient: OAuthClientInformationMixed | None = (
            cast(OAuthClientInformationMixed, {
                "client_id": options["clientId"],
                **({"client_secret": options["clientSecret"]} if options.get("clientSecret") else {}),
            })
            if options.get("clientId")
            else None
        )
        store = options.get("store")
        self._store: McpOAuthStateStore = store if store is not None else MemoryOAuthStateStore()
        self._onRedirect = options["onRedirect"]
        # MISAKA fork: `Promise.resolve()` starts the write chain; a future needs a running loop, so
        # `None` stands for "nothing pending".
        self._writes: asyncio.Future[None] | None = None

    async def state(self) -> str:
        existing = (await self._load()).get("oauthState")
        if existing:
            return existing
        state = secrets.token_bytes(32).hex()
        await self._update(lambda value: {**value, "oauthState": state})
        return state

    async def clientInformation(self) -> OAuthClientInformationMixed | None:
        if self._configuredClient is not None:
            return self._configuredClient
        return (await self._load()).get("clientInformation")

    async def saveClientInformation(self, information: OAuthClientInformationMixed) -> None:
        if self._configuredClient is not None:
            return
        await self._update(lambda value: {**value, "clientInformation": information})

    async def tokens(self) -> OAuthTokens | None:
        return (await self._load()).get("tokens")

    async def saveTokens(self, tokens: OAuthTokens) -> None:
        expiresAt = (
            None
            if "expires_in" not in tokens
            # `null * 1000` is 0.
            else int(time.time() * 1000) + (tokens["expires_in"] if tokens["expires_in"] is not None else 0) * 1000
        )

        def update(value: McpOAuthState) -> McpOAuthState:
            next: McpOAuthState = {**value, "tokens": tokens}
            if expiresAt is None:
                next.pop("tokensExpireAt", None)
            else:
                next["tokensExpireAt"] = expiresAt
            return next

        await self._update(update)

    async def redirectToAuthorization(self, url: URL) -> None:
        await maybe_await(self._onRedirect(url))

    async def saveCodeVerifier(self, verifier: str) -> None:
        await self._update(lambda value: {**value, "codeVerifier": verifier})

    async def codeVerifier(self) -> str:
        verifier = (await self._load()).get("codeVerifier")
        if not verifier:
            raise RuntimeError("No OAuth PKCE code verifier is stored")
        return verifier

    async def invalidateCredentials(self, kind: Literal["all", "client", "tokens", "verifier", "discovery"]) -> None:
        def update(value: McpOAuthState) -> McpOAuthState:
            next: McpOAuthState = {**value}
            if kind == "all" or kind == "client":
                next.pop("clientInformation", None)
            if kind == "all" or kind == "tokens":
                next.pop("tokens", None)
                next.pop("tokensExpireAt", None)
            if kind == "all" or kind == "verifier":
                next.pop("codeVerifier", None)
            if kind == "all" or kind == "discovery":
                next.pop("discovery", None)
            if kind == "all":
                next.pop("oauthState", None)
            return next

        await self._update(update)

    async def saveDiscoveryState(self, discovery: OAuthDiscoveryState) -> None:
        await self._update(lambda value: {**value, "discovery": discovery})

    async def discoveryState(self) -> OAuthDiscoveryState | None:
        return (await self._load()).get("discovery")

    async def _load(self) -> McpOAuthState:
        if self._writes is not None:
            await asyncio.shield(self._writes)
        return self._own(await maybe_await(self._store.load()))

    async def _update(self, update: Callable[[McpOAuthState], McpOAuthState]) -> None:
        previous = self._writes

        async def write() -> None:
            if previous is not None:
                await asyncio.shield(previous)
            await maybe_await(self._store.save(update(self._own(await maybe_await(self._store.load())))))

        # MISAKA fork: each write is a task chained on the previous one and shielded from its caller,
        # as a promise keeps running when nobody awaits it; its failure is observed either way.
        self._writes = asyncio.ensure_future(write())
        self._writes.add_done_callback(lambda done: done.cancelled() or done.exception())
        await asyncio.shield(self._writes)

    def _own(self, state: McpOAuthState | None) -> McpOAuthState:
        """Stored state for another server URL is ignored so credentials never leak across servers."""
        if state is not None and state.get("serverUrl") == self._serverUrl:
            return state
        return {"serverUrl": self._serverUrl}


__all__ = [
    "McpOAuthProvider",
    "McpOAuthProviderOptions",
    "McpOAuthState",
    "McpOAuthStateStore",
    "MemoryOAuthStateStore",
]

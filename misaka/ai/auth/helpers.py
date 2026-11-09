"""Translated from pi's ``packages/ai/src/auth/helpers.ts`` (v1.1.0).

MISAKA fork: ``lazyOAuth`` takes keyword parameters for upstream's single ``input`` object, and the shared load is
shielded, so a first caller whose task is cancelled cannot cancel the load every later caller waits on (a JavaScript
promise cannot be cancelled by an awaiter).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, NotRequired, TypedDict

from misaka.ai.auth.types import (
    ApiKeyAuth,
    ApiKeyCredential,
    AuthResult,
    LoginOptions,
    ModelAuth,
    OAuthAuth,
    OAuthCredential,
    ProviderAuthInteraction,
    SecretPrompt,
)
from misaka.utils.js import throwIfAborted


def envApiKeyAuth(name: str, envVars: list[str] | tuple[str, ...]) -> ApiKeyAuth:
    """Standard api-key auth: a stored credential key wins, otherwise the first
    set env var resolves. Includes a `login` that prompts for the key.
    Providers with non-standard resolution (provider env, ambient files, IAM)
    write their own `ApiKeyAuth`.
    """

    async def login(interaction: ProviderAuthInteraction) -> ApiKeyCredential:
        throwIfAborted(interaction.signal)
        key = await interaction.prompt(SecretPrompt(message=f"Enter {name}"))
        throwIfAborted(interaction.signal)
        return ApiKeyCredential(type="api_key", key=key)

    async def resolve(*, ctx: Any, credential: ApiKeyCredential | None = None, signal: Any) -> AuthResult | None:
        throwIfAborted(signal)
        if credential is not None and credential.key:
            return AuthResult(auth=ModelAuth(apiKey=credential.key), env=credential.env, source="stored credential")
        for envVar in envVars:
            value = await ctx.env(envVar)
            throwIfAborted(signal)
            if value:
                return AuthResult(auth=ModelAuth(apiKey=value), source=envVar)
        return None

    return ApiKeyAuth(name=name, login=login, resolve=resolve)


class LazyOAuthInput(TypedDict):
    """The object ``lazyOAuth`` takes."""

    name: str
    isSubscription: NotRequired[bool]
    loginLabel: NotRequired[str]
    load: Callable[[], Awaitable[OAuthAuth]]


def lazyOAuth(input: LazyOAuthInput) -> OAuthAuth:
    """Wraps a dynamically imported `OAuthAuth` so provider definitions can
    advertise OAuth without importing the implementation. The flow loads on
    first `login`/`refresh`/`toAuth` call; callers keep Node-only flow code out
    of bundles by loading through a bundler-opaque dynamic import (variable
    specifier, see the bedrock lazy wrapper).
    """
    promise: asyncio.Future[OAuthAuth] | None = None

    def loaded() -> Awaitable[OAuthAuth]:
        nonlocal promise
        if promise is None:
            promise = asyncio.ensure_future(input["load"]())
        return asyncio.shield(promise)

    async def login(interaction: ProviderAuthInteraction, options: LoginOptions | None = None) -> OAuthCredential:
        return await (await loaded()).login(interaction, options)

    async def refresh(credential: OAuthCredential, signal: Any) -> OAuthCredential:
        return await (await loaded()).refresh(credential, signal)

    async def toAuth(credential: OAuthCredential) -> ModelAuth:
        return await (await loaded()).toAuth(credential)

    return OAuthAuth(
        name=input["name"],
        isSubscription=input.get("isSubscription"),
        loginLabel=input.get("loginLabel"),
        login=login,
        refresh=refresh,
        toAuth=toAuth,
    )


__all__ = ["envApiKeyAuth", "lazyOAuth"]

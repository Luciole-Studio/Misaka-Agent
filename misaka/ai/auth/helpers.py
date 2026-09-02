"""Auth-method constructors, translated from pi's ``packages/ai/src/auth/helpers.ts``.

``envApiKeyAuth`` is the shape most providers use verbatim; providers with non-standard
resolution (provider env, ambient files, IAM) write their own ``ApiKeyAuth`` instead.
``lazyOAuth`` wraps an ``OAuthAuth`` whose implementation is produced by a ``load``
callable, run on the first ``login``/``refresh``/``toAuth`` call rather than when the
provider definition is built.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from misaka.ai.auth.types import (
    ApiKeyAuth,
    ApiKeyCredential,
    AuthResult,
    ModelAuth,
    OAuthAuth,
    OAuthCredential,
    ProviderAuthInteraction,
    SecretPrompt,
)
from misaka.utils.values import signal_aborted


def _throwIfAborted(signal: Any) -> None:
    """pi calls ``signal.throwIfAborted()``; this is misaka's spelling of it.

    The message matches ``ai/providers/_common.py`` so an abort raised here is
    indistinguishable from one raised on the request path.
    """
    if signal_aborted(signal):
        raise RuntimeError("Request was aborted")


def envApiKeyAuth(name: str, envVars: list[str] | tuple[str, ...]) -> ApiKeyAuth:
    """Standard api-key auth: a stored credential key wins, else the first set env var.

    The order matters and is upstream's: someone who ran ``/login`` and typed a key meant
    that key, even on a machine where an older variable is still exported.
    """

    async def login(interaction: ProviderAuthInteraction) -> ApiKeyCredential:
        _throwIfAborted(interaction.signal)
        key = await interaction.prompt(SecretPrompt(message=f"Enter {name}"))
        _throwIfAborted(interaction.signal)
        return ApiKeyCredential(key=key)

    async def resolve(*, ctx: Any, credential: ApiKeyCredential | None, signal: Any) -> AuthResult | None:
        _throwIfAborted(signal)
        if credential is not None:
            if credential.key:
                return AuthResult(
                    auth=ModelAuth(apiKey=credential.key),
                    env=credential.env,
                    source="stored credential",
                )
            return None
        for envVar in envVars:
            value = await ctx.env(envVar)
            _throwIfAborted(signal)
            if value:
                return AuthResult(auth=ModelAuth(apiKey=value), source=envVar)
        return None

    return ApiKeyAuth(name=name, resolve=resolve, login=login)


def lazyOAuth(
    *,
    name: str,
    load: Callable[[], Awaitable[OAuthAuth]],
    isSubscription: bool | None = None,
    loginLabel: str | None = None,
) -> OAuthAuth:
    """Wrap an ``OAuthAuth`` that is imported on first use.

    ``load`` runs at most once even under concurrent first calls. Upstream writes
    ``promise ??= input.load()`` in a non-async ``loaded()``, so the assignment lands
    before anything awaits and every concurrent caller awaits the one promise. Caching
    the awaited *result* instead of the pending future loses that: four simultaneous
    first calls each find the cache still empty and start their own load.
    ``tests/test_ai_auth_stack.py::test_a_lazy_oauth_flow_loads_once_under_concurrent_first_use``
    pins the difference.
    """
    pending: asyncio.Future[OAuthAuth] | None = None

    async def _loaded() -> OAuthAuth:
        nonlocal pending
        if pending is None:
            pending = asyncio.ensure_future(load())
        return await pending

    async def login(interaction: ProviderAuthInteraction) -> OAuthCredential:
        return await (await _loaded()).login(interaction)

    async def refresh(credential: OAuthCredential, signal: Any) -> OAuthCredential:
        return await (await _loaded()).refresh(credential, signal)

    async def toAuth(credential: OAuthCredential) -> ModelAuth:
        return await (await _loaded()).toAuth(credential)

    return OAuthAuth(
        name=name,
        login=login,
        refresh=refresh,
        toAuth=toAuth,
        isSubscription=isSubscription,
        loginLabel=loginLabel,
    )


__all__ = ["envApiKeyAuth", "lazyOAuth"]

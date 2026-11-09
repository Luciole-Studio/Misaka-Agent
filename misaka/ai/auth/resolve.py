"""Translated from pi's ``packages/ai/src/auth/resolve.ts`` (v1.1.0).

MISAKA fork (each is marked where it happens): the env overlay object literal is ``_EnvOverlayAuthContext``, and a
started refresh is shielded from the caller's task cancellation, which, like an aborted signal, cancels only the wait
for the lock.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from misaka.ai.auth.types import (
    ApiKeyAuth,
    ApiKeyCredential,
    AuthContext,
    AuthOperationOptions,
    AuthResult,
    Credential,
    CredentialStore,
    OAuthAuth,
    OAuthCredential,
    ProviderAuth,
)
from misaka.ai.types import ProviderEnv
from misaka.ai.utils.abort import operationSignal, raceWithAbortSignal
from misaka.ai.utils.models_error import ModelsError, ModelsErrorCode
from misaka.utils.js import (
    AbortController,
    AbortSignal,
    abortReason,
    addAbortListener,
    throwIfAborted,
)
from misaka.utils.values import signal_aborted


class AuthResolutionOverrides(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    apiKey: str | None = None
    env: ProviderEnv | None = None
    # Require this much remaining OAuth-token validity; defaults to five minutes.
    minOAuthValidityMs: int | None = None
    signal: Any | None = None


class _ResolvableProvider(Protocol):
    """Upstream's inline `{ id: string; auth: ProviderAuth }`."""

    id: str
    auth: ProviderAuth


def resolveProviderAuth(
    provider: _ResolvableProvider,
    credentials: CredentialStore,
    authContext: AuthContext,
    overrides: AuthResolutionOverrides | None = None,
) -> asyncio.Future[AuthResult | None]:
    """Auth resolution shared by all operations in a `Models` collection.
    A stored credential owns the provider: ambient/env is consulted only when
    nothing is stored. No silent env fallback after a failed refresh or for a
    credential type without a matching handler.
    """
    signal = operationSignal(overrides.signal if overrides is not None else None)
    return raceWithAbortSignal(
        _resolveProviderAuthWithSignal(provider, credentials, authContext, overrides, signal),
        signal,
    )


async def _resolveProviderAuthWithSignal(
    provider: _ResolvableProvider,
    credentials: CredentialStore,
    authContext: AuthContext,
    overrides: AuthResolutionOverrides | None,
    signal: Any,
) -> AuthResult | None:
    throwIfAborted(signal)
    requestAuthContext = (
        _overlayEnvAuthContext(authContext, overrides.env) if overrides is not None and overrides.env else authContext
    )

    if overrides is not None and overrides.apiKey is not None and provider.auth.apiKey:
        return await _resolveApiKey(
            requestAuthContext,
            provider.auth.apiKey,
            provider.id,
            ApiKeyCredential(
                type="api_key",
                key=overrides.apiKey,
                env=overrides.env,
            ),
            signal,
        )

    stored = await _readCredential(credentials, provider.id, signal)
    if stored:
        if stored.type == "oauth" and provider.auth.oauth:
            return await _resolveStoredOAuth(
                credentials,
                provider.id,
                provider.auth.oauth,
                stored,
                signal,
                overrides.minOAuthValidityMs if overrides is not None else None,
            )
        if stored.type == "api_key" and provider.auth.apiKey:
            credential = (
                stored.model_copy(update={"env": {**(stored.env or {}), **overrides.env}})
                if overrides is not None and overrides.env
                else stored
            )
            return await _resolveApiKey(requestAuthContext, provider.auth.apiKey, provider.id, credential, signal)
        return None

    # Ambient (env vars, AWS profiles, ADC files).
    return (
        await _resolveApiKey(requestAuthContext, provider.auth.apiKey, provider.id, None, signal)
        if provider.auth.apiKey
        else None
    )


class _EnvOverlayAuthContext:
    """MISAKA fork: the object literal `_overlayEnvAuthContext` returns."""

    def __init__(self, base: AuthContext, env: ProviderEnv) -> None:
        self._base = base
        self._env = env

    async def env(self, name: str) -> str | None:
        return self._env.get(name) or await self._base.env(name)

    def fileExists(self, path: str) -> Any:
        return self._base.fileExists(path)


def _overlayEnvAuthContext(base: AuthContext, env: ProviderEnv) -> AuthContext:
    return _EnvOverlayAuthContext(base, env)


_DEFAULT_OAUTH_MINIMUM_VALIDITY_MS = 5 * 60 * 1000
_DEFAULT_OAUTH_REFRESH_TIMEOUT_MS = 15_000


async def refreshStoredOAuthCredential(
    credentials: CredentialStore,
    providerId: str,
    oauth: OAuthAuth,
    needsRefresh: Callable[[OAuthCredential], bool],
    signal: Any,
) -> OAuthCredential | None:
    """Refresh a stored OAuth credential under the credential-store lock and persist
    the result before the lock is released. `needsRefresh` is re-checked under
    the lock, so concurrent callers and processes refresh only once.

    `signal` cancels only the wait for the lock. Once a refresh starts, the
    provider may already have rotated the refresh token, so the refresh and its
    persistence ignore `signal` and are bounded only by a timeout. Otherwise a
    cancelled caller could discard the only valid refresh token. Callers that
    must return promptly on cancellation race this promise with their signal.

    Resolves with the stored OAuth credential after the operation, or undefined
    when the provider no longer has an OAuth credential.
    """
    post: Credential | None = None
    lockWait = AbortController()
    started = False

    def cancelLockWait() -> None:
        lockWait.abort(abortReason(signal))

    removeAbortListener = addAbortListener(signal, cancelLockWait)
    if signal_aborted(signal):
        cancelLockWait()

    async def fn(current: Credential | None) -> Credential | None:
        nonlocal started
        started = True
        removeAbortListener()
        throwIfAborted(signal)
        if current is None or current.type != "oauth":
            return None  # logged out meanwhile
        if not needsRefresh(current):
            return None  # another process/request refreshed
        try:
            return await oauth.refresh(current, AbortSignal.timeout(_DEFAULT_OAUTH_REFRESH_TIMEOUT_MS))
        except Exception as error:
            raise ModelsError("oauth", f"OAuth refresh failed for {providerId}", {"cause": error}) from error

    try:
        # MISAKA fork: shielded, so a cancelled caller task cannot kill a started refresh; before it starts, the
        # cancellation cancels the lock wait as an aborted signal does.
        modifying = asyncio.ensure_future(
            credentials.modify(providerId, fn, AuthOperationOptions(signal=lockWait.signal))
        )
        try:
            post = await asyncio.shield(modifying)
        except asyncio.CancelledError:
            if not started:
                lockWait.abort()
            raise
    except ModelsError:
        raise
    except Exception as error:
        throwIfAborted(signal)
        raise ModelsError("auth", f"Credential store modify failed for {providerId}", {"cause": error}) from error
    finally:
        removeAbortListener()
    return post if post is not None and post.type == "oauth" else None


async def _resolveStoredOAuth(
    credentials: CredentialStore,
    providerId: str,
    oauth: OAuthAuth,
    stored: OAuthCredential,
    signal: Any,
    minOAuthValidityMs: int | None = None,
) -> AuthResult | None:
    """OAuth resolution with double-checked locking: tokens with less than five
    minutes remaining are refreshed through `refreshStoredOAuthCredential`.
    """
    minimumValidityMs = max(
        _DEFAULT_OAUTH_MINIMUM_VALIDITY_MS, minOAuthValidityMs if minOAuthValidityMs is not None else 0
    )

    def expiresSoon(credential: OAuthCredential) -> bool:
        return time.time() * 1000 + minimumValidityMs >= credential.expires

    credential = stored

    if expiresSoon(credential):
        # Optimistic check said expired; the authoritative check runs under the lock.
        post = await refreshStoredOAuthCredential(credentials, providerId, oauth, expiresSoon, signal)
        if not post:
            return None  # logged out meanwhile
        credential = post
        # The normal five-minute window triggers a refresh but does not impose a
        # provider contract. Explicit callers (such as bearer-token export) do
        # require the requested minimum after the refresh.
        if minOAuthValidityMs is not None and expiresSoon(credential):
            raise ModelsError("oauth", f"OAuth refresh returned a token that expires too soon for {providerId}")

    try:
        return AuthResult(auth=await oauth.toAuth(credential), source="OAuth")
    except Exception as error:
        raise ModelsError("oauth", f"OAuth auth derivation failed for {providerId}", {"cause": error}) from error


async def _resolveApiKey(
    authContext: AuthContext,
    apiKey: ApiKeyAuth,
    providerId: str,
    credential: ApiKeyCredential | None,
    signal: Any,
) -> AuthResult | None:
    try:
        return await apiKey.resolve(ctx=authContext, credential=credential, signal=signal)
    except Exception as error:
        raise ModelsError("auth", f"API key auth failed for provider {providerId}", {"cause": error}) from error


async def _readCredential(credentials: CredentialStore, providerId: str, signal: Any) -> Credential | None:
    try:
        return await credentials.read(providerId, AuthOperationOptions(signal=signal))
    except Exception as error:
        raise ModelsError("auth", f"Credential store read failed for {providerId}", {"cause": error}) from error


__all__ = [
    "AuthResolutionOverrides",
    "ModelsError",
    "ModelsErrorCode",
    "refreshStoredOAuthCredential",
    "resolveProviderAuth",
]

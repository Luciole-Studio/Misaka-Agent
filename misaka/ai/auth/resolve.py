# ruff: noqa: UP040 - `ModelsErrorCode` below is spelled with `TypeAlias`, not the PEP 695
# `type` statement.

"""Auth resolution, translated from pi's ``packages/ai/src/auth/resolve.ts``.

The rule the whole file exists to enforce: **a stored credential owns the provider.**
Ambient sources (env vars, AWS profiles, ADC files) are consulted only when nothing is
stored, and never as a quiet fallback after a refresh fails or when a stored credential
has no matching handler. Falling back there would sign the user in as somebody else --
a personal API key silently standing in for the work subscription they logged into.

The whole resolution races the caller's signal, while the credential store keeps any
abandoned refresh observed until it settles and refuses a late write.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict

from misaka.ai.auth.types import (
    ApiKeyAuth,
    ApiKeyCredential,
    AuthContext,
    AuthResult,
    CredentialStore,
    CredentialValue,
    OAuthAuth,
    OAuthCredential,
    ProviderAuth,
    ProviderEnv,
)
from misaka.ai.utils.abort import race_with_abort_signal
from misaka.ai.utils.diagnostics import format_thrown_value
from misaka.utils.values import signal_aborted

ModelsErrorCode: TypeAlias = Literal[
    "model_source", "model_validation", "provider", "stream", "auth", "oauth"
]

DEFAULT_OAUTH_MINIMUM_VALIDITY_MS = 5 * 60 * 1000
DEFAULT_OAUTH_REFRESH_TIMEOUT_MS = 15_000


class ModelsError(Exception):
    """An error the model layer raises with a machine-readable ``code``.

    The underlying reason is folded into the message, which is upstream's reason for
    ``withCauseDetail`` ("Callers surface ``error.message`` only", ``resolve.ts:36``);
    ``__cause__`` is still set when the cause is an exception.
    """

    def __init__(self, code: ModelsErrorCode, message: str, cause: Any = None) -> None:
        super().__init__(_withCauseDetail(message, cause))
        self.code: ModelsErrorCode = code
        if cause is not None:
            self.__cause__ = cause if isinstance(cause, BaseException) else None


def _withCauseDetail(message: str, cause: Any) -> str:
    if cause is None:
        return message
    detail = format_thrown_value(cause).strip()
    if not detail or detail in message:
        return message
    return f"{message}: {detail}"


class AuthResolutionOverrides(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    apiKey: str | None = None
    env: ProviderEnv | None = None
    # Raise the required remaining OAuth-token validity: the effective minimum is the
    # larger of this and DEFAULT_OAUTH_MINIMUM_VALIDITY_MS (see `_resolveStoredOAuth`).
    minOAuthValidityMs: int | None = None
    signal: Any | None = None


class _ResolvableProvider:
    """The slice of a provider this module needs: its id and its auth methods."""

    id: str
    auth: ProviderAuth


def _throwIfAborted(signal: Any) -> None:
    if signal_aborted(signal):
        raise RuntimeError("Request was aborted")


class _EnvOverlayAuthContext:
    """``overrides.env`` shadows the ambient environment, falling through when unset."""

    def __init__(self, base: AuthContext, env: ProviderEnv) -> None:
        self._base = base
        self._env = env

    async def env(self, name: str) -> str | None:
        return self._env.get(name) or await self._base.env(name)

    async def fileExists(self, path: str) -> bool:
        return await self._base.fileExists(path)


async def resolveProviderAuth(
    provider: _ResolvableProvider,
    credentials: CredentialStore,
    authContext: AuthContext,
    overrides: AuthResolutionOverrides | None = None,
) -> AuthResult | None:
    """Auth resolution shared by the ``Models`` and images collections."""
    signal = overrides.signal if overrides is not None else None
    return await race_with_abort_signal(
        _resolveProviderAuthWithSignal(
            provider, credentials, authContext, overrides, signal
        ),
        signal,
    )


async def _resolveProviderAuthWithSignal(
    provider: _ResolvableProvider,
    credentials: CredentialStore,
    authContext: AuthContext,
    overrides: AuthResolutionOverrides | None,
    signal: Any,
) -> AuthResult | None:
    _throwIfAborted(signal)

    requestAuthContext: AuthContext = authContext
    if overrides is not None and overrides.env:
        requestAuthContext = _EnvOverlayAuthContext(authContext, overrides.env)

    if overrides is not None and overrides.apiKey is not None and provider.auth.apiKey is not None:
        return await _resolveApiKey(
            requestAuthContext,
            provider.auth.apiKey,
            provider.id,
            ApiKeyCredential(key=overrides.apiKey, env=overrides.env),
            signal,
        )

    stored = await _readCredential(credentials, provider.id, signal)
    if stored is not None:
        if isinstance(stored, OAuthCredential) and provider.auth.oauth is not None:
            return await _resolveStoredOAuth(
                credentials,
                provider.id,
                provider.auth.oauth,
                stored,
                signal,
                overrides.minOAuthValidityMs if overrides is not None else None,
            )
        if isinstance(stored, ApiKeyCredential) and provider.auth.apiKey is not None:
            credential = stored
            if overrides is not None and overrides.env:
                credential = stored.model_copy(
                    update={"env": {**(stored.env or {}), **overrides.env}}
                )
            return await _resolveApiKey(
                requestAuthContext, provider.auth.apiKey, provider.id, credential, signal
            )
        # A stored credential with no matching handler is not a reason to try the
        # environment: the user logged in, and answering with somebody else's key
        # would be worse than answering "not configured".
        return None

    # Ambient (env vars, AWS profiles, ADC files).
    if provider.auth.apiKey is None:
        return None
    return await _resolveApiKey(requestAuthContext, provider.auth.apiKey, provider.id, None, signal)


async def _resolveStoredOAuth(
    credentials: CredentialStore,
    providerId: str,
    oauth: OAuthAuth,
    stored: OAuthCredential,
    signal: Any,
    minOAuthValidityMs: int | None,
) -> AuthResult | None:
    """OAuth resolution with double-checked locking.

    A token with less than five minutes left goes through ``credentials.modify``, which
    re-checks expiry under that store's lock, refreshes, and persists the rotated
    credential before the lock is released. A caller that finds the token already
    refreshed under the lock returns ``None`` from ``fn`` and leaves the entry alone.
    """
    minimumValidityMs = max(DEFAULT_OAUTH_MINIMUM_VALIDITY_MS, minOAuthValidityMs or 0)

    def expiresSoon(credential: OAuthCredential) -> bool:
        return time.time() * 1000 + minimumValidityMs >= credential.expires

    credential = stored

    if expiresSoon(credential):
        # The optimistic check said expired; the authoritative one runs under the lock.
        async def refreshUnderLock(current: CredentialValue | None) -> CredentialValue | None:
            if not isinstance(current, OAuthCredential):
                return None  # logged out meanwhile
            if not expiresSoon(current):
                return None  # another process or request refreshed it
            try:
                return await asyncio.wait_for(
                    oauth.refresh(current, signal),
                    timeout=DEFAULT_OAUTH_REFRESH_TIMEOUT_MS / 1000,
                )
            except Exception as error:
                raise ModelsError(
                    "oauth", f"OAuth refresh failed for {providerId}", error
                ) from error

        try:
            post = await credentials.modify(
                providerId, refreshUnderLock, _operationOptions(signal)
            )
        except ModelsError:
            raise
        except Exception as error:
            raise ModelsError(
                "auth", f"Credential store modify failed for {providerId}", error
            ) from error

        if not isinstance(post, OAuthCredential):
            return None  # logged out meanwhile
        credential = post
        # The five-minute default triggers a refresh but is not enforced afterwards; only
        # an explicitly passed `minOAuthValidityMs` is checked against the new token.
        if minOAuthValidityMs is not None and expiresSoon(credential):
            raise ModelsError(
                "oauth",
                f"OAuth refresh returned a token that expires too soon for {providerId}",
            )

    try:
        return AuthResult(auth=await oauth.toAuth(credential), source="OAuth")
    except Exception as error:
        raise ModelsError(
            "oauth", f"OAuth auth derivation failed for {providerId}", error
        ) from error


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
        raise ModelsError(
            "auth", f"API key auth failed for provider {providerId}", error
        ) from error


async def _readCredential(
    credentials: CredentialStore, providerId: str, signal: Any
) -> CredentialValue | None:
    try:
        return await credentials.read(providerId, _operationOptions(signal))
    except Exception as error:
        raise ModelsError(
            "auth", f"Credential store read failed for {providerId}", error
        ) from error


def _operationOptions(signal: Any):
    from misaka.ai.auth.types import AuthOperationOptions

    return AuthOperationOptions(signal=signal)


__all__ = [
    "DEFAULT_OAUTH_MINIMUM_VALIDITY_MS",
    "DEFAULT_OAUTH_REFRESH_TIMEOUT_MS",
    "AuthResolutionOverrides",
    "ModelsError",
    "ModelsErrorCode",
    "resolveProviderAuth",
]

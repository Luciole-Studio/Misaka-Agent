"""Non-persistent runtime API keys over a credential store.

Translated from pi's ``coding-agent/src/core/runtime-credentials.ts``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from misaka.ai.auth.types import (
    ApiKeyCredential,
    AuthOperationOptions,
    Credential,
    CredentialInfo,
    CredentialStore,
)
from misaka.utils._abort_signal import throw_if_aborted


def _signal(options: AuthOperationOptions | None) -> object:
    return options.signal if options is not None else None


class RuntimeCredentials:
    """Async credential store overlay for non-persistent runtime API keys."""

    def __init__(self, store: CredentialStore) -> None:
        self._store = store
        self._overrides: dict[str, str] = {}

    def setRuntimeApiKey(self, providerId: str, apiKey: str) -> None:
        self._overrides[providerId] = apiKey

    def removeRuntimeApiKey(self, providerId: str) -> None:
        self._overrides.pop(providerId, None)

    def hasRuntimeApiKey(self, providerId: str) -> bool:
        return providerId in self._overrides

    async def read(
        self, providerId: str, options: AuthOperationOptions | None = None
    ) -> Credential | None:
        throw_if_aborted(_signal(options))
        override = self._overrides.get(providerId)
        return ApiKeyCredential(key=override) if override else await self._store.read(providerId, options)

    async def list(self, options: AuthOperationOptions | None = None) -> list[CredentialInfo]:
        entries = {entry.providerId: entry for entry in await self._store.list(options)}
        throw_if_aborted(_signal(options))
        for providerId in self._overrides:
            entries[providerId] = CredentialInfo(providerId=providerId, type="api_key")
        return list(entries.values())

    def modify(
        self,
        providerId: str,
        fn: Callable[[Credential | None], Awaitable[Credential | None]],
        options: AuthOperationOptions | None = None,
    ) -> Awaitable[Credential | None]:
        return self._store.modify(providerId, fn, options)

    async def delete(self, providerId: str, options: AuthOperationOptions | None = None) -> None:
        throw_if_aborted(_signal(options))
        await self._store.delete(providerId, options)
        self._overrides.pop(providerId, None)


__all__ = ["RuntimeCredentials"]

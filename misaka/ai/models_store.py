"""Persistent model catalogs, translated from pi's ``packages/ai/src/models-store.ts``.

This is the cache behind a remote catalog: what a provider last told us its models are,
plus the two HTTP validators that let the next check be a 304 instead of a download.
``etag`` is stored verbatim, quotes included, because the quotes are part of the entity-tag
syntax and the stored value goes straight back out as ``If-None-Match``: upstream's catalog
client does exactly that (``coding-agent/src/core/remote-catalog-provider.ts:88``). No
misaka caller sends that header yet -- the field is here so a future one can.

Entries are copied on the way in and out. Upstream uses ``structuredClone`` for this;
the reason is the same in either language -- a caller that mutates the list it read must
not be quietly editing the cache other callers are about to read.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from misaka.ai.types import Model
from misaka.utils.values import signal_aborted


class ModelsStoreEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: list[Model]
    # Unix timestamp from the remote catalog's Last-Modified header.
    lastModified: int | None = None
    # Unix timestamp of the last completed remote check.
    checkedAt: int | None = None
    # Opaque validator from the remote catalog's ETag header, stored verbatim.
    etag: str | None = None


class ModelsStoreOperationOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    signal: Any | None = None


def _throwIfAborted(options: ModelsStoreOperationOptions | None) -> None:
    if options is not None and signal_aborted(options.signal):
        raise RuntimeError("Request was aborted")


class InMemoryModelsStore:
    """The default store: catalogs keyed by provider id, kept for this process only."""

    def __init__(self) -> None:
        self._entries: dict[str, ModelsStoreEntry] = {}

    async def read(
        self, providerId: str, options: ModelsStoreOperationOptions | None = None
    ) -> ModelsStoreEntry | None:
        _throwIfAborted(options)
        entry = self._entries.get(providerId)
        return entry.model_copy(deep=True) if entry is not None else None

    async def write(
        self,
        providerId: str,
        entry: ModelsStoreEntry,
        options: ModelsStoreOperationOptions | None = None,
    ) -> None:
        _throwIfAborted(options)
        self._entries[providerId] = entry.model_copy(deep=True)

    async def delete(
        self, providerId: str, options: ModelsStoreOperationOptions | None = None
    ) -> None:
        _throwIfAborted(options)
        self._entries.pop(providerId, None)


__all__ = ["InMemoryModelsStore", "ModelsStoreEntry", "ModelsStoreOperationOptions"]

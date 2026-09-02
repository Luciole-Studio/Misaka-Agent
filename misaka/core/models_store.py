"""Locked JSON storage for dynamically refreshed provider catalogs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from misaka.ai.models_store import ModelsStoreEntry, ModelsStoreOperationOptions
from misaka.ai.utils.abort import throw_if_aborted
from misaka.core.auth_storage import FileAuthStorageBackend, LockResult


class FileModelsStore:
    """Pi's ``FileModelsStore``, reusing Misaka's hardened locked JSON backend."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._storage = FileAuthStorageBackend(self.path)

    @staticmethod
    def _parse(content: str | None) -> dict[str, Any]:
        if not content:
            return {}
        parsed = json.loads(content.removeprefix("\ufeff"))
        if not isinstance(parsed, dict):
            raise RuntimeError("models-store.json must contain an object")  # noqa: TRY004
        return parsed

    async def read(
        self,
        providerId: str,
        options: ModelsStoreOperationOptions | None = None,
    ) -> ModelsStoreEntry | None:
        signal = options.signal if options is not None else None
        throw_if_aborted(signal)

        async def read_locked(content: str | None) -> LockResult:
            throw_if_aborted(signal)
            raw = self._parse(content).get(providerId)
            entry = ModelsStoreEntry.model_validate(raw) if raw is not None else None
            throw_if_aborted(signal)
            return LockResult(
                result=entry.model_copy(deep=True) if entry is not None else None
            )

        return await self._storage.withLockAsync(read_locked)

    async def write(
        self,
        providerId: str,
        entry: ModelsStoreEntry,
        options: ModelsStoreOperationOptions | None = None,
    ) -> None:
        signal = options.signal if options is not None else None
        throw_if_aborted(signal)

        async def write_locked(content: str | None) -> LockResult:
            throw_if_aborted(signal)
            current = self._parse(content)
            current[providerId] = entry.model_copy(deep=True).model_dump(
                mode="json", exclude_none=True
            )
            throw_if_aborted(signal)
            return LockResult(result=None, next=json.dumps(current, indent=2))

        await self._storage.withLockAsync(write_locked)

    async def delete(
        self,
        providerId: str,
        options: ModelsStoreOperationOptions | None = None,
    ) -> None:
        signal = options.signal if options is not None else None
        throw_if_aborted(signal)

        async def delete_locked(content: str | None) -> LockResult:
            throw_if_aborted(signal)
            current = self._parse(content)
            current.pop(providerId, None)
            throw_if_aborted(signal)
            return LockResult(result=None, next=json.dumps(current, indent=2))

        await self._storage.withLockAsync(delete_locked)


__all__ = ["FileModelsStore"]

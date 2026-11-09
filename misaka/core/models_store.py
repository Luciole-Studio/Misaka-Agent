"""Locked JSON-backed storage for dynamically refreshed provider catalogs.

Translated from pi's ``coding-agent/src/core/models-store.ts``.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from misaka.ai.auth.types import AuthOperationOptions
from misaka.ai.models import _hasKnownModelType
from misaka.ai.models_store import ModelsStoreEntry, ModelsStoreOperationOptions
from misaka.ai.types import ANY_MODEL_ADAPTER
from misaka.config import home
from misaka.core.auth_storage import FileAuthStorageBackend, FileRevision, LockResult
from misaka.utils.abort import raceWithAbortSignal
from misaka.utils.js import AbortController, throwIfAborted
from misaka.utils.paths import getFileRevision, normalizePath

StoredModels = dict[str, Any]


@dataclass
class _ModelsFileReload:
    controller: AbortController
    promise: asyncio.Task[StoredModels]
    # MISAKA fork: an asyncio task belongs to the event loop that created it, so a
    # reader on another loop starts its own reload instead of joining this one.
    loop: asyncio.AbstractEventLoop
    readers: int = 0


@dataclass
class _ModelsFileReadState:
    data: StoredModels = field(default_factory=dict)
    revision: FileRevision | None = None
    reload: _ModelsFileReload | None = None


@dataclass
class _SharedModelsFileReadState:
    path: str
    readState: _ModelsFileReadState


# Optimize the common path without retaining an unbounded set of custom paths.
_sharedModelsFileReadState: _SharedModelsFileReadState | None = None


def _signal(options: ModelsStoreOperationOptions | None) -> Any:
    return options.signal if options is not None else None


def _lockOptions(options: ModelsStoreOperationOptions | None) -> AuthOperationOptions | None:
    return AuthOperationOptions(signal=options.signal) if options is not None else None


class InMemoryCodingAgentModelsStore:
    def __init__(self) -> None:
        self._entries: dict[str, ModelsStoreEntry] = {}

    async def read(
        self, providerId: str, options: ModelsStoreOperationOptions | None = None
    ) -> ModelsStoreEntry | None:
        throwIfAborted(_signal(options))
        entry = self._entries.get(providerId)
        return entry.model_copy(deep=True) if entry is not None else None

    async def write(
        self, providerId: str, entry: ModelsStoreEntry, options: ModelsStoreOperationOptions | None = None
    ) -> None:
        throwIfAborted(_signal(options))
        self._entries[providerId] = entry.model_copy(deep=True)

    async def delete(self, providerId: str, options: ModelsStoreOperationOptions | None = None) -> None:
        throwIfAborted(_signal(options))
        self._entries.pop(providerId, None)


def _readableEntry(entry: Any) -> ModelsStoreEntry | None:
    # MISAKA fork: pi returns the stored object as it is, and `Models` drops the models whose
    # type it does not know (`withKnownModelTypes`). Python has to read the models here, so a
    # stored model this version cannot read (a newer build's type or field) is dropped here the
    # same way, and an entry it cannot read is no entry; the next network refresh rewrites it.
    if not isinstance(entry, dict):
        return None
    models = []
    for model in entry.get("models") or []:
        if not _hasKnownModelType(model):
            continue
        try:
            models.append(ANY_MODEL_ADAPTER.validate_python(model))
        except ValidationError:
            continue
    try:
        return ModelsStoreEntry.model_validate(
            {**{key: value for key, value in entry.items() if key in ModelsStoreEntry.model_fields}, "models": models}
        )
    except ValidationError:
        return None


class FileModelsStore:
    """Locked JSON-backed storage for dynamically refreshed provider catalogs.

    MISAKA fork: the locked backend is the auth store's, which narrows the file to 0600.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        global _sharedModelsFileReadState
        # MISAKA fork: the default store lives in the home's state directory.
        self._path = normalizePath(os.fspath(path if path is not None else home.path("models_store")))
        self._storage = FileAuthStorageBackend(self._path)
        self._readState = (
            _sharedModelsFileReadState.readState
            if _sharedModelsFileReadState is not None and _sharedModelsFileReadState.path == self._path
            else _ModelsFileReadState()
        )
        if _sharedModelsFileReadState is None:
            _sharedModelsFileReadState = _SharedModelsFileReadState(path=self._path, readState=self._readState)

    @staticmethod
    def _parse(content: str | None) -> StoredModels:
        if not content:
            return {}
        parsed = json.loads(content.removeprefix("﻿"))
        # MISAKA fork: a non-object file is refused here rather than failing on first lookup.
        if not isinstance(parsed, dict):
            raise RuntimeError("models-store.json must contain an object")  # noqa: TRY004
        return parsed

    @staticmethod
    def _updateReadState(
        readState: _ModelsFileReadState, data: StoredModels, revision: FileRevision | None = None
    ) -> None:
        readState.data = data
        readState.revision = revision

    async def _reloadFromStorage(
        self, readState: _ModelsFileReadState, options: AuthOperationOptions | None = None
    ) -> StoredModels:
        async def reload(content: str | None) -> LockResult:
            data = self._parse(content)
            self._updateReadState(readState, data, getFileRevision(self._path))
            return LockResult(result=data)

        return await self._storage.withLockAsync(reload, options)

    async def _readLatest(
        self, readState: _ModelsFileReadState, options: ModelsStoreOperationOptions | None = None
    ) -> StoredModels:
        signal = _signal(options)
        throwIfAborted(signal)
        revision = getFileRevision(self._path)
        if revision is not None and revision == readState.revision:
            return readState.data
        loop = asyncio.get_running_loop()
        if readState.reload is None or readState.reload.loop is not loop:
            controller = AbortController()
            started = _ModelsFileReload(
                controller=controller,
                promise=loop.create_task(
                    self._reloadFromStorage(readState, AuthOperationOptions(signal=controller.signal))
                ),
                loop=loop,
            )
            readState.reload = started

            def settle(task: asyncio.Task[StoredModels]) -> None:
                if readState.reload is started:
                    readState.reload = None
                if not task.cancelled():
                    task.exception()

            started.promise.add_done_callback(settle)

        reload = readState.reload
        reload.readers += 1
        try:
            return await raceWithAbortSignal(asyncio.shield(reload.promise), signal)
        finally:
            reload.readers -= 1
            if reload.readers == 0 and readState.reload is reload:
                readState.reload = None
                reload.controller.abort()

    async def read(
        self, providerId: str, options: ModelsStoreOperationOptions | None = None
    ) -> ModelsStoreEntry | None:
        entry = (await self._readLatest(self._readState, options)).get(providerId)
        throwIfAborted(_signal(options))
        return _readableEntry(copy.deepcopy(entry)) if entry is not None else None

    async def write(
        self, providerId: str, entry: ModelsStoreEntry, options: ModelsStoreOperationOptions | None = None
    ) -> None:
        latest: StoredModels | None = None

        async def write_locked(content: str | None) -> LockResult:
            nonlocal latest
            current = self._parse(content)
            current[providerId] = entry.model_dump(mode="json", exclude_unset=True)
            latest = current
            return LockResult(result=None, next=json.dumps(current, indent=2, ensure_ascii=False))

        await self._storage.withLockAsync(write_locked, _lockOptions(options))
        if latest is not None:
            self._updateReadState(self._readState, latest)

    async def delete(self, providerId: str, options: ModelsStoreOperationOptions | None = None) -> None:
        latest: StoredModels | None = None

        async def delete_locked(content: str | None) -> LockResult:
            nonlocal latest
            current = self._parse(content)
            current.pop(providerId, None)
            latest = current
            return LockResult(result=None, next=json.dumps(current, indent=2, ensure_ascii=False))

        await self._storage.withLockAsync(delete_locked, _lockOptions(options))
        if latest is not None:
            self._updateReadState(self._readState, latest)


__all__ = ["FileModelsStore", "InMemoryCodingAgentModelsStore"]

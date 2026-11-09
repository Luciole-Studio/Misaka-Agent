"""Shared interactive all-catalog refreshes.

Translated from pi's ``coding-agent/src/modes/interactive/model-catalog-refresh.ts``.
"""

from __future__ import annotations

import asyncio
import weakref
from dataclasses import dataclass
from typing import Any

from misaka.ai.models import ModelsRefreshOptions, ModelsRefreshResult
from misaka.utils.abort import raceWithAbortSignal
from misaka.utils.js import AbortController, throwIfAborted


@dataclass(slots=True)
class _ActiveModelCatalogRefresh:
    controller: AbortController
    promise: asyncio.Future[ModelsRefreshResult]
    waiters: int = 0


class _ModelCatalogRefreshCoordinator:
    def __init__(self) -> None:
        self._activeByRuntime: weakref.WeakKeyDictionary[Any, _ActiveModelCatalogRefresh] = (
            weakref.WeakKeyDictionary()
        )

    async def refresh(self, modelRuntime: Any, signal: Any) -> ModelsRefreshResult:
        throwIfAborted(signal)
        active = self._activeByRuntime.get(modelRuntime)
        if active is None:
            controller = AbortController()
            # Started eagerly, as a JavaScript call runs up to its first await.
            operation = asyncio.Task(
                modelRuntime.refresh(ModelsRefreshOptions(signal=controller.signal)),
                loop=asyncio.get_running_loop(),
                eager_start=True,
            )
            promise = asyncio.ensure_future(raceWithAbortSignal(operation, controller.signal))
            created = _ActiveModelCatalogRefresh(controller=controller, promise=promise)

            def settle(task: asyncio.Future[ModelsRefreshResult]) -> None:
                if self._activeByRuntime.get(modelRuntime) is created:
                    del self._activeByRuntime[modelRuntime]
                if not task.cancelled():
                    task.exception()

            promise.add_done_callback(settle)
            active = created
            self._activeByRuntime[modelRuntime] = active

        active.waiters += 1
        try:
            return await raceWithAbortSignal(asyncio.shield(active.promise), signal)
        finally:
            active.waiters -= 1
            if active.waiters == 0 and self._activeByRuntime.get(modelRuntime) is active:
                # MISAKA fork: the abandoned refresh is forgotten now rather than when it settles: in pi the
                # rejection and its cleanup run before any later caller can look.
                del self._activeByRuntime[modelRuntime]
                active.controller.abort()


_modelCatalogRefreshCoordinator = _ModelCatalogRefreshCoordinator()


async def refreshModelCatalogs(modelRuntime: Any, signal: Any) -> ModelsRefreshResult:
    """Share concurrent interactive all-catalog refreshes while keeping each caller's cancellation independent."""
    return await _modelCatalogRefreshCoordinator.refresh(modelRuntime, signal)


__all__ = ["refreshModelCatalogs"]

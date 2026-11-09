"""pi's ``model-runtime-test-utils.ts``: registries over a real ModelRuntime for tests."""

from __future__ import annotations

import weakref

from misaka.core.auth_storage import AuthStorage
from misaka.core.model_registry import ModelRegistry
from misaka.core.model_runtime import ModelRuntime
from misaka.core.models_store import InMemoryCodingAgentModelsStore

_runtimes: weakref.WeakKeyDictionary[ModelRegistry, ModelRuntime] = weakref.WeakKeyDictionary()


def _wrap(runtime: ModelRuntime) -> ModelRegistry:
    registry = ModelRegistry(runtime)
    _runtimes[registry] = runtime
    return registry


async def createModelRegistry(authStorage: AuthStorage, modelsPath: str | None = None) -> ModelRegistry:
    """Load optional models.json configuration without file-backed catalog locks."""
    return _wrap(await ModelRuntime.create({"authStorage": authStorage, "modelsPath": modelsPath, "modelsStore": InMemoryCodingAgentModelsStore(), "allowModelNetwork": False}))


async def createInMemoryModelRegistry(authStorage: AuthStorage) -> ModelRegistry:
    return _wrap(await ModelRuntime.create({"authStorage": authStorage, "modelsPath": None, "allowModelNetwork": False}))


def getModelRuntime(modelRegistry: ModelRegistry) -> ModelRuntime:
    runtime = _runtimes.get(modelRegistry)
    if runtime is None:
        raise RuntimeError("ModelRegistry was not created by the test helper")
    return runtime

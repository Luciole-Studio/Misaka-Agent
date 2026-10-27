"""Translated from pi's ``packages/ai/src/providers/images/register-builtins.ts`` (v1.1.0).

MISAKA fork: the module cache holds the imported module or the import's exception, where upstream keeps the import
promise (the first outcome is kept either way).
"""

from __future__ import annotations

import importlib
import time
from types import ModuleType
from typing import Any

from misaka.ai.images_api_registry import ImagesApiProvider, registerImagesApiProvider
from misaka.ai.types import AssistantImages, ImageModel, ImagesContext, ImagesOptions
from misaka.utils.js import jsString

_openRouterImagesProviderModulePromise: ModuleType | BaseException | None = None


def _createLazyLoadErrorImages(model: ImageModel, error: Any) -> AssistantImages:
    return AssistantImages(
        api=model.api,
        provider=model.provider,
        model=model.id,
        output=[],
        stopReason="error",
        errorMessage=(str(error) or type(error).__name__) if isinstance(error, BaseException) else jsString(error),
        timestamp=time.time_ns() // 1_000_000,
    )


def _loadOpenRouterImagesProviderModule() -> ModuleType:
    global _openRouterImagesProviderModulePromise
    if _openRouterImagesProviderModulePromise is None:
        try:
            _openRouterImagesProviderModulePromise = importlib.import_module("misaka.ai.api.openrouter_images")
        except Exception as error:  # noqa: BLE001 - the rejected import is cached, as the promise is
            _openRouterImagesProviderModulePromise = error
    if isinstance(_openRouterImagesProviderModulePromise, BaseException):
        raise _openRouterImagesProviderModulePromise
    return _openRouterImagesProviderModulePromise


async def generateImagesOpenRouter(
    model: ImageModel,
    context: ImagesContext,
    options: ImagesOptions | None = None,
) -> AssistantImages:
    try:
        module = _loadOpenRouterImagesProviderModule()
        return await module.generateImages(model, context, options)
    except Exception as error:  # noqa: BLE001 - the failure is the result
        return _createLazyLoadErrorImages(model, error)


def registerBuiltInImagesApiProviders() -> None:
    registerImagesApiProvider(
        ImagesApiProvider(
            api="openrouter-images",
            generateImages=generateImagesOpenRouter,
        )
    )


registerBuiltInImagesApiProviders()

__all__ = ["generateImagesOpenRouter", "registerBuiltInImagesApiProviders"]

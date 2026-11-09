"""Translated from pi's ``packages/ai/src/images-api-registry.ts`` (v1.1.0).

MISAKA fork: Python image functions are coroutines, so the wrapper awaits them.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from misaka.ai.types import AssistantImages, ImageModel, ImagesContext, ImagesOptions

type ImagesApiFunction = Callable[[ImageModel, ImagesContext, ImagesOptions | None], Coroutine[Any, Any, AssistantImages]]


@dataclass(slots=True)
class ImagesApiProvider:
    api: str
    generateImages: ImagesApiFunction


# The registry holds `ImagesApiProvider`s whose function is the wrapped one.
_ImagesApiProviderInternal = ImagesApiProvider


@dataclass(slots=True)
class _RegisteredImagesApiProvider:
    provider: _ImagesApiProviderInternal
    sourceId: str | None = None


_imagesApiProviderRegistry: dict[str, _RegisteredImagesApiProvider] = {}


def _wrapGenerateImages(api: str, generateImages: ImagesApiFunction) -> ImagesApiFunction:
    async def wrapped(model: ImageModel, context: ImagesContext, options: ImagesOptions | None = None) -> AssistantImages:
        if model.api != api:
            raise RuntimeError(f"Mismatched api: {model.api} expected {api}")
        return await generateImages(model, context, options)

    return wrapped


def registerImagesApiProvider(provider: ImagesApiProvider, sourceId: str | None = None) -> None:
    _imagesApiProviderRegistry[provider.api] = _RegisteredImagesApiProvider(
        provider=ImagesApiProvider(
            api=provider.api,
            generateImages=_wrapGenerateImages(provider.api, provider.generateImages),
        ),
        sourceId=sourceId,
    )


def getImagesApiProvider(api: str) -> _ImagesApiProviderInternal | None:
    entry = _imagesApiProviderRegistry.get(api)
    return entry.provider if entry else None


__all__ = ["ImagesApiFunction", "ImagesApiProvider", "getImagesApiProvider", "registerImagesApiProvider"]

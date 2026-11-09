"""Translated from pi's ``packages/ai/src/images.ts`` (v1.1.0)."""

from __future__ import annotations

import misaka.ai.providers.images.register_builtins  # noqa: F401 - registers the builtin image APIs
from misaka.ai.images_api_registry import getImagesApiProvider
from misaka.ai.types import (
    AssistantImages,
    ImageModel,
    ImagesContext,
    ProviderImagesOptions,
)


def _resolveImagesApiProvider(api: str):
    provider = getImagesApiProvider(api)
    if not provider:
        raise RuntimeError(f"No API provider registered for api: {api}")
    return provider


async def generateImages(
    model: ImageModel,
    context: ImagesContext,
    options: ProviderImagesOptions | None = None,
) -> AssistantImages:
    """Global image generation dispatched on `model.api` through the images api
    registry. Auth must be passed explicitly via `options.apiKey`; prefer
    `Models.generateImages()`, which resolves provider auth.
    """
    provider = _resolveImagesApiProvider(model.api)
    # MISAKA fork: a plain mapping context is read into ImagesContext.
    resolvedContext = context if isinstance(context, ImagesContext) else ImagesContext.model_validate(context)
    return await provider.generateImages(model, resolvedContext, options)


__all__ = ["generateImages"]

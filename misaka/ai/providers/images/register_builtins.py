"""Lazy registration for built-in image generation providers."""

from __future__ import annotations

import importlib
import time
from types import ModuleType
from typing import Any

from misaka.ai.images_api_registry import (
    ImagesApiProvider,
    register_images_api_provider,
)
from misaka.ai.types import AssistantImages, ImagesContext, ImagesModel, ImagesOptions

_openrouter_images_provider_module: ModuleType | Exception | None = None


def _create_lazy_load_error_images(model: ImagesModel, error: Any) -> AssistantImages:
    return AssistantImages(
        api=model.api,
        provider=model.provider,
        model=model.id,
        output=[],
        stopReason="error",
        errorMessage=str(error),
        timestamp=time.time_ns() // 1_000_000,
    )


def _load_openrouter_images_provider_module() -> ModuleType:
    global _openrouter_images_provider_module
    if _openrouter_images_provider_module is None:
        try:
            _openrouter_images_provider_module = importlib.import_module("misaka.ai.providers.images.openrouter")
        except Exception as error:
            _openrouter_images_provider_module = error
            raise
    if isinstance(_openrouter_images_provider_module, Exception):
        raise _openrouter_images_provider_module
    return _openrouter_images_provider_module


async def generate_images_openrouter(
    model: ImagesModel,
    context: ImagesContext,
    options: ImagesOptions | None = None,
) -> AssistantImages:
    try:
        module = _load_openrouter_images_provider_module()
        return await module.generateImagesOpenRouter(model, context, options)
    except Exception as error:  # noqa: BLE001
        return _create_lazy_load_error_images(model, error)


def register_built_in_images_api_providers() -> None:
    register_images_api_provider(
        ImagesApiProvider(api="openrouter-images", generateImages=generate_images_openrouter)
    )


register_built_in_images_api_providers()

generateImagesOpenRouter = generate_images_openrouter

__all__ = ["generateImagesOpenRouter"]

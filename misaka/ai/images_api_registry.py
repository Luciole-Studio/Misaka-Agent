"""Registry for image-generation provider functions."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from misaka.ai.types import AssistantImages, ImagesContext, ImagesModel, ImagesOptions

ImagesApiFunction = Callable[[ImagesModel, ImagesContext, ImagesOptions | None], Coroutine[Any, Any, AssistantImages]]


@dataclass(slots=True)
class ImagesApiProvider:
    api: str
    generateImages: ImagesApiFunction


@dataclass(slots=True)
class RegisteredImagesApiProvider:
    provider: ImagesApiProvider
    source_id: str | None = None


_images_api_provider_registry: dict[str, RegisteredImagesApiProvider] = {}


def _wrap_generate_images(api: str, generate_images: ImagesApiFunction) -> ImagesApiFunction:
    async def wrapped(
        model: ImagesModel,
        context: ImagesContext,
        options: ImagesOptions | None = None,
    ) -> AssistantImages:
        if model.api != api:
            raise ValueError(f"Mismatched api: {model.api} expected {api}")
        return await generate_images(model, context, options)

    return wrapped


def register_images_api_provider(provider: ImagesApiProvider, source_id: str | None = None) -> None:
    _images_api_provider_registry[provider.api] = RegisteredImagesApiProvider(
        provider=ImagesApiProvider(
            api=provider.api,
            generateImages=_wrap_generate_images(provider.api, provider.generateImages),
        ),
        source_id=source_id,
    )


def get_images_api_provider(api: str) -> ImagesApiProvider | None:
    entry = _images_api_provider_registry.get(api)
    return entry.provider if entry else None



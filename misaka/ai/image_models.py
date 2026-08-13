"""Image model registry backed by generated image model definitions."""

from __future__ import annotations

from misaka.ai.image_models_generated import IMAGE_MODELS
from misaka.ai.types import ImagesModel

_image_model_registry: dict[str, dict[str, ImagesModel]] = {
    provider: dict(models)
    for provider, models in IMAGE_MODELS.items()
}


def get_image_model(provider: str, model_id: str) -> ImagesModel | None:
    provider_models = _image_model_registry.get(provider)
    if not provider_models:
        return None
    return provider_models.get(model_id)


def get_image_providers() -> list[str]:
    return list(_image_model_registry.keys())


def get_image_models(provider: str) -> list[ImagesModel]:
    models = _image_model_registry.get(provider)
    return list(models.values()) if models else []


getImageModel = get_image_model
getImageProviders = get_image_providers
getImageModels = get_image_models

"""Translated from pi's ``packages/ai/src/image-models.ts`` (v1.1.0).

Compat reads of the generated catalog restricted to image models. New code
uses `Models.getModelOfType("image", ...)` or `getBuiltinImageModel()` from `providers/all`.
"""

from __future__ import annotations

from misaka.ai.models_generated import IMAGE_MODELS
from misaka.ai.types import ImageModel

# Built-in providers with at least one image model in the generated catalog.
type BuiltinImageProvider = str

_imageModelsByProvider: dict[str, dict[str, ImageModel]] = {}
for _provider, _models in IMAGE_MODELS.items():
    _imageModels: dict[str, ImageModel] = {}
    for _model in _models.values():
        _imageModels[_model.id] = _model
    if len(_imageModels) > 0:
        _imageModelsByProvider[_provider] = _imageModels


def getImageModel(provider: BuiltinImageProvider, modelId: str) -> ImageModel | None:
    """@deprecated Static catalog read. Use `getBuiltinImageModel` from "@earendil-works/pi-ai/providers/all" or
    `Models.getModelOfType("image", ...)`."""
    return (_imageModelsByProvider.get(provider) or {}).get(modelId)


def getImageProviders() -> list[BuiltinImageProvider]:
    """@deprecated Static catalog read. Use `Models.getProviders()`."""
    return list(_imageModelsByProvider.keys())


def getImageModels(provider: BuiltinImageProvider) -> list[ImageModel]:
    """@deprecated Static catalog read. Use `getBuiltinImageModels` from "@earendil-works/pi-ai/providers/all" or
    `Models.getModelsOfType("image")`."""
    models = _imageModelsByProvider.get(provider)
    return list(models.values()) if models else []


__all__ = ["BuiltinImageProvider", "getImageModel", "getImageModels", "getImageProviders"]

"""Translated from pi's ``packages/ai/src/model-catalog.ts`` (v1.1.0).

pi's catalog types are compile-time only, mapped from the JSON's literal types; here they are plain aliases.

MISAKA fork: each ``flatten*ModelCatalog`` validates its entries into the model class of its type (pi's tables hold
the data as typed at compile time), so the generated ``providers/<provider>_models.py`` modules and
``models_generated`` share validated objects.
"""

from __future__ import annotations

from typing import Any

from misaka.ai.types import ClassifierModel, ImageModel, Model, ModelType

type ModelGroups = dict[str, dict[str, Any]]

type ChatModelCatalog = dict[str, Model]

type ImageModelCatalog = dict[str, ImageModel]

type ClassifierModelCatalog = dict[str, ClassifierModel]


def _flattenModelCatalog(groups: ModelGroups, type: ModelType) -> dict[str, Any]:
    return {
        model["id"]: model
        for models in groups.values()
        for model in models.values()
        if model.get("type") == type
    }


def flattenChatModelCatalog(_provider: str, groups: ModelGroups) -> ChatModelCatalog:
    return {modelId: Model.model_validate(model) for modelId, model in _flattenModelCatalog(groups, "chat").items()}


def flattenImageModelCatalog(_provider: str, groups: ModelGroups) -> ImageModelCatalog:
    return {
        modelId: ImageModel.model_validate(model) for modelId, model in _flattenModelCatalog(groups, "image").items()
    }


def flattenClassifierModelCatalog(_provider: str, groups: ModelGroups) -> ClassifierModelCatalog:
    return {
        modelId: ClassifierModel.model_validate(model)
        for modelId, model in _flattenModelCatalog(groups, "classifier").items()
    }


__all__ = [
    "ChatModelCatalog",
    "ClassifierModelCatalog",
    "ImageModelCatalog",
    "ModelGroups",
    "flattenChatModelCatalog",
    "flattenClassifierModelCatalog",
    "flattenImageModelCatalog",
]

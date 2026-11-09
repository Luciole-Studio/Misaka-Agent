"""Translated from pi's ``packages/ai/src/utils/model-operations.ts`` (v1.1.0).

MISAKA fork: models and classifier contexts may be mappings (a stored or fetched entry not yet validated); a TypeScript
object and a Python dict both carry the fields.
"""

from __future__ import annotations

import time
from typing import Any

from misaka.ai.types import (
    AssistantImages,
    ClassifierModel,
    ClassifierResult,
    ImageModel,
    ModelType,
)
from misaka.ai.utils.models_error import ModelsError
from misaka.utils.js import jsString
from misaka.utils.values import read_field


def getModelType(model: Any) -> ModelType:
    """The type of a model. Models without `type` are chat models."""
    type = read_field(model, "type", None)
    return "chat" if type is None else type


def isModelType(model: Any, type: ModelType) -> bool:
    """Runtime-checked model type narrowing, including legacy chat models without `type`."""
    return getModelType(model) == type


def assertChatModel(model: Any) -> None:
    if not isModelType(model, "chat"):
        raise ModelsError("provider", f"Model {model.provider}/{model.id} is not a chat model")


def assertImageModel(model: Any) -> None:
    if not isModelType(model, "image"):
        raise ModelsError("provider", f"Model {model.provider}/{model.id} is not an image model")


def assertClassifierModel(model: Any) -> None:
    if not isModelType(model, "classifier"):
        raise ModelsError("provider", f"Model {model.provider}/{model.id} is not a classifier model")


def assertClassifierInputSupported(model: ClassifierModel, context: Any) -> None:
    """Rejects classifier images for models whose catalog entry does not accept image input."""
    images = read_field(context, "images", None)
    if images and "image" not in model.input:
        raise ModelsError("provider", f"Model {model.provider}/{model.id} does not accept image input")


def imageErrorResult(model: ImageModel, error: Any, aborted: bool = False) -> AssistantImages:
    return AssistantImages(
        api=model.api,
        provider=model.provider,
        model=model.id,
        output=[],
        stopReason="aborted" if aborted else "error",
        errorMessage=(str(error) or type(error).__name__) if isinstance(error, BaseException) else jsString(error),
        timestamp=int(time.time() * 1000),
    )


def classifierErrorResult(model: ClassifierModel, error: Any, aborted: bool = False) -> ClassifierResult:
    return ClassifierResult(
        api=model.api,
        provider=model.provider,
        model=model.id,
        answers={},
        stopReason="aborted" if aborted else "error",
        errorMessage=(str(error) or type(error).__name__) if isinstance(error, BaseException) else jsString(error),
        timestamp=int(time.time() * 1000),
    )


__all__ = [
    "assertChatModel",
    "assertClassifierInputSupported",
    "assertClassifierModel",
    "assertImageModel",
    "classifierErrorResult",
    "getModelType",
    "imageErrorResult",
    "isModelType",
]

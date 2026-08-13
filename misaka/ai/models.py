"""Model registry and helpers backed by generated catalog data."""

from __future__ import annotations

from misaka.ai.models_generated import MODELS
from misaka.ai.types import Model, ModelThinkingLevel, Usage, UsageCost

_model_registry: dict[str, dict[str, Model]] = {
    provider: dict(models)
    for provider, models in MODELS.items()
}

_EXTENDED_THINKING_LEVELS: tuple[ModelThinkingLevel, ...] = ("off", "minimal", "low", "medium", "high", "xhigh")
_MISSING = object()


def get_model(provider: str, model_id: str) -> Model | None:
    provider_models = _model_registry.get(provider)
    if not provider_models:
        return None
    return provider_models.get(model_id)


def get_providers() -> list[str]:
    return list(_model_registry.keys())


def get_models(provider: str) -> list[Model]:
    models = _model_registry.get(provider)
    return list(models.values()) if models else []


def calculate_cost(model: Model, usage: Usage) -> UsageCost:
    usage.cost.input = (model.cost.input / 1_000_000) * usage.input
    usage.cost.output = (model.cost.output / 1_000_000) * usage.output
    usage.cost.cacheRead = (model.cost.cacheRead / 1_000_000) * usage.cacheRead
    usage.cost.cacheWrite = (model.cost.cacheWrite / 1_000_000) * usage.cacheWrite
    usage.cost.total = usage.cost.input + usage.cost.output + usage.cost.cacheRead + usage.cost.cacheWrite
    return usage.cost


def get_supported_thinking_levels(model: Model) -> list[ModelThinkingLevel]:
    if not model.reasoning:
        return ["off"]

    supported: list[ModelThinkingLevel] = []
    for level in _EXTENDED_THINKING_LEVELS:
        mapped = (
            model.thinkingLevelMap[level]
            if model.thinkingLevelMap is not None and level in model.thinkingLevelMap
            else _MISSING
        )
        if mapped is None:
            continue
        if level == "xhigh":
            if mapped is not _MISSING:
                supported.append(level)
            continue
        supported.append(level)
    return supported


def clamp_thinking_level(model: Model, level: ModelThinkingLevel) -> ModelThinkingLevel:
    available_levels = get_supported_thinking_levels(model)
    if level in available_levels:
        return level

    requested_index = _EXTENDED_THINKING_LEVELS.index(level) if level in _EXTENDED_THINKING_LEVELS else -1
    if requested_index == -1:
        return available_levels[0] if available_levels else "off"

    for candidate in _EXTENDED_THINKING_LEVELS[requested_index:]:
        if candidate in available_levels:
            return candidate
    for candidate in reversed(_EXTENDED_THINKING_LEVELS[:requested_index]):
        if candidate in available_levels:
            return candidate
    return available_levels[0] if available_levels else "off"


def models_are_equal(a: Model | None, b: Model | None) -> bool:
    if not a or not b:
        return False
    return a.id == b.id and a.provider == b.provider


getModel = get_model
getProviders = get_providers
getModels = get_models
calculateCost = calculate_cost
getSupportedThinkingLevels = get_supported_thinking_levels
clampThinkingLevel = clamp_thinking_level
modelsAreEqual = models_are_equal

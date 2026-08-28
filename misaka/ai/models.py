"""Model registry and helpers backed by generated catalog data."""

from __future__ import annotations

from datetime import datetime

from misaka.ai.models_generated import BUILTIN_MODEL_DATA_GENERATED_AT, MODELS
from misaka.ai.types import (
    Model,
    ModelCost,
    ModelCostTier,
    ModelThinkingLevel,
    Usage,
    UsageCost,
)

_model_registry: dict[str, dict[str, Model]] = {
    provider: dict(models)
    for provider, models in MODELS.items()
}

_EXTENDED_THINKING_LEVELS: tuple[ModelThinkingLevel, ...] = (
    "off", "minimal", "low", "medium", "high", "xhigh", "max",
)
_MISSING = object()


def get_builtin_model_data_generated_at() -> int | None:
    """When the built-in catalog was generated upstream, in Unix milliseconds.

    A remote catalog overlay compares what it fetched against this: data no newer than
    what shipped is not worth applying. ``None`` when the stamp is unparseable, which is
    upstream's answer too -- an unknown generation must not make everything look stale.
    """
    try:
        stamp = datetime.fromisoformat(BUILTIN_MODEL_DATA_GENERATED_AT)
    except ValueError:
        return None
    return int(stamp.timestamp() * 1000)


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
    """Price one response, honouring pricing bands and Anthropic's 1h cache split.

    Two things here are not obvious from the arithmetic:

    * A tier applies to the **whole** request once its threshold is crossed, not just to
      the tokens above it -- so the loop picks the single highest matching band rather
      than accumulating. The comparison counts cached reads and writes as input.
    * A cache entry written with 1h retention is priced at **2x the base input rate**, not
      at the cache-write rate.
    """
    input_tokens = usage.input + usage.cacheRead + usage.cacheWrite
    rates: ModelCost | ModelCostTier = model.cost
    matched_threshold = -1
    for tier in model.cost.tiers or []:
        if input_tokens > tier.inputTokensAbove and tier.inputTokensAbove > matched_threshold:
            rates = tier
            matched_threshold = tier.inputTokensAbove

    long_write = usage.cacheWrite1h or 0
    short_write = usage.cacheWrite - long_write
    usage.cost.input = (rates.input / 1_000_000) * usage.input
    usage.cost.output = (rates.output / 1_000_000) * usage.output
    usage.cost.cacheRead = (rates.cacheRead / 1_000_000) * usage.cacheRead
    usage.cost.cacheWrite = (rates.cacheWrite * short_write + rates.input * 2 * long_write) / 1_000_000
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
        # xhigh and max are opt-in: a model offers them only when its map names one.
        if level in ("xhigh", "max"):
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
modelsAreEqual = models_are_equal

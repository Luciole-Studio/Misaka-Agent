"""Translated from pi's ``packages/ai/src/providers/openrouter-api.ts`` (v1.3.0).

MISAKA fork: the interfaces are ``TypedDict``s, since both readers hold OpenRouter's JSON as parsed (the catalog
script and ``personalizeOpenRouterCatalog``); ``openRouterCost`` returns the ``ModelCost`` record as a plain mapping
for the same reason, and the runtime validates it into ``ModelCost`` where it lands on a model.
"""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict

from misaka.utils.js import UNDEFINED, parseFloat, toFixed


class OpenRouterPricingOverride(TypedDict, total=False):
    """A conditional price. `min_prompt_tokens` selects prompt-length pricing; `utc_*` fields select
    time-of-day or weekday pricing. Missing rates keep the base price."""

    min_prompt_tokens: int
    utc_start: int
    utc_end: int
    utc_days: list[str]
    prompt: str
    completion: str
    input_cache_read: str
    input_cache_write: str


class OpenRouterPricing(TypedDict, total=False):
    """OpenRouter's per-token prices, as decimal strings."""

    prompt: str
    completion: str
    input_cache_read: str
    input_cache_write: str
    overrides: list[OpenRouterPricingOverride]


class _OpenRouterTopProvider(TypedDict, total=False):
    context_length: int | None
    max_completion_tokens: int | None


class OpenRouterModel(TypedDict):
    """The fields pi reads from a model in OpenRouter's `GET /models` and `GET /models/user`."""

    id: str
    context_length: NotRequired[int | None]
    top_provider: NotRequired[_OpenRouterTopProvider]
    pricing: NotRequired[OpenRouterPricing]


def _perMillion(value: str | None, fallback: float) -> float:
    """Convert one $/token price to $/1M tokens. 0 means free. Missing, invalid and negative prices use
    the fallback: OpenRouter reports -1 for router models such as `openrouter/auto`, whose price
    depends on the model they pick."""
    perToken = parseFloat("" if value is None or value is UNDEFINED else value)
    # `NaN >= 0` is false, as in JavaScript.
    return float(toFixed(perToken * 1_000_000, 6)) if perToken >= 0 else fallback


def openRouterCost(pricing: OpenRouterPricing | None) -> dict[str, Any]:
    """Convert OpenRouter's $/token prices to pi's $/1M tokens. Missing, invalid and negative base
    prices are 0. Prompt-length overrides become request-wide tiers; a rate a tier does not list
    keeps the base price. Time-of-day overrides are skipped because ModelCost cannot express them."""
    pricing = pricing or {}
    base: dict[str, Any] = {
        "input": _perMillion(pricing.get("prompt"), 0),
        "output": _perMillion(pricing.get("completion"), 0),
        "cacheRead": _perMillion(pricing.get("input_cache_read"), 0),
        "cacheWrite": _perMillion(pricing.get("input_cache_write"), 0),
    }
    tiers: list[dict[str, Any]] = []
    for override in pricing.get("overrides") or []:
        if (
            "min_prompt_tokens" not in override
            or "utc_start" in override
            or "utc_end" in override
            or "utc_days" in override
        ):
            continue
        tiers.append(
            {
                "inputTokensAbove": override["min_prompt_tokens"],
                "input": _perMillion(override.get("prompt"), base["input"]),
                "output": _perMillion(override.get("completion"), base["output"]),
                "cacheRead": _perMillion(override.get("input_cache_read"), base["cacheRead"]),
                "cacheWrite": _perMillion(override.get("input_cache_write"), base["cacheWrite"]),
            }
        )
    return {**base, "tiers": tiers} if len(tiers) > 0 else base


__all__ = ["OpenRouterModel", "OpenRouterPricing", "OpenRouterPricingOverride", "openRouterCost"]

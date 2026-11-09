"""Translated from pi's ``packages/ai/scripts/ai-gateway-pricing.ts`` (v1.1.0).

Vercel AI Gateway pricing is in $/token: ``input``, ``output``, ``input_cache_read``, ``input_cache_write`` (strings or
numbers), and per rate a list of prompt-length brackets ``{"cost"?, "min"?, "max"?}`` (``min`` inclusive, ``max``
exclusive, in prompt tokens).
"""

from __future__ import annotations

import math
from typing import Any

from misaka.utils.js import parseFloat, toFixed

RATE_FIELDS = [
    {"rate": "input", "tiers": "input_tiers"},
    {"rate": "output", "tiers": "output_tiers"},
    {"rate": "cacheRead", "tiers": "input_cache_read_tiers"},
    {"rate": "cacheWrite", "tiers": "input_cache_write_tiers"},
]


def perMillion(value: str | float | None) -> float:
    parsed = value if isinstance(value, int | float) and not isinstance(value, bool) else parseFloat("0" if value is None else value)
    return float(toFixed(parsed * 1_000_000, 6)) if math.isfinite(parsed) else 0


def getAiGatewayCost(pricing: dict[str, Any] | None) -> dict[str, Any]:
    """Convert AI Gateway pricing to $/million tokens. Each rate lists its own prompt-length brackets; every bracket
    start above zero becomes a request-wide tier with the rates in effect there."""
    pricing = pricing or {}
    base: dict[str, Any] = {
        "input": perMillion(pricing.get("input")),
        "output": perMillion(pricing.get("output")),
        "cacheRead": perMillion(pricing.get("input_cache_read")),
        "cacheWrite": perMillion(pricing.get("input_cache_write")),
    }

    starts: list[int | float] = []
    for field in RATE_FIELDS:
        for bracket in pricing.get(field["tiers"]) or []:
            if bracket.get("min") is not None and bracket["min"] > 0 and bracket["min"] not in starts:
                starts.append(bracket["min"])

    def tier(start: float) -> dict[str, Any]:
        result: dict[str, Any] = {"inputTokensAbove": start - 1, **base}
        for field in RATE_FIELDS:
            bracket = next(
                (
                    candidate
                    for candidate in pricing.get(field["tiers"]) or []
                    if (candidate.get("min") if candidate.get("min") is not None else 0) <= start
                    # `candidate.max === undefined || start < candidate.max`: a JSON `null` compares as 0.
                    and ("max" not in candidate or start < (candidate["max"] if candidate["max"] is not None else 0))
                ),
                None,
            )
            if bracket is not None and "cost" in bracket:
                result[field["rate"]] = perMillion(bracket["cost"])
        return result

    tiers = [tier(start) for start in sorted(starts)]
    return {**base, "tiers": tiers} if len(tiers) > 0 else base


__all__ = ["RATE_FIELDS", "getAiGatewayCost", "perMillion"]

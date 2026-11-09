"""Translated from pi's ``packages/ai/scripts/openrouter-reasoning-options.ts`` (v1.1.0).

OpenRouter's reasoning metadata is ``{"mandatory"?, "default_enabled"?, "supported_efforts"?, "default_effort"?}``.
"""

from __future__ import annotations

from typing import Any

from models_dev_reasoning_options import getEffortThinkingLevelMap


def getOpenRouterThinkingLevelMap(reasoning: dict[str, Any] | None) -> dict[str, str | None] | None:
    """Convert OpenRouter's reasoning metadata into Pi model capabilities."""
    if not reasoning:
        return None
    if not reasoning.get("supported_efforts"):
        return {"off": None} if reasoning.get("mandatory") is True else None

    # OpenRouter's supported_efforts uses the same effort values as models.dev reasoning_options,
    # so both sources can share the same Pi thinking-level conversion.
    map = getEffortThinkingLevelMap([{"type": "effort", "values": reasoning["supported_efforts"]}])
    if not map:
        return {"off": None} if reasoning.get("mandatory") is True else None
    return {**map, "off": None if reasoning.get("mandatory") is True else "none"}


__all__ = ["getOpenRouterThinkingLevelMap"]

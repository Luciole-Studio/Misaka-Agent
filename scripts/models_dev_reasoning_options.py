"""Translated from pi's ``packages/ai/scripts/models-dev-reasoning-options.ts`` (v1.1.0).

A models.dev reasoning option is ``{"type": "toggle"}``, ``{"type": "effort", "values": [...]}`` (``"none"``,
``"minimal"``, ``"low"``, ``"medium"``, ``"high"``, ``"xhigh"``, ``"max"``, ``"default"`` or ``None``), or
``{"type": "budget_tokens", "min"?, "max"?}``.
"""

from __future__ import annotations

from typing import Any

THINKING_LEVELS = ["minimal", "low", "medium", "high", "xhigh", "max"]


def getEffortThinkingLevelMap(options: list[dict[str, Any]]) -> dict[str, str | None] | None:
    """Converts models.dev verified effort values into Pi's selectable thinking levels. Values without a Pi equivalent
    (``default`` and JSON ``null``) are intentionally omitted."""
    effortValues = [value for option in options if option.get("type") == "effort" for value in option.get("values", [])]
    if len(effortValues) == 0:
        return None

    supported = set(effortValues)
    if not any(level in supported for level in THINKING_LEVELS) and "none" not in supported:
        return None

    map: dict[str, str | None] = {"off": "none" if "none" in supported else None}
    for level in THINKING_LEVELS:
        map[level] = level if level in supported else None
    return map


__all__ = ["THINKING_LEVELS", "getEffortThinkingLevelMap"]

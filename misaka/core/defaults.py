"""Shared default values for coding-agent core modules."""

from __future__ import annotations

from misaka.agent.types import ThinkingLevel

DEFAULT_THINKING_LEVEL: ThinkingLevel = "medium"
THINKING_LEVEL_OPTIONS: tuple[ThinkingLevel, ...] = (
    "off",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)

__all__ = ["DEFAULT_THINKING_LEVEL", "THINKING_LEVEL_OPTIONS"]

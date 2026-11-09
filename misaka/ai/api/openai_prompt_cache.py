"""Translated from pi's ``packages/ai/src/api/openai-prompt-cache.ts`` (v1.1.0)."""

from __future__ import annotations

OPENAI_PROMPT_CACHE_KEY_MAX_LENGTH = 64


def clampOpenAIPromptCacheKey(key: str | None) -> str | None:
    if key is None:
        return None
    # `Array.from(key)`: code points, as Python iterates a string.
    chars = list(key)
    if len(chars) <= OPENAI_PROMPT_CACHE_KEY_MAX_LENGTH:
        return key
    return "".join(chars[:OPENAI_PROMPT_CACHE_KEY_MAX_LENGTH])


__all__ = ["OPENAI_PROMPT_CACHE_KEY_MAX_LENGTH", "clampOpenAIPromptCacheKey"]

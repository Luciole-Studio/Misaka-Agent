"""Helpers for OpenAI prompt-cache metadata keys."""

from __future__ import annotations

OPENAI_PROMPT_CACHE_KEY_MAX_LENGTH = 64


def clamp_openai_prompt_cache_key(key: str | None) -> str | None:
    if key is None:
        return None
    chars = list(key)
    if len(chars) <= OPENAI_PROMPT_CACHE_KEY_MAX_LENGTH:
        return key
    return "".join(chars[:OPENAI_PROMPT_CACHE_KEY_MAX_LENGTH])


clampOpenAIPromptCacheKey = clamp_openai_prompt_cache_key

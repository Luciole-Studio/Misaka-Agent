"""Translated from pi's ``packages/ai/src/utils/overflow.ts`` (v1.1.0)."""

from __future__ import annotations

import re

from misaka.ai.types import AssistantMessage

# Regex patterns to detect context overflow errors from different providers.
#
# These patterns match error messages returned when the input exceeds
# the model's context window.
#
# Provider-specific patterns (with example error messages):
#
# - Anthropic: "prompt is too long: 213462 tokens > 200000 maximum"
# - Anthropic: "413 {\"error\":{\"type\":\"request_too_large\",\"message\":\"Request exceeds the maximum size\"}}"
# - OpenAI: "Your input exceeds the context window of this model"
# - OpenAI/LiteLLM: "Requested token count exceeds the model's maximum context length of 131072 tokens"
# - OpenAI-compatible: "Input length (265330) exceeds model's maximum context length (262144)."
# - Google: "The input token count (1196265) exceeds the maximum number of tokens allowed (1048575)"
# - xAI: "This model's maximum prompt length is 131072 but the request contains 537812 tokens"
# - Groq: "Please reduce the length of the messages or completion"
# - OpenRouter: "This endpoint's maximum context length is X tokens. However, you requested about Y tokens"
# - OpenRouter/Poolside: "Input length X exceeds the maximum allowed input length of Y tokens."
# - Together AI: "The input (X tokens) is longer than the model's context length (Y tokens)."
# - llama.cpp: "the request exceeds the available context size, try increasing it"
# - LM Studio: "tokens to keep from the initial prompt is greater than the context length"
# - GitHub Copilot: "prompt token count of X exceeds the limit of Y"
# - MiniMax: "invalid params, context window exceeds limit"
# - Kimi For Coding: "Your request exceeded model token limit: X (requested: Y)"
# - DS4: "Prompt has X tokens, but the configured context size is Y tokens"
# - Cerebras: "400/413 status code (no body)"
# - Mistral: "Prompt contains X tokens ... too large for model with Y maximum context length"
# - z.ai: `{"code":"1261","message":"Prompt too long"}`, `{"code":"1261","message":"Prompt exceeds max length"}` (CN endpoint), or silent overflow via usage.input > contextWindow
# - Xiaomi MiMo: Truncates input to fill contextWindow exactly, then returns finish_reason "length"
#   with output=0 (no room left to generate). Detected via stopReason "length" + zero output +
#   input filling the context window.
# - DashScope/Qwen: "Range of input length should be [1, X]" (HTTP 400 invalid_parameter_error)
# - Ollama: Some deployments truncate silently, others return errors like "prompt too long; exceeded max context length by X tokens"
_OVERFLOW_PATTERNS = [
    re.compile(r"prompt (?:is )?too long", re.IGNORECASE),  # Anthropic and z.ai token overflow
    re.compile(r"prompt exceeds max length", re.IGNORECASE),  # z.ai CN endpoint token overflow
    re.compile(r"request_too_large", re.IGNORECASE),  # Anthropic request byte-size overflow (HTTP 413)
    re.compile(r"input is too long for requested model", re.IGNORECASE),  # Amazon Bedrock
    re.compile(r"exceeds the context window", re.IGNORECASE),  # OpenAI (Completions & Responses API)
    re.compile(
        r"exceeds (?:the )?(?:model'?s )?maximum context length(?: of [\d,]+ tokens?|\s*\([\d,]+\))", re.IGNORECASE
    ),  # OpenAI-compatible proxies (LiteLLM)
    re.compile(r"input token count.*exceeds the maximum", re.IGNORECASE),  # Google (Gemini)
    re.compile(r"maximum prompt length is \d+", re.IGNORECASE),  # xAI (Grok)
    re.compile(r"reduce the length of the messages", re.IGNORECASE),  # Groq
    re.compile(r"maximum context length is \d+ tokens", re.IGNORECASE),  # OpenRouter (most backends)
    re.compile(r"exceeds (?:the )?maximum allowed input length of [\d,]+ tokens?", re.IGNORECASE),  # OpenRouter/Poolside
    re.compile(
        r"input \(\d+ tokens\) is longer than the model'?s context length \(\d+ tokens\)", re.IGNORECASE
    ),  # Together AI
    re.compile(r"exceeds the limit of \d+", re.IGNORECASE),  # GitHub Copilot
    re.compile(r"exceeds the available context size", re.IGNORECASE),  # llama.cpp server
    re.compile(r"greater than the context length", re.IGNORECASE),  # LM Studio
    re.compile(r"context window exceeds limit", re.IGNORECASE),  # MiniMax
    re.compile(r"exceeded model token limit", re.IGNORECASE),  # Kimi For Coding
    re.compile(r"too large for model with \d+ maximum context length", re.IGNORECASE),  # Mistral
    re.compile(
        r"prompt has [\d,]+ tokens?, but the configured context size is [\d,]+ tokens?", re.IGNORECASE
    ),  # DS4 server
    re.compile(r"model_context_window_exceeded", re.IGNORECASE),  # z.ai non-standard finish_reason surfaced as error text
    re.compile(r"prompt too long; exceeded (?:max )?context length", re.IGNORECASE),  # Ollama explicit overflow error
    re.compile(r"range of input length should be", re.IGNORECASE),  # DashScope / Qwen Token Plan
    re.compile(r"context[_ ]length[_ ]exceeded", re.IGNORECASE),  # Generic fallback
    re.compile(r"too many tokens", re.IGNORECASE),  # Generic fallback
    re.compile(r"token limit exceeded", re.IGNORECASE),  # Generic fallback
]

_CEREBRAS_BODYLESS_OVERFLOW_PATTERN = re.compile(r"^4(?:00|13)\s*(?:status code)?\s*\(no body\)", re.IGNORECASE)

# Patterns that indicate non-overflow errors (e.g. rate limiting, server errors).
# Error messages matching any of these are excluded from overflow detection
# even if they also match an OVERFLOW_PATTERN.
#
# Example: Bedrock formats throttling errors as "ThrottlingException: Too many tokens,
# please wait before trying again." which would match the /too many tokens/i overflow
# pattern without this exclusion.
_NON_OVERFLOW_PATTERNS = [
    # AWS Bedrock non-overflow errors (human-readable prefixes from formatBedrockError)
    re.compile(r"^(Throttling error|Service unavailable):", re.IGNORECASE),
    re.compile(r"rate limit", re.IGNORECASE),  # Generic rate limiting
    re.compile(r"too many requests", re.IGNORECASE),  # Generic HTTP 429 style
]


def isContextOverflow(message: AssistantMessage, contextWindow: int | None = None) -> bool:
    """Check if an assistant message represents a context overflow error.

    This handles three cases:
    1. Error-based overflow: Most providers return stopReason "error" with a
       specific error message pattern.
    2. Silent overflow: Some providers accept overflow requests and return
       successfully. For these, we check if usage.input exceeds the context window.
    3. Length-stop overflow: Xiaomi MiMo can return "length" with zero output when
       the input fills the context window.

    ## Reliability by Provider

    **Reliable detection (returns error with detectable message):**
    - Anthropic: "prompt is too long: X tokens > Y maximum" or "request_too_large"
    - OpenAI (Completions & Responses): "exceeds the context window", "exceeds the model's maximum context length of X tokens", or "exceeds model's maximum context length (X)"
    - Google Gemini: "input token count exceeds the maximum"
    - xAI (Grok): "maximum prompt length is X but request contains Y"
    - Groq: "reduce the length of the messages"
    - Cerebras: 400/413 status code (no body)
    - Mistral: "Prompt contains X tokens ... too large for model with Y maximum context length"
    - OpenRouter (most backends): "maximum context length is X tokens"
    - OpenRouter/Poolside: "Input length X exceeds the maximum allowed input length of Y tokens."
    - Together AI: "The input (X tokens) is longer than the model's context length (Y tokens)."
    - llama.cpp: "exceeds the available context size"
    - LM Studio: "greater than the context length"
    - Kimi For Coding: "exceeded model token limit: X (requested: Y)"
    - DS4: "Prompt has X tokens, but the configured context size is Y tokens"
    - DashScope/Qwen: "Range of input length should be [1, X]"
    - z.ai: "Prompt too long"

    **Unreliable detection:**
    - z.ai: Sometimes accepts overflow silently (detectable via usage.input > contextWindow),
      sometimes returns rate limit errors. Pass contextWindow param to detect silent overflow.
    - Xiaomi MiMo: Truncates input to fit contextWindow then returns stopReason "length" with
      output=0. Pass contextWindow param to detect via the "filled context + zero output" signal.
    - Ollama: May truncate input silently for some setups, but may also return explicit
      overflow errors that match the patterns above. Silent truncation still cannot be
      detected here because we do not know the expected token count.

    ## Custom Providers

    If you've added custom models via settings.json, this function may not detect
    overflow errors from those providers. To add support:

    1. Send a request that exceeds the model's context window
    2. Check the errorMessage in the response
    3. Create a regex pattern that matches the error
    4. The pattern should be added to OVERFLOW_PATTERNS in this file, or
       check the errorMessage yourself before calling this function
    """
    # Case 1: Check error message patterns
    if message.stopReason == "error" and message.errorMessage:
        # Skip messages matching known non-overflow patterns (e.g. throttling / rate-limit)
        isNonOverflow = any(p.search(message.errorMessage) for p in _NON_OVERFLOW_PATTERNS)
        if not isNonOverflow:
            if any(p.search(message.errorMessage) for p in _OVERFLOW_PATTERNS):
                return True
            if message.provider == "cerebras" and _CEREBRAS_BODYLESS_OVERFLOW_PATTERN.search(message.errorMessage):
                return True

    # Case 2: Silent overflow (z.ai style) - successful but usage exceeds context
    if contextWindow and message.stopReason == "stop":
        inputTokens = message.usage.input + message.usage.cacheRead
        if inputTokens > contextWindow:
            return True

    # Case 3: Length-stop overflow (Xiaomi MiMo style) - server truncates oversized input
    # to fit the context window, leaving no room for output. Returns stopReason "length"
    # with output=0 and input+cacheRead filling the context window.
    if contextWindow and message.stopReason == "length" and message.usage.output == 0:
        inputTokens = message.usage.input + message.usage.cacheRead
        if inputTokens >= contextWindow * 0.99:
            return True

    return False


def isRecoverableLength(message: AssistantMessage, desiredMaxOutput: int) -> bool:
    """Check whether a length stop ended below the caller or model's intended output limit.
    Such responses may be caused by context pressure or provider-side truncation, so callers
    can make one bounded compact-and-retry attempt. `desiredMaxOutput` must be the original
    limit before any context-based clamping."""
    return message.stopReason == "length" and desiredMaxOutput > 0 and message.usage.output < desiredMaxOutput


def getOverflowPatterns() -> list[re.Pattern[str]]:
    """Get the overflow patterns for testing purposes."""
    return list(_OVERFLOW_PATTERNS)


__all__ = ["getOverflowPatterns", "isContextOverflow", "isRecoverableLength"]

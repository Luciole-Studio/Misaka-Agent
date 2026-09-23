"""Provider-specific context overflow detection heuristics."""

from __future__ import annotations

import re

from misaka.ai.types import AssistantMessage

_OVERFLOW_PATTERNS = [
    re.compile(r"prompt (?:is )?too long", re.IGNORECASE),  # Anthropic and z.ai token overflow
    re.compile(r"request_too_large", re.IGNORECASE),
    re.compile(r"input is too long for requested model", re.IGNORECASE),
    re.compile(r"exceeds the context window", re.IGNORECASE),
    re.compile(
        r"exceeds (?:the )?(?:model'?s )?maximum context length(?: of [\d,]+ tokens?|\s*\([\d,]+\))",
        re.IGNORECASE,
    ),
    re.compile(r"input token count.*exceeds the maximum", re.IGNORECASE),
    re.compile(r"maximum prompt length is \d+", re.IGNORECASE),
    re.compile(r"reduce the length of the messages", re.IGNORECASE),
    re.compile(r"maximum context length is \d+ tokens", re.IGNORECASE),
    re.compile(r"exceeds (?:the )?maximum allowed input length of [\d,]+ tokens?", re.IGNORECASE),
    re.compile(r"input \(\d+ tokens\) is longer than the model'?s context length \(\d+ tokens\)", re.IGNORECASE),
    re.compile(r"exceeds the limit of \d+", re.IGNORECASE),
    re.compile(r"exceeds the available context size", re.IGNORECASE),
    re.compile(r"greater than the context length", re.IGNORECASE),
    re.compile(r"context window exceeds limit", re.IGNORECASE),
    re.compile(r"exceeded model token limit", re.IGNORECASE),
    re.compile(r"too large for model with \d+ maximum context length", re.IGNORECASE),
    re.compile(r"prompt has [\d,]+ tokens?, but the configured context size is [\d,]+ tokens?", re.IGNORECASE),
    re.compile(r"model_context_window_exceeded", re.IGNORECASE),
    re.compile(r"prompt too long; exceeded (?:max )?context length", re.IGNORECASE),
    re.compile(r"range of input length should be", re.IGNORECASE),
    re.compile(r"context[_ ]length[_ ]exceeded", re.IGNORECASE),
    re.compile(r"too many tokens", re.IGNORECASE),
    re.compile(r"token limit exceeded", re.IGNORECASE),
]
# Cerebras: 400/413 with no body. Bodyless 400/413 from other providers are not overflow
# (pi #9482), so this one is checked against the message's provider.
_CEREBRAS_BODYLESS_OVERFLOW_PATTERN = re.compile(r"^4(?:00|13)\s*(?:status code)?\s*\(no body\)", re.IGNORECASE)

_NON_OVERFLOW_PATTERNS = [
    re.compile(r"^(Throttling error|Service unavailable):", re.IGNORECASE),
    re.compile(r"rate limit", re.IGNORECASE),
    re.compile(r"too many requests", re.IGNORECASE),
]


def is_context_overflow(message: AssistantMessage, context_window: int | None = None) -> bool:
    if message.stopReason == "error" and message.errorMessage:
        is_non_overflow = any(pattern.search(message.errorMessage) for pattern in _NON_OVERFLOW_PATTERNS)
        if not is_non_overflow:
            if any(pattern.search(message.errorMessage) for pattern in _OVERFLOW_PATTERNS):
                return True
            if message.provider == "cerebras" and _CEREBRAS_BODYLESS_OVERFLOW_PATTERN.search(message.errorMessage):
                return True

    if context_window and message.stopReason == "stop":
        input_tokens = message.usage.input + message.usage.cacheRead
        if input_tokens > context_window:
            return True

    if context_window and message.stopReason == "length" and message.usage.output == 0:
        input_tokens = message.usage.input + message.usage.cacheRead
        if input_tokens >= context_window * 0.99:
            return True

    return False


def is_recoverable_length(message: AssistantMessage, desired_max_output: int) -> bool:
    """True when a ``length`` stop is a recoverable truncation.

    Output below the model's raw output cap means context pressure or a
    provider-side cut, so the caller may do one bounded compact-and-retry.
    ``desired_max_output`` must be the original, unclamped cap
    (pi overflow.ts isRecoverableLength, #7540/32850ef7c).
    """
    return (message.stopReason == "length" and desired_max_output > 0
            and message.usage.output < desired_max_output)


OUTPUT_LIMIT_MARK = "output token limit"


def _field(value, name, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def output_limit_error(message) -> str:
    """What a ``length`` stop means to the caller, in words the model can act on.

    pi's own recovery covers a reply cut *below* the output cap (``is_recoverable_length``);
    a reply that spent the whole cap -- adaptive thinking at effort max can -- is not retried
    by pi and used to reach research callers as "request length", which Last Order read as
    "the request was too long" and shrank her plan. Name the cause and the fact that no tool
    call happened, so the driver can nudge one more turn and the model knows what to do.
    """
    usage = _field(message, "usage")
    output = _field(usage, "output", 0) or 0
    return (f"the reply hit the model's {OUTPUT_LIMIT_MARK} ({output} tokens) before it finished; "
            "no tool call was made")


def hit_output_limit(error) -> bool:
    return bool(error) and OUTPUT_LIMIT_MARK in str(error)


__all__ = [
    "hit_output_limit",
    "is_context_overflow",
    "is_recoverable_length",
    "output_limit_error",
]

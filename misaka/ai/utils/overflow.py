"""Provider-specific context overflow detection heuristics."""

from __future__ import annotations

import re

from misaka.ai.types import AssistantMessage

_OVERFLOW_PATTERNS = [
    re.compile(r"prompt is too long", re.IGNORECASE),
    re.compile(r"request_too_large", re.IGNORECASE),
    re.compile(r"input is too long for requested model", re.IGNORECASE),
    re.compile(r"exceeds the context window", re.IGNORECASE),
    re.compile(r"exceeds (?:the )?(?:model'?s )?maximum context length of [\d,]+ tokens?", re.IGNORECASE),
    re.compile(r"input token count.*exceeds the maximum", re.IGNORECASE),
    re.compile(r"maximum prompt length is \d+", re.IGNORECASE),
    re.compile(r"reduce the length of the messages", re.IGNORECASE),
    re.compile(r"maximum context length is \d+ tokens", re.IGNORECASE),
    re.compile(r"input \(\d+ tokens\) is longer than the model'?s context length \(\d+ tokens\)", re.IGNORECASE),
    re.compile(r"exceeds the limit of \d+", re.IGNORECASE),
    re.compile(r"exceeds the available context size", re.IGNORECASE),
    re.compile(r"greater than the context length", re.IGNORECASE),
    re.compile(r"context window exceeds limit", re.IGNORECASE),
    re.compile(r"exceeded model token limit", re.IGNORECASE),
    re.compile(r"too large for model with \d+ maximum context length", re.IGNORECASE),
    re.compile(r"model_context_window_exceeded", re.IGNORECASE),
    re.compile(r"prompt too long; exceeded (?:max )?context length", re.IGNORECASE),
    re.compile(r"context[_ ]length[_ ]exceeded", re.IGNORECASE),
    re.compile(r"too many tokens", re.IGNORECASE),
    re.compile(r"token limit exceeded", re.IGNORECASE),
    re.compile(r"^4(?:00|13)\s*(?:status code)?\s*\(no body\)", re.IGNORECASE),
]

_NON_OVERFLOW_PATTERNS = [
    re.compile(r"^(Throttling error|Service unavailable):", re.IGNORECASE),
    re.compile(r"rate limit", re.IGNORECASE),
    re.compile(r"too many requests", re.IGNORECASE),
]


def is_context_overflow(message: AssistantMessage, context_window: int | None = None) -> bool:
    if message.stopReason == "error" and message.errorMessage:
        is_non_overflow = any(pattern.search(message.errorMessage) for pattern in _NON_OVERFLOW_PATTERNS)
        if not is_non_overflow and any(pattern.search(message.errorMessage) for pattern in _OVERFLOW_PATTERNS):
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
    """length 停止且输出低于模型原始输出上限＝可恢复截断（上下文压力/供应商侧截断），
    调用方可做一次有界的 compact-and-retry。desired_max_output 必须是钳制前的原始上限。
    （pi overflow.ts isRecoverableLength，pre-pin 漏移植；#7540/32850ef7c）"""
    return (message.stopReason == "length" and desired_max_output > 0
            and message.usage.output < desired_max_output)


def get_overflow_patterns() -> list[re.Pattern[str]]:
    return list(_OVERFLOW_PATTERNS)


isContextOverflow = is_context_overflow
getOverflowPatterns = get_overflow_patterns

__all__ = [
    "getOverflowPatterns",
    "get_overflow_patterns",
    "isContextOverflow",
    "is_context_overflow",
]

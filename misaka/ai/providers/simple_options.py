"""Shared helpers for mapping simple reasoning options to provider options."""

from __future__ import annotations

from dataclasses import dataclass

from misaka.ai.types import Model, SimpleStreamOptions, StreamOptions, ThinkingBudgets, ThinkingLevel


def build_base_options(_model: Model, options: SimpleStreamOptions | None = None, api_key: str | None = None) -> StreamOptions:
    if options is None:
        return StreamOptions(apiKey=api_key or None)

    return StreamOptions(
        temperature=options.temperature,
        maxTokens=options.maxTokens,
        signal=options.signal,
        apiKey=api_key or options.apiKey,
        transport=options.transport,
        cacheRetention=options.cacheRetention,
        sessionId=options.sessionId,
        headers=options.headers,
        onPayload=options.onPayload,
        onResponse=options.onResponse,
        timeoutMs=options.timeoutMs,
        maxRetries=options.maxRetries,
        maxRetryDelayMs=options.maxRetryDelayMs,
        metadata=options.metadata,
    )


def clamp_reasoning(effort: ThinkingLevel | None) -> ThinkingLevel | None:
    return "high" if effort == "xhigh" else effort


@dataclass(frozen=True, slots=True)
class AdjustedThinkingTokens:
    maxTokens: int
    thinkingBudget: int


def adjust_max_tokens_for_thinking(
    base_max_tokens: int | None,
    model_max_tokens: int,
    reasoning_level: ThinkingLevel,
    custom_budgets: ThinkingBudgets | None = None,
) -> AdjustedThinkingTokens:
    default_budgets = ThinkingBudgets(minimal=1024, low=2048, medium=8192, high=16384)
    budgets = default_budgets.model_dump()
    if custom_budgets is not None:
        budgets.update({key: value for key, value in custom_budgets.model_dump().items() if value is not None})

    min_output_tokens = 1024
    level = clamp_reasoning(reasoning_level)
    if level is None:
        raise ValueError("reasoning_level must not be None")

    thinking_budget = budgets[level]
    if thinking_budget is None:
        raise ValueError(f"No thinking budget configured for reasoning level {reasoning_level}")

    max_tokens = model_max_tokens if base_max_tokens is None else min(base_max_tokens + thinking_budget, model_max_tokens)
    if max_tokens <= thinking_budget:
        thinking_budget = max(0, max_tokens - min_output_tokens)

    return AdjustedThinkingTokens(maxTokens=max_tokens, thinkingBudget=thinking_budget)


buildBaseOptions = build_base_options
clampReasoning = clamp_reasoning
adjustMaxTokensForThinking = adjust_max_tokens_for_thinking

__all__ = [
    "adjustMaxTokensForThinking",
    "buildBaseOptions",
    "clampReasoning",
]

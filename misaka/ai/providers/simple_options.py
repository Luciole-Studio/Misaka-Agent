"""Shared helpers for mapping simple reasoning options to provider options."""

from __future__ import annotations

from dataclasses import dataclass

from misaka.ai.types import (
    Model,
    SimpleStreamOptions,
    StreamOptions,
    ThinkingBudgets,
    ThinkingLevel,
    TranscriptContext,
)
from misaka.ai.utils.estimate import clamp_max_tokens_to_context


def build_base_options(
    model: Model,
    context: TranscriptContext,
    options: SimpleStreamOptions | None = None,
    api_key: str | None = None,
) -> StreamOptions:
    # Model-level sampling defaults merged under request-level ones, upstream's
    # `{...model.samplingParams, ...options?.samplingParams}` (api/simple-options.ts:27-33).
    # openai_completions additionally re-merges the same two sources itself with a
    # declared, deliberate twist (named request fields win there); the re-merge is
    # idempotent over this one, so both stand until the owner unifies them.
    model_sampling = model.samplingParams
    options_sampling = options.samplingParams if options is not None else None
    sampling = (
        {**(model_sampling or {}), **(options_sampling or {})}
        if (model_sampling or options_sampling)
        else None
    )
    # `?? model.maxTokens`, then clamped to what the window can still hold
    # (api/simple-options.ts:34). Passing the caller's value straight through meant a
    # request with no explicit maxTokens sent none at all -- and one near a full window
    # asked for more output than the provider could grant, failing instead of shrinking.
    requested_max = options.maxTokens if options is not None and options.maxTokens is not None else model.maxTokens
    max_tokens = clamp_max_tokens_to_context(model, context, requested_max)
    if options is None:
        return StreamOptions(apiKey=api_key or None, samplingParams=sampling, maxTokens=max_tokens)

    return StreamOptions(
        samplingParams=sampling,
        temperature=options.temperature,
        maxTokens=max_tokens,
        signal=options.signal,
        apiKey=api_key or options.apiKey,
        env=options.env,
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
    """Fold the two top levels onto a key the thinking-budget table actually has.

    This is a *lookup* clamp, not a wire restriction. The budget table below stops at
    `high`, so `budgets[level]` would raise on `xhigh` or `max`; both borrow high's budget
    instead. Upstream calls its `clampReasoning` in exactly the same two places and for
    the same reason (`api/simple-options.ts:70`, `api/bedrock-converse-stream.ts:550`).

    What this deliberately does *not* do is clamp the request path: `reasoningEffort`
    carries `xhigh`/`max` through to the provider.
    """
    return "high" if effort in ("xhigh", "max") else effort


MIN_ANSWER_TOKENS = 1024
"""Tokens always left for the answer when a thinking budget shares the response ceiling."""


def clamp_thinking_budget_to_answer_room(thinking_budget: int, ceiling: int) -> int:
    """Cap a thinking budget so at least MIN_ANSWER_TOKENS remain under a shared ceiling."""
    return min(thinking_budget, max(0, ceiling - MIN_ANSWER_TOKENS))


@dataclass(frozen=True, slots=True)
class AdjustedThinkingTokens:
    maxTokens: int
    thinkingBudget: int


DEFAULT_THINKING_BUDGETS = ThinkingBudgets(minimal=1024, low=2048, medium=8192, high=16384)


def thinking_budget_for_level(
    reasoning_level: ThinkingLevel, custom_budgets: ThinkingBudgets | None = None
) -> int:
    """The token budget one thinking level asks for, before any clamping.

    Its own function, as upstream's ``thinkingBudgetForLevel`` is: two callers need it --
    the Anthropic-style adjustment below and openai-completions' ``thinking_token_budget``
    field -- and the table has to be the same one for both.
    """
    budgets = DEFAULT_THINKING_BUDGETS.model_dump()
    if custom_budgets is not None:
        budgets.update({key: value for key, value in custom_budgets.model_dump().items() if value is not None})
    level = clamp_reasoning(reasoning_level)
    if level is None:
        raise ValueError("reasoning_level must not be None")
    budget = budgets[level]
    if budget is None:
        raise ValueError(f"No thinking budget configured for reasoning level {reasoning_level}")
    return budget


def adjust_max_tokens_for_thinking(
    base_max_tokens: int | None,
    model_max_tokens: int,
    reasoning_level: ThinkingLevel,
    custom_budgets: ThinkingBudgets | None = None,
) -> AdjustedThinkingTokens:
    thinking_budget = thinking_budget_for_level(reasoning_level, custom_budgets)

    max_tokens = model_max_tokens if base_max_tokens is None else min(base_max_tokens + thinking_budget, model_max_tokens)
    if max_tokens <= thinking_budget:
        thinking_budget = clamp_thinking_budget_to_answer_room(thinking_budget, max_tokens)

    return AdjustedThinkingTokens(maxTokens=max_tokens, thinkingBudget=thinking_budget)


__all__ = [
    "DEFAULT_THINKING_BUDGETS",
    "thinking_budget_for_level",
    ]

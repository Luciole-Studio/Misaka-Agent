"""Translated from pi's ``packages/ai/src/api/simple-options.ts`` (v1.1.0).

MISAKA fork: options are read through ``_common._option``, so any options object (a model or a plain mapping) works
and a missing field reads as undefined; ``buildBaseOptions`` returns a ``StreamOptions`` model, which has no
``fetch`` (Python has no fetch override). ``thinkingBudgetForLevel`` also takes budgets from a settings mapping and
raises where upstream's non-null assertion would hand back ``undefined``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from misaka.ai.api._common import _option
from misaka.ai.models import clampThinkingLevel
from misaka.ai.types import (
    Model,
    ModelThinkingLevel,
    SamplingParams,
    SimpleStreamOptions,
    StreamOptions,
    ThinkingBudgets,
    ThinkingLevel,
    TranscriptContext,
)
from misaka.ai.utils.estimate import estimateContextTokens

_CONTEXT_SAFETY_TOKENS = 4096
_MIN_MAX_TOKENS = 1


def clampMaxTokensToContext(model: Model, context: TranscriptContext, maxTokens: int) -> int:
    if model.contextWindow <= 0:
        return max(_MIN_MAX_TOKENS, maxTokens)
    available = model.contextWindow - estimateContextTokens(context).tokens - _CONTEXT_SAFETY_TOKENS
    return min(maxTokens, max(_MIN_MAX_TOKENS, available))


def resolveSamplingParams(
    model: Model,
    thinkingLevel: ModelThinkingLevel,
    requestParams: SamplingParams | None = None,
) -> SamplingParams | None:
    effectiveThinkingLevel = clampThinkingLevel(model, thinkingLevel)
    byThinkingLevel = model.samplingParamsByThinkingLevel
    thinkingLevelParams = byThinkingLevel.get(effectiveThinkingLevel) if byThinkingLevel is not None else None
    # `a || b || c` on objects: an empty object is truthy.
    return (
        {**(model.samplingParams or {}), **(thinkingLevelParams or {}), **(requestParams or {})}
        if model.samplingParams is not None or thinkingLevelParams is not None or requestParams is not None
        else None
    )


def buildBaseOptions(
    model: Model,
    context: TranscriptContext,
    options: SimpleStreamOptions | Any | None = None,
    apiKey: str | None = None,
) -> StreamOptions:
    reasoning = _option(options, "reasoning")
    samplingParams = resolveSamplingParams(
        model, reasoning if reasoning is not None else "off", _option(options, "samplingParams")
    )
    maxTokens = _option(options, "maxTokens")
    return StreamOptions(
        temperature=_option(options, "temperature"),
        samplingParams=samplingParams,
        maxTokens=clampMaxTokensToContext(model, context, maxTokens if maxTokens is not None else model.maxTokens),
        signal=_option(options, "signal"),
        telemetryContext=_option(options, "telemetryContext"),
        apiKey=apiKey or _option(options, "apiKey"),
        transport=_option(options, "transport"),
        cacheRetention=_option(options, "cacheRetention"),
        sessionId=_option(options, "sessionId"),
        headers=_option(options, "headers"),
        onPayload=_option(options, "onPayload"),
        onResponse=_option(options, "onResponse"),
        onProviderStreamEvent=_option(options, "onProviderStreamEvent"),
        timeoutMs=_option(options, "timeoutMs"),
        websocketConnectTimeoutMs=_option(options, "websocketConnectTimeoutMs"),
        maxRetries=_option(options, "maxRetries"),
        maxRetryDelayMs=_option(options, "maxRetryDelayMs"),
        metadata=_option(options, "metadata"),
        env=_option(options, "env"),
    )


# Tokens always left for the answer when a thinking budget shares the response ceiling.
MIN_ANSWER_TOKENS = 1024

DEFAULT_THINKING_BUDGETS = ThinkingBudgets(minimal=1024, low=2048, medium=8192, high=16384)


def clampReasoning(effort: ThinkingLevel | None) -> ThinkingLevel | None:
    return "high" if effort in ("xhigh", "max") else effort


def thinkingBudgetForLevel(
    reasoningLevel: ThinkingLevel, customBudgets: ThinkingBudgets | Mapping[str, Any] | None = None
) -> int:
    budgets = DEFAULT_THINKING_BUDGETS.model_dump()
    if customBudgets is not None:
        # `{ ...DEFAULT_THINKING_BUDGETS, ...customBudgets }`: a settings mapping spreads as well as the model.
        items = customBudgets.items() if isinstance(customBudgets, Mapping) else customBudgets.model_dump().items()
        budgets.update({key: value for key, value in items if key in budgets and value is not None})
    level = clampReasoning(reasoningLevel)
    if level is None:
        raise ValueError("reasoningLevel must not be None")
    budget = budgets[level]
    if budget is None:
        raise ValueError(f"No thinking budget configured for reasoning level {reasoningLevel}")
    return int(budget)


def clampThinkingBudgetToAnswerRoom(thinkingBudget: int, ceiling: int) -> int:
    """Cap a thinking budget so at least MIN_ANSWER_TOKENS remain under a shared response ceiling."""
    return min(thinkingBudget, max(0, ceiling - MIN_ANSWER_TOKENS))


@dataclass(frozen=True, slots=True)
class _AdjustedThinkingTokens:
    maxTokens: int
    thinkingBudget: int


def adjustMaxTokensForThinking(
    # Undefined means no explicit caller cap. Use the model cap and fit thinking inside it.
    baseMaxTokens: int | None,
    modelMaxTokens: int,
    reasoningLevel: ThinkingLevel,
    customBudgets: ThinkingBudgets | Mapping[str, Any] | None = None,
) -> _AdjustedThinkingTokens:
    thinkingBudget = thinkingBudgetForLevel(reasoningLevel, customBudgets)
    maxTokens = modelMaxTokens if baseMaxTokens is None else min(baseMaxTokens + thinkingBudget, modelMaxTokens)

    if maxTokens <= thinkingBudget:
        thinkingBudget = clampThinkingBudgetToAnswerRoom(thinkingBudget, maxTokens)

    return _AdjustedThinkingTokens(maxTokens=maxTokens, thinkingBudget=thinkingBudget)


__all__ = [
    "DEFAULT_THINKING_BUDGETS",
    "MIN_ANSWER_TOKENS",
    "adjustMaxTokensForThinking",
    "buildBaseOptions",
    "clampMaxTokensToContext",
    "clampReasoning",
    "clampThinkingBudgetToAnswerRoom",
    "resolveSamplingParams",
    "thinkingBudgetForLevel",
]

"""Translated from pi's ``packages/ai/test/context-estimate.test.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ai.api.simple_options import buildBaseOptions
from misaka.ai.types import (
    AssistantMessage,
    Model,
    ModelCost,
    TextContent,
    Usage,
    UsageCost,
    UserMessage,
)
from misaka.ai.utils.estimate import ContextUsageEstimate, estimateContextTokens
from misaka.ai.utils.transcript import normalizeContext


def createUsage(totalTokens: int) -> Usage:
    return Usage(
        input=totalTokens,
        output=0,
        cacheRead=0,
        cacheWrite=0,
        totalTokens=totalTokens,
        cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
    )


def createAssistant(timestamp: int, totalTokens: int) -> AssistantMessage:
    return AssistantMessage(
        role="assistant",
        content=[TextContent(text="kept")],
        api="openai-responses",
        provider="openai",
        model="test-model",
        usage=createUsage(totalTokens),
        stopReason="stop",
        timestamp=timestamp,
    )


model = Model(
    id="test-model",
    name="Test Model",
    api="openai-responses",
    provider="openai",
    baseUrl="https://api.openai.com/v1",
    reasoning=False,
    input=["text"],
    cost=ModelCost(input=0, output=0, cacheRead=0, cacheWrite=0),
    contextWindow=10_000,
    maxTokens=8_000,
)


def test_reserves_3_5_characters_per_token_for_new_text_when_limiting_output():
    # Regression for #10497: large new inputs need more room than chars/4 allows.
    context = normalizeContext(
        {"messages": [createAssistant(100, 2_000), UserMessage(content="x" * 3_500, timestamp=200)]}
    )

    assert estimateContextTokens(context) == ContextUsageEstimate(
        tokens=3_000, usageTokens=2_000, trailingTokens=1_000, lastUsageIndex=0
    )
    assert buildBaseOptions(model, context).maxTokens == 2_904


def test_ignores_stale_assistant_usage_after_a_newer_message_is_inserted_before_it():
    context = normalizeContext(
        {
            "systemPrompt": "system",
            "messages": [
                UserMessage(content="summary", timestamp=200),
                createAssistant(100, 9_500),
                UserMessage(content="x" * 4_000, timestamp=300),
            ],
        }
    )

    assert estimateContextTokens(context) == ContextUsageEstimate(
        tokens=1_149, usageTokens=0, trailingTokens=1_149, lastUsageIndex=None
    )
    assert buildBaseOptions(model, context).maxTokens == 4_755


def test_uses_assistant_usage_again_after_a_response_to_the_inserted_context():
    context = normalizeContext(
        {
            "messages": [
                UserMessage(content="summary", timestamp=200),
                createAssistant(100, 9_500),
                UserMessage(content="new prompt", timestamp=300),
                createAssistant(400, 2_000),
                UserMessage(content="tail", timestamp=500),
            ]
        }
    )

    assert estimateContextTokens(context) == ContextUsageEstimate(
        tokens=2_002, usageTokens=2_000, trailingTokens=2, lastUsageIndex=3
    )

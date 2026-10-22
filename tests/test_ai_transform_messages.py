"""Translated from pi's ``packages/ai/test/{transform-messages-copilot-openai-to-anthropic,lax-message-content}.test.ts``
(v1.3.0)."""

from __future__ import annotations

import re
import time
from typing import Any

from misaka.ai.api.transform_messages import transformMessages
from misaka.ai.types import (
    AssistantMessage,
    Model,
    TextContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UsageCost,
    UserMessage,
)
from misaka.utils.js import jsonStringify


def _now() -> int:
    return time.time_ns() // 1_000_000


def _usage() -> Usage:
    return Usage(
        input=0,
        output=0,
        cacheRead=0,
        cacheWrite=0,
        totalTokens=0,
        cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
    )


# --- transform-messages-copilot-openai-to-anthropic.test.ts -------------------------------------


# Normalize function matching what anthropic.ts uses
def anthropicNormalizeToolCallId(id: str, _model: Model, _source: AssistantMessage) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", id)[:64]


def makeCopilotClaudeModel() -> Model:
    return Model.model_validate(
        {
            "id": "claude-sonnet-4.6",
            "name": "Claude Sonnet 4.6",
            "api": "anthropic-messages",
            "provider": "github-copilot",
            "baseUrl": "https://api.individual.githubcopilot.com",
            "reasoning": True,
            "input": ["text", "image"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 128000,
            "maxTokens": 16000,
        }
    )


def makeAssistantMessage(content: list[Any]) -> AssistantMessage:
    return AssistantMessage(
        content=content,
        api="openai-responses",
        provider="github-copilot",
        model="gpt-5",
        usage=_usage(),
        stopReason="toolUse",
        timestamp=_now(),
    )


def test_removes_thought_signature_from_tool_calls_when_migrating_between_models():
    model = makeCopilotClaudeModel()
    messages = [
        UserMessage(content="run a command", timestamp=_now()),
        AssistantMessage(
            content=[
                ToolCall(
                    id="call_123",
                    name="bash",
                    arguments={"command": "ls"},
                    thoughtSignature=jsonStringify({"type": "reasoning.encrypted", "id": "call_123", "data": "encrypted"}),
                )
            ],
            api="openai-responses",
            provider="github-copilot",
            model="gpt-5",
            usage=_usage(),
            stopReason="toolUse",
            timestamp=_now(),
        ),
        ToolResultMessage(
            toolCallId="call_123", toolName="bash", content=[TextContent(text="output")], isError=False, timestamp=_now()
        ),
    ]

    result = transformMessages(messages, model, anthropicNormalizeToolCallId)
    assistantMsg = next(message for message in result if message.role == "assistant")
    toolCall = next(block for block in assistantMsg.content if block.type == "toolCall")

    assert toolCall.thoughtSignature is None


def test_adds_synthetic_tool_results_for_trailing_orphaned_tool_calls():
    model = makeCopilotClaudeModel()
    messages = [
        UserMessage(content="read the file", timestamp=_now()),
        makeAssistantMessage([ToolCall(id="call_123|fc_123", name="read", arguments={"path": "README.md"})]),
    ]

    result = transformMessages(messages, model, anthropicNormalizeToolCallId)
    lastMessage = result[-1]

    assert lastMessage.role == "toolResult"
    assert lastMessage.toolCallId == "call_123_fc_123"
    assert lastMessage.toolName == "read"
    assert lastMessage.isError is True
    assert [block.model_dump(exclude_none=True) for block in lastMessage.content] == [
        {"type": "text", "text": "No result provided"}
    ]


def test_adds_synthetic_results_only_for_trailing_tool_calls_that_are_still_missing_results():
    model = makeCopilotClaudeModel()
    messages = [
        UserMessage(content="run commands", timestamp=_now()),
        makeAssistantMessage([
            ToolCall(id="call_1|fc_1", name="read", arguments={"path": "README.md"}),
            ToolCall(id="call_2|fc_2", name="bash", arguments={"command": "pwd"}),
        ]),
        ToolResultMessage(
            toolCallId="call_1|fc_1", toolName="read", content=[TextContent(text="done")], isError=False, timestamp=_now()
        ),
    ]

    result = transformMessages(messages, model, anthropicNormalizeToolCallId)
    syntheticResults = [message for message in result if message.role == "toolResult" and message.isError]

    assert len(syntheticResults) == 1
    assert syntheticResults[0].role == "toolResult"
    assert syntheticResults[0].toolCallId == "call_2_fc_2"
    assert syntheticResults[0].toolName == "bash"
    assert [block.model_dump(exclude_none=True) for block in syntheticResults[0].content] == [
        {"type": "text", "text": "No result provided"}
    ]


# --- lax-message-content.test.ts ----------------------------------------------------------------
# The Message types require `content` to always be present, but untyped callers (custom tools, hand-built histories,
# old session files) can violate that contract. `transformMessages` is the choke point before every provider request
# and is intentionally lax: it normalizes null/missing content to an empty array (issues #6259, #6276).


# Text-only model so the image downgrade path (replaceImagesWithPlaceholder) runs,
# which was the primary crash site for null tool result content.
def makeTextOnlyModel() -> Model:
    return Model.model_validate(
        {
            "id": "test-model",
            "name": "Test Model",
            "api": "openai-completions",
            "provider": "openai",
            "baseUrl": "https://example.invalid/v1",
            "reasoning": False,
            "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 128000,
            "maxTokens": 16000,
        }
    )


def test_normalizes_null_or_missing_content_to_an_empty_array_instead_of_crashing():
    # `as unknown as Message[]`: built without validation, as an untyped caller would.
    messages = [
        UserMessage.model_construct(role="user", content=None, timestamp=_now()),
        AssistantMessage.model_construct(
            role="assistant",
            content=None,
            api="openai-completions",
            provider="openai",
            model="test-model",
            usage=_usage(),
            stopReason="stop",
            timestamp=_now(),
        ),
        ToolResultMessage.model_construct(
            role="toolResult", toolCallId="call_1", toolName="web_search", isError=False, timestamp=_now()
        ),
    ]

    result = transformMessages(messages, makeTextOnlyModel())

    assert len(result) == 3
    for msg in result:
        assert msg.content == []

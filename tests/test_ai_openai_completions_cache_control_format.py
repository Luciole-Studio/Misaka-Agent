"""Translated from pi's ``packages/ai/test/openai-completions-cache-control-format.test.ts`` (v1.1.0)."""

from __future__ import annotations

import time
from typing import Any

import pytest

from misaka.ai.api.openai_completions import stream as streamOpenAICompletions
from misaka.ai.compat import getModel
from misaka.ai.types import (
    AssistantMessage,
    Context,
    Model,
    TextContent,
    Tool,
    ToolCall,
    ToolResultMessage,
    Usage,
    UsageCost,
    UserMessage,
)
from misaka.ai.utils.transcript import normalizeContext
from tests.openai_fake import installFakeOpenAI

DEFAULT_CHUNKS = [
    {
        "id": "chatcmpl-test",
        "choices": [{"delta": {}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "prompt_tokens_details": {"cached_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": 0},
        },
    }
]


@pytest.fixture
def mockState(monkeypatch):
    return installFakeOpenAI(monkeypatch, DEFAULT_CHUNKS)


async def capturePayload(mockState: Any, model: Model, options: dict[str, Any] | None = None, messages: list[Any] | None = None):
    timestamp = time.time_ns() // 1_000_000

    await streamOpenAICompletions(
        model,
        normalizeContext(
            Context(
                systemPrompt="System prompt",
                messages=messages if messages is not None else [UserMessage(content="Hello", timestamp=timestamp)],
                tools=[
                    Tool(
                        name="read",
                        description="Read a file",
                        parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                    )
                ],
            )
        ),
        {"apiKey": "test-key", **(options or {})},
    ).result()

    if not mockState.lastParams:
        raise AssertionError("Expected payload to be captured")

    return mockState.lastParams


def getInstructionMessage(params: dict[str, Any]) -> dict[str, Any] | None:
    return next((message for message in params["messages"] if message["role"] in ("system", "developer")), None)


def expectAnthropicCacheMarkers(params: dict[str, Any]) -> None:
    instructionMessage = getInstructionMessage(params)
    assert instructionMessage is not None
    assert isinstance(instructionMessage["content"], list)
    assert instructionMessage["content"][0]["cache_control"] == {"type": "ephemeral"}

    assert len(params["tools"]) == 1
    assert params["tools"][0]["cache_control"] == {"type": "ephemeral"}

    lastMessage = params["messages"][-1]
    assert lastMessage["role"] == "user"
    assert isinstance(lastMessage["content"], list)
    assert lastMessage["content"][0]["cache_control"] == {"type": "ephemeral"}


def _customQwen() -> Model:
    return Model.model_validate(
        {
            "id": "custom-qwen",
            "name": "Custom Qwen",
            "api": "openai-completions",
            "provider": "openrouter",
            "baseUrl": "https://example.com/v1",
            "reasoning": True,
            "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 128000,
            "maxTokens": 32000,
            "compat": {"cacheControlFormat": "anthropic"},
        }
    )


async def test_applies_anthropic_style_cache_markers_when_model_compat_enables_them(mockState):
    params = await capturePayload(mockState, _customQwen())
    expectAnthropicCacheMarkers(params)


async def test_preserves_anthropic_style_cache_markers_for_openrouter_anthropic_batch_aliases(mockState):
    params = await capturePayload(mockState, getModel("openrouter", "anthropic/claude-fable-5.1:batch"))
    expectAnthropicCacheMarkers(params)


async def test_moves_the_conversation_cache_marker_to_a_tool_result(mockState):
    model = getModel("openrouter", "anthropic/claude-fable-5.1:batch")
    timestamp = time.time_ns() // 1_000_000
    params = await capturePayload(
        mockState,
        model,
        None,
        [
            UserMessage(content="Read the file", timestamp=timestamp),
            AssistantMessage(
                content=[ToolCall(id="call_1", name="read", arguments={"path": "README.md"})],
                api="openai-completions",
                provider="openrouter",
                model=model.id,
                usage=Usage(
                    input=0,
                    output=0,
                    cacheRead=0,
                    cacheWrite=0,
                    totalTokens=0,
                    cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
                ),
                stopReason="toolUse",
                timestamp=timestamp,
            ),
            ToolResultMessage(
                toolCallId="call_1",
                toolName="read",
                content=[TextContent(text="file contents")],
                isError=False,
                timestamp=timestamp,
            ),
        ],
    )

    userMessage = next(message for message in params["messages"] if message["role"] == "user")
    assert userMessage["content"] == "Read the file"

    toolMessage = params["messages"][-1]
    assert toolMessage["role"] == "tool"
    assert isinstance(toolMessage["content"], list)
    assert toolMessage["content"][0]["cache_control"] == {"type": "ephemeral"}


async def test_omits_anthropic_style_cache_markers_when_cache_retention_is_none(mockState):
    params = await capturePayload(mockState, _customQwen(), {"cacheRetention": "none"})
    instructionMessage = getInstructionMessage(params)

    assert not isinstance(instructionMessage["content"], list)
    assert "cache_control" not in params["tools"][0]
    assert isinstance(params["messages"][-1]["content"], str)

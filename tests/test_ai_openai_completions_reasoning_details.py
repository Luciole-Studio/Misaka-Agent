"""Translated from pi's ``packages/ai/test/openai-completions-reasoning-details.test.ts`` (v1.3.0)."""

from __future__ import annotations

from typing import Any

import pytest

from misaka.ai.api.openai_completions import stream as streamOpenAICompletions
from misaka.ai.types import AssistantMessage, Context, Model, Tool
from misaka.ai.utils.transcript import normalizeContext
from misaka.utils.js import jsonStringify
from tests.openai_fake import installFakeOpenAI

reasoningDetail = {"type": "reasoning.encrypted", "id": "call_1", "data": "encrypted-signature"}
signedReasoningTextDetail = {
    "type": "reasoning.text",
    "text": "I should call the read tool.",
    "signature": "sha256:signed-text",
    "id": "reasoning-text-1",
    "format": "anthropic-claude-v1",
    "index": 0,
}
reasoningSummaryDetail = {
    "type": "reasoning.summary",
    "summary": "Decided to inspect the requested file.",
    "id": "reasoning-summary-1",
    "format": "anthropic-claude-v1",
    "index": 1,
}
readTool = Tool(
    name="read",
    description="Read a file",
    parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
)


@pytest.fixture
def mockState(monkeypatch):
    fake = installFakeOpenAI(monkeypatch)
    fake.chunkSets = []
    fake.create = lambda _resource, _params, _requestOptions: fake.chunkSets.pop(0) if fake.chunkSets else []
    return fake


def model() -> Model:
    return Model.model_validate(
        {
            "id": "google/gemini-test",
            "name": "Gemini Test",
            "api": "openai-completions",
            "provider": "openrouter",
            "baseUrl": "https://openrouter.ai/api/v1",
            "reasoning": True,
            "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 100_000,
            "maxTokens": 4096,
        }
    )


def chunk(delta: dict[str, Any], finishReason: str | None = None) -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "model": "google/gemini-test",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finishReason}],
    }


def toolCallChunk() -> dict[str, Any]:
    return chunk(
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"path":"README.md"}'},
                }
            ]
        }
    )


async def runOpenAICompletionsStream(messages: list[AssistantMessage] | None = None) -> AssistantMessage:
    return await streamOpenAICompletions(
        model(), normalizeContext(Context(messages=messages or [], tools=[readTool])), {"apiKey": "test"}
    ).result()


def getAssistantPayload(payload: Any) -> dict[str, Any] | None:
    return next((message for message in payload.get("messages") or [] if message.get("role") == "assistant"), None)


def _block(message: AssistantMessage, type: str) -> dict[str, Any] | None:
    block = next((block for block in message.content if block.type == type), None)
    return None if block is None else block.model_dump(exclude_none=True)


async def test_preserves_reasoning_details_in_the_thinking_signature(mockState):
    mockState.chunkSets = [
        [chunk({"reasoning_details": [reasoningDetail]}), toolCallChunk(), chunk({}, "tool_calls")],
        [chunk({"content": "ok"}), chunk({}, "stop")],
    ]

    assistantMessage = await runOpenAICompletionsStream()
    assert _block(assistantMessage, "thinking") == {
        "type": "thinking",
        "thinking": "",
        "thinkingSignature": jsonStringify([reasoningDetail]),
    }
    assert _block(assistantMessage, "toolCall") == {
        "type": "toolCall",
        "id": "call_1",
        "name": "read",
        "arguments": {"path": "README.md"},
    }

    await runOpenAICompletionsStream([assistantMessage])

    assert getAssistantPayload(mockState.requests[1])["reasoning_details"] == [reasoningDetail]


async def test_falls_back_to_encrypted_tool_call_signatures_for_older_stored_assistant_messages(mockState):
    mockState.chunkSets = [
        [chunk({"reasoning_details": [reasoningDetail]}), toolCallChunk(), chunk({}, "tool_calls")],
        [chunk({"content": "ok"}), chunk({}, "stop")],
    ]

    assistantMessage = await runOpenAICompletionsStream()
    assistantMessage.content = [block for block in assistantMessage.content if block.type != "thinking"]
    toolCall = next(block for block in assistantMessage.content if block.type == "toolCall")
    toolCall.thoughtSignature = jsonStringify(reasoningDetail)

    await runOpenAICompletionsStream([assistantMessage])

    assert getAssistantPayload(mockState.requests[1])["reasoning_details"] == [reasoningDetail]


async def test_preserves_signed_text_and_summary_reasoning_details_in_their_original_sequence(mockState):
    mockState.chunkSets = [
        [
            chunk({"reasoning": signedReasoningTextDetail["text"], "reasoning_details": [signedReasoningTextDetail]}),
            chunk({"reasoning_details": [reasoningDetail, reasoningSummaryDetail]}),
            toolCallChunk(),
            chunk({}, "tool_calls"),
        ],
        [chunk({"content": "ok"}), chunk({}, "stop")],
    ]

    assistantMessage = await runOpenAICompletionsStream()
    expectedReasoningDetails = [signedReasoningTextDetail, reasoningDetail, reasoningSummaryDetail]
    assert _block(assistantMessage, "thinking") == {
        "type": "thinking",
        "thinking": signedReasoningTextDetail["text"],
        "thinkingSignature": jsonStringify(expectedReasoningDetails),
    }

    await runOpenAICompletionsStream([assistantMessage])

    payload = getAssistantPayload(mockState.requests[1])
    assert payload["reasoning_details"] == expectedReasoningDetails
    assert "reasoning" not in payload


async def test_merges_consecutive_text_and_summary_reasoning_details_deltas_before_replay(mockState):
    textDelta = {"type": "reasoning.text", "text": "The", "index": 0}
    textDeltaWithSignature = {
        "type": "reasoning.text",
        "text": " user wants the time.",
        "signature": "sha256:text-signature",
        "format": "openai-responses-v1",
        "index": 0,
    }
    summaryDelta = {"type": "reasoning.summary", "summary": "Looked", "index": 0}
    summaryDeltaWithFormat = {"type": "reasoning.summary", "summary": " up time.", "format": "openai-responses-v1", "index": 0}
    laterSummaryDelta = {
        "type": "reasoning.summary",
        "summary": "After encrypted block.",
        "format": "openai-responses-v1",
        "index": 0,
    }
    expectedReasoningDetails = [
        {
            "type": "reasoning.text",
            "text": "The user wants the time.",
            "index": 0,
            "signature": "sha256:text-signature",
            "format": "openai-responses-v1",
        },
        {"type": "reasoning.summary", "summary": "Looked up time.", "index": 0, "format": "openai-responses-v1"},
        reasoningDetail,
        laterSummaryDelta,
    ]

    mockState.chunkSets = [
        [
            chunk({"reasoning_details": [textDelta]}),
            chunk({"reasoning_details": [textDeltaWithSignature]}),
            chunk({"reasoning_details": [summaryDelta]}),
            chunk({"reasoning_details": [summaryDeltaWithFormat]}),
            chunk({"reasoning_details": [reasoningDetail]}),
            chunk({"reasoning_details": [laterSummaryDelta]}),
            toolCallChunk(),
            chunk({}, "tool_calls"),
        ],
        [chunk({"content": "ok"}), chunk({}, "stop")],
    ]

    assistantMessage = await runOpenAICompletionsStream()
    assert _block(assistantMessage, "thinking") == {
        "type": "thinking",
        "thinking": "",
        "thinkingSignature": jsonStringify(expectedReasoningDetails),
    }

    await runOpenAICompletionsStream([assistantMessage])

    assert getAssistantPayload(mockState.requests[1])["reasoning_details"] == expectedReasoningDetails


# --- openai-completions reasoning and content in one delta ---------------------------------------


# https://github.com/earendil-works/pi/issues/10926
async def test_places_thinking_before_text_when_one_delta_carries_reasoning_content_and_content(mockState):
    mockState.chunkSets = [[chunk({"role": "assistant", "reasoning_content": "thinking", "content": "answer"}, "stop")]]

    assistantMessage = await runOpenAICompletionsStream()
    assert [block.model_dump(exclude_none=True) for block in assistantMessage.content] == [
        {"type": "thinking", "thinking": "thinking", "thinkingSignature": "reasoning_content"},
        {"type": "text", "text": "answer"},
    ]


async def test_places_thinking_before_text_when_one_delta_carries_reasoning_details_and_content(mockState):
    mockState.chunkSets = [[chunk({"reasoning_details": [reasoningDetail], "content": "answer"}, "stop")]]

    assistantMessage = await runOpenAICompletionsStream()
    assert [block.type for block in assistantMessage.content] == ["thinking", "text"]

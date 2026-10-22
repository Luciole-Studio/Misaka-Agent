"""Translated from pi's ``packages/coding-agent/test/compaction-serialization.test.ts`` and
``compaction-summary-reasoning.test.ts`` (v1.3.0)."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.types import AssistantMessage, Model, SimpleStreamOptions
from misaka.ai.utils.transcript import normalizeContext
from misaka.core.compaction import (
    CompactionPreparation,
    CompactionSettings,
    compact,
    completeSummarization,
    generateSummary,
    generateSummaryWithUsage,
)
from misaka.core.compaction import compaction as compaction_module
from misaka.core.compaction.utils import FileOperations, serializeConversation
from misaka.modes.json_event import to_jsonable
from misaka.utils.js import jsonStringify
from misaka.utils.values import read_field


def nowMs() -> int:
    return int(time.time() * 1000)


# --- serializeConversation ----------------------------------------------------------------------


def toolResult(text: str) -> dict[str, Any]:
    return {
        "role": "toolResult",
        "toolCallId": "tc1",
        "toolName": "read",
        "content": [{"type": "text", "text": text}],
        "isError": False,
        "timestamp": nowMs(),
    }


def test_should_truncate_long_tool_results():
    result = serializeConversation([toolResult("x" * 5000)])

    assert "[Tool result]:" in result
    assert "[... 3000 more characters truncated]" in result
    assert "x" * 3000 not in result
    # First 2000 chars should be present
    assert "x" * 2000 in result


def test_should_not_truncate_short_tool_results():
    shortContent = "x" * 1500

    result = serializeConversation([toolResult(shortContent)])

    assert result == f"[Tool result]: {shortContent}"
    assert "truncated" not in result


def test_should_not_truncate_assistant_or_user_messages():
    longText = "y" * 5000
    messages = [
        {"role": "user", "content": [{"type": "text", "text": longText}], "timestamp": nowMs()},
        AssistantMessage.model_validate({
            "role": "assistant",
            "content": [{"type": "text", "text": longText}],
            "api": "anthropic",
            "provider": "anthropic",
            "model": "test",
            "usage": {
                "input": 0,
                "output": 0,
                "cacheRead": 0,
                "cacheWrite": 0,
                "totalTokens": 0,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
            },
            "stopReason": "stop",
            "timestamp": nowMs(),
        }),
    ]

    result = serializeConversation(messages)

    assert "truncated" not in result
    assert longText in result


def test_should_omit_thinking():
    messages = [
        AssistantMessage.model_validate({
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "private reasoning", "thinkingSignature": "sig"},
                {"type": "text", "text": "visible reply"},
                {"type": "toolCall", "id": "tc1", "name": "read", "arguments": {"path": "a.ts"}},
            ],
            "api": "anthropic",
            "provider": "anthropic",
            "model": "test",
            "usage": {
                "input": 0,
                "output": 0,
                "cacheRead": 0,
                "cacheWrite": 0,
                "totalTokens": 0,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
            },
            "stopReason": "toolUse",
            "timestamp": nowMs(),
        }),
    ]

    result = serializeConversation(messages)

    assert result == '[Assistant]: visible reply\n\n[Assistant tool calls]: read(path="a.ts")'


# --- generateSummary reasoning options ----------------------------------------------------------


def createModel(reasoning: bool, maxTokens: int = 8192, compat: Any = None) -> Model:
    return Model.model_validate({
        "id": "reasoning-model" if reasoning else "non-reasoning-model",
        "name": "Reasoning Model" if reasoning else "Non-reasoning Model",
        "api": "anthropic-messages",
        "provider": "anthropic",
        "baseUrl": "https://api.anthropic.com",
        "reasoning": reasoning,
        "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 200000,
        "maxTokens": maxTokens,
        **({"compat": compat} if compat else {}),
    })


SUMMARY_USAGE = {
    "input": 10,
    "output": 10,
    "cacheRead": 0,
    "cacheWrite": 0,
    "totalTokens": 20,
    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
}

mockSummaryResponse = AssistantMessage.model_validate({
    "role": "assistant",
    "content": [{"type": "text", "text": "## Goal\nTest summary"}],
    "api": "anthropic-messages",
    "provider": "anthropic",
    "model": "claude-sonnet-4-5",
    "usage": SUMMARY_USAGE,
    "stopReason": "stop",
    "timestamp": nowMs(),
})

mockToolCallResponse = mockSummaryResponse.model_copy(update={
    "content": AssistantMessage.model_validate({
        **to_jsonable(mockSummaryResponse),
        "content": [{"type": "toolCall", "id": "tool-call-1", "name": "read", "arguments": {"path": "README.md"}}],
    }).content,
    "stopReason": "toolUse",
})

messages = [{"role": "user", "content": "Summarize this.", "timestamp": nowMs()}]


@pytest.fixture
def completeSimpleMock(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    mock = SimpleNamespace(calls=[], responses=[])

    async def completeSimple(*args: Any) -> AssistantMessage:
        mock.calls.append(args)
        return mock.responses.pop(0) if mock.responses else mockSummaryResponse

    monkeypatch.setattr(compaction_module, "completeSimple", completeSimple)
    return mock


def preparation(**overrides: Any) -> CompactionPreparation:
    values: dict[str, Any] = {
        "firstKeptEntryId": "entry-keep",
        "messagesToSummarize": [],
        "turnPrefixMessages": messages,
        "isSplitTurn": True,
        "tokensBefore": 100,
        "previousSummary": None,
        "fileOps": FileOperations(),
        "settings": CompactionSettings(enabled=True, reserveTokens=2000, keepRecentTokens=20),
    }
    values.update(overrides)
    return CompactionPreparation(**values)


def options(call: tuple[Any, ...]) -> Any:
    return call[2]


def hasSet(value: Any, field: str) -> bool:
    """Whether ``field`` is present on the request options, as pi's ``toHaveProperty`` sees it."""
    return read_field(value, field) is not None


async def test_uses_the_provided_thinking_level_for_reasoning_capable_models(completeSimpleMock: SimpleNamespace):
    result = await generateSummaryWithUsage(
        messages, createModel(True), 2000, "test-key", None, None, None, None, "medium"
    )

    assert result.text == "## Goal\nTest summary"
    assert to_jsonable(result.usage) == to_jsonable(mockSummaryResponse.usage)

    assert len(completeSimpleMock.calls) == 1
    assert read_field(options(completeSimpleMock.calls[0]), "reasoning") == "medium"
    assert read_field(options(completeSimpleMock.calls[0]), "apiKey") == "test-key"


async def test_preserves_the_string_result_from_generate_summary(completeSimpleMock: SimpleNamespace):
    assert await generateSummary(messages, createModel(False), 2000, "test-key") == "## Goal\nTest summary"


async def test_uses_fresh_routing_sessions_without_prompt_caching(completeSimpleMock: SimpleNamespace):
    await generateSummary(messages, createModel(False), 2000, "test-key")
    await generateSummary(messages, createModel(False), 2000, "test-key")

    requestOptions = [options(call) for call in completeSimpleMock.calls]
    assert len(requestOptions) == 2
    assert all(read_field(option, "cacheRetention") == "none" for option in requestOptions)

    sessionIds = [read_field(option, "sessionId") for option in requestOptions]
    assert sessionIds[0] != sessionIds[1]


async def test_honors_caller_supplied_routing_session_and_tool_choice_without_prompt_caching(
    completeSimpleMock: SimpleNamespace,
):
    await completeSummarization(
        createModel(False),
        normalizeContext({"systemPrompt": "Summarize", "messages": []}),
        SimpleStreamOptions(sessionId="current-routing-session", cacheRetention="long", toolChoice="auto"),
    )

    requestOptions = options(completeSimpleMock.calls[0])
    assert read_field(requestOptions, "sessionId") == "current-routing-session"
    assert read_field(requestOptions, "cacheRetention") == "none"
    assert read_field(requestOptions, "toolChoice") == "auto"


async def test_preserves_the_previous_summary_without_an_empty_history_request_for_a_split_turn(
    completeSimpleMock: SimpleNamespace,
):
    result = await compact(preparation(previousSummary="previous checkpoint"), createModel(False), "test-key")

    assert len(completeSimpleMock.calls) == 1
    assert "previous checkpoint" in result.summary
    requestContext = completeSimpleMock.calls[0][1]
    prompt = jsonStringify(to_jsonable(read_field(requestContext, "messages")))
    # Regression test for #9652: clear boundaries and continuation wording avoid the reasoning-extraction false positive.
    assert "# Conversation\\n[User]: Summarize this." in prompt
    assert "# Instructions\\nThe messages above are earlier context from an ongoing conversation." in prompt


async def test_rejects_tool_calls_from_conversation_summaries(completeSimpleMock: SimpleNamespace):
    completeSimpleMock.responses.append(mockToolCallResponse)

    with pytest.raises(Exception, match="Summarization attempted to call a tool"):
        await generateSummaryWithUsage(messages, createModel(False), 2000, "test-key")


async def test_rejects_tool_calls_from_split_turn_summaries(completeSimpleMock: SimpleNamespace):
    completeSimpleMock.responses.append(mockToolCallResponse)

    with pytest.raises(Exception, match="Turn prefix summarization attempted to call a tool"):
        await compact(preparation(), createModel(False), "test-key")


def lengthLimited() -> AssistantMessage:
    return AssistantMessage.model_validate({
        **to_jsonable(mockSummaryResponse),
        "stopReason": "length",
        "content": [{"type": "text", "text": "partial"}],
    })


async def test_rejects_a_length_limited_history_summary(completeSimpleMock: SimpleNamespace):
    completeSimpleMock.responses.append(lengthLimited())

    with pytest.raises(Exception, match="generation hit the token cap"):
        await generateSummaryWithUsage(messages, createModel(False), 2000, "test-key")


async def test_rejects_a_length_limited_split_turn_summary(completeSimpleMock: SimpleNamespace):
    completeSimpleMock.responses.append(lengthLimited())

    with pytest.raises(Exception, match="generation hit the token cap"):
        await compact(preparation(), createModel(False), "test-key")


async def test_does_not_set_reasoning_when_thinking_is_off(completeSimpleMock: SimpleNamespace):
    await generateSummary(messages, createModel(True), 2000, "test-key", None, None, None, None, "off")

    assert len(completeSimpleMock.calls) == 1
    assert read_field(options(completeSimpleMock.calls[0]), "apiKey") == "test-key"
    assert not hasSet(options(completeSimpleMock.calls[0]), "reasoning")


async def test_does_not_set_reasoning_for_non_reasoning_models(completeSimpleMock: SimpleNamespace):
    await generateSummary(messages, createModel(False), 2000, "test-key", None, None, None, None, "medium")

    assert len(completeSimpleMock.calls) == 1
    assert read_field(options(completeSimpleMock.calls[0]), "apiKey") == "test-key"
    assert not hasSet(options(completeSimpleMock.calls[0]), "reasoning")


async def test_leaves_anthropic_refusal_fallback_handling_to_pi_ai_model_metadata(completeSimpleMock: SimpleNamespace):
    await generateSummary(
        messages,
        createModel(
            True,
            8192,
            {
                "allowedFallbackModels": [
                    {
                        "provider": "anthropic",
                        "model": "claude-opus-4-8",
                        "cost": {"input": 5, "output": 25, "cacheRead": 0.5, "cacheWrite": 6.25},
                    }
                ]
            },
        ),
        2000,
        "test-key",
    )

    assert len(completeSimpleMock.calls) == 1
    assert not hasSet(options(completeSimpleMock.calls[0]), "refusalFallbacks")


async def test_does_not_set_anthropic_refusal_fallback_for_models_without_allowed_fallback_targets(
    completeSimpleMock: SimpleNamespace,
):
    await generateSummary(messages, createModel(True), 2000, "test-key")

    assert len(completeSimpleMock.calls) == 1
    assert not hasSet(options(completeSimpleMock.calls[0]), "refusalFallbacks")


async def test_clamps_compaction_summary_max_tokens_to_the_model_output_cap(completeSimpleMock: SimpleNamespace):
    result = await compact(
        preparation(
            messagesToSummarize=messages,
            tokensBefore=600000,
            settings=CompactionSettings(enabled=True, reserveTokens=500000, keepRecentTokens=20000),
        ),
        createModel(False, 128000),
        "test-key",
    )

    assert to_jsonable(result.usage) == to_jsonable(mockSummaryResponse.usage.model_copy(update={
        "input": 20,
        "output": 20,
        "totalTokens": 40,
    }))
    assert [read_field(options(call), "maxTokens") for call in completeSimpleMock.calls] == [128000, 128000]

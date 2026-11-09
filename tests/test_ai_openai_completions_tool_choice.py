"""Translated from pi's ``packages/ai/test/openai-completions-tool-choice.test.ts`` (v1.3.0)."""

from __future__ import annotations

import time
from typing import Any

import pytest

from misaka.ai.api.openai_completions import convertMessages
from misaka.ai.compat import getModel, stream, streamSimple
from misaka.ai.types import (
    AssistantMessage,
    Context,
    Model,
    TextContent,
    ThinkingContent,
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


def _now() -> int:
    return time.time_ns() // 1_000_000


def _object(required: dict[str, Any], optional: dict[str, Any] | None = None) -> dict[str, Any]:
    """``Type.Object({...})`` with ``Type.Optional`` members in ``optional``."""
    return {"type": "object", "properties": {**required, **(optional or {})}, "required": list(required)}


STRING = {"type": "string"}
BOOLEAN = {"type": "boolean"}


def _usage() -> Usage:
    return Usage(
        input=0,
        output=0,
        cacheRead=0,
        cacheWrite=0,
        totalTokens=0,
        cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
    )


def _withoutCompat(provider: str, modelId: str, **fields: Any) -> Model:
    """``const { compat: _compat, ...baseModel } = getModel(...); { ...baseModel, api: "openai-completions", ... }``."""
    return Model.model_validate(
        {**getModel(provider, modelId).model_dump(exclude={"compat"}), "api": "openai-completions", **fields}
    )


def _with(model: Model, **fields: Any) -> Model:
    return Model.model_validate({**model.model_dump(), **fields})


def _local(**fields: Any) -> Model:
    return Model.model_validate(
        {
            "api": "openai-completions",
            "provider": "local-vllm",
            "baseUrl": "http://localhost:8000/v1",
            "reasoning": True,
            "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 128000,
            "maxTokens": 8192,
            **fields,
        }
    )


def _hi(content: str = "Hi", **context: Any) -> Context:
    return Context(messages=[UserMessage(content=content, timestamp=_now())], **context)


async def _capture(mockState: Any, model: Model, context: Context, options: dict[str, Any] | None = None) -> Any:
    captured: list[Any] = []
    await streamSimple(
        model,
        context,
        {"apiKey": "test", **(options or {}), "onPayload": lambda params, _model: captured.append(params)},
    ).result()
    return captured[-1] if captured else mockState.lastParams


async def captureSimpleParams(mockState: Any, model: Model, reasoning: str | None = None) -> Any:
    return await _capture(mockState, model, _hi(), {"reasoning": reasoning})


async def test_forwards_tool_choice_from_simple_options_to_payload(mockState):
    model = _withoutCompat("openai", "gpt-4o-mini")
    tools = [Tool(name="ping", description="Ping tool", parameters=_object({"ok": BOOLEAN}))]

    params = await _capture(mockState, model, _hi("Call ping with ok=true", tools=tools), {"toolChoice": "required"})

    assert params["tool_choice"] == "required"
    assert isinstance(params["tools"], list)
    assert len(params["tools"]) > 0


async def test_includes_tool_choice_when_no_tools_are_provided(mockState):
    model = _withoutCompat("openai", "gpt-4o-mini")

    params = await _capture(mockState, model, _hi("Summarize the conversation"), {"toolChoice": "none"})

    assert params["tool_choice"] == "none"
    assert "tools" not in params


async def test_omits_strict_when_compat_disables_strict_mode(mockState):
    model = _withoutCompat("openai", "gpt-4o-mini", compat={"supportsStrictMode": False})
    tools = [Tool(name="ping", description="Ping tool", parameters=_object({"ok": BOOLEAN}))]

    params = await _capture(mockState, model, _hi("Call ping with ok=true", tools=tools))

    tool = params["tools"][0]["function"]
    assert tool
    assert "strict" not in tool


# Regression test for #9816.
async def test_defaults_unknown_openai_compatible_endpoints_to_non_strict_tools(mockState):
    model = _local(id="local-model", name="Local Model")
    tool = Tool(
        name="ping",
        description="Ping tool",
        parameters=_object({"required": STRING}, {"optional": STRING}),
        constrainedSampling={"type": "json_schema", "strict": "prefer"},
    )

    params = await _capture(mockState, model, _hi("Call ping", tools=[tool]))

    functionTool = params["tools"][0]["function"]
    assert "strict" not in functionTool
    assert functionTool["parameters"]["required"] == ["required"]


async def test_preserves_strict_tools_for_capable_built_in_chat_completions_models(mockState):
    model = getModel("groq", "openai/gpt-oss-20b")
    assert model.compat.supportsStrictMode is True
    tool = Tool(
        name="ping",
        description="Ping tool",
        parameters=_object({"required": STRING}, {"optional": STRING}),
        constrainedSampling={"type": "json_schema", "strict": "prefer"},
    )

    params = await _capture(mockState, model, _hi("Call ping", tools=[tool]))

    functionTool = params["tools"][0]["function"]
    assert functionTool["strict"] is True
    assert functionTool["parameters"]["required"] == ["required", "optional"]


async def test_maps_groq_qwen_reasoning_levels_to_default_reasoning_effort(mockState):
    params = await _capture(mockState, getModel("groq", "qwen/qwen3.6-27b"), _hi(), {"reasoning": "medium"})

    assert params["reasoning_effort"] == "default"


async def test_keeps_normal_reasoning_effort_for_groq_models_without_compat_mapping(mockState):
    params = await _capture(mockState, getModel("groq", "openai/gpt-oss-20b"), _hi(), {"reasoning": "medium"})

    assert params["reasoning_effort"] == "medium"


async def test_enables_tool_stream_for_supported_zai_models_with_tools(mockState):
    tools = [Tool(name="ping", description="Ping tool", parameters=_object({"ok": BOOLEAN}))]

    params = await _capture(mockState, getModel("zai", "glm-5.2"), _hi("Call ping with ok=true", tools=tools))

    assert params["tool_stream"] is True


def test_stores_zai_tool_stream_support_in_model_compat_metadata():
    assert getModel("zai", "glm-4.7").compat.zaiToolStream is True
    assert getModel("zai", "glm-4.7").compat.zaiToolStream is True
    assert getModel("zai", "glm-5-turbo").compat.zaiToolStream is True
    assert getModel("zai", "glm-5.2").compat.zaiToolStream is True


def test_stores_zai_effort_metadata():
    for modelId in ("glm-5.2", "glm-5.2-highspeed"):
        model = getModel("zai", modelId)
        assert model.compat.supportsReasoningEffort is True
        assert model.thinkingLevelMap == {
            "off": "none",
            "minimal": None,
            "low": None,
            "medium": None,
            "high": "high",
            "xhigh": None,
            "max": "max",
        }

    for provider in ("zai", "zai-coding-cn"):
        glm53 = getModel(provider, "glm-5.3")
        assert glm53.compat.supportsReasoningEffort is True
        assert glm53.thinkingLevelMap == {
            "off": None,
            "minimal": None,
            "low": "low",
            "medium": None,
            "high": "high",
            "xhigh": None,
            "max": "max",
        }


async def test_maps_zai_glm_5_2_thinking_levels_to_reasoning_effort(mockState):
    model = getModel("zai", "glm-5.2")
    cases = [
        {"reasoning": "low", "effort": "high"},
        {"reasoning": "medium", "effort": "high"},
        {"reasoning": "high", "effort": "high"},
        {"reasoning": "max", "effort": "max"},
    ]

    for testCase in cases:
        params = await _capture(mockState, model, _hi(), {"reasoning": testCase["reasoning"]})

        assert params["thinking"] == {"type": "enabled", "clear_thinking": False}
        assert params["reasoning_effort"] == testCase["effort"]


async def test_preserves_zai_thinking_when_replaying_reasoning_content(mockState):
    model = getModel("zai", "glm-5.2")
    assistantMessage = AssistantMessage(
        api="openai-completions",
        provider="zai",
        model="glm-5.2",
        content=[
            ThinkingContent(thinking="prior reasoning", thinkingSignature="reasoning_content"),
            ToolCall(id="call_1", name="read", arguments={"path": "README.md"}),
        ],
        usage=_usage(),
        stopReason="toolUse",
        timestamp=_now(),
    )
    toolResult = ToolResultMessage(
        toolCallId="call_1", toolName="read", content=[TextContent(text="contents")], isError=False, timestamp=_now()
    )

    params = await _capture(
        mockState,
        model,
        Context(
            messages=[
                UserMessage(content="Read README.md", timestamp=_now()),
                assistantMessage,
                toolResult,
                UserMessage(content="Continue", timestamp=_now()),
            ]
        ),
        {"reasoning": "high"},
    )

    replayedAssistant = next(message for message in params["messages"] if message["role"] == "assistant")
    assert replayedAssistant["reasoning_content"] == "prior reasoning"
    assert params["thinking"] == {"type": "enabled", "clear_thinking": False}


async def test_omits_zai_glm_5_2_reasoning_effort_when_thinking_is_off(mockState):
    params = await _capture(mockState, getModel("zai", "glm-5.2"), _hi())

    assert params["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in params


async def test_respects_explicit_zai_tool_stream_compat_override(mockState):
    baseModel = getModel("zai", "glm-5.2")
    model = _with(baseModel, compat={**baseModel.compat.model_dump(exclude_none=True), "zaiToolStream": True})
    tools = [Tool(name="ping", description="Ping tool", parameters=_object({"ok": BOOLEAN}))]

    params = await _capture(mockState, model, _hi("Call ping with ok=true", tools=tools))

    assert params["tool_stream"] is True


async def test_omits_tool_stream_when_no_tools_are_provided(mockState):
    params = await _capture(mockState, getModel("zai", "glm-5.2"), _hi())

    assert "tool_stream" not in params


async def test_maps_non_standard_provider_finish_reason_values_to_stop_reason_error(mockState):
    mockState.chunks = [
        {"choices": [{"delta": {"content": "partial"}, "finish_reason": None}]},
        {
            "choices": [{"delta": {}, "finish_reason": "network_error"}],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
        },
    ]

    response = await streamSimple(getModel("zai", "glm-5.2"), _hi(), {"apiKey": "test"}).result()

    assert response.stopReason == "error"
    assert response.errorMessage == "Provider finish_reason: network_error"


async def test_ignores_null_stream_chunks_from_openai_compatible_providers(mockState):
    mockState.chunks = [
        None,
        {"id": "chatcmpl-test", "choices": [{"delta": {"content": "OK"}, "finish_reason": None}]},
        {
            "id": "chatcmpl-test",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 3,
                "completion_tokens": 1,
                "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
        },
    ]

    response = await streamSimple(
        _withoutCompat("openai", "gpt-4o-mini"), _hi("Reply with exactly OK"), {"apiKey": "test"}
    ).result()

    assert response.stopReason == "stop"
    assert response.errorMessage is None
    assert response.responseId == "chatcmpl-test"
    assert response.usage.totalTokens == 4
    assert [block.model_dump(exclude_none=True) for block in response.content] == [{"type": "text", "text": "OK"}]


async def test_errors_when_a_stream_ends_after_only_null_finish_reason_chunks(mockState):
    mockState.chunks = [
        {"id": "chatcmpl-truncated", "choices": [{"delta": {"content": "partial answer"}, "finish_reason": None}]},
        {"id": "chatcmpl-truncated", "choices": [{"delta": {"content": "partial answer"}, "finish_reason": None}]},
    ]

    response = await streamSimple(
        _withoutCompat("openai", "gpt-4o-mini"), _hi("Reply with a longer sentence"), {"apiKey": "test"}
    ).result()

    assert response.stopReason == "error"
    assert response.errorMessage == "Stream ended without finish_reason"


async def test_accepts_streams_without_finish_reason_when_compat_disables_it(mockState):
    mockState.chunks = [
        {"id": "chatcmpl-no-finish-reason", "choices": [{"delta": {"content": "complete answer"}, "finish_reason": None}]},
    ]
    model = _withoutCompat("openai", "gpt-4o-mini", compat={"supportsFinishReason": False})

    response = await streamSimple(model, _hi("Reply with a complete answer"), {"apiKey": "test"}).result()

    assert response.stopReason == "stop"
    assert response.errorMessage is None
    assert [block.model_dump(exclude_none=True) for block in response.content] == [
        {"type": "text", "text": "complete answer"}
    ]


async def test_ignores_empty_custom_objects_on_function_tool_call_deltas(mockState):
    mockState.chunks = [
        {
            "id": "chatcmpl-empty-custom",
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "read", "arguments": '{"path":"README.md"}'},
                                "custom": {},
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
    ]
    tool = Tool(name="read", description="Read a file", parameters=_object({"path": STRING}))

    response = await streamSimple(
        _withoutCompat("openai", "gpt-4o-mini"), _hi("Read README.md", tools=[tool]), {"apiKey": "test"}
    ).result()

    assert [block.model_dump(exclude_none=True) for block in response.content] == [
        {"type": "toolCall", "id": "call_1", "name": "read", "arguments": {"path": "README.md"}}
    ]


def _toolCallChunk(chunkId: str, toolCalls: list[dict[str, Any]], finishReason: str | None, **extra: Any) -> dict[str, Any]:
    return {"id": chunkId, "choices": [{"delta": {"tool_calls": toolCalls}, "finish_reason": finishReason}], **extra}


async def test_coalesces_tool_call_deltas_by_stable_index_when_provider_mutates_ids_mid_stream(mockState):
    mockState.chunks = [
        _toolCallChunk(
            "chatcmpl-kimi-bad-stream",
            [{"index": 0, "id": "functions.read:0", "type": "function", "function": {"name": "read", "arguments": ""}}],
            None,
        ),
        _toolCallChunk(
            "chatcmpl-kimi-bad-stream",
            [
                {
                    "index": 0,
                    "id": "chatcmpl-tool-a",
                    "type": "function",
                    "function": {"name": None, "arguments": '{"path":"README'},
                }
            ],
            None,
        ),
        _toolCallChunk(
            "chatcmpl-kimi-bad-stream",
            [{"index": 0, "id": "chatcmpl-tool-b", "type": "function", "function": {"name": None, "arguments": '.md"}'}}],
            "tool_calls",
            usage={
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
        ),
    ]
    tool = Tool(name="read", description="Read a file", parameters=_object({"path": STRING}))
    s = streamSimple(_withoutCompat("openai", "gpt-4o-mini"), _hi("Read README.md", tools=[tool]), {"apiKey": "test"})

    toolCallContentIndexes: list[int] = []
    async for event in s:
        if event.type in ("toolcall_start", "toolcall_delta", "toolcall_end"):
            toolCallContentIndexes.append(event.contentIndex)

    response = await s.result()
    assert response.stopReason == "toolUse"
    assert toolCallContentIndexes == [0, 0, 0, 0, 0]
    assert len(response.content) == 1
    toolCall = response.content[0]
    assert toolCall.type == "toolCall"
    assert toolCall.id == "functions.read:0"
    assert toolCall.name == "read"
    assert toolCall.arguments == {"path": "README.md"}
    assert "streamIndex" not in toolCall.model_dump()
    assert "partialArgs" not in toolCall.model_dump()


async def test_accumulates_mixed_content_reasoning_and_parallel_tool_call_deltas_independently(mockState):
    mockState.chunks = [
        {
            "id": "chatcmpl-mixed-deltas",
            "choices": [
                {
                    "delta": {
                        "content": "answer 1",
                        "reasoning_content": "think 1",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "tc_read_initial",
                                "type": "function",
                                "function": {"name": "read", "arguments": '{"path":"README'},
                            },
                            {
                                "index": 1,
                                "id": "tc_grep_initial",
                                "type": "function",
                                "function": {"name": "grep", "arguments": '{"pattern":"TODO'},
                            },
                            {
                                "id": "tc_list_no_index",
                                "type": "function",
                                "function": {"name": "list", "arguments": '{"path":"packages'},
                            },
                            {
                                "id": "tc_write_no_index",
                                "type": "function",
                                "function": {"name": "write", "arguments": '{"path":"out'},
                            },
                        ],
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-mixed-deltas",
            "choices": [
                {
                    "delta": {
                        "content": " answer 2",
                        "tool_calls": [
                            {
                                "index": 1,
                                "id": "tc_grep_changed",
                                "type": "function",
                                "function": {"arguments": '","path":"src'},
                            },
                            {
                                "id": "tc_write_no_index",
                                "type": "function",
                                "function": {"arguments": '.txt","content":"ok"}'},
                            },
                            {"id": "tc_list_no_index", "type": "function", "function": {"arguments": '/ai"}'}},
                        ],
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-mixed-deltas",
            "choices": [
                {
                    "delta": {
                        "content": "\n",
                        "reasoning_content": " think 2",
                        "tool_calls": [
                            {"index": 0, "id": "tc_read_changed", "type": "function", "function": {"arguments": '.md"}'}},
                            {"index": 1, "type": "function", "function": {"arguments": '"}'}},
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 8,
                "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 2},
            },
        },
    ]
    tools = [
        Tool(name="read", description="Read a file", parameters=_object({"path": STRING})),
        Tool(name="grep", description="Search a file", parameters=_object({"pattern": STRING, "path": STRING})),
        Tool(name="list", description="List a directory", parameters=_object({"path": STRING})),
        Tool(name="write", description="Write a file", parameters=_object({"path": STRING, "content": STRING})),
    ]
    s = streamSimple(
        _withoutCompat("openai", "gpt-4o-mini"), _hi("Think, answer, and use tools.", tools=tools), {"apiKey": "test"}
    )

    eventTypes: list[str] = []
    toolEventsByContentIndex: dict[int, list[str]] = {}
    async for event in s:
        eventTypes.append(event.type)
        if event.type in ("toolcall_start", "toolcall_delta", "toolcall_end"):
            toolEventsByContentIndex.setdefault(event.contentIndex, []).append(event.type)

    response = await s.result()
    assert response.stopReason == "toolUse"
    counts = {kind: eventTypes.count(kind) for kind in set(eventTypes)}
    assert counts["text_start"] == 1
    assert counts["text_delta"] == 3
    assert counts["text_end"] == 1
    assert counts["thinking_start"] == 1
    assert counts["thinking_delta"] == 2
    assert counts["thinking_end"] == 1
    assert counts["toolcall_start"] == 4
    assert counts["toolcall_delta"] == 9
    assert counts["toolcall_end"] == 4
    assert toolEventsByContentIndex[2] == ["toolcall_start", "toolcall_delta", "toolcall_delta", "toolcall_end"]
    assert toolEventsByContentIndex[3] == [
        "toolcall_start",
        "toolcall_delta",
        "toolcall_delta",
        "toolcall_delta",
        "toolcall_end",
    ]
    assert toolEventsByContentIndex[4] == ["toolcall_start", "toolcall_delta", "toolcall_delta", "toolcall_end"]
    assert toolEventsByContentIndex[5] == ["toolcall_start", "toolcall_delta", "toolcall_delta", "toolcall_end"]

    assert len(response.content) == 6
    assert response.content[0].model_dump(exclude_none=True) == {
        "type": "thinking",
        "thinking": "think 1 think 2",
        "thinkingSignature": "reasoning_content",
    }
    assert response.content[1].model_dump(exclude_none=True) == {"type": "text", "text": "answer 1 answer 2\n"}
    readCall, grepCall, listCall, writeCall = response.content[2:]
    assert [call.type for call in (readCall, grepCall, listCall, writeCall)] == ["toolCall"] * 4
    assert (readCall.id, readCall.name, readCall.arguments) == ("tc_read_initial", "read", {"path": "README.md"})
    assert (grepCall.id, grepCall.name, grepCall.arguments) == ("tc_grep_initial", "grep", {"pattern": "TODO", "path": "src"})
    assert (listCall.id, listCall.name, listCall.arguments) == ("tc_list_no_index", "list", {"path": "packages/ai"})
    assert (writeCall.id, writeCall.name, writeCall.arguments) == (
        "tc_write_no_index",
        "write",
        {"path": "out.txt", "content": "ok"},
    )
    for call in (readCall, grepCall, listCall, writeCall):
        assert "streamIndex" not in call.model_dump()
        assert "partialArgs" not in call.model_dump()


async def test_uses_system_messages_for_non_openai_anthropic_openrouter_reasoning_model_instructions(mockState):
    params = await _capture(
        mockState,
        getModel("openrouter", "deepseek/deepseek-v4-pro"),
        Context(systemPrompt="Follow instructions.", messages=[UserMessage(content="Hi", timestamp=_now())]),
    )

    assert params["messages"][0]["role"] == "system"


async def test_keeps_developer_messages_for_openai_and_anthropic_openrouter_batch_instructions(mockState):
    for model in (getModel("openrouter", "openai/gpt-5.2-codex"), getModel("openrouter", "anthropic/claude-fable-5.1:batch")):
        assert model is not None
        params = await _capture(
            mockState,
            model,
            Context(systemPrompt="Follow instructions.", messages=[UserMessage(content="Hi", timestamp=_now())]),
        )

        assert params["messages"][0]["role"] == "developer"


async def test_keeps_developer_messages_for_openai_reasoning_model_instructions(mockState):
    params = await _capture(
        mockState,
        _withoutCompat("openai", "gpt-5.5"),
        Context(systemPrompt="Follow instructions.", messages=[UserMessage(content="Hi", timestamp=_now())]),
    )

    assert params["messages"][0]["role"] == "developer"


def test_stores_openrouter_kimi_k2_6_reasoning_replay_compat_in_built_in_metadata():
    # `:free` variant delisted from the OpenRouter API; the generator override
    # matches any `moonshotai/kimi-k2.6*` variant that is listed.
    model = getModel("openrouter", "moonshotai/kimi-k2.6")
    assert model.compat.supportsDeveloperRole is False
    assert model.compat.requiresReasoningContentOnAssistantMessages is True


def test_stores_xiaomi_mimo_reasoning_replay_compat_in_built_in_metadata():
    for provider in ("xiaomi", "xiaomi-token-plan-cn", "xiaomi-token-plan-ams", "xiaomi-token-plan-sgp"):
        model = getModel(provider, "mimo-v2.5-pro")
        assert model.compat.requiresReasoningContentOnAssistantMessages is True
        assert model.compat.thinkingFormat == "deepseek"
        assert model.compat.maxTokensField is None
        assert model.compat.supportsDeveloperRole is None


def test_stores_qwen_token_plan_reasoning_replay_compat_in_built_in_metadata():
    for provider in ("qwen-token-plan", "qwen-token-plan-cn", "qwen-token-plan-individual"):
        model = getModel(provider, "qwen3.7-max")
        assert model.compat.thinkingFormat == "qwen"
        assert model.compat.requiresReasoningContentOnAssistantMessages is None
        assert model.compat.supportsDeveloperRole is False
        assert model.compat.supportsStore is False


async def test_replays_xiaomi_mimo_assistant_tool_calls_with_empty_reasoning_content_when_thinking_is_missing(mockState):
    model = getModel("xiaomi", "mimo-v2.5-pro")
    assistantMessage = AssistantMessage(
        api="openai-completions",
        provider="xiaomi",
        model="mimo-v2.5-pro",
        content=[ToolCall(id="call_1", name="read", arguments={"path": "README.md"})],
        usage=_usage(),
        stopReason="toolUse",
        timestamp=_now(),
    )
    toolResult = ToolResultMessage(
        toolCallId="call_1", toolName="read", content=[TextContent(text="contents")], isError=False, timestamp=_now()
    )

    params = await _capture(
        mockState,
        model,
        Context(messages=[UserMessage(content="Read README.md", timestamp=_now()), assistantMessage, toolResult]),
        {"reasoning": "high"},
    )

    replayedAssistant = next(message for message in params["messages"] if message["role"] == "assistant")
    assert replayedAssistant["role"] == "assistant"
    assert replayedAssistant["reasoning_content"] == ""
    assert params["thinking"] == {"type": "enabled"}
    assert "reasoning_effort" not in params


async def test_normalizes_opencode_go_reasoning_deltas_to_reasoning_content_for_replay(mockState):
    mockState.chunks = [
        {"id": "chatcmpl-opencode-go-reasoning", "choices": [{"delta": {"reasoning": "think"}, "finish_reason": "stop"}]}
    ]

    response = await streamSimple(_withoutCompat("opencode-go", "kimi-k3"), _hi("Use reasoning."), {"apiKey": "test"}).result()

    assert [block.model_dump(exclude_none=True) for block in response.content] == [
        {"type": "thinking", "thinking": "think", "thinkingSignature": "reasoning_content"}
    ]


async def test_keeps_non_opencode_go_reasoning_deltas_on_the_original_reasoning_field(mockState):
    mockState.chunks = [{"id": "chatcmpl-reasoning", "choices": [{"delta": {"reasoning": "think"}, "finish_reason": "stop"}]}]

    response = await streamSimple(_withoutCompat("openai", "gpt-4o-mini"), _hi("Use reasoning."), {"apiKey": "test"}).result()

    assert [block.model_dump(exclude_none=True) for block in response.content] == [
        {"type": "thinking", "thinking": "think", "thinkingSignature": "reasoning"}
    ]


def test_replays_opencode_go_reasoning_thinking_blocks_as_reasoning_content():
    model = _withoutCompat("opencode-go", "kimi-k3")
    messages = convertMessages(
        model,
        normalizeContext(
            Context(
                messages=[
                    AssistantMessage(
                        api="openai-completions",
                        provider="opencode-go",
                        model="kimi-k3",
                        content=[
                            ThinkingContent(thinking="think", thinkingSignature="reasoning"),
                            ToolCall(id="call_1", name="read", arguments={"path": "README.md"}),
                        ],
                        usage=_usage(),
                        stopReason="stop",
                        timestamp=_now(),
                    )
                ]
            )
        ),
        {
            "supportsStore": False,
            "supportsDeveloperRole": False,
            "supportsReasoningEffort": True,
            "supportsUsageInStreaming": True,
            "supportsFinishReason": True,
            "maxTokensField": "max_completion_tokens",
            "requiresToolResultName": False,
            "requiresAssistantAfterToolResult": False,
            "requiresThinkingAsText": False,
            "requiresReasoningContentOnAssistantMessages": False,
            "thinkingFormat": "openai",
            "openRouterRouting": {},
            "vercelGatewayRouting": {},
            "chatTemplateKwargs": {},
            "chatTemplateArgs": {},
            "zaiToolStream": False,
            "supportsStrictMode": True,
            "supportsOpenAIGrammarTools": False,
            "sendSessionAffinityHeaders": False,
            "sessionAffinityFormat": "openai",
            "supportsLongCacheRetention": True,
        },
    )

    assert messages[0]["role"] == "assistant"
    assert messages[0]["reasoning_content"] == "think"
    assert "reasoning" not in messages[0]


async def test_sends_thinking_disabled_for_opencode_kimi_k2_6_when_thinking_is_off(mockState):
    params = await _capture(mockState, getModel("opencode", "kimi-k2.6"), _hi())

    assert params["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in params


async def test_sends_thinking_enabled_for_opencode_kimi_k2_6_when_thinking_is_enabled(mockState):
    params = await _capture(mockState, getModel("opencode", "kimi-k2.6"), _hi(), {"reasoning": "high"})

    assert params["thinking"] == {"type": "enabled"}
    assert "reasoning_effort" not in params


async def test_omits_disabled_thinking_for_moonshot_kimi_k2_7_code_models(mockState):
    for model in (getModel("moonshotai", "kimi-k2.7-code"), getModel("moonshotai-cn", "kimi-k2.7-code")):
        assert model is not None
        params = await _capture(mockState, model, _hi())

        assert "thinking" not in params
        assert "reasoning_effort" not in params


async def test_keeps_disabled_thinking_for_moonshot_kimi_k2_6_when_thinking_is_off(mockState):
    params = await _capture(mockState, getModel("moonshotai-cn", "kimi-k2.6"), _hi())

    assert params["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in params


async def test_sends_max_tokens_for_opencode_completions_models(mockState):
    for model in (getModel("opencode-go", "kimi-k3"), getModel("opencode", "kimi-k2.6")):
        assert model.compat.maxTokensField == "max_tokens"
        params = await _capture(mockState, model, _hi(), {"maxTokens": 123})

        assert params["max_tokens"] == 123
        assert "max_completion_tokens" not in params


async def test_sends_max_tokens_for_built_in_and_custom_deepseek_api_models(mockState):
    customModel = _local(
        id="custom-deepseek-model",
        name="Custom DeepSeek Model",
        provider="custom-deepseek",
        baseUrl="https://api.deepseek.com",
    )
    customUppercaseModel = _with(
        customModel, id="custom-uppercase-deepseek-model", name="Custom Uppercase DeepSeek Model", baseUrl="https://API.DeepSeek.COM"
    )
    nativeModels = [getModel("deepseek", "deepseek-flash"), getModel("deepseek", "deepseek-v4-pro")]

    for model in nativeModels:
        assert model.compat.maxTokensField == "max_tokens"

    for model in [*nativeModels, customModel, customUppercaseModel]:
        params = await _capture(mockState, model, _hi(), {"maxTokens": 123})

        assert params["max_tokens"] == 123
        assert "max_completion_tokens" not in params


async def test_sends_max_tokens_for_zai_completions_models(mockState):
    for model in (getModel("zai", "glm-5-turbo"), getModel("zai", "glm-5.2")):
        assert model.compat.maxTokensField == "max_tokens"
        params = await _capture(mockState, model, _hi(), {"maxTokens": 123})

        assert params["max_tokens"] == 123
        assert "max_completion_tokens" not in params


async def test_omits_reasoning_effort_for_opencode_grok_build(mockState):
    params = await _capture(mockState, getModel("opencode", "grok-build-0.1"), _hi(), {"reasoning": "high"})

    assert "reasoning_effort" not in params


async def test_does_not_double_count_reasoning_tokens_in_completion_usage(mockState):
    mockState.chunks = [
        {
            "id": "chatcmpl-reasoning-usage",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 33,
                "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 21},
            },
        }
    ]

    response = await streamSimple(_withoutCompat("openai", "gpt-4o-mini"), _hi("Use reasoning."), {"apiKey": "test"}).result()

    assert response.usage.input == 10
    assert response.usage.output == 33
    assert response.usage.totalTokens == 43


_CACHE_WRITE_USAGE = {
    "prompt_tokens": 100,
    "completion_tokens": 5,
    "prompt_tokens_details": {"cached_tokens": 50, "cache_write_tokens": 30},
    "completion_tokens_details": {"reasoning_tokens": 0},
}


async def test_preserves_prompt_tokens_details_cache_read_write_fields_from_chunk_usage(mockState):
    mockState.chunks = [
        {"id": "chatcmpl-cache-write", "choices": [{"delta": {"content": "OK"}, "finish_reason": None}]},
        {"id": "chatcmpl-cache-write", "choices": [{"delta": {}, "finish_reason": "stop"}], "usage": _CACHE_WRITE_USAGE},
    ]

    response = await streamSimple(
        _withoutCompat("openai", "gpt-4o-mini"), _hi("Reply with exactly OK"), {"apiKey": "test"}
    ).result()

    # cached_tokens is documented as cache reads; cache_write_tokens is separate.
    assert response.usage.input == 20
    assert response.usage.cacheRead == 50
    assert response.usage.cacheWrite == 30
    assert response.usage.totalTokens == 105


async def test_preserves_prompt_tokens_details_cache_read_write_fields_from_choice_usage_fallback(mockState):
    mockState.chunks = [
        {"id": "chatcmpl-cache-write-choice", "choices": [{"delta": {"content": "OK"}, "finish_reason": None}]},
        {
            "id": "chatcmpl-cache-write-choice",
            "choices": [{"delta": {}, "finish_reason": "stop", "usage": _CACHE_WRITE_USAGE}],
        },
    ]

    response = await streamSimple(
        _withoutCompat("openai", "gpt-4o-mini"), _hi("Reply with exactly OK"), {"apiKey": "test"}
    ).result()

    # cached_tokens is documented as cache reads; cache_write_tokens is separate.
    assert response.usage.input == 20
    assert response.usage.cacheRead == 50
    assert response.usage.cacheWrite == 30
    assert response.usage.totalTokens == 105


async def test_uses_openrouter_reasoning_object_instead_of_reasoning_effort(mockState):
    params = await _capture(mockState, getModel("openrouter", "deepseek/deepseek-r1"), _hi(), {"reasoning": "high"})

    assert params["reasoning"] == {"effort": "high"}
    assert "reasoning_effort" not in params


async def test_uses_configurable_chat_template_boolean_thinking_kwargs(mockState):
    model = _local(
        id="deepseek-ai/DeepSeek-V3.1",
        name="DeepSeek V3.1 via vLLM",
        compat={
            "thinkingFormat": "chat-template",
            "supportsReasoningEffort": False,
            "chatTemplateKwargs": {"thinking": {"$var": "thinking.enabled"}},
        },
    )

    for testCase in ({"reasoning": "high", "expected": True}, {"reasoning": None, "expected": False}):
        params = await captureSimpleParams(mockState, model, testCase["reasoning"])

        assert params["chat_template_kwargs"] == {"thinking": testCase["expected"]}
        assert "thinking" not in params
        assert "reasoning_effort" not in params


async def test_uses_qwen_chat_template_thinking_kwargs(mockState):
    model = _local(
        id="Qwen/Qwen3-Coder",
        name="Qwen3 Coder via vLLM",
        compat={"thinkingFormat": "qwen-chat-template", "supportsReasoningEffort": False},
    )

    for testCase in ({"reasoning": "high", "expected": True}, {"reasoning": None, "expected": False}):
        params = await captureSimpleParams(mockState, model, testCase["reasoning"])

        assert params["chat_template_kwargs"] == {"enable_thinking": testCase["expected"], "preserve_thinking": True}
        assert "reasoning_effort" not in params


async def test_uses_configurable_chat_template_effort_kwargs_with_static_kwargs(mockState):
    model = _local(
        id="unsloth/gpt-oss-120b-GGUF",
        name="GPT OSS via vLLM",
        thinkingLevelMap={"xhigh": "max"},
        compat={
            "thinkingFormat": "chat-template",
            "supportsReasoningEffort": False,
            "chatTemplateKwargs": {
                "preserve_thinking": True,
                "reasoning_effort": {"$var": "thinking.effort", "omitWhenOff": True},
            },
        },
    )

    params = await captureSimpleParams(mockState, model, "xhigh")

    assert params["chat_template_kwargs"] == {"preserve_thinking": True, "reasoning_effort": "max"}
    assert "reasoning_effort" not in params


async def test_uses_ant_ling_compatibility_metadata(mockState):
    model = getModel("ant-ling", "Ring-2.6-1T")

    compat = model.compat.model_dump()
    assert {
        key: compat[key]
        for key in (
            "supportsStore",
            "supportsDeveloperRole",
            "supportsReasoningEffort",
            "maxTokensField",
            "thinkingFormat",
            "supportsLongCacheRetention",
        )
    } == {
        "supportsStore": False,
        "supportsDeveloperRole": False,
        "supportsReasoningEffort": False,
        "maxTokensField": "max_tokens",
        "thinkingFormat": "ant-ling",
        "supportsLongCacheRetention": False,
    }
    assert model.compat.supportsStrictMode is True
    assert model.compat.requiresReasoningContentOnAssistantMessages is None

    params = await _capture(
        mockState,
        model,
        Context(systemPrompt="Follow instructions.", messages=[UserMessage(content="Hi", timestamp=_now())]),
        {"maxTokens": 123, "reasoning": "high", "cacheRetention": "long", "sessionId": "ant-ling-session"},
    )

    assert params["max_tokens"] == 123
    assert "max_completion_tokens" not in params
    assert params["messages"][0]["role"] == "system"
    assert params["reasoning"] == {"effort": "high"}
    assert "reasoning_effort" not in params
    assert "store" not in params
    assert params.get("prompt_cache_key") is None
    assert params.get("prompt_cache_retention") is None


async def test_omits_ant_ling_reasoning_for_unmapped_direct_reasoning_efforts_and_non_reasoning_models(mockState):
    captured: list[Any] = []
    await stream(
        getModel("ant-ling", "Ring-2.6-1T"),
        _hi(),
        {"apiKey": "test", "reasoningEffort": "medium", "onPayload": lambda params, _model: captured.append(params)},
    ).result()

    assert "reasoning" not in captured[-1]

    await streamSimple(
        getModel("ant-ling", "Ling-2.6-flash"),
        _hi(),
        {"apiKey": "test", "reasoning": "high", "onPayload": lambda params, _model: captured.append(params)},
    ).result()

    assert "reasoning" not in captured[-1]

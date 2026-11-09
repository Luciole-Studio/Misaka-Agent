"""Translated from pi's ``packages/ai/test/anthropic-sse-parsing.test.ts`` (v1.1.0)."""

from __future__ import annotations

import json
import time
from typing import Any

from misaka.ai.api.anthropic_messages import stream as streamAnthropic
from misaka.ai.api.transform_messages import transformMessages
from misaka.ai.compat import getModel
from misaka.ai.types import Tool, UserMessage
from misaka.ai.utils.transcript import normalizeContext
from tests.anthropic_sse import createFakeAnthropicClient, createSseResponse


def _frame(event: str, data: dict[str, Any]) -> dict[str, str]:
    return {"event": event, "data": json.dumps(data)}


minimalAnthropicEvents = [
    _frame(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": "msg_test",
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
            },
        },
    ),
    _frame("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
    _frame(
        "content_block_delta",
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}},
    ),
    _frame("content_block_stop", {"type": "content_block_stop", "index": 0}),
    _frame(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {
                "input_tokens": 12,
                "output_tokens": 5,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        },
    ),
    _frame("message_stop", {"type": "message_stop"}),
]


def _hello() -> Any:
    return normalizeContext({"messages": [UserMessage(content="Hello", timestamp=1)]})


def _dumped(content: list[Any]) -> list[dict[str, Any]]:
    return [block.model_dump(exclude_none=True) for block in content]


def createResponseModelSseResponse(model: str, contentBlock: dict[str, Any]) -> Any:
    return createSseResponse(
        [
            _frame(
                "message_start",
                {
                    "type": "message_start",
                    "message": {"id": "msg_response_model", "model": model, "usage": {"input_tokens": 100, "output_tokens": 0}},
                },
            ),
            _frame("content_block_start", {"type": "content_block_start", "index": 0, "content_block": contentBlock}),
            _frame("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _frame(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"input_tokens": 100, "output_tokens": 20},
                },
            ),
            _frame("message_stop", {"type": "message_stop"}),
        ]
    )


async def test_forwards_parsed_provider_stream_events_in_order():
    model = getModel("anthropic", "claude-haiku-4-5")
    providerEvents: list[Any] = []
    eventModels: list[Any] = []

    async def onProviderStreamEvent(event: Any, eventModel: Any) -> None:
        providerEvents.append(event)
        eventModels.append(eventModel)

    result = await streamAnthropic(
        model,
        _hello(),
        {
            "client": createFakeAnthropicClient(createSseResponse(minimalAnthropicEvents)),
            "onProviderStreamEvent": onProviderStreamEvent,
        },
    ).result()

    assert result.stopReason == "stop"
    assert [event["type"] for event in providerEvents] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert eventModels == [model] * 6


async def test_keeps_signed_thinking_replayable_when_a_proxy_relabels_the_model():
    # Regression test for earendil-works/pi#9188.
    model = getModel("anthropic", "claude-opus-5")
    responseModel = "kimi-for-coding"
    initialContext = _hello()
    first = await streamAnthropic(
        model,
        initialContext,
        {
            "client": createFakeAnthropicClient(
                createResponseModelSseResponse(
                    responseModel, {"type": "thinking", "thinking": "reasoning", "signature": "signature"}
                )
            )
        },
    ).result()

    assert first.model == model.id
    assert first.responseModel == responseModel

    transformed = transformMessages([*initialContext.messages, first], model)
    replayedAssistant = next(message for message in transformed if message.role == "assistant")
    assert _dumped(replayedAssistant.content) == [
        {"type": "thinking", "thinking": "reasoning", "thinkingSignature": "signature"}
    ]


async def test_uses_a_returned_fallback_model_for_cost_attribution():
    fallbackModel = "fallback-model"
    native = getModel("anthropic", "claude-opus-5")
    model = type(native).model_validate(
        {
            **native.model_dump(exclude_none=True),
            "compat": {
                "allowedFallbackModels": [
                    {
                        "provider": "anthropic",
                        "model": fallbackModel,
                        "cost": {"input": 3, "output": 5, "cacheRead": 0, "cacheWrite": 0},
                    }
                ]
            },
        }
    )
    result = await streamAnthropic(
        model,
        _hello(),
        {"client": createFakeAnthropicClient(createResponseModelSseResponse(fallbackModel, {"type": "text", "text": "done"}))},
    ).result()

    assert result.model == model.id
    assert result.responseModel == fallbackModel
    assert abs(result.usage.cost.input - 0.0003) < 1e-10
    assert abs(result.usage.cost.output - 0.0001) < 1e-10


async def test_fails_safely_when_anthropic_falls_back_after_output_begins():
    model = getModel("anthropic", "claude-opus-5")
    response = createSseResponse(
        [
            _frame(
                "message_start",
                {
                    "type": "message_start",
                    "message": {"id": "msg_fallback", "model": "claude-opus-5", "usage": {"input_tokens": 1, "output_tokens": 0}},
                },
            ),
            _frame(
                "content_block_start",
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": "partial"}},
            ),
            _frame("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _frame(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {
                        "type": "fallback",
                        "from": {"model": "claude-opus-5"},
                        "to": {"model": "claude-opus-4-8"},
                    },
                },
            ),
        ]
    )

    result = await streamAnthropic(model, _hello(), {"client": createFakeAnthropicClient(response)}).result()

    assert result.stopReason == "error"
    assert "unsupported mid-output model fallback" in result.errorMessage


async def test_forces_streaming_after_an_on_payload_replacement():
    streaming: list[Any] = []
    client = createFakeAnthropicClient(
        createSseResponse(minimalAnthropicEvents), onCreate=lambda params: streaming.append(params.get("stream"))
    )

    await streamAnthropic(
        getModel("anthropic", "claude-fable-5-1"),
        _hello(),
        {"client": client, "onPayload": lambda payload, _model: {**payload, "stream": False}},
    ).result()

    assert streaming == [True]


async def test_omits_the_interleaved_thinking_beta_when_thinking_is_disabled():
    betaFeatures: list[Any] = []
    client = createFakeAnthropicClient(
        createSseResponse(minimalAnthropicEvents), onCreate=lambda params: betaFeatures.append(params.get("betas"))
    )

    await streamAnthropic(
        getModel("openrouter", "anthropic/claude-haiku-4.5"),
        _hello(),
        {"client": client, "thinkingEnabled": False},
    ).result()

    assert "interleaved-thinking-2025-05-14" not in (betaFeatures[0] or [])


async def test_passes_managed_beta_features_to_injected_clients():
    betaFeatures: list[Any] = []
    client = createFakeAnthropicClient(
        createSseResponse(minimalAnthropicEvents), onCreate=lambda params: betaFeatures.append(params.get("betas"))
    )

    result = await streamAnthropic(getModel("anthropic", "claude-fable-5-1"), _hello(), {"client": client}).result()

    assert result.stopReason == "stop"
    assert "mid-conversation-output-config-2026-07-01" in betaFeatures[0]
    assert "thinking-binding-controls-2026-08-01" in betaFeatures[0]


async def test_uses_the_serving_model_input_transformations_from_the_final_stream_event():
    events = [dict(event) for event in minimalAnthropicEvents]
    events[0]["data"] = json.dumps(
        {
            "type": "message_start",
            "message": {
                "id": "msg_transformations",
                "model": "claude-fable-5-1",
                "usage": {"input_tokens": 12, "output_tokens": 0},
                "input_transformations": [
                    {"type": "thinking_dropped", "path": "messages.1.content.0", "reason": "prefix_binding_mismatch"}
                ],
            },
        }
    )
    delta = json.loads(events[4]["data"])
    delta["input_transformations"] = [
        {"type": "thinking_dropped", "path": "messages.3.content.0", "reason": "model_binding_mismatch"}
    ]
    events[4]["data"] = json.dumps(delta)

    result = await streamAnthropic(
        getModel("anthropic", "claude-fable-5-1"),
        _hello(),
        {"client": createFakeAnthropicClient(createSseResponse(events))},
    ).result()

    [diagnostic] = [item.model_dump(exclude_none=True) for item in result.diagnostics or []]
    assert isinstance(diagnostic.pop("timestamp"), int)
    assert diagnostic == {
        "type": "anthropic_input_transformations",
        "details": {
            "transformations": [
                {"type": "thinking_dropped", "path": "messages.3.content.0", "reason": "model_binding_mismatch"}
            ]
        },
    }


async def test_repairs_malformed_sse_json_and_malformed_streamed_tool_json():
    model = getModel("anthropic", "claude-haiku-4-5")
    context = normalizeContext(
        {
            "messages": [UserMessage(content="Use the edit tool.", timestamp=time.time_ns() // 1_000_000)],
            "tools": [
                Tool(
                    name="edit",
                    description="Edit a file.",
                    parameters={
                        "type": "object",
                        "properties": {"path": {"type": "string"}, "text": {"type": "string"}},
                        "required": ["path", "text"],
                    },
                )
            ],
        }
    )

    malformedToolJsonDelta = (
        r'{"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":'
        r'"{\"path\":\"A\H\",\"text\":\"col1' + "\t" + r'col2\"}"}}'
    )

    response = createSseResponse(
        [
            _frame(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_test",
                        "usage": {
                            "input_tokens": 12,
                            "output_tokens": 0,
                            "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 0,
                        },
                    },
                },
            ),
            _frame(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "tool_use", "id": "toolu_test", "name": "edit", "input": {}},
                },
            ),
            {"event": "content_block_delta", "data": malformedToolJsonDelta},
            _frame("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _frame(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use"},
                    "usage": {
                        "input_tokens": 12,
                        "output_tokens": 5,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    },
                },
            ),
            _frame("message_stop", {"type": "message_stop"}),
        ]
    )

    result = await streamAnthropic(model, context, {"client": createFakeAnthropicClient(response)}).result()

    assert result.stopReason == "toolUse"
    assert result.errorMessage is None

    toolCall = next(block for block in result.content if block.type == "toolCall")
    assert toolCall.arguments == {"path": "A\\H", "text": "col1\tcol2"}


async def test_preserves_content_from_content_block_start_events():
    model = getModel("anthropic", "claude-haiku-4-5")
    context = normalizeContext({"messages": [UserMessage(content="Say hello.", timestamp=time.time_ns() // 1_000_000)]})
    response = createSseResponse(
        [
            _frame(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_initial_content",
                        "usage": {
                            "input_tokens": 12,
                            "output_tokens": 0,
                            "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 0,
                        },
                    },
                },
            ),
            _frame(
                "content_block_start",
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": "Initial text"}},
            ),
            _frame(
                "content_block_delta",
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": " plus delta"}},
            ),
            _frame("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _frame(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "thinking", "thinking": "Initial thinking", "signature": "initial signature"},
                },
            ),
            _frame(
                "content_block_delta",
                {"type": "content_block_delta", "index": 1, "delta": {"type": "thinking_delta", "thinking": " plus delta"}},
            ),
            _frame(
                "content_block_delta",
                {"type": "content_block_delta", "index": 1, "delta": {"type": "signature_delta", "signature": " plus delta"}},
            ),
            _frame("content_block_stop", {"type": "content_block_stop", "index": 1}),
            _frame(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {
                        "input_tokens": 12,
                        "output_tokens": 5,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    },
                },
            ),
            _frame("message_stop", {"type": "message_stop"}),
        ]
    )

    result = await streamAnthropic(model, context, {"client": createFakeAnthropicClient(response)}).result()

    assert _dumped(result.content) == [
        {"type": "text", "text": "Initial text plus delta"},
        {
            "type": "thinking",
            "thinking": "Initial thinking plus delta",
            "thinkingSignature": "initial signature plus delta",
        },
    ]


def _stop_response(messageId: str, inputTokens: int, delta: dict[str, Any]) -> Any:
    usage = {"input_tokens": inputTokens, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    return createSseResponse(
        [
            _frame("message_start", {"type": "message_start", "message": {"id": messageId, "usage": usage}}),
            _frame("message_delta", {"type": "message_delta", "delta": delta, "usage": usage}),
            _frame("message_stop", {"type": "message_stop"}),
        ]
    )


async def test_preserves_refusal_stop_details_from_message_delta():
    model = getModel("anthropic", "claude-fable-5")
    context = normalizeContext({"messages": [UserMessage(content="blocked request", timestamp=time.time_ns() // 1_000_000)]})
    explanation = (
        "This request triggered restrictions on violative cyber content and was blocked under Anthropic's Usage "
        "Policy. To learn more, provide feedback, or request an exemption based on how you use Claude, visit our help "
        "center: https://support.claude.com/en/articles/14604842-real-time-cyber-safeguards-on-claude."
    )
    response = _stop_response(
        "msg_01XFUDYJgAACzvnptvVoYEL",
        412,
        {"stop_reason": "refusal", "stop_details": {"type": "refusal", "category": "cyber", "explanation": explanation}},
    )

    result = await streamAnthropic(model, context, {"client": createFakeAnthropicClient(response)}).result()

    assert result.stopReason == "error"
    assert result.rawStopReason == "refusal"
    assert result.errorMessage == explanation


async def test_preserves_sensitive_stop_reasons_with_a_descriptive_error_message():
    model = getModel("anthropic", "claude-haiku-4-5")
    context = normalizeContext({"messages": [UserMessage(content="blocked request", timestamp=time.time_ns() // 1_000_000)]})
    response = _stop_response("msg_sensitive", 12, {"stop_reason": "sensitive"})

    result = await streamAnthropic(model, context, {"client": createFakeAnthropicClient(response)}).result()

    assert result.stopReason == "error"
    assert result.rawStopReason == "sensitive"
    assert result.errorMessage == "Provider stopped with: sensitive"


async def test_treats_message_delta_without_usage_as_a_no_op_for_usage_accumulation():
    model = getModel("anthropic", "claude-haiku-4-5")
    context = normalizeContext({"messages": [UserMessage(content="Say hello.", timestamp=time.time_ns() // 1_000_000)]})
    response = createSseResponse(
        [
            _frame("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}})
            if event["event"] == "message_delta"
            else event
            for event in minimalAnthropicEvents
        ]
    )

    result = await streamAnthropic(model, context, {"client": createFakeAnthropicClient(response)}).result()

    assert result.stopReason == "stop"
    assert result.errorMessage is None
    assert _dumped(result.content) == [{"type": "text", "text": "Hello"}]
    assert result.usage.input == 12
    assert result.usage.totalTokens == 12


async def test_ignores_unknown_sse_events_after_message_stop():
    model = getModel("anthropic", "claude-haiku-4-5")
    context = normalizeContext({"messages": [UserMessage(content="Say hello.", timestamp=time.time_ns() // 1_000_000)]})
    response = createSseResponse(
        [*minimalAnthropicEvents, {"event": "done", "data": "[DONE]"}, {"event": "proxy.stats", "data": "not json"}]
    )

    result = await streamAnthropic(model, context, {"client": createFakeAnthropicClient(response)}).result()

    assert result.stopReason == "stop"
    assert result.errorMessage is None
    assert _dumped(result.content) == [{"type": "text", "text": "Hello"}]

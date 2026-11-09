"""Translated from pi's ``packages/ai/test/anthropic-strict-tool-schema.test.ts`` and
``anthropic-cache-write-1h-cost.test.ts`` (v1.1.0)."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from misaka.ai.api.anthropic_messages import stream as streamAnthropic
from misaka.ai.compat import getModel
from misaka.ai.types import Model, ModelCost, Tool, UserMessage
from misaka.ai.utils.transcript import normalizeContext
from tests.anthropic_sse import createFakeAnthropicClient, createSseResponse

# --- anthropic-strict-tool-schema.test.ts ---------------------------------------------------


class PayloadCaptured(Exception):
    pass


def createModel() -> Model:
    return Model(
        id="claude-opus-4-8",
        name="Claude Opus 4.8",
        api="anthropic-messages",
        provider="test-anthropic",
        baseUrl="http://127.0.0.1:9",
        reasoning=True,
        input=["text"],
        cost=ModelCost(input=0, output=0, cacheRead=0, cacheWrite=0),
        contextWindow=200000,
        maxTokens=32000,
        compat={"forceAdaptiveThinking": True, "supportsStrictTools": True},
    )


def createTool(parameters: dict[str, Any], constrainedSampling: dict[str, Any] | None = None) -> Tool:
    return Tool(
        name="lookup",
        description="Look up a value",
        parameters=parameters,
        **({"constrainedSampling": constrainedSampling} if constrainedSampling else {}),
    )


def createStrictTool(parameters: dict[str, Any]) -> Tool:
    return createTool(parameters, {"type": "json_schema", "strict": "prefer"})


async def captureFirstTool(tool: Tool) -> dict[str, Any]:
    captured: list[Any] = []

    def onPayload(value: Any, _model: Any) -> Any:
        captured.append(value)
        raise PayloadCaptured()

    stream = streamAnthropic(
        createModel(),
        normalizeContext(
            {"messages": [UserMessage(content="Use the tool", timestamp=time.time_ns() // 1_000_000)], "tools": [tool]}
        ),
        {"apiKey": "test-key", "cacheRetention": "none", "onPayload": onPayload},
    )
    await stream.result()

    if not captured or not captured[0].get("tools"):
        raise RuntimeError("Expected a tool in the captured Anthropic payload")
    return captured[0]["tools"][0]


async def test_only_sends_the_full_input_schema_for_strict_json_schema_tools():
    legacyParameters = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
        "title": "LookupInput",
    }
    legacyTool = await captureFirstTool(createTool(legacyParameters))
    assert "strict" not in legacyTool
    assert legacyTool["input_schema"] == {
        "type": "object",
        "properties": legacyParameters["properties"],
        "required": legacyParameters["required"],
    }

    strictTool = await captureFirstTool(
        createStrictTool(
            {
                "type": "object",
                "properties": {"value": {"type": "string"}, "optional": {"type": "number"}},
                "required": ["value"],
                "title": "StrictLookupInput",
            }
        )
    )
    assert strictTool["strict"] is True
    assert strictTool["input_schema"]["additionalProperties"] is False
    assert strictTool["input_schema"]["required"] == ["value", "optional"]
    assert strictTool["input_schema"]["properties"]["optional"] == {"anyOf": [{"type": "number"}, {"type": "null"}]}
    assert strictTool["input_schema"]["title"] == "StrictLookupInput"


async def test_keeps_schema_definitions_on_non_strict_tools():
    # MISAKA fork: MISAKA's tools reference `$defs` from their properties, so the non-strict schema keeps them.
    parameters = {
        "type": "object",
        "properties": {"value": {"$ref": "#/$defs/v"}},
        "required": ["value"],
        "$defs": {"v": {"type": "string"}},
        "title": "LookupInput",
    }
    tool = await captureFirstTool(createTool(parameters))
    assert tool["input_schema"] == {
        "type": "object",
        "properties": parameters["properties"],
        "required": ["value"],
        "$defs": {"v": {"type": "string"}},
    }


async def test_sends_prefer_tools_non_strict_when_they_use_keywords_anthropic_strict_mode_rejects():
    # https://github.com/earendil-works/pi/issues/9953
    unsupportedParameters = [
        {"type": "object", "properties": {"timeoutMs": {"type": "integer", "minimum": 1, "maximum": 300000}}},
        {
            "type": "object",
            "properties": {
                "options": {
                    "type": "object",
                    "properties": {"tags": {"type": "array", "items": {"type": "string"}, "minItems": 2}},
                    "required": ["tags"],
                }
            },
            "required": ["options"],
        },
        {"type": "object", "properties": {"expression": {"type": "string", "format": "regex"}}, "required": ["expression"]},
    ]
    for parameters in unsupportedParameters:
        tool = await captureFirstTool(createStrictTool(parameters))
        assert "strict" not in tool, parameters

    supportedTool = await captureFirstTool(
        createStrictTool(
            {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "minLength": 1, "maxLength": 1000, "pattern": "^[a-z]+$"},
                    "url": {"type": "string", "format": "uri"},
                    "tags": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                },
                "required": ["code", "url", "tags"],
            }
        )
    )
    assert supportedTool["strict"] is True


# --- anthropic-cache-write-1h-cost.test.ts --------------------------------------------------


def _frame(event: str, data: dict[str, Any]) -> dict[str, str]:
    return {"event": event, "data": json.dumps(data)}


def eventsWithCacheCreation(cacheCreation: dict[str, int] | None) -> list[dict[str, str]]:
    startUsage: dict[str, Any] = {
        "input_tokens": 100,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 1_000_000,
    }
    if cacheCreation:
        startUsage["cache_creation"] = cacheCreation
    return [
        _frame("message_start", {"type": "message_start", "message": {"id": "msg_test", "usage": startUsage}}),
        _frame("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        _frame("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi"}}),
        _frame("content_block_stop", {"type": "content_block_stop", "index": 0}),
        _frame(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 1_000_000,
                },
            },
        ),
        _frame("message_stop", {"type": "message_stop"}),
    ]


# claude-opus-4-8: input 5, cacheWrite (5m) 6.25 per Mtok. 1h write = 2x input = 10.
context = normalizeContext({"messages": [UserMessage(content="hi", timestamp=time.time_ns() // 1_000_000)]})


async def test_prices_the_1h_portion_at_2x_input_and_the_rest_at_the_5m_rate():
    model = getModel("anthropic", "claude-opus-4-8")
    response = createSseResponse(
        eventsWithCacheCreation({"ephemeral_5m_input_tokens": 600_000, "ephemeral_1h_input_tokens": 400_000})
    )
    result = await streamAnthropic(model, context, {"client": createFakeAnthropicClient(response)}).result()

    assert result.usage.cacheWrite == 1_000_000
    assert result.usage.cacheWrite1h == 400_000
    # 600k * 6.25/Mtok + 400k * 10/Mtok = 3.75 + 4.0 = 7.75
    assert result.usage.cost.cacheWrite == pytest.approx(7.75, abs=1e-10)


async def test_prices_1h_cache_writes_reported_only_in_message_delta():
    # Regression for #9210: Vercel AI Gateway sends cache usage in message_delta, not message_start.
    model = getModel("vercel-ai-gateway", "anthropic/claude-haiku-4.5")
    response = createSseResponse(
        [
            _frame("message_start", {"type": "message_start", "message": {"id": "msg_test", "usage": {"input_tokens": 0, "output_tokens": 0}}}),
            _frame(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {
                        "input_tokens": 3,
                        "output_tokens": 4,
                        "cache_creation_input_tokens": 6535,
                        "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 6535},
                    },
                },
            ),
            _frame("message_stop", {"type": "message_stop"}),
        ]
    )
    result = await streamAnthropic(model, context, {"client": createFakeAnthropicClient(response)}).result()

    assert result.usage.cacheWrite == 6535
    assert result.usage.cacheWrite1h == 6535
    assert result.usage.cost.cacheWrite == pytest.approx((6535 * model.cost.input * 2) / 1_000_000, abs=1e-10)


async def test_falls_back_to_the_5m_rate_when_no_breakdown_is_reported():
    model = getModel("anthropic", "claude-opus-4-8")
    response = createSseResponse(eventsWithCacheCreation(None))
    result = await streamAnthropic(model, context, {"client": createFakeAnthropicClient(response)}).result()

    assert result.usage.cacheWrite == 1_000_000
    assert (result.usage.cacheWrite1h or 0) == 0
    # 1M * 6.25/Mtok = 6.25
    assert result.usage.cost.cacheWrite == pytest.approx(6.25, abs=1e-10)

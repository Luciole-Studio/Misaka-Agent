"""Translated from pi's Anthropic compat tests (v1.3.0): ``anthropic-temperature-compat.test.ts``,
``anthropic-force-adaptive-thinking.test.ts``, ``anthropic-empty-thinking-signature-compat.test.ts``,
``anthropic-eager-tool-input-compat.test.ts`` and the unit half of ``anthropic-thinking-disable.test.ts`` (its
credential-gated E2E block is not ported)."""

from __future__ import annotations

import time
from typing import Any

import pytest

from misaka.ai.api.anthropic_messages import stream as streamAnthropic
from misaka.ai.compat import getModel, getModels
from misaka.ai.types import (
    AssistantMessage,
    Context,
    Model,
    ModelCost,
    TextContent,
    ThinkingContent,
    Tool,
    Usage,
    UsageCost,
    UserMessage,
)
from misaka.ai.utils.transcript import normalizeContext
from tests.http_capture import captureServer
from tests.payload_capture import captureSimplePayload


def _now() -> int:
    return time.time_ns() // 1_000_000


def makeContext() -> Context:
    return Context(messages=[UserMessage(content="Hello", timestamp=_now())])


def _model(base: Model, **update: Any) -> Model:
    return type(base).model_validate({**base.model_dump(exclude_none=True), **update})


def _custom(id: str, name: str, compat: dict[str, Any] | None) -> Model:
    return Model(
        id=id,
        name=name,
        api="anthropic-messages",
        provider="vendor-proxy",
        baseUrl="http://127.0.0.1:9",
        reasoning=True,
        input=["text"],
        cost=ModelCost(input=0, output=0, cacheRead=0, cacheWrite=0),
        contextWindow=200000,
        maxTokens=32000,
        **({} if compat is None else {"compat": compat}),
    )


async def capturePayload(model: Model, options: dict[str, Any] | None = None) -> Any:
    return await captureSimplePayload(_model(model, baseUrl="http://127.0.0.1:9"), makeContext(), options)


# --- anthropic-temperature-compat.test.ts ---------------------------------------------------


async def test_omits_temperature_for_claude_opus_4_7():
    payload = await capturePayload(getModel("anthropic", "claude-opus-4-7"), {"temperature": 0})
    assert "temperature" not in payload


async def test_omits_temperature_for_claude_opus_4_8():
    payload = await capturePayload(getModel("anthropic", "claude-opus-4-8"), {"temperature": 0})
    assert "temperature" not in payload


async def test_omits_default_temperature_for_claude_opus_4_7():
    payload = await capturePayload(getModel("anthropic", "claude-opus-4-7"), {"temperature": 1})
    assert "temperature" not in payload


async def test_keeps_temperature_for_claude_opus_4_6():
    payload = await capturePayload(getModel("anthropic", "claude-opus-4-6"), {"temperature": 0})
    assert payload["temperature"] == 0


async def test_keeps_temperature_for_claude_sonnet_4_6():
    payload = await capturePayload(getModel("anthropic", "claude-sonnet-4-6"), {"temperature": 0})
    assert payload["temperature"] == 0


async def test_omits_temperature_for_custom_models_with_supports_temperature_disabled():
    model = _custom("vendor--claude-opus-4-7", "Vendor Proxy Opus 4.7", {"supportsTemperature": False})
    payload = await capturePayload(model, {"temperature": 0})
    assert "temperature" not in payload


# --- anthropic-force-adaptive-thinking.test.ts ----------------------------------------------


def _adaptiveCustom(compat: dict[str, Any] | None = None) -> Model:
    # Id intentionally does not match any built-in adaptive substring. This
    # mirrors corporate proxy schemes such as `anthropic--claude-opus-latest`.
    return _custom("vendor--claude-opus-latest", "Vendor Proxy Opus Latest", compat)


async def test_sends_legacy_thinking_payload_for_custom_model_ids_by_default():
    payload = await capturePayload(_adaptiveCustom(), {"reasoning": "medium"})

    assert payload["thinking"]["type"] == "enabled"
    assert "output_config" not in payload


async def test_sends_adaptive_thinking_payload_when_compat_force_adaptive_thinking_is_true():
    payload = await capturePayload(_adaptiveCustom({"forceAdaptiveThinking": True}), {"reasoning": "medium"})

    assert payload["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert payload["output_config"] == {"effort": "medium"}


async def test_uses_adaptive_thinking_with_native_xhigh_effort_for_claude_fable_5():
    payload = await capturePayload(getModel("anthropic", "claude-fable-5"), {"reasoning": "xhigh"})

    assert payload["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert payload["output_config"] == {"effort": "xhigh"}


async def test_allows_built_in_adaptive_models_to_opt_out_with_compat_force_adaptive_thinking_false():
    model = _model(getModel("anthropic", "claude-opus-4-8"), compat={"forceAdaptiveThinking": False})
    payload = await capturePayload(model, {"reasoning": "medium"})

    assert payload["thinking"]["type"] == "enabled"
    assert "output_config" not in payload


async def test_preserves_thinking_type_disabled_when_reasoning_is_off_regardless_of_override():
    payload = await capturePayload(_adaptiveCustom({"forceAdaptiveThinking": True}))

    assert payload["thinking"] == {"type": "disabled"}
    assert "output_config" not in payload


# --- anthropic-empty-thinking-signature-compat.test.ts --------------------------------------


def _signatureModel(allowEmptySignature: bool | None = None) -> Model:
    return Model(
        id="mimo-v2.5-pro",
        name="MiMo-V2.5-Pro",
        api="anthropic-messages",
        provider="xiaomi-token-plan-ams",
        baseUrl="http://127.0.0.1:9/anthropic",
        reasoning=True,
        input=["text"],
        cost=ModelCost(input=0, output=0, cacheRead=0, cacheWrite=0),
        contextWindow=1048576,
        maxTokens=1024,
        **({} if allowEmptySignature is None else {"compat": {"allowEmptySignature": allowEmptySignature}}),
    )


def _signatureContext(
    thinkingSignature: str,
    thinking: str = "internal reasoning",
    provider: str = "xiaomi-token-plan-ams",
    model: str = "mimo-v2.5-pro",
) -> Context:
    assistant = AssistantMessage(
        role="assistant",
        content=[ThinkingContent(thinking=thinking, thinkingSignature=thinkingSignature)],
        provider=provider,
        api="anthropic-messages",
        model=model,
        timestamp=_now(),
        usage=Usage(
            input=0,
            output=0,
            cacheRead=0,
            cacheWrite=0,
            totalTokens=0,
            cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
        ),
        stopReason="stop",
    )
    return Context(
        messages=[
            UserMessage(content="first", timestamp=_now()),
            assistant,
            UserMessage(content="second", timestamp=_now()),
        ]
    )


def _assistantContent(payload: Any) -> Any:
    return next(message for message in payload["messages"] if message["role"] == "assistant")["content"]


async def test_converts_empty_signature_thinking_to_text_by_default():
    payload = await captureSimplePayload(_signatureModel(), _signatureContext(""))
    assert _assistantContent(payload) == [{"type": "text", "text": "internal reasoning"}]


async def test_preserves_empty_thinking_text_when_the_signature_is_present():
    payload = await captureSimplePayload(_signatureModel(), _signatureContext("signed-thinking", ""))
    assert _assistantContent(payload) == [{"type": "thinking", "thinking": "", "signature": "signed-thinking"}]


async def test_preserves_empty_signature_thinking_when_allow_empty_signature_is_enabled():
    payload = await captureSimplePayload(_signatureModel(True), _signatureContext(" "))
    assert _assistantContent(payload) == [{"type": "thinking", "thinking": "internal reasoning", "signature": ""}]


def test_allows_empty_thinking_signatures_for_every_vercel_ai_gateway_model():
    # Regression for #9676: Vercel AI Gateway emits unsigned thinking for translated models.
    models = getModels("vercel-ai-gateway")
    assert len(models) > 0
    assert all(model.compat is not None and model.compat.allowEmptySignature is True for model in models)


@pytest.mark.parametrize(
    "modelId",
    [
        "accounts/fireworks/models/deepseek-v4p1-flash",
        "accounts/fireworks/models/qwen3p8-max",
        "accounts/fireworks/models/qwen3p8-2p4t-a95b",
        "accounts/fireworks/models/nemotron-3-ultra-nvfp4",
    ],
)
async def test_preserves_unsigned_thinking_for_fireworks(modelId):
    # Regression for #9323: Fireworks emits unsigned thinking that must survive replay.
    model = getModel("fireworks", modelId)
    assert model.compat.allowEmptySignature is True
    context = _signatureContext("", "internal reasoning", "fireworks", modelId)
    context.messages[1].content.append(TextContent(text="answer"))
    payload = await captureSimplePayload(model, context)
    assert _assistantContent(payload) == [
        {"type": "thinking", "thinking": "internal reasoning", "signature": ""},
        {"type": "text", "text": "answer"},
    ]


# --- anthropic-eager-tool-input-compat.test.ts ----------------------------------------------


def _eagerModel(baseUrl: str, compat: dict[str, Any] | None = None) -> Model:
    return Model(
        id="claude-opus-4-8",
        name="Claude Opus 4.8",
        api="anthropic-messages",
        provider="test-anthropic",
        baseUrl=baseUrl,
        reasoning=True,
        input=["text"],
        cost=ModelCost(input=0, output=0, cacheRead=0, cacheWrite=0),
        contextWindow=200000,
        maxTokens=32000,
        compat={"forceAdaptiveThinking": True, **(compat or {})},
    )


lookupTool = Tool(
    name="lookup",
    description="Look up a value",
    parameters={"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
)


def _toolContext(tools: list[Tool] | None = None) -> Context:
    tools = [lookupTool] if tools is None else tools
    return Context(
        messages=[UserMessage(content="Use the tool", timestamp=_now())],
        **({"tools": tools} if len(tools) > 0 else {}),
    )


async def captureAnthropicRequest(compat: dict[str, Any] | None, context: Context) -> Any:
    with captureServer() as server:
        stream = streamAnthropic(
            _eagerModel(server.baseUrl, compat),
            normalizeContext(context),
            {"apiKey": "test-key", "cacheRetention": "none"},
        )
        async for event in stream:
            if event.type in ("done", "error"):
                break
    if not server.requests:
        raise RuntimeError("Anthropic request was not captured")
    return server.requests[0]


async def test_sends_per_tool_eager_input_streaming_by_default():
    request = await captureAnthropicRequest(None, _toolContext())

    assert request.body["tools"][0]["eager_input_streaming"] is True
    assert "anthropic-beta" not in request.headers


async def test_uses_the_legacy_fine_grained_tool_streaming_beta_when_eager_tool_input_streaming_is_disabled():
    request = await captureAnthropicRequest({"supportsEagerToolInputStreaming": False}, _toolContext())

    assert "eager_input_streaming" not in request.body["tools"][0]
    assert request.headers["anthropic-beta"] == "fine-grained-tool-streaming-2025-05-14"


async def test_does_not_send_the_legacy_fine_grained_tool_streaming_beta_when_there_are_no_tools():
    request = await captureAnthropicRequest({"supportsEagerToolInputStreaming": False}, _toolContext([]))

    assert "tools" not in request.body
    assert "anthropic-beta" not in request.headers


# --- anthropic-thinking-disable.test.ts (unit) ----------------------------------------------


async def test_sends_thinking_disabled_for_budget_based_reasoning_models_when_thinking_is_off():
    payload = await capturePayload(getModel("anthropic", "claude-sonnet-4-5"))

    assert payload["thinking"] == {"type": "disabled"}
    assert "output_config" not in payload


async def test_sends_thinking_disabled_for_adaptive_reasoning_models_when_thinking_is_off():
    payload = await capturePayload(getModel("anthropic", "claude-opus-4-6"))

    assert payload["thinking"] == {"type": "disabled"}
    assert "output_config" not in payload


async def test_sends_thinking_disabled_for_claude_opus_4_8_when_thinking_is_off():
    payload = await capturePayload(getModel("anthropic", "claude-opus-4-8"))

    assert payload["thinking"] == {"type": "disabled"}
    assert "output_config" not in payload


async def test_omits_thinking_disabled_for_claude_fable_5_when_thinking_is_off():
    payload = await capturePayload(getModel("anthropic", "claude-fable-5"))

    assert "thinking" not in payload
    assert "output_config" not in payload


async def test_uses_adaptive_thinking_for_claude_opus_4_8_when_reasoning_is_enabled():
    payload = await capturePayload(getModel("anthropic", "claude-opus-4-8"), {"reasoning": "high"})

    assert payload["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert payload["output_config"] == {"effort": "high"}


async def test_uses_adaptive_thinking_for_claude_sonnet_5_when_reasoning_is_enabled():
    payload = await capturePayload(getModel("anthropic", "claude-sonnet-5"), {"reasoning": "high"})

    assert payload["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert payload["output_config"] == {"effort": "high"}


async def test_maps_xhigh_reasoning_to_effort_xhigh_for_claude_opus_4_8():
    payload = await capturePayload(getModel("anthropic", "claude-opus-4-8"), {"reasoning": "xhigh"})

    assert payload["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert payload["output_config"] == {"effort": "xhigh"}

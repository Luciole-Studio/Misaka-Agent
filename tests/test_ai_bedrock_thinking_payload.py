"""Translated from pi's ``packages/ai/test/bedrock-thinking-payload.test.ts`` (v1.1.0); its credential-gated E2E
block is not ported."""

from __future__ import annotations

import time
from typing import Any

import pytest

from misaka.ai.api.bedrock_converse_stream import stream as streamBedrock
from misaka.ai.compat import getModel
from misaka.ai.types import Context, Model, UserMessage
from misaka.ai.utils.transcript import normalizeContext

THINKING_BINDING_CONTROLS_BETA = "thinking-binding-controls-2026-08-01"
ADAPTIVE_WITH_BINDING = {
    "type": "adaptive",
    "display": "summarized",
    "block_binding": {"prefix_mismatch_behavior": "drop_block"},
}


class PayloadCaptured(Exception):
    pass


def makeContext() -> Context:
    return Context(messages=[UserMessage(content="Hello", timestamp=time.time_ns() // 1_000_000)])


def _model(base: Model, **update: Any) -> Model:
    return type(base).model_validate({**base.model_dump(exclude_none=True), **update})


async def _captureWith(model: Model, context: Context, options: dict[str, Any]) -> Any:
    captured: list[Any] = []

    def onPayload(payload: Any, _model: Any) -> Any:
        captured.append(payload)
        raise PayloadCaptured()

    s = streamBedrock(model, normalizeContext(context), {**options, "onPayload": onPayload})
    async for event in s:
        if event.type == "error":
            break
    if not captured:
        raise RuntimeError("Expected Bedrock payload to be captured before request abort")
    return captured[0]


async def capturePayload(model: Model, options: dict[str, Any] | None = None) -> Any:
    options = options or {}
    return await _captureWith(model, makeContext(), {**options, "reasoning": options.get("reasoning") or "high"})


def _opus48() -> Model:
    return _model(
        getModel("amazon-bedrock", "global.anthropic.claude-opus-4-6-v1"),
        id="global.anthropic.claude-opus-4-8-v1",
        name="Claude Opus 4.8 (Global)",
    )


async def test_uses_adaptive_thinking_for_claude_opus_4_8_when_reasoning_is_enabled():
    fields = (await capturePayload(_opus48()))["additionalModelRequestFields"]

    assert fields["thinking"] == ADAPTIVE_WITH_BINDING
    assert fields["output_config"] == {"effort": "high"}
    assert fields["anthropic_beta"] == [THINKING_BINDING_CONTROLS_BETA]


async def test_maps_xhigh_reasoning_to_effort_xhigh_for_claude_opus_4_8():
    fields = (await capturePayload(_opus48(), {"reasoning": "xhigh"}))["additionalModelRequestFields"]

    assert fields["thinking"] == ADAPTIVE_WITH_BINDING
    assert fields["output_config"] == {"effort": "xhigh"}
    assert fields["anthropic_beta"] == [THINKING_BINDING_CONTROLS_BETA]


@pytest.mark.parametrize(
    "modelId", ["global.anthropic.claude-fable-5", "global.anthropic.claude-sonnet-5", "global.anthropic.claude-opus-5"]
)
async def test_uses_adaptive_thinking_for_claude_5_models_when_reasoning_is_enabled(modelId):
    fields = (await capturePayload(getModel("amazon-bedrock", modelId)))["additionalModelRequestFields"]

    assert fields["thinking"] == ADAPTIVE_WITH_BINDING
    assert fields["output_config"] == {"effort": "high"}
    assert fields["anthropic_beta"] == [THINKING_BINDING_CONTROLS_BETA]


async def test_maps_xhigh_reasoning_to_effort_xhigh_for_claude_opus_5():
    fields = (await capturePayload(getModel("amazon-bedrock", "global.anthropic.claude-opus-5"), {"reasoning": "xhigh"}))[
        "additionalModelRequestFields"
    ]

    assert fields["thinking"] == ADAPTIVE_WITH_BINDING
    assert fields["output_config"] == {"effort": "xhigh"}
    assert fields["anthropic_beta"] == [THINKING_BINDING_CONTROLS_BETA]


async def test_maps_xhigh_reasoning_to_effort_xhigh_for_claude_fable_5():
    fields = (await capturePayload(getModel("amazon-bedrock", "global.anthropic.claude-fable-5"), {"reasoning": "xhigh"}))[
        "additionalModelRequestFields"
    ]

    assert fields["thinking"] == ADAPTIVE_WITH_BINDING
    assert fields["output_config"] == {"effort": "xhigh"}


async def test_sends_block_binding_and_the_binding_beta_for_claude_opus_5_5():
    # https://github.com/earendil-works/pi/issues/10324
    fields = (await capturePayload(getModel("amazon-bedrock", "global.anthropic.claude-opus-5-5")))[
        "additionalModelRequestFields"
    ]

    assert fields["thinking"] == ADAPTIVE_WITH_BINDING
    assert fields["anthropic_beta"] == [THINKING_BINDING_CONTROLS_BETA]


async def test_treats_claude_haiku_5_5_like_the_other_claude_5_models():
    # MISAKA addition: pi 1.1.0 adds `haiku-5` to every Claude 5 marker but has no Bedrock case for it.
    model = getModel("amazon-bedrock", "global.anthropic.claude-haiku-5-5")
    fields = (await capturePayload(model, {"reasoning": "xhigh"}))["additionalModelRequestFields"]
    payload = await _captureWith(
        model,
        Context(
            systemPrompt="You are helpful.",
            messages=[UserMessage(content="Hello", timestamp=time.time_ns() // 1_000_000)],
        ),
        {},
    )

    assert fields["thinking"] == ADAPTIVE_WITH_BINDING
    assert fields["output_config"] == {"effort": "xhigh"}
    assert fields["anthropic_beta"] == [THINKING_BINDING_CONTROLS_BETA]
    assert "cachePoint" in payload["system"][1]


@pytest.mark.parametrize("modelId", ["global.anthropic.claude-opus-4-6-v1", "global.anthropic.claude-sonnet-4-6"])
async def test_omits_block_binding_for_4_6_models(modelId):
    # Bedrock rejects block_binding on 4.6 models: "Extra inputs are not permitted" (#10324)
    fields = (await capturePayload(getModel("amazon-bedrock", modelId)))["additionalModelRequestFields"]

    assert fields["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert "anthropic_beta" not in fields


async def test_omits_display_for_gov_cloud_model_ids_on_non_adaptive_claude_thinking():
    model = _model(
        getModel("amazon-bedrock", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"),
        id="us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0",
        name="Claude Sonnet 4.5 (GovCloud)",
    )

    fields = (await capturePayload(model))["additionalModelRequestFields"]

    assert fields["thinking"] == {"type": "enabled", "budget_tokens": 16384}
    assert fields["anthropic_beta"] == ["interleaved-thinking-2025-05-14"]


async def test_omits_display_for_gov_cloud_regions_on_adaptive_claude_thinking():
    fields = (await capturePayload(_opus48(), {"region": "us-gov-west-1"}))["additionalModelRequestFields"]

    assert fields["thinking"] == {"type": "adaptive"}
    assert fields["output_config"] == {"effort": "high"}
    assert "anthropic_beta" not in fields


# --- Bedrock OpenAI reasoning payload --------------------------------------------------------


@pytest.mark.parametrize(
    ("reasoning", "effort"),
    [("minimal", "low"), ("low", "low"), ("medium", "medium"), ("high", "high"), ("xhigh", "xhigh"), ("max", "max")],
)
async def test_sends_reasoning_as_reasoning_effort_for_gpt_6_and_gpt_5_6(reasoning, effort):
    # Regression for #9331: the configured thinking level never reached OpenAI models on Bedrock.
    for id in ["global.openai.gpt-6-sol", "us.openai.gpt-6-luna", "global.openai.gpt-5.6-sol"]:
        payload = await capturePayload(getModel("amazon-bedrock", id), {"reasoning": reasoning})

        assert payload["additionalModelRequestFields"] == {"reasoning": {"effort": effort}}, id


async def test_sends_reasoning_effort_when_only_model_name_identifies_a_gpt_model():
    model = _model(
        getModel("amazon-bedrock", "global.openai.gpt-6-sol"),
        id="arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/my-profile",
        name="GPT-6 Sol",
    )

    payload = await capturePayload(model, {"reasoning": "medium"})

    assert payload["additionalModelRequestFields"] == {"reasoning": {"effort": "medium"}}


async def test_sends_flat_reasoning_effort_for_gpt_oss_clamped_to_high():
    model = getModel("amazon-bedrock", "openai.gpt-oss-120b-1:0")

    assert (await capturePayload(model, {"reasoning": "minimal"}))["additionalModelRequestFields"] == {
        "reasoning_effort": "low"
    }
    assert (await capturePayload(model, {"reasoning": "medium"}))["additionalModelRequestFields"] == {
        "reasoning_effort": "medium"
    }
    assert (await capturePayload(model, {"reasoning": "xhigh"}))["additionalModelRequestFields"] == {
        "reasoning_effort": "high"
    }


async def test_sends_no_reasoning_fields_when_reasoning_is_off():
    payload = await _captureWith(getModel("amazon-bedrock", "global.openai.gpt-6-sol"), makeContext(), {})

    assert payload["additionalModelRequestFields"] is None


# --- Application inference profile support ---------------------------------------------------

_PROFILE_ARN = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/my-profile"


async def test_uses_adaptive_thinking_when_model_name_contains_the_model_name_but_arn_does_not():
    model = _model(getModel("amazon-bedrock", "global.anthropic.claude-opus-4-6-v1"), id=_PROFILE_ARN, name="Claude Opus 4.6")

    fields = (await capturePayload(model))["additionalModelRequestFields"]

    assert fields["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert fields["output_config"] == {"effort": "high"}


async def test_injects_cache_points_when_model_name_identifies_a_supported_claude_model():
    model = _model(
        getModel("amazon-bedrock", "global.anthropic.claude-opus-4-6-v1"), id=_PROFILE_ARN, name="Claude Sonnet 4.6"
    )

    payload = await _captureWith(
        model,
        Context(
            systemPrompt="You are helpful.",
            messages=[UserMessage(content="Hello", timestamp=time.time_ns() // 1_000_000)],
        ),
        {},
    )

    # System prompt should have a cache point
    assert len(payload["system"]) == 2
    assert "cachePoint" in payload["system"][1]

    # Last user message should have a cache point
    lastMsg = payload["messages"][-1]
    assert "cachePoint" in lastMsg["content"][-1]


async def test_falls_back_to_fixed_budget_thinking_for_non_adaptive_claude_via_model_name():
    model = _model(
        getModel("amazon-bedrock", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"), id=_PROFILE_ARN, name="Claude Sonnet 4.5"
    )

    fields = (await capturePayload(model))["additionalModelRequestFields"]

    assert fields["thinking"]["type"] == "enabled"
    assert isinstance(fields["thinking"]["budget_tokens"], int)
    assert fields["anthropic_beta"] == ["interleaved-thinking-2025-05-14"]

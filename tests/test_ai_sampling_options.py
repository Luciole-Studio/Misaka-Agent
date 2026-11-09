"""Translated from pi's ``packages/ai/test/sampling-options.test.ts`` (v1.1.0)."""

from __future__ import annotations

import time
from typing import Any

import pytest

from misaka.ai.compat import stream, streamSimple
from misaka.ai.types import Context, Model, UserMessage


class PayloadCaptured(Exception):
    pass


def makeContext() -> Context:
    return Context(messages=[UserMessage(content="Hello", timestamp=time.time_ns() // 1_000_000)])


def makeModel(api: str, samplingParams: dict[str, Any] | None = None, **overrides: Any) -> Model:
    return Model.model_validate(
        {
            "id": "custom-model",
            "name": "Custom Model",
            "api": api,
            "provider": "custom-provider",
            "baseUrl": "http://127.0.0.1:9/v1",
            "reasoning": False,
            "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 128000,
            "maxTokens": 16384,
            "samplingParams": samplingParams,
            **overrides,
        }
    )


def capturingOptions(captured: list[Any]) -> dict[str, Any]:
    def onPayload(payload: Any, _model: Any) -> Any:
        captured.append(payload)
        raise PayloadCaptured("payload captured")

    return {"apiKey": "fake-key", "onPayload": onPayload}


async def capturePayload(model: Model, options: dict[str, Any] | None = None) -> dict[str, Any]:
    captured: list[Any] = []
    await stream(model, makeContext(), {**(options or {}), **capturingOptions(captured)}).result()
    if not captured:
        raise AssertionError("Expected payload to be captured before request failure")
    return captured[0]


async def captureSimplePayload(model: Model, options: dict[str, Any] | None = None) -> dict[str, Any]:
    captured: list[Any] = []
    await streamSimple(model, makeContext(), {**(options or {}), **capturingOptions(captured)}).result()
    if not captured:
        raise AssertionError("Expected payload to be captured before request failure")
    return captured[0]


async def test_merges_request_sampling_params_into_the_request_body():
    payload = await capturePayload(makeModel("openai-completions"), {"samplingParams": {"top_p": 0.95, "top_k": 0, "min_p": 0}})

    assert payload["top_p"] == 0.95
    assert payload["top_k"] == 0
    assert payload["min_p"] == 0


async def test_omits_sampling_params_when_neither_options_nor_model_set_them():
    payload = await capturePayload(makeModel("openai-completions"))

    assert "temperature" not in payload
    assert "top_p" not in payload


# Model defaults must apply to direct stream()/complete() calls, not only streamSimple() (#9506)
@pytest.mark.parametrize("api", ["openai-completions", "openai-responses", "azure-openai-responses"])
async def test_applies_model_level_sampling_params_with_request_keys_taking_precedence(api):
    payload = await capturePayload(makeModel(api, {"top_p": 0.95, "min_p": 0.05}), {"samplingParams": {"top_p": 0.5}})

    assert payload["top_p"] == 0.5
    assert payload["min_p"] == 0.05


async def test_passes_request_sampling_params_through_stream_simple():
    payload = await captureSimplePayload(makeModel("openai-completions"), {"samplingParams": {"top_p": 0.5}})

    assert payload["top_p"] == 0.5


async def test_applies_sampling_params_for_the_effective_thinking_level_over_model_defaults():
    payload = await captureSimplePayload(
        makeModel(
            "openai-completions",
            {"temperature": 1, "top_p": 0.95},
            reasoning=True,
            thinkingLevelMap={"low": None, "medium": None},
            samplingParamsByThinkingLevel={"high": {"temperature": 0.8, "top_k": 64}},
        ),
        {"reasoning": "low"},
    )

    assert payload["temperature"] == 0.8
    assert payload["top_p"] == 0.95
    assert payload["top_k"] == 64


async def test_applies_off_sampling_params_when_reasoning_is_disabled():
    payload = await captureSimplePayload(
        makeModel("openai-completions", None, samplingParamsByThinkingLevel={"off": {"temperature": 0.7}})
    )

    assert payload["temperature"] == 0.7


async def test_merges_stream_option_keys_over_thinking_level_keys():
    payload = await captureSimplePayload(
        makeModel(
            "openai-completions",
            None,
            reasoning=True,
            samplingParamsByThinkingLevel={"low": {"temperature": 0.6, "top_p": 0.95}},
        ),
        {"reasoning": "low", "samplingParams": {"top_p": 0.5}},
    )

    assert payload["temperature"] == 0.6
    assert payload["top_p"] == 0.5


@pytest.mark.parametrize("api", ["openai-completions", "openai-responses", "azure-openai-responses"])
async def test_applies_thinking_level_params_between_model_and_request_params(api):
    payload = await capturePayload(
        makeModel(
            api,
            {"temperature": 1, "top_p": 0.95},
            reasoning=True,
            samplingParamsByThinkingLevel={"low": {"temperature": 0.6, "top_k": 64}},
        ),
        {"reasoningEffort": "low", "samplingParams": {"top_p": 0.5}},
    )

    assert payload["temperature"] == 0.6
    assert payload["top_p"] == 0.5
    assert payload["top_k"] == 64


@pytest.mark.parametrize("api", ["openai-responses", "azure-openai-responses"])
async def test_uses_medium_sampling_params_for_summary_only_requests(api):
    payload = await capturePayload(
        makeModel(
            api,
            None,
            reasoning=True,
            samplingParamsByThinkingLevel={"off": {"temperature": 0.7}, "medium": {"temperature": 0.8}},
        ),
        {"reasoningSummary": "auto"},
    )

    assert payload["reasoning"]["effort"] == "medium"
    assert payload["temperature"] == 0.8


async def test_overrides_named_request_fields():
    payload = await capturePayload(makeModel("openai-completions"), {"temperature": 0, "samplingParams": {"temperature": 1}})

    assert payload["temperature"] == 1


async def test_is_ignored_by_non_openai_compatible_apis():
    payload = await capturePayload(makeModel("anthropic-messages"), {"samplingParams": {"top_p": 0.9, "top_k": 40}})

    assert "top_p" not in payload
    assert "top_k" not in payload

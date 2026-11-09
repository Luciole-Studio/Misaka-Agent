"""Translated from pi's ``packages/ai/test/qwen-token-plan-models.test.ts`` (v1.1.0)."""

from __future__ import annotations

import time
from typing import Any

import pytest

from misaka.ai.compat import getModels, streamSimple
from misaka.ai.env_api_keys import findEnvKeys
from misaka.ai.types import Context, UserMessage
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

TEXT_MODELS = [
    "MiniMax-M2.5",
    "deepseek-v3.2",
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "glm-5",
    "glm-5.1",
    "glm-5.2",
    "kimi-k2.5",
    "kimi-k2.6",
    "kimi-k2.7-code",
    "qwen3.6-flash",
    "qwen3.6-plus",
    "qwen3.7-max",
    "qwen3.7-plus",
    "qwen3.8-flash",
    "qwen3.8-max",
]

INDIVIDUAL_TEXT_MODELS = [
    "deepseek-v4-flash-0731",
    "deepseek-v4-pro",
    "deepseek-v4-pro-0813",
    "glm-5.2",
    "qwen3.6-flash",
    "qwen3.7-max",
    "qwen3.7-plus",
    "qwen3.8-flash",
    "qwen3.8-max",
]

IMAGE_MODELS = ["qwen-image-2.0", "qwen-image-2.0-pro", "wan2.7-image", "wan2.7-image-pro"]

QWEN_THINKING_MODELS = [
    "deepseek-v3.2",
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "glm-5",
    "glm-5.1",
    "glm-5.2",
    "kimi-k2.5",
    "kimi-k2.6",
    "kimi-k2.7-code",
    "qwen3.6-flash",
    "qwen3.6-plus",
    "qwen3.7-max",
    "qwen3.7-plus",
    "qwen3.8-flash",
    "qwen3.8-max",
]

QWEN_THINKING_MODEL_CASES = [
    *((provider, modelId) for provider in ("qwen-token-plan", "qwen-token-plan-cn") for modelId in QWEN_THINKING_MODELS),
    *(("qwen-token-plan-individual", modelId) for modelId in INDIVIDUAL_TEXT_MODELS),
]

QWEN_REASONING_EFFORT_MODELS = ["deepseek-v4-flash", "deepseek-v4-pro", "glm-5", "glm-5.1", "glm-5.2"]
QWEN38_MODELS = ["qwen3.8-flash", "qwen3.8-max"]

QWEN_REASONING_EFFORT_MODEL_CASES = [
    *(
        (provider, modelId)
        for provider in ("qwen-token-plan", "qwen-token-plan-cn")
        for modelId in QWEN_REASONING_EFFORT_MODELS
    ),
    *(
        ("qwen-token-plan-individual", modelId)
        for modelId in ["deepseek-v4-flash-0731", "deepseek-v4-pro", "deepseek-v4-pro-0813", "glm-5.2"]
    ),
]

QWEN38_MODEL_CASES = [
    (provider, modelId)
    for provider in ("qwen-token-plan", "qwen-token-plan-cn", "qwen-token-plan-individual")
    for modelId in QWEN38_MODELS
]


@pytest.fixture
def mockState(monkeypatch):
    return installFakeOpenAI(monkeypatch, DEFAULT_CHUNKS)


def _model(provider: str, modelId: str) -> Any:
    model = next((candidate for candidate in getModels(provider) if candidate.id == modelId), None)
    assert model is not None, f"Missing model: {provider}/{modelId}"
    return model


async def _payload(model: Any, reasoning: str) -> Any:
    payload: dict[str, Any] = {}

    def onPayload(params: Any, *_args: Any) -> None:
        payload["value"] = params

    await streamSimple(
        model,
        Context(messages=[UserMessage(content="Hi", timestamp=time.time_ns() // 1_000_000)]),
        {"apiKey": "test", "reasoning": reasoning, "onPayload": onPayload},
    ).result()
    return payload["value"]


# #9021
def test_exposes_exactly_the_documented_individual_text_models():
    modelIds = sorted(model.id for model in getModels("qwen-token-plan-individual"))

    assert modelIds == sorted(INDIVIDUAL_TEXT_MODELS)


def test_reuses_the_international_token_plan_environment_variable():
    assert findEnvKeys("qwen-token-plan-individual", {"QWEN_TOKEN_PLAN_API_KEY": "test"}) == ["QWEN_TOKEN_PLAN_API_KEY"]


@pytest.mark.parametrize("provider", ["qwen-token-plan", "qwen-token-plan-cn"])
def test_exposes_all_text_models(provider):
    modelIds = [model.id for model in getModels(provider)]
    for expected in TEXT_MODELS:
        assert expected in modelIds, f"{provider} should include {expected}"


@pytest.mark.parametrize("provider", ["qwen-token-plan", "qwen-token-plan-cn"])
def test_omits_image_models(provider):
    modelIds = [model.id for model in getModels(provider)]
    for excluded in IMAGE_MODELS:
        assert excluded not in modelIds, f"{provider} should not include {excluded}"


# docs: https://modelstudio.console.alibabacloud.com/ap-southeast-1?tab=api&commonbuy=1#/api/?type=model&url=3016807
@pytest.mark.parametrize(("provider", "modelId"), QWEN_THINKING_MODEL_CASES)
async def test_sends_qwen_thinking_fields(mockState, provider, modelId):
    payload = await _payload(_model(provider, modelId), "high")

    assert payload.get("enable_thinking") is True
    assert "thinking" not in payload


@pytest.mark.parametrize(("provider", "modelId"), QWEN_REASONING_EFFORT_MODEL_CASES)
def test_exposes_qwen_reasoning_effort_levels(provider, modelId):
    model = _model(provider, modelId)

    expected = {"minimal": None, "low": None, "medium": None, "high": "high", "xhigh": None, "max": "max"}
    assert {key: (model.thinkingLevelMap or {}).get(key, "absent") for key in expected} == expected


@pytest.mark.parametrize(("provider", "modelId"), QWEN38_MODEL_CASES)
def test_exposes_qwen38_reasoning_effort_levels(provider, modelId):
    model = _model(provider, modelId)

    expected = {"minimal": None, "low": "low", "medium": "medium", "high": None, "xhigh": "xhigh", "max": None}
    assert {key: (model.thinkingLevelMap or {}).get(key, "absent") for key in expected} == expected


@pytest.mark.parametrize("provider", ["qwen-token-plan", "qwen-token-plan-cn", "qwen-token-plan-individual"])
def test_omits_retired_qwen38_max_preview(provider):
    modelIds = [model.id for model in getModels(provider)]
    assert "qwen3.8-max-preview" not in modelIds


@pytest.mark.parametrize(("provider", "modelId"), QWEN_REASONING_EFFORT_MODEL_CASES)
async def test_sends_qwen_reasoning_effort(mockState, provider, modelId):
    payload = await _payload(_model(provider, modelId), "high")

    assert payload.get("reasoning_effort") == "high"


@pytest.mark.parametrize(("provider", "modelId"), QWEN38_MODEL_CASES)
async def test_sends_qwen38_xhigh_reasoning_effort(mockState, provider, modelId):
    payload = await _payload(_model(provider, modelId), "xhigh")

    assert payload.get("enable_thinking") is True
    assert payload.get("reasoning_effort") == "xhigh"
    assert "thinking" not in payload

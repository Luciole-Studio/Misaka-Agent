"""Translated from pi's ``packages/ai/test/max-thinking.test.ts`` (v1.1.0).

``xhigh.test.ts`` is not ported: it needs a live ``OPENAI_API_KEY``.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from misaka.ai.api.openai_codex_responses import (
    streamSimple as streamSimpleOpenAICodexResponses,
)
from misaka.ai.compat import getModel
from misaka.ai.models import clampThinkingLevel, getSupportedThinkingLevels
from misaka.ai.types import Context, Model, UserMessage
from misaka.ai.utils.transcript import normalizeContext


def mockToken() -> str:
    payload = base64.b64encode(
        json.dumps({"https://api.openai.com/auth": {"chatgpt_account_id": "acc_test"}}).encode("utf-8")
    ).decode("ascii")
    return f"aaa.{payload}.bbb"


def _model(**overrides: Any) -> Model:
    return Model.model_validate(
        {
            "api": "openai-completions",
            "provider": "test",
            "baseUrl": "https://example.com/v1",
            "reasoning": True,
            "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 128000,
            "maxTokens": 4096,
            **overrides,
        }
    )


def test_is_opt_in_for_ordinary_reasoning_models():
    model = _model(id="ordinary-reasoning", name="Ordinary Reasoning")

    assert getSupportedThinkingLevels(model) == ["off", "minimal", "low", "medium", "high"]
    assert clampThinkingLevel(model, "max") == "high"


@pytest.mark.parametrize("modelId", ["gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-6-luna", "gpt-6-sol"])
def test_exposes_xhigh_and_max_for_openai_codex(modelId):
    model = getModel("openai-codex", modelId)
    assert model is not None
    assert (model.thinkingLevelMap["xhigh"], model.thinkingLevelMap["max"]) == ("xhigh", "max")
    assert getSupportedThinkingLevels(model) == ["off", "minimal", "low", "medium", "high", "xhigh", "max"]


def test_supports_a_hole_between_high_and_max():
    model = _model(id="high-and-max", name="High and Max", thinkingLevelMap={"xhigh": None, "max": "max"})

    assert getSupportedThinkingLevels(model) == ["off", "minimal", "low", "medium", "high", "max"]
    assert clampThinkingLevel(model, "xhigh") == "max"


@pytest.mark.parametrize("modelId", ["gpt-5.6-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-6.1-sol"])
async def test_sends_max_to_the_codex_responses_api(modelId):
    model = getModel("openai-codex", modelId)
    context = normalizeContext(
        Context(systemPrompt="You are a helpful assistant.", messages=[UserMessage(content="Hello", timestamp=0)])
    )
    captured: dict[str, Any] = {}

    def onPayload(request: Any, _model: Any) -> Any:
        captured["payload"] = request
        raise RuntimeError("payload captured")

    await streamSimpleOpenAICodexResponses(
        model, context, {"apiKey": mockToken(), "reasoning": "max", "onPayload": onPayload}
    ).result()

    reasoning = captured["payload"]["reasoning"]
    assert (reasoning["effort"], reasoning["summary"]) == ("max", "auto")

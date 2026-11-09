"""Translated from pi's ``packages/ai/test/baseten-models.test.ts`` (v1.1.0)."""

from __future__ import annotations

from typing import Any

import pytest

from misaka.ai.compat import getModel, streamSimple
from misaka.ai.env_api_keys import findEnvKeys, getEnvApiKey
from misaka.ai.models import getSupportedThinkingLevels
from misaka.utils.values import read_field


def test_keeps_both_glm_5_2_endpoints_text_only():
    assert getModel("baseten", "zai-org/GLM-5.2").input == ["text"]
    assert getModel("baseten", "zai-org/GLM-5.2-Fast").input == ["text"]


async def capturePayload(model: Any, options: dict[str, Any]) -> dict[str, Any] | None:
    captured: dict[str, Any] = {}

    def onPayload(value: Any, *_: Any) -> Any:
        captured["payload"] = value
        raise RuntimeError("payload captured")

    await streamSimple(
        model,
        {"messages": [{"role": "user", "content": "test", "timestamp": 0}]},
        {"apiKey": "test-baseten-key", "onPayload": onPayload, **options},
    ).result()
    return captured.get("payload")


async def test_models_kimi_k2_6_reasoning_as_an_explicit_off_on_toggle():
    model = getModel("baseten", "moonshotai/Kimi-K2.6")

    assert model.thinkingLevelMap == {
        "off": "off",
        "minimal": None,
        "low": None,
        "medium": None,
        "high": "high",
        "xhigh": None,
        "max": None,
    }
    compat = model.compat
    assert read_field(compat, "supportsReasoningEffort") is False
    assert read_field(compat, "thinkingFormat") == "baseten"
    assert read_field(compat, "chatTemplateArgs") == {"enable_thinking": {"$var": "thinking.enabled"}}
    assert getSupportedThinkingLevels(model) == ["off", "high"]

    payload = await capturePayload(model, {"reasoning": "high"})

    assert payload is not None
    assert payload.get("chat_template_args") == {"enable_thinking": True}
    assert "reasoning_effort" not in payload


async def test_sends_baseten_chat_template_args_with_reasoning_effort():
    payload = await capturePayload(getModel("baseten", "zai-org/GLM-5.2"), {"reasoning": "high"})

    assert payload is not None
    assert payload.get("chat_template_args") == {"enable_thinking": True}
    assert payload.get("reasoning_effort") == "high"


async def test_disables_baseten_opt_in_reasoning_when_thinking_is_off():
    payload = await capturePayload(getModel("baseten", "zai-org/GLM-5.2"), {})

    assert payload is not None
    assert payload.get("chat_template_args") == {"enable_thinking": False}
    assert payload.get("reasoning_effort") == "none"


def test_resolves_baseten_api_key_from_the_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BASETEN_API_KEY", "test-baseten-key")

    assert findEnvKeys("baseten") == ["BASETEN_API_KEY"]
    assert getEnvApiKey("baseten") == "test-baseten-key"

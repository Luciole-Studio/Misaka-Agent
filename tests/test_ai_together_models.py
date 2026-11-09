"""Translated from pi's ``packages/ai/test/together-models.test.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ai.compat import getModel
from misaka.ai.env_api_keys import findEnvKeys, getEnvApiKey


def test_registers_the_default_kimi_k3_model_via_openai_compatible_chat_completions_api():
    model = getModel("together", "moonshotai/Kimi-K3")

    assert model is not None
    assert model.api == "openai-completions"
    assert model.provider == "together"
    assert model.baseUrl == "https://api.together.ai/v1"
    assert model.reasoning is True
    assert model.thinkingLevelMap == {"minimal": None, "low": None, "medium": None}
    assert model.input == ["text", "image"]
    assert model.contextWindow == 1048576
    assert model.maxTokens == 131072
    assert model.cost.model_dump(exclude_none=True) == {
        "input": 3,
        "output": 15,
        "cacheRead": 0.3,
        "cacheWrite": 0,
    }
    assert model.compat.model_dump(exclude_none=True) == {
        "supportsStore": False,
        "supportsDeveloperRole": False,
        "supportsReasoningEffort": False,
        "maxTokensField": "max_tokens",
        "thinkingFormat": "together",
        "supportsStrictMode": False,
        "supportsLongCacheRetention": False,
    }


def test_models_together_reasoning_controls_from_the_together_api_surface():
    gptOss = getModel("together", "openai/gpt-oss-120b")
    assert gptOss.thinkingLevelMap == {
        "off": None,
        "minimal": None,
        "low": "low",
        "medium": "medium",
        "high": "high",
        "max": None,
        "xhigh": None,
    }
    assert (gptOss.compat.supportsReasoningEffort, gptOss.compat.thinkingFormat) == (True, "openai")

    deepSeekV4 = getModel("together", "deepseek-ai/DeepSeek-V4-Pro-0813")
    assert deepSeekV4.thinkingLevelMap == {
        "minimal": None,
        "low": None,
        "medium": None,
        "high": "high",
        "xhigh": None,
    }
    assert (deepSeekV4.compat.supportsReasoningEffort, deepSeekV4.compat.thinkingFormat) == (True, "together")

    minimax = getModel("together", "MiniMaxAI/MiniMax-M2.7")
    assert minimax.thinkingLevelMap == {"off": None, "minimal": None, "low": None, "medium": None}
    assert minimax.compat.thinkingFormat is None
    assert minimax.compat.supportsReasoningEffort is False


def test_resolves_together_api_key_from_the_environment(monkeypatch):
    monkeypatch.setenv("TOGETHER_API_KEY", "test-together-key")

    assert findEnvKeys("together") == ["TOGETHER_API_KEY"]
    assert getEnvApiKey("together") == "test-together-key"

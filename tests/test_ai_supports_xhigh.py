"""Translated from pi's ``packages/ai/test/supports-xhigh.test.ts`` (v1.3.0)."""

from __future__ import annotations

from typing import Any

import pytest

from misaka.ai.compat import getModel
from misaka.ai.models import getSupportedThinkingLevels


def levels(provider: str, modelId: str) -> list[str]:
    model = getModel(provider, modelId)
    assert model is not None, f"{provider}/{modelId}"
    return getSupportedThinkingLevels(model)


def compatOf(model: Any) -> dict[str, Any]:
    return model.compat.model_dump(exclude_none=True)


def test_includes_max_but_not_xhigh_for_anthropic_opus_4_6_on_anthropic_messages_api():
    assert "max" in levels("anthropic", "claude-opus-4-6")
    assert "xhigh" not in levels("anthropic", "claude-opus-4-6")


def test_includes_xhigh_and_max_for_anthropic_opus_4_8_on_anthropic_messages_api():
    assert {"xhigh", "max"} <= set(levels("anthropic", "claude-opus-4-8"))


def test_includes_xhigh_and_max_for_anthropic_opus_5_on_anthropic_messages_api():
    assert {"xhigh", "max"} <= set(levels("anthropic", "claude-opus-5"))


def test_includes_claude_opus_5_5_with_its_always_on_effort_levels_and_official_pricing():
    model = getModel("anthropic", "claude-opus-5-5")
    assert model.cost.model_dump(exclude_none=True) == {"input": 4, "output": 20, "cacheRead": 0.2, "cacheWrite": 5}
    assert (model.contextWindow, model.maxTokens) == (1_000_000, 128_000)
    assert {
        "forceAdaptiveThinking": True,
        "supportsMidConvoEffort": True,
        "supportsMidConvoSystemMessages": True,
        "supportsMidConvoToolChanges": True,
    }.items() <= compatOf(model).items()
    assert getSupportedThinkingLevels(model) == ["low", "medium", "high", "xhigh", "max"]


def test_includes_claude_sonnet_5_5_with_managed_effort_levels_and_official_pricing():
    model = getModel("anthropic", "claude-sonnet-5-5")
    assert model.cost.model_dump(exclude_none=True) == {"input": 2, "output": 10, "cacheRead": 0.1, "cacheWrite": 2.5}
    assert (model.contextWindow, model.maxTokens) == (1_000_000, 128_000)
    assert {
        "forceAdaptiveThinking": True,
        "supportsMidConvoEffort": True,
        "supportsMidConvoSystemMessages": True,
        "supportsMidConvoToolChanges": True,
        "supportsTemperature": False,
    }.items() <= compatOf(model).items()
    assert getSupportedThinkingLevels(model) == ["low", "medium", "high", "xhigh", "max"]


def test_includes_max_but_not_xhigh_for_anthropic_sonnet_4_6_on_anthropic_messages_api():
    assert "max" in levels("anthropic", "claude-sonnet-4-6")
    assert "xhigh" not in levels("anthropic", "claude-sonnet-4-6")


def test_includes_xhigh_and_max_for_anthropic_sonnet_5_on_anthropic_messages_api():
    assert {"xhigh", "max"} <= set(levels("anthropic", "claude-sonnet-5"))


def test_includes_xhigh_and_max_but_not_off_for_anthropic_claude_fable_5_on_anthropic_messages_api():
    supported = levels("anthropic", "claude-fable-5")
    assert {"xhigh", "max"} <= set(supported)
    assert "off" not in supported


def test_does_not_include_xhigh_or_max_for_claude_sonnet_4_5():
    supported = levels("anthropic", "claude-sonnet-4-5")
    assert "xhigh" not in supported
    assert "max" not in supported


@pytest.mark.parametrize(
    "modelId",
    ["gpt-5.5", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-6.1-sol"],
)
def test_includes_xhigh_for_openai_codex_models(modelId):
    assert "xhigh" in levels("openai-codex", modelId)


@pytest.mark.parametrize("modelId", ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-6-sol", "gpt-6-luna"])
def test_includes_xhigh_and_max_for_openai_models(modelId):
    assert levels("openai", modelId) == ["off", "low", "medium", "high", "xhigh", "max"]


# OpenAI and Codex reject reasoning.effort "none" for GPT-6.1 Sol.
def test_does_not_support_off_for_gpt_6_1_sol():
    expected = {
        "openai": ["low", "medium", "high", "xhigh", "max"],
        "azure": ["low", "medium", "high", "xhigh", "max"],
        "openai-codex": ["minimal", "low", "medium", "high", "xhigh", "max"],
    }
    for provider, supported in expected.items():
        model = getModel(provider, "gpt-6.1-sol")
        assert model is not None, provider
        assert getSupportedThinkingLevels(model) == supported, provider
        assert "off" in model.thinkingLevelMap and model.thinkingLevelMap["off"] is None, provider


@pytest.mark.parametrize(
    ("modelId", "cost", "codexContextWindow"),
    [
        ("gpt-6-sol", {"input": 2, "output": 10, "cacheRead": 0.2, "cacheWrite": 2.5}, 272000),
        ("gpt-6-luna", {"input": 0.1, "output": 0.5, "cacheRead": 0.01, "cacheWrite": 0.125}, 272000),
        # Codex serves GPT-6.1 Sol with a 500K default context window (#10840)
        ("gpt-6.1-sol", {"input": 2, "output": 10, "cacheRead": 0.1, "cacheWrite": 2.5}, 500000),
    ],
)
def test_includes_official_metadata_for_openai_and_codex(modelId, cost, codexContextWindow):
    for provider in ["openai", "openai-codex"]:
        model = getModel(provider, modelId)
        assert model.input == ["text", "image"], provider
        assert model.cost.model_dump(exclude_none=True) == {
            **cost,
            "tiers": [
                {
                    "inputTokensAbove": 272000,
                    "input": pytest.approx(cost["input"] * 2),
                    "output": pytest.approx(cost["output"] * 1.5),
                    "cacheRead": pytest.approx(cost["cacheRead"] * 2),
                    "cacheWrite": pytest.approx(cost["cacheWrite"] * 2),
                }
            ],
        }, provider
        assert (model.contextWindow, model.maxTokens) == (
            codexContextWindow if provider == "openai-codex" else 272000,
            128000,
        ), provider
        assert {
            "supportsAdditionalTools": True,
            "supportsMidConvoSystemMessages": True,
            "supportsOpenAIGrammarTools": True,
            "supportsToolSearch": True,
        }.items() <= compatOf(model).items(), provider


def test_includes_only_medium_high_xhigh_for_openai_gpt_5_5_pro():
    assert levels("openai", "gpt-5.5-pro") == ["medium", "high", "xhigh"]


def test_includes_only_medium_high_xhigh_for_openrouter_gpt_5_5_pro():
    assert levels("openrouter", "openai/gpt-5.5-pro") == ["medium", "high", "xhigh"]


def test_includes_low_high_max_plus_off_for_deepseek_v4_1_flash_on_the_deepseek_provider():
    assert levels("deepseek", "deepseek-flash") == ["off", "low", "high", "max"]


def test_preserves_low_high_max_metadata_for_deepseek_v4_1_flash_on_openrouter():
    assert levels("openrouter", "deepseek/deepseek-v4.1-flash") == ["off", "low", "high", "max"]


def test_preserves_low_high_max_metadata_for_deepseek_v4_1_flash_on_opencode_go():
    assert levels("opencode-go", "deepseek-v4.1-flash") == ["low", "high", "max"]


def test_excludes_thinking_off_for_moonshot_kimi_k2_7_code_models():
    for provider in ["moonshotai", "moonshotai-cn"]:
        assert levels(provider, "kimi-k2.7-code") == ["minimal", "low", "medium", "high"]


@pytest.mark.parametrize("provider", ["moonshotai", "moonshotai-cn"])
def test_uses_the_verified_effort_options_for_kimi_k3(provider):
    assert levels(provider, "kimi-k3") == ["low", "high", "max"]


def test_includes_only_low_high_max_for_kimi_coding_k3():
    assert levels("kimi-coding", "k3") == ["low", "high", "max"]


def test_excludes_off_for_always_thinking_kimi_coding_highspeed():
    model = getModel("kimi-coding", "kimi-for-coding-highspeed")
    assert model is not None
    assert "off" not in getSupportedThinkingLevels(model)


def test_includes_only_high_for_opencode_grok_build():
    assert levels("opencode", "grok-build-0.1") == ["high"]


def test_includes_only_high_xhigh_plus_off_for_deepseek_v4_flash_on_openrouter():
    assert levels("openrouter", "deepseek/deepseek-v4-flash") == ["off", "high", "xhigh"]


def test_includes_max_but_not_xhigh_for_openrouter_opus_4_6_openai_completions_api():
    supported = levels("openrouter", "anthropic/claude-opus-4.6")
    assert "max" in supported
    assert "xhigh" not in supported


def test_includes_xhigh_and_max_for_bedrock_claude_opus_5():
    assert {"xhigh", "max"} <= set(levels("amazon-bedrock", "global.anthropic.claude-opus-5"))


def test_includes_xhigh_but_not_off_or_max_for_xai_grok_4_6():
    assert levels("xai", "grok-4.6") == ["low", "medium", "high", "xhigh"]


def test_includes_xhigh_and_max_but_not_off_for_bedrock_claude_fable_5():
    supported = levels("amazon-bedrock", "global.anthropic.claude-fable-5")
    assert {"xhigh", "max"} <= set(supported)
    assert "off" not in supported

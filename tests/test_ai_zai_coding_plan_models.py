"""Translated from pi's ``packages/ai/test/zai-coding-plan-models.test.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ai.providers.all import getBuiltinModel


def test_exposes_glm_4_6v_on_the_china_coding_plan_catalog():
    model = getBuiltinModel("zai-coding-cn", "glm-4.6v")

    assert (model.id, model.provider, model.api, model.baseUrl, model.reasoning, model.input) == (
        "glm-4.6v",
        "zai-coding-cn",
        "openai-completions",
        "https://open.bigmodel.cn/api/coding/paas/v4",
        True,
        ["text", "image"],
    )
    assert model.cost.model_dump(exclude_none=True) == {"input": 0.3, "output": 0.9, "cacheRead": 0, "cacheWrite": 0}
    assert (model.contextWindow, model.maxTokens) == (128000, 32768)
    assert (model.compat.maxTokensField, model.compat.thinkingFormat, model.compat.zaiToolStream) == (
        "max_tokens",
        "zai",
        True,
    )


def test_uses_api_equivalent_reference_costs_for_coding_plan_models():
    assert getBuiltinModel("zai", "glm-5.2").cost.model_dump(exclude_none=True) == {
        "input": 1.4,
        "output": 4.4,
        "cacheRead": 0.26,
        "cacheWrite": 0,
    }
    for provider in ["zai", "zai-coding-cn"]:
        assert getBuiltinModel(provider, "glm-5.3").cost.model_dump(exclude_none=True) == {
            "input": 1.4,
            "output": 4.4,
            "cacheRead": 0.26,
            "cacheWrite": 0,
        }


def test_keeps_zero_costs_for_coding_plan_models_without_a_matching_api_price():
    zeroCost = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}

    assert getBuiltinModel("zai", "glm-5.2-highspeed").cost.model_dump(exclude_none=True) == zeroCost

    for provider in ["zai", "zai-coding-cn"]:
        assert getBuiltinModel(provider, "glm-5.3-highspeed").cost.model_dump(exclude_none=True) == zeroCost

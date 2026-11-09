"""Translated from pi's ``packages/ai/test/model-catalog-types.test.ts`` (v1.1.0).

pi's ``expectTypeOf`` assertions check compile-time literal types; Python has none, so the runtime values they describe
are asserted instead.
"""

from __future__ import annotations

from misaka.ai.providers.github_copilot_models import GITHUB_COPILOT_MODELS
from misaka.ai.providers.xai_models import XAI_MODELS


def test_derives_model_api_id_and_provider_literals_from_grouped_model_data():
    assert (XAI_MODELS["grok-4.5"].api, XAI_MODELS["grok-4.5"].id, XAI_MODELS["grok-4.5"].provider) == (
        "openai-responses",
        "grok-4.5",
        "xai",
    )
    assert (XAI_MODELS["grok-4.6"].api, XAI_MODELS["grok-4.6"].id) == ("openai-responses", "grok-4.6")
    assert (XAI_MODELS["grok-4.7"].api, XAI_MODELS["grok-4.7"].id) == ("openai-responses", "grok-4.7")
    assert XAI_MODELS["grok-4.3"].api == "openai-responses"


def test_routes_github_copilot_grok_4_5_through_the_responses_api():
    assert GITHUB_COPILOT_MODELS["grok-4.5"].api == "openai-responses"


# Regression test for https://github.com/earendil-works/pi/issues/9209
def test_routes_all_github_copilot_gpt_models_through_the_responses_api():
    gptModels = [model for model in GITHUB_COPILOT_MODELS.values() if model.id.startswith("gpt-")]
    assert len(gptModels) > 0
    assert all(model.api == "openai-responses" for model in gptModels)
    assert GITHUB_COPILOT_MODELS["gpt-6-astra"].api == "openai-responses"
    for modelId in ["gpt-6-sol", "gpt-6-luna"]:
        model = GITHUB_COPILOT_MODELS[modelId]
        assert (model.api, model.contextWindow, model.maxTokens) == ("openai-responses", 1000000, 128000)
        assert (model.thinkingLevelMap["off"], model.thinkingLevelMap["max"]) == ("none", "max")

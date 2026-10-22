"""Translated from pi's ``packages/ai/test/xiaomi-models.test.ts`` and ``openrouter-cache-control-models.test.ts``
(v1.3.0)."""

from __future__ import annotations

from typing import Any

import pytest

from misaka.ai.compat import getModel, getModels, streamSimple
from misaka.ai.models import getSupportedThinkingLevels
from misaka.ai.types import Context, UserMessage
from misaka.utils.values import read_field

XIAOMI_PROVIDERS = ["xiaomi", "xiaomi-token-plan-cn", "xiaomi-token-plan-ams", "xiaomi-token-plan-sgp"]
DEPRECATED_MODEL_IDS = ["mimo-v2-flash", "mimo-v2-omni", "mimo-v2-pro"]
REPLACEMENT_MODEL_IDS = ["mimo-v2.5", "mimo-v2.5-pro"]


# --- Xiaomi MiMo models -------------------------------------------------------------------------


@pytest.mark.parametrize("provider", XIAOMI_PROVIDERS)
def test_omits_deprecated_models(provider: str):
    modelIds = [model.id for model in getModels(provider)]
    for modelId in DEPRECATED_MODEL_IDS:
        assert modelId not in modelIds


@pytest.mark.parametrize("provider", XIAOMI_PROVIDERS)
def test_keeps_replacement_models(provider: str):
    modelIds = [model.id for model in getModels(provider)]
    for modelId in REPLACEMENT_MODEL_IDS:
        assert modelId in modelIds


# #10818: MiMo Chat Completions only supports a thinking on/off toggle.
@pytest.mark.parametrize("provider", XIAOMI_PROVIDERS)
async def test_models_reasoning_as_an_off_on_toggle(provider: str):
    models = [model for model in getModels(provider) if model.reasoning]
    assert len(models) > 0

    for model in models:
        assert getSupportedThinkingLevels(model) == ["off", "high"]

        async def capturePayload(reasoning: str | None, model: Any = model) -> dict[str, Any] | None:
            payload: dict[str, Any] | None = None

            def onPayload(value: Any, _model: Any) -> None:
                nonlocal payload
                payload = value
                raise RuntimeError("payload captured")

            await streamSimple(
                model,
                Context(messages=[UserMessage(content="test", timestamp=0)]),
                {"apiKey": "test-xiaomi-key", "reasoning": reasoning, "onPayload": onPayload},
            ).result()
            return payload

        enabled = await capturePayload("high")
        assert enabled["thinking"] == {"type": "enabled"}
        assert "reasoning_effort" not in enabled

        disabled = await capturePayload(None)
        assert disabled["thinking"] == {"type": "disabled"}
        assert "reasoning_effort" not in disabled


# --- OpenRouter Anthropic latest alias metadata ---------------------------------------------------

OPENROUTER_ANTHROPIC_LATEST_MODEL_IDS = [
    "~anthropic/claude-fable-latest",
    "~anthropic/claude-haiku-latest",
    "~anthropic/claude-opus-latest",
    "~anthropic/claude-sonnet-latest",
]


@pytest.mark.parametrize("modelId", OPENROUTER_ANTHROPIC_LATEST_MODEL_IDS)
def test_keeps_completions_cache_control(modelId: str):
    model = getModel("openrouter", modelId)
    assert model.api == "openai-completions"
    assert read_field(model.compat, "cacheControlFormat") == "anthropic"

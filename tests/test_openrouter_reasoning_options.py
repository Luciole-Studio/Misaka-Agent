"""Translated from pi's ``packages/ai/test/openrouter-reasoning-options.test.ts`` (v1.1.0)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from misaka.ai.api.openai_completions import streamSimple
from misaka.ai.types import Model
from misaka.ai.utils.transcript import normalizeContext

# pi's tests import `../scripts/<name>.ts`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from openrouter_reasoning_options import getOpenRouterThinkingLevelMap

context = normalizeContext(
    {
        "messages": [{"role": "user", "content": "Hello", "timestamp": 0}],
    }
)


def openRouterModel(thinkingLevelMap: dict[str, Any] | None = None) -> Model:
    return Model.model_validate(
        {
            "id": "stealth/ox-alpha",
            "name": "Ox Alpha",
            "api": "openai-completions",
            "provider": "openrouter",
            "baseUrl": "https://example.invalid/v1",
            "reasoning": True,
            **({"thinkingLevelMap": thinkingLevelMap} if thinkingLevelMap is not None else {}),
            "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 128000,
            "maxTokens": 4096,
            "compat": {"thinkingFormat": "openrouter"},
        }
    )


async def capturePayload(model: Model, reasoning: str | None = None) -> dict[str, Any]:
    captured: list[Any] = []

    def onPayload(request: Any, _model: Any) -> Any:
        captured.append(request)
        raise RuntimeError("payload captured")

    await streamSimple(
        model,
        context,
        {"apiKey": "test", **({"reasoning": reasoning} if reasoning else {}), "onPayload": onPayload},
    ).result()
    if not captured:
        raise RuntimeError("OpenRouter payload was not captured")
    return captured[0]


class TestGetOpenRouterThinkingLevelMap:
    def test_marks_mandatory_reasoning_and_unsupported_efforts_unavailable(self) -> None:
        assert getOpenRouterThinkingLevelMap(
            {
                "mandatory": True,
                "default_enabled": True,
                "supported_efforts": ["max", "high", "low"],
                "default_effort": "max",
            }
        ) == {
            "off": None,
            "minimal": None,
            "low": "low",
            "medium": None,
            "high": "high",
            "xhigh": None,
            "max": "max",
        }

    def test_still_marks_off_unavailable_when_openrouter_omits_effort_metadata(self) -> None:
        assert getOpenRouterThinkingLevelMap({"mandatory": True}) == {"off": None}

    def test_keeps_off_available_while_restricting_optional_models_to_supported_efforts(self) -> None:
        assert getOpenRouterThinkingLevelMap(
            {
                "mandatory": False,
                "default_enabled": True,
                "supported_efforts": ["high", "low"],
            }
        ) == {
            "off": "none",
            "minimal": None,
            "low": "low",
            "medium": None,
            "high": "high",
            "xhigh": None,
            "max": None,
        }

    def test_does_not_add_metadata_for_optional_models_without_effort_controls(self) -> None:
        assert getOpenRouterThinkingLevelMap({"mandatory": False}) is None


class TestOpenRouterMandatoryReasoningPayloads:
    mandatoryMap = getOpenRouterThinkingLevelMap(
        {
            "mandatory": True,
            "supported_efforts": ["max", "high", "low"],
        }
    )

    @pytest.mark.asyncio
    async def test_omits_reasoning_when_a_background_call_does_not_request_it(self) -> None:
        assert "reasoning" not in await capturePayload(openRouterModel(self.mandatoryMap))

    @pytest.mark.asyncio
    async def test_still_sends_an_explicitly_selected_supported_effort(self) -> None:
        payload = await capturePayload(openRouterModel(self.mandatoryMap), "low")
        assert payload["reasoning"]["effort"] == "low"

    @pytest.mark.asyncio
    async def test_continues_to_explicitly_disable_reasoning_for_optional_models(self) -> None:
        payload = await capturePayload(openRouterModel())
        assert payload["reasoning"]["effort"] == "none"

"""Translated from pi's ``packages/ai/test/google-thinking-level-map.test.ts`` (v1.3.0)."""

from __future__ import annotations

from typing import Any

import pytest

from misaka.ai.api.google_generative_ai import streamSimple as streamSimpleGoogle
from misaka.ai.api.google_shared import resolveGoogleThinkingLevel
from misaka.ai.api.google_vertex import streamSimple as streamSimpleVertex
from misaka.ai.types import Model
from misaka.ai.utils.transcript import normalizeContext

context = normalizeContext({"messages": [{"role": "user", "content": "Hello", "timestamp": 0}]})


def googleModel(id: str, thinkingLevelMap: dict[str, Any]) -> Model:
    return Model.model_validate(
        {
            "id": id,
            "name": id,
            "api": "google-generative-ai",
            "provider": "test-google",
            "baseUrl": "https://example.invalid/v1beta",
            "reasoning": True,
            "thinkingLevelMap": thinkingLevelMap,
            "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 128000,
            "maxTokens": 4096,
        }
    )


def vertexModel(id: str, thinkingLevelMap: dict[str, Any]) -> Model:
    return Model.model_validate(
        {
            "id": id,
            "name": id,
            "api": "google-vertex",
            "provider": "test-vertex",
            "baseUrl": "https://example.invalid/v1",
            "reasoning": True,
            "thinkingLevelMap": thinkingLevelMap,
            "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 128000,
            "maxTokens": 4096,
        }
    )


async def _capture(streamSimple: Any, model: Model, reasoning: str | None, thinkingBudgets: Any) -> dict[str, Any]:
    payload: dict[str, Any] | None = None

    def onPayload(request: Any, _model: Any) -> Any:
        nonlocal payload
        payload = request
        raise RuntimeError("payload captured")

    result = await streamSimple(
        model,
        context,
        {
            "apiKey": "test",
            "reasoning": reasoning,
            "thinkingBudgets": thinkingBudgets,
            "onPayload": onPayload,
        },
    ).result()

    assert "payload captured" in result.errorMessage
    if not payload:
        raise RuntimeError("payload was not captured")
    return payload


async def captureGooglePayload(model: Model, reasoning: str | None = None, thinkingBudgets: Any = None) -> dict[str, Any]:
    return await _capture(streamSimpleGoogle, model, reasoning, thinkingBudgets)


async def captureVertexPayload(model: Model, reasoning: str | None = None, thinkingBudgets: Any = None) -> dict[str, Any]:
    return await _capture(streamSimpleVertex, model, reasoning, thinkingBudgets)


googleAdapters = [
    pytest.param(lambda id, thinkingLevelMap, reasoning=None: captureGooglePayload(googleModel(id, thinkingLevelMap), reasoning), id="Google Generative AI"),
    pytest.param(lambda id, thinkingLevelMap, reasoning=None: captureVertexPayload(vertexModel(id, thinkingLevelMap), reasoning), id="Google Vertex"),
]


def test_exhaustively_resolves_supported_logical_levels_and_mapping_values():
    defaultExpectations = {
        "minimal": "minimal",
        "low": "low",
        "medium": "medium",
        "high": "high",
    }
    for level, expected in defaultExpectations.items():
        assert resolveGoogleThinkingLevel(googleModel("gemini-3.7-flash", {}), level) == expected

    mappedExpectations = {
        "minimal": "minimal",
        "low": "low",
        "medium": "medium",
        "high": "high",
        "MINIMAL": "minimal",
        "LOW": "low",
        "MEDIUM": "medium",
        "HIGH": "high",
    }
    for mapped, expected in mappedExpectations.items():
        model = googleModel("gemini-3.7-flash", {"high": mapped, "xhigh": mapped, "max": mapped})
        assert resolveGoogleThinkingLevel(model, "high") == expected
        assert resolveGoogleThinkingLevel(model, "xhigh") == expected
        assert resolveGoogleThinkingLevel(model, "max") == expected

    invalidModel = googleModel("gemini-3.7-flash", {"xhigh": "extreme"})
    with pytest.raises(
        RuntimeError, match="Unsupported Google thinking level mapping for test-google/gemini-3.7-flash: xhigh -> extreme"
    ):
        resolveGoogleThinkingLevel(invalidModel, "xhigh")
    with pytest.raises(
        RuntimeError, match="Unsupported Google thinking level mapping for test-google/gemini-3.7-flash: max -> undefined"
    ):
        resolveGoogleThinkingLevel(googleModel("gemini-3.7-flash", {}), "max")


# Regression test for https://github.com/earendil-works/pi/issues/9455
@pytest.mark.parametrize("capture", googleAdapters)
async def test_uses_the_lowest_supported_level_when_reasoning_is_omitted(capture):
    payload = await capture(
        "gemini-3.8-flash",
        {
            "off": None,
            "minimal": None,
            "low": "low",
            "medium": "medium",
            "high": "high",
            "xhigh": None,
            "max": None,
        },
    )

    assert payload["config"]["thinkingConfig"] == {"thinkingLevel": "LOW"}


@pytest.mark.parametrize("capture", googleAdapters)
async def test_preserves_native_medium_effort_for_gemini_3_1_pro(capture):
    payload = await capture(
        "gemini-3.1-pro-preview",
        {"off": None, "minimal": None, "low": "low", "medium": "medium", "high": "high", "xhigh": None, "max": None},
        "medium",
    )

    assert payload["config"]["thinkingConfig"] == {"includeThoughts": True, "thinkingLevel": "MEDIUM"}


@pytest.mark.parametrize("capture", googleAdapters)
async def test_disables_gemini_2_5_thinking_when_reasoning_is_omitted(capture):
    payload = await capture("gemini-2.5-flash", {})

    assert payload["config"]["thinkingConfig"] == {"thinkingBudget": 0}


# Regression test for https://github.com/earendil-works/pi/issues/10890
@pytest.mark.parametrize("capture", googleAdapters)
async def test_uses_thinking_levels_for_gemini_4_models(capture):
    payload = await capture("gemini-4-argon", {}, "medium")

    assert payload["config"]["thinkingConfig"] == {"includeThoughts": True, "thinkingLevel": "MEDIUM"}


@pytest.mark.parametrize("reasoning", ["xhigh", "max"])
async def test_maps_google_generative_ai_extended_levels_to_a_supported_level(reasoning):
    payload = await captureGooglePayload(
        googleModel("gemini-3.7-flash", {"xhigh": "high", "max": "high"}),
        reasoning,
    )

    assert payload["config"]["thinkingConfig"] == {"includeThoughts": True, "thinkingLevel": "HIGH"}


async def test_honors_uppercase_provider_values_for_standard_google_generative_ai_levels():
    payload = await captureGooglePayload(googleModel("gemini-3.7-flash", {"high": "LOW"}), "high")

    assert payload["config"]["thinkingConfig"]["thinkingLevel"] == "LOW"


async def test_uses_mapped_google_generative_ai_levels_for_token_budgets():
    payload = await captureGooglePayload(
        googleModel("gemini-2.5-flash", {"xhigh": "high"}),
        "xhigh",
        {
            "high": 1234,
        },
    )

    assert payload["config"]["thinkingConfig"]["thinkingBudget"] == 1234


async def test_maps_google_vertex_extended_levels():
    payload = await captureVertexPayload(vertexModel("gemini-3.7-flash", {"xhigh": "high"}), "xhigh")

    assert payload["config"]["thinkingConfig"] == {"includeThoughts": True, "thinkingLevel": "HIGH"}


async def test_uses_mapped_google_vertex_levels_for_token_budgets():
    payload = await captureVertexPayload(
        vertexModel("gemini-2.5-flash", {"max": "high"}),
        "max",
        {
            "high": 4321,
        },
    )

    assert payload["config"]["thinkingConfig"]["thinkingBudget"] == 4321

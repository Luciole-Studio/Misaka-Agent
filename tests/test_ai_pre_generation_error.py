"""Translated from pi's ``packages/ai/test/pre-generation-error.test.ts`` (v1.1.0)."""

from __future__ import annotations

import pytest

from misaka.ai.api.anthropic_messages import streamSimple as streamAnthropic
from misaka.ai.api.azure_openai_responses import streamSimple as streamAzure
from misaka.ai.api.google_generative_ai import streamSimple as streamGoogle
from misaka.ai.api.mistral_conversations import streamSimple as streamMistral
from misaka.ai.api.openai_codex_responses import streamSimple as streamCodex
from misaka.ai.api.openai_completions import streamSimple as streamOpenAICompletions
from misaka.ai.api.openai_responses import streamSimple as streamOpenAIResponses
from misaka.ai.types import Model
from misaka.ai.utils.transcript import normalizeContext


def model(api: str) -> Model:
    return Model.model_validate(
        {
            "id": "test-model",
            "name": "Test",
            "api": api,
            "provider": "test-provider",
            "baseUrl": "https://example.invalid",
            "reasoning": False,
            "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 1_000,
            "maxTokens": 100,
        }
    )


def expectMissingAuthThrows(create) -> None:
    with pytest.raises(Exception, match="No API key for provider: test-provider"):
        create()


def test_throws_synchronously_when_auth_is_missing():
    context = normalizeContext({"messages": []})
    expectMissingAuthThrows(lambda: streamAnthropic(model("anthropic-messages"), context, {}))
    expectMissingAuthThrows(lambda: streamAzure(model("azure-openai-responses"), context, {}))
    expectMissingAuthThrows(lambda: streamGoogle(model("google-generative-ai"), context, {}))
    expectMissingAuthThrows(lambda: streamMistral(model("mistral-conversations"), context, {}))
    expectMissingAuthThrows(lambda: streamCodex(model("openai-codex-responses"), context, {}))
    expectMissingAuthThrows(lambda: streamOpenAICompletions(model("openai-completions"), context, {}))
    expectMissingAuthThrows(lambda: streamOpenAIResponses(model("openai-responses"), context, {}))

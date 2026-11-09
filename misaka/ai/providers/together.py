"""Translated from pi's ``packages/ai/src/providers/together.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ai.api.openai_completions_lazy import openAICompletionsApi
from misaka.ai.auth.helpers import envApiKeyAuth
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.together_models import TOGETHER_MODELS


def togetherProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="together",
            name="Together",
            baseUrl="https://api.together.ai/v1",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Together API key", ["TOGETHER_API_KEY"])),
            models=list(TOGETHER_MODELS.values()),
            api=openAICompletionsApi(),
        )
    )


__all__ = ["togetherProvider"]

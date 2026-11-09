"""Translated from pi's ``packages/ai/src/providers/moonshotai.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ai.api.openai_completions_lazy import openAICompletionsApi
from misaka.ai.auth.helpers import envApiKeyAuth
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.moonshotai_models import MOONSHOTAI_MODELS


def moonshotaiProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="moonshotai",
            name="Moonshot AI",
            baseUrl="https://api.moonshot.ai/v1",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Moonshot AI API key", ["MOONSHOT_API_KEY"])),
            models=list(MOONSHOTAI_MODELS.values()),
            api=openAICompletionsApi(),
        )
    )


__all__ = ["moonshotaiProvider"]

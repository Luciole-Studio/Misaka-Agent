"""Translated from pi's ``packages/ai/src/providers/cerebras.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ai.api.openai_completions_lazy import openAICompletionsApi
from misaka.ai.auth.helpers import envApiKeyAuth
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.cerebras_models import CEREBRAS_MODELS


def cerebrasProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="cerebras",
            name="Cerebras",
            baseUrl="https://api.cerebras.ai/v1",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Cerebras API key", ["CEREBRAS_API_KEY"])),
            models=list(CEREBRAS_MODELS.values()),
            api=openAICompletionsApi(),
        )
    )


__all__ = ["cerebrasProvider"]

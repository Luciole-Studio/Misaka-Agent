"""Translated from pi's ``packages/ai/src/providers/baseten.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ai.api.openai_completions_lazy import openAICompletionsApi
from misaka.ai.auth.helpers import envApiKeyAuth
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.baseten_models import BASETEN_MODELS


def basetenProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="baseten",
            name="Baseten",
            baseUrl="https://inference.baseten.co/v1",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Baseten API key", ["BASETEN_API_KEY"])),
            models=list(BASETEN_MODELS.values()),
            api=openAICompletionsApi(),
        )
    )


__all__ = ["basetenProvider"]

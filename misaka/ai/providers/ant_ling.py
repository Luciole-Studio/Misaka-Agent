"""Translated from pi's ``packages/ai/src/providers/ant-ling.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ai.api.openai_completions_lazy import openAICompletionsApi
from misaka.ai.auth.helpers import envApiKeyAuth
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.ant_ling_models import ANT_LING_MODELS


def antLingProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="ant-ling",
            name="Ant Ling",
            baseUrl="https://api.ant-ling.com/v1",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Ant Ling API key", ["ANT_LING_API_KEY"])),
            models=list(ANT_LING_MODELS.values()),
            api=openAICompletionsApi(),
        )
    )


__all__ = ["antLingProvider"]

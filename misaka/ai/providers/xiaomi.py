"""Translated from pi's ``packages/ai/src/providers/xiaomi.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ai.api.openai_completions_lazy import openAICompletionsApi
from misaka.ai.auth.helpers import envApiKeyAuth
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.xiaomi_models import XIAOMI_MODELS


def xiaomiProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="xiaomi",
            name="Xiaomi",
            baseUrl="https://api.xiaomimimo.com/v1",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Xiaomi API key", ["XIAOMI_API_KEY"])),
            models=list(XIAOMI_MODELS.values()),
            api=openAICompletionsApi(),
        )
    )


__all__ = ["xiaomiProvider"]

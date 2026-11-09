"""Translated from pi's ``packages/ai/src/providers/zai.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ai.api.openai_completions_lazy import openAICompletionsApi
from misaka.ai.auth.helpers import envApiKeyAuth
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.zai_models import ZAI_MODELS


def zaiProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="zai",
            name="Z.AI",
            baseUrl="https://api.z.ai/api/coding/paas/v4",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Z.AI API key", ["ZAI_API_KEY"])),
            models=list(ZAI_MODELS.values()),
            api=openAICompletionsApi(),
        )
    )


__all__ = ["zaiProvider"]

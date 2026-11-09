"""Translated from pi's ``packages/ai/src/providers/deepseek.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ai.api.openai_completions_lazy import openAICompletionsApi
from misaka.ai.auth.helpers import envApiKeyAuth
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.deepseek_models import DEEPSEEK_MODELS


def deepseekProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="deepseek",
            name="DeepSeek",
            baseUrl="https://api.deepseek.com",
            auth=ProviderAuth(apiKey=envApiKeyAuth("DeepSeek API key", ["DEEPSEEK_API_KEY"])),
            models=list(DEEPSEEK_MODELS.values()),
            api=openAICompletionsApi(),
        )
    )


__all__ = ["deepseekProvider"]

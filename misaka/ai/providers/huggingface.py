"""Translated from pi's ``packages/ai/src/providers/huggingface.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ai.api.openai_completions_lazy import openAICompletionsApi
from misaka.ai.auth.helpers import envApiKeyAuth
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.huggingface_models import HUGGINGFACE_MODELS


def huggingfaceProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="huggingface",
            name="Hugging Face",
            baseUrl="https://router.huggingface.co/v1",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Hugging Face token", ["HF_TOKEN"])),
            models=list(HUGGINGFACE_MODELS.values()),
            api=openAICompletionsApi(),
        )
    )


__all__ = ["huggingfaceProvider"]

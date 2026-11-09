"""Translated from pi's ``packages/ai/src/providers/nvidia.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ai.api.openai_completions_lazy import openAICompletionsApi
from misaka.ai.auth.helpers import envApiKeyAuth
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.nvidia_models import NVIDIA_MODELS


def nvidiaProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="nvidia",
            name="NVIDIA",
            baseUrl="https://integrate.api.nvidia.com/v1",
            auth=ProviderAuth(apiKey=envApiKeyAuth("NVIDIA API key", ["NVIDIA_API_KEY"])),
            models=list(NVIDIA_MODELS.values()),
            api=openAICompletionsApi(),
        )
    )


__all__ = ["nvidiaProvider"]

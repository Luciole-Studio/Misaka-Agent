"""Translated from pi's ``packages/ai/src/providers/mistral.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ai.api.mistral_conversations_lazy import mistralConversationsApi
from misaka.ai.auth.helpers import envApiKeyAuth
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.mistral_models import MISTRAL_MODELS


def mistralProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="mistral",
            name="Mistral",
            baseUrl="https://api.mistral.ai",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Mistral API key", ["MISTRAL_API_KEY"])),
            models=list(MISTRAL_MODELS.values()),
            api=mistralConversationsApi(),
        )
    )


__all__ = ["mistralProvider"]

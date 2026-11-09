"""Translated from pi's ``packages/ai/src/providers/fireworks.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ai.api.anthropic_messages_lazy import anthropicMessagesApi
from misaka.ai.api.openai_completions_lazy import openAICompletionsApi
from misaka.ai.auth.helpers import envApiKeyAuth
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.fireworks_models import FIREWORKS_MODELS


def fireworksProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="fireworks",
            name="Fireworks",
            baseUrl="https://api.fireworks.ai/inference",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Fireworks API key", ["FIREWORKS_API_KEY"])),
            models=list(FIREWORKS_MODELS.values()),
            api={
                "anthropic-messages": anthropicMessagesApi(),
                "openai-completions": openAICompletionsApi(),
            },
        )
    )


__all__ = ["fireworksProvider"]

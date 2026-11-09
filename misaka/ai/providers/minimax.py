"""Translated from pi's ``packages/ai/src/providers/minimax.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ai.api.anthropic_messages_lazy import anthropicMessagesApi
from misaka.ai.auth.helpers import envApiKeyAuth
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.minimax_models import MINIMAX_MODELS


def minimaxProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="minimax",
            name="MiniMax",
            baseUrl="https://api.minimax.io/anthropic",
            auth=ProviderAuth(apiKey=envApiKeyAuth("MiniMax API key", ["MINIMAX_API_KEY"])),
            models=list(MINIMAX_MODELS.values()),
            api=anthropicMessagesApi(),
        )
    )


__all__ = ["minimaxProvider"]

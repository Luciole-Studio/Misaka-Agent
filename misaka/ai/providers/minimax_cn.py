"""Translated from pi's ``packages/ai/src/providers/minimax-cn.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ai.api.anthropic_messages_lazy import anthropicMessagesApi
from misaka.ai.auth.helpers import envApiKeyAuth
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.minimax_cn_models import MINIMAX_CN_MODELS


def minimaxCnProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="minimax-cn",
            name="MiniMax CN",
            baseUrl="https://api.minimaxi.com/anthropic",
            auth=ProviderAuth(apiKey=envApiKeyAuth("MiniMax CN API key", ["MINIMAX_CN_API_KEY"])),
            models=list(MINIMAX_CN_MODELS.values()),
            api=anthropicMessagesApi(),
        )
    )


__all__ = ["minimaxCnProvider"]

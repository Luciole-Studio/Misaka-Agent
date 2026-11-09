"""Translated from pi's ``packages/ai/src/providers/openai-codex.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ai.api.openai_codex_responses_lazy import openAICodexResponsesApi
from misaka.ai.auth.helpers import lazyOAuth
from misaka.ai.auth.oauth.load import loadOpenAICodexOAuth
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.openai_codex_models import OPENAI_CODEX_MODELS


def openaiCodexProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="openai-codex",
            name="OpenAI Codex (legacy)",
            baseUrl="https://chatgpt.com/backend-api",
            auth=ProviderAuth(
                oauth=lazyOAuth({"name": "OpenAI (ChatGPT Plus/Pro)", "isSubscription": True, "load": loadOpenAICodexOAuth}),
            ),
            models=list(OPENAI_CODEX_MODELS.values()),
            api=openAICodexResponsesApi(),
        )
    )


__all__ = ["openaiCodexProvider"]

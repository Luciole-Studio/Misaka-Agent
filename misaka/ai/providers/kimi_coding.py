"""Translated from pi's ``packages/ai/src/providers/kimi-coding.ts`` (v1.3.0)."""

from __future__ import annotations

from misaka.ai.api.openai_completions_lazy import openAICompletionsApi
from misaka.ai.auth.helpers import envApiKeyAuth, lazyOAuth
from misaka.ai.auth.oauth.load import loadKimiCodingOAuth
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.kimi_coding_models import KIMI_CODING_MODELS


def kimiCodingProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="kimi-coding",
            name="Kimi For Coding",
            baseUrl="https://api.kimi.com/coding/v1",
            auth=ProviderAuth(
                apiKey=envApiKeyAuth("Kimi API key", ["KIMI_API_KEY"]),
                oauth=lazyOAuth({"name": "Kimi Code (subscription)", "isSubscription": True, "loginLabel": "Sign in with Kimi Code", "load": loadKimiCodingOAuth}),
            ),
            models=list(KIMI_CODING_MODELS.values()),
            api=openAICompletionsApi(),
        )
    )


__all__ = ["kimiCodingProvider"]

"""Translated from pi's ``packages/ai/src/providers/xai.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ai.api.openai_responses_lazy import openAIResponsesApi
from misaka.ai.auth.helpers import envApiKeyAuth, lazyOAuth
from misaka.ai.auth.oauth.load import loadXaiOAuth
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.xai_models import XAI_MODELS


def xaiProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="xai",
            name="xAI",
            baseUrl="https://api.x.ai/v1",
            auth=ProviderAuth(
                apiKey=envApiKeyAuth("xAI API key", ["XAI_API_KEY"]),
                oauth=lazyOAuth({"name": "xAI (Grok/X subscription)", "isSubscription": True, "loginLabel": "Sign in with SuperGrok or X Premium", "load": loadXaiOAuth}),
            ),
            models=list(XAI_MODELS.values()),
            api=openAIResponsesApi(),
        )
    )


__all__ = ["xaiProvider"]

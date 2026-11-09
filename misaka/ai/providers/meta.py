"""Translated from pi's ``packages/ai/src/providers/meta.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ai.api.openai_responses_lazy import openAIResponsesApi
from misaka.ai.auth.helpers import envApiKeyAuth, lazyOAuth
from misaka.ai.auth.oauth.load import loadMetaOAuth
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.meta_models import META_MODELS


def metaProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="meta",
            name="Meta",
            baseUrl="https://api.meta.ai/v1",
            auth=ProviderAuth(
                apiKey=envApiKeyAuth("Meta Model API key", ["META_API_KEY"]),
                oauth=lazyOAuth({"name": "Meta (Muse subscription)", "isSubscription": True, "loginLabel": "Sign in with Meta", "load": loadMetaOAuth}),
            ),
            models=list(META_MODELS.values()),
            api=openAIResponsesApi(),
        )
    )


__all__ = ["metaProvider"]

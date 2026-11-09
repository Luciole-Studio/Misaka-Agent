"""Translated from pi's ``packages/ai/src/providers/openai.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ai.api.openai_decisions_lazy import openAIDecisionsApi
from misaka.ai.api.openai_responses_lazy import openAIResponsesApi
from misaka.ai.auth.helpers import envApiKeyAuth, lazyOAuth
from misaka.ai.auth.oauth.load import loadOpenAIChatGPTOAuth
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.openai_models import OPENAI_CLASSIFIER_MODELS, OPENAI_MODELS
from misaka.ai.utils.model_operations import isModelType
from misaka.utils.values import read_field


def openaiProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="openai",
            name="OpenAI",
            baseUrl="https://api.openai.com/v1",
            auth=ProviderAuth(
                apiKey=envApiKeyAuth("OpenAI API key", ["OPENAI_API_KEY"]),
                oauth=lazyOAuth({"name": "OpenAI (ChatGPT subscription)", "isSubscription": True, "loginLabel": "Sign in with ChatGPT", "load": loadOpenAIChatGPTOAuth}),
            ),
            models=[
                *OPENAI_MODELS.values(),
                *OPENAI_CLASSIFIER_MODELS.values(),
            ],
            # Sign in with ChatGPT tokens only reach the Responses API; the Decisions API rejects them.
            filterAllModels=lambda models, credential: (
                [model for model in models if not isModelType(model, "classifier")]
                if read_field(credential, "type") == "oauth"
                else models
            ),
            api=openAIResponsesApi(),
            classifiers={
                "openai-decisions": openAIDecisionsApi(),
            },
        )
    )


__all__ = ["openaiProvider"]

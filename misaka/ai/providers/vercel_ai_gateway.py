"""Translated from pi's ``packages/ai/src/providers/vercel-ai-gateway.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ai.api.anthropic_messages_lazy import anthropicMessagesApi
from misaka.ai.api.typesafe_system_one_lazy import typesafeSystemOneApi
from misaka.ai.auth.helpers import envApiKeyAuth
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.vercel_ai_gateway_models import (
    VERCEL_AI_GATEWAY_CLASSIFIER_MODELS,
    VERCEL_AI_GATEWAY_MODELS,
)


def vercelAIGatewayProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="vercel-ai-gateway",
            name="Vercel AI Gateway",
            baseUrl="https://ai-gateway.vercel.sh",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Vercel AI Gateway API key", ["AI_GATEWAY_API_KEY"])),
            models=[*VERCEL_AI_GATEWAY_MODELS.values(), *VERCEL_AI_GATEWAY_CLASSIFIER_MODELS.values()],
            api=anthropicMessagesApi(),
            # AI Gateway serves TypeSafe's System One protocol at /typesafe/v1/systemone.
            classifiers={"typesafe-system-one": typesafeSystemOneApi()},
        )
    )


__all__ = ["vercelAIGatewayProvider"]

"""Translated from pi's ``packages/ai/src/providers/cloudflare-ai-gateway.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ai.api.anthropic_messages_lazy import anthropicMessagesApi
from misaka.ai.api.openai_completions_lazy import openAICompletionsApi
from misaka.ai.api.openai_responses_lazy import openAIResponsesApi
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.cloudflare_ai_gateway_models import (
    CLOUDFLARE_AI_GATEWAY_MODELS,
)
from misaka.ai.providers.cloudflare_auth import cloudflareAIGatewayAuth
from misaka.ai.providers.cloudflare_stream import cloudflareStreams


def cloudflareAIGatewayProvider() -> Provider:
    # The api map is pinned to all three APIs: models.dev's gateway catalog drops and
    # restores `workers-ai/*` (openai-completions) entries over time, and inference from
    # `models` alone would otherwise reject the openai-completions entry whenever the
    # generated catalog happens to contain none.
    return createProvider(
        CreateProviderOptions(
            id="cloudflare-ai-gateway",
            name="Cloudflare AI Gateway",
            auth=ProviderAuth(apiKey=cloudflareAIGatewayAuth()),
            models=list(CLOUDFLARE_AI_GATEWAY_MODELS.values()),
            api={
                "anthropic-messages": cloudflareStreams(anthropicMessagesApi()),
                "openai-completions": cloudflareStreams(openAICompletionsApi()),
                "openai-responses": cloudflareStreams(openAIResponsesApi()),
            },
        )
    )


__all__ = ["cloudflareAIGatewayProvider"]

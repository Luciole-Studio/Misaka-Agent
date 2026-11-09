"""Translated from pi's ``packages/ai/src/providers/cloudflare-workers-ai.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ai.api.cloudflare_workers_ai_system_one_lazy import (
    cloudflareWorkersAISystemOneApi,
)
from misaka.ai.api.openai_completions_lazy import openAICompletionsApi
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.cloudflare_auth import cloudflareWorkersAIAuth
from misaka.ai.providers.cloudflare_stream import (
    cloudflareClassifier,
    cloudflareStreams,
)
from misaka.ai.providers.cloudflare_workers_ai_models import (
    CLOUDFLARE_WORKERS_AI_CLASSIFIER_MODELS,
    CLOUDFLARE_WORKERS_AI_MODELS,
)


def cloudflareWorkersAIProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="cloudflare-workers-ai",
            name="Cloudflare Workers AI",
            auth=ProviderAuth(apiKey=cloudflareWorkersAIAuth()),
            models=[
                *CLOUDFLARE_WORKERS_AI_MODELS.values(),
                *CLOUDFLARE_WORKERS_AI_CLASSIFIER_MODELS.values(),
            ],
            api=cloudflareStreams(openAICompletionsApi()),
            classifiers={
                "cloudflare-workers-ai-system-one": cloudflareClassifier(cloudflareWorkersAISystemOneApi()),
            },
        )
    )


__all__ = ["cloudflareWorkersAIProvider"]

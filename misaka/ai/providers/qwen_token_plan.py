"""Translated from pi's ``packages/ai/src/providers/qwen-token-plan.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ai.api.openai_completions_lazy import openAICompletionsApi
from misaka.ai.auth.helpers import envApiKeyAuth
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.qwen_token_plan_models import QWEN_TOKEN_PLAN_MODELS


def qwenTokenPlanProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="qwen-token-plan",
            name="Qwen Token Plan",
            baseUrl="https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Qwen Token Plan API key", ["QWEN_TOKEN_PLAN_API_KEY"])),
            models=list(QWEN_TOKEN_PLAN_MODELS.values()),
            api=openAICompletionsApi(),
        )
    )


__all__ = ["qwenTokenPlanProvider"]

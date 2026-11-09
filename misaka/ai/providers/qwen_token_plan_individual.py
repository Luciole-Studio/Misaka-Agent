"""Translated from pi's ``packages/ai/src/providers/qwen-token-plan-individual.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ai.api.openai_completions_lazy import openAICompletionsApi
from misaka.ai.auth.helpers import envApiKeyAuth
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.qwen_token_plan_individual_models import (
    QWEN_TOKEN_PLAN_INDIVIDUAL_MODELS,
)


def qwenTokenPlanIndividualProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="qwen-token-plan-individual",
            name="Qwen Token Plan Individual",
            baseUrl="https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Qwen Token Plan Individual API key", ["QWEN_TOKEN_PLAN_API_KEY"])),
            models=list(QWEN_TOKEN_PLAN_INDIVIDUAL_MODELS.values()),
            api=openAICompletionsApi(),
        )
    )


__all__ = ["qwenTokenPlanIndividualProvider"]

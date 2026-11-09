"""Translated from pi's ``packages/ai/src/providers/xiaomi-token-plan-ams.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ai.api.openai_completions_lazy import openAICompletionsApi
from misaka.ai.auth.helpers import envApiKeyAuth
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.xiaomi_token_plan_ams_models import (
    XIAOMI_TOKEN_PLAN_AMS_MODELS,
)


def xiaomiTokenPlanAmsProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="xiaomi-token-plan-ams",
            name="Xiaomi Token Plan AMS",
            baseUrl="https://token-plan-ams.xiaomimimo.com/v1",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Xiaomi Token Plan AMS API key", ["XIAOMI_TOKEN_PLAN_AMS_API_KEY"])),
            models=list(XIAOMI_TOKEN_PLAN_AMS_MODELS.values()),
            api=openAICompletionsApi(),
        )
    )


__all__ = ["xiaomiTokenPlanAmsProvider"]

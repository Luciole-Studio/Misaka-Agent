"""Translated from pi's ``packages/ai/src/providers/xiaomi-token-plan-sgp.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ai.api.openai_completions_lazy import openAICompletionsApi
from misaka.ai.auth.helpers import envApiKeyAuth
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.xiaomi_token_plan_sgp_models import (
    XIAOMI_TOKEN_PLAN_SGP_MODELS,
)


def xiaomiTokenPlanSgpProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="xiaomi-token-plan-sgp",
            name="Xiaomi Token Plan SGP",
            baseUrl="https://token-plan-sgp.xiaomimimo.com/v1",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Xiaomi Token Plan SGP API key", ["XIAOMI_TOKEN_PLAN_SGP_API_KEY"])),
            models=list(XIAOMI_TOKEN_PLAN_SGP_MODELS.values()),
            api=openAICompletionsApi(),
        )
    )


__all__ = ["xiaomiTokenPlanSgpProvider"]

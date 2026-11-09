"""Translated from pi's ``packages/ai/src/providers/xiaomi-token-plan-cn.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ai.api.openai_completions_lazy import openAICompletionsApi
from misaka.ai.auth.helpers import envApiKeyAuth
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.xiaomi_token_plan_cn_models import XIAOMI_TOKEN_PLAN_CN_MODELS


def xiaomiTokenPlanCnProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="xiaomi-token-plan-cn",
            name="Xiaomi Token Plan CN",
            baseUrl="https://token-plan-cn.xiaomimimo.com/v1",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Xiaomi Token Plan CN API key", ["XIAOMI_TOKEN_PLAN_CN_API_KEY"])),
            models=list(XIAOMI_TOKEN_PLAN_CN_MODELS.values()),
            api=openAICompletionsApi(),
        )
    )


__all__ = ["xiaomiTokenPlanCnProvider"]

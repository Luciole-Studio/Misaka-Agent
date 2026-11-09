"""Translated from pi's ``packages/ai/src/providers/zai-coding-cn.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ai.api.openai_completions_lazy import openAICompletionsApi
from misaka.ai.auth.helpers import envApiKeyAuth
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.zai_coding_cn_models import ZAI_CODING_CN_MODELS


def zaiCodingCnProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="zai-coding-cn",
            name="Z.AI Coding CN",
            baseUrl="https://open.bigmodel.cn/api/coding/paas/v4",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Z.AI Coding CN API key", ["ZAI_CODING_CN_API_KEY"])),
            models=list(ZAI_CODING_CN_MODELS.values()),
            api=openAICompletionsApi(),
        )
    )


__all__ = ["zaiCodingCnProvider"]

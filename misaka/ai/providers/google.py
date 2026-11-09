"""Translated from pi's ``packages/ai/src/providers/google.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ai.api.google_generative_ai_lazy import googleGenerativeAIApi
from misaka.ai.auth.helpers import envApiKeyAuth
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.google_models import GOOGLE_MODELS


def googleProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="google",
            name="Google",
            baseUrl="https://generativelanguage.googleapis.com/v1beta",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Gemini API key", ["GEMINI_API_KEY"])),
            models=list(GOOGLE_MODELS.values()),
            api=googleGenerativeAIApi(),
        )
    )


__all__ = ["googleProvider"]

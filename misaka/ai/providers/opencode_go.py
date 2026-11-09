"""Translated from pi's ``packages/ai/src/providers/opencode-go.ts`` (v1.3.0)."""

from __future__ import annotations

from misaka.ai.api.anthropic_messages_lazy import anthropicMessagesApi
from misaka.ai.api.google_generative_ai_lazy import googleGenerativeAIApi
from misaka.ai.api.openai_completions_lazy import openAICompletionsApi
from misaka.ai.api.openai_responses_lazy import openAIResponsesApi
from misaka.ai.auth.helpers import envApiKeyAuth
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.opencode_go_models import OPENCODE_GO_MODELS
from misaka.ai.providers.opencode_headers import withOpenCodeSessionHeader


def opencodeGoProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="opencode-go",
            name="OpenCode Go",
            auth=ProviderAuth(apiKey=envApiKeyAuth("OpenCode API key", ["OPENCODE_API_KEY"])),
            models=list(OPENCODE_GO_MODELS.values()),
            api={
                "anthropic-messages": withOpenCodeSessionHeader(anthropicMessagesApi()),
                "google-generative-ai": withOpenCodeSessionHeader(googleGenerativeAIApi()),
                "openai-completions": withOpenCodeSessionHeader(openAICompletionsApi()),
                "openai-responses": withOpenCodeSessionHeader(openAIResponsesApi()),
            },
        )
    )


__all__ = ["opencodeGoProvider"]

"""Translated from pi's ``packages/ai/src/providers/github-copilot.ts`` (v1.1.0).

MISAKA fork: the multi-statement ``filterModels`` arrow is the module-level ``_filterModels``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from misaka.ai.api.anthropic_messages_lazy import anthropicMessagesApi
from misaka.ai.api.openai_completions_lazy import openAICompletionsApi
from misaka.ai.api.openai_responses_lazy import openAIResponsesApi
from misaka.ai.auth.helpers import envApiKeyAuth, lazyOAuth
from misaka.ai.auth.oauth.load import loadGitHubCopilotOAuth
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.github_copilot_models import GITHUB_COPILOT_MODELS
from misaka.ai.types import Model
from misaka.utils.values import read_field


def _filterModels(models: Sequence[Model], credential: Any) -> Sequence[Model]:
    if read_field(credential, "type") != "oauth":
        return models
    availableModelIds = read_field(credential, "availableModelIds")
    if not isinstance(availableModelIds, list) or not all(isinstance(id, str) for id in availableModelIds):
        return models
    available = set(availableModelIds)
    return [model for model in models if model.id in available]


def githubCopilotProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="github-copilot",
            name="GitHub Copilot",
            baseUrl="https://api.individual.githubcopilot.com",
            auth=ProviderAuth(
                apiKey=envApiKeyAuth("GitHub Copilot token", ["COPILOT_GITHUB_TOKEN"]),
                oauth=lazyOAuth({"name": "GitHub Copilot", "isSubscription": True, "load": loadGitHubCopilotOAuth}),
            ),
            models=list(GITHUB_COPILOT_MODELS.values()),
            filterModels=_filterModels,
            api={
                "anthropic-messages": anthropicMessagesApi(),
                "openai-completions": openAICompletionsApi(),
                "openai-responses": openAIResponsesApi(),
            },
        )
    )


__all__ = ["githubCopilotProvider"]

"""OAuth credential management for AI providers."""

from __future__ import annotations

import time
from typing import TypedDict

from misaka.ai.utils.oauth.anthropic import (
    anthropicOAuthProvider,
    loginAnthropic,
    refreshAnthropicToken,
)
from misaka.ai.utils.oauth.device_code import (
    OAuthDeviceCodeCompleteResult,
    OAuthDeviceCodeFailedResult,
    OAuthDeviceCodePendingResult,
    OAuthDeviceCodePollOptions,
    OAuthDeviceCodePollResult,
    OAuthDeviceCodeSlowDownResult,
    pollOAuthDeviceCodeFlow,
)
from misaka.ai.utils.oauth.github_copilot import (
    getGitHubCopilotBaseUrl,
    githubCopilotOAuthProvider,
    loginGitHubCopilot,
    normalizeDomain,
    refreshGitHubCopilotToken,
)
from misaka.ai.utils.oauth.openai_codex import (
    loginOpenAICodex,
    openaiCodexOAuthProvider,
    refreshOpenAICodexToken,
)
from misaka.ai.utils.oauth.types import (
    OAuthAuthInfo,
    OAuthCredentials,
    OAuthDeviceCodeInfo,
    OAuthLoginCallbacks,
    OAuthPrompt,
    OAuthProvider,
    OAuthProviderId,
    OAuthProviderInfo,
    OAuthProviderInterface,
    OAuthSelectOption,
    OAuthSelectPrompt,
)

_BUILT_IN_OAUTH_PROVIDERS: list[OAuthProviderInterface] = [
    anthropicOAuthProvider,
    githubCopilotOAuthProvider,
    openaiCodexOAuthProvider,
]

_oauth_provider_registry: dict[str, OAuthProviderInterface] = {provider.id: provider for provider in _BUILT_IN_OAUTH_PROVIDERS}


class _OAuthApiKeyResult(TypedDict):
    newCredentials: OAuthCredentials
    apiKey: str


def get_oauth_provider(provider_id: OAuthProviderId):
    return _oauth_provider_registry.get(provider_id)


def register_oauth_provider(provider: OAuthProviderInterface) -> None:
    _oauth_provider_registry[provider.id] = provider


def reset_oauth_providers() -> None:
    _oauth_provider_registry.clear()
    for provider in _BUILT_IN_OAUTH_PROVIDERS:
        _oauth_provider_registry[provider.id] = provider


def get_oauth_providers() -> list[OAuthProviderInterface]:
    return list(_oauth_provider_registry.values())


async def get_oauth_api_key(
    provider_id: OAuthProviderId,
    credentials: dict[str, OAuthCredentials],
) -> _OAuthApiKeyResult | None:
    provider = get_oauth_provider(provider_id)
    if provider is None:
        raise RuntimeError(f"Unknown OAuth provider: {provider_id}")

    creds = credentials.get(provider_id)
    if creds is None:
        return None

    if int(time.time() * 1000) >= creds.expires:
        try:
            creds = await provider.refreshToken(creds)
        except Exception as error:
            raise RuntimeError(f"Failed to refresh OAuth token for {provider_id}") from error

    api_key = provider.getApiKey(creds)
    return {"newCredentials": creds, "apiKey": api_key}


getOAuthProvider = get_oauth_provider
registerOAuthProvider = register_oauth_provider
resetOAuthProviders = reset_oauth_providers
getOAuthProviders = get_oauth_providers
getOAuthApiKey = get_oauth_api_key

__all__ = [
    "OAuthAuthInfo",
    "OAuthCredentials",
    "OAuthDeviceCodeCompleteResult",
    "OAuthDeviceCodeFailedResult",
    "OAuthDeviceCodeInfo",
    "OAuthDeviceCodePendingResult",
    "OAuthDeviceCodePollOptions",
    "OAuthDeviceCodePollResult",
    "OAuthDeviceCodeSlowDownResult",
    "OAuthLoginCallbacks",
    "OAuthPrompt",
    "OAuthProvider",
    "OAuthProviderId",
    "OAuthProviderInfo",
    "OAuthProviderInterface",
    "OAuthSelectOption",
    "OAuthSelectPrompt",
    "anthropicOAuthProvider",
    "getGitHubCopilotBaseUrl",
    "getOAuthApiKey",
    "getOAuthProvider",
    "getOAuthProviders",
    "githubCopilotOAuthProvider",
    "loginAnthropic",
    "loginGitHubCopilot",
    "loginOpenAICodex",
    "normalizeDomain",
    "openaiCodexOAuthProvider",
    "pollOAuthDeviceCodeFlow",
    "refreshAnthropicToken",
    "refreshGitHubCopilotToken",
    "refreshOpenAICodexToken",
    "registerOAuthProvider",
    "resetOAuthProviders",
    ]

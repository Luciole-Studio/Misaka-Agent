"""OAuth credential management for AI providers."""

from __future__ import annotations

import inspect
import time
from typing import Any, TypedDict

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
from misaka.ai.utils.oauth.kimi_coding import (
    kimiCodingOAuthProvider,
    loginKimiCoding,
    refreshKimiCodingToken,
)
from misaka.ai.utils.oauth.openai_codex import (
    loginOpenAICodex,
    loginOpenAICodexDeviceCode,
    openaiCodexOAuthProvider,
    refreshOpenAICodexToken,
)
from misaka.ai.utils.oauth.openrouter import (
    loginOpenRouter,
    openrouterOAuthProvider,
)
from misaka.ai.utils.oauth.radius import radius_oauth_provider
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
from misaka.ai.utils.oauth.xai import (
    loginXai,
    refreshXaiToken,
    xaiOAuthProvider,
)

_BUILT_IN_OAUTH_PROVIDERS: list[OAuthProviderInterface] = [
    anthropicOAuthProvider,
    githubCopilotOAuthProvider,
    kimiCodingOAuthProvider,
    openaiCodexOAuthProvider,
    openrouterOAuthProvider,
    radius_oauth_provider,
    xaiOAuthProvider,
]

_oauth_provider_registry: dict[str, OAuthProviderInterface] = {provider.id: provider for provider in _BUILT_IN_OAUTH_PROVIDERS}


# pi auth/resolve.ts:117 DEFAULT_OAUTH_MINIMUM_VALIDITY_MS -- a token with under five
# minutes of validity left is refreshed now, rather than handed to a request that would
# outlive it.
OAUTH_MINIMUM_VALIDITY_MS = 5 * 60 * 1000


def oauth_credentials_expire_soon(credentials: OAuthCredentials) -> bool:
    """Whether a stored OAuth token is inside the refresh window."""
    return int(time.time() * 1000) + OAUTH_MINIMUM_VALIDITY_MS >= credentials.expires


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


def _call_refresh_token(refresh_token: Any, credentials: OAuthCredentials, signal: Any) -> Any:
    """Invoke a provider's ``refreshToken`` with the upstream two-argument contract.

    Upstream is JavaScript, where a handler declared with one parameter simply ignores
    the second argument. Python raises instead, so a provider that still takes only the
    credentials is called with one argument -- the same observable behaviour, and the
    same shape ``misaka.core.extensions.runner`` uses for extension handlers.
    """
    try:
        signature = inspect.signature(refresh_token)
    except (TypeError, ValueError):
        return refresh_token(credentials, signal)

    parameters = signature.parameters.values()
    if any(parameter.kind == inspect.Parameter.VAR_POSITIONAL for parameter in parameters):
        return refresh_token(credentials, signal)
    positional = [
        parameter
        for parameter in parameters
        if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if len(positional) >= 2:
        return refresh_token(credentials, signal)
    return refresh_token(credentials)


async def get_oauth_api_key(
    provider_id: OAuthProviderId,
    credentials: dict[str, OAuthCredentials],
    signal: Any | None = None,
) -> _OAuthApiKeyResult | None:
    provider = get_oauth_provider(provider_id)
    if provider is None:
        raise RuntimeError(f"Unknown OAuth provider: {provider_id}")

    creds = credentials.get(provider_id)
    if creds is None:
        return None

    if oauth_credentials_expire_soon(creds):
        try:
            creds = await _call_refresh_token(provider.refreshToken, creds, signal)
        except Exception as error:
            raise RuntimeError(f"Failed to refresh OAuth token for {provider_id}") from error

    api_key = provider.getApiKey(creds)
    return {"newCredentials": creds, "apiKey": api_key}


getOAuthProvider = get_oauth_provider
registerOAuthProvider = register_oauth_provider
resetOAuthProviders = reset_oauth_providers
getOAuthProviders = get_oauth_providers
getOAuthApiKey = get_oauth_api_key
oauthCredentialsExpireSoon = oauth_credentials_expire_soon

__all__ = [
    "OAUTH_MINIMUM_VALIDITY_MS",
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
    "kimiCodingOAuthProvider",
    "loginAnthropic",
    "loginGitHubCopilot",
    "loginKimiCoding",
    "loginOpenAICodex",
    "loginOpenAICodexDeviceCode",
    "loginOpenRouter",
    "loginXai",
    "normalizeDomain",
    "oauthCredentialsExpireSoon",
    "openaiCodexOAuthProvider",
    "openrouterOAuthProvider",
    "pollOAuthDeviceCodeFlow",
    "radius_oauth_provider",
    "refreshAnthropicToken",
    "refreshGitHubCopilotToken",
    "refreshKimiCodingToken",
    "refreshOpenAICodexToken",
    "refreshXaiToken",
    "registerOAuthProvider",
    "resetOAuthProviders",
    "xaiOAuthProvider",
    ]

"""Translated from pi's ``packages/ai/src/providers/anthropic.ts`` (v1.1.0)."""

from __future__ import annotations

from typing import Any

from misaka.ai.api.anthropic_messages_lazy import anthropicMessagesApi
from misaka.ai.auth.helpers import lazyOAuth
from misaka.ai.auth.oauth.load import loadAnthropicOAuth
from misaka.ai.auth.types import (
    ApiKeyAuth,
    ApiKeyCredential,
    AuthContext,
    AuthResult,
    ModelAuth,
    ProviderAuth,
    ProviderAuthInteraction,
    SecretPrompt,
)
from misaka.ai.env_api_keys import (
    ANTHROPIC_API_KEY_ENV,
    ANTHROPIC_AUTH_TOKEN_ENV,
    ANTHROPIC_FEDERATION_RULE_ID_ENV,
    ANTHROPIC_IDENTITY_TOKEN_FILE_ENV,
    ANTHROPIC_OAUTH_TOKEN_ENV,
    ANTHROPIC_ORGANIZATION_ID_ENV,
    ANTHROPIC_SERVICE_ACCOUNT_ID_ENV,
    ANTHROPIC_WORKSPACE_ID_ENV,
)
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.anthropic_models import ANTHROPIC_MODELS
from misaka.ai.types import ProviderEnv
from misaka.utils.js import throwIfAborted


def _anthropicApiKeyAuth() -> ApiKeyAuth:
    async def login(interaction: ProviderAuthInteraction) -> ApiKeyCredential:
        throwIfAborted(interaction.signal)
        key = await interaction.prompt(SecretPrompt(type="secret", message="Enter Anthropic API key"))
        throwIfAborted(interaction.signal)
        return ApiKeyCredential(type="api_key", key=key)

    async def resolve(*, ctx: AuthContext, credential: ApiKeyCredential | None = None, signal: Any) -> AuthResult | None:
        throwIfAborted(signal)
        if credential is not None and credential.key:
            return AuthResult(auth=ModelAuth(apiKey=credential.key), env=credential.env, source="stored credential")

        authToken = await ctx.env(ANTHROPIC_AUTH_TOKEN_ENV)
        throwIfAborted(signal)
        if authToken:
            return AuthResult(
                auth=ModelAuth(headers={"Authorization": f"Bearer {authToken}"}),
                source=ANTHROPIC_AUTH_TOKEN_ENV,
            )

        for envVar in [ANTHROPIC_OAUTH_TOKEN_ENV, ANTHROPIC_API_KEY_ENV]:
            apiKey = await ctx.env(envVar)
            throwIfAborted(signal)
            if apiKey:
                return AuthResult(auth=ModelAuth(apiKey=apiKey), source=envVar)

        # Workload identity federation: the Anthropic SDK exchanges the identity
        # token for a short-lived access token and refreshes it itself. Last in
        # line so keys and ANTHROPIC_AUTH_TOKEN keep winning, as in the SDK. The
        # ids are provider config rather than auth, so they travel in `env`.
        federation: ProviderEnv = {}
        for envVar in [
            ANTHROPIC_FEDERATION_RULE_ID_ENV,
            ANTHROPIC_ORGANIZATION_ID_ENV,
            ANTHROPIC_IDENTITY_TOKEN_FILE_ENV,
        ]:
            value = await ctx.env(envVar)
            throwIfAborted(signal)
            if not value:
                return None
            federation[envVar] = value
        for envVar in [ANTHROPIC_SERVICE_ACCOUNT_ID_ENV, ANTHROPIC_WORKSPACE_ID_ENV]:
            value = await ctx.env(envVar)
            throwIfAborted(signal)
            if value:
                federation[envVar] = value
        return AuthResult(auth=ModelAuth(), env=federation, source="workload identity federation")

    return ApiKeyAuth(name="Anthropic API key", login=login, resolve=resolve)


def anthropicProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="anthropic",
            name="Anthropic",
            baseUrl="https://api.anthropic.com",
            auth=ProviderAuth(
                apiKey=_anthropicApiKeyAuth(),
                oauth=lazyOAuth({"name": "Anthropic (Claude Pro/Max)", "isSubscription": True, "load": loadAnthropicOAuth}),
            ),
            models=list(ANTHROPIC_MODELS.values()),
            api=anthropicMessagesApi(),
        )
    )


__all__ = ["anthropicProvider"]

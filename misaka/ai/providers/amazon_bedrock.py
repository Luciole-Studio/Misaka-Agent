"""Translated from pi's ``packages/ai/src/providers/amazon-bedrock.ts`` (v1.1.0).

MISAKA fork: the object literal's multi-statement ``login``/``resolve`` arrows are the module-level ``_login`` and
``_resolve``.
"""

from __future__ import annotations

from typing import Any

from misaka.ai.api.bedrock_converse_stream_lazy import bedrockConverseStreamApi
from misaka.ai.auth.types import (
    ApiKeyAuth,
    ApiKeyCredential,
    AuthContext,
    AuthInfoLink,
    AuthResult,
    InfoEvent,
    ModelAuth,
    ProviderAuth,
    ProviderAuthInteraction,
    SecretPrompt,
    SelectOption,
    SelectPrompt,
    TextPrompt,
)
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.amazon_bedrock_models import AMAZON_BEDROCK_MODELS
from misaka.utils.js import throwIfAborted


async def _login(interaction: ProviderAuthInteraction) -> ApiKeyCredential:
    throwIfAborted(interaction.signal)
    method = await interaction.prompt(
        SelectPrompt(
            type="select",
            message="Select Amazon Bedrock authentication method:",
            options=[
                SelectOption(id="bearer-token", label="Bearer token"),
                SelectOption(id="aws-profile", label="AWS profile"),
                SelectOption(id="credential-chain", label="Existing AWS credential chain"),
            ],
        )
    )
    throwIfAborted(interaction.signal)
    if method == "bearer-token":
        return ApiKeyCredential(
            type="api_key",
            key=await interaction.prompt(SecretPrompt(type="secret", message="Enter Amazon Bedrock bearer token")),
        )
    interaction.notify(
        InfoEvent(
            type="info",
            message="Amazon Bedrock supports AWS profiles, IAM credentials, and role-based credentials.",
            links=[
                AuthInfoLink(
                    label="AWS credential provider chain",
                    url="https://docs.aws.amazon.com/sdkref/latest/guide/standardized-credentials.html",
                ),
            ],
        )
    )
    if method == "aws-profile":
        return ApiKeyCredential(
            type="api_key",
            env={"AWS_PROFILE": await interaction.prompt(TextPrompt(type="text", message="Enter AWS profile name"))},
        )
    if method != "credential-chain":
        raise RuntimeError(f"Unknown Amazon Bedrock auth method: {method}")
    await interaction.prompt(
        TextPrompt(
            type="text",
            message="Configure AWS credentials, then press Enter to continue",
        )
    )
    return ApiKeyCredential(type="api_key")


async def _resolve(*, ctx: AuthContext, credential: ApiKeyCredential | None = None, signal: Any) -> AuthResult | None:
    async def env(name: str) -> str | None:
        throwIfAborted(signal)
        value = await ctx.env(name)
        throwIfAborted(signal)
        return value

    if credential is not None and credential.key:
        return AuthResult(auth=ModelAuth(apiKey=credential.key), env=credential.env, source="stored credential")
    if await env("AWS_BEARER_TOKEN_BEDROCK"):
        return AuthResult(auth=ModelAuth(), source="AWS_BEARER_TOKEN_BEDROCK")
    storedProfile = (credential.env or {}).get("AWS_PROFILE") if credential is not None else None
    if storedProfile if storedProfile is not None else await env("AWS_PROFILE"):
        return AuthResult(
            auth=ModelAuth(),
            env=credential.env if credential is not None else None,
            source="stored credential" if storedProfile else "AWS_PROFILE",
        )
    if (await env("AWS_ACCESS_KEY_ID")) and (await env("AWS_SECRET_ACCESS_KEY")):
        return AuthResult(auth=ModelAuth(), source="AWS access keys")
    if await env("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"):
        return AuthResult(auth=ModelAuth(), source="ECS task role")
    if await env("AWS_CONTAINER_CREDENTIALS_FULL_URI"):
        return AuthResult(auth=ModelAuth(), source="ECS task role")
    if await env("AWS_WEB_IDENTITY_TOKEN_FILE"):
        return AuthResult(auth=ModelAuth(), source="web identity token")
    return None


# Bedrock accepts a bearer token or the AWS SDK's default credential chain.
# The login flow can store a token/profile choice; resolve also detects ambient
# AWS credentials without copying them into pi's credential store.
_bedrockAuth = ApiKeyAuth(name="AWS credentials or bearer token", login=_login, resolve=_resolve)


def amazonBedrockProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="amazon-bedrock",
            name="Amazon Bedrock",
            auth=ProviderAuth(apiKey=_bedrockAuth),
            models=list(AMAZON_BEDROCK_MODELS.values()),
            api=bedrockConverseStreamApi(),
        )
    )


__all__ = ["amazonBedrockProvider"]

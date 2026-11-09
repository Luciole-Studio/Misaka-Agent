"""Translated from pi's ``packages/ai/src/providers/cloudflare-auth.ts`` (v1.1.0)."""

from __future__ import annotations

from typing import Any, Literal

from misaka.ai.auth.types import (
    ApiKeyAuth,
    ApiKeyCredential,
    AuthContext,
    AuthResult,
    ModelAuth,
    ProviderAuthInteraction,
    SecretPrompt,
    TextPrompt,
)
from misaka.ai.types import ProviderEnv
from misaka.utils.js import throwIfAborted

_CLOUDFLARE_API_KEY = "CLOUDFLARE_API_KEY"
_CLOUDFLARE_ACCOUNT_ID = "CLOUDFLARE_ACCOUNT_ID"
_CLOUDFLARE_GATEWAY_ID = "CLOUDFLARE_GATEWAY_ID"

type _CloudflareAuthKind = Literal["workers-ai", "ai-gateway"]


async def _resolveValue(
    name: str,
    ctx: AuthContext,
    credential: ApiKeyCredential | None,
    signal: Any,
) -> str | None:
    # Per-field merge: prefer the credential value, fall back to ambient env.
    # A credential carrying only the API key must still pick up the account /
    # gateway id from the environment.
    fromCredential = (
        (credential.key if name == _CLOUDFLARE_API_KEY else (credential.env or {}).get(name))
        if credential
        else None
    )
    if fromCredential is not None:
        return fromCredential
    throwIfAborted(signal)
    value = await ctx.env(name)
    throwIfAborted(signal)
    return value


async def _resolveCloudflareEnv(
    kind: _CloudflareAuthKind,
    ctx: AuthContext,
    credential: ApiKeyCredential | None,
    signal: Any,
) -> dict[str, Any] | None:
    apiKey = await _resolveValue(_CLOUDFLARE_API_KEY, ctx, credential, signal)
    accountId = await _resolveValue(_CLOUDFLARE_ACCOUNT_ID, ctx, credential, signal)
    gatewayId = (
        await _resolveValue(_CLOUDFLARE_GATEWAY_ID, ctx, credential, signal) if kind == "ai-gateway" else None
    )

    if not apiKey or not accountId or (kind == "ai-gateway" and not gatewayId):
        return None

    env: ProviderEnv = {
        "CLOUDFLARE_ACCOUNT_ID": accountId,
        **({"CLOUDFLARE_GATEWAY_ID": gatewayId} if gatewayId else {}),
    }
    return {
        "apiKey": apiKey,
        "env": env,
        "source": "stored credential" if credential else _CLOUDFLARE_API_KEY,
    }


def cloudflareWorkersAIAuth() -> ApiKeyAuth:
    async def login(interaction: ProviderAuthInteraction) -> ApiKeyCredential:
        key = await interaction.prompt(SecretPrompt(type="secret", message="Enter Cloudflare API key"))
        accountId = await interaction.prompt(TextPrompt(type="text", message="Enter Cloudflare account ID"))
        return ApiKeyCredential(type="api_key", key=key, env={"CLOUDFLARE_ACCOUNT_ID": accountId})

    async def resolve(*, ctx: AuthContext, credential: ApiKeyCredential | None = None, signal: Any) -> AuthResult | None:
        resolved = await _resolveCloudflareEnv("workers-ai", ctx, credential, signal)
        if not resolved:
            return None
        return AuthResult(
            auth=ModelAuth(apiKey=resolved["apiKey"]),
            env=resolved["env"],
            source=resolved["source"],
        )

    return ApiKeyAuth(name="Cloudflare API key", login=login, resolve=resolve)


def cloudflareAIGatewayAuth() -> ApiKeyAuth:
    async def login(interaction: ProviderAuthInteraction) -> ApiKeyCredential:
        key = await interaction.prompt(SecretPrompt(type="secret", message="Enter Cloudflare API key"))
        accountId = await interaction.prompt(TextPrompt(type="text", message="Enter Cloudflare account ID"))
        gatewayId = await interaction.prompt(TextPrompt(type="text", message="Enter Cloudflare AI Gateway ID"))
        return ApiKeyCredential(
            type="api_key",
            key=key,
            env={"CLOUDFLARE_ACCOUNT_ID": accountId, "CLOUDFLARE_GATEWAY_ID": gatewayId},
        )

    async def resolve(*, ctx: AuthContext, credential: ApiKeyCredential | None = None, signal: Any) -> AuthResult | None:
        resolved = await _resolveCloudflareEnv("ai-gateway", ctx, credential, signal)
        if not resolved:
            return None
        return AuthResult(
            auth=ModelAuth(
                headers={
                    "cf-aig-authorization": f"Bearer {resolved['apiKey']}",
                    "Authorization": None,
                    "x-api-key": None,
                },
            ),
            env=resolved["env"],
            source=resolved["source"],
        )

    return ApiKeyAuth(name="Cloudflare API key", login=login, resolve=resolve)


__all__ = ["cloudflareAIGatewayAuth", "cloudflareWorkersAIAuth"]

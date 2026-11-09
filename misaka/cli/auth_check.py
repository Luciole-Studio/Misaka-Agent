"""Translated from pi's ``packages/coding-agent/src/cli/auth-check.ts`` (v1.1.0)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from misaka.cli.args import Args
from misaka.cli.auth_command import (
    AuthCommandError,
    getAuthCredential,
    validateAuthCommandArgs,
)
from misaka.core.model_resolver import resolveCliModel
from misaka.core.model_runtime import ModelRuntime
from misaka.core.models_store import InMemoryCodingAgentModelsStore

type AuthCheckStatus = Literal["ready", "not_ready", "invalid"]
type AuthCheckReason = Literal["provider_not_found", "credentials_not_configured", "credential_not_available", "invalid_state"]


@dataclass(slots=True)
class AuthCheckResult:
    status: AuthCheckStatus
    provider: str
    reason: AuthCheckReason | None = None
    authType: Literal["api_key", "oauth"] | None = None


async def checkProviderAuth(
    args: Args,
    modelRuntime: ModelRuntime,
    options: dict[str, bool] | None = None,
) -> AuthCheckResult:
    options = options if options is not None else {"refresh": False}
    requested = validateAuthCommandArgs(args, "check")
    cliProvider, cliModel = requested["provider"], requested["model"]
    provider = cliProvider
    if cliModel:
        resolved = resolveCliModel({"cliProvider": cliProvider, "cliModel": cliModel, "modelRuntime": modelRuntime})
        if resolved.error or not resolved.model:
            raise AuthCommandError(resolved.error if resolved.error is not None else f'Unable to resolve model "{cliModel}"')
        provider = resolved.model.provider
    if not provider:
        raise AuthCommandError("Unable to resolve an auth provider")
    if modelRuntime.getError():
        return AuthCheckResult(status="invalid", provider=provider, reason="invalid_state")
    if not modelRuntime.getProvider(provider):
        return AuthCheckResult(status="not_ready", provider=provider, reason="provider_not_found")
    try:
        auth = await modelRuntime.checkAuth(provider)
        if not auth:
            return AuthCheckResult(status="not_ready", provider=provider, reason="credentials_not_configured")
        if options.get("refresh") and not await modelRuntime.getAuth(provider):
            return AuthCheckResult(status="not_ready", provider=provider, reason="credentials_not_configured")
        return AuthCheckResult(status="ready", provider=provider, authType=auth.type)
    except Exception:  # noqa: BLE001 - upstream's catch-all: any failure is an invalid state
        return AuthCheckResult(status="invalid", provider=provider, reason="invalid_state")


async def getProviderCredential(
    providerId: str,
    modelRuntime: ModelRuntime,
    credentials: Any,
    options: dict[str, bool],
) -> str | None:
    credential = await credentials.read(providerId)
    if not options.get("refresh") and credential is not None and credential.type == "oauth":
        return credential.access
    return getAuthCredential(await modelRuntime.getAuth(providerId))


async def createAuthCheckModelRuntime(credentials: Any) -> ModelRuntime:
    return await ModelRuntime.create({"credentials": credentials, "modelsStore": InMemoryCodingAgentModelsStore(), "allowModelNetwork": False, "refreshOnCreate": False})


__all__ = [
    "AuthCheckReason",
    "AuthCheckResult",
    "AuthCheckStatus",
    "checkProviderAuth",
    "createAuthCheckModelRuntime",
    "getProviderCredential",
]

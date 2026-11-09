"""Translated from pi's ``packages/coding-agent/src/cli/credential-print.ts`` (v1.3.0)."""

from __future__ import annotations

from typing import Any, Literal

from misaka.ai.auth.types import AuthOperationOptions
from misaka.cli.args import Args
from misaka.cli.auth_command import (
    AuthCommandError,
    getAuthCredential,
    validateAuthCommandArgs,
)
from misaka.core.model_resolver import (
    ModelReferenceRequest,
    resolveCliModel,
    resolveModelReference,
)
from misaka.core.model_runtime import ModelRuntime, ModelRuntimeAuthOverrides

DEFAULT_BEARER_TOKEN_MIN_EXPIRY_MS = 30 * 60_000

type CredentialPrintKind = Literal["api_key", "bearer_token"]


async def resolveCredentialForPrint(
    args: Args,
    modelRuntime: ModelRuntime,
    kind: CredentialPrintKind,
    minExpiryMs: int | None = None,
    signal: Any | None = None,
) -> str:
    """Resolve one configured provider credential.

    This intentionally calls ModelRuntime.getAuth(), which refreshes and persists
    OAuth credentials with less than five minutes remaining through the normal request-auth path.
    """
    requested = validateAuthCommandArgs(args, kind)
    cliProvider, cliModel = requested["provider"], requested["model"]
    credentialTypes = {
        credential.providerId: credential.type
        for credential in await modelRuntime.listCredentials(AuthOperationOptions(signal=signal))
    }
    providers: list[dict[str, Any]] = []
    if cliProvider:
        provider = modelRuntime.getProvider(cliProvider)
        if not provider:
            raise AuthCommandError(f'Unknown provider "{cliProvider}". Use --list-models to see available providers.')
        if cliModel:
            resolved = resolveCliModel({"cliProvider": provider.id, "cliModel": cliModel, "modelRuntime": modelRuntime})
            if resolved.error or not resolved.model:
                raise AuthCommandError(
                    resolved.error if resolved.error is not None else "Unable to resolve the requested provider/model"
                )
            providers.append({"id": provider.id, "model": resolved.model})
        else:
            providers.append({"id": provider.id})
    else:
        models = modelRuntime.getModels()
        for provider in modelRuntime.getProviders():
            if provider.id not in credentialTypes:
                continue
            reference = resolveModelReference(
                ModelReferenceRequest(text=cliModel, provider=provider.id, thinkingSuffix="strict", fuzzy=True),
                models,
                lambda _provider: True,
            )
            if reference.status == "resolved":
                providers.append({"id": provider.id, "model": reference.model})
        if len(providers) == 0:
            raise AuthCommandError(f'Model "{cliModel}" not found. Use --list-models to see available models.')

    credentials: list[dict[str, str]] = []
    for provider in providers:
        type = credentialTypes.get(provider["id"])
        if kind == "api_key" and type == "oauth":
            continue
        if kind == "bearer_token" and type != "oauth":
            continue
        authOptions = ModelRuntimeAuthOverrides(
            **(
                {"minOAuthValidityMs": minExpiryMs if minExpiryMs is not None else DEFAULT_BEARER_TOKEN_MIN_EXPIRY_MS}
                if kind == "bearer_token"
                else {}
            ),
            signal=signal,
        )
        auth = (
            await modelRuntime.getAuth(provider["model"], authOptions)
            if provider.get("model")
            else await modelRuntime.getAuth(provider["id"], authOptions)
        )
        value = getAuthCredential(auth)
        if value:
            credentials.append({"providerId": provider["id"], "value": value})

    if len(credentials) == 1:
        return credentials[0]["value"]
    if len(credentials) == 0:
        providerId = providers[0]["id"] if providers else None
        type = credentialTypes.get(providerId) if providerId else None
        if cliProvider and kind == "api_key" and type == "oauth":
            raise AuthCommandError(f'Provider "{providerId}" is configured with OAuth, not an API key')
        if cliProvider and kind == "bearer_token" and type != "oauth":
            raise AuthCommandError(f'Provider "{providerId}" is not configured with an OAuth bearer token')
        raise AuthCommandError(f"No usable {'API key' if kind == 'api_key' else 'OAuth bearer token'} is configured")
    raise AuthCommandError(
        f"Multiple configured providers matched ({', '.join(credential['providerId'] for credential in credentials)}). Specify --provider."
    )


__all__ = ["resolveCredentialForPrint"]

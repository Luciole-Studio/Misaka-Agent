"""Translated from pi's ``packages/ai/src/providers/google-vertex.ts`` (v1.1.0).

MISAKA fork: the object literal's multi-statement ``login``/``resolve`` arrows are the module-level ``_login`` and
``_resolve``.
"""

from __future__ import annotations

from typing import Any

from misaka.ai.api.google_vertex_lazy import googleVertexApi
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
from misaka.ai.providers.google_vertex_models import GOOGLE_VERTEX_MODELS
from misaka.utils.js import nullishCoalesce, throwIfAborted

_VERTEX_ADC_PATH = "~/.config/gcloud/application_default_credentials.json"


async def _login(interaction: ProviderAuthInteraction) -> ApiKeyCredential:
    throwIfAborted(interaction.signal)
    method = await interaction.prompt(
        SelectPrompt(
            type="select",
            message="Select Google Vertex AI authentication method:",
            options=[
                SelectOption(id="api-key", label="Google Cloud API key"),
                SelectOption(id="adc", label="Application Default Credentials"),
                SelectOption(id="service-account", label="Service account credentials file"),
            ],
        )
    )
    throwIfAborted(interaction.signal)
    if method == "api-key":
        return ApiKeyCredential(
            type="api_key",
            key=await interaction.prompt(SecretPrompt(type="secret", message="Enter Google Cloud API key")),
        )
    if method != "adc" and method != "service-account":
        raise RuntimeError(f"Unknown Google Vertex AI auth method: {method}")
    interaction.notify(
        InfoEvent(
            type="info",
            message=(
                "Run `gcloud auth application-default login`, then provide the project and location."
                if method == "adc"
                else "Provide a service account credentials file, project, and location."
            ),
            links=[
                AuthInfoLink(
                    label="Application Default Credentials",
                    url="https://cloud.google.com/docs/authentication/provide-credentials-adc",
                ),
            ],
        )
    )
    project = await interaction.prompt(TextPrompt(type="text", message="Enter Google Cloud project ID"))
    location = await interaction.prompt(TextPrompt(type="text", message="Enter Google Cloud location"))
    credentialsPath = (
        await interaction.prompt(TextPrompt(type="text", message="Enter service account credentials file path"))
        if method == "service-account"
        else None
    )
    return ApiKeyCredential(
        type="api_key",
        env={
            "GOOGLE_CLOUD_PROJECT": project,
            "GOOGLE_CLOUD_LOCATION": location,
            **({"GOOGLE_APPLICATION_CREDENTIALS": credentialsPath} if credentialsPath else {}),
        },
    )


async def _resolve(*, ctx: AuthContext, credential: ApiKeyCredential | None = None, signal: Any) -> AuthResult | None:
    async def env(name: str) -> str | None:
        throwIfAborted(signal)
        value = await ctx.env(name)
        throwIfAborted(signal)
        return value

    credentialEnv = (credential.env or {}) if credential is not None else {}
    storedKey = credential.key if credential is not None else None
    key = storedKey if storedKey is not None else await env("GOOGLE_CLOUD_API_KEY")
    if key:
        return AuthResult(auth=ModelAuth(apiKey=key), source="stored credential" if storedKey else "GOOGLE_CLOUD_API_KEY")

    storedAdcPath = credentialEnv.get("GOOGLE_APPLICATION_CREDENTIALS")
    adcPath = storedAdcPath if storedAdcPath is not None else await env("GOOGLE_APPLICATION_CREDENTIALS")
    throwIfAborted(signal)
    hasCredentials = await ctx.fileExists(nullishCoalesce(adcPath, _VERTEX_ADC_PATH))
    throwIfAborted(signal)
    project = credentialEnv.get("GOOGLE_CLOUD_PROJECT")
    if project is None:
        project = await env("GOOGLE_CLOUD_PROJECT")
    if project is None:
        project = await env("GCLOUD_PROJECT")
    location = credentialEnv.get("GOOGLE_CLOUD_LOCATION")
    if location is None:
        location = await env("GOOGLE_CLOUD_LOCATION")
    if hasCredentials and project and location:
        return AuthResult(
            auth=ModelAuth(),
            env=credential.env if credential is not None else None,
            source="stored credential" if credential else "gcloud application default credentials",
        )
    return None


# Vertex accepts an explicit API key or Application Default Credentials
# (`gcloud auth application-default login`). ADC additionally requires
# project and location env vars, which the implementation reads itself.
_vertexAuth = ApiKeyAuth(name="Google Cloud credentials", login=_login, resolve=_resolve)


def googleVertexProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="google-vertex",
            name="Google Vertex AI",
            auth=ProviderAuth(apiKey=_vertexAuth),
            models=list(GOOGLE_VERTEX_MODELS.values()),
            api=googleVertexApi(),
        )
    )


__all__ = ["googleVertexProvider"]

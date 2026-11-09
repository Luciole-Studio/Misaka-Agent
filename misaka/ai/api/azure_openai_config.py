"""Translated from pi's ``packages/ai/src/api/azure-openai-config.ts`` (v1.1.0).

MISAKA fork: ``urllib.parse`` stands in for the WHATWG ``URL``. A URL it cannot take apart (no scheme or host, a port
that is not a number in range) is the ``new URL`` failure; the URL is put back together from its own parts rather than
re-serialized.
"""

from __future__ import annotations

import re
from typing import Any, TypedDict
from urllib.parse import urlparse, urlunparse

from misaka.ai.api._common import _option
from misaka.ai.types import CacheRetention, Model, ProviderEnv, ProviderHeaders
from misaka.ai.utils.provider_env import getProviderEnvValue
from misaka.utils.js import jsTrim

_DEFAULT_AZURE_API_VERSION = "v1"


class AzureEndpointOptions(TypedDict, total=False):
    """Azure models ship without a baseUrl: one resource per user, resolved per request."""

    # StreamOptions members.
    temperature: float
    samplingParams: dict[str, Any]
    maxTokens: int
    signal: Any
    telemetryContext: Any
    apiKey: str
    transport: Any
    cacheRetention: CacheRetention
    sessionId: str
    headers: ProviderHeaders
    onPayload: Any
    onResponse: Any
    onProviderStreamEvent: Any
    timeoutMs: int
    websocketConnectTimeoutMs: int
    maxRetries: int
    maxRetryDelayMs: int
    metadata: dict[str, Any]
    env: ProviderEnv
    azureApiVersion: str
    azureResourceName: str
    azureBaseUrl: str
    azureDeploymentName: str


def _parseDeploymentNameMap(value: str | None) -> dict[str, str]:
    map: dict[str, str] = {}
    if not value:
        return map
    for entry in value.split(","):
        trimmed = jsTrim(entry)
        if not trimmed:
            continue
        # JS `split("=", 2)`: whatever follows a second "=" is dropped.
        modelId, deploymentName = (trimmed.split("=") + [""])[:2]
        if not modelId or not deploymentName:
            continue
        map[jsTrim(modelId)] = jsTrim(deploymentName)
    return map


def resolveDeploymentName(model: Model, options: AzureEndpointOptions | Any | None = None) -> str:
    if _option(options, "azureDeploymentName"):
        return _option(options, "azureDeploymentName")
    mappedDeployment = _parseDeploymentNameMap(
        getProviderEnvValue("AZURE_OPENAI_DEPLOYMENT_NAME_MAP", _option(options, "env")),
    ).get(model.id)
    return mappedDeployment or model.id


def _normalizeAzureBaseUrl(baseUrl: str) -> str:
    trimmed = re.sub(r"/+$", "", jsTrim(baseUrl))
    url = urlparse(trimmed)
    try:
        hostname = url.hostname
        _ = url.port
    except ValueError as error:
        raise RuntimeError(f"Invalid Azure OpenAI base URL: {baseUrl}") from error
    if not url.scheme or not url.netloc or hostname is None:
        raise RuntimeError(f"Invalid Azure OpenAI base URL: {baseUrl}")

    isAzureHost = (
        hostname.endswith(".openai.azure.com")  # noqa: PIE810 - upstream spells out the three suffixes
        or hostname.endswith(".cognitiveservices.azure.com")
        or hostname.endswith(".ai.azure.com")
    )
    normalizedPath = re.sub(r"/+$", "", url.path)

    # Ensure Azure hosts have /openai/v1 as base path so the AzureOpenAI SDK
    # can append /deployments/<model>/... and ?api-version=v1 correctly.
    if isAzureHost and (
        normalizedPath == "" or normalizedPath == "/" or normalizedPath == "/openai" or normalizedPath == "/openai/v1/responses"
    ):
        url = url._replace(path="/openai/v1", query="")

    return re.sub(r"/+$", "", urlunparse(url))


def _buildDefaultBaseUrl(resourceName: str) -> str:
    return f"https://{resourceName}.openai.azure.com/openai/v1"


def resolveAzureBaseUrl(model: Model, options: AzureEndpointOptions | Any | None = None) -> str:
    baseUrl = (
        jsTrim(_option(options, "azureBaseUrl") or "")
        or jsTrim(getProviderEnvValue("AZURE_OPENAI_BASE_URL", _option(options, "env")) or "")
        or None
    )
    resourceName = _option(options, "azureResourceName") or getProviderEnvValue(
        "AZURE_OPENAI_RESOURCE_NAME", _option(options, "env")
    )

    resolvedBaseUrl = baseUrl

    if not resolvedBaseUrl and resourceName:
        resolvedBaseUrl = _buildDefaultBaseUrl(resourceName)

    if not resolvedBaseUrl and model.baseUrl:
        resolvedBaseUrl = model.baseUrl

    if not resolvedBaseUrl:
        raise RuntimeError(
            "Azure OpenAI base URL is required. Set AZURE_OPENAI_BASE_URL or AZURE_OPENAI_RESOURCE_NAME, or pass azureBaseUrl, azureResourceName, or model.baseUrl.",
        )

    return _normalizeAzureBaseUrl(resolvedBaseUrl)


def resolveAzureConfig(model: Model, options: AzureEndpointOptions | Any | None = None) -> dict[str, str]:
    return {
        "baseUrl": resolveAzureBaseUrl(model, options),
        "apiVersion": (
            _option(options, "azureApiVersion")
            or getProviderEnvValue("AZURE_OPENAI_API_VERSION", _option(options, "env"))
            or _DEFAULT_AZURE_API_VERSION
        ),
    }


__all__ = ["AzureEndpointOptions", "resolveAzureBaseUrl", "resolveAzureConfig", "resolveDeploymentName"]

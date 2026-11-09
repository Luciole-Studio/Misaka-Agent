"""Translated from pi's ``packages/ai/src/auth/oauth/github-copilot.ts`` (v1.1.0).

GitHub Copilot OAuth flow

MISAKA fork (each is marked where it happens): ``fetch`` is ``_fetch.fetch``; ``Number.parseFloat`` and ``Date.parse``
of a ``retry-after`` value are ``provider_retry._parse_float`` and ``email.utils``; the catalog's GitHub Copilot ids
are read from the generated catalog; and the login takes the ``options`` every login is called with.
"""

from __future__ import annotations

import base64
import math
import re
import time
from email.utils import parsedate_to_datetime
from typing import Any, TypedDict

from misaka.ai.auth.oauth._fetch import RequestInit, Response, fetch
from misaka.ai.auth.oauth.device_code import pollOAuthDeviceCodeFlow
from misaka.ai.auth.types import (
    DeviceCodeEvent,
    LoginOptions,
    ModelAuth,
    OAuthAuth,
    OAuthCredential,
    ProgressEvent,
    ProviderAuthInteraction,
    TextPrompt,
)
from misaka.ai.utils.provider_retry import _parse_float
from misaka.ai.utils.sleep import sleep
from misaka.utils.js import AbortSignal, isNumber, jsonStringify, jsTrim
from misaka.utils.values import signal_aborted
from misaka.utils.whatwg_url import URL, URLSearchParams


def _decode(s: str) -> str:
    return base64.b64decode(s).decode("latin-1")


_CLIENT_ID = _decode("SXYxLmI1MDdhMDhjODdlY2ZlOTg=")

_COPILOT_HEADERS = {
    "User-Agent": "GitHubCopilotChat/0.35.0",
    "Editor-Version": "vscode/1.107.0",
    "Editor-Plugin-Version": "copilot-chat/0.35.0",
    "Copilot-Integration-Id": "vscode-chat",
}
_COPILOT_API_VERSION = "2026-06-01"


class _DeviceCodeResponse(TypedDict):
    device_code: str
    user_code: str
    verification_uri: str
    interval: float | None
    expires_in: float


def _normalizeDomain(input: str) -> str | None:
    trimmed = jsTrim(input)
    if not trimmed:
        return None
    try:
        url = URL(trimmed) if "://" in trimmed else URL(f"https://{trimmed}")
        return url.hostname
    except Exception:  # noqa: BLE001 - `catch { return null; }`
        return None


def _getUrls(domain: str) -> dict[str, str]:
    return {
        "deviceCodeUrl": f"https://{domain}/login/device/code",
        "accessTokenUrl": f"https://{domain}/login/oauth/access_token",
        "copilotTokenUrl": f"https://api.{domain}/copilot_internal/v2/token",
    }


def _getBaseUrlFromToken(token: str) -> str | None:
    """Parse the proxy-ep from a Copilot token and convert to API base URL.
    Token format: tid=...;exp=...;proxy-ep=proxy.individual.githubcopilot.com;...
    Returns API URL like https://api.individual.githubcopilot.com
    """
    match = re.search(r"proxy-ep=([^;]+)", token)
    if not match:
        return None
    proxyHost = match[1]
    # Convert proxy.xxx to api.xxx
    apiHost = re.sub(r"^proxy\.", "api.", proxyHost)
    return f"https://{apiHost}"


def _getGitHubCopilotBaseUrl(token: str | None = None, enterpriseDomain: str | None = None) -> str:
    # If we have a token, extract the base URL from proxy-ep
    if token:
        urlFromToken = _getBaseUrlFromToken(token)
        if urlFromToken:
            return urlFromToken
    # Fallback for enterprise or if token parsing fails
    if enterpriseDomain:
        return f"https://copilot-api.{enterpriseDomain}"
    return "https://api.individual.githubcopilot.com"


def _asRecord(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _githubCopilotCatalogIds() -> dict[str, Any]:
    # MISAKA fork: `GITHUB_COPILOT_MODELS` is the generated catalog's github-copilot table.
    from misaka.ai.models_generated import MODELS

    return MODELS.get("github-copilot", {})


def _parseGitHubCopilotModelCatalog(raw: Any, allowPolicyFallback: bool) -> dict[str, list[str]]:
    record = _asRecord(raw)
    data = record.get("data") if record is not None else None
    if not isinstance(data, list):
        raise RuntimeError("Invalid Copilot models response")  # noqa: TRY004 - upstream throws Error

    accountModels: list[dict[str, Any]] = []
    for rawItem in data:
        item = _asRecord(rawItem)
        id = item.get("id") if item is not None else None
        if not item or not isinstance(id, str):
            continue

        capabilities = _asRecord(item.get("capabilities"))
        supports = _asRecord(capabilities.get("supports")) if capabilities is not None else None
        if supports is not None and supports.get("tool_calls") is False:
            continue

        policy = _asRecord(item.get("policy"))
        accountModels.append(
            {
                "id": id,
                "pickerEnabled": item.get("model_picker_enabled") is True,
                "policyState": policy.get("state") if policy is not None else None,
            }
        )
    pickerModelIds = [
        model["id"] for model in accountModels if model["pickerEnabled"] and model["policyState"] != "disabled"
    ]
    usePolicyFallback = allowPolicyFallback and len(pickerModelIds) == 0
    availableModelIds = (
        pickerModelIds
        if len(pickerModelIds) > 0 or not allowPolicyFallback
        else [model["id"] for model in accountModels if model["policyState"] == "enabled"]
    )
    catalog = _githubCopilotCatalogIds()
    policyModelIds = [
        model["id"]
        for model in accountModels
        if model["policyState"] == "unconfigured"
        and model["id"] in catalog
        and (model["pickerEnabled"] or usePolicyFallback)
    ]
    return {"availableModelIds": availableModelIds, "policyModelIds": policyModelIds}


def _dateParse(value: str) -> float:
    """MISAKA fork: `Date.parse(value)`: milliseconds since the epoch, `NaN` when the date does not parse."""
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return math.nan
    return when.timestamp() * 1000 if when is not None else math.nan


async def _fetchWithRateLimitRetry(url: str, init: RequestInit, signal: Any, retryPolicy: dict[str, int]) -> Response:
    retryBudgetSignal = (
        AbortSignal.timeout(retryPolicy["maxElapsedMs"])
        if retryPolicy["maxRetries"] > 0 and retryPolicy["maxElapsedMs"] > 0
        else None
    )
    requestSignal = AbortSignal.any([signal, retryBudgetSignal]) if retryBudgetSignal else signal
    retryDeadline = time.time() * 1000 + retryPolicy["maxElapsedMs"] if retryBudgetSignal else None
    retry = 0
    while True:
        response = await fetch(
            url,
            {
                **init,
                "signal": AbortSignal.any([requestSignal, AbortSignal.timeout(5000)]),
            },
        )
        if response.status != 429 or retry == retryPolicy["maxRetries"]:
            return response

        retryAfter = response.headers.get("retry-after")
        delayMs = 500 * 2**retry
        if retryAfter:
            seconds = _parse_float(retryAfter)
            delayMs = _dateParse(retryAfter) - time.time() * 1000 if math.isnan(seconds) else seconds * 1000
            if not math.isfinite(delayMs):
                return response
        delayMs = max(0, delayMs)
        if retryDeadline is not None and delayMs >= retryDeadline - time.time() * 1000:
            return response
        if response.body is not None:
            await response.body.cancel()
        await sleep(delayMs, requestSignal)
        retry += 1


async def _fetchGitHubCopilotModels(
    copilotToken: str,
    enterpriseDomain: str | None,
    signal: Any,
    retryPolicy: dict[str, int],
) -> dict[str, list[str]]:
    baseUrl = _getGitHubCopilotBaseUrl(copilotToken, enterpriseDomain)
    # Some Individual accounts return false for every picker flag despite explicit enabled policies.
    # Limit the fallback to that endpoint so other account types keep strict picker semantics.
    allowPolicyFallback = baseUrl == "https://api.individual.githubcopilot.com"
    response = await _fetchWithRateLimitRetry(
        f"{baseUrl}/models",
        {
            "headers": {
                "Accept": "application/json",
                "Authorization": f"Bearer {copilotToken}",
                **_COPILOT_HEADERS,
                "X-GitHub-Api-Version": _COPILOT_API_VERSION,
            },
        },
        signal,
        retryPolicy,
    )
    if not response.ok:
        raise RuntimeError(f"{response.status} {response.statusText}: {await response.text()}")
    return _parseGitHubCopilotModelCatalog(await response.json(), allowPolicyFallback)


async def _fetchJson(url: str, init: RequestInit) -> Any:
    response = await fetch(url, init)
    if not response.ok:
        text = await response.text()
        raise RuntimeError(f"{response.status} {response.statusText}: {text}")
    return await response.json()


async def _startDeviceFlow(domain: str, signal: Any) -> _DeviceCodeResponse:
    urls = _getUrls(domain)
    data = await _fetchJson(
        urls["deviceCodeUrl"],
        {
            "method": "POST",
            "headers": {
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "GitHubCopilotChat/0.35.0",
            },
            "body": URLSearchParams(
                {
                    "client_id": _CLIENT_ID,
                    "scope": "read:user",
                }
            ),
            "signal": signal,
        },
    )

    if not data or not isinstance(data, dict):
        raise RuntimeError("Invalid device code response")

    deviceCode = data.get("device_code")
    userCode = data.get("user_code")
    verificationUri = data.get("verification_uri")
    interval = data.get("interval")
    expiresIn = data.get("expires_in")

    if (
        not isinstance(deviceCode, str)
        or not isinstance(userCode, str)
        or not isinstance(verificationUri, str)
        or ("interval" in data and not isNumber(interval))
        or not isNumber(expiresIn)
    ):
        raise RuntimeError("Invalid device code response fields")

    # The verification URI is opened in the user's browser and to prevent `open` from
    # opening an executable or similar, we force it to be a URL.
    try:
        parsedUri = URL(verificationUri)
    except Exception as error:
        raise RuntimeError("Untrusted verification_uri in device code response") from error
    if parsedUri.protocol != "https:" and parsedUri.protocol != "http:":
        raise RuntimeError("Untrusted verification_uri in device code response")

    return {
        "device_code": deviceCode,
        "user_code": userCode,
        "verification_uri": parsedUri.href,
        "interval": interval,
        "expires_in": expiresIn,
    }


async def _pollForGitHubAccessToken(domain: str, device: _DeviceCodeResponse, signal: Any) -> str:
    urls = _getUrls(domain)

    async def poll() -> Any:
        raw = await _fetchJson(
            urls["accessTokenUrl"],
            {
                "method": "POST",
                "headers": {
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "GitHubCopilotChat/0.35.0",
                },
                "body": URLSearchParams(
                    {
                        "client_id": _CLIENT_ID,
                        "device_code": device["device_code"],
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    }
                ),
                "signal": signal,
            },
        )

        if isinstance(raw, dict) and isinstance(raw.get("access_token"), str):
            return {"status": "complete", "value": raw["access_token"]}

        if isinstance(raw, dict) and isinstance(raw.get("error"), str):
            error, description, interval = raw["error"], raw.get("error_description"), raw.get("interval")
            if error == "authorization_pending":
                return {"status": "pending"}

            if error == "slow_down":
                return {"status": "slow_down", "intervalSeconds": interval if isNumber(interval) else None}

            descriptionSuffix = f": {description}" if description else ""
            return {"status": "failed", "message": f"Device flow failed: {error}{descriptionSuffix}"}

        return {"status": "failed", "message": "Invalid device token response"}

    return await pollOAuthDeviceCodeFlow(
        {
            "intervalSeconds": device["interval"],
            "expiresInSeconds": device["expires_in"],
            "waitBeforeFirstPoll": True,
            "signal": signal,
            "poll": poll,
        }
    )


async def _refreshGitHubCopilotAccessToken(
    refreshToken: str,
    enterpriseDomain: str | None,
    signal: Any,
) -> OAuthCredential:
    domain = enterpriseDomain or "github.com"
    urls = _getUrls(domain)

    raw = await _fetchJson(
        urls["copilotTokenUrl"],
        {
            "headers": {
                "Accept": "application/json",
                "Authorization": f"Bearer {refreshToken}",
                **_COPILOT_HEADERS,
            },
            "signal": signal,
        },
    )

    if not raw or not isinstance(raw, dict):
        raise RuntimeError("Invalid Copilot token response")

    token = raw.get("token")
    expiresAt = raw.get("expires_at")

    if not isinstance(token, str) or not isNumber(expiresAt):
        raise RuntimeError("Invalid Copilot token response fields")

    return OAuthCredential(
        type="oauth",
        refresh=refreshToken,
        access=token,
        expires=expiresAt * 1000 - 5 * 60 * 1000,
        **({"enterpriseUrl": enterpriseDomain} if enterpriseDomain is not None else {}),
    )


async def _refreshGitHubCopilotToken(refreshToken: str, enterpriseDomain: str | None, signal: Any) -> OAuthCredential:
    """Refresh GitHub Copilot token"""
    credentials = await _refreshGitHubCopilotAccessToken(refreshToken, enterpriseDomain, signal)
    availableModelIds = (
        await _fetchGitHubCopilotModels(
            credentials.access,
            enterpriseDomain,
            signal,
            {
                "maxRetries": 0,
                "maxElapsedMs": 0,
            },
        )
    )["availableModelIds"]
    return credentials.model_copy(update={"availableModelIds": availableModelIds})


async def _enableGitHubCopilotModel(token: str, modelId: str, enterpriseDomain: str | None, signal: Any) -> bool:
    """Enable a model for the user's GitHub Copilot account.
    This is required for some models (like Claude, Grok) before they can be used.
    """
    baseUrl = _getGitHubCopilotBaseUrl(token, enterpriseDomain)
    url = f"{baseUrl}/models/{modelId}/policy"

    try:
        response = await _fetchWithRateLimitRetry(
            url,
            {
                "method": "POST",
                "headers": {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                    **_COPILOT_HEADERS,
                    "openai-intent": "chat-policy",
                    "x-interaction-type": "chat-policy",
                },
                "body": jsonStringify({"state": "enabled"}),
            },
            signal,
            {"maxRetries": 2, "maxElapsedMs": 5000},
        )
    except Exception:
        if signal_aborted(signal):
            raise
        return False
    if response.status == 429:
        raise RuntimeError(f"{response.status} {response.statusText}: {await response.text()}")
    return response.ok


async def _enableGitHubCopilotModels(
    token: str,
    modelIds: list[str],
    enterpriseDomain: str | None,
    signal: Any,
) -> list[str]:
    """Enable the requested GitHub Copilot models and return the successful IDs.
    Policy updates are best effort; exhausted rate limiting stops the batch.
    """
    enabledModelIds: list[str] = []
    for modelId in modelIds:
        try:
            if await _enableGitHubCopilotModel(token, modelId, enterpriseDomain, signal):
                enabledModelIds.append(modelId)
        except Exception:
            if signal_aborted(signal):
                raise
            break
    return enabledModelIds


async def _loginGitHubCopilot(interaction: ProviderAuthInteraction, options: LoginOptions | None = None) -> OAuthCredential:
    # MISAKA fork: `options` is accepted because every login is called with it.
    input = await interaction.prompt(
        TextPrompt(
            type="text",
            message="GitHub Enterprise URL/domain (blank for github.com)",
            placeholder="company.ghe.com",
        )
    )
    if signal_aborted(interaction.signal):
        raise RuntimeError("Login cancelled")

    trimmed = jsTrim(input)
    enterpriseDomain = _normalizeDomain(input)
    if trimmed and not enterpriseDomain:
        raise RuntimeError("Invalid GitHub Enterprise URL/domain")
    domain = enterpriseDomain or "github.com"

    device = await _startDeviceFlow(domain, interaction.signal)
    interaction.notify(
        DeviceCodeEvent(
            type="device_code",
            userCode=device["user_code"],
            verificationUri=device["verification_uri"],
            intervalSeconds=device["interval"],
            expiresInSeconds=device["expires_in"],
        )
    )

    githubAccessToken = await _pollForGitHubAccessToken(domain, device, interaction.signal)
    credentials = await _refreshGitHubCopilotAccessToken(
        githubAccessToken,
        enterpriseDomain,
        interaction.signal,
    )
    models = await _fetchGitHubCopilotModels(
        credentials.access,
        enterpriseDomain,
        interaction.signal,
        {
            "maxRetries": 2,
            "maxElapsedMs": 5000,
        },
    )
    enabledModelIds: list[str] = []
    if len(models["policyModelIds"]) > 0:
        interaction.notify(ProgressEvent(type="progress", message="Enabling models..."))
        enabledModelIds = await _enableGitHubCopilotModels(
            credentials.access,
            models["policyModelIds"],
            enterpriseDomain,
            interaction.signal,
        )
    return credentials.model_copy(
        update={"availableModelIds": list(dict.fromkeys([*models["availableModelIds"], *enabledModelIds]))}
    )


def _copilotEnterpriseDomain(credential: OAuthCredential) -> str | None:
    enterpriseUrl = getattr(credential, "enterpriseUrl", None)
    if not isinstance(enterpriseUrl, str) or not enterpriseUrl:
        return None
    return _normalizeDomain(enterpriseUrl)


async def _githubCopilotOAuth_toAuth(credential: OAuthCredential) -> ModelAuth:
    """Derive the credential-specific proxy endpoint for each request."""
    return ModelAuth(
        apiKey=credential.access,
        baseUrl=_getGitHubCopilotBaseUrl(credential.access, _copilotEnterpriseDomain(credential)),
    )


githubCopilotOAuth = OAuthAuth(
    name="GitHub Copilot",
    isSubscription=True,
    login=_loginGitHubCopilot,
    refresh=lambda credential, signal: _refreshGitHubCopilotToken(
        credential.refresh, _copilotEnterpriseDomain(credential), signal
    ),
    toAuth=_githubCopilotOAuth_toAuth,
)

__all__ = ["githubCopilotOAuth"]

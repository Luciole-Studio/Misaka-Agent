"""Translated from pi's ``packages/ai/src/auth/oauth/kimi-coding.ts`` (v1.1.0).

Kimi Code (subscription) OAuth flow

RFC 8628 device authorization grant against https://auth.kimi.com with JSON
responses. The access token authenticates requests to
https://api.kimi.com/coding as an `Authorization: Bearer` header.

MISAKA fork (each is marked where it happens): ``fetch`` is ``_fetch.fetch``, a JSON ``true`` is not a number, and the
login takes the ``options`` every login is called with.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from typing import Any

from misaka.ai.auth.oauth._fetch import Response, fetch
from misaka.ai.auth.oauth.device_code import pollOAuthDeviceCodeFlow
from misaka.ai.auth.types import (
    DeviceCodeEvent,
    LoginOptions,
    ModelAuth,
    OAuthAuth,
    OAuthCredential,
    ProviderAuthInteraction,
)
from misaka.ai.utils.provider_env import getProviderEnvValue
from misaka.ai.utils.sleep import sleep
from misaka.utils.js import AbortSignal, isNumber, jsonStringify
from misaka.utils.values import signal_aborted
from misaka.utils.whatwg_url import URL, URLSearchParams

_CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
_DEFAULT_OAUTH_HOST = "https://auth.kimi.com"
_DEVICE_CODE_TIMEOUT_SECONDS = 15 * 60
_DEFAULT_POLL_INTERVAL_SECONDS = 5
_REQUEST_TIMEOUT_MS = 30 * 1000
_REFRESH_MAX_RETRIES = 3


@dataclass(slots=True)
class _DeviceAuthorization:
    deviceCode: str
    userCode: str
    verificationUri: str
    verificationUriComplete: str
    intervalSeconds: float
    expiresInSeconds: float


@dataclass(slots=True)
class _TokenResponse:
    access: str
    refresh: str
    expires: float


def _getOauthHost() -> str:
    override = getProviderEnvValue("KIMI_CODE_OAUTH_HOST") or getProviderEnvValue("KIMI_OAUTH_HOST")
    # MISAKA fork: `\Z`, as Python's `$` also matches before a trailing newline.
    return re.sub(r"/+\Z", "", override or _DEFAULT_OAUTH_HOST)


def _requestSignal(signal: Any) -> Any:
    return AbortSignal.any([AbortSignal.timeout(_REQUEST_TIMEOUT_MS), signal])


def _formUrlEncode(fields: dict[str, str]) -> str:
    return URLSearchParams(fields).toString()


async def _readJson(response: Response) -> dict[str, Any] | None:
    try:
        json = await response.json()
        # MISAKA fork: `typeof json === "object"` holds for an array too; only a dict has the fields read below.
        return json if isinstance(json, dict) else None
    except Exception:  # noqa: BLE001 - `catch { return null; }`
        return None


def _trustedHttpUrl(value: Any) -> str | None:
    """The verification URI is opened in the user's browser; only http(s) URLs are trusted."""
    if not isinstance(value, str) or not value:
        return None
    try:
        url = URL(value)
        if url.protocol != "https:" and url.protocol != "http:":
            return None
        return url.href
    except Exception:  # noqa: BLE001 - `catch { return null; }`
        return None


async def _responseText(response: Response) -> str:
    """`response.text().catch(() => "")`."""
    try:
        return await response.text()
    except Exception:  # noqa: BLE001 - `.catch(() => "")`
        return ""


async def _startDeviceAuthorization(oauthHost: str, signal: Any) -> _DeviceAuthorization:
    response = await fetch(
        f"{oauthHost}/api/oauth/device_authorization",
        {
            "method": "POST",
            "headers": {
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            "body": _formUrlEncode({"client_id": _CLIENT_ID}),
            "signal": _requestSignal(signal),
        },
    )

    if not response.ok:
        text = await _responseText(response)
        raise RuntimeError(
            f"Kimi Code device authorization failed with status {response.status}{f': {text}' if text else ''}"
        )

    json = await _readJson(response)
    field = (lambda name: json.get(name)) if json is not None else (lambda name: None)
    deviceCode = field("device_code")
    userCode = field("user_code")
    verificationUri = field("verification_uri")
    verificationUriComplete = field("verification_uri_complete")
    if (
        not isinstance(deviceCode, str)
        or not isinstance(userCode, str)
        or not isinstance(verificationUri, str)
        or not isinstance(verificationUriComplete, str)
        or not _trustedHttpUrl(verificationUriComplete)
        or not _trustedHttpUrl(verificationUri)
    ):
        raise RuntimeError(f"Invalid Kimi Code device authorization response: {jsonStringify(json)}")

    interval = field("interval")
    expiresIn = field("expires_in")
    return _DeviceAuthorization(
        deviceCode=deviceCode,
        userCode=userCode,
        verificationUri=verificationUri,
        verificationUriComplete=verificationUriComplete,
        intervalSeconds=interval
        if isNumber(interval) and math.isfinite(interval) and interval > 0
        else _DEFAULT_POLL_INTERVAL_SECONDS,
        expiresInSeconds=expiresIn
        if isNumber(expiresIn) and math.isfinite(expiresIn) and expiresIn > 0
        else _DEVICE_CODE_TIMEOUT_SECONDS,
    )


def _parseTokenResponse(json: dict[str, Any] | None, operation: str) -> _TokenResponse:
    accessToken = json.get("access_token") if json is not None else None
    refreshToken = json.get("refresh_token") if json is not None else None
    expiresIn = json.get("expires_in") if json is not None else None
    if (
        not isinstance(accessToken, str)
        or not accessToken
        or not isinstance(refreshToken, str)
        or not refreshToken
        or not isNumber(expiresIn)
        or not math.isfinite(expiresIn)
        or expiresIn <= 0
    ):
        raise RuntimeError(f"Kimi Code token {operation} response missing fields: {jsonStringify(json)}")
    return _TokenResponse(
        access=accessToken,
        refresh=refreshToken,
        expires=time.time() * 1000 + expiresIn * 1000,
    )


async def _pollForToken(oauthHost: str, device: _DeviceAuthorization, signal: Any) -> _TokenResponse:
    async def poll() -> Any:
        response = await fetch(
            f"{oauthHost}/api/oauth/token",
            {
                "method": "POST",
                "headers": {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                "body": _formUrlEncode(
                    {
                        "client_id": _CLIENT_ID,
                        "device_code": device.deviceCode,
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    }
                ),
                "signal": _requestSignal(signal),
            },
        )

        if response.status >= 500:
            text = await _responseText(response)
            return {
                "status": "failed",
                "message": f"Kimi Code device token request failed with status {response.status}{f': {text}' if text else ''}",
            }

        json = await _readJson(response)
        if response.ok and json is not None and isinstance(json.get("access_token"), str):
            try:
                return {"status": "complete", "value": _parseTokenResponse(json, "poll")}
            except Exception as error:  # noqa: BLE001 - the failure is the poll result
                return {"status": "failed", "message": str(error)}

        error = json.get("error") if json is not None else None
        description = (
            f": {json['error_description']}"
            if json is not None and isinstance(json.get("error_description"), str)
            else ""
        )
        if error == "authorization_pending":
            return {"status": "pending"}
        if error == "slow_down":
            interval = json.get("interval") if json is not None else None
            return {
                "status": "slow_down",
                "intervalSeconds": interval if isNumber(interval) and interval > 0 else None,
            }
        if error == "expired_token":
            return {"status": "failed", "message": "Kimi Code device authorization expired. Please restart login."}
        if error == "access_denied":
            return {"status": "failed", "message": "Kimi Code login was denied."}
        return {
            "status": "failed",
            "message": f"Kimi Code device token request failed (status {response.status})"
            f"{f': {error}{description}' if isinstance(error, str) else ''}",
        }

    return await pollOAuthDeviceCodeFlow(
        {
            "intervalSeconds": device.intervalSeconds,
            "expiresInSeconds": device.expiresInSeconds,
            "waitBeforeFirstPoll": True,
            "signal": signal,
            "poll": poll,
        }
    )


def _isRetryableRefreshFailure(response: Response) -> bool:
    return response.status == 429 or response.status >= 500


async def _refreshToken(oauthHost: str, refreshTokenValue: str, signal: Any) -> _TokenResponse:
    lastError: Exception | None = None
    for attempt in range(_REFRESH_MAX_RETRIES + 1):
        if attempt > 0:
            await sleep(1000 * 2 ** (attempt - 1), signal)
        if signal_aborted(signal):
            raise RuntimeError("Kimi Code token refresh aborted")

        try:
            response = await fetch(
                f"{oauthHost}/api/oauth/token",
                {
                    "method": "POST",
                    "headers": {
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Accept": "application/json",
                    },
                    "body": _formUrlEncode(
                        {
                            "client_id": _CLIENT_ID,
                            "grant_type": "refresh_token",
                            "refresh_token": refreshTokenValue,
                        }
                    ),
                    "signal": _requestSignal(signal),
                },
            )
        except Exception as error:  # noqa: BLE001 - kept as the last error and retried
            lastError = error
            continue

        json = await _readJson(response)
        if response.ok:
            return _parseTokenResponse(json, "refresh")

        # Unauthorized: the stored credential is dead; Models clears it and prompts re-login.
        if response.status == 401 or response.status == 403 or (json is not None and json.get("error") == "invalid_grant"):
            description = (
                f": {json['error_description']}"
                if json is not None and isinstance(json.get("error_description"), str)
                else ""
            )
            raise RuntimeError(f"Kimi Code token refresh unauthorized (status {response.status}){description}")

        if _isRetryableRefreshFailure(response) and attempt < _REFRESH_MAX_RETRIES:
            lastError = RuntimeError(f"Kimi Code token refresh failed with status {response.status}")
            continue

        text = jsonStringify(json)
        raise RuntimeError(f"Kimi Code token refresh failed with status {response.status}{f': {text}' if text else ''}")

    raise lastError if lastError is not None else RuntimeError("Kimi Code token refresh failed")


async def _loginKimiCoding(interaction: ProviderAuthInteraction, options: LoginOptions | None = None) -> OAuthCredential:
    # MISAKA fork: `options` is accepted because every login is called with it.
    oauthHost = _getOauthHost()
    device = await _startDeviceAuthorization(oauthHost, interaction.signal)
    interaction.notify(
        DeviceCodeEvent(
            type="device_code",
            userCode=device.userCode,
            verificationUri=device.verificationUriComplete,
            intervalSeconds=device.intervalSeconds,
            expiresInSeconds=device.expiresInSeconds,
        )
    )
    token = await _pollForToken(oauthHost, device, interaction.signal)
    return OAuthCredential(type="oauth", access=token.access, refresh=token.refresh, expires=token.expires)


async def _kimiCodingOAuth_refresh(credential: OAuthCredential, signal: Any) -> OAuthCredential:
    token = await _refreshToken(_getOauthHost(), credential.refresh, signal)
    return OAuthCredential(type="oauth", access=token.access, refresh=token.refresh, expires=token.expires)


async def _kimiCodingOAuth_toAuth(credential: OAuthCredential) -> ModelAuth:
    return ModelAuth(headers={"Authorization": f"Bearer {credential.access}"})


kimiCodingOAuth = OAuthAuth(
    name="Kimi Code (subscription)",
    isSubscription=True,
    loginLabel="Sign in with Kimi Code",
    login=_loginKimiCoding,
    refresh=_kimiCodingOAuth_refresh,
    toAuth=_kimiCodingOAuth_toAuth,
)

__all__ = ["kimiCodingOAuth"]

"""Translated from pi's ``packages/ai/src/auth/oauth/meta.ts`` (v1.1.0).

Meta Model API OAuth flow

RFC 8628 device authorization grant against https://auth.meta.com (JSON
responses). Meta splits identity from API access: the resulting identity
token is not accepted for inference, so it is exchanged for a Model API
key via the Muse Code key-mint endpoint (minted keys live about a day).
The identity token is stored as `refresh` and the minted key as `access`,
so the standard OAuth scheduler re-mints the key when it expires with no
bespoke renewal machinery. The identity token itself is not renewable
(auth.meta.com answers grant_type=refresh_token with 404 and issues no
refresh_token), so a 401/403 from mint means the session is dead and the
user must sign in again.

MISAKA fork (each is marked where it happens): ``fetch`` is ``_fetch.fetch``, a JSON ``true`` is not a number, and the
login takes the ``options`` every login is called with.
"""

from __future__ import annotations

import math
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
    ProgressEvent,
    ProviderAuthInteraction,
)
from misaka.utils.js import AbortSignal, jsonStringify, jsTrim
from misaka.utils.values import signal_aborted
from misaka.utils.whatwg_url import URL, URLSearchParams

# Muse Code CLI client id.
_CLIENT_ID = "1031625952748946"
_AUTH_HOST = "https://auth.meta.com"
_DEVICE_AUTHORIZATION_URL = f"{_AUTH_HOST}/oidc/device/authorization/"
_DEVICE_TOKEN_URL = f"{_AUTH_HOST}/oidc/device/token/"
_API_KEY_MINT_URL = "https://api.meta.ai/muse-code/key"
_API_KEY_LIFETIME_MS = 24 * 60 * 60 * 1000
_REQUEST_TIMEOUT_MS = 30 * 1000


@dataclass(slots=True)
class _DeviceAuthorization:
    deviceCode: str
    userCode: str
    verificationUri: str
    intervalSeconds: float | None = None
    expiresInSeconds: float | None = None


def _requestSignal(signal: Any) -> Any:
    return AbortSignal.any([AbortSignal.timeout(_REQUEST_TIMEOUT_MS), signal])


async def _readJson(response: Response) -> dict[str, Any] | None:
    try:
        json = await response.json()
        # MISAKA fork: `typeof json === "object"` holds for an array too; only a dict has the fields read below.
        return json if isinstance(json, dict) else None
    except Exception:  # noqa: BLE001 - `catch { return null; }`
        return None


def _errorDetail(json: dict[str, Any] | None) -> str:
    for key in ["error_description", "detail", "message", "error"]:
        value = json.get(key) if json is not None else None
        if isinstance(value, str) and jsTrim(value):
            return f": {jsTrim(value)}"
    return ""


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


def _positiveNumber(value: Any) -> float | None:
    # MISAKA fork: a JSON `true` is not a number.
    if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
        return value
    return None


async def _startDeviceAuthorization(signal: Any) -> _DeviceAuthorization:
    response = await fetch(
        _DEVICE_AUTHORIZATION_URL,
        {
            "method": "POST",
            "headers": {
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            "body": URLSearchParams({"client_id": _CLIENT_ID}).toString(),
            "signal": _requestSignal(signal),
        },
    )
    json = await _readJson(response)
    if not response.ok:
        raise RuntimeError(f"Meta device authorization failed with status {response.status}{_errorDetail(json)}")
    deviceCode = json.get("device_code") if json is not None else None
    userCode = json.get("user_code") if json is not None else None
    verificationUri = _trustedHttpUrl(json.get("verification_uri_complete") if json is not None else None) or (
        _trustedHttpUrl(json.get("verification_uri") if json is not None else None)
    )
    if not isinstance(deviceCode, str) or not deviceCode or not isinstance(userCode, str) or not userCode or not verificationUri:
        raise RuntimeError(f"Invalid Meta device authorization response: {jsonStringify(json)}")
    return _DeviceAuthorization(
        deviceCode=deviceCode,
        userCode=userCode,
        verificationUri=verificationUri,
        intervalSeconds=_positiveNumber(json.get("interval")),
        expiresInSeconds=_positiveNumber(json.get("expires_in")),
    )


async def _pollForIdentityToken(device: _DeviceAuthorization, signal: Any) -> str:
    async def poll() -> Any:
        response = await fetch(
            _DEVICE_TOKEN_URL,
            {
                "method": "POST",
                "headers": {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                "body": URLSearchParams(
                    {
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        "device_code": device.deviceCode,
                        "client_id": _CLIENT_ID,
                    }
                ).toString(),
                "signal": _requestSignal(signal),
            },
        )
        json = await _readJson(response)
        accessToken = json.get("access_token") if json is not None else None
        if response.ok and isinstance(accessToken, str) and accessToken:
            return {"status": "complete", "value": accessToken}
        match json.get("error") if json is not None else None:
            case "authorization_pending":
                return {"status": "pending"}
            case "slow_down":
                return {"status": "slow_down", "intervalSeconds": _positiveNumber(json.get("interval"))}
            case "access_denied":
                return {"status": "failed", "message": "Meta login was denied."}
            case "expired_token":
                return {"status": "failed", "message": "Meta device authorization expired. Please restart login."}
            case _:
                return {
                    "status": "failed",
                    "message": f"Meta device token request failed with status {response.status}{_errorDetail(json)}",
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


async def _mintApiKey(identityToken: str, signal: Any) -> OAuthCredential:
    """Exchange an identity token for a Model API key. Keys are valid for about a day."""
    response = await fetch(
        _API_KEY_MINT_URL,
        {
            "method": "POST",
            "headers": {
                "Accept": "application/json",
                "Authorization": f"Bearer {identityToken}",
                "Content-Type": "application/json",
                "x-api-version": "1.0.0",
            },
            "body": "{}",
            "signal": _requestSignal(signal),
        },
    )
    json = await _readJson(response)
    if response.status == 401 or response.status == 403:
        # Identity token is not renewable (see file header); only a fresh device flow helps.
        raise RuntimeError(
            f"Meta session expired (status {response.status}). Run `/login meta` to sign in again.{_errorDetail(json)}"
        )
    if not response.ok:
        raise RuntimeError(f"Meta API key mint failed with status {response.status}{_errorDetail(json)}")
    apiKey = json.get("api_key") if json is not None else None
    if not isinstance(apiKey, str) or not apiKey:
        actionUrl = _trustedHttpUrl(json.get("action_url") if json is not None else None)
        raise RuntimeError(f"Meta did not issue an API key.{f' Complete setup at {actionUrl}' if actionUrl else ''}")
    return OAuthCredential(
        type="oauth", refresh=identityToken, access=apiKey, expires=time.time() * 1000 + _API_KEY_LIFETIME_MS
    )


async def _loginMeta(interaction: ProviderAuthInteraction, options: LoginOptions | None = None) -> OAuthCredential:
    # MISAKA fork: `options` is accepted because every login is called with it.
    try:
        device = await _startDeviceAuthorization(interaction.signal)
        interaction.notify(
            DeviceCodeEvent(
                type="device_code",
                userCode=device.userCode,
                verificationUri=device.verificationUri,
                intervalSeconds=device.intervalSeconds,
                expiresInSeconds=device.expiresInSeconds,
            )
        )
        identityToken = await _pollForIdentityToken(device, interaction.signal)
        interaction.notify(ProgressEvent(type="progress", message="Enabling Meta Model API access..."))
        return await _mintApiKey(identityToken, interaction.signal)
    except Exception as e:
        # An in-flight fetch rejects with a DOMException on abort; the login UI matches on this message.
        if signal_aborted(interaction.signal):
            raise RuntimeError("Login cancelled") from e
        raise


async def _metaOAuth_toAuth(credential: OAuthCredential) -> ModelAuth:
    return ModelAuth(apiKey=credential.access)


metaOAuth = OAuthAuth(
    name="Meta (Muse subscription)",
    isSubscription=True,
    loginLabel="Sign in with Meta",
    login=_loginMeta,
    refresh=lambda credential, signal: _mintApiKey(credential.refresh, signal),
    toAuth=_metaOAuth_toAuth,
)

__all__ = ["metaOAuth"]

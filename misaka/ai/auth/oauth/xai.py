"""Translated from pi's ``packages/ai/src/auth/oauth/xai.ts`` (v1.1.0).

xAI OAuth device-code flow.

MISAKA fork (each is marked where it happens): ``fetch`` is ``_fetch.fetch``, a JSON ``true`` is not a number, and the
login takes the ``options`` every login is called with.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

from misaka.ai.auth.oauth._fetch import fetch
from misaka.ai.auth.oauth.device_code import pollOAuthDeviceCodeFlow
from misaka.ai.auth.types import (
    DeviceCodeEvent,
    LoginOptions,
    ModelAuth,
    OAuthAuth,
    OAuthCredential,
    ProviderAuthInteraction,
)
from misaka.utils.js import isNumber
from misaka.utils.values import signal_aborted
from misaka.utils.whatwg_url import URL, URLSearchParams

_XAI_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
_XAI_SCOPE = "openid profile email offline_access grok-cli:access api:access"
_XAI_DEVICE_CODE_URL = "https://auth.x.ai/oauth2/device/code"
_XAI_TOKEN_URL = "https://auth.x.ai/oauth2/token"
# Refresh slightly before the reported expiry to avoid using a token that dies mid-request.
_REFRESH_SKEW_MS = 5 * 60 * 1000
_DEFAULT_TOKEN_LIFETIME_SECONDS = 3600

_JsonObject = dict[str, Any]


@dataclass(slots=True)
class _OAuthHttpResponse:
    ok: bool
    status: int
    body: _JsonObject


@dataclass(slots=True)
class _XaiDeviceCode:
    deviceCode: str
    userCode: str
    verificationUri: str
    verificationUriComplete: str | None
    intervalSeconds: float | None
    expiresInSeconds: float


def _requiredString(body: _JsonObject, field: str) -> str:
    value = body.get(field)
    if not isinstance(value, str) or len(value) == 0:
        raise RuntimeError(f"Invalid xAI OAuth response field: {field}")
    return value


def _positiveNumber(body: _JsonObject, field: str) -> float:
    value = body.get(field)
    if not isNumber(value) or not math.isfinite(value) or value <= 0:
        raise RuntimeError(f"Invalid xAI OAuth response field: {field}")
    return value


# The verification URI is opened in the user's browser; force it to be an https URL
# so a malicious response cannot make `open` launch something else.
def _validateVerificationUri(raw: str) -> str:
    try:
        url = URL(raw)
    except Exception as error:
        raise RuntimeError("Untrusted verification URI in xAI OAuth response") from error
    if url.protocol != "https:":
        raise RuntimeError("Untrusted verification URI in xAI OAuth response")
    return url.href


async def _postForm(url: str, fields: dict[str, str], signal: Any) -> _OAuthHttpResponse:
    try:
        response = await fetch(
            url,
            {
                "method": "POST",
                "headers": {
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                "body": URLSearchParams(fields),
                "signal": signal,
            },
        )
    except Exception as error:
        if signal_aborted(signal):
            raise RuntimeError("Login cancelled") from error
        raise

    try:
        parsed = await response.json()
        body = parsed if isinstance(parsed, dict) else {}
    except Exception as error:
        if signal_aborted(signal):
            raise RuntimeError("Login cancelled") from error
        raise RuntimeError(f"xAI OAuth returned invalid JSON (HTTP {response.status})") from error
    return _OAuthHttpResponse(
        ok=response.ok,
        status=response.status,
        body=body,
    )


def _requestFailure(action: str, response: _OAuthHttpResponse) -> Exception:
    error = response.body.get("error") if isinstance(response.body.get("error"), str) else None
    description = (
        response.body.get("error_description") if isinstance(response.body.get("error_description"), str) else None
    )
    detail = ": ".join(part for part in (error, description) if part)
    return RuntimeError(f"xAI OAuth {action} failed (HTTP {response.status}){f': {detail}' if detail else ''}")


def _parseDeviceCode(body: _JsonObject) -> _XaiDeviceCode:
    # RFC 8628 allows interval 0 (no minimum wait); fall back to the poller's
    # default instead of failing on non-positive or malformed values.
    interval = body.get("interval")
    intervalSeconds = interval if isNumber(interval) and math.isfinite(interval) and interval > 0 else None
    verificationUriComplete = (
        _validateVerificationUri(body["verification_uri_complete"])
        if isinstance(body.get("verification_uri_complete"), str) and len(body["verification_uri_complete"]) > 0
        else None
    )
    return _XaiDeviceCode(
        deviceCode=_requiredString(body, "device_code"),
        userCode=_requiredString(body, "user_code"),
        verificationUri=_validateVerificationUri(_requiredString(body, "verification_uri")),
        verificationUriComplete=verificationUriComplete,
        intervalSeconds=intervalSeconds,
        expiresInSeconds=_positiveNumber(body, "expires_in"),
    )


def _credentialsFromTokenResponse(body: _JsonObject, previousRefreshToken: str | None = None) -> OAuthCredential:
    access = _requiredString(body, "access_token")
    # xAI may omit refresh_token on refresh when the token is not rotated.
    refresh = (
        previousRefreshToken
        if "refresh_token" not in body and previousRefreshToken
        else _requiredString(body, "refresh_token")
    )
    expiresInSeconds = (
        _DEFAULT_TOKEN_LIFETIME_SECONDS if "expires_in" not in body else _positiveNumber(body, "expires_in")
    )
    return OAuthCredential(
        type="oauth",
        access=access,
        refresh=refresh,
        expires=time.time() * 1000 + expiresInSeconds * 1000 - _REFRESH_SKEW_MS,
    )


async def _requestDeviceCode(signal: Any) -> _XaiDeviceCode:
    response = await _postForm(
        _XAI_DEVICE_CODE_URL,
        {
            "client_id": _XAI_CLIENT_ID,
            "scope": _XAI_SCOPE,
            "referrer": "pi",
        },
        signal,
    )
    if not response.ok:
        raise _requestFailure("device authorization", response)
    return _parseDeviceCode(response.body)


async def _pollForTokens(device: _XaiDeviceCode, signal: Any) -> OAuthCredential:
    async def poll() -> Any:
        response = await _postForm(
            _XAI_TOKEN_URL,
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": _XAI_CLIENT_ID,
                "device_code": device.deviceCode,
            },
            signal,
        )

        if response.ok:
            return {"status": "complete", "value": _credentialsFromTokenResponse(response.body)}

        error = response.body.get("error")
        if error == "authorization_pending":
            return {"status": "pending"}
        if error == "slow_down":
            interval = response.body.get("interval")
            return {"status": "slow_down", "intervalSeconds": interval if isNumber(interval) else None}
        if error == "access_denied" or error == "authorization_denied":
            return {"status": "failed", "message": "xAI device authorization was denied"}
        if error == "expired_token":
            return {"status": "failed", "message": "xAI device code expired"}
        return {"status": "failed", "message": str(_requestFailure("device token polling", response))}

    return await pollOAuthDeviceCodeFlow(
        {
            "intervalSeconds": device.intervalSeconds,
            "expiresInSeconds": device.expiresInSeconds,
            "waitBeforeFirstPoll": True,
            "signal": signal,
            "poll": poll,
        }
    )


async def _loginXai(interaction: ProviderAuthInteraction, options: LoginOptions | None = None) -> OAuthCredential:
    # MISAKA fork: `options` is accepted because every login is called with it.
    device = await _requestDeviceCode(interaction.signal)
    interaction.notify(
        DeviceCodeEvent(
            type="device_code",
            userCode=device.userCode,
            verificationUri=device.verificationUriComplete
            if device.verificationUriComplete is not None
            else device.verificationUri,
            intervalSeconds=device.intervalSeconds,
            expiresInSeconds=device.expiresInSeconds,
        )
    )
    return await _pollForTokens(device, interaction.signal)


async def _refreshXaiToken(refreshToken: str, signal: Any) -> OAuthCredential:
    response = await _postForm(
        _XAI_TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "client_id": _XAI_CLIENT_ID,
            "refresh_token": refreshToken,
        },
        signal,
    )
    if not response.ok:
        raise _requestFailure("token refresh", response)
    return _credentialsFromTokenResponse(response.body, refreshToken)


async def _xaiOAuth_toAuth(credential: OAuthCredential) -> ModelAuth:
    return ModelAuth(apiKey=credential.access)


xaiOAuth = OAuthAuth(
    name="xAI (Grok/X subscription)",
    isSubscription=True,
    loginLabel="Sign in with SuperGrok or X Premium",
    login=_loginXai,
    refresh=lambda credential, signal: _refreshXaiToken(credential.refresh, signal),
    toAuth=_xaiOAuth_toAuth,
)

__all__ = ["xaiOAuth"]

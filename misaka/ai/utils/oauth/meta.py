"""Meta Model API OAuth flow, translated from pi's ``auth/oauth/meta.ts``.

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

The HTTP call is injected rather than imported, as in ``kimi_coding.py``, so the tests
can drive the flow without a network.
"""

from __future__ import annotations

import json as json_module
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from misaka.ai.utils.abort import race_with_abort_signal
from misaka.ai.utils.oauth.device_code import poll_oauth_device_code_flow
from misaka.ai.utils.oauth.types import (
    OAuthCredentials,
    OAuthDeviceCodeInfo,
    OAuthLoginCallbacks,
)
from misaka.utils.values import signal_aborted

# Muse Code CLI client id.
CLIENT_ID = "1031625952748946"
AUTH_HOST = "https://auth.meta.com"
DEVICE_AUTHORIZATION_URL = f"{AUTH_HOST}/oidc/device/authorization/"
DEVICE_TOKEN_URL = f"{AUTH_HOST}/oidc/device/token/"
API_KEY_MINT_URL = "https://api.meta.ai/muse-code/key"
API_KEY_LIFETIME_MS = 24 * 60 * 60 * 1000
REQUEST_TIMEOUT_MS = 30 * 1000


@dataclass(slots=True)
class MetaHttpResponse:
    """The slice of ``Response`` this flow reads: a status and an already-read body."""

    status: int
    body: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


Post = Callable[[str, dict[str, str], str, Any], Awaitable[MetaHttpResponse]]


def _new_http_client() -> httpx.AsyncClient:
    # Upstream's 30s `AbortSignal.timeout` is this client's timeout.
    return httpx.AsyncClient(timeout=REQUEST_TIMEOUT_MS / 1000)


async def _default_post(url: str, headers: dict[str, str], body: str, signal: Any) -> MetaHttpResponse:
    async def request() -> MetaHttpResponse:
        async with _new_http_client() as client:
            response = await client.post(url, headers=headers, content=body.encode())
        return MetaHttpResponse(status=response.status_code, body=response.text)

    return await race_with_abort_signal(request(), signal)


def _read_json(response: MetaHttpResponse) -> Any:
    try:
        parsed = json_module.loads(response.body)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _error_detail(payload: Any) -> str:
    for key in ("error_description", "detail", "message", "error"):
        value = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(value, str) and value.strip():
            return f": {value.strip()}"
    return ""


def _trusted_http_url(value: Any) -> str | None:
    """The verification URI is opened in the user's browser; only http(s) URLs are trusted.

    See ``kimi_coding._trusted_http_url`` for why ``urlsplit`` stands in for ``new URL``.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        return None
    return value


def _positive_number(value: Any) -> float | None:
    return (
        value
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0
        else None
    )


@dataclass(slots=True)
class _DeviceAuthorization:
    deviceCode: str
    userCode: str
    verificationUri: str
    intervalSeconds: float | None
    expiresInSeconds: float | None


_FORM_HEADERS = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}


async def _start_device_authorization(signal: Any, post: Post) -> _DeviceAuthorization:
    response = await post(DEVICE_AUTHORIZATION_URL, _FORM_HEADERS, urlencode({"client_id": CLIENT_ID}), signal)
    payload = _read_json(response)
    if not response.ok:
        raise RuntimeError(f"Meta device authorization failed with status {response.status}{_error_detail(payload)}")
    device_code = payload.get("device_code") if payload else None
    user_code = payload.get("user_code") if payload else None
    verification_uri = (
        _trusted_http_url(payload.get("verification_uri_complete")) or _trusted_http_url(payload.get("verification_uri"))
        if payload
        else None
    )
    if not isinstance(device_code, str) or not device_code or not isinstance(user_code, str) or not user_code or not verification_uri:
        raise RuntimeError(f"Invalid Meta device authorization response: {json_module.dumps(payload)}")
    return _DeviceAuthorization(
        deviceCode=device_code,
        userCode=user_code,
        verificationUri=verification_uri,
        intervalSeconds=_positive_number(payload.get("interval")),
        expiresInSeconds=_positive_number(payload.get("expires_in")),
    )


async def _poll_for_identity_token(device: _DeviceAuthorization, signal: Any, post: Post) -> str:
    async def poll() -> Any:
        response = await post(
            DEVICE_TOKEN_URL,
            _FORM_HEADERS,
            urlencode({
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device.deviceCode,
                "client_id": CLIENT_ID,
            }),
            signal,
        )
        payload = _read_json(response)
        access_token = payload.get("access_token") if payload else None
        if response.ok and isinstance(access_token, str) and access_token:
            return {"status": "complete", "value": access_token}
        error = payload.get("error") if payload else None
        if error == "authorization_pending":
            return {"status": "pending"}
        if error == "slow_down":
            return {"status": "slow_down", "intervalSeconds": _positive_number(payload.get("interval"))}
        if error == "access_denied":
            return {"status": "failed", "message": "Meta login was denied."}
        if error == "expired_token":
            return {"status": "failed", "message": "Meta device authorization expired. Please restart login."}
        return {
            "status": "failed",
            "message": f"Meta device token request failed with status {response.status}{_error_detail(payload)}",
        }

    return await poll_oauth_device_code_flow(
        intervalSeconds=device.intervalSeconds,
        expiresInSeconds=device.expiresInSeconds,
        waitBeforeFirstPoll=True,
        signal=signal,
        poll=poll,
    )


async def mint_api_key(identity_token: str, signal: Any, post: Post | None = None) -> OAuthCredentials:
    """Exchange an identity token for a Model API key. Keys are valid for about a day."""
    send = post or _default_post
    response = await send(
        API_KEY_MINT_URL,
        {
            "Accept": "application/json",
            "Authorization": f"Bearer {identity_token}",
            "Content-Type": "application/json",
            "x-api-version": "1.0.0",
        },
        "{}",
        signal,
    )
    payload = _read_json(response)
    if response.status in (401, 403):
        # Identity token is not renewable (see module docstring); only a fresh device flow helps.
        raise RuntimeError(
            f"Meta session expired (status {response.status}). Run `/login meta` to sign in again."
            f"{_error_detail(payload)}"
        )
    if not response.ok:
        raise RuntimeError(f"Meta API key mint failed with status {response.status}{_error_detail(payload)}")
    api_key = payload.get("api_key") if payload else None
    if not isinstance(api_key, str) or not api_key:
        action_url = _trusted_http_url(payload.get("action_url")) if payload else None
        raise RuntimeError(f"Meta did not issue an API key.{f' Complete setup at {action_url}' if action_url else ''}")
    return OAuthCredentials(refresh=identity_token, access=api_key, expires=int(time.time() * 1000) + API_KEY_LIFETIME_MS)


async def login_meta(callbacks: OAuthLoginCallbacks, *, post: Post | None = None) -> OAuthCredentials:
    """Run the device-code login, then mint the Model API key the credentials carry."""
    send = post or _default_post
    signal = getattr(callbacks, "signal", None)
    try:
        device = await _start_device_authorization(signal, send)
        callbacks.onDeviceCode(
            OAuthDeviceCodeInfo(
                userCode=device.userCode,
                verificationUri=device.verificationUri,
                intervalSeconds=device.intervalSeconds,
                expiresInSeconds=device.expiresInSeconds,
            )
        )
        identity_token = await _poll_for_identity_token(device, signal, send)
        on_progress = getattr(callbacks, "onProgress", None)
        if on_progress is not None:
            on_progress("Enabling Meta Model API access...")
        return await mint_api_key(identity_token, signal, send)
    except Exception:
        # An in-flight fetch rejects with a DOMException on abort; the login UI matches on this message.
        if signal_aborted(signal):
            raise RuntimeError("Login cancelled") from None
        raise


class _MetaOAuthProvider:
    id = "meta"
    name = "Meta (Muse subscription)"
    # Device-code: the user authorises out of band, so nothing listens on localhost.
    usesCallbackServer = False
    isSubscription = True
    loginLabel = "Sign in with Meta"

    def __init__(self, *, post: Post | None = None) -> None:
        self._post = post

    async def login(self, callbacks: OAuthLoginCallbacks) -> OAuthCredentials:
        return await login_meta(callbacks, post=self._post)

    async def refreshToken(self, credentials: OAuthCredentials, signal: Any | None = None) -> OAuthCredentials:
        return await mint_api_key(credentials.refresh, signal, self._post)

    def getApiKey(self, credentials: OAuthCredentials) -> str:
        # Upstream's `toAuth` returns `{apiKey: credential.access}`: the minted key is an
        # ordinary API key.
        return credentials.access


meta_oauth_provider = _MetaOAuthProvider()

loginMeta = login_meta
mintMetaApiKey = mint_api_key
metaOAuthProvider = meta_oauth_provider

__all__ = [
    "MetaHttpResponse",
    "loginMeta",
    "login_meta",
    "metaOAuthProvider",
    "meta_oauth_provider",
    "mintMetaApiKey",
    "mint_api_key",
]

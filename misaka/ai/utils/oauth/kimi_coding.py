"""Kimi Code (subscription) OAuth flow, translated from pi's ``auth/oauth/kimi-coding.ts``.

RFC 8628 device authorization grant against https://auth.kimi.com with JSON responses.
The access token authenticates requests to https://api.kimi.com/coding as an
``Authorization: Bearer`` header.

Two things upstream gets from the platform have to be built here instead:

* ``AbortSignal.any([AbortSignal.timeout(30s), signal])`` is a composed signal in the
  browser/Bun runtime. misaka's signals are duck-typed objects with no timeout variant,
  so the 30s budget becomes the HTTP client's own timeout and the caller's signal is
  raced against the request by ``race_with_abort_signal``. What the caller sees is the
  same -- whichever fires first is what it hears about -- but the race only stops the
  *waiting*; it does not cancel the request, and the client's own timeout is what closes
  the abandoned socket.
* ``JSON.stringify`` has no separators; ``json.dumps`` inserts spaces unless told not to,
  so ``_stringify`` below passes ``separators=(",", ":")``.

The HTTP call and the retry backoff are injected rather than imported, so the tests can
drive the whole flow without a socket or a real delay.
"""

from __future__ import annotations

import json as json_module
import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from misaka.ai.utils.abort import race_with_abort_signal, sleep
from misaka.ai.utils.oauth.device_code import poll_oauth_device_code_flow
from misaka.ai.utils.oauth.types import (
    OAuthCredentials,
    OAuthDeviceCodeInfo,
    OAuthLoginCallbacks,
)
from misaka.ai.utils.provider_env import get_provider_env_value
from misaka.utils.values import signal_aborted

CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
DEFAULT_OAUTH_HOST = "https://auth.kimi.com"
DEVICE_CODE_TIMEOUT_SECONDS = 15 * 60
DEFAULT_POLL_INTERVAL_SECONDS = 5
REQUEST_TIMEOUT_MS = 30 * 1000
REFRESH_MAX_RETRIES = 3

# Identical on all three endpoints upstream, so they are a property of the transport
# rather than of any one call; the injection point below carries only what varies.
FORM_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json",
}


@dataclass(slots=True)
class KimiHttpResponse:
    """The slice of ``Response`` this flow reads: a status and an already-read body.

    Upstream picks one accessor per branch and never reads a body twice: ``.text()`` on
    the failure branches (``kimi-coding.ts:82,168``), ``readJson``'s ``.json()`` on the
    rest. Each ``.text()`` there carries a ``.catch(() => "")``, guarding a read that can
    still fail. Here the bytes are already in hand as ``body``, and both ``json()`` and
    the messages that quote the body read that same string, so there is nothing left for
    such a guard to catch.
    """

    status: int
    body: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        return json_module.loads(self.body)


PostForm = Callable[[str, dict[str, str], Any], Awaitable[KimiHttpResponse]]
SleepMs = Callable[[float, Any], Awaitable[None]]


def _new_http_client() -> httpx.AsyncClient:
    """The client the production path uses; a seam so the path itself can be tested.

    Upstream's 30s ``AbortSignal.timeout`` is this client's timeout (see the module
    docstring). Kept as its own function so a test can replace it without touching the
    ``httpx`` module itself.
    """
    return httpx.AsyncClient(timeout=REQUEST_TIMEOUT_MS / 1000)


async def _default_post_form(url: str, fields: dict[str, str], signal: Any) -> KimiHttpResponse:
    async def request() -> KimiHttpResponse:
        async with _new_http_client() as client:
            response = await client.post(url, headers=FORM_HEADERS, content=urlencode(fields).encode())
        return KimiHttpResponse(status=response.status_code, body=response.text)

    return await race_with_abort_signal(request(), signal)


def get_kimi_coding_oauth_host(env: dict[str, str] | None = None) -> str:
    """The auth host, with the two documented overrides taking precedence in order."""
    override = get_provider_env_value("KIMI_CODE_OAUTH_HOST", env) or get_provider_env_value("KIMI_OAUTH_HOST", env)
    # `\Z`, not `$`: upstream's `/\/+$/` (no `m` flag) anchors at the very end of the
    # string, while Python's `$` also matches just before a trailing newline -- which
    # would silently eat the slash out of an env value that ended in "/\n" and leave the
    # newline in the host.
    return re.sub(r"/+\Z", "", override or DEFAULT_OAUTH_HOST)


def _stringify(value: Any) -> str:
    """``JSON.stringify``: no spaces, and no ``\\uXXXX`` escaping of non-ASCII."""
    return json_module.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _read_json(response: KimiHttpResponse) -> Any:
    """``json && typeof json === "object"`` -- which in JS admits arrays, so this does too."""
    try:
        value = response.json()
    except Exception:  # noqa: BLE001 - an unparseable body is "no JSON", not a failure
        return None
    return value if isinstance(value, (dict, list)) else None


def _field(payload: Any, key: str) -> Any:
    """``payload?.key``: reading a field off a non-object yields nothing, never an error."""
    return payload.get(key) if isinstance(payload, dict) else None


def _is_number(value: Any) -> bool:
    """``typeof value === "number" && Number.isFinite(value)``.

    ``bool`` is an ``int`` subclass in Python but JSON ``true`` is not a number in JS, so
    a server answering ``{"expires_in": true}`` must fail validation here as it does there.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _trusted_http_url(value: Any) -> str | None:
    """The verification URI is opened in the user's browser; only http(s) URLs are trusted.

    ``new URL(value)`` throws on a relative reference, which is what rejects ``"foo"``
    upstream. ``urlsplit`` mostly does not throw, so the scheme and authority are checked
    instead. Where the two parsers differ, and why none of it changes the outcome here:

    * ``urlsplit`` does raise, on a malformed IPv6 literal (``"http://[::1"``), so the
      ``try`` stays -- that case is a rejection either way.
    * ``new URL`` normalises (``"HTTPS://X"`` -> ``"https://x/"``) and pi returns
      ``url.href``. This returns the caller's own string, because the *caller* is what
      matters: upstream reads this only as a boolean guard and then stores the raw
      ``verification_uri`` from the response, exactly as ``_start_device_authorization``
      does below. Nothing downstream ever sees the normalised form.
    * WHATWG's "special scheme" rule accepts a single slash -- ``new URL("https:/x")``
      parses with host ``x`` -- while ``urlsplit`` leaves ``netloc`` empty and this
      rejects it. That is stricter than upstream, and strictness is the safe direction
      for a value that gets handed to a browser.

    ``urlsplit`` adopted part of the WHATWG trimming: it removes embedded tabs and
    newlines, and it *lstrips* C0 controls and spaces -- but only lstrips, its own source
    saying "Only lstrip url as some applications rely on preserving trailing space".
    Leading junk is therefore handled the same on both sides, so ``"  javascript:..."``
    is rejected here as it is upstream rather than sneaking through as a relative
    reference. Trailing junk survives, which changes nothing: what this returns is the
    caller's own string either way, and the scheme/authority the check reads sit at the
    front.
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
    expires: int


async def _start_device_authorization(
    oauth_host: str,
    signal: Any,
    post_form: PostForm,
) -> _DeviceAuthorization:
    response = await post_form(f"{oauth_host}/api/oauth/device_authorization", {"client_id": CLIENT_ID}, signal)

    if not response.ok:
        suffix = f": {response.body}" if response.body else ""
        raise RuntimeError(f"Kimi Code device authorization failed with status {response.status}{suffix}")

    payload = _read_json(response)
    device_code = _field(payload, "device_code")
    user_code = _field(payload, "user_code")
    verification_uri = _field(payload, "verification_uri")
    verification_uri_complete = _field(payload, "verification_uri_complete")
    if (
        not isinstance(device_code, str)
        or not isinstance(user_code, str)
        or not isinstance(verification_uri, str)
        or not isinstance(verification_uri_complete, str)
        or not _trusted_http_url(verification_uri_complete)
        or not _trusted_http_url(verification_uri)
    ):
        raise RuntimeError(f"Invalid Kimi Code device authorization response: {_stringify(payload)}")

    interval = _field(payload, "interval")
    expires_in = _field(payload, "expires_in")
    return _DeviceAuthorization(
        deviceCode=device_code,
        userCode=user_code,
        verificationUri=verification_uri,
        verificationUriComplete=verification_uri_complete,
        intervalSeconds=interval if _is_number(interval) and interval > 0 else DEFAULT_POLL_INTERVAL_SECONDS,
        expiresInSeconds=expires_in if _is_number(expires_in) and expires_in > 0 else DEVICE_CODE_TIMEOUT_SECONDS,
    )


def _parse_token_response(payload: Any, operation: str) -> _TokenResponse:
    access_token = _field(payload, "access_token")
    refresh_token = _field(payload, "refresh_token")
    expires_in = _field(payload, "expires_in")
    if (
        not isinstance(access_token, str)
        or not access_token
        or not isinstance(refresh_token, str)
        or not refresh_token
        or not _is_number(expires_in)
        or expires_in <= 0
    ):
        raise RuntimeError(f"Kimi Code token {operation} response missing fields: {_stringify(payload)}")
    return _TokenResponse(
        access=access_token,
        refresh=refresh_token,
        # `Date.now() + expiresIn * 1000`: an absolute deadline in epoch *milliseconds*,
        # which is the unit `oauth_credentials_expire_soon` compares against. Storing
        # seconds here would leave every fresh token looking decades expired.
        expires=int(time.time() * 1000) + int(expires_in * 1000),
    )


async def _poll_for_token(
    oauth_host: str,
    device: _DeviceAuthorization,
    signal: Any,
    post_form: PostForm,
) -> _TokenResponse:
    # misaka's shared poller is not generic: it hands back the access token string only.
    # The rest of the token lives on this closure instead, so the flow still returns the
    # refresh token and expiry upstream's generic `pollOAuthDeviceCodeFlow<T>` carries.
    settled: _TokenResponse | None = None

    async def poll() -> Any:
        nonlocal settled
        response = await post_form(
            f"{oauth_host}/api/oauth/token",
            {
                "client_id": CLIENT_ID,
                "device_code": device.deviceCode,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            signal,
        )

        if response.status >= 500:
            suffix = f": {response.body}" if response.body else ""
            return {
                "status": "failed",
                "message": f"Kimi Code device token request failed with status {response.status}{suffix}",
            }

        payload = _read_json(response)
        if response.ok and isinstance(_field(payload, "access_token"), str):
            try:
                settled = _parse_token_response(payload, "poll")
            except Exception as error:  # noqa: BLE001 - a malformed success ends the flow, it does not crash it
                return {"status": "failed", "message": str(error)}
            return {"status": "complete", "accessToken": settled.access}

        error_code = _field(payload, "error")
        raw_description = _field(payload, "error_description")
        description = f": {raw_description}" if isinstance(raw_description, str) else ""
        if error_code == "authorization_pending":
            return {"status": "pending"}
        if error_code == "slow_down":
            # KNOWN GAP, and it is this function's: upstream forwards a server-supplied
            # `interval` (kimi-coding.ts:189-195), and misaka's poller does have the
            # channel for it -- `OAuthDeviceCodeSlowDownResult.intervalSeconds`, which
            # `ai/utils/oauth/device_code.py` honours and `xai.py` feeds. This branch is
            # simply not filling it in, so the poller falls back to its fixed
            # five-second bump.
            return {"status": "slow_down"}
        if error_code == "expired_token":
            return {"status": "failed", "message": "Kimi Code device authorization expired. Please restart login."}
        if error_code == "access_denied":
            return {"status": "failed", "message": "Kimi Code login was denied."}
        detail = f": {error_code}{description}" if isinstance(error_code, str) else ""
        return {
            "status": "failed",
            "message": f"Kimi Code device token request failed (status {response.status}){detail}",
        }

    await poll_oauth_device_code_flow(
        intervalSeconds=device.intervalSeconds,
        expiresInSeconds=device.expiresInSeconds,
        poll=poll,
        signal=signal,
        # Upstream sets this: the endpoint rejects a poll that arrives before the
        # device code is registered (device-code.ts callers).
        waitBeforeFirstPoll=True,
    )
    if settled is None:  # pragma: no cover - the poller only returns after `complete` set it
        raise RuntimeError("Kimi Code device token request completed without a token")
    return settled


def _is_retryable_refresh_failure(response: KimiHttpResponse) -> bool:
    return response.status == 429 or response.status >= 500


async def refresh_kimi_coding_token(
    credentials: OAuthCredentials,
    signal: Any = None,
    *,
    oauth_host: str | None = None,
    post_form: PostForm | None = None,
    sleep_ms: SleepMs | None = None,
) -> OAuthCredentials:
    """Exchange the stored refresh token for a fresh access token."""
    host = oauth_host if oauth_host is not None else get_kimi_coding_oauth_host()
    send = post_form or _default_post_form
    wait = sleep_ms or sleep

    last_error: Exception | None = None
    for attempt in range(REFRESH_MAX_RETRIES + 1):
        if attempt > 0:
            await wait(1000 * 2 ** (attempt - 1), signal)
        if signal_aborted(signal):
            raise RuntimeError("Kimi Code token refresh aborted")

        try:
            response = await send(
                f"{host}/api/oauth/token",
                {
                    "client_id": CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": credentials.refresh,
                },
                signal,
            )
        except Exception as error:  # noqa: BLE001 - a transport failure is retried, like any 5xx
            last_error = error
            continue

        payload = _read_json(response)
        if response.ok:
            token = _parse_token_response(payload, "refresh")
            return OAuthCredentials(refresh=token.refresh, access=token.access, expires=token.expires)

        # Unauthorized: the stored credential is dead, so retrying it is pointless -- raise
        # instead of looping. Upstream's comment adds that Models then clears the credential.
        # `core/auth_storage.getApiKey` catches this, reloads, and returns None.
        if response.status in (401, 403) or _field(payload, "error") == "invalid_grant":
            raw_description = _field(payload, "error_description")
            description = f": {raw_description}" if isinstance(raw_description, str) else ""
            raise RuntimeError(f"Kimi Code token refresh unauthorized (status {response.status}){description}")

        if _is_retryable_refresh_failure(response) and attempt < REFRESH_MAX_RETRIES:
            last_error = RuntimeError(f"Kimi Code token refresh failed with status {response.status}")
            continue

        # `JSON.stringify` of a parsed body is never the empty string -- `null` stringifies
        # to "null" -- so upstream's truthiness guard on it can never fail.
        raise RuntimeError(f"Kimi Code token refresh failed with status {response.status}: {_stringify(payload)}")

    raise last_error or RuntimeError("Kimi Code token refresh failed")


async def login_kimi_coding(
    callbacks: OAuthLoginCallbacks,
    *,
    oauth_host: str | None = None,
    post_form: PostForm | None = None,
) -> OAuthCredentials:
    """Run the device-code login and return the credentials to store."""
    host = oauth_host if oauth_host is not None else get_kimi_coding_oauth_host()
    send = post_form or _default_post_form
    signal = getattr(callbacks, "signal", None)

    device = await _start_device_authorization(host, signal, send)
    callbacks.onDeviceCode(
        OAuthDeviceCodeInfo(
            userCode=device.userCode,
            # The *complete* URI is the one shown: it carries the code, so the user does
            # not have to retype it.
            verificationUri=device.verificationUriComplete,
            intervalSeconds=device.intervalSeconds,
            expiresInSeconds=device.expiresInSeconds,
        )
    )
    token = await _poll_for_token(host, device, signal, send)
    return OAuthCredentials(refresh=token.refresh, access=token.access, expires=token.expires)


class _KimiCodingOAuthProvider:
    id = "kimi-coding"
    name = "Kimi Code (subscription)"
    # Device-code: the user authorises out of band, so nothing listens on localhost.
    usesCallbackServer = False
    # Carried here so the wording lives next to the flow it labels; the OAuth bridge takes
    # both as arguments when it builds the `OAuthAuth`.
    isSubscription = True
    loginLabel = "Sign in with Kimi Code"

    def __init__(self, *, post_form: PostForm | None = None, sleep_ms: SleepMs | None = None) -> None:
        self._post_form = post_form
        self._sleep_ms = sleep_ms

    async def login(self, callbacks: OAuthLoginCallbacks) -> OAuthCredentials:
        return await login_kimi_coding(callbacks, post_form=self._post_form)

    async def refreshToken(self, credentials: OAuthCredentials, signal: Any | None = None) -> OAuthCredentials:
        return await refresh_kimi_coding_token(
            credentials, signal, post_form=self._post_form, sleep_ms=self._sleep_ms
        )

    def getApiKey(self, credentials: OAuthCredentials) -> str:
        # Upstream's `toAuth` returns `{headers: {Authorization: "Bearer <access>"}}`
        # (kimi-coding.ts:293-295). `ModelAuth` here has a `headers` slot too, but misaka's
        # OAuth flow protocol (`ai/utils/oauth/types.OAuthProviderInterface`) exposes only
        # `getApiKey` -- plus the optional `getBaseUrl` that `github_copilot.py` adds -- so
        # `ai/auth/oauth_bridge.toAuth` has nothing to fill those headers from. This hands
        # back the access token, which the bridge puts in `ModelAuth.apiKey`.
        return credentials.access

    def getAuthHeaders(self, credentials: OAuthCredentials) -> dict[str, str]:
        """The token goes out as a bearer header, which is how upstream sends it.

        ``kimi-coding.ts:293-295`` returns ``{headers: {Authorization: "Bearer <access>"}}``
        from ``toAuth`` -- no ``apiKey``. Routing it through ``getApiKey`` instead put it in
        ``x-api-key``, which is a different credential slot entirely.
        """
        return {"Authorization": f"Bearer {credentials.access}"}


kimi_coding_oauth_provider = _KimiCodingOAuthProvider()

getKimiCodingOAuthHost = get_kimi_coding_oauth_host
loginKimiCoding = login_kimi_coding
refreshKimiCodingToken = refresh_kimi_coding_token
kimiCodingOAuthProvider = kimi_coding_oauth_provider

__all__ = [
    "FORM_HEADERS",
    "KimiHttpResponse",
    "PostForm",
    "SleepMs",
    "getKimiCodingOAuthHost",
    "get_kimi_coding_oauth_host",
    "kimiCodingOAuthProvider",
    "kimi_coding_oauth_provider",
    "loginKimiCoding",
    "login_kimi_coding",
    "refreshKimiCodingToken",
    "refresh_kimi_coding_token",
]

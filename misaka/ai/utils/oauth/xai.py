"""xAI OAuth device-code flow (SuperGrok / X Premium subscriptions).

Unlike the anthropic flow this one never opens a callback server and never builds a PKCE
challenge: xAI issues a device code, the user approves it in a browser, and the CLI polls
the token endpoint. There is therefore no authorization URL to construct and no `state`
to match -- the device code itself is the correlation handle.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

from misaka.ai.utils.abort import race_with_abort_signal
from misaka.ai.utils.oauth.device_code import poll_oauth_device_code_flow
from misaka.ai.utils.oauth.types import (
    OAuthCredentials,
    OAuthDeviceCodeInfo,
    OAuthLoginCallbacks,
)
from misaka.utils.async_lifecycle import settle
from misaka.utils.values import signal_aborted

CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
SCOPE = "openid profile email offline_access grok-cli:access api:access"
DEVICE_CODE_URL = "https://auth.x.ai/oauth2/device/code"
TOKEN_URL = "https://auth.x.ai/oauth2/token"
# Refresh slightly before the reported expiry to avoid using a token that dies mid-request.
REFRESH_SKEW_MS = 5 * 60 * 1000
DEFAULT_TOKEN_LIFETIME_SECONDS = 3600
CANCEL_MESSAGE = "Login cancelled"
# Upstream passes no timeout and leaves it to the runtime's `fetch`. httpx does have a
# default (`httpx.AsyncClient().timeout` is `Timeout(timeout=5.0)` on the pinned 0.28.1),
# so this is not about adding a bound that was missing -- it is about widening a 5s one
# that a token endpoint on a slow link would trip. 30s matches the anthropic OAuth flow next door
# (`anthropic.py` builds its client with `timeout=30.0`).
REQUEST_TIMEOUT_MS = 30 * 1000
_http_options: ContextVar[Callable[[str], dict[str, Any]] | None] = ContextVar("xai_oauth_http_options", default=None)


@contextmanager
def with_http_options(options):
    """Let the host supply its network route without importing host code into ai."""
    token = _http_options.set(options)
    try:
        yield
    finally:
        _http_options.reset(token)

# WHATWG's URL parser removes leading *and* trailing C0 controls and spaces from its input
# before it parses, which is why `new URL(raw).href` never carries them. CPython's
# `urlsplit` only lstrips -- its own source says "Only lstrip url as some applications rely
# on preserving trailing space" -- so the trailing end has to be trimmed by hand to match.
# U+0000..U+001F are the C0 controls, U+0020 is the space.
_C0_CONTROL_OR_SPACE = "".join(map(chr, range(0x21)))


@dataclass(slots=True)
class _OAuthHttpResponse:
    ok: bool
    status: int
    body: dict[str, Any]


@dataclass(slots=True)
class _XaiDeviceCode:
    deviceCode: str
    userCode: str
    verificationUri: str
    verificationUriComplete: str | None
    intervalSeconds: int | float | None
    expiresInSeconds: int | float


def _required_string(body: Mapping[str, Any], field: str) -> str:
    value = body.get(field)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"Invalid xAI OAuth response field: {field}")
    return value


def _positive_number(body: Mapping[str, Any], field: str) -> int | float:
    value = body.get(field)
    # `typeof value === "number"` excludes JSON booleans; in Python `bool` is an `int`
    # subclass, so it has to be excluded by hand to keep the same acceptance set.
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise RuntimeError(f"Invalid xAI OAuth response field: {field}")
    return value


def _validate_verification_uri(raw: str) -> str:
    """Reject anything that is not an https URL.

    The verification URI is opened in the user's browser; forcing https keeps a malicious
    response from making `open` launch something else (auth/oauth/xai.ts:49-62).

    Python has no `URL.href`, so the normalisation upstream gets for free is done by hand
    over the parts that matter. `urlsplit` is not `new URL`, and the three places they
    disagree are worth naming rather than leaving to be rediscovered:

    * `urlsplit` accepts a relative string, so an empty scheme or host has to be
      rejected explicitly. That also rejects `https:/host`, which `new URL` normalises to
      host `host`. Stricter than upstream, and stricter is the harmless direction: the
      worst case is refusing to open a URL.
    * `urlsplit` happily returns a netloc whose port `new URL` rejects outright
      (`https://x.ai:99999`, `https://x.ai:abc`). That difference runs the *other* way --
      looser than upstream -- so `parts.port` is read below purely to borrow urllib's
      range check and raise the same `ValueError`.
    * `urlsplit` trims C0 controls and spaces off the *front* only, never off the back,
      so the trim is done here instead -- see `_C0_CONTROL_OR_SPACE`. Left to `urlsplit`,
      a verification URI ending in a space or a form feed keeps it: the path comes back as
      `/device` plus the trailing junk, and the browser is handed a URL `new URL().href`
      would never have produced.

    What is still not reproduced is `href`'s percent-encoding of characters *inside* the
    URL: `new URL("https://x.ai/a b").href` yields `.../a%20b`, this yields `.../a b`. That
    changes how the URL renders, not which origin it points at, and the origin is what the
    https check exists to pin down.
    """
    # Both ends, which is WHATWG's first parsing step. The leading half duplicates what
    # `urlsplit` already does; only the trailing half is load-bearing, and doing both here
    # keeps the trim in one visible place instead of half here and half implied.
    trimmed = raw.strip(_C0_CONTROL_OR_SPACE)
    try:
        parts = urlsplit(trimmed)
        # Reading the port is the check; urllib validates the range and digits lazily.
        _ = parts.port
    except ValueError as error:
        raise RuntimeError("Untrusted verification URI in xAI OAuth response") from error
    if parts.scheme.lower() != "https" or not parts.netloc:
        raise RuntimeError("Untrusted verification URI in xAI OAuth response")
    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path or "/", parts.query, parts.fragment))


async def _http_post_form(url: str, fields: dict[str, str], signal: Any = None) -> _OAuthHttpResponse:
    """The real transport. Every caller takes it as a parameter so tests can replace it."""
    if signal_aborted(signal):
        # `fetch` rejects an already-aborted signal without opening a socket
        # (auth/oauth/xai.ts:67-79 lands straight in the `signal.aborted` catch), so the
        # request is not worth sending either.
        raise RuntimeError(CANCEL_MESSAGE)

    async def send() -> Any:
        options = _http_options.get()
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_MS / 1000, **(options(url) if options is not None else {})) as client:
            return await client.post(
                url,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                # `urlencode` percent-encodes with `+` for spaces, which is what
                # `new URLSearchParams(fields)` produces for the space-separated scope.
                content=urlencode(fields).encode(),
            )

    task = asyncio.create_task(send())
    try:
        response = await race_with_abort_signal(task, signal)
    except Exception as error:
        if signal_aborted(signal):
            raise RuntimeError(CANCEL_MESSAGE) from error
        raise
    finally:
        # This HTTP request is owned, unlike a shared Promise passed to the race helper.
        if not task.done():
            task.cancel()
        _, cancelled = await settle(asyncio.gather(task, return_exceptions=True))
        if cancelled is not None:
            raise cancelled

    try:
        parsed: Any = response.json()
    except Exception as error:
        if signal_aborted(signal):
            raise RuntimeError(CANCEL_MESSAGE) from error
        raise RuntimeError(f"xAI OAuth returned invalid JSON (HTTP {response.status_code})") from error

    body = parsed if isinstance(parsed, dict) else {}
    return _OAuthHttpResponse(ok=response.is_success, status=response.status_code, body=body)


def _request_failure_message(action: str, response: _OAuthHttpResponse) -> str:
    error = response.body.get("error")
    description = response.body.get("error_description")
    parts = [part for part in (error, description) if isinstance(part, str) and part]
    detail = ": ".join(parts)
    return f"xAI OAuth {action} failed (HTTP {response.status})" + (f": {detail}" if detail else "")


def _parse_device_code(body: Mapping[str, Any]) -> _XaiDeviceCode:
    # Fall back to the poller's default instead of failing on non-positive or
    # malformed values.
    interval = body.get("interval")
    interval_seconds = (
        interval
        if isinstance(interval, (int, float))
        and not isinstance(interval, bool)
        and math.isfinite(interval)
        and interval > 0
        else None
    )
    raw_complete = body.get("verification_uri_complete")
    verification_uri_complete = (
        _validate_verification_uri(raw_complete) if isinstance(raw_complete, str) and raw_complete else None
    )
    return _XaiDeviceCode(
        deviceCode=_required_string(body, "device_code"),
        userCode=_required_string(body, "user_code"),
        verificationUri=_validate_verification_uri(_required_string(body, "verification_uri")),
        verificationUriComplete=verification_uri_complete,
        intervalSeconds=interval_seconds,
        expiresInSeconds=_positive_number(body, "expires_in"),
    )


def _credentials_from_token_response(
    body: Mapping[str, Any],
    previous_refresh_token: str | None = None,
) -> OAuthCredentials:
    access = _required_string(body, "access_token")
    # xAI may omit refresh_token on refresh when the token is not rotated.
    refresh = (
        previous_refresh_token
        if "refresh_token" not in body and previous_refresh_token
        else _required_string(body, "refresh_token")
    )
    expires_in_seconds = (
        DEFAULT_TOKEN_LIFETIME_SECONDS if "expires_in" not in body else _positive_number(body, "expires_in")
    )
    return OAuthCredentials(
        refresh=refresh,
        access=access,
        expires=int(_now_ms() + expires_in_seconds * 1000 - REFRESH_SKEW_MS),
    )


def _now_ms() -> int:
    """Milliseconds since the epoch, matching `Date.now()`; a seam tests can freeze."""
    return int(time.time() * 1000)


async def _request_device_code(signal: Any, post_form: Any) -> _XaiDeviceCode:
    response = await post_form(
        DEVICE_CODE_URL,
        {
            "client_id": CLIENT_ID,
            "scope": SCOPE,
            "referrer": "pi",
        },
        signal,
    )
    if not response.ok:
        raise RuntimeError(_request_failure_message("device authorization", response))
    return _parse_device_code(response.body)


async def _poll_for_tokens(
    device: _XaiDeviceCode,
    signal: Any,
    post_form: Any,
    poll_device_code: Any,
) -> OAuthCredentials:
    """Drive the shared poller, which reports only an access token string.

    Upstream's poller is generic over the completion value; misaka's carries an
    ``accessToken`` and nothing else, so the full credential is captured here instead of
    being returned through the poller.

    Upstream honours a server-supplied ``interval`` on ``slow_down``
    (``auth/oauth/device-code.ts:78-86``) and so does misaka's poller
    (``ai/utils/oauth/device_code.py``) -- this flow forwards it as ``intervalSeconds``.
    """
    credential: OAuthCredentials | None = None

    async def poll() -> Any:
        nonlocal credential
        response = await post_form(
            TOKEN_URL,
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": CLIENT_ID,
                "device_code": device.deviceCode,
            },
            signal,
        )

        if response.ok:
            credential = _credentials_from_token_response(response.body)
            return {"status": "complete", "accessToken": credential.access}

        error = response.body.get("error")
        if error == "authorization_pending":
            return {"status": "pending"}
        if error == "slow_down":
            # Forward the server's own minimum. The poller prefers it over the RFC 8628
            # five-second bump, because a client that only ever tracks its own interval
            # polls early forever once its clock drifts -- the WSL and VM case.
            interval = response.body.get("interval")
            return {
                "status": "slow_down",
                # `bool` is an `int` subclass, so a JSON `true` would pass an
                # `isinstance(..., int)` test and be read as one second -- collapsing the
                # poll interval to the floor instead of backing off. The same exclusion is
                # applied where this file reads `interval` on the authorisation response.
                "intervalSeconds": (
                    interval
                    if isinstance(interval, (int, float)) and not isinstance(interval, bool)
                    else None
                ),
            }
        if error in ("access_denied", "authorization_denied"):
            return {"status": "failed", "message": "xAI device authorization was denied"}
        if error == "expired_token":
            return {"status": "failed", "message": "xAI device code expired"}
        return {"status": "failed", "message": _request_failure_message("device token polling", response)}

    await poll_device_code(
        intervalSeconds=device.intervalSeconds,
        expiresInSeconds=device.expiresInSeconds,
        poll=poll,
        signal=signal,
        # Upstream sets this: the endpoint rejects a poll that arrives before the
        # device code is registered (device-code.ts callers).
        waitBeforeFirstPoll=True,
    )
    if credential is None:
        # Only reachable if the poller reports completion without ever running `poll`.
        raise RuntimeError("xAI OAuth polling completed without credentials")
    return credential


async def login_xai(
    options: Mapping[str, Any],
    *,
    post_form: Any = None,
    poll_device_code: Any = None,
) -> OAuthCredentials:
    post_form = post_form or _http_post_form
    poll_device_code = poll_device_code or poll_oauth_device_code_flow
    signal = options.get("signal")

    device = await _request_device_code(signal, post_form)
    options["onDeviceCode"](
        OAuthDeviceCodeInfo(
            userCode=device.userCode,
            # The complete URI already carries the code, so it is the one worth showing.
            verificationUri=device.verificationUriComplete or device.verificationUri,
            intervalSeconds=device.intervalSeconds,
            expiresInSeconds=device.expiresInSeconds,
        )
    )
    return await _poll_for_tokens(device, signal, post_form, poll_device_code)


async def refresh_xai_token(
    refresh_token: str,
    signal: Any = None,
    *,
    post_form: Any = None,
) -> OAuthCredentials:
    post_form = post_form or _http_post_form
    response = await post_form(
        TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": refresh_token,
        },
        signal,
    )
    if not response.ok:
        raise RuntimeError(_request_failure_message("token refresh", response))
    return _credentials_from_token_response(response.body, refresh_token)


class _XaiOAuthProvider:
    id = "xai"
    name = "xAI (Grok/X subscription)"
    # Device code: the user approves in a browser and the CLI polls, so nothing listens locally.
    usesCallbackServer = False

    async def login(
        self,
        callbacks: OAuthLoginCallbacks,
        *,
        post_form: Any = None,
        poll_device_code: Any = None,
    ) -> OAuthCredentials:
        return await login_xai(
            {
                "onDeviceCode": callbacks.onDeviceCode,
                "signal": callbacks.signal,
            },
            post_form=post_form,
            poll_device_code=poll_device_code,
        )

    async def refreshToken(
        self,
        credentials: OAuthCredentials,
        signal: Any | None = None,
        *,
        post_form: Any = None,
    ) -> OAuthCredentials:
        return await refresh_xai_token(credentials.refresh, signal, post_form=post_form)

    def getApiKey(self, credentials: OAuthCredentials) -> str:
        return credentials.access


xai_oauth_provider = _XaiOAuthProvider()

loginXai = login_xai
refreshXaiToken = refresh_xai_token
xaiOAuthProvider = xai_oauth_provider

__all__ = [
    "loginXai",
    "login_xai",
    "refreshXaiToken",
    "refresh_xai_token",
    "xaiOAuthProvider",
    "xai_oauth_provider",
]

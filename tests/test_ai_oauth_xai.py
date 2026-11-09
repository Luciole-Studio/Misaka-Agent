"""Translated from pi's ``packages/ai/test/xai-oauth.test.ts`` (v1.1.0). See ``tests/oauth_fetch.py`` for the
``fetch`` and fake-timer stand-ins."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from misaka.ai.auth.oauth import xai
from misaka.ai.auth.oauth.xai import xaiOAuth
from misaka.ai.auth.types import ModelAuth, OAuthCredential
from misaka.utils.js import UNDEFINED, AbortController
from tests.oauth_fetch import (
    epochMs,
    interaction,
    jsonResponse,
    requestForm,
    requestUrl,
    stubFetch,
    useFakeTimers,
    withOverrides,
)


def deviceCodeResponse(**overrides: Any) -> dict[str, Any]:
    return withOverrides(
        {
            "device_code": "device-code",
            "user_code": "ABCD-1234",
            "verification_uri": "https://accounts.x.ai/oauth2/device",
            "expires_in": 900,
            "interval": 5,
        },
        overrides,
    )


def tokenResponse(**overrides: Any) -> dict[str, Any]:
    return withOverrides(
        {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 21_600,
            "token_type": "Bearer",
        },
        overrides,
    )


def loginXaiForTest(onDeviceCode: Any, signal: Any = None) -> asyncio.Future[OAuthCredential]:
    def notify(event: Any) -> None:
        if event.type == "device_code":
            onDeviceCode(event.model_dump(exclude={"type"}, exclude_none=True))

    return asyncio.ensure_future(xaiOAuth.login(interaction(signal=signal, notify=notify)))


def refreshXaiForTest(refreshToken: str) -> Any:
    return xaiOAuth.refresh(
        OAuthCredential(type="oauth", access="old-access", refresh=refreshToken, expires=0),
        AbortController().signal,
    )


async def test_uses_the_device_grant_delays_polling_and_handles_pending_and_slow_down(monkeypatch):
    startTime = epochMs("2026-07-09T20:00:00Z")
    timers = useFakeTimers(monkeypatch, startTime)
    pollTimes: list[float] = []
    tokenReplies = [
        jsonResponse({"error": "authorization_pending"}, 400),
        jsonResponse({"error": "slow_down", "interval": 10}, 400),
        jsonResponse(tokenResponse()),
    ]

    def fetchMock(input: Any, init: Any = None) -> Any:
        url = requestUrl(input)

        if url == "https://auth.x.ai/oauth2/device/code":
            form = requestForm(init)
            assert form.get("client_id") == "b1a00492-073a-47ea-816f-4c329264a828"
            assert form.get("scope") == "openid profile email offline_access grok-cli:access api:access"
            assert form.get("referrer") == "pi"
            return jsonResponse(deviceCodeResponse())

        if url == "https://auth.x.ai/oauth2/token":
            pollTimes.append(timers.now)
            form = requestForm(init)
            assert form.get("grant_type") == "urn:ietf:params:oauth:grant-type:device_code"
            assert form.get("client_id") == "b1a00492-073a-47ea-816f-4c329264a828"
            assert form.get("device_code") == "device-code"
            if not tokenReplies:
                raise RuntimeError("Unexpected token poll")
            return tokenReplies.pop(0)

        raise RuntimeError(f"Unexpected request: {url}")

    stubFetch(monkeypatch, xai, fetchMock)

    deviceCodes: list[dict[str, Any]] = []
    loginPromise = loginXaiForTest(deviceCodes.append)

    await timers.advanceAsync(0)
    assert deviceCodes == [
        {
            "userCode": "ABCD-1234",
            "verificationUri": "https://accounts.x.ai/oauth2/device",
            "intervalSeconds": 5,
            "expiresInSeconds": 900,
        },
    ]
    assert pollTimes == []

    await timers.advanceAsync(5000)
    assert pollTimes == [startTime + 5000]

    # slow_down raised the interval to 10 seconds
    await timers.advanceAsync(5000)
    assert pollTimes == [startTime + 5000, startTime + 10_000]

    await timers.advanceAsync(10_000)
    credentials = await loginPromise
    assert pollTimes == [startTime + 5000, startTime + 10_000, startTime + 20_000]
    assert credentials.model_dump() == {
        "type": "oauth",
        "access": "access-token",
        "refresh": "refresh-token",
        "expires": startTime + 20_000 + 21_600_000 - 300_000,
    }


async def test_falls_back_to_the_default_poll_interval_when_the_response_reports_interval_0(monkeypatch):
    startTime = epochMs("2026-07-09T20:00:00Z")
    timers = useFakeTimers(monkeypatch, startTime)
    pollTimes: list[float] = []

    def fetchMock(input: Any, init: Any = None) -> Any:
        if requestUrl(input) == "https://auth.x.ai/oauth2/device/code":
            return jsonResponse(deviceCodeResponse(interval=0))
        pollTimes.append(timers.now)
        return jsonResponse(tokenResponse())

    stubFetch(monkeypatch, xai, fetchMock)

    loginPromise = loginXaiForTest(lambda _info: None)
    # RFC 8628 default interval is 5 seconds when the server does not require a wait.
    await timers.advanceAsync(5000)
    await loginPromise
    assert pollTimes == [startTime + 5000]


async def test_prefers_verification_uri_complete_when_the_server_provides_it(monkeypatch):
    timers = useFakeTimers(monkeypatch)

    def fetchMock(input: Any, init: Any = None) -> Any:
        if requestUrl(input) == "https://auth.x.ai/oauth2/device/code":
            return jsonResponse(
                deviceCodeResponse(verification_uri_complete="https://accounts.x.ai/oauth2/device?user_code=ABCD-1234")
            )
        return jsonResponse(tokenResponse())

    stubFetch(monkeypatch, xai, fetchMock)

    deviceCodes: list[dict[str, Any]] = []
    loginPromise = loginXaiForTest(deviceCodes.append)
    await timers.advanceAsync(5000)
    await loginPromise
    assert deviceCodes == [
        {
            "userCode": "ABCD-1234",
            "verificationUri": "https://accounts.x.ai/oauth2/device?user_code=ABCD-1234",
            "intervalSeconds": 5,
            "expiresInSeconds": 900,
        },
    ]


async def test_rejects_a_non_https_verification_uri_complete(monkeypatch):
    stubFetch(
        monkeypatch,
        xai,
        lambda *_: jsonResponse(
            deviceCodeResponse(verification_uri_complete="http://accounts.x.ai/oauth2/device?user_code=ABCD-1234")
        ),
    )

    with pytest.raises(RuntimeError, match="Untrusted verification URI"):
        await loginXaiForTest(lambda _info: None)


@pytest.mark.parametrize("verificationUri", ["http://accounts.x.ai/oauth2/device", "file:///etc/passwd", "not a url"])
async def test_rejects_a_non_https_verification_uri(monkeypatch, verificationUri):
    stubFetch(monkeypatch, xai, lambda *_: jsonResponse(deviceCodeResponse(verification_uri=verificationUri)))

    with pytest.raises(RuntimeError, match="Untrusted verification URI"):
        await loginXaiForTest(lambda _info: None)


@pytest.mark.parametrize("error", ["access_denied", "authorization_denied"])
async def test_fails_when_device_authorization_is_denied(monkeypatch, error):
    timers = useFakeTimers(monkeypatch)
    requestCount = 0

    def fetchMock(*_: Any) -> Any:
        nonlocal requestCount
        requestCount += 1
        return jsonResponse(deviceCodeResponse(interval=1)) if requestCount == 1 else jsonResponse({"error": error}, 400)

    stubFetch(monkeypatch, xai, fetchMock)

    loginPromise = loginXaiForTest(lambda _info: None)
    await timers.advanceAsync(1000)
    with pytest.raises(RuntimeError, match="xAI device authorization was denied"):
        await loginPromise


async def test_cancels_while_waiting_for_the_first_token_poll(monkeypatch):
    useFakeTimers(monkeypatch)
    controller = AbortController()
    fetchMock = stubFetch(monkeypatch, xai, lambda *_: jsonResponse(deviceCodeResponse()))

    loginPromise = loginXaiForTest(lambda _info: controller.abort(), signal=controller.signal)

    with pytest.raises(RuntimeError, match="Login cancelled"):
        await loginPromise
    assert len(fetchMock.calls) == 1


async def test_refreshes_tokens_and_preserves_an_unrotated_refresh_token(monkeypatch):
    requestCount = 0

    def fetchMock(input: Any, init: Any = None) -> Any:
        nonlocal requestCount
        assert requestUrl(input) == "https://auth.x.ai/oauth2/token"
        form = requestForm(init)
        assert form.get("grant_type") == "refresh_token"
        assert form.get("client_id") == "b1a00492-073a-47ea-816f-4c329264a828"
        requestCount += 1
        if requestCount == 1:
            assert form.get("refresh_token") == "old-refresh"
            return jsonResponse(tokenResponse(access_token="new-access", refresh_token="new-refresh"))
        assert form.get("refresh_token") == "keep-refresh"
        return jsonResponse(tokenResponse(access_token="newer-access", refresh_token=UNDEFINED))

    stubFetch(monkeypatch, xai, fetchMock)

    rotated = await refreshXaiForTest("old-refresh")
    preserved = await refreshXaiForTest("keep-refresh")
    assert rotated.type == "oauth"
    assert rotated.refresh == "new-refresh"
    assert rotated.access == "new-access"
    assert preserved.refresh == "keep-refresh"
    assert preserved.access == "newer-access"
    assert xaiOAuth.name == "xAI (Grok/X subscription)"
    assert await xaiOAuth.toAuth(preserved) == ModelAuth(apiKey="newer-access")


async def test_assumes_a_one_hour_lifetime_when_expires_in_is_missing(monkeypatch):
    startTime = epochMs("2026-07-09T20:00:00Z")
    useFakeTimers(monkeypatch, startTime)
    stubFetch(monkeypatch, xai, lambda *_: jsonResponse(tokenResponse(expires_in=UNDEFINED)))

    credentials = await refreshXaiForTest("old-refresh")
    assert credentials.expires == startTime + 3_600_000 - 300_000


async def test_rejects_token_responses_with_missing_fields(monkeypatch):
    stubFetch(monkeypatch, xai, lambda *_: jsonResponse(tokenResponse(access_token=UNDEFINED)))

    with pytest.raises(RuntimeError, match="Invalid xAI OAuth response field: access_token"):
        await refreshXaiForTest("old-refresh")


async def test_surfaces_the_upstream_error_code_and_description_on_refresh_failure(monkeypatch):
    stubFetch(
        monkeypatch,
        xai,
        lambda *_: jsonResponse({"error": "invalid_grant", "error_description": "refresh token revoked"}, 400),
    )

    with pytest.raises(
        RuntimeError, match="xAI OAuth token refresh failed \\(HTTP 400\\): invalid_grant: refresh token revoked"
    ):
        await refreshXaiForTest("old-refresh")

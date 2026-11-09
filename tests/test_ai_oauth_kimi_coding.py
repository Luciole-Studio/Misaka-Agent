"""Translated from pi's ``packages/ai/test/kimi-coding-oauth.test.ts`` (v1.1.0). See ``tests/oauth_fetch.py`` for the
``fetch`` and fake-timer stand-ins."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from misaka.ai.auth.oauth import kimi_coding
from misaka.ai.auth.oauth.kimi_coding import kimiCodingOAuth
from misaka.ai.auth.types import ModelAuth, OAuthCredential
from misaka.utils.js import AbortController
from misaka.utils.whatwg_url import URLSearchParams
from tests.oauth_fetch import (
    epochMs,
    interaction,
    jsonResponse,
    requestUrl,
    stubFetch,
    useFakeTimers,
)

CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
OAUTH_HOST = "https://auth.kimi.com"


def deviceAuthorizationResponse(**overrides: Any) -> Any:
    return jsonResponse(
        {
            "user_code": "ABCD-1234",
            "device_code": "device-code-123",
            "verification_uri": "https://www.kimi.com/code",
            "verification_uri_complete": "https://www.kimi.com/code?user_code=ABCD-1234",
            "interval": 5,
            "expires_in": 600,
            **overrides,
        }
    )


def createInteraction(events: list[dict[str, Any]]) -> Any:
    async def prompt(_prompt: Any) -> str:
        raise RuntimeError("Kimi Code login should not prompt")

    return interaction(prompt=prompt, notify=lambda event: events.append(event.model_dump(exclude_none=True)))


@pytest.fixture(autouse=True)
def _host(monkeypatch):
    monkeypatch.delenv("KIMI_CODE_OAUTH_HOST", raising=False)
    monkeypatch.delenv("KIMI_OAUTH_HOST", raising=False)


async def test_logs_in_with_the_device_authorization_flow(monkeypatch):
    startTime = epochMs("2026-07-20T00:00:00Z")
    timers = useFakeTimers(monkeypatch, startTime)

    events: list[dict[str, Any]] = []
    pollResponses = [
        jsonResponse({"error": "authorization_pending"}, 400),
        jsonResponse({"access_token": "access-token", "refresh_token": "refresh-token", "expires_in": 3600}),
    ]
    pollTimes: list[float] = []

    def fetchMock(input: Any, init: Any = None) -> Any:
        url = requestUrl(input)
        if url == f"{OAUTH_HOST}/api/oauth/device_authorization":
            assert init["method"] == "POST"
            assert init["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
            assert init["headers"]["Accept"] == "application/json"
            assert URLSearchParams(str(init["body"])).get("client_id") == CLIENT_ID
            return deviceAuthorizationResponse()
        if url == f"{OAUTH_HOST}/api/oauth/token":
            pollTimes.append(timers.now)
            params = URLSearchParams(str(init["body"]))
            assert params.get("grant_type") == "urn:ietf:params:oauth:grant-type:device_code"
            assert params.get("client_id") == CLIENT_ID
            assert params.get("device_code") == "device-code-123"
            if not pollResponses:
                raise RuntimeError("Unexpected extra token poll")
            return pollResponses.pop(0)
        raise RuntimeError(f"Unexpected fetch URL: {url}")

    stubFetch(monkeypatch, kimi_coding, fetchMock)

    credentialPromise = asyncio.ensure_future(kimiCodingOAuth.login(createInteraction(events)))
    for _ in range(5):
        if events:
            break
        await timers.advanceAsync(0)
    assert events == [
        {
            "type": "device_code",
            "userCode": "ABCD-1234",
            "verificationUri": "https://www.kimi.com/code?user_code=ABCD-1234",
            "intervalSeconds": 5,
            "expiresInSeconds": 600,
        },
    ]

    # waitBeforeFirstPoll: first poll happens after the 5s interval.
    await timers.advanceAsync(4999)
    assert pollTimes == []
    await timers.advanceAsync(1)
    assert pollTimes == [startTime + 5000]

    await timers.advanceAsync(5000)
    assert (await credentialPromise).model_dump() == {
        "type": "oauth",
        "access": "access-token",
        "refresh": "refresh-token",
        "expires": startTime + 10000 + 3600 * 1000,
    }
    assert pollTimes == [startTime + 5000, startTime + 10000]


async def _deviceThenToken(monkeypatch, tokenReply: Any) -> asyncio.Future[Any]:
    def fetchMock(input: Any, init: Any = None) -> Any:
        url = requestUrl(input)
        if url == f"{OAUTH_HOST}/api/oauth/device_authorization":
            return deviceAuthorizationResponse()
        if url == f"{OAUTH_HOST}/api/oauth/token":
            return tokenReply
        raise RuntimeError(f"Unexpected fetch URL: {url}")

    stubFetch(monkeypatch, kimi_coding, fetchMock)
    return asyncio.ensure_future(kimiCodingOAuth.login(createInteraction([])))


async def test_fails_when_the_device_code_expires(monkeypatch):
    timers = useFakeTimers(monkeypatch)
    credentialPromise = await _deviceThenToken(monkeypatch, jsonResponse({"error": "expired_token"}, 400))
    await timers.advanceAsync(5000)
    with pytest.raises(RuntimeError, match="expired"):
        await credentialPromise


async def test_fails_when_the_user_denies_the_login(monkeypatch):
    timers = useFakeTimers(monkeypatch)
    credentialPromise = await _deviceThenToken(monkeypatch, jsonResponse({"error": "access_denied"}, 400))
    await timers.advanceAsync(5000)
    with pytest.raises(RuntimeError, match="denied"):
        await credentialPromise


async def test_honors_the_kimi_code_oauth_host_override(monkeypatch):
    timers = useFakeTimers(monkeypatch)
    monkeypatch.setenv("KIMI_CODE_OAUTH_HOST", "https://auth.example.com/")

    urls: list[str] = []

    def fetchMock(input: Any, init: Any = None) -> Any:
        url = requestUrl(input)
        urls.append(url)
        if url == "https://auth.example.com/api/oauth/device_authorization":
            return deviceAuthorizationResponse(interval=1)
        if url == "https://auth.example.com/api/oauth/token":
            return jsonResponse({"access_token": "a", "refresh_token": "r", "expires_in": 60})
        raise RuntimeError(f"Unexpected fetch URL: {url}")

    stubFetch(monkeypatch, kimi_coding, fetchMock)

    credentialPromise = asyncio.ensure_future(kimiCodingOAuth.login(createInteraction([])))
    await timers.advanceAsync(1000)
    credential = await credentialPromise
    assert (credential.access, credential.refresh) == ("a", "r")
    assert urls == [
        "https://auth.example.com/api/oauth/device_authorization",
        "https://auth.example.com/api/oauth/token",
    ]


async def test_refreshes_tokens_and_returns_a_bearer_header_for_requests(monkeypatch):
    def fetchMock(input: Any, init: Any = None) -> Any:
        url = requestUrl(input)
        assert url == f"{OAUTH_HOST}/api/oauth/token"
        params = URLSearchParams(str(init["body"]))
        assert params.get("grant_type") == "refresh_token"
        assert params.get("refresh_token") == "old-refresh"
        assert params.get("client_id") == CLIENT_ID
        return jsonResponse({"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600})

    stubFetch(monkeypatch, kimi_coding, fetchMock)

    before = time.time() * 1000
    credential = await kimiCodingOAuth.refresh(
        OAuthCredential(type="oauth", access="old-access", refresh="old-refresh", expires=before),
        AbortController().signal,
    )
    dumped = credential.model_dump()
    assert {key: dumped[key] for key in ("type", "access", "refresh")} == {
        "type": "oauth",
        "access": "new-access",
        "refresh": "new-refresh",
    }
    assert isinstance(credential.expires, int)
    assert credential.expires >= before + 3600 * 1000 - 1

    assert await kimiCodingOAuth.toAuth(credential) == ModelAuth(headers={"Authorization": "Bearer new-access"})


async def test_retries_refresh_on_429_and_fails_unauthorized_on_invalid_grant(monkeypatch):
    timers = useFakeTimers(monkeypatch)

    # 429 once, then success.
    calls = 0

    def fetchMock(*_: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            return jsonResponse({"error": "temporarily_unavailable"}, 429)
        return jsonResponse({"access_token": "a", "refresh_token": "r", "expires_in": 60})

    stubFetch(monkeypatch, kimi_coding, fetchMock)

    refreshPromise = asyncio.ensure_future(
        kimiCodingOAuth.refresh(
            OAuthCredential(type="oauth", access="old", refresh="old", expires=0),
            AbortController().signal,
        )
    )
    await timers.advanceAsync(1000)
    assert (await refreshPromise).access == "a"
    assert calls == 2

    # invalid_grant is not retried.
    stubFetch(monkeypatch, kimi_coding, lambda *_: jsonResponse({"error": "invalid_grant"}, 400))
    with pytest.raises(RuntimeError, match="unauthorized"):
        await kimiCodingOAuth.refresh(
            OAuthCredential(type="oauth", access="old", refresh="old", expires=0),
            AbortController().signal,
        )

"""Translated from pi's ``packages/ai/test/meta-oauth.test.ts`` (v1.1.0). See ``tests/oauth_fetch.py`` for the
``fetch`` and fake-timer stand-ins."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from misaka.ai.auth.oauth import meta
from misaka.ai.auth.oauth.meta import metaOAuth
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

CLIENT_ID = "1031625952748946"
DEVICE_AUTHORIZATION_URL = "https://auth.meta.com/oidc/device/authorization/"
DEVICE_TOKEN_URL = "https://auth.meta.com/oidc/device/token/"
MINT_URL = "https://api.meta.ai/muse-code/key"
DAY_MS = 24 * 60 * 60 * 1000


def createInteraction(events: list[dict[str, Any]]) -> Any:
    async def prompt(_prompt: Any) -> str:
        raise RuntimeError("Meta login should not prompt")

    return interaction(prompt=prompt, notify=lambda event: events.append(event.model_dump(exclude_none=True)))


async def test_logs_in_with_the_device_flow_and_mints_a_model_api_key(monkeypatch):
    startTime = epochMs("2026-09-03T00:00:00Z")
    timers = useFakeTimers(monkeypatch, startTime)

    events: list[dict[str, Any]] = []
    pollResponses = [
        jsonResponse({"error": "authorization_pending"}, 400),
        jsonResponse({"access_token": "identity-token", "token_type": "Bearer"}),
    ]

    def fetchMock(input: Any, init: Any = None) -> Any:
        url = requestUrl(input)
        if url == DEVICE_AUTHORIZATION_URL:
            assert init["method"] == "POST"
            assert URLSearchParams(str(init["body"])).get("client_id") == CLIENT_ID
            return jsonResponse(
                {
                    "device_code": "device-code-123",
                    "user_code": "ABCD-1234",
                    "verification_uri": "https://auth.meta.com/oauth/device/",
                    "verification_uri_complete": "https://auth.meta.com/oauth/device/?code=ABCD-1234",
                    "interval": 5,
                    "expires_in": 600,
                }
            )
        if url == DEVICE_TOKEN_URL:
            params = URLSearchParams(str(init["body"]))
            assert params.get("grant_type") == "urn:ietf:params:oauth:grant-type:device_code"
            assert params.get("client_id") == CLIENT_ID
            assert params.get("device_code") == "device-code-123"
            if not pollResponses:
                raise RuntimeError("Unexpected extra token poll")
            return pollResponses.pop(0)
        if url == MINT_URL:
            assert init["method"] == "POST"
            assert init["headers"]["Authorization"] == "Bearer identity-token"
            return jsonResponse({"api_key": "LLM|minted-key"})
        raise RuntimeError(f"Unexpected fetch URL: {url}")

    stubFetch(monkeypatch, meta, fetchMock)

    credentialPromise = asyncio.ensure_future(metaOAuth.login(createInteraction(events)))
    for _ in range(5):
        if events:
            break
        await timers.advanceAsync(0)
    assert events[0] == {
        "type": "device_code",
        "userCode": "ABCD-1234",
        "verificationUri": "https://auth.meta.com/oauth/device/?code=ABCD-1234",
        "intervalSeconds": 5,
        "expiresInSeconds": 600,
    }

    await timers.advanceAsync(10000)
    assert (await credentialPromise).model_dump() == {
        "type": "oauth",
        "refresh": "identity-token",
        "access": "LLM|minted-key",
        "expires": startTime + 10000 + DAY_MS,
    }


async def test_re_mints_the_api_key_from_the_stored_identity_token_on_refresh(monkeypatch):
    now = epochMs("2026-09-03T12:00:00Z")
    useFakeTimers(monkeypatch, now)

    def fetchMock(input: Any, init: Any = None) -> Any:
        assert requestUrl(input) == MINT_URL
        assert init["headers"]["Authorization"] == "Bearer identity-token"
        return jsonResponse({"api_key": "LLM|fresh-key"})

    stubFetch(monkeypatch, meta, fetchMock)

    refreshed = await metaOAuth.refresh(
        OAuthCredential(type="oauth", refresh="identity-token", access="LLM|old-key", expires=1),
        AbortController().signal,
    )
    assert refreshed.model_dump() == {
        "type": "oauth",
        "refresh": "identity-token",
        "access": "LLM|fresh-key",
        "expires": now + DAY_MS,
    }


async def test_reports_the_setup_url_when_meta_issues_no_key(monkeypatch):
    stubFetch(
        monkeypatch,
        meta,
        lambda *_: jsonResponse({"require_payment": True, "action_url": "https://dev.meta.ai/billing"}),
    )
    with pytest.raises(RuntimeError, match="Complete setup at https://dev.meta.ai/billing"):
        await metaOAuth.refresh(
            OAuthCredential(type="oauth", refresh="identity-token", access="", expires=1),
            AbortController().signal,
        )


async def test_uses_the_minted_key_as_the_request_api_key():
    assert await metaOAuth.toAuth(
        OAuthCredential(type="oauth", refresh="identity-token", access="LLM|key", expires=1)
    ) == ModelAuth(apiKey="LLM|key")

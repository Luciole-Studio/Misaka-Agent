"""Translated from pi's ``packages/ai/test/radius-oauth.test.ts`` (v1.1.0). See ``tests/oauth_fetch.py`` for the
``fetch`` and fake-timer stand-ins; the browser's request to the loopback callback goes out over httpx."""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import httpx
import pytest

from misaka.ai.auth.oauth import radius
from misaka.ai.auth.oauth.radius import RadiusOAuthOptions, createRadiusOAuth
from misaka.ai.auth.types import OAuthCredential
from misaka.utils.js import AbortController
from misaka.utils.whatwg_url import URL, URLSearchParams
from tests.oauth_fetch import (
    epochMs,
    jsonResponse,
    requestUrl,
    stubFetch,
    useFakeTimers,
)
from tests.oauth_fetch import interaction as baseInteraction

GATEWAY = "https://radius.example"


def interaction(loginMethod: str, events: list[dict[str, Any]] | None = None) -> Any:
    sink = events if events is not None else []

    async def prompt(_prompt: Any) -> str:
        return loginMethod

    return baseInteraction(prompt=prompt, notify=lambda event: sink.append(event.model_dump(exclude_none=True)))


async def test_uses_gateway_endpoints_directly_for_device_login(monkeypatch):
    timers = useFakeTimers(monkeypatch, epochMs("2026-07-24T00:00:00Z"))
    events: list[dict[str, Any]] = []
    urls: list[str] = []

    def fetchMock(input: Any, init: Any = None) -> Any:
        url = requestUrl(input)
        urls.append(url)
        form = URLSearchParams(str(init.get("body")))
        if url == f"{GATEWAY}/v1/oauth/device":
            assert form.get("client_id") == "pi-gateway"
            assert form.get("scope") == "gateway offline_access"
            return jsonResponse(
                {
                    "device_code": "device-code",
                    "user_code": "ABCD-1234",
                    "verification_uri": "https://radius-ui.example/pair",
                    "expires_in": 600,
                    "interval": 5,
                }
            )
        if url == f"{GATEWAY}/v1/oauth/token":
            assert form.get("grant_type") == "urn:ietf:params:oauth:grant-type:device_code"
            assert form.get("client_id") == "pi-gateway"
            assert form.get("device_code") == "device-code"
            return jsonResponse(
                {
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "expires_in": 3600,
                    "scope": "gateway offline_access",
                }
            )
        raise RuntimeError(f"Unexpected request: {url}")

    stubFetch(monkeypatch, radius, fetchMock)

    oauth = createRadiusOAuth(RadiusOAuthOptions(name="Radius", gateway=GATEWAY))
    credential = await oauth.login(interaction("device-code", events))
    assert credential.model_dump() == {
        "type": "oauth",
        "access": "access-token",
        "refresh": "refresh-token",
        "expires": timers.now + 3600 * 1000 - 60_000,
        "scope": "gateway offline_access",
    }
    assert events == [
        {
            "type": "device_code",
            "userCode": "ABCD-1234",
            "verificationUri": "https://radius-ui.example/pair",
            "intervalSeconds": 5,
            "expiresInSeconds": 600,
        },
    ]
    assert urls == [f"{GATEWAY}/v1/oauth/device", f"{GATEWAY}/v1/oauth/token"]


async def test_refreshes_directly_through_the_gateway_without_discovery(monkeypatch):
    def fetchMock(input: Any, init: Any = None) -> Any:
        assert requestUrl(input) == f"{GATEWAY}/v1/oauth/token"
        form = URLSearchParams(str(init.get("body")))
        assert form.get("grant_type") == "refresh_token"
        assert form.get("client_id") == "pi-gateway"
        assert form.get("refresh_token") == "old-refresh"
        return jsonResponse({"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600})

    fetchMock_ = stubFetch(monkeypatch, radius, fetchMock)

    oauth = createRadiusOAuth(RadiusOAuthOptions(name="Radius", gateway=GATEWAY))
    refreshed = await oauth.refresh(
        OAuthCredential(type="oauth", access="old-access", refresh="old-refresh", expires=0),
        AbortController().signal,
    )
    assert (refreshed.access, refreshed.refresh) == ("new-access", "new-refresh")
    assert len(fetchMock_.calls) == 1


async def test_discovers_only_the_interactive_browser_authorization_endpoint(monkeypatch):
    def fetchMock(input: Any, init: Any = None) -> Any:
        assert requestUrl(input) == f"{GATEWAY}/v1/oauth"
        return jsonResponse({"issuer": "https://radius-ui.example"})

    fetchMock_ = stubFetch(monkeypatch, radius, fetchMock)

    oauth = createRadiusOAuth(RadiusOAuthOptions(name="Radius", gateway=GATEWAY))
    with pytest.raises(RuntimeError, match=f"Invalid Radius OAuth config from {GATEWAY}"):
        await oauth.login(interaction("browser"))
    assert len(fetchMock_.calls) == 1


def _requirePort(port: int) -> None:
    """MISAKA fork: the flow binds a fixed port; a machine that reserves it cannot run the browser leg."""
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", port))
    except OSError as error:
        pytest.skip(f"port {port} is unavailable here: {error}")
    finally:
        probe.close()


async def test_exchanges_the_browser_callback_code_before_showing_the_sign_in_page(monkeypatch):
    _requirePort(1456)
    tokenStatus = 400

    def fetchMock(input: Any, init: Any = None) -> Any:
        url = requestUrl(input)
        if url == f"{GATEWAY}/v1/oauth":
            return jsonResponse({"authorizationEndpoint": f"{GATEWAY}/authorize"})
        if url == f"{GATEWAY}/v1/oauth/token":
            body = URLSearchParams(str(init.get("body")))
            assert body.get("code") == "browser-code"
            return (
                jsonResponse({"access_token": "access", "refresh_token": "refresh", "expires_in": 3600})
                if tokenStatus == 200
                else jsonResponse({"error": "invalid_grant", "error_description": "code expired"}, tokenStatus)
            )
        raise RuntimeError(f"Unexpected request: {url}")

    stubFetch(monkeypatch, radius, fetchMock)

    oauth = createRadiusOAuth(RadiusOAuthOptions(name="Radius", gateway=GATEWAY))

    async def login() -> dict[str, Any]:
        pages: list[asyncio.Future[httpx.Response]] = []

        def notify(event: Any) -> None:
            if event.type != "auth_url":
                return
            authorize = URL(event.url)
            callback = URL(authorize.searchParams.get("redirect_uri") or "")
            callback.searchParams.set("code", "browser-code")
            callback.searchParams.set("state", authorize.searchParams.get("state") or "")

            async def get() -> httpx.Response:
                async with httpx.AsyncClient(trust_env=False) as client:
                    return await client.get(callback.toString())

            pages.append(asyncio.ensure_future(get()))

        login = interaction("browser")
        login.notify = notify
        try:
            outcome: dict[str, Any] = {"credential": await oauth.login(login)}
        except Exception as error:  # noqa: BLE001 - the outcome is asserted
            outcome = {"error": error}
        response = await pages[0]
        return {**outcome, "status": response.status_code, "page": response.text}

    failed = await login()
    assert "invalid_grant: code expired" in str(failed["error"])
    assert failed["status"] == 502
    assert "code expired" in failed["page"]

    tokenStatus = 200
    succeeded = await login()
    assert succeeded["credential"].access == "access"
    assert succeeded["status"] == 200
    assert "Signed in to Radius." in succeeded["page"]

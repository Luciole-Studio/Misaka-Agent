"""Translated from pi's ``packages/ai/test/anthropic-oauth.test.ts`` (v1.1.0). See ``tests/oauth_fetch.py`` for the
``fetch`` stand-in; the browser's request to the loopback callback goes out over httpx."""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any

import httpx
import pytest

from misaka.ai.auth.oauth import anthropic
from misaka.ai.auth.oauth.anthropic import anthropicOAuth
from misaka.ai.auth.types import OAuthCredential
from misaka.utils.js import AbortController
from misaka.utils.whatwg_url import URL, URLSearchParams
from tests.oauth_fetch import interaction, jsonResponse, requestUrl, stubFetch


def getJsonBody(init: Any) -> dict[str, str]:
    body = (init or {}).get("body")
    if not isinstance(body, str):
        raise RuntimeError(f"Expected string request body, got {type(body).__name__}")  # noqa: TRY004 - upstream throws Error
    return json.loads(body)


@pytest.fixture(autouse=True)
def _callbackHost(monkeypatch):
    monkeypatch.delenv("MISAKA_OAUTH_CALLBACK_HOST", raising=False)


def _portFree(port: int) -> bool:
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


async def test_keeps_the_localhost_redirect_uri_for_manual_callback_login(monkeypatch):
    if not _portFree(53692):
        pytest.skip("port 53692 is unavailable here")
    sink: dict[str, str] = {}

    def fetchMock(input: Any, init: Any = None) -> Any:
        assert requestUrl(input) == "https://platform.claude.com/v1/oauth/token"
        assert init["method"] == "POST"
        body = getJsonBody(init)
        assert body["grant_type"] == "authorization_code"
        assert body["code"] == "manual-code"
        assert body["redirect_uri"] == "http://localhost:53692/callback"
        return jsonResponse({"access_token": "access-token", "refresh_token": "refresh-token", "expires_in": 3600})

    fetchMock_ = stubFetch(monkeypatch, anthropic, fetchMock)

    def notify(event: Any) -> None:
        if event.type == "auth_url":
            sink["authUrl"] = event.url

    async def prompt(request: Any) -> str:
        if request.type == "select":
            return "browser"
        if request.type != "manual_code":
            raise RuntimeError(f"Unexpected prompt: {request.type}")
        url = URL(sink["authUrl"])
        state = url.searchParams.get("state")
        redirectUri = url.searchParams.get("redirect_uri")
        if not state or not redirectUri:
            raise RuntimeError("Missing OAuth state or redirect_uri in auth URL")
        return f"{redirectUri}?code=manual-code&state={state}"

    credentials = await anthropicOAuth.login(interaction(prompt=prompt, notify=notify))

    assert credentials.access == "access-token"
    assert credentials.refresh == "refresh-token"
    assert len(fetchMock_.calls) == 1


async def test_offers_browser_login_first_and_uses_the_selected_anthropic_copy_code_flow(monkeypatch):
    selectPrompts: list[dict[str, Any]] = []
    sink: dict[str, str] = {}

    def fetchMock(input: Any, init: Any = None) -> Any:
        assert requestUrl(input) == "https://platform.claude.com/v1/oauth/token"
        body = getJsonBody(init)
        assert body["grant_type"] == "authorization_code"
        assert body["code"] == "copied-code"
        assert body["state"] == URL(sink["authUrl"]).searchParams.get("state")
        assert body["redirect_uri"] == "https://platform.claude.com/oauth/code/callback"
        return jsonResponse({"access_token": "access-token", "refresh_token": "refresh-token", "expires_in": 3600})

    fetchMock_ = stubFetch(monkeypatch, anthropic, fetchMock)

    def notify(event: Any) -> None:
        if event.type == "auth_url":
            sink["authUrl"] = event.url

    async def prompt(request: Any) -> str:
        if request.type == "select":
            selectPrompts.append(request.model_dump(exclude_none=True))
            return "copy_code"
        if request.type != "manual_code":
            raise RuntimeError(f"Unexpected prompt: {request.type}")
        return f"copied-code#{URL(sink['authUrl']).searchParams.get('state')}"

    credentials = await anthropicOAuth.login(interaction(prompt=prompt, notify=notify))

    assert credentials.access == "access-token"
    assert credentials.refresh == "refresh-token"
    assert URL(sink["authUrl"]).searchParams.get("redirect_uri") == "https://platform.claude.com/oauth/code/callback"
    assert len(fetchMock_.calls) == 1
    assert selectPrompts == [
        {
            "type": "select",
            "message": "Select Anthropic login method:",
            "options": [
                {"id": "browser", "label": "Browser login (default)"},
                {"id": "copy_code", "label": "Copy code login (headless)"},
            ],
        },
    ]


async def test_cancels_when_anthropic_login_method_selection_is_cancelled():
    async def prompt(_request: Any) -> str:
        raise RuntimeError("Login cancelled")

    with pytest.raises(RuntimeError, match="Login cancelled"):
        await anthropicOAuth.login(interaction(prompt=prompt))


async def test_omits_scope_from_refresh_token_requests(monkeypatch):
    def fetchMock(input: Any, init: Any = None) -> Any:
        assert requestUrl(input) == "https://platform.claude.com/v1/oauth/token"
        assert init["method"] == "POST"
        body = getJsonBody(init)
        assert body["grant_type"] == "refresh_token"
        assert body["client_id"]
        assert body["refresh_token"] == "refresh-token"
        assert "scope" not in body
        return jsonResponse({"access_token": "new-access-token", "refresh_token": "new-refresh-token", "expires_in": 3600})

    fetchMock_ = stubFetch(monkeypatch, anthropic, fetchMock)

    credentials = await anthropicOAuth.refresh(
        OAuthCredential(type="oauth", access="old-access-token", refresh="refresh-token", expires=0),
        AbortController().signal,
    )

    assert credentials.access == "new-access-token"
    assert credentials.refresh == "new-refresh-token"
    assert len(fetchMock_.calls) == 1


async def test_anthropic_oauth_login_resolves_through_the_manual_code_prompt_and_aborts_it_after_settling(monkeypatch):
    if not _portFree(53692):
        pytest.skip("port 53692 is unavailable here")

    def fetchMock(input: Any, init: Any = None) -> Any:
        url = requestUrl(input)
        if "/oauth/token" in url:
            return jsonResponse({"access_token": "access", "refresh_token": "refresh", "expires_in": 3600})
        raise RuntimeError(f"Unexpected fetch: {url}")

    stubFetch(monkeypatch, anthropic, fetchMock)

    events: list[Any] = []
    prompts: list[Any] = []
    manual: dict[str, Any] = {}

    async def prompt(request: Any) -> str:
        prompts.append(request)
        if request.type == "select":
            return "browser"
        if request.type == "manual_code":
            manual["signal"] = request.signal
            return "the-code"
        raise RuntimeError(f"Unexpected prompt: {request.type}")

    credential = await anthropicOAuth.login(interaction(prompt=prompt, notify=events.append))

    assert credential.type == "oauth"
    assert credential.access == "access"
    assert any(event.type == "auth_url" for event in events)
    assert any(request.type == "manual_code" for request in prompts)
    # the prompt's signal is aborted once login settles, so UIs can dismiss it
    assert manual["signal"].aborted is True


async def loginThroughBrowserCallback(monkeypatch) -> dict[str, Any]:
    exchanged: dict[str, str] = {}

    def fetchMock(input: Any, init: Any = None) -> Any:
        assert requestUrl(input) == "https://platform.claude.com/v1/oauth/token"
        body = getJsonBody(init)
        exchanged["code"] = body["code"]
        exchanged["redirectUri"] = body["redirect_uri"]
        return jsonResponse({"access_token": "access", "refresh_token": "refresh", "expires_in": 3600})

    stubFetch(monkeypatch, anthropic, fetchMock)

    sink: dict[str, Any] = {}

    def notify(event: Any) -> None:
        if event.type != "auth_url":
            return
        params = URL(event.url).searchParams
        redirectUri = params.get("redirect_uri") or ""
        sink["redirectUri"] = redirectUri
        callbackUrl = URL(redirectUri.replace("//localhost:", "//127.0.0.1:"))
        callbackUrl.search = URLSearchParams({"code": "browser-code", "state": params.get("state") or ""}).toString()

        async def get() -> httpx.Response:
            async with httpx.AsyncClient(trust_env=False) as client:
                return await client.get(callbackUrl.toString())

        sink["callbackPage"] = asyncio.ensure_future(get())

    async def prompt(request: Any) -> str:
        if request.type == "select":
            return "browser"
        await request.signal.wait()
        raise RuntimeError("aborted")

    credential = await anthropicOAuth.login(interaction(prompt=prompt, notify=notify))
    page = await sink["callbackPage"]
    return {
        "credential": credential,
        "redirectUri": sink["redirectUri"],
        "exchangedCode": exchanged.get("code"),
        "exchangedRedirectUri": exchanged.get("redirectUri"),
        "pageStatus": page.status_code,
        "pageText": page.text,
    }


async def test_completes_login_through_the_browser_callback_and_shows_the_sign_in_page(monkeypatch):
    login = await loginThroughBrowserCallback(monkeypatch)

    assert login["credential"].access == "access"
    assert login["exchangedCode"] == "browser-code"
    assert login["exchangedRedirectUri"] == login["redirectUri"]
    assert login["pageStatus"] == 200
    assert "Signed in to Anthropic." in login["pageText"]


# #10571
async def test_falls_back_to_a_free_callback_port_when_the_preferred_port_cannot_be_bound(monkeypatch):
    blocker = await asyncio.start_server(lambda _reader, writer: writer.close(), "127.0.0.1", 53692)
    try:
        login = await loginThroughBrowserCallback(monkeypatch)
        redirectUri = URL(login["redirectUri"])

        assert redirectUri.hostname == "localhost"
        assert redirectUri.pathname == "/callback"
        assert redirectUri.port != "53692"
        assert login["credential"].access == "access"
        assert login["exchangedRedirectUri"] == login["redirectUri"]
        assert login["pageStatus"] == 200
    finally:
        blocker.close()
        await blocker.wait_closed()

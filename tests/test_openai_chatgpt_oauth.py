"""Translated from pi's ``packages/ai/test/openai-chatgpt-oauth.test.ts`` (v1.1.0) and the ``deviceId`` case of
coding-agent's ``settings-manager.test.ts``, plus MISAKA regressions. See ``tests/oauth_fetch.py`` for the ``fetch``
stand-in."""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

import pytest

from misaka.ai.auth.oauth import openai_chatgpt
from misaka.ai.auth.oauth.openai_chatgpt import openaiChatGPTOAuth
from misaka.ai.auth.types import LoginOptions, OAuthCredential
from misaka.ai.providers.openai import openaiProvider
from misaka.config import home
from misaka.core.settings_manager import SettingsManager
from misaka.utils.js import AbortController
from misaka.utils.whatwg_url import URL, URLSearchParams
from tests.oauth_fetch import interaction, jsonResponse, requestUrl, stubFetch

TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
REQUIRED_SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
DEVICE_ID = "e61bbe28-07ef-466d-8e5d-a344f94ab305"


def tokenResponse(scope: str = REQUIRED_SCOPE) -> dict[str, Any]:
    return {
        "access_token": "access-token",
        "refresh_token": "refresh-token",
        "expires_in": 3600,
        "id_token": "id-token",
        "scope": scope,
    }


@pytest.fixture
def stubTokenEndpoint(monkeypatch):
    def stub(response: Any, inspect: Any = None) -> Any:
        def fetchMock(input: Any, init: Any = None) -> Any:
            assert requestUrl(input) == TOKEN_URL
            if inspect is not None:
                inspect(URLSearchParams(str(init.get("body"))))
            return jsonResponse(response)

        return stubFetch(monkeypatch, openai_chatgpt, fetchMock)

    return stub


@pytest.fixture(autouse=True)
def _callbackPort():
    """MISAKA fork: the flow binds the fixed port 1455; a machine that holds it cannot run the login cases."""
    import socket

    probe = socket.socket()
    # As asyncio.start_server binds: a callback answered a moment ago leaves the port in TIME_WAIT.
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", 1455))
    except OSError as error:
        pytest.skip(f"port 1455 is unavailable here: {error}")
    finally:
        probe.close()


def loginInteraction(*, callbackClientId: str | None = None, onAuthorize: Any = None) -> Any:
    authorize: dict[str, URL] = {}

    def notify(event: Any) -> None:
        if event.type != "auth_url":
            return
        authorize["url"] = URL(event.url)
        if onAuthorize is not None:
            onAuthorize(authorize["url"])

    async def prompt(request: Any) -> str:
        if request.type != "manual_code":
            raise RuntimeError(f"Unexpected prompt: {request.type}")
        if "url" not in authorize:
            raise RuntimeError("Authorization URL was not emitted before the callback prompt")
        authorizeUrl = authorize["url"]
        callback = URL(authorizeUrl.searchParams.get("redirect_uri") or "")
        callback.searchParams.set("code", "authorization-code")
        callback.searchParams.set("state", authorizeUrl.searchParams.get("state") or "")
        if callbackClientId:
            callback.searchParams.set("client_id", callbackClientId)
        return callback.toString()

    return interaction(prompt=prompt, notify=notify)


def connectedCredential() -> OAuthCredential:
    return OAuthCredential.model_validate(
        {
            "type": "oauth",
            "access": "old-access",
            "refresh": "old-refresh",
            "expires": 0,
            "clientId": "oaiapp_existing",
            "scopes": REQUIRED_SCOPE.split(" "),
        }
    )


async def test_registers_a_user_owned_client_and_stores_its_issued_id_and_granted_scopes(stubTokenEndpoint):
    seen: dict[str, Any] = {}
    stubTokenEndpoint(tokenResponse(), lambda body: seen.update(exchangeBody=body))

    credential = await openaiChatGPTOAuth.login(
        loginInteraction(callbackClientId="oaiapp_issued", onAuthorize=lambda url: seen.update(authorizeUrl=url)),
        LoginOptions(getDeviceId=lambda: DEVICE_ID),
    )

    authorizeUrl, exchangeBody = seen["authorizeUrl"], seen["exchangeBody"]
    assert authorizeUrl.searchParams.get("client_id") == "dynamic_agent_client"
    assert authorizeUrl.searchParams.get("agent_name_hint") == "Pi"
    assert authorizeUrl.searchParams.get("ext_agent_host_id") == f"urn:uuid:{DEVICE_ID}"
    assert authorizeUrl.searchParams.get("scope") == REQUIRED_SCOPE
    assert authorizeUrl.searchParams.get("redirect_uri") == "http://127.0.0.1:1455/auth/callback"
    assert authorizeUrl.searchParams.get("resource") == "https://api.openai.com/v1"
    assert authorizeUrl.searchParams.get("code_challenge_method") == "S256"
    assert exchangeBody.get("client_id") == "oaiapp_issued"
    assert exchangeBody.get("code") == "authorization-code"
    assert exchangeBody.get("resource") == "https://api.openai.com/v1"
    assert exchangeBody.get("code_verifier")
    assert (credential.type, credential.access, credential.refresh, credential.clientId, credential.scopes) == (
        "oauth",
        "access-token",
        "refresh-token",
        "oaiapp_issued",
        REQUIRED_SCOPE.split(" "),
    )


async def test_uses_the_apps_agent_name_as_the_name_hint(stubTokenEndpoint):
    seen: dict[str, Any] = {}
    stubTokenEndpoint(tokenResponse())

    await openaiChatGPTOAuth.login(
        loginInteraction(callbackClientId="oaiapp_issued", onAuthorize=lambda url: seen.update(authorizeUrl=url)),
        LoginOptions(getDeviceId=lambda: DEVICE_ID, agentName="my-app"),
    )

    assert seen["authorizeUrl"].searchParams.get("agent_name_hint") == "my-app"


async def test_rejects_registration_without_an_issued_client_id(stubTokenEndpoint):
    fetchMock = stubTokenEndpoint(tokenResponse())

    with pytest.raises(RuntimeError, match="registration callback did not contain an issued client ID"):
        await openaiChatGPTOAuth.login(loginInteraction(), LoginOptions(getDeviceId=lambda: DEVICE_ID))
    assert fetchMock.calls == []


async def test_rejects_a_token_response_that_did_not_grant_direct_token_use(stubTokenEndpoint):
    stubTokenEndpoint(tokenResponse("openid profile email offline_access resource.invoke"))

    with pytest.raises(RuntimeError, match="grant did not include chatgpt.tokens.use.direct"):
        await openaiChatGPTOAuth.login(
            loginInteraction(callbackClientId="oaiapp_issued"), LoginOptions(getDeviceId=lambda: DEVICE_ID)
        )


async def test_requires_a_device_id_before_starting_authorization():
    started: list[bool] = []
    login = loginInteraction(onAuthorize=lambda _url: started.append(True))

    with pytest.raises(RuntimeError, match="requires a device ID"):
        await openaiChatGPTOAuth.login(login)
    with pytest.raises(RuntimeError, match="requires a device ID"):
        await openaiChatGPTOAuth.login(login, LoginOptions(getDeviceId=lambda: "not-a-uuid"))
    assert started == []


async def test_requires_refresh_responses_to_rotate_the_refresh_token(stubTokenEndpoint):
    responseWithoutRefresh = {key: value for key, value in tokenResponse().items() if key != "refresh_token"}
    stubTokenEndpoint(responseWithoutRefresh)

    with pytest.raises(RuntimeError, match="token response has invalid refresh_token"):
        await openaiChatGPTOAuth.refresh(connectedCredential(), AbortController().signal)


async def test_refreshes_with_the_credentials_issued_client_id_and_stores_replacement_scopes(stubTokenEndpoint):
    seen: dict[str, Any] = {}
    stubTokenEndpoint(
        {**tokenResponse(), "access_token": "new-access", "refresh_token": "new-refresh"},
        lambda body: seen.update(refreshBody=body),
    )

    before = time.time() * 1000
    credential = await openaiChatGPTOAuth.refresh(connectedCredential(), AbortController().signal)

    # expires_in is 3600 seconds; the credential expires 3 minutes early so it is refreshed in time.
    assert credential.expires >= before + (3600 - 180) * 1000 - 1
    assert credential.expires <= time.time() * 1000 + (3600 - 180) * 1000

    refreshBody = seen["refreshBody"]
    assert refreshBody.get("grant_type") == "refresh_token"
    assert refreshBody.get("client_id") == "oaiapp_existing"
    assert refreshBody.get("refresh_token") == "old-refresh"
    assert refreshBody.get("resource") == "https://api.openai.com/v1"
    assert refreshBody.has("scope") is False
    assert (credential.access, credential.refresh, credential.clientId, credential.scopes) == (
        "new-access",
        "new-refresh",
        "oaiapp_existing",
        REQUIRED_SCOPE.split(" "),
    )


# --- MISAKA: the provider's lazy login ------------------------------------------------------


async def test_the_lazy_provider_login_forwards_the_device_id(stubTokenEndpoint):
    stubTokenEndpoint(tokenResponse())
    seen: dict[str, Any] = {}

    credential = await openaiProvider().auth.oauth.login(
        loginInteraction(callbackClientId="oaiapp_issued", onAuthorize=lambda url: seen.update(authorizeUrl=url)),
        LoginOptions(getDeviceId=lambda: DEVICE_ID),
    )
    assert seen["authorizeUrl"].searchParams.get("ext_agent_host_id") == f"urn:uuid:{DEVICE_ID}"
    assert credential.access == "access-token"


# --- settings-manager.test.ts: deviceId -----------------------------------------------------


async def test_creates_one_global_device_id_and_reuses_it_in_later_processes(tmp_path):
    agentDir = tmp_path / "agent"
    projectDir = tmp_path / "project"
    agentDir.mkdir()
    (projectDir / home.DIR_NAME).mkdir(parents=True)
    settingsPath = agentDir / "settings.json"
    settingsPath.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    (projectDir / home.DIR_NAME / "settings.json").write_text(json.dumps({"deviceId": "project-device"}), encoding="utf-8")
    options = {"projectTrusted": True}
    first = SettingsManager.create(str(projectDir), str(agentDir), options)

    deviceId = first.getOrCreateDeviceId()
    await first.flush()

    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", deviceId)
    assert first.getOrCreateDeviceId() == deviceId
    assert SettingsManager.create(str(projectDir), str(agentDir), options).getOrCreateDeviceId() == deviceId
    assert json.loads(settingsPath.read_text(encoding="utf-8")) == {"theme": "dark", "deviceId": deviceId}


# --- MISAKA regressions ---------------------------------------------------------------------


async def test_an_aborted_token_request_is_torn_down_as_fetch_does(monkeypatch):
    started = asyncio.Event()
    cancelled: list[bool] = []

    async def fetchMock(input: Any, init: Any = None) -> Any:
        from misaka.utils.js import raceAbort

        started.set()
        try:
            await raceAbort(asyncio.Event().wait(), init["signal"])
        except BaseException:
            cancelled.append(True)
            raise

    monkeypatch.setattr(openai_chatgpt, "fetch", fetchMock)
    controller = AbortController()
    task = asyncio.ensure_future(openaiChatGPTOAuth.refresh(connectedCredential(), controller.signal))
    await asyncio.wait_for(started.wait(), 5)
    controller.abort()
    with pytest.raises(Exception):  # noqa: B017 - fetch rejects with the abort reason
        await asyncio.wait_for(task, 5)

    assert cancelled == [True]


async def test_a_fractional_expires_in_is_stored_as_whole_milliseconds(stubTokenEndpoint):
    stubTokenEndpoint({**tokenResponse(), "expires_in": 3600.5})

    credential = await openaiChatGPTOAuth.refresh(connectedCredential(), AbortController().signal)

    assert isinstance(credential.expires, int)


async def test_a_busy_callback_port_fails_the_login_with_a_clear_message():
    """AI-OAI-5: without the callback server the browser's redirect would reach whatever holds the port."""
    blocker = await asyncio.start_server(lambda _reader, writer: writer.close(), "127.0.0.1", 1455)
    try:
        with pytest.raises(RuntimeError, match="Port 1455 is in use, probably by an unfinished login in another misaka"):
            await openaiChatGPTOAuth.login(loginInteraction(), LoginOptions(getDeviceId=lambda: DEVICE_ID))
    finally:
        blocker.close()
        await blocker.wait_closed()


async def _browserCallback(authorizeUrl: URL, clientId: str = "oaiapp_issued") -> tuple[int, str]:
    """MISAKA regression: what the browser is shown when it follows the redirect to the loopback server."""
    callback = URL(authorizeUrl.searchParams.get("redirect_uri") or "")
    callback.searchParams.set("code", "authorization-code")
    callback.searchParams.set("state", authorizeUrl.searchParams.get("state") or "")
    callback.searchParams.set("client_id", clientId)
    reader, writer = await asyncio.open_connection("127.0.0.1", 1455)
    writer.write(f"GET {callback.pathname}{callback.search} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
    await writer.drain()
    raw = (await reader.read()).decode()
    writer.close()
    return int(raw.split()[1]), raw


async def _loginThroughTheBrowser() -> tuple[tuple[int, str], BaseException | None]:
    seen: dict[str, URL] = {}

    async def neverPasted(_request: Any) -> str:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    login = asyncio.ensure_future(openaiChatGPTOAuth.login(
        interaction(prompt=neverPasted, notify=lambda event: event.type == "auth_url" and seen.update(url=URL(event.url))),
        LoginOptions(getDeviceId=lambda: DEVICE_ID)))
    while "url" not in seen:
        await asyncio.sleep(0.01)
    page = await asyncio.wait_for(_browserCallback(seen["url"]), 10)
    try:
        await login
    except Exception as error:  # noqa: BLE001 - the failing case is the point
        return page, error
    return page, None


async def test_the_browser_is_told_completed_only_after_the_code_is_exchanged(monkeypatch):
    """A refused code used to sit under "ChatGPT authentication completed": the page went out before the exchange."""
    stubFetch(monkeypatch, openai_chatgpt, lambda *_args: jsonResponse({"error": "invalid_grant"}, status=400))

    (status, page), error = await _loginThroughTheBrowser()

    assert isinstance(error, RuntimeError) and "invalid_grant" in str(error)
    assert status == 502 and "sign-in failed" in page and "completed" not in page


async def test_an_exchanged_code_shows_the_completed_page(stubTokenEndpoint):
    stubTokenEndpoint(tokenResponse())

    (status, page), error = await _loginThroughTheBrowser()

    assert error is None
    assert status == 200 and "ChatGPT authentication completed" in page

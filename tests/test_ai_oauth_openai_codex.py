"""Translated from pi's ``packages/ai/test/openai-codex-oauth.test.ts`` (v1.1.0). See ``tests/oauth_fetch.py`` for the
``fetch`` and fake-timer stand-ins."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest

from misaka.ai.auth.oauth import openai_codex
from misaka.ai.auth.oauth.openai_codex import openaiCodexOAuth
from misaka.ai.auth.types import LoginOptions, OAuthCredential
from misaka.utils.js import AbortController
from misaka.utils.whatwg_url import URL, URLSearchParams
from tests.oauth_fetch import (
    epochMs,
    interaction,
    jsonResponse,
    requestUrl,
    stubFetch,
    textResponse,
    useFakeTimers,
)


def createAccessToken(accountId: str) -> str:
    header = base64.b64encode(json.dumps({"alg": "none"}).encode()).decode()
    payload = base64.b64encode(
        json.dumps({"https://api.openai.com/auth": {"chatgpt_account_id": accountId}}).encode()
    ).decode()
    return f"{header}.{payload}.signature"


def deviceAuthPendingResponse() -> Any:
    return jsonResponse(
        {
            "error": {
                "message": "Device authorization is pending. Please try again.",
                "type": "invalid_request_error",
                "param": None,
                "code": "deviceauth_authorization_pending",
            },
        },
        403,
    )


def loginOpenAICodexDeviceCodeForTest(onDeviceCode: Any, signal: Any = None) -> asyncio.Future[OAuthCredential]:
    async def prompt(request: Any) -> str:
        if request.type != "select":
            raise RuntimeError(f"Unexpected prompt: {request.type}")
        return "device_code"

    def notify(event: Any) -> None:
        if event.type == "device_code":
            onDeviceCode(event.model_dump(exclude={"type"}, exclude_none=True))

    return asyncio.ensure_future(openaiCodexOAuth.login(interaction(signal=signal, prompt=prompt, notify=notify)))


@pytest.fixture(autouse=True)
def _callbackHost(monkeypatch):
    monkeypatch.delenv("MISAKA_OAUTH_CALLBACK_HOST", raising=False)


async def test_logs_in_with_the_openai_codex_device_code_flow(monkeypatch):
    startTime = epochMs("2026-05-20T00:00:00Z")
    timers = useFakeTimers(monkeypatch, startTime)

    accessToken = createAccessToken("account-123")
    deviceInfos: list[dict[str, Any]] = []
    pollTimes: list[float] = []
    pollResponses = [
        deviceAuthPendingResponse(),
        jsonResponse(
            {
                "authorization_code": "oauth-code",
                "code_challenge": "device-code-challenge",
                "code_verifier": "device-code-verifier",
            }
        ),
    ]

    def fetchMock(input: Any, init: Any = None) -> Any:
        url = requestUrl(input)

        if url == "https://auth.openai.com/api/accounts/deviceauth/usercode":
            assert init["method"] == "POST"
            assert init["headers"]["Content-Type"] == "application/json"
            assert json.loads(str(init["body"])) == {"client_id": "app_EMoamEEZ73f0CkXaXp7hrann"}
            return jsonResponse({"device_auth_id": "device-auth-id", "user_code": "ABCD-1234", "interval": "5"})

        if url == "https://auth.openai.com/api/accounts/deviceauth/token":
            pollTimes.append(timers.now)
            assert init["method"] == "POST"
            assert init["headers"]["Content-Type"] == "application/json"
            assert json.loads(str(init["body"])) == {"device_auth_id": "device-auth-id", "user_code": "ABCD-1234"}
            if not pollResponses:
                raise RuntimeError("Unexpected extra device auth poll")
            return pollResponses.pop(0)

        if url == "https://auth.openai.com/oauth/token":
            assert init["method"] == "POST"
            assert init["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
            params = URLSearchParams(str(init["body"]))
            assert params.get("grant_type") == "authorization_code"
            assert params.get("client_id") == "app_EMoamEEZ73f0CkXaXp7hrann"
            assert params.get("code") == "oauth-code"
            assert params.get("redirect_uri") == "https://auth.openai.com/deviceauth/callback"
            assert params.get("code_verifier") == "device-code-verifier"
            return jsonResponse({"access_token": accessToken, "refresh_token": "refresh-token", "expires_in": 3600})

        raise RuntimeError(f"Unexpected fetch URL: {url}")

    stubFetch(monkeypatch, openai_codex, fetchMock)

    credentialsPromise = loginOpenAICodexDeviceCodeForTest(deviceInfos.append)

    for _ in range(5):
        if pollTimes:
            break
        await timers.advanceAsync(0)
    assert deviceInfos == [
        {
            "userCode": "ABCD-1234",
            "verificationUri": "https://auth.openai.com/codex/device",
            "intervalSeconds": 5,
            "expiresInSeconds": 900,
        },
    ]
    assert pollTimes == [startTime]

    await timers.advanceAsync(4999)
    assert pollTimes == [startTime]

    await timers.advanceAsync(1)
    credentials = await credentialsPromise
    assert (credentials.access, credentials.refresh, credentials.expires, credentials.accountId) == (
        accessToken,
        "refresh-token",
        startTime + 5000 + 3600 * 1000,
        "account-123",
    )
    assert pollTimes == [startTime, startTime + 5000]


async def test_offers_browser_login_first_and_uses_the_selected_openai_codex_device_code_flow(monkeypatch):
    accessToken = createAccessToken("account-456")
    selectPrompts: list[dict[str, Any]] = []
    deviceInfos: list[dict[str, Any]] = []

    def fetchMock(input: Any, init: Any = None) -> Any:
        url = requestUrl(input)
        if url == "https://auth.openai.com/api/accounts/deviceauth/usercode":
            assert json.loads(str(init["body"])) == {"client_id": "app_EMoamEEZ73f0CkXaXp7hrann"}
            return jsonResponse({"device_auth_id": "device-auth-id", "user_code": "WXYZ-7890", "interval": "5"})
        if url == "https://auth.openai.com/api/accounts/deviceauth/token":
            return jsonResponse(
                {
                    "authorization_code": "oauth-code",
                    "code_challenge": "device-code-challenge",
                    "code_verifier": "device-code-verifier",
                }
            )
        if url == "https://auth.openai.com/oauth/token":
            return jsonResponse({"access_token": accessToken, "refresh_token": "refresh-token", "expires_in": 3600})
        raise RuntimeError(f"Unexpected fetch URL: {url}")

    stubFetch(monkeypatch, openai_codex, fetchMock)

    async def prompt(request: Any) -> str:
        if request.type != "select":
            raise RuntimeError("Text prompt should not be used")
        selectPrompts.append(request.model_dump(exclude_none=True))
        return "device_code"

    def notify(event: Any) -> None:
        if event.type == "auth_url":
            raise RuntimeError("Browser login should not start")
        if event.type == "device_code":
            deviceInfos.append(event.model_dump(exclude={"type"}, exclude_none=True))

    credential = await openaiCodexOAuth.login(interaction(prompt=prompt, notify=notify))
    assert (credential.type, credential.access, credential.refresh, credential.accountId) == (
        "oauth",
        accessToken,
        "refresh-token",
        "account-456",
    )

    assert selectPrompts == [
        {
            "type": "select",
            "message": "Select OpenAI Codex login method:",
            "options": [
                {"id": "browser", "label": "Browser login (default)"},
                {"id": "device_code", "label": "Device code login (headless)"},
            ],
        },
    ]
    assert deviceInfos == [
        {
            "userCode": "WXYZ-7890",
            "verificationUri": "https://auth.openai.com/codex/device",
            "intervalSeconds": 5,
            "expiresInSeconds": 900,
        },
    ]


async def test_cancels_when_openai_codex_login_method_selection_is_cancelled():
    async def prompt(_request: Any) -> str:
        raise RuntimeError("Login cancelled")

    with pytest.raises(RuntimeError, match="Login cancelled"):
        await openaiCodexOAuth.login(interaction(prompt=prompt))


async def test_cancels_the_openai_codex_device_code_flow_while_waiting(monkeypatch):
    timers = useFakeTimers(monkeypatch)
    controller = AbortController()
    pollTimes: list[float] = []

    def fetchMock(input: Any, init: Any = None) -> Any:
        url = requestUrl(input)
        if url == "https://auth.openai.com/api/accounts/deviceauth/usercode":
            assert json.loads(str(init["body"])) == {"client_id": "app_EMoamEEZ73f0CkXaXp7hrann"}
            return jsonResponse({"device_auth_id": "device-auth-id", "user_code": "ABCD-1234", "interval": "5"})
        if url == "https://auth.openai.com/api/accounts/deviceauth/token":
            pollTimes.append(timers.now)
            return deviceAuthPendingResponse()
        raise RuntimeError(f"Unexpected fetch URL: {url}")

    stubFetch(monkeypatch, openai_codex, fetchMock)

    credentialsPromise = loginOpenAICodexDeviceCodeForTest(lambda _info: None, signal=controller.signal)

    for _ in range(5):
        if pollTimes:
            break
        await timers.advanceAsync(0)
    assert len(pollTimes) == 1

    controller.abort()
    with pytest.raises(RuntimeError) as rejection:
        await credentialsPromise
    assert str(rejection.value) == "Login cancelled"


async def test_times_out_the_openai_codex_device_code_flow_after_15_minutes(monkeypatch):
    timers = useFakeTimers(monkeypatch)
    pollTimes: list[float] = []

    def fetchMock(input: Any, init: Any = None) -> Any:
        url = requestUrl(input)
        if url == "https://auth.openai.com/api/accounts/deviceauth/usercode":
            assert json.loads(str(init["body"])) == {"client_id": "app_EMoamEEZ73f0CkXaXp7hrann"}
            return jsonResponse({"device_auth_id": "device-auth-id", "user_code": "ABCD-1234", "interval": "60"})
        if url == "https://auth.openai.com/api/accounts/deviceauth/token":
            pollTimes.append(timers.now)
            return deviceAuthPendingResponse()
        raise RuntimeError(f"Unexpected fetch URL: {url}")

    stubFetch(monkeypatch, openai_codex, fetchMock)

    credentialsPromise = loginOpenAICodexDeviceCodeForTest(lambda _info: None)

    for _ in range(5):
        if pollTimes:
            break
        await timers.advanceAsync(0)
    assert len(pollTimes) == 1

    await timers.advanceAsync(15 * 60 * 1000)
    with pytest.raises(RuntimeError) as rejection:
        await credentialsPromise
    assert str(rejection.value) == "Device flow timed out"


async def test_treats_openai_codex_device_auth_403_and_404_responses_as_pending(monkeypatch):
    timers = useFakeTimers(monkeypatch)
    accessToken = createAccessToken("account-403-404")
    pollTimes: list[float] = []
    pollResponses = [
        jsonResponse({"error": "access_denied", "error_description": "denied"}, 403),
        textResponse("not ready", 404, {"Content-Type": "text/plain"}),
        jsonResponse(
            {
                "authorization_code": "oauth-code",
                "code_challenge": "device-code-challenge",
                "code_verifier": "device-code-verifier",
            }
        ),
    ]

    def fetchMock(input: Any, init: Any = None) -> Any:
        url = requestUrl(input)
        if url == "https://auth.openai.com/api/accounts/deviceauth/usercode":
            return jsonResponse({"device_auth_id": "device-auth-id", "user_code": "ABCD-1234", "interval": "1"})
        if url == "https://auth.openai.com/api/accounts/deviceauth/token":
            pollTimes.append(timers.now)
            if not pollResponses:
                raise RuntimeError("Unexpected extra device auth poll")
            return pollResponses.pop(0)
        if url == "https://auth.openai.com/oauth/token":
            return jsonResponse({"access_token": accessToken, "refresh_token": "refresh-token", "expires_in": 3600})
        raise RuntimeError(f"Unexpected fetch URL: {url}")

    stubFetch(monkeypatch, openai_codex, fetchMock)

    credentialsPromise = loginOpenAICodexDeviceCodeForTest(lambda _info: None)

    for _ in range(5):
        if pollTimes:
            break
        await timers.advanceAsync(0)
    await timers.advanceAsync(1000)
    await timers.advanceAsync(1000)

    credentials = await credentialsPromise
    assert (credentials.access, credentials.refresh, credentials.accountId) == (
        accessToken,
        "refresh-token",
        "account-403-404",
    )
    assert len(pollTimes) == 3


async def test_includes_the_response_body_in_openai_codex_device_auth_poll_failures(monkeypatch):
    def fetchMock(input: Any, init: Any = None) -> Any:
        url = requestUrl(input)
        if url == "https://auth.openai.com/api/accounts/deviceauth/usercode":
            return jsonResponse({"device_auth_id": "device-auth-id", "user_code": "ABCD-1234", "interval": "5"})
        if url == "https://auth.openai.com/api/accounts/deviceauth/token":
            return jsonResponse({"error": "server_error", "error_description": "try again later"}, 500)
        raise RuntimeError(f"Unexpected fetch URL: {url}")

    stubFetch(monkeypatch, openai_codex, fetchMock)

    with pytest.raises(RuntimeError) as rejection:
        await loginOpenAICodexDeviceCodeForTest(lambda _info: None)
    assert (
        'OpenAI Codex device auth failed with status 500: {"error":"server_error","error_description":"try again later"}'
        in str(rejection.value)
    )


async def test_does_not_write_token_refresh_failures_to_stderr(monkeypatch, capsys):
    from misaka.utils.whatwg_fetch import Response

    stubFetch(
        monkeypatch,
        openai_codex,
        lambda *_: Response(
            json.dumps(
                {
                    "error": {
                        "message": "Could not validate your token. Please try signing in again.",
                        "type": "invalid_request_error",
                    }
                }
            ),
            status=401,
            statusText="Unauthorized",
            headers={"Content-Type": "application/json"},
        ),
    )

    with pytest.raises(RuntimeError, match=r"OpenAI Codex token refresh failed \(401\).*Could not validate your token"):
        await openaiCodexOAuth.refresh(
            OAuthCredential(type="oauth", access="invalid-access-token", refresh="invalid-refresh-token", expires=0),
            AbortController().signal,
        )
    assert capsys.readouterr().err == ""


def _pasteCallback(sink: dict[str, str]) -> Any:
    async def prompt(request: Any) -> str:
        if request.type == "select":
            return "browser"
        if request.type != "manual_code":
            raise RuntimeError(f"Unexpected prompt: {request.type}")
        state = URL(sink["authUrl"]).searchParams.get("state")
        return f"http://localhost:1455/auth/callback?code=pasted-code&state={state}"

    return prompt


async def test_uses_the_apps_agent_name_as_the_browser_login_originator(monkeypatch):
    stubFetch(
        monkeypatch,
        openai_codex,
        lambda *_: jsonResponse(
            {"access_token": createAccessToken("acct"), "refresh_token": "refresh", "expires_in": 3600}
        ),
    )

    sink: dict[str, str] = {}

    def notify(event: Any) -> None:
        if event.type == "auth_url":
            sink["authUrl"] = event.url

    await openaiCodexOAuth.login(interaction(prompt=_pasteCallback(sink), notify=notify), LoginOptions(agentName="my-app"))

    assert URL(sink["authUrl"]).searchParams.get("originator") == "my-app"


async def test_falls_back_to_the_pasted_redirect_url_when_the_fixed_callback_port_is_taken(monkeypatch):
    # Port 1455 is registered with OpenAI; the Codex CLI may hold it. Occupy it unless it already is.
    try:
        blocker = await asyncio.start_server(lambda _reader, writer: writer.close(), "127.0.0.1", 1455)
    except OSError:
        blocker = None
    try:
        exchangeBody: dict[str, URLSearchParams] = {}

        def fetchMock(input: Any, init: Any = None) -> Any:
            assert requestUrl(input) == "https://auth.openai.com/oauth/token"
            exchangeBody["params"] = URLSearchParams(str(init["body"]))
            return jsonResponse(
                {"access_token": createAccessToken("acct"), "refresh_token": "refresh", "expires_in": 3600}
            )

        stubFetch(monkeypatch, openai_codex, fetchMock)

        sink: dict[str, str] = {}

        def notify(event: Any) -> None:
            if event.type == "auth_url":
                sink["authUrl"] = event.url

        credential = await openaiCodexOAuth.login(interaction(prompt=_pasteCallback(sink), notify=notify))

        assert credential.accountId == "acct"
        assert exchangeBody["params"].get("code") == "pasted-code"
        assert exchangeBody["params"].get("redirect_uri") == "http://localhost:1455/auth/callback"
    finally:
        if blocker is not None:
            blocker.close()
            await blocker.wait_closed()

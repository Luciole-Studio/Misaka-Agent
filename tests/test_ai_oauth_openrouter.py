"""Translated from pi's ``packages/ai/test/openrouter-oauth.test.ts`` (v1.1.0). See ``tests/oauth_fetch.py`` for the
``fetch`` stand-in; the browser's request to the loopback callback goes out over httpx. The provider-level cases ("is
exposed alongside API-key auth", "resolves the same stored OAuth key for chat and image models") are in
``tests/test_openrouter_images.py``."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from typing import Any

import httpx
import pytest

from misaka.ai.auth.oauth import openrouter
from misaka.ai.auth.oauth.openrouter import openRouterOAuth
from misaka.utils.js import AbortController
from misaka.utils.whatwg_url import URL
from tests.oauth_fetch import interaction, jsonResponse, requestUrl, stubFetch

TOKEN_URL = "https://openrouter.ai/api/v1/auth/keys"
MAX_SAFE_INTEGER = 9007199254740991


def base64url(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii").replace("+", "-").replace("/", "_").replace("=", "")


def nativeFetch(url: Any) -> asyncio.Future[httpx.Response]:
    """The browser following the redirect to the loopback server."""

    async def get() -> httpx.Response:
        async with httpx.AsyncClient(trust_env=False) as client:
            return await client.get(str(url))

    return asyncio.ensure_future(get())


async def never(prompt: Any) -> str:
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


def recordingPrompt(record: dict[str, Any]) -> Any:
    async def prompt(request: Any) -> str:
        record["signal"] = request.signal
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    return prompt


@pytest.fixture(autouse=True)
def _callbackHost(monkeypatch):
    monkeypatch.delenv("MISAKA_OAUTH_CALLBACK_HOST", raising=False)


async def test_runs_pkce_on_a_one_shot_loopback_callback_and_exchanges_the_code_for_a_permanent_api_key(monkeypatch):
    exchangeBody: dict[str, Any] = {}

    def fetchMock(input: Any, init: Any = None) -> Any:
        assert requestUrl(input) == TOKEN_URL
        exchangeBody.update(json.loads(str(init["body"])))
        return jsonResponse({"key": "sk-or-test"})

    fetchMock_ = stubFetch(monkeypatch, openrouter, fetchMock)

    authorize: dict[str, Any] = {}
    manual: dict[str, Any] = {}

    def notify(event: Any) -> None:
        if event.type != "auth_url":
            return
        authorizeUrl = URL(event.url)
        authorize["url"] = authorizeUrl
        callbackUrl = URL(authorizeUrl.searchParams.get("callback_url") or "")
        callbackUrl.searchParams.set("code", "authorization-code")
        authorize["callbackResponse"] = nativeFetch(callbackUrl)

    credential = await openRouterOAuth.login(interaction(prompt=recordingPrompt(manual), notify=notify))

    assert credential.model_dump() == {
        "type": "oauth",
        "access": "sk-or-test",
        "refresh": "",
        "expires": MAX_SAFE_INTEGER,
    }
    assert (await authorize["callbackResponse"]).status_code == 200
    assert manual["signal"].aborted is True
    authorizeUrl = authorize["url"]
    assert authorizeUrl.origin == "https://openrouter.ai"
    assert authorizeUrl.pathname == "/auth"
    assert authorizeUrl.searchParams.get("code_challenge_method") == "S256"

    callbackUrl = URL(authorizeUrl.searchParams.get("callback_url") or "")
    assert callbackUrl.hostname == "127.0.0.1"
    assert re.fullmatch(r"/oauth/callback/[0-9a-f-]+", callbackUrl.pathname)

    assert exchangeBody["code"] == "authorization-code"
    assert exchangeBody["code_challenge_method"] == "S256"
    verifier = exchangeBody.get("code_verifier")
    assert isinstance(verifier, str)
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    assert authorizeUrl.searchParams.get("code_challenge") == base64url(digest)
    assert len(fetchMock_.calls) == 1


def _hitCallback(code: str, sink: dict[str, Any]) -> Any:
    def notify(event: Any) -> None:
        if event.type != "auth_url":
            return
        callbackUrl = URL(URL(event.url).searchParams.get("callback_url") or "")
        callbackUrl.searchParams.set("code", code)
        sink["callbackUrl"] = callbackUrl
        sink["callbackResponse"] = nativeFetch(callbackUrl)

    return notify


async def test_reports_token_exchange_failures_through_both_the_callback_page_and_login(monkeypatch):
    stubFetch(monkeypatch, openrouter, lambda *_: jsonResponse({"error": {"message": "invalid code"}}, 403))

    sink: dict[str, Any] = {}
    login = openRouterOAuth.login(interaction(prompt=never, notify=_hitCallback("bad-code", sink)))

    with pytest.raises(RuntimeError, match=r"OpenRouter OAuth key exchange failed \(HTTP 403\): invalid code"):
        await login
    assert (await sink["callbackResponse"]).status_code == 502


async def test_allows_only_one_token_exchange_for_a_callback(monkeypatch):
    exchange: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

    async def fetchMock(*_: Any) -> Any:
        return await exchange

    fetchMock_ = stubFetch(monkeypatch, openrouter, fetchMock)

    sink: dict[str, Any] = {}
    login = asyncio.ensure_future(
        openRouterOAuth.login(interaction(prompt=never, notify=_hitCallback("authorization-code", sink)))
    )

    for _ in range(500):
        if len(fetchMock_.calls) == 1:
            break
        await asyncio.sleep(0.01)
    assert len(fetchMock_.calls) == 1
    assert (await nativeFetch(sink["callbackUrl"])).status_code == 409
    assert len(fetchMock_.calls) == 1
    exchange.set_result(jsonResponse({"key": "sk-or-test"}))

    assert (await login).access == "sk-or-test"
    assert (await sink["callbackResponse"]).status_code == 200


async def test_rejects_a_successful_response_that_does_not_contain_a_key(monkeypatch):
    stubFetch(monkeypatch, openrouter, lambda *_: jsonResponse({"user_id": "user-1"}))

    sink: dict[str, Any] = {}
    login = openRouterOAuth.login(interaction(prompt=never, notify=_hitCallback("code-without-key", sink)))

    with pytest.raises(RuntimeError, match='OpenRouter OAuth response carries no "key"'):
        await login
    assert (await sink["callbackResponse"]).status_code == 502


async def test_mints_a_key_from_a_pasted_redirect_url_when_the_loopback_callback_never_arrives(monkeypatch):
    exchangeBody: dict[str, Any] = {}

    def fetchMock(input: Any, init: Any = None) -> Any:
        assert requestUrl(input) == TOKEN_URL
        exchangeBody.update(json.loads(str(init["body"])))
        return jsonResponse({"key": "sk-or-manual"})

    fetchMock_ = stubFetch(monkeypatch, openrouter, fetchMock)

    sink: dict[str, Any] = {}

    async def prompt(request: Any) -> str:
        if request.type != "manual_code":
            raise RuntimeError(f"Unexpected prompt: {request.type}")
        return f"{sink['callbackUrl']}?code=manual-code"

    def notify(event: Any) -> None:
        if event.type == "auth_url":
            sink["callbackUrl"] = URL(event.url).searchParams.get("callback_url")

    credential = await openRouterOAuth.login(interaction(prompt=prompt, notify=notify))

    assert credential.model_dump() == {
        "type": "oauth",
        "access": "sk-or-manual",
        "refresh": "",
        "expires": MAX_SAFE_INTEGER,
    }
    assert (exchangeBody["code"], exchangeBody["code_challenge_method"]) == ("manual-code", "S256")
    assert len(fetchMock_.calls) == 1


async def test_accepts_a_bare_authorization_code_from_the_manual_prompt(monkeypatch):
    exchangeBody: dict[str, Any] = {}

    def fetchMock(input: Any, init: Any = None) -> Any:
        exchangeBody.update(json.loads(str(init["body"])))
        return jsonResponse({"key": "sk-or-manual"})

    stubFetch(monkeypatch, openrouter, fetchMock)

    async def prompt(_request: Any) -> str:
        return "  manual-code  "

    credential = await openRouterOAuth.login(interaction(prompt=prompt))

    assert credential.access == "sk-or-manual"
    assert exchangeBody["code"] == "manual-code"


async def test_fails_login_when_the_manual_prompt_is_cancelled(monkeypatch):
    fetchMock = stubFetch(monkeypatch, openrouter, lambda *_: jsonResponse({"key": "sk-or-unexpected"}))

    async def prompt(_request: Any) -> str:
        raise RuntimeError("Login cancelled")

    with pytest.raises(RuntimeError, match="Login cancelled"):
        await openRouterOAuth.login(interaction(prompt=prompt))
    assert fetchMock.calls == []


async def test_rejects_empty_manual_input_without_exchanging_a_code(monkeypatch):
    fetchMock = stubFetch(monkeypatch, openrouter, lambda *_: jsonResponse({"key": "sk-or-unexpected"}))

    async def prompt(_request: Any) -> str:
        return "   "

    with pytest.raises(RuntimeError, match="Missing authorization code"):
        await openRouterOAuth.login(interaction(prompt=prompt))
    assert fetchMock.calls == []


async def test_closes_the_pending_callback_when_login_is_cancelled():
    controller = AbortController()
    sink: dict[str, Any] = {}

    def notify(event: Any) -> None:
        if event.type != "auth_url":
            return
        sink["callbackUrl"] = URL(URL(event.url).searchParams.get("callback_url") or "")
        controller.abort()

    with pytest.raises(RuntimeError, match="Login cancelled"):
        await openRouterOAuth.login(interaction(signal=controller.signal, prompt=never, notify=notify))
    assert sink.get("callbackUrl") is not None
    with pytest.raises(httpx.HTTPError):
        await nativeFetch(sink["callbackUrl"])


async def test_rejects_before_opening_a_callback_server_when_login_is_already_cancelled():
    controller = AbortController()
    controller.abort()

    async def prompt(_request: Any) -> str:
        return ""

    def notify(_event: Any) -> None:
        raise RuntimeError("Cancelled login must not emit events")

    with pytest.raises(RuntimeError, match="Login cancelled"):
        await openRouterOAuth.login(interaction(signal=controller.signal, prompt=prompt, notify=notify))


async def test_uses_the_configured_oauth_callback_host(monkeypatch):
    # MISAKA fork: the variable is MISAKA_OAUTH_CALLBACK_HOST.
    monkeypatch.setenv("MISAKA_OAUTH_CALLBACK_HOST", "localhost")
    controller = AbortController()
    sink: dict[str, Any] = {}

    def notify(event: Any) -> None:
        if event.type != "auth_url":
            return
        sink["callbackUrl"] = URL(URL(event.url).searchParams.get("callback_url") or "")
        controller.abort()

    with pytest.raises(RuntimeError, match="Login cancelled"):
        await openRouterOAuth.login(interaction(signal=controller.signal, prompt=never, notify=notify))
    assert sink["callbackUrl"].hostname == "localhost"

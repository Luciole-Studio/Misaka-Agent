"""Translated from pi's ``packages/ai/test/oauth-callback-server.test.ts`` (v1.1.0), plus MISAKA regressions."""

from __future__ import annotations

import asyncio
import errno
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
import pytest

from misaka.ai.auth.oauth.callback_server import (
    OAuthCallbackServerOptions,
    startOAuthCallbackServer,
    waitForCallbackOrManualInput,
)
from misaka.utils.js import AbortController


def callbackUrl(redirectUri: str, params: dict[str, str]) -> str:
    parts = urlsplit(redirectUri)
    return urlunsplit(parts._replace(query=urlencode(params)))


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(trust_env=False)


async def page(response: httpx.Response) -> dict:
    return {"status": response.status_code, "contentType": response.headers.get("content-type"), "body": response.text}


def interaction(prompt):
    """A `ProviderAuthInteraction` whose `prompt` is the test's."""
    return SimpleNamespace(prompt=prompt, notify=lambda _event: None, signal=AbortController().signal)


def pendingPrompt(onPrompt=None):
    """A manual prompt that stays open until its signal aborts."""

    async def prompt(request):
        if onPrompt is not None:
            onPrompt(request)
        await request.signal.wait()
        raise RuntimeError("prompt aborted")

    return prompt


async def _identity(code: str) -> str:
    return code


@pytest.fixture
async def start():
    servers = []

    async def start(**options):
        async def complete(code: str) -> str:
            return f"completed:{code}"

        server = await startOAuthCallbackServer(OAuthCallbackServerOptions(**{
            "providerName": "Example",
            "host": "127.0.0.1",
            "port": 0,
            "path": "/callback",
            "state": "expected-state",
            "complete": complete,
            **options,
        }))
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.close()


# --- OAuth callback server ------------------------------------------------------------------


async def test_ignores_stray_requests_and_resolves_with_the_completed_code(start):
    server = await start()
    assert urlsplit(server.redirectUri).hostname == "127.0.0.1"
    assert urlsplit(server.redirectUri).path == "/callback"

    async with _client() as client:
        wrongPath = await page(await client.get(server.redirectUri.replace("/callback", "/other")))
        assert wrongPath["status"] == 404
        wrongState = await page(await client.get(callbackUrl(server.redirectUri, {"code": "c", "state": "other"})))
        assert (wrongState["status"], wrongState["contentType"]) == (400, "text/html; charset=utf-8")
        assert "State mismatch." in wrongState["body"]
        post = await client.post(callbackUrl(server.redirectUri, {"code": "c", "state": "expected-state"}))
        assert post.status_code == 404
        missingCode = await page(await client.get(callbackUrl(server.redirectUri, {"state": "expected-state"})))
        assert missingCode["status"] == 400

        success = await page(await client.get(callbackUrl(server.redirectUri, {"code": "the-code", "state": "expected-state"})))
    assert (success["status"], success["contentType"]) == (200, "text/html; charset=utf-8")
    assert "Authentication successful" in success["body"]
    assert "Signed in to Example." in success["body"]
    assert 'fill="#F09082"' in success["body"]
    assert 'fill="#4D9ABF"' in success["body"]
    assert 'fill="#F1BE58"' in success["body"]
    assert await server.wait() == "completed:the-code"


async def test_uses_the_redirect_host_and_skips_the_state_check_when_none_is_expected(start):
    server = await start(redirectHost="localhost", state=None)
    assert server.redirectUri.startswith("http://localhost:")
    assert server.redirectUri.endswith("/callback")
    async with _client() as client:
        url = callbackUrl(server.redirectUri, {"code": "no-state"}).replace("localhost", "127.0.0.1")
        response = await client.get(url)
    assert response.status_code == 200
    assert await server.wait() == "completed:no-state"


async def test_shows_completion_failures_on_the_page_and_rejects_the_wait(start):
    async def complete(code: str) -> str:
        raise RuntimeError("token exchange failed")

    server = await start(complete=complete)
    async with _client() as client:
        failure = await page(await client.get(callbackUrl(server.redirectUri, {"code": "c", "state": "expected-state"})))
    assert failure["status"] == 502
    assert "Example sign-in failed." in failure["body"]
    assert "token exchange failed" in failure["body"]
    with pytest.raises(RuntimeError, match="token exchange failed"):
        await server.wait()


async def test_rejects_the_wait_when_the_provider_redirects_with_an_error(start):
    server = await start()
    async with _client() as client:
        failure = await page(await client.get(callbackUrl(server.redirectUri, {
            "error": "access_denied",
            "error_description": "User denied access",
            "state": "expected-state",
        })))
    assert failure["status"] == 400
    assert "User denied access" in failure["body"]
    with pytest.raises(RuntimeError, match="Example authorization failed: User denied access"):
        await server.wait()


async def test_completes_only_the_first_callback(start):
    finishExchange: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    started = asyncio.Event()

    async def complete(code: str) -> str:
        started.set()
        return await finishExchange

    server = await start(complete=complete)
    url = callbackUrl(server.redirectUri, {"code": "c", "state": "expected-state"})
    async with _client() as client:
        first = asyncio.ensure_future(client.get(url))
        await asyncio.wait_for(started.wait(), 5)
        second = await client.get(url)
        assert second.status_code == 409
        # A claimed callback keeps completing even when the caller switches to manual input.
        server.cancel()
        finishExchange.set_result("done")
        assert (await first).status_code == 200
    assert await server.wait() == "done"


async def test_resolves_with_none_after_cancel(start):
    server = await start()
    server.cancel()
    assert await server.wait() is None
    async with _client() as client:
        late = await client.get(callbackUrl(server.redirectUri, {"code": "c", "state": "expected-state"}))
    assert late.status_code == 409


async def test_rejects_the_wait_on_abort_and_on_timeout(start):
    controller = AbortController()
    aborted = await start(signal=controller.signal)
    controller.abort()
    with pytest.raises(RuntimeError, match="Login cancelled"):
        await asyncio.wait_for(aborted.wait(), 5)

    timedOut = await start(timeoutMs=10)
    with pytest.raises(RuntimeError, match="Example sign-in timed out"):
        await asyncio.wait_for(timedOut.wait(), 5)

    alreadyAborted = AbortController()
    alreadyAborted.abort()
    with pytest.raises(RuntimeError, match="Login cancelled"):
        await start(signal=alreadyAborted.signal)


async def test_fails_instead_of_picking_another_port_when_the_requested_port_is_taken(start):
    blocker = await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1", 0)
    try:
        port = blocker.sockets[0].getsockname()[1]
        with pytest.raises(OSError) as raised:
            await start(port=port)
        assert raised.value.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", errno.EADDRINUSE))
    finally:
        blocker.close()


async def test_close_drops_sockets_that_never_sent_a_request(start):
    # MISAKA fork: a browser preconnect holds a socket open without speaking.
    server = await start()
    port = int(urlsplit(server.redirectUri).port)
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        await asyncio.sleep(0.05)
        server.close()
        assert await asyncio.wait_for(reader.read(), 5) == b""
    finally:
        writer.close()
    with pytest.raises(RuntimeError, match="OAuth callback server closed"):
        await server.wait()


# --- waitForCallbackOrManualInput -----------------------------------------------------------


async def test_returns_the_browser_callback_and_aborts_the_manual_prompt():
    manualSignal = None

    def capture(request):
        nonlocal manualSignal
        manualSignal = request.signal

    server = await startOAuthCallbackServer(OAuthCallbackServerOptions(
        providerName="Example", host="127.0.0.1", port=0, path="/callback", complete=_identity,
    ))
    try:
        result = asyncio.ensure_future(waitForCallbackOrManualInput(
            interaction(pendingPrompt(capture)), server, {"message": "paste", "placeholder": server.redirectUri},
        ))
        async with _client() as client:
            await client.get(callbackUrl(server.redirectUri, {"code": "from-browser"}))
        assert await asyncio.wait_for(result, 5) == {"type": "callback", "value": "from-browser"}
        assert manualSignal is not None and manualSignal.aborted
    finally:
        server.close()


async def test_returns_pasted_input_and_stops_waiting_for_the_browser():
    server = await startOAuthCallbackServer(OAuthCallbackServerOptions(
        providerName="Example", host="127.0.0.1", port=0, path="/callback", complete=_identity,
    ))

    async def pasted(_request):
        return "pasted"

    try:
        result = await asyncio.wait_for(waitForCallbackOrManualInput(
            interaction(pasted), server, {"message": "paste", "placeholder": server.redirectUri},
        ), 5)
        assert result == {"type": "manual", "input": "pasted"}
    finally:
        server.close()


async def test_uses_only_the_manual_prompt_without_a_callback_server():
    async def pasted(_request):
        return "pasted"

    result = await waitForCallbackOrManualInput(
        interaction(pasted), None, {"message": "paste", "placeholder": "http://localhost/callback"},
    )
    assert result == {"type": "manual", "input": "pasted"}


async def test_propagates_manual_prompt_failures():
    server = await startOAuthCallbackServer(OAuthCallbackServerOptions(
        providerName="Example", host="127.0.0.1", port=0, path="/callback", complete=_identity,
    ))

    async def cancelled(_request):
        raise RuntimeError("prompt cancelled")

    try:
        with pytest.raises(RuntimeError, match="prompt cancelled"):
            await asyncio.wait_for(waitForCallbackOrManualInput(
                interaction(cancelled), server, {"message": "paste", "placeholder": server.redirectUri},
            ), 5)
    finally:
        server.close()


async def test_cancelling_the_login_while_a_stubborn_prompt_retires_still_cancels_it():
    # MISAKA fork: the prompt task is cancelled as well as signalled; waiting for it must not
    # swallow a cancellation of the login itself.
    server = await startOAuthCallbackServer(OAuthCallbackServerOptions(
        providerName="Example", host="127.0.0.1", port=0, path="/callback", complete=_identity,
    ))

    async def stubborn(_request):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.3)
            raise

    try:
        task = asyncio.ensure_future(waitForCallbackOrManualInput(
            interaction(stubborn), server, {"message": "paste", "placeholder": server.redirectUri},
        ))
        async with _client() as client:
            await client.get(callbackUrl(server.redirectUri, {"code": "from-browser"}))
        await asyncio.sleep(0.05)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
    finally:
        server.close()

"""pi coding-agent's ``test/suite/agent-session-mcp-oauth.test.ts``."""

from __future__ import annotations

import asyncio
import json
import socket
import tempfile
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import pytest

from misaka.ai.providers.faux import fauxAssistantMessage, fauxToolCall
from misaka.core.auth_storage import InMemoryAuthStorageBackend, LockResult
from misaka.core.extensions.runner import emitSessionShutdownEvent
from misaka.extensions.mcp import createMcpExtension
from misaka.extensions.mcp.cli import runMcpCommand
from misaka.extensions.mcp.oauth import McpOAuthCredentialStore
from misaka.utils._abort_signal import wait_for_abort
from misaka.utils.js import spawn
from misaka.utils.whatwg_fetch import fetch
from misaka.utils.whatwg_url import URL
from tests.mcp_helpers import closeServers
from tests.mcp_oauth_server import startOAuthMcpServer
from tests.suite_harness import (
    Harness,
    createHarness,
    createTestUiContext,
    getMessageText,
)


@pytest.fixture(autouse=True)
def _noProxy(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
async def stack():
    async with AsyncExitStack() as cleanups:
        yield cleanups
    await closeServers()


async def waitFor(predicate, timeout: float = 1.0) -> None:
    """``vi.waitFor``: poll until ``predicate`` holds."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def setup(stack: AsyncExitStack, browser: str, oauth: dict[str, Any] | None = None):
    server = await startOAuthMcpServer()
    backend = InMemoryAuthStorageBackend()
    entry = {
        "name": "issues",
        "config": {"url": server.url, "exposure": "direct", **({"oauth": oauth} if oauth else {})},
        "source": "test",
    }
    notifications: list[str] = []
    opened: list[URL] = []
    redirectLocation: asyncio.Future[str] | None = None
    tasks: set[asyncio.Task] = set()

    async def follow(url: str) -> None:
        await (await fetch(url)).text()

    async def location(url: str) -> str:
        response = await fetch(url, {"redirect": "manual"})
        # MISAKA fork: the unread body is dropped, which releases its connection; Node collects it instead.
        if response.body is not None:
            await response.body.cancel()
        return response.headers.get("location") or ""

    def openUrl(url: str) -> None:
        nonlocal redirectLocation
        opened.append(URL(url))
        if browser == "follow":
            # The browser follows the authorization redirect to the loopback callback.
            spawn(follow(url), tasks)
        else:
            # The browser cannot reach the callback; the user pastes the redirect URL.
            redirectLocation = spawn(location(url), tasks)

    harness: Harness = await stack.enter_async_context(createHarness(
        initialActiveToolNames=[],
        extensionFactories=[
            createMcpExtension({
                "loadConfig": lambda _ctx: {"servers": [entry], "errors": []},
                "credentials": McpOAuthCredentialStore(backend),
                "openUrl": openUrl,
            }),
        ],
    ))

    # The paste prompt waits until sign-in completes unless the user pastes the redirect URL.
    async def input(_title, _placeholder=None, opts=None):
        if browser == "paste":
            return await redirectLocation if redirectLocation is not None else None
        signal = (opts or {}).get("signal")
        if signal is not None:
            await wait_for_abort(signal)
        return None

    await harness.session.bindExtensions({
        "uiContext": createTestUiContext(notify=lambda message, *_args: notifications.append(message), input=input),
    })
    return harness, server, notifications, backend, opened


async def prompt(harness: Harness, text: str) -> None:
    await harness.session.prompt(text)
    await harness.session.waitForIdle()


async def callWhoami(harness: Harness):
    harness.setResponses([
        fauxAssistantMessage([fauxToolCall("mcp__issues__whoami", {})], {"stopReason": "toolUse"}),
        fauxAssistantMessage("done"),
    ])
    before = len(harness.session.messages)
    await prompt(harness, "who am i")
    result = next(
        (message for message in harness.session.messages[before:] if getattr(message, "role", None) == "toolResult"),
        None,
    )
    if result is None:
        raise RuntimeError("no tool result")
    return result


def storedStates(backend: InMemoryAuthStorageBackend) -> str:
    return backend.withLock(lambda current: LockResult(result=current))


def setExpiry(backend: InMemoryAuthStorageBackend, expiresAt: float) -> None:
    def update(current):
        states = json.loads(current or "{}")
        for state in states.values():
            state["tokensExpireAt"] = expiresAt
        return LockResult(result=None, next=json.dumps(states))

    backend.withLock(update)


async def test_signs_in_through_the_browser_refreshes_expired_tokens_and_signs_out(stack):
    harness, server, notifications, backend, _opened = await setup(stack, "follow")

    await prompt(harness, "/mcp")
    # Startup problems are reported once, pointing to /mcp.
    assert "MCP servers need attention:\n  issues: needs sign-in\nRun /mcp to fix." in notifications
    assert notifications[-1] == "issues: needs sign-in, run /mcp login issues (direct)"

    await prompt(harness, "/mcp login issues")
    assert notifications[-1] == 'Signed in to MCP server "issues" (1 tools).'
    assert server.log == ["401 none", "register", "token code"]
    assert '"access_token": "access-1"' in storedStates(backend)

    assert getMessageText(await callWhoami(harness)) == "token access-1"

    # An expired access token is refreshed without user interaction.
    server.expireAccessTokens()
    assert getMessageText(await callWhoami(harness)) == "token access-2"
    assert server.log[-3:] == ["401 access-1", "token refresh", "call access-2"]

    # A token past its expiry is refreshed before the request, without a 401 round trip.
    setExpiry(backend, time.time() * 1000 - 1_000)
    assert getMessageText(await callWhoami(harness)) == "token access-3"
    assert server.log[-2:] == ["token refresh", "call access-3"]

    await prompt(harness, "/mcp logout issues")
    assert notifications[-1] == 'Signed out of MCP server "issues".'
    result = await callWhoami(harness)
    assert result.isError is True
    assert getMessageText(result) == 'MCP server "issues" requires sign-in. Run /mcp to sign in.'


async def test_accepts_a_pasted_redirect_url_when_the_browser_cannot_reach_the_callback(stack):
    harness, server, notifications, _backend, _opened = await setup(stack, "paste")

    await prompt(harness, "/mcp login")
    assert notifications[-1] == 'Signed in to MCP server "issues" (1 tools).'
    assert getMessageText(await callWhoami(harness)) == "token access-1"
    assert "token code" in server.log


async def shutdown(harness: Harness) -> float:
    """Emit session_shutdown like quitting does; resolves to how long the handlers took, in milliseconds."""
    start = time.monotonic()
    await emitSessionShutdownEvent(harness.session.extensionRunner, {"type": "session_shutdown", "reason": "quit"})
    return (time.monotonic() - start) * 1000


# #10565
@pytest.mark.parametrize("path", ["/.well-known/oauth-authorization-server", "/token"])
async def test_cancels_a_sign_in_waiting_on_a_path_when_the_session_shuts_down(stack, path):
    harness, server, notifications, _backend, _opened = await setup(stack, "follow")
    server.stall.add(path)
    login = asyncio.ensure_future(prompt(harness, "/mcp login issues"))
    await waitFor(lambda: len(server.stalled) == 1, timeout=5.0)

    # Shutdown waits for the sign-in to clean up, which aborting makes immediate.
    assert await shutdown(harness) < 2_000
    await login
    # The request is aborted instead of left open, and the ended session reports nothing.
    await waitFor(lambda: server.stalled[0]["request"].socket.destroyed)
    assert [message for message in notifications if message.startswith("Sign-in")] == []


# #10565
async def test_closes_the_session_without_refreshing_an_expiring_token(stack):
    harness, server, notifications, backend, _opened = await setup(stack, "follow")
    await prompt(harness, "/mcp login issues")
    assert notifications[-1] == 'Signed in to MCP server "issues" (1 tools).'
    # Still accepted, but close enough to expiry that the next request would refresh it first.
    setExpiry(backend, time.time() * 1000 + 10_000)
    server.stall.add("/token")

    assert await shutdown(harness) < 2_000
    assert server.stalled == []
    assert server.deletes == ["access-1"]


def freePort() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def test_uses_the_configured_callback_url_and_scope(stack):
    callbackUrl = f"http://localhost:{freePort()}/callback"
    harness, _server, notifications, _backend, opened = await setup(
        stack, "follow", {"callbackUrl": callbackUrl, "scope": "issues:read"}
    )

    await prompt(harness, "/mcp login issues")
    assert notifications[-1] == 'Signed in to MCP server "issues" (1 tools).'
    assert opened[0].searchParams.get("redirect_uri") == callbackUrl
    assert opened[0].searchParams.get("scope") == "issues:read"
    assert getMessageText(await callWhoami(harness)) == "token access-1"


# #10226
async def test_registers_with_the_configured_client_name(stack):
    harness, server, notifications, _backend, _opened = await setup(stack, "follow", {"clientName": "Claude Code"})
    await prompt(harness, "/mcp login issues")
    assert notifications[-1] == 'Signed in to MCP server "issues" (1 tools).'
    assert [metadata["client_name"] for metadata in server.registrations] == ["Claude Code"]

    fallbackHarness, fallbackServer, _notifications, _backend, _opened = await setup(stack, "follow")
    await prompt(fallbackHarness, "/mcp login issues")
    # MISAKA fork: misaka registers as `misaka`, where pi registers as `pi`.
    assert [metadata["client_name"] for metadata in fallbackServer.registrations] == ["misaka"]
    # OpenID Connect servers reject the loopback redirect URI of a `web` client (#10493).
    assert [metadata["application_type"] for metadata in fallbackServer.registrations] == ["native"]


async def test_adds_the_listening_port_to_a_callback_url_without_one(stack):
    harness, _server, notifications, _backend, opened = await setup(
        stack, "follow", {"callbackUrl": "http://127.0.0.1/oauth/done"}
    )
    await prompt(harness, "/mcp login issues")
    assert notifications[-1] == 'Signed in to MCP server "issues" (1 tools).'
    redirect = opened[0].searchParams.get("redirect_uri") or ""
    assert redirect.startswith("http://127.0.0.1:") and redirect.endswith("/oauth/done")
    assert redirect.removeprefix("http://127.0.0.1:").removesuffix("/oauth/done").isdigit()

    port = freePort()
    fixedHarness, _server, _notifications, _backend, fixedOpened = await setup(
        stack, "follow", {"callbackUrl": "http://127.0.0.1/oauth/done", "callbackPort": port}
    )
    await prompt(fixedHarness, "/mcp login issues")
    assert fixedOpened[0].searchParams.get("redirect_uri") == f"http://127.0.0.1:{port}/oauth/done"


async def test_gives_up_on_mcp_login_after_timeout_also_while_the_authorization_server_hangs(stack):
    server = await startOAuthMcpServer()
    agentDir = stack.enter_context(tempfile.TemporaryDirectory(prefix="pi-mcp-login-"))
    Path(agentDir, "mcp.json").write_text(json.dumps({"mcpServers": {"issues": {"url": server.url}}}), encoding="utf-8")

    async def login():
        output: list[str] = []
        exitCode = await runMcpCommand(["login", "issues", "--timeout", "0.5"], {
            "cwd": agentDir,
            "agentDir": agentDir,
            "credentials": McpOAuthCredentialStore(InMemoryAuthStorageBackend()),
            # The user never approves in the browser.
            "openUrl": lambda _url: None,
            "log": output.append,
            "error": output.append,
        })
        return exitCode, output

    exitCode, output = await login()
    assert exitCode == 1
    assert "was cancelled or not completed within" in output[-1]

    # #10565
    server.stall.add("/.well-known/oauth-authorization-server")
    exitCode, output = await login()
    assert exitCode == 1
    assert "was cancelled or not completed within" in output[-1]


async def test_uses_credentials_from_mcp_login_on_the_next_turn(stack):
    harness, server, _notifications, backend, _opened = await setup(stack, "follow")
    agentDir = stack.enter_context(tempfile.TemporaryDirectory(prefix="pi-mcp-login-"))
    Path(agentDir, "mcp.json").write_text(json.dumps({"mcpServers": {"issues": {"url": server.url}}}), encoding="utf-8")
    tasks: set[asyncio.Task] = set()

    async def follow(url: str) -> None:
        await (await fetch(url)).text()

    # The agent runs `misaka mcp login issues` through bash; the user approves in the browser.
    output: list[str] = []
    exitCode = await runMcpCommand(["login", "issues"], {
        "cwd": agentDir,
        "agentDir": agentDir,
        "credentials": McpOAuthCredentialStore(backend),
        "openUrl": lambda url: spawn(follow(url), tasks) and None,
        "log": output.append,
        "error": output.append,
    })
    assert exitCode == 0
    assert output[-1] == 'Signed in to MCP server "issues" (1 tools).'

    # The session still waits for a sign-in, and reconnects when the next turn starts.
    assert getMessageText(await callWhoami(harness)) == "token access-1"


async def test_a_sisters_mcp_login_through_bash_reaches_her_session_on_the_next_turn(stack, monkeypatch):
    """MISAKA fork, pi's loop for a role: a Sister's agent runs `misaka mcp login` through bash, whose environment
    names her profile (as a Hermes profile's terminal gets its HERMES_HOME). The sign-in lands in her profile, not
    Last Order's, and her session picks it up on the next turn."""
    import types

    import misaka.core.http_dispatcher as dispatcher
    import misaka.extensions.mcp as mcp_extension
    from misaka.config import home
    from misaka.extensions.mcp import cli

    server = await startOAuthMcpServer()
    sister = home.path("profiles_root") / "10032"
    sister.mkdir(parents=True)
    (sister / "mcp.json").write_text(
        json.dumps({"mcpServers": {"issues": {"url": server.url, "exposure": "direct"}}}), encoding="utf-8")
    spec = types.SimpleNamespace(role="sisters/10032", mcp_role="10032", profile_dir=str(sister))
    harness: Harness = await stack.enter_async_context(createHarness(
        initialActiveToolNames=[], extensionFactories=[mcp_extension.createMisakaMcpExtension(spec)]))
    await harness.session.bindExtensions({"uiContext": createTestUiContext()})

    for name, value in (("MISAKA_PROFILE_DIR", str(sister)), ("MISAKA_MCP_ROLE", "10032"), ("MISAKA_WHO", "10032")):
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(dispatcher, "applyHttpProxySettings", lambda _proxy: None)
    monkeypatch.setattr(dispatcher, "configureHttpDispatcher", lambda: None)
    tasks: set[asyncio.Task] = set()

    async def follow(url: str) -> None:
        await (await fetch(url)).text()

    # The browser the command opens follows the authorization redirect to the loopback callback.
    monkeypatch.setattr(cli, "openBrowser", lambda url: spawn(follow(url), tasks) and None)
    assert await asyncio.to_thread(mcp_extension.command, ["login", "issues"]) == 0

    assert (sister / "credentials" / "mcp-auth.json").is_file()
    assert not (home.path("roles_root") / "last_order" / "credentials" / "mcp-auth.json").exists()
    # Fenced as untrusted data, as every MCP result in a role's session.
    assert "\ntoken access-1\n" in getMessageText(await callWhoami(harness))


async def test_a_sign_in_to_a_global_server_serves_every_role(stack, monkeypatch):
    """MISAKA fork: a global server's sign-ins are the home's, as the `/login` ones are. A Sister signs in through
    bash, and Last Order's session uses it on her next turn without signing in herself."""
    import types

    import misaka.core.http_dispatcher as dispatcher
    import misaka.extensions.mcp as mcp_extension
    from misaka.config import home
    from misaka.extensions.mcp import cli

    server = await startOAuthMcpServer()
    home.path("mcp_config").write_text(
        json.dumps({"mcpServers": {"issues": {"url": server.url, "exposure": "direct"}}}), encoding="utf-8")
    sister = home.path("profiles_root") / "10032"
    sister.mkdir(parents=True)
    lastOrder = home.path("roles_root") / "last_order"
    spec = types.SimpleNamespace(role="last_order", mcp_role="last-order", profile_dir=str(lastOrder))
    harness: Harness = await stack.enter_async_context(createHarness(
        initialActiveToolNames=[], extensionFactories=[mcp_extension.createMisakaMcpExtension(spec)]))
    await harness.session.bindExtensions({"uiContext": createTestUiContext()})

    for name, value in (("MISAKA_PROFILE_DIR", str(sister)), ("MISAKA_MCP_ROLE", "10032"), ("MISAKA_WHO", "10032")):
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(dispatcher, "applyHttpProxySettings", lambda _proxy: None)
    monkeypatch.setattr(dispatcher, "configureHttpDispatcher", lambda: None)
    tasks: set[asyncio.Task] = set()

    async def follow(url: str) -> None:
        await (await fetch(url)).text()

    monkeypatch.setattr(cli, "openBrowser", lambda url: spawn(follow(url), tasks) and None)
    assert await asyncio.to_thread(mcp_extension.command, ["login", "issues"]) == 0

    assert home.path("mcp_auth").is_file()
    assert not (sister / "credentials" / "mcp-auth.json").exists()
    assert "\ntoken access-1\n" in getMessageText(await callWhoami(harness))

"""pi's ``test/mcp-oauth-refresh.test.ts`` and ``test/mcp-oauth-store.test.ts``."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os

import pytest

from misaka.core.auth_storage import InMemoryAuthStorageBackend, LockResult
from misaka.extensions.mcp.oauth import (
    McpOAuthCredentialStore,
    createMcpAuthProvider,
    signInMcpServer,
)
from misaka.mcp.oauth import OAuthIssuerMismatchError
from misaka.utils.js import spawn
from misaka.utils.whatwg_fetch import Response, fetch
from misaka.utils.whatwg_url import URL
from tests.mcp_helpers import closeServers
from tests.mcp_oauth_server import startOAuthMcpServer


@pytest.fixture(autouse=True)
def _noProxy(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
async def _servers():
    yield
    await closeServers()


class BrowserPrompt:
    """Upstream's prompt literal: `showAuthorizationUrl` opens the URL (which redirects to the callback),
    `promptForRedirectUrl` gives up once the callback arrives."""

    def __init__(self) -> None:
        self.opened: list = []
        self.tasks: set[asyncio.Task] = set()

    def showAuthorizationUrl(self, url) -> None:
        self.opened.append(url)

        async def open() -> None:
            await (await fetch(url)).text()

        task = asyncio.ensure_future(open())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def promptForRedirectUrl(self, signal):
        await signal.wait()


# -- MCP OAuth refresh ----------------------------------------------------------------------------


async def signedIn(tmp_path):
    server = await startOAuthMcpServer()
    lockDir = tmp_path / "locks"
    lockDir.mkdir()
    # Stores sharing the credential file and lock directory stand in for separate misaka processes.
    backend = InMemoryAuthStorageBackend()

    def process():
        store = McpOAuthCredentialStore(backend, str(lockDir)).forServer("test", server.url)
        provider = createMcpAuthProvider({
            "serverUrl": server.url,
            "store": store,
            "settings": dict,
            "onChallenge": lambda _challenge: None,
        })
        return store, provider

    await signInMcpServer({"serverUrl": server.url, "store": process()[0], "settings": {}, "prompt": BrowserPrompt()})
    return server, lockDir, process


def unauthorized(server):
    return {
        "response": Response(None, status=401),
        "serverUrl": URL(server.url),
        "fetch": fetch,
        "token": "access-1",
    }


async def test_refreshes_once_when_several_processes_find_the_same_token_rejected(tmp_path):
    server, lockDir, process = await signedIn(tmp_path)
    server.expireAccessTokens()
    processes = [process(), process(), process()]

    # The server rotates refresh tokens: a second refresh with refresh-1 would fail with invalid_grant.
    await asyncio.gather(*(provider.onUnauthorized(unauthorized(server)) for _store, provider in processes))

    assert server.log.count("token refresh") == 1
    for _store, provider in processes:
        assert await provider.token() == "access-2"
    # The lock is released.
    assert os.listdir(lockDir) == []


async def test_waits_for_a_running_refresh_to_save_the_new_tokens(tmp_path):
    server, _lockDir, process = await signedIn(tmp_path)
    server.expireAccessTokens()
    store, provider = process()

    # A JavaScript async call runs to its first await at once; `spawn` starts the task the same way.
    refresh = spawn(provider.onUnauthorized(unauthorized(server)), set())
    await provider.settled()
    assert store.load()["tokens"]["access_token"] == "access-2"
    await refresh


# -- MCP OAuth sign-in ----------------------------------------------------------------------------


async def signIn(iss, settings=lambda _serverUrl: {}):
    server = await startOAuthMcpServer(iss)
    try:
        await signInMcpServer({
            "serverUrl": server.url,
            "store": McpOAuthCredentialStore(InMemoryAuthStorageBackend()).forServer("test", server.url),
            "settings": settings(server.url),
            "prompt": BrowserPrompt(),
        })
    finally:
        await server.close()


async def test_rejects_an_authorization_response_from_another_issuer():
    with pytest.raises(OAuthIssuerMismatchError):
        await signIn("https://attacker.example")


async def test_keeps_the_granted_scope_when_the_server_asks_for_more():
    server = await startOAuthMcpServer()
    try:
        store = McpOAuthCredentialStore(InMemoryAuthStorageBackend()).forServer("test", server.url)
        prompt = BrowserPrompt()

        async def signInWith(challenge) -> None:
            await signInMcpServer({
                "serverUrl": server.url,
                "store": store,
                "settings": {},
                "challenge": challenge,
                "prompt": prompt,
            })

        await signInWith({"scope": "issues:read"})
        # The token response names no scope, so the grant has the requested one.
        assert store.load()["tokens"]["scope"] == "issues:read"
        # The step-up challenge lists only the missing scope. Requesting just that would lose
        # issues:read, so the next request would ask for sign-in again.
        await signInWith({"error": "insufficient_scope", "scope": "issues:write"})
        assert [url.searchParams.get("scope") for url in prompt.opened] == ["issues:read", "issues:read issues:write"]
        assert store.load()["tokens"]["scope"] == "issues:read issues:write"
    finally:
        await server.close()


# #10172
async def test_uses_the_configured_authorization_server_metadata_url():
    with pytest.raises(RuntimeError, match="HTTP 404 loading authorization server metadata"):
        await signIn(None, lambda serverUrl: {"authServerMetadataUrl": URL("/missing", serverUrl)})


# -- MCP OAuth client ID metadata documents (#10302) ----------------------------------------------
# The configuration rejects `clientRegistration: "cimd"` (a MISAKA fork); the mechanism is reached here
# through the settings, as upstream's tests reach it.

CIMD = {"clientRegistration": "cimd"}


async def startCimdServer(**options):
    server = await startOAuthMcpServer(**options)
    store = McpOAuthCredentialStore(InMemoryAuthStorageBackend()).forServer("test", server.url)

    async def signInWith(settings, prompt=None):
        await signInMcpServer({
            "serverUrl": server.url,
            "store": store,
            "settings": settings,
            "prompt": prompt if prompt is not None else BrowserPrompt(),
        })

    return server, store, signInWith


async def test_registers_dynamically_by_default_even_when_the_server_supports_documents():
    server, _store, signInWith = await startCimdServer(cimd=True, issParameter=True)
    await signInWith({})
    assert len(server.registrations) == 1
    assert server.authorizations[0].get("client_id") == "client-1"


async def test_uses_pis_document_when_authorization_responses_name_their_issuer():
    server, store, signInWith = await startCimdServer(cimd=True, issParameter=True)
    await signInWith(CIMD)
    [authorization] = server.authorizations
    assert authorization.get("client_id") == "https://pi.dev/oauth/client.json"
    redirect = URL(authorization.get("redirect_uri") or "")
    assert f"{redirect.hostname}{redirect.pathname}" == "127.0.0.1/callback"
    assert redirect.port != ""
    assert server.registrations == []
    assert server.tokenRequests[0].get("client_id") == "https://pi.dev/oauth/client.json"
    assert server.tokenRequests[0].get("redirect_uri") == redirect.href
    # The document is not stored, so signing in again refreshes the tokens instead of discarding them.
    assert (store.load() or {}).get("clientInformation") is None
    await signInWith(CIMD)
    assert len(server.authorizations) == 1
    assert server.tokenRequests[1].get("grant_type") == "refresh_token"
    assert server.tokenRequests[1].get("client_id") == "https://pi.dev/oauth/client.json"


async def test_uses_a_document_and_callback_path_specific_to_the_mcp_server_without_the_iss_parameter():
    server, _store, signInWith = await startCimdServer(cimd=True)
    await signInWith(CIMD)
    # Computed like Codex: the first 9 bytes of the SHA-256 of the MCP server URL.
    id = base64.urlsafe_b64encode(hashlib.sha256(server.url.encode()).digest()[:9]).decode()
    [authorization] = server.authorizations
    assert authorization.get("client_id") == f"https://pi.dev/oauth/{id}/client.json"
    redirect = URL(authorization.get("redirect_uri") or "")
    assert redirect.pathname == f"/callback/{id}"
    assert server.registrations == []
    assert server.tokenRequests[0].get("redirect_uri") == redirect.href


async def test_replaces_a_registered_client_when_switching_to_the_document():
    server, store, signInWith = await startCimdServer(cimd=True, issParameter=True)
    await signInWith({})
    assert store.load()["clientInformation"]["client_id"] == "client-1"
    await signInWith(CIMD)
    # The registered client's tokens are not refreshed with another client.
    assert [authorization.get("client_id") for authorization in server.authorizations] == [
        "client-1",
        "https://pi.dev/oauth/client.json",
    ]
    assert (store.load() or {}).get("clientInformation") is None


async def test_accepts_the_authorization_response_only_on_the_server_specific_redirect_uri():
    # A mixed-up authorization server redirects to the shared callback path.
    _server, _store, signInWith = await startCimdServer(cimd=True, redirectPath="/callback")
    with pytest.raises(RuntimeError, match="arrived on another redirect URI"):
        await signInWith(CIMD)

    # The same for a redirect URL pasted from the browser.
    _server, _store, signInByPaste = await startCimdServer(cimd=True)
    shown = None

    class Paste:
        def showAuthorizationUrl(self, url) -> None:
            nonlocal shown
            shown = url

        async def promptForRedirectUrl(self, _signal):
            redirect = URL(shown.searchParams.get("redirect_uri") or "")
            redirect.pathname = "/callback"
            redirect.search = f"?code=code-1&state={shown.searchParams.get('state')}"
            return redirect.href

    with pytest.raises(RuntimeError, match="does not match this sign-in's redirect URI"):
        await signInByPaste(CIMD, Paste())


async def test_fails_instead_of_registering_when_the_server_does_not_support_documents():
    server, _store, signInWith = await startCimdServer(issParameter=True)
    with pytest.raises(RuntimeError, match="does not support Client ID Metadata Documents"):
        await signInWith(CIMD)
    assert server.registrations == []


# -- MCP OAuth credential store -------------------------------------------------------------------

SERVER_URL = "https://mcp.example.com/mcp"


def state(accessToken: str) -> dict:
    return {"serverUrl": SERVER_URL, "tokens": {"access_token": accessToken, "token_type": "Bearer"}}


def storedKeys(backend: InMemoryAuthStorageBackend) -> list[str]:
    return list(json.loads(backend.withLock(lambda current: LockResult(result=current if current is not None else "{}"))))


def seedLegacy(backend: InMemoryAuthStorageBackend) -> None:
    backend.withLock(lambda _current: LockResult(result=None, next=json.dumps({SERVER_URL: state("legacy-token")})))


# https://github.com/earendil-works/pi/issues/10252
def test_keeps_separate_credentials_for_servers_sharing_a_server_url():
    store = McpOAuthCredentialStore(InMemoryAuthStorageBackend())
    store.forServer("work", SERVER_URL).save(state("work-token"))
    store.forServer("personal", SERVER_URL).save(state("personal-token"))

    assert store.forServer("work", SERVER_URL).load()["tokens"]["access_token"] == "work-token"
    assert store.forServer("personal", SERVER_URL).load()["tokens"]["access_token"] == "personal-token"

    assert store.remove("work", SERVER_URL) is True
    assert store.forServer("work", SERVER_URL).load() is None
    assert store.tokens("personal", SERVER_URL)["access_token"] == "personal-token"


def test_moves_credentials_stored_by_server_url_to_the_first_server_that_loads_them():
    backend = InMemoryAuthStorageBackend()
    seedLegacy(backend)
    store = McpOAuthCredentialStore(backend)

    # Reading tokens does not take the legacy state over.
    assert store.tokens("work", SERVER_URL)["access_token"] == "legacy-token"
    assert storedKeys(backend) == [SERVER_URL]

    assert store.forServer("my_work", SERVER_URL).load()["tokens"]["access_token"] == "legacy-token"
    # Names differing only in `-` and `_` are the same server.
    assert store.tokens("my-work", SERVER_URL)["access_token"] == "legacy-token"
    assert store.forServer("personal", SERVER_URL).load() is None
    assert storedKeys(backend) == [f"mcp__my_work|{SERVER_URL}"]


def test_signs_out_of_credentials_stored_by_server_url():
    backend = InMemoryAuthStorageBackend()
    seedLegacy(backend)
    store = McpOAuthCredentialStore(backend)

    assert store.remove("work", SERVER_URL) is True
    assert storedKeys(backend) == []
    assert store.remove("work", SERVER_URL) is False


def test_the_default_store_lives_with_the_other_credentials(tmp_path, monkeypatch):
    """MISAKA fork: the credentials file and the refresh locks come from the home layout."""
    from misaka.config import home

    monkeypatch.setenv(home.ENV_HOME, str(tmp_path))
    store = McpOAuthCredentialStore()
    store.forServer("docs", SERVER_URL).save(state("token"))
    assert os.path.isfile(home.path("mcp_auth"))
    assert store.tokens("docs", SERVER_URL)["access_token"] == "token"


def test_a_callback_port_written_as_a_float_is_a_port_number():
    from misaka.extensions.mcp.oauth import callbackSettings

    settings = callbackSettings({"callbackPort": 8080.0})

    assert settings.port == 8080 and isinstance(settings.port, int)
    assert settings.fixedRedirectUrl == "http://127.0.0.1:8080/callback"

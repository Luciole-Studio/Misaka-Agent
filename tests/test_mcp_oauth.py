"""pi's ``packages/mcp/test/oauth.test.ts``, plus the places where Python would diverge by default
(URL normalisation, the `TypeError` boundary, cancellation)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time

import pytest

from misaka.mcp.client import McpClient
from misaka.mcp.oauth import (
    McpOAuthAuthorizationRequiredError,
    McpOAuthProvider,
    MemoryOAuthStateStore,
    OAuthCallbackServer,
    OAuthInsecureEndpointError,
    OAuthIssuerMismatchError,
    adaptOAuthProvider,
    authorizeMcp,
    discoverAuthorizationServerMetadata,
    discoverOAuthServerInfo,
    registerClient,
    startAuthorization,
)
from misaka.mcp.oauth.types import parseAuthorizationServerMetadata, parseOAuthTokens
from misaka.mcp.transports.streamable_http import StreamableHttpTransport
from misaka.utils.js import AbortController
from misaka.utils.whatwg_fetch import Response, fetch
from misaka.utils.whatwg_url import URL, URLSearchParams
from tests.mcp_helpers import closeServers, listen, readBody


@pytest.fixture(autouse=True)
def _noProxy(monkeypatch):
    """The shim honours the proxy environment, which would route the loopback servers elsewhere."""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
async def _servers():
    yield
    await closeServers()


class OAuthTestProvider:
    """Upstream's ``TestOAuthProvider``: every member synchronous, so each goes through ``maybe_await``."""

    __test__ = False

    def __init__(self, redirectUrl: str) -> None:
        self.redirectUrl = redirectUrl
        self.clientMetadata = {
            "redirect_uris": [redirectUrl],
            "client_name": "pi-mcp-test",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        self.client = None
        self.tokenSet = None
        self.verifier = None
        self.discovery = None
        self.authorizationUrl = None

    def state(self):
        return "expected-state"

    def clientInformation(self):
        return self.client

    def saveClientInformation(self, information):
        self.client = information

    def tokens(self):
        return self.tokenSet

    def saveTokens(self, tokens):
        self.tokenSet = tokens

    def redirectToAuthorization(self, url):
        self.authorizationUrl = url

    def saveCodeVerifier(self, verifier):
        self.verifier = verifier

    def codeVerifier(self):
        if not self.verifier:
            raise RuntimeError("Missing code verifier")
        return self.verifier

    def invalidateCredentials(self, kind):
        if kind in ("all", "client"):
            self.client = None
        if kind in ("all", "tokens"):
            self.tokenSet = None
        if kind in ("all", "verifier"):
            self.verifier = None
        if kind in ("all", "discovery"):
            self.discovery = None

    def saveDiscoveryState(self, state):
        self.discovery = state

    def discoveryState(self):
        return self.discovery


def jsonResponse(response, value, status=200):
    response.writeHead(status, {"content-type": "application/json"})
    response.end(json.dumps(value))


async def drain(response: Response) -> Response:
    """Reads a test-side response to the end so its connection is released."""
    await response.text()
    return response


# -- MCP OAuth ----------------------------------------------------------------------------------


async def test_discovers_registers_authorizes_with_pkce_and_refreshes_on_401():
    expectedChallenge = None
    refreshes = 0

    async def handler(request, response, serverOrigin):
        nonlocal expectedChallenge, refreshes
        url = URL(request.url or "/", serverOrigin)
        if url.pathname == "/.well-known/oauth-protected-resource/mcp":
            jsonResponse(response, {
                "resource": f"{serverOrigin}/mcp",
                "authorization_servers": [serverOrigin],
                "scopes_supported": ["org:read"],
            })
            return
        if url.pathname == "/.well-known/oauth-authorization-server":
            jsonResponse(response, {
                "issuer": serverOrigin,
                "authorization_endpoint": f"{serverOrigin}/authorize",
                "token_endpoint": f"{serverOrigin}/token",
                "registration_endpoint": f"{serverOrigin}/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_methods_supported": ["none"],
                "code_challenge_methods_supported": ["S256"],
            })
            return
        if url.pathname == "/register":
            metadata = json.loads(await readBody(request))
            # Empty and null optional fields count as absent (#10266).
            jsonResponse(response, {**metadata, "client_id": "test-client", "client_secret": ""}, 201)
            return
        if url.pathname == "/authorize":
            expectedChallenge = url.searchParams.get("code_challenge")
            redirect = URL(url.searchParams.get("redirect_uri") or "")
            redirect.searchParams.set("code", "test-code")
            redirect.searchParams.set("state", url.searchParams.get("state") or "")
            response.writeHead(302, {"location": redirect.href}).end()
            return
        if url.pathname == "/token":
            params = URLSearchParams(await readBody(request))
            if params.get("grant_type") == "refresh_token":
                refreshes += 1
                jsonResponse(response, {
                    "access_token": "refreshed-token",
                    "token_type": "Bearer",
                    "refresh_token": "",
                    "expires_in": None,
                })
                return
            digest = hashlib.sha256((params.get("code_verifier") or "").encode()).digest()
            challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
            if params.get("code") != "test-code" or challenge != expectedChallenge:
                jsonResponse(response, {"error": "invalid_grant"}, 400)
                return
            jsonResponse(response, {
                "access_token": "first-token",
                "refresh_token": "refresh-token",
                "token_type": "Bearer",
                "scope": "",
            })
            return
        if url.pathname != "/mcp":
            response.statusCode = 404
            response.end()
            return
        if request.method == "GET":
            response.statusCode = 405
            response.end()
            return
        if request.method == "DELETE":
            response.statusCode = 200
            response.end()
            return
        token = request.headers.get("authorization")
        if token != "Bearer first-token" and token != "Bearer refreshed-token":
            await readBody(request)
            response.writeHead(401, {
                # An empty scope falls through to the resource metadata's scopes_supported.
                "www-authenticate": (
                    f'Bearer resource_metadata="{serverOrigin}/.well-known/oauth-protected-resource/mcp", scope=""'
                ),
            })
            response.end("Unauthorized")
            return
        message = json.loads(await readBody(request))
        if "id" not in message:
            response.statusCode = 202
            response.end()
            return
        result = (
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "oauth-test", "version": "1.0.0"},
            }
            if message["method"] == "initialize"
            else {"tools": [{"name": "issues", "inputSchema": {"type": "object"}}]}
        )
        jsonResponse(response, {"jsonrpc": "2.0", "id": message["id"], "result": result})

    origin = await listen(handler)
    callback = await OAuthCallbackServer.listen()
    provider = OAuthTestProvider(callback.redirectUrl)

    def transport():
        return StreamableHttpTransport({
            "url": f"{origin}/mcp",
            "headers": {"Authorization": "Bearer caller-supplied-stale-token"},
            "authProvider": adaptOAuthProvider(provider),
            "openGetStream": False,
        })

    firstClient = McpClient({"name": "oauth-test", "version": "1.0.0"})
    with pytest.raises(McpOAuthAuthorizationRequiredError):
        await firstClient.connect(transport())
    assert provider.authorizationUrl.searchParams.get("scope") == "org:read"
    assert provider.authorizationUrl.searchParams.get("resource") == f"{origin}/mcp"

    callbackResult = callback.waitForCallback("expected-state")
    authorizationResponse = await drain(await fetch(provider.authorizationUrl, {"redirect": "manual"}))
    await drain(await fetch(authorizationResponse.headers.get("location")))
    code = (await callbackResult)["code"]
    assert await authorizeMcp(provider, {"serverUrl": f"{origin}/mcp", "authorizationCode": code, "fetch": fetch}) == (
        "AUTHORIZED"
    )

    client = McpClient({"name": "oauth-test", "version": "1.0.0"})
    await client.connect(transport())
    assert await client.listTools() == [{"name": "issues", "inputSchema": {"type": "object"}}]
    await client.close()

    provider.tokenSet = {**provider.tokenSet, "access_token": "stale-token"}
    refreshedClient = McpClient({"name": "oauth-test", "version": "1.0.0"})
    await refreshedClient.connect(transport())
    # Neither token response names a scope, so the grant has the requested scope.
    assert provider.tokenSet == {
        "access_token": "refreshed-token",
        "refresh_token": "refresh-token",
        "token_type": "Bearer",
        "scope": "org:read",
    }
    assert refreshes == 1
    await refreshedClient.close()
    await callback.close()


async def test_shares_one_refresh_between_concurrent_401s_when_refresh_tokens_rotate():
    grants: list[str] = []

    async def handler(request, response, serverOrigin):
        url = URL(request.url or "/", serverOrigin)
        if url.pathname == "/.well-known/oauth-protected-resource/mcp":
            # Invalid resource metadata falls back to the server origin instead of failing discovery.
            jsonResponse(response, {"resource": f"{serverOrigin}/mcp", "authorization_servers": ["not a url"]})
            return
        if url.pathname == "/.well-known/oauth-authorization-server":
            jsonResponse(response, {
                # Issuer without the trailing slash that URL parsing adds to the fallback server URL.
                "issuer": serverOrigin,
                "authorization_endpoint": f"{serverOrigin}/authorize",
                "token_endpoint": f"{serverOrigin}/token",
                "response_types_supported": ["code"],
            })
            return
        if url.pathname == "/token":
            params = URLSearchParams(await readBody(request))
            refreshToken = params.get("refresh_token") or ""
            grants.append(refreshToken)
            if refreshToken != "r1":
                jsonResponse(response, {"error": "invalid_grant"}, 400)
                return
            await asyncio.sleep(0.02)
            jsonResponse(response, {"access_token": "a2", "refresh_token": "r2", "token_type": "Bearer", "expires_in": 3600})
            return
        response.statusCode = 404
        response.end()

    origin = await listen(handler)
    store = MemoryOAuthStateStore()
    provider = McpOAuthProvider({
        "serverUrl": f"{origin}/mcp",
        "redirectUrl": "http://127.0.0.1/callback",
        "clientMetadata": {"client_name": "test"},
        "clientId": "client",
        "store": store,
        "onRedirect": lambda _url: None,
    })
    await provider.saveTokens({"access_token": "a1", "refresh_token": "r1", "token_type": "Bearer"})
    auth = adaptOAuthProvider(provider)

    def unauthorized():
        return {
            "response": Response(None, status=401, headers={"www-authenticate": "Bearer"}),
            "serverUrl": URL(f"{origin}/mcp"),
            "fetch": fetch,
            "token": "a1",
        }

    await asyncio.gather(auth.onUnauthorized(unauthorized()), auth.onUnauthorized(unauthorized()))
    # A late 401 for a request that still carried the old token must not refresh again.
    await auth.onUnauthorized(unauthorized())
    assert grants == ["r1"]
    assert await auth.token() == "a2"
    state = store.load()
    assert state["tokens"]["refresh_token"] == "r2"
    assert state["tokensExpireAt"] > time.time() * 1000 + 3_500_000


async def test_asks_for_authorization_instead_of_refreshing_when_the_server_needs_more_scope():
    provider = OAuthTestProvider("http://127.0.0.1/callback")
    provider.client = {"client_id": "client"}
    provider.tokenSet = {"access_token": "a1", "refresh_token": "r1", "token_type": "Bearer", "scope": "repo read:org"}

    async def handler(request, response, serverOrigin):
        url = URL(request.url or "/", serverOrigin)
        if url.pathname == "/.well-known/oauth-authorization-server":
            jsonResponse(response, {
                "issuer": serverOrigin,
                "authorization_endpoint": f"{serverOrigin}/authorize",
                "token_endpoint": f"{serverOrigin}/token",
                "response_types_supported": ["code"],
            })
            return
        response.statusCode = 500 if url.pathname == "/token" else 404
        response.end()

    origin = await listen(handler)
    auth = adaptOAuthProvider(provider)
    with pytest.raises(McpOAuthAuthorizationRequiredError):
        await auth.onUnauthorized({
            "response": Response(
                None,
                status=403,
                headers={"www-authenticate": 'Bearer error="insufficient_scope", scope="repo admin"'},
            ),
            "serverUrl": URL(f"{origin}/mcp"),
            "fetch": fetch,
            "token": "a1",
        })
    # The challenge may list only the missing scopes; the new grant keeps the old ones too.
    assert provider.authorizationUrl.searchParams.get("scope") == "repo read:org admin"
    # The working grant is kept until the user authorizes the new scope.
    assert provider.tokenSet["access_token"] == "a1"


async def test_binds_persisted_credentials_to_the_exact_mcp_server_url():
    store = MemoryOAuthStateStore()
    first = McpOAuthProvider({
        "serverUrl": "https://one.example/mcp",
        "redirectUrl": "http://127.0.0.1/callback",
        "clientMetadata": {"client_name": "test"},
        "store": store,
        "onRedirect": lambda _url: None,
    })
    await first.saveTokens({"access_token": "secret", "token_type": "Bearer"})
    assert (await first.tokens())["access_token"] == "secret"

    second = McpOAuthProvider({
        "serverUrl": "https://two.example/mcp",
        "redirectUrl": "http://127.0.0.1/callback",
        "clientMetadata": {"client_name": "test"},
        "store": store,
        "onRedirect": lambda _url: None,
    })
    assert await second.tokens() is None


# #10493
async def test_registers_with_an_application_type_derived_from_the_redirect_uris_unless_one_is_set():
    bodies: list[dict] = []

    async def handler(request, response, _serverOrigin):
        metadata = json.loads(await readBody(request))
        bodies.append(metadata)
        response.writeHead(201, {"content-type": "application/json"})
        response.end(json.dumps({**metadata, "client_id": "client"}))

    origin = await listen(handler)

    async def register(redirect_uris, application_type=None):
        return await registerClient(origin, {
            "clientMetadata": {
                "redirect_uris": redirect_uris,
                **({"application_type": application_type} if application_type else {}),
            },
        })

    await register(["http://127.0.0.1:1234/callback"])
    await register(["http://[::1]/callback"])
    await register(["com.example.app:/callback"])
    await register(["https://app.example/callback"])
    await register(["http://localhost/callback"], "web")
    assert [body.get("application_type") for body in bodies] == ["native", "native", "native", "web", "web"]


async def test_rejects_authorization_metadata_whose_issuer_does_not_match_discovery():
    async def handler(request, response, serverOrigin):
        url = URL(request.url or "/", serverOrigin)
        if url.pathname == "/.well-known/oauth-authorization-server":
            jsonResponse(response, {
                "issuer": "https://attacker.example",
                "authorization_endpoint": f"{serverOrigin}/authorize",
                "token_endpoint": f"{serverOrigin}/token",
                "response_types_supported": ["code"],
            })
            return
        response.statusCode = 404
        response.end()

    origin = await listen(handler)
    with pytest.raises(OAuthIssuerMismatchError):
        await discoverAuthorizationServerMetadata(origin)


# #10172
async def test_uses_a_configured_authorization_server_metadata_document_as_is():
    async def handler(request, response, serverOrigin):
        url = URL(request.url or "/", serverOrigin)
        response.setHeader("content-type", "application/json")
        if url.pathname == "/.well-known/oauth-protected-resource/mcp":
            # Names the MCP server itself, which serves no authorization server metadata.
            response.end(json.dumps({"resource": f"{serverOrigin}/mcp", "authorization_servers": [serverOrigin]}))
        elif url.pathname == "/idp/metadata.json":
            response.end(json.dumps({
                # Not derivable from the document URL; a configured document is not checked.
                "issuer": "https://idp.example",
                "authorization_endpoint": f"{serverOrigin}/idp/authorize",
                "token_endpoint": f"{serverOrigin}/idp/token",
                "response_types_supported": ["code"],
            }))
        else:
            response.statusCode = 404
            response.end()

    origin = await listen(handler)
    provider = OAuthTestProvider("http://127.0.0.1/callback")
    provider.client = {"client_id": "client"}
    options = {"serverUrl": f"{origin}/mcp", "authorizationServerMetadataUrl": URL(f"{origin}/idp/metadata.json")}
    assert await authorizeMcp(provider, options) == "REDIRECT"
    authorizationUrl = provider.authorizationUrl
    assert f"{authorizationUrl.origin}{authorizationUrl.pathname}" == f"{origin}/idp/authorize"
    assert authorizationUrl.searchParams.get("resource") == f"{origin}/mcp"

    insecure = {**options, "authorizationServerMetadataUrl": URL("http://idp.example/metadata.json")}
    with pytest.raises(OAuthInsecureEndpointError):
        await authorizeMcp(provider, insecure)


async def test_exchanges_a_code_only_when_its_iss_parameter_names_the_authorization_server():
    codes: list[str] = []

    async def handler(request, response, _serverOrigin):
        if request.method != "POST":
            response.statusCode = 404
            response.end()
            return
        codes.append(URLSearchParams(await readBody(request)).get("code") or "")
        jsonResponse(response, {"access_token": "token", "token_type": "Bearer"})

    origin = await listen(handler)

    async def exchange(code, iss, issParameterSupported, metadata=True):
        provider = OAuthTestProvider("http://127.0.0.1/callback")
        provider.client = {"client_id": "client"}
        provider.verifier = "verifier"
        provider.discovery = {
            "authorizationServerUrl": origin,
            "authorizationServerMetadata": {
                "issuer": origin,
                "authorization_endpoint": f"{origin}/authorize",
                "token_endpoint": f"{origin}/token",
                "response_types_supported": ["code"],
                "authorization_response_iss_parameter_supported": issParameterSupported,
            }
            if metadata
            else None,
        }
        options = {"serverUrl": f"{origin}/mcp", "authorizationCode": code}
        if iss is not None:
            options["iss"] = iss
        return await authorizeMcp(provider, options)

    with pytest.raises(OAuthIssuerMismatchError):
        await exchange("other", "https://attacker.example", False)
    with pytest.raises(OAuthIssuerMismatchError):
        await exchange("missing", None, True)
    assert await exchange("matching", origin, True) == "AUTHORIZED"
    # Servers that do not promise the parameter may omit it.
    assert await exchange("omitted", None, False) == "AUTHORIZED"
    # Without metadata there is no issuer to compare with, so a server cannot skip the check by hiding it.
    with pytest.raises(OAuthIssuerMismatchError):
        await exchange("unverifiable", "https://attacker.example", False, False)
    assert await exchange("no-metadata", None, False, False) == "AUTHORIZED"
    assert codes == ["matching", "omitted", "no-metadata"]


# #10565
async def test_stops_when_its_signal_aborts_without_falling_back_to_a_redirect():
    stalled: list[str] = []

    # Accepts every request and never answers.
    async def handler(request, _response, serverOrigin):
        stalled.append(URL(request.url or "/", serverOrigin).pathname)

    origin = await listen(handler)

    async def run(provider):
        controller = AbortController()
        count = len(stalled)
        flow = asyncio.ensure_future(authorizeMcp(provider, {"serverUrl": f"{origin}/mcp", "signal": controller.signal}))
        for _ in range(500):
            if len(stalled) == count + 1:
                break
            await asyncio.sleep(0.01)
        assert len(stalled) == count + 1
        controller.abort()
        with pytest.raises(Exception) as caught:
            await flow
        assert getattr(caught.value, "name", None) == "AbortError"
        assert provider.authorizationUrl is None
        return stalled[-1]

    # Discovery.
    assert await run(OAuthTestProvider("http://127.0.0.1/callback")) == "/.well-known/oauth-protected-resource/mcp"

    # A failed refresh otherwise falls back to a new authorization.
    refreshing = OAuthTestProvider("http://127.0.0.1/callback")
    refreshing.client = {"client_id": "client"}
    refreshing.tokenSet = {"access_token": "a1", "refresh_token": "r1", "token_type": "Bearer"}
    refreshing.discovery = {
        "authorizationServerUrl": origin,
        "authorizationServerMetadata": {
            "issuer": origin,
            "authorization_endpoint": f"{origin}/authorize",
            "token_endpoint": f"{origin}/token",
            "response_types_supported": ["code"],
        },
    }
    assert await run(refreshing) == "/token"


# -- OAuthCallbackServer pages ------------------------------------------------------------------


async def test_renders_plain_text_by_default():
    callback = await OAuthCallbackServer.listen()
    try:
        pending = callback.waitForCallback("s1")
        response = await fetch(f"{callback.redirectUrl}?code=abc&state=s1")
        assert response.headers.get("content-type") == "text/plain; charset=utf-8"
        assert await response.text() == "Authorization complete. You may close this window."
        assert (await pending)["code"] == "abc"
    finally:
        await callback.close()


# #10302
async def test_rejects_a_response_on_another_path_than_the_expected_one():
    callback = await OAuthCallbackServer.listen({"extraPaths": ["/callback/server-id"]})
    try:
        origin = URL(callback.redirectUrl).origin
        mixedUp = callback.waitForCallback("s1", "/callback/server-id")
        wrong = await drain(await fetch(f"{origin}/callback?code=abc&state=s1"))
        assert wrong.status == 400
        with pytest.raises(RuntimeError, match="arrived on another redirect URI"):
            await mixedUp

        pending = callback.waitForCallback("s2", "/callback/server-id")
        right = await drain(await fetch(f"{origin}/callback/server-id?code=abc&state=s2"))
        assert right.status == 200
        assert (await pending)["code"] == "abc"
    finally:
        await callback.close()


async def test_renders_pages_through_render_page():
    pages: list = []

    def renderPage(page):
        pages.append(page)
        return "<p>ok</p>" if page["ok"] else f"<p>{page['message']}</p>"

    callback = await OAuthCallbackServer.listen({"renderPage": renderPage})
    try:
        denied = callback.waitForCallback("s1")
        failure = await drain(
            await fetch(f"{callback.redirectUrl}?error=access_denied&error_description=Denied&state=s1")
        )
        assert failure.headers.get("content-type") == "text/html; charset=utf-8"
        with pytest.raises(RuntimeError, match="Denied"):
            await denied
        assert pages[-1] == {
            "ok": False,
            "message": "Authorization failed. You may close this window.",
            "details": "Denied",
        }

        pending = callback.waitForCallback("s2")
        success = await fetch(f"{callback.redirectUrl}?code=abc&state=s2")
        assert await success.text() == "<p>ok</p>"
        assert (await pending)["code"] == "abc"
    finally:
        await callback.close()


# -- MISAKA: where Python would diverge by default ----------------------------------------------


async def test_a_bare_origin_server_url_is_stored_under_its_normalised_href():
    """`String(new URL("https://One.example"))` is `https://one.example/`; both spellings share one state."""
    store = MemoryOAuthStateStore()

    def provider(serverUrl):
        return McpOAuthProvider({
            "serverUrl": serverUrl,
            "redirectUrl": "http://127.0.0.1/callback",
            "clientMetadata": {"client_name": "test"},
            "store": store,
            "onRedirect": lambda _url: None,
        })

    await provider("https://One.example").saveTokens({"access_token": "secret", "token_type": "Bearer"})
    assert (await provider("https://one.example/").tokens())["access_token"] == "secret"


async def test_a_network_failure_during_resource_discovery_propagates_as_a_type_error():
    """Discovery swallows every protected-resource failure but `TypeError`, which fetch raises for
    network failures; a port nobody listens on is one."""
    with pytest.raises(TypeError):
        await discoverOAuthServerInfo("http://127.0.0.1:1/mcp")


def test_blank_and_null_optional_fields_count_as_absent_and_numbers_read_as_number_does():
    tokens = parseOAuthTokens({"access_token": "a", "token_type": "Bearer", "refresh_token": None, "expires_in": "3600"})
    assert tokens == {"access_token": "a", "token_type": "Bearer", "expires_in": 3600}
    assert isinstance(tokens["expires_in"], int)
    assert parseOAuthTokens({"access_token": "a", "token_type": "Bearer", "expires_in": None, "scope": ""}) == {
        "access_token": "a",
        "token_type": "Bearer",
    }
    with pytest.raises(RuntimeError, match="Invalid expires_in"):
        parseOAuthTokens({"access_token": "a", "token_type": "Bearer", "expires_in": "soon"})
    with pytest.raises(RuntimeError, match="Invalid OAuth token response"):
        parseOAuthTokens(None)


async def test_empty_lists_are_present_as_in_javascript():
    """`[]` is truthy in JavaScript: an empty `response_types_supported` parses, and an empty
    `code_challenge_methods_supported` means S256 is unsupported."""
    metadata = parseAuthorizationServerMetadata({
        "issuer": "https://as.example",
        "authorization_endpoint": "https://as.example/authorize",
        "token_endpoint": "https://as.example/token",
        "response_types_supported": [],
    })
    assert metadata["response_types_supported"] == []
    with pytest.raises(RuntimeError, match="does not support PKCE S256"):
        await startAuthorization("https://as.example", {
            "metadata": {**metadata, "response_types_supported": ["code"], "code_challenge_methods_supported": []},
            "clientInformation": {"client_id": "client"},
            "redirectUrl": "http://127.0.0.1/callback",
        })


async def test_a_cancelled_wait_does_not_break_a_later_callback_or_close():
    callback = await OAuthCallbackServer.listen()
    try:
        abandoned = callback.waitForCallback("s1")
        abandoned.cancel()
        await asyncio.sleep(0)
        await drain(await fetch(f"{callback.redirectUrl}?code=abc&state=s1"))
        pending = callback.waitForCallback("s2")
        await drain(await fetch(f"{callback.redirectUrl}?code=def&state=s2"))
        assert (await pending)["code"] == "def"
    finally:
        await callback.close()


async def test_a_cancelled_waiter_does_not_cancel_the_refresh_the_others_share():
    grants: list[str] = []

    async def handler(request, response, serverOrigin):
        url = URL(request.url or "/", serverOrigin)
        if url.pathname == "/.well-known/oauth-authorization-server":
            jsonResponse(response, {
                "issuer": serverOrigin,
                "authorization_endpoint": f"{serverOrigin}/authorize",
                "token_endpoint": f"{serverOrigin}/token",
                "response_types_supported": ["code"],
            })
            return
        if url.pathname == "/token":
            grants.append(URLSearchParams(await readBody(request)).get("refresh_token") or "")
            await asyncio.sleep(0.05)
            jsonResponse(response, {"access_token": "a2", "refresh_token": "r2", "token_type": "Bearer"})
            return
        response.statusCode = 404
        response.end()

    origin = await listen(handler)
    provider = OAuthTestProvider("http://127.0.0.1/callback")
    provider.client = {"client_id": "client"}
    provider.tokenSet = {"access_token": "a1", "refresh_token": "r1", "token_type": "Bearer"}
    auth = adaptOAuthProvider(provider)

    def unauthorized():
        return {
            "response": Response(None, status=401, headers={"www-authenticate": "Bearer"}),
            "serverUrl": URL(f"{origin}/mcp"),
            "fetch": fetch,
            "token": "a1",
        }

    first = asyncio.ensure_future(auth.onUnauthorized(unauthorized()))
    second = asyncio.ensure_future(auth.onUnauthorized(unauthorized()))
    await asyncio.sleep(0.01)
    first.cancel()
    await second
    assert grants == ["r1"]
    assert await auth.token() == "a2"


async def test_metadata_without_an_authorization_endpoint_falls_back_to_authorize():
    started = await startAuthorization(
        "https://auth.example.com/tenant",
        {
            "metadata": {"issuer": "https://auth.example.com", "response_types_supported": ["code"]},
            "clientInformation": {"client_id": "client"},
            "redirectUrl": "http://127.0.0.1:1/callback",
        },
    )
    assert started["authorizationUrl"].href.startswith("https://auth.example.com/authorize?")


async def test_metadata_without_response_types_fails_as_a_type_error():
    with pytest.raises(TypeError):
        await startAuthorization(
            "https://auth.example.com",
            {
                "metadata": {"issuer": "https://auth.example.com", "authorization_endpoint": "https://auth.example.com/a"},
                "clientInformation": {"client_id": "client"},
                "redirectUrl": "http://127.0.0.1:1/callback",
            },
        )


def test_an_issuer_mismatch_without_an_expected_issuer_names_it_none():
    from misaka.utils.js import UNDEFINED

    # 1.2.0: both sides print `none` when absent (was `undefined` for a missing expected issuer).
    for expected in (None, UNDEFINED):
        error = OAuthIssuerMismatchError(expected, "https://other")
        assert str(error) == 'OAuth issuer mismatch: expected none, received "https://other"'
        assert error.expected is expected
    assert str(OAuthIssuerMismatchError(None, None)) == "OAuth issuer mismatch: expected none, received none"


def test_www_authenticate_fields_follow_javascripts_whitespace_and_case_rules():
    from misaka.mcp.oauth.discovery import parseWwwAuthenticate

    # U+FEFF separates fields as JavaScript's `\s`; U+001C does not.
    assert parseWwwAuthenticate('Bearer error="x",﻿scope="a b"').get("scope") == "a b"
    assert parseWwwAuthenticate('Bearer error="x"\x1cscope="a"').get("scope") is None
    # A case-insensitive JavaScript pattern never matches the long s to an ASCII "s".
    assert parseWwwAuthenticate('Bearer ſcope="a"').get("scope") is None
    assert parseWwwAuthenticate('BEARER SCOPE="a"').get("scope") == "a"

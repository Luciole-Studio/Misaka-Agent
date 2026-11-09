"""pi's ``test/suite/mcp-oauth-server.ts``: an MCP server protected by OAuth, with its own
authorization server (discovery, DCR, PKCE, refresh). ``iss`` is sent as the ``iss`` parameter of
authorization responses (RFC 9207). ``issParameter`` advertises that parameter and sends the server's
issuer. ``cimd`` advertises Client ID Metadata Documents. ``redirectPath`` replaces the path of the
redirect URI, like a mixed-up authorization server. The MCP endpoint assigns a session, so closing a
connection sends a DELETE. Paths added to ``stall`` accept requests and never answer them, like an
unresponsive server."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any

from misaka.mcp.protocol.types import LATEST_PROTOCOL_VERSION
from misaka.utils.whatwg_url import URL, URLSearchParams
from tests.mcp_helpers import (
    IncomingMessage,
    ServerResponse,
    closeServer,
    listen,
    readBody,
)


def _json(response: ServerResponse, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
    response.writeHead(status, {"content-type": "application/json", **(headers or {})}).end(json.dumps(body))


class OAuthMcpServer:
    def __init__(
        self,
        origin: str,
        log: list[str],
        registrations: list[dict[str, Any]],
        authorizations: list[URLSearchParams],
        tokenRequests: list[URLSearchParams],
        deletes: list[str | None],
        stall: set[str],
        stalled: list[dict[str, Any]],
        validTokens: set[str],
    ) -> None:
        self._origin = origin
        self.url = f"{origin}/mcp"
        self.log = log
        # Client metadata of dynamic client registrations.
        self.registrations = registrations
        # Query parameters of authorization requests.
        self.authorizations = authorizations
        # Parameters of token requests.
        self.tokenRequests = tokenRequests
        # Access tokens of session DELETE requests.
        self.deletes = deletes
        self.stall = stall
        # Requests to stalled paths, still open unless the client gave up.
        self.stalled = stalled
        self._validTokens = validTokens

    def expireAccessTokens(self) -> None:
        """Simulates access token expiry."""
        self._validTokens.clear()

    async def close(self) -> None:
        await closeServer(self._origin)


async def startOAuthMcpServer(
    iss: str | None = None, *, issParameter: bool = False, cimd: bool = False, redirectPath: str | None = None
) -> OAuthMcpServer:
    log: list[str] = []
    registrations: list[dict[str, Any]] = []
    authorizations: list[URLSearchParams] = []
    tokenRequests: list[URLSearchParams] = []
    deletes: list[str | None] = []
    stall: set[str] = set()
    stalled: list[dict[str, Any]] = []
    validTokens: set[str] = set()
    refreshTokens: set[str] = set()
    challenges: dict[str, str] = {}
    issued = 0

    def issueTokens() -> dict[str, Any]:
        nonlocal issued
        issued += 1
        tokens = {"access_token": f"access-{issued}", "refresh_token": f"refresh-{issued}"}
        validTokens.add(tokens["access_token"])
        refreshTokens.add(tokens["refresh_token"])
        return {**tokens, "token_type": "Bearer", "expires_in": 3600}

    async def handleMcp(request: IncomingMessage, response: ServerResponse, origin: str) -> None:
        authorization = request.headers.get("authorization")
        token = re.sub(r"^Bearer ", "", authorization) if authorization is not None else None
        if request.method == "DELETE":
            deletes.append(token)
        if request.method != "POST":
            response.writeHead(405 if request.method == "GET" else 200).end()
            return
        if not token or token not in validTokens:
            log.append(f"401 {token if token is not None else 'none'}")
            response.writeHead(401, {
                "www-authenticate": f'Bearer resource_metadata="{origin}/.well-known/oauth-protected-resource/mcp"',
            }).end()
            return
        message = json.loads(await readBody(request))
        if message.get("id") is None:
            response.writeHead(202).end()
            return
        if message["method"] == "initialize":
            result: Any = {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "issues", "version": "1.0.0"},
            }
        elif message["method"] == "tools/list":
            result = {"tools": [{"name": "whoami", "inputSchema": {"type": "object", "properties": {}}}]}
        elif message["method"] == "tools/call":
            log.append(f"call {token}")
            result = {"content": [{"type": "text", "text": f"token {token}"}]}
        else:
            result = {}
        _json(response, 200, {"jsonrpc": "2.0", "id": message["id"], "result": result}, {"mcp-session-id": "session-1"})

    async def handle(request: IncomingMessage, response: ServerResponse, origin: str) -> None:
        url = URL(request.url or "/", origin)
        if url.pathname in stall:
            stalled.append({"path": url.pathname, "request": request})
            return
        match url.pathname:
            case "/mcp":
                await handleMcp(request, response, origin)
            case "/.well-known/oauth-protected-resource/mcp":
                _json(response, 200, {"resource": f"{origin}/mcp", "authorization_servers": [origin]})
            case "/.well-known/oauth-authorization-server":
                _json(response, 200, {
                    "issuer": origin,
                    "authorization_endpoint": f"{origin}/authorize",
                    "token_endpoint": f"{origin}/token",
                    "registration_endpoint": f"{origin}/register",
                    "response_types_supported": ["code"],
                    "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": ["none"],
                    **({"client_id_metadata_document_supported": True} if cimd else {}),
                    **({"authorization_response_iss_parameter_supported": True} if issParameter else {}),
                })
            case "/register":
                metadata = json.loads(await readBody(request))
                log.append("register")
                registrations.append(metadata)
                _json(response, 201, {**metadata, "client_id": "client-1"})
            case "/authorize":
                authorizations.append(url.searchParams)
                code = f"code-{len(challenges) + 1}"
                challenges[code] = url.searchParams.get("code_challenge") or ""
                redirect = URL(url.searchParams.get("redirect_uri") or "")
                if redirectPath:
                    redirect.pathname = redirectPath
                redirect.searchParams.set("code", code)
                redirect.searchParams.set("state", url.searchParams.get("state") or "")
                issuer = iss if iss is not None else (origin if issParameter else None)
                if issuer:
                    redirect.searchParams.set("iss", issuer)
                response.writeHead(302, {"location": redirect.href}).end()
            case "/token":
                params = URLSearchParams(await readBody(request))
                tokenRequests.append(params)
                if params.get("grant_type") == "authorization_code":
                    code = params.get("code") or ""
                    challenge = challenges.get(code)
                    digest = hashlib.sha256((params.get("code_verifier") or "").encode()).digest()
                    verifier = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
                    if not challenge or challenge != verifier:
                        _json(response, 400, {"error": "invalid_grant"})
                        return
                    del challenges[code]
                    log.append("token code")
                    _json(response, 200, issueTokens())
                    return
                refresh = params.get("refresh_token") or ""
                if refresh not in refreshTokens:
                    _json(response, 400, {"error": "invalid_grant"})
                    return
                refreshTokens.discard(refresh)
                log.append("token refresh")
                _json(response, 200, issueTokens())
            case _:
                response.writeHead(404).end()

    origin = await listen(handle)
    return OAuthMcpServer(
        origin, log, registrations, authorizations, tokenRequests, deletes, stall, stalled, validTokens
    )


__all__ = ["OAuthMcpServer", "startOAuthMcpServer"]

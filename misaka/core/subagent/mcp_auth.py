"""CCB MCP OAuth lifecycle adapted to the official Python MCP SDK.

Source: services/mcp/auth.ts at 77a7934e15d69da13879112ed7db695c9ee7a52a.
The SDK owns discovery, issuer/resource validation, DCR, PKCE and token exchange.
MISAKA owns role-private storage, refresh exclusion and explicit UI interaction.
Unlike the source keychain, our secure file can retain validated discovery metadata
alongside tokens, so restart refresh does not invent a second discovery stack.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import httpx
from filelock import FileLock, Timeout
from mcp.client.auth import OAuthClientProvider
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    create_oauth_metadata_request,
    credentials_match_issuer,
    handle_auth_metadata_response,
)
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
)

from misaka.config import get_agent_dir
from misaka.core.auth_storage import FileAuthStorageBackend, LockResult
from misaka.utils.async_lifecycle import settle

AUTH_TIMEOUT = 300


class McpAuthRequired(RuntimeError):
    """An explicit user login is needed; a probe must never launch a browser."""


class McpAuthCancelled(McpAuthRequired):
    """The user dismissed the explicit authentication dialog."""


def server_key(name: str, cfg: dict) -> str:
    # Source getServerKey uses insertion-ordered JSON.stringify, not sorted JSON.
    data = {"type": cfg.get("type") or "http", "url": cfg["url"], "headers": cfg.get("headers") or {}}
    digest = hashlib.sha256(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:16]
    return f"{name}|{digest}"


class McpTokenStorage:
    def __init__(self, name, cfg, context):
        self.cfg = cfg
        self.key = server_key(name, cfg)
        role = hashlib.sha256(context.role.encode()).hexdigest()[:16]
        root = Path(context.profile_dir) if context.profile_dir else Path(get_agent_dir())
        # Server names and roles are data, never filesystem components.
        self.path = root / "mcp-auth" / role / (hashlib.sha256(self.key.encode()).hexdigest() + ".json")
        self.backend = FileAuthStorageBackend(str(self.path))
        self.generation = None
        self.metadata = None

    async def read(self):
        if not self.path.exists() and not self.path.is_symlink():
            return {}
        async def get(raw):
            value = json.loads(raw or "{}")
            if not isinstance(value, dict):
                raise TypeError("Invalid MCP credential storage")
            return LockResult(value)
        return await self.backend.withLockAsync(get)

    async def update(self, changes, *, reset=False):
        async def modify(raw):
            data = json.loads(raw or "{}")
            if not isinstance(data, dict):
                raise TypeError("Invalid MCP credential storage")
            generation = data.get("generation", 0)
            if reset:
                data = {"generation": generation + 1}
            elif self.generation is not None and self.generation != generation:
                raise McpAuthRequired("MCP credentials changed during authentication; retry /mcp auth")
            data.update(changes)
            return LockResult(data, json.dumps(data, ensure_ascii=False))
        result = await self.backend.withLockAsync(modify)
        self.generation = result.get("generation", 0)
        return result

    async def get_tokens(self, data=None):
        data = await self.read() if data is None else data
        self.generation = data.get("generation", 0)
        if not data.get("tokens"):
            return None
        tokens = OAuthToken.model_validate(data["tokens"])
        tokens.expires_in = max(0, int(data["expiresAt"] - time.time()))
        return tokens

    async def set_tokens(self, tokens):
        # Source saveTokens defaults an omitted lifetime to one hour. Preserve 0.
        await self.update({"tokens": tokens.model_dump(mode="json"),
                           "expiresAt": time.time() + (3600 if tokens.expires_in is None else tokens.expires_in),
                           "metadata": self.metadata, "stepUpScope": None})

    async def get_client_info(self, data=None):
        data = await self.read() if data is None else data
        if data.get("client"):
            return OAuthClientInformationFull.model_validate(data["client"])
        client_id = (self.cfg.get("oauth") or {}).get("clientId")
        if client_id:
            return OAuthClientInformationFull(client_id=client_id, redirect_uris=None,
                                               token_endpoint_auth_method="none")
        return None

    async def set_client_info(self, info):
        await self.update({"client": info.model_dump(mode="json")})

    @asynccontextmanager
    async def refresh_lock(self):
        self.backend.ensureParentDir()
        lock = FileLock(str(self.path) + ".refresh.lock", thread_local=False)
        # Nonblocking OS locks avoid abandoned executor threads on cancellation.
        async with asyncio.timeout(30):
            while True:
                try:
                    lock.acquire(timeout=0)
                    break
                except Timeout:
                    await asyncio.sleep(0.05)
        try:
            yield
        finally:
            lock.release()


async def clear_server_tokens(storage, client_factory=httpx.AsyncClient, *, preserve_scope=False):
    """Source revokeServerTokens: refresh first, access second, best-effort remote."""
    data = await storage.read()
    await storage.update({"stepUpScope": data.get("stepUpScope")} if preserve_scope else {}, reset=True)
    raw_metadata, tokens, info = data.get("metadata"), data.get("tokens") or {}, data.get("client") or {}
    if not raw_metadata or not tokens:
        return None
    metadata = OAuthMetadata.model_validate(raw_metadata)
    endpoint = metadata.revocation_endpoint
    if endpoint is None:
        return None
    methods = metadata.revocation_endpoint_auth_methods_supported or metadata.token_endpoint_auth_methods_supported or []
    post_secret = "client_secret_basic" not in methods and "client_secret_post" in methods
    success = True
    async with client_factory(timeout=30, follow_redirects=False) as client:
        for kind in ("refresh_token", "access_token"):
            token = tokens.get(kind)
            if not token:
                continue
            params = {"token": token, "token_type_hint": kind}
            headers = {}
            client_id, secret = info.get("client_id"), info.get("client_secret")
            if client_id and secret:
                if post_secret:
                    params.update(client_id=client_id, client_secret=secret)
                else:
                    encoded = ":".join(quote(value, safe="-_.!~*'()") for value in (client_id, secret))
                    headers["Authorization"] = "Basic " + base64.b64encode(encoded.encode()).decode()
            elif client_id:
                params["client_id"] = client_id
            try:
                response = await client.post(str(endpoint), data=params, headers=headers)
                if response.status_code == 401 and tokens.get("access_token"):
                    params.pop("client_id", None)
                    params.pop("client_secret", None)
                    response = await client.post(str(endpoint), data=params,
                                                 headers={"Authorization": "Bearer " + tokens["access_token"]})
                success = success and response.is_success
            except httpx.HTTPError:
                success = False
    return success


def _transient_refresh_response(response):
    try:
        error = response.json().get("error")
        transient = isinstance(error, str) and error in {"server_error", "temporarily_unavailable"}
    except (ValueError, AttributeError):
        transient = False
    return response.status_code in {429, 500, 502, 503, 504} or transient


def _redact_auth_response(request, response, *, token_request):
    # SDK failure messages include response bodies. Validate before handing
    # failures to it, so tokens and client secrets never reach error logs.
    try:
        body = response.json()
    except ValueError:
        body = {}
    error_code = body.get("error") if isinstance(body, dict) else None
    failed = response.status_code >= 400
    if not failed:
        try:
            parsed = body
            if not token_request and isinstance(body, dict):
                # SDK handle_registration_response drops peer-supplied issuer;
                # only its discovered AS is allowed to bind the credentials.
                parsed = {key: value for key, value in body.items() if key != "issuer"}
            (OAuthToken if token_request else OAuthClientInformationFull).model_validate(parsed)
        except ValueError:
            failed = True
    if failed:
        known_errors = {"invalid_grant", "invalid_client", "invalid_scope", "unauthorized_client",
                        "access_denied", "server_error", "temporarily_unavailable"}
        code = error_code if isinstance(error_code, str) and error_code in known_errors else "invalid_response"
        return httpx.Response(max(400, response.status_code), json={"error": code}, request=request)
    return response


class McpOAuthProvider(OAuthClientProvider):
    def __init__(self, name, cfg, context, *, callback=None, refresh_client_factory=None):
        oauth = cfg.get("oauth") or {}
        if oauth.get("xaa"):
            raise ValueError("MCP XAA requires an owning IdP host connection")
        self.name, self.cfg = name, cfg
        self.storage = McpTokenStorage(name, cfg, context)
        self.callback = callback
        self.refresh_client_factory = refresh_client_factory
        self._flow_lock = asyncio.Lock()
        self._request_authorization = None
        self._refresh_lease = None
        metadata = OAuthClientMetadata(
            client_name=f"MISAKA ({name})", redirect_uris=[callback.uri if callback else "http://localhost:3118/callback"],
            token_endpoint_auth_method="none", grant_types=["authorization_code", "refresh_token"], response_types=["code"],
        )
        super().__init__(cfg["url"], metadata, self.storage, redirect_handler=self._redirect,
                         callback_handler=callback.receive if callback else None, timeout=AUTH_TIMEOUT,
                         client_metadata_url=os.environ.get("MCP_OAUTH_CLIENT_METADATA_URL"))

    def _add_auth_header(self, request):
        # Source requestInit/eventSourceInit headers override provider headers.
        # Preserve an explicit static/helper credential, but replace our own stale
        # header after refresh or scope step-up within the same flow.
        if self._request_authorization is not None:
            request.headers["Authorization"] = self._request_authorization
        else:
            super()._add_auth_header(request)

    async def _redirect(self, url):
        scope = parse_qs(urlsplit(url).query).get("scope", [None])[0]
        if self.callback is None:
            await self.storage.update({"stepUpScope": scope})
            raise McpAuthRequired(f"MCP {self.name} needs authentication; run /mcp auth {self.name}")
        await self.callback.redirect(url)

    async def _initialize(self):
        data = await self.storage.read()
        self.context.current_tokens = await self.storage.get_tokens(data)
        self.context.client_info = await self.storage.get_client_info(data)
        self._initialized = True
        tokens = self.context.current_tokens
        self.context.token_expiry_time = None
        self.context.oauth_metadata = None
        self.context.auth_server_url = None
        if data.get("metadata"):
            metadata = OAuthMetadata.model_validate(data["metadata"])
            info = self.context.client_info
            if info is not None and credentials_match_issuer(info, str(metadata.issuer), self.context.client_metadata_url):
                self.context.oauth_metadata = metadata
                self.context.auth_server_url = str(metadata.issuer)
        if tokens is not None:
            # The SDK does not restore expiry on initialize. CCB refreshes 5m early.
            scope = data.get("stepUpScope")
            if scope and not set(scope.split()).issubset(set((tokens.scope or "").split())):
                tokens.refresh_token = None  # Refresh cannot elevate scopes.
            can_refresh = tokens.refresh_token and self.context.client_info and self.context.oauth_metadata
            self.context.token_expiry_time = time.time() + tokens.expires_in - (300 if can_refresh else 0)

    async def _perform_authorization(self):
        pending_scope = (await self.storage.read()).get("stepUpScope")
        if pending_scope:
            self.context.client_metadata.scope = pending_scope
        if self.callback is None:
            await self.storage.update({"stepUpScope": self.context.client_metadata.scope})
            raise McpAuthRequired(f"MCP {self.name} needs authentication; run /mcp auth {self.name}")
        return await super()._perform_authorization()

    async def _handle_token_response(self, response):
        self.storage.metadata = self.context.oauth_metadata.model_dump(mode="json") if self.context.oauth_metadata else None
        try:
            await super()._handle_token_response(response)
        except McpAuthRequired:
            raise
        except Exception:  # noqa: BLE001 - SDK response bodies may contain secrets
            # SDK errors can include token response bodies. Never expose them to UI/LLM.
            raise McpAuthRequired("MCP OAuth token exchange failed") from None

    async def _handle_refresh_response(self, response):
        self.storage.metadata = self.context.oauth_metadata.model_dump(mode="json") if self.context.oauth_metadata else None
        previous = await self.storage.read()
        try:
            ok = await super()._handle_refresh_response(response)
            if not ok:
                try:
                    invalid_grant = response.json().get("error") == "invalid_grant"
                except (ValueError, AttributeError):
                    invalid_grant = False
                if invalid_grant:
                    await self.storage.update({"tokens": None, "expiresAt": 0})
                elif previous.get("tokens") and previous.get("expiresAt", 0) > time.time():
                    # Source tokens() falls back to the still-live access token.
                    self.context.current_tokens = OAuthToken.model_validate(previous["tokens"])
                    self.context.token_expiry_time = previous["expiresAt"]
            return ok
        finally:
            await self._release_refresh()

    async def _refresh_using_client(self):
        # The SDK builds/validates the protocol; a native owned HTTP client lets
        # transport errors retry too (an auth generator only sees responses).
        async with self.refresh_client_factory(timeout=30, follow_redirects=False, auth=None) as client:
            for attempt in range(3):
                request = await self._refresh_token()
                try:
                    response = await client.send(request, auth=None, follow_redirects=False)
                except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
                    response = httpx.Response(503, json={"error": "temporarily_unavailable"}, request=request)
                if attempt == 2 or not _transient_refresh_response(response):
                    break
                await response.aclose()
                await asyncio.sleep(2 ** attempt)  # Source _doRefresh: 1s, 2s, three attempts.
            await self._handle_refresh_response(_redact_auth_response(request, response, token_request=True))

    async def _release_refresh(self):
        lease, self._refresh_lease = self._refresh_lease, None
        if lease is not None:
            await lease.__aexit__(None, None, None)

    async def _auth_flow(self, request):
        async with self._flow_lock:
            self._request_authorization = request.headers.get("Authorization")
            flow = None
            try:
                await self._initialize()  # Observe refresh/logout performed by another owner.
                self.context.protocol_version = request.headers.get("MCP-Protocol-Version")
                if not self.context.is_token_valid() and self.context.can_refresh_token():
                    self._refresh_lease = self.storage.refresh_lock()
                    await self._refresh_lease.__aenter__()
                    await self._initialize()  # Source reread under the cross-process refresh lock.
                    if self.context.is_token_valid():
                        await self._release_refresh()
                    elif self.context.oauth_metadata is None:
                        # No issuer-bound endpoint: rediscover on the resource challenge,
                        # never post a refresh token to a guessed resource-origin /token.
                        self.context.current_tokens = None
                        await self._release_refresh()
                    elif self.refresh_client_factory is not None:
                        await self._refresh_using_client()
                hint_response = None
                flow = super()._auth_flow(request)
                outgoing = await anext(flow)
                while True:
                    # The explicit metadata URL is an administrator-selected HTTPS hint.
                    hint = (self.cfg.get("oauth") or {}).get("authServerMetadataUrl")
                    discovery_urls = build_oauth_authorization_server_metadata_discovery_urls(self.context.auth_server_url, self.cfg["url"])
                    if hint and outgoing is not request and outgoing.method == "GET" and str(outgoing.url) in discovery_urls:
                        if urlsplit(hint).scheme != "https":
                            raise ValueError("MCP OAuth metadata URL must use HTTPS")
                        outgoing = httpx.Request("GET", hint, headers=outgoing.headers)
                    if hint_response is not None and str(outgoing.url) == hint:
                        response, hint_response = hint_response, None
                    else:
                        response = yield outgoing
                    if hint and outgoing is request and response.status_code in {401, 403} and self.context.auth_server_url is None:
                        if urlsplit(hint).scheme != "https":
                            raise ValueError("MCP OAuth metadata URL must use HTTPS")
                        # Source discoveryState() accepts an explicit external AS hint
                        # even when the resource has no PRM. Seed only the issuer;
                        # the SDK still performs resource discovery and validates the
                        # metadata against any issuer actually advertised by the PRM.
                        candidate = yield create_oauth_metadata_request(hint)
                        _, metadata = await handle_auth_metadata_response(candidate)
                        if metadata is not None:
                            self.context.auth_server_url = str(metadata.issuer)
                            hint_response = candidate
                    refresh = outgoing.method == "POST" and parse_qs(outgoing.content.decode(errors="replace")).get("grant_type") == ["refresh_token"]
                    if refresh:
                        for delay in (1, 2):
                            if not _transient_refresh_response(response):
                                break
                            await response.aclose()
                            await asyncio.sleep(delay)
                            response = yield outgoing
                    if outgoing is not request and outgoing.method == "POST":
                        token_request = bool(parse_qs(outgoing.content.decode(errors="replace")).get("grant_type"))
                        response = _redact_auth_response(outgoing, response, token_request=token_request)
                    try:
                        outgoing = await flow.asend(response)
                    except StopAsyncIteration:
                        break
            finally:
                if flow is not None:
                    await flow.aclose()
                await self._release_refresh()


class OAuthCallback:
    """Source loopback /callback + manual paste, with one cancellable owner."""
    def __init__(self, notify, *, port=None):
        self.notify = notify
        self.port = port
        self.server = None
        self.uri = ""
        self.state = None
        self.future = None
        self.handlers = set()

    async def __aenter__(self):
        self.future = asyncio.get_running_loop().create_future()
        port = self.port
        if port is None:
            configured = os.environ.get("MCP_OAUTH_CALLBACK_PORT")
            port = int(configured) if configured else 0
        if isinstance(port, float) and port.is_integer():
            port = int(port)  # JSON has one number type; 8000.0 is a valid source port.
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("Invalid MCP OAuth callback port")
        # Bind once, not probe-close-rebind: the chosen ephemeral port stays owned.
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", port, limit=8192)
        self.uri = f"http://localhost:{self.server.sockets[0].getsockname()[1]}/callback"
        return self

    async def __aexit__(self, *_):
        _, cancelled = await settle(asyncio.create_task(self._close()))
        if cancelled is not None:
            raise cancelled

    async def _close(self):
        self.server.close()
        await self.server.wait_closed()
        if not self.future.done():
            self.future.cancel()
        else:
            self.future.exception()
        for task in self.handlers:
            task.cancel()
        await asyncio.gather(*self.handlers, return_exceptions=True)

    async def redirect(self, url):
        if urlsplit(url).scheme not in {"http", "https"}:
            raise ValueError("Invalid MCP OAuth authorization URL")
        self.state = parse_qs(urlsplit(url).query).get("state", [None])[0]
        # The native UI displays a clickable URL; background probes never get here.
        await self.notify(url)

    def submit(self, url):
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        if self.future.done() or parsed.path != "/callback":
            return False
        states, codes, errors = query.get("state", []), query.get("code", []), query.get("error", [])
        if not self.state or len(states) != 1 or not secrets.compare_digest(states[0], self.state):
            return False
        if errors:
            self.future.set_exception(McpAuthRequired("MCP OAuth authorization was declined"))
        elif len(codes) == 1 and codes[0]:
            self.future.set_result((codes[0], states[0]))
        else:
            return False
        return True

    async def receive(self):
        return await asyncio.wait_for(asyncio.shield(self.future), AUTH_TIMEOUT)

    async def _handle(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        try:
            async with asyncio.timeout(5):
                line = (await reader.readline()).decode("ascii")
                method, target, _ = line.split()
                await reader.readuntil(b"\r\n\r\n")
                ok = method == "GET" and self.submit(target)
                body = b"Return to MISAKA." if ok else b"Invalid OAuth callback."
                status = b"200 OK" if ok else b"400 Bad Request"
                writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Type: text/plain\r\nConnection: close\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
                await writer.drain()
        except (ValueError, UnicodeError, TimeoutError, OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            finally:
                self.handlers.discard(task)

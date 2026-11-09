"""Translated from pi's ``packages/ai/src/auth/oauth/openai-chatgpt.ts`` (v1.1.0).

OpenAI Responses API token sharing through Sign in with ChatGPT.

This public-client flow uses no client secret and sends the resulting user
access token directly to api.openai.com.

MISAKA fork (each is marked where it happens): ``fetch`` is ``_fetch.fetch``; the callback host variable is
``MISAKA_OAUTH_CALLBACK_HOST``; ``node:http`` becomes ``asyncio.start_server`` with a request-line reader and tracked
connections for ``closeAllConnections``; a stored expiry is whole milliseconds; the busy-port message names misaka;
the callback page waits for the token exchange, as ``callback-server.ts``'s does.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import errno
import math
import re
import secrets
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, TypedDict

from misaka.ai.auth.oauth._fetch import fetch
from misaka.ai.auth.oauth.pkce import generatePKCE
from misaka.ai.auth.types import (
    AuthUrlEvent,
    LoginOptions,
    ManualCodePrompt,
    ModelAuth,
    OAuthAuth,
    OAuthCredential,
    ProgressEvent,
    ProviderAuthInteraction,
)
from misaka.ai.utils.oauth_page import oauthErrorHtml, oauthSuccessHtml
from misaka.ai.utils.provider_env import getProviderEnvValue
from misaka.utils.js import AbortController, AbortSignal, jsTrim
from misaka.utils.values import signal_aborted
from misaka.utils.whatwg_url import URL, URLSearchParams

# every login registers a new client with this ID; OpenAI returns the issued client ID in the callback
_DYNAMIC_CLIENT_ID = "dynamic_agent_client"
_AGENT_NAME_HINT = "Pi"
_UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z", re.IGNORECASE)
_AUTHORIZE_URL = "https://auth.openai.com/api/accounts/authorize"
_TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
_RESOURCE = "https://api.openai.com/v1"
# MISAKA fork: MISAKA_OAUTH_CALLBACK_HOST for upstream's PI_OAUTH_CALLBACK_HOST.
_CALLBACK_HOST = getProviderEnvValue("MISAKA_OAUTH_CALLBACK_HOST") or "127.0.0.1"
_CALLBACK_PORT = 1455
_CALLBACK_PATH = "/auth/callback"
_REDIRECT_URI = f"http://127.0.0.1:{_CALLBACK_PORT}{_CALLBACK_PATH}"
_DIRECT_TOKEN_SCOPE = "chatgpt.tokens.use.direct"
_SCOPE = f"openid profile email offline_access resource.invoke {_DIRECT_TOKEN_SCOPE}"
# Refresh this long before the real expiry so a request never starts with a token about to expire.
_EXPIRY_MARGIN_MS = 3 * 60 * 1000


@dataclass(slots=True, frozen=True)
class _AuthorizationResult:
    code: str
    clientId: str


@dataclass(slots=True)
class _CallbackServer:
    server: asyncio.Server
    result: asyncio.Future[_AuthorizationResult]
    # MISAKA fork: what `closeAllConnections` closes; asyncio's server does not track it.
    connections: set[asyncio.StreamWriter] = field(default_factory=set)
    # MISAKA fork: the browser's callback, held open until the code is exchanged.
    pending: list[asyncio.StreamWriter] = field(default_factory=list)


class _TokenResponse(TypedDict, total=False):
    access_token: Any
    refresh_token: Any
    expires_in: Any
    id_token: Any
    scope: Any


def _randomValue() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")


def _authorizationResultFromCallback(url: URL, expectedState: str) -> _AuthorizationResult:
    code = url.searchParams.get("code")
    if not code:
        raise RuntimeError("Missing authorization code")
    state = url.searchParams.get("state")
    if not state:
        raise RuntimeError("Missing OAuth state")
    if state != expectedState:
        raise RuntimeError("OAuth state mismatch")
    clientId = url.searchParams.get("client_id")
    clientId = jsTrim(clientId) if clientId is not None else None
    if not clientId:
        raise RuntimeError("OpenAI OAuth registration callback did not contain an issued client ID")
    return _AuthorizationResult(code=code, clientId=clientId)


def _authorizationResultFromManualInput(input: str, expectedState: str) -> _AuthorizationResult:
    try:
        url = URL(jsTrim(input))
    except Exception as error:
        raise RuntimeError("Paste the full callback URL from the browser") from error
    expected = URL(_REDIRECT_URI)
    if url.origin != expected.origin or url.pathname != expected.pathname:
        raise RuntimeError(f"The pasted callback URL must start with {_REDIRECT_URI}")
    error = url.searchParams.get("error")
    if error:
        raise RuntimeError(f"ChatGPT authorization failed: {error}")
    return _authorizationResultFromCallback(url, expectedState)


def _sendHtml(response: asyncio.StreamWriter, status: int, body: str) -> None:
    encoded = body.encode("utf-8")
    head = (
        f"HTTP/1.1 {status} {HTTPStatus(status).phrase}\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(encoded)}\r\n"
        "Connection: close\r\n\r\n"
    )
    # MISAKA fork: the peer may be gone already; node's `response.end` does not throw for it.
    with contextlib.suppress(OSError, RuntimeError):
        response.write(head.encode("latin-1") + encoded)
        response.close()


async def _startCallbackServer(expectedState: str) -> _CallbackServer:
    result: asyncio.Future[_AuthorizationResult] = asyncio.get_running_loop().create_future()
    # `Promise.race` observes the loser; a result nobody reads must not be reported.
    result.add_done_callback(lambda done: done.cancelled() or done.exception())
    connections: set[asyncio.StreamWriter] = set()
    pending: list[asyncio.StreamWriter] = []

    def resolveResult(value: _AuthorizationResult) -> None:
        if not result.done():
            result.set_result(value)

    def rejectResult(error: Exception) -> None:
        if not result.done():
            result.set_exception(error)

    async def handle(request: asyncio.StreamReader, response: asyncio.StreamWriter) -> None:
        connections.add(response)
        try:
            requestLine = await request.readline()
            while requestLine.strip():
                header = await request.readline()
                if not header or header in (b"\r\n", b"\n"):
                    break
        except (OSError, ValueError, asyncio.IncompleteReadError):
            requestLine = b""
        finally:
            connections.discard(response)
        parts = requestLine.decode("latin-1").split()
        if len(parts) < 2:
            # MISAKA fork: a peer that sent no request line gets no answer.
            response.transport.abort()
            return
        try:
            url = URL(parts[1] or "", _REDIRECT_URI)
            if url.pathname != _CALLBACK_PATH:
                _sendHtml(response, 404, oauthErrorHtml("Callback route not found."))
                return

            error = url.searchParams.get("error")
            if error:
                _sendHtml(response, 400, oauthErrorHtml("ChatGPT was not connected.", f"Error: {error}"))
                rejectResult(RuntimeError(f"ChatGPT authorization failed: {error}"))
                return

            try:
                authorizationResult = _authorizationResultFromCallback(url, expectedState)
            except Exception as failure:  # noqa: BLE001 - shown on the page; the login keeps waiting
                message = str(failure) or "Invalid callback"
                _sendHtml(response, 400, oauthErrorHtml(message))
                return

            # MISAKA fork: the page waits for the token exchange, as callback-server.ts's does. Sent
            # here, "completed" stood over a login whose code OpenAI then refused (invalid_grant).
            if result.done():
                _sendHtml(response, 409, oauthErrorHtml("This sign-in has already been handled."))
                return
            pending.append(response)
            resolveResult(authorizationResult)
        except Exception:  # noqa: BLE001 - a failing callback still gets an answer
            _sendHtml(response, 500, oauthErrorHtml("Internal error while processing the callback."))

    server = await asyncio.start_server(handle, _CALLBACK_HOST, _CALLBACK_PORT)
    return _CallbackServer(server=server, result=result, connections=connections, pending=pending)


def _answerCallback(callback: _CallbackServer, status: int, body: str) -> None:
    """MISAKA fork: the held browser callback gets the page for how the login ended."""
    while callback.pending:
        _sendHtml(callback.pending.pop(), status, body)


async def _requestToken(body: URLSearchParams, signal: Any) -> _TokenResponse:
    response = await fetch(
        _TOKEN_URL,
        {
            "method": "POST",
            "headers": {
                "accept": "application/json",
                "content-type": "application/x-www-form-urlencoded",
            },
            "body": body,
            "signal": signal,
        },
    )
    if not response.ok:
        try:
            responseBody = await response.text()
        except Exception:  # noqa: BLE001 - `.catch(() => "")`
            responseBody = ""
        raise RuntimeError(
            f"OpenAI OAuth token request failed ({response.status}): {responseBody or response.statusText}"
        )
    data = await response.json()
    if not isinstance(data, dict):
        raise RuntimeError("OpenAI OAuth token response must be an object")  # noqa: TRY004 - upstream throws Error
    return data  # type: ignore[return-value]


def _requireTokenString(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(jsTrim(value)) == 0:
        raise RuntimeError(f"OpenAI OAuth token response has invalid {field}")
    return value


def _credentialFromTokenResponse(token: _TokenResponse, clientId: str) -> OAuthCredential:
    access = _requireTokenString(token.get("access_token"), "access_token")
    refresh = _requireTokenString(token.get("refresh_token"), "refresh_token")
    scope = _requireTokenString(token.get("scope"), "scope")
    expiresIn = token.get("expires_in")
    if (
        isinstance(expiresIn, bool)
        or not isinstance(expiresIn, int | float)
        or not math.isfinite(expiresIn)
        or expiresIn <= 0
    ):
        raise RuntimeError("OpenAI OAuth token response has invalid expires_in")
    scopes = [part for part in re.split(r"\s+", jsTrim(scope)) if part]
    if _DIRECT_TOKEN_SCOPE not in scopes:
        raise RuntimeError(f"OpenAI OAuth grant did not include {_DIRECT_TOKEN_SCOPE}")
    return OAuthCredential(
        type="oauth",
        access=access,
        refresh=refresh,
        expires=time.time() * 1000 + expiresIn * 1000 - _EXPIRY_MARGIN_MS,
        clientId=clientId,
        scopes=scopes,
    )


async def _exchangeAuthorizationCode(code: str, verifier: str, clientId: str, signal: Any) -> OAuthCredential:
    token = await _requestToken(
        URLSearchParams(
            {
                "grant_type": "authorization_code",
                "client_id": clientId,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": _REDIRECT_URI,
                "resource": _RESOURCE,
            }
        ),
        signal,
    )
    # Pi does not use the ID token to identify the user or read profile data.
    # Keep the presence check as part of the token-response contract.
    idToken = token.get("id_token")
    if not isinstance(idToken, str) or len(jsTrim(idToken)) == 0:
        raise RuntimeError("OpenAI OAuth token response did not contain an ID token")
    return _credentialFromTokenResponse(token, clientId)


async def _refreshAccessToken(credential: OAuthCredential, signal: Any) -> OAuthCredential:
    clientId = getattr(credential, "clientId", None)
    if not isinstance(clientId, str) or len(jsTrim(clientId)) == 0:
        raise RuntimeError("Stored OpenAI OAuth credential does not contain an issued client ID; reconnect ChatGPT")
    token = await _requestToken(
        URLSearchParams(
            {
                "grant_type": "refresh_token",
                "client_id": clientId,
                "refresh_token": credential.refresh,
                "resource": _RESOURCE,
            }
        ),
        signal,
    )
    return _credentialFromTokenResponse(token, clientId)


def _agentHostId(deviceId: str | None) -> str:
    """OpenAI identifies each installation ("agent host") by a stable URI such as `urn:uuid:<uuid>`."""
    if not deviceId or not _UUID_PATTERN.match(deviceId):
        raise RuntimeError("Sign in with ChatGPT requires a device ID (UUID) for this installation")
    return f"urn:uuid:{deviceId.lower()}"


async def _loginOpenAIChatGPT(
    interaction: ProviderAuthInteraction,
    options: LoginOptions | None = None,
) -> OAuthCredential:
    getDeviceId = options.getDeviceId if options is not None else None
    hostId = _agentHostId(getDeviceId() if getDeviceId is not None else None)
    pkce = await generatePKCE()
    verifier, challenge = pkce.verifier, pkce.challenge
    state = _randomValue()
    nonce = _randomValue()
    # Without this server, the browser's callback would reach whatever else holds the port (another
    # pending login or the Codex CLI), which rejects it as a state mismatch. Fail with a clear error instead.
    try:
        callback = await _startCallbackServer(state)
    except OSError as error:
        if error.errno != errno.EADDRINUSE:
            raise
        # MISAKA fork: the session is a misaka one.
        raise RuntimeError(
            f"Port {_CALLBACK_PORT} is in use, probably by an unfinished login in another misaka session or by the Codex CLI. Cancel that login and try again."
        ) from error

    authorizationUrl = URL(_AUTHORIZE_URL)
    authorizationUrl.search = URLSearchParams(
        {
            "client_id": _DYNAMIC_CLIENT_ID,
            "agent_name_hint": options.agentName
            if options is not None and options.agentName is not None
            else _AGENT_NAME_HINT,
            "ext_agent_host_id": hostId,
            "response_type": "code",
            "redirect_uri": _REDIRECT_URI,
            "resource": _RESOURCE,
            "scope": _SCOPE,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "nonce": nonce,
        }
    ).toString()
    interaction.notify(
        AuthUrlEvent(
            type="auth_url",
            url=authorizationUrl.toString(),
            instructions="Complete sign-in in your browser. If the callback does not complete, paste the final redirect URL here.",
        )
    )

    manualAbort = AbortController()

    async def manualCodePrompt() -> _AuthorizationResult:
        input = await interaction.prompt(
            ManualCodePrompt(
                type="manual_code",
                message="Complete login in your browser, or paste the final redirect URL here:",
                placeholder=_REDIRECT_URI,
                signal=AbortSignal.any([manualAbort.signal, interaction.signal]),
            )
        )
        return _authorizationResultFromManualInput(input, state)

    manualCode = asyncio.ensure_future(manualCodePrompt())

    try:
        done, _ = await asyncio.wait({callback.result, manualCode}, return_when=asyncio.FIRST_COMPLETED)
        result = (callback.result if callback.result in done else manualCode).result()
        interaction.notify(ProgressEvent(type="progress", message="Exchanging authorization code for tokens..."))
        credential = await _exchangeAuthorizationCode(result.code, verifier, result.clientId, interaction.signal)
        _answerCallback(callback, 200, oauthSuccessHtml("ChatGPT authentication completed. You can close this window."))
        return credential
    except Exception as error:
        # MISAKA fork: callback-server.ts's 502 page for a code the token endpoint refused.
        _answerCallback(callback, 502, oauthErrorHtml("ChatGPT sign-in failed.", str(error)))
        if signal_aborted(interaction.signal):
            raise RuntimeError("Login cancelled") from error
        raise
    finally:
        # MISAKA fork: a cancelled login still answers the browser instead of leaving it loading.
        _answerCallback(callback, 400, oauthErrorHtml("ChatGPT was not connected."))
        manualAbort.abort()
        # MISAKA fork: also stop awaiting a prompt that ignores its signal. `asyncio.wait`, so
        # a cancellation of this login itself still propagates.
        if not manualCode.done():
            manualCode.cancel()
            await asyncio.wait({manualCode})
        if not manualCode.cancelled():
            manualCode.exception()
        callback.server.close()
        # close() only stops accepting new connections. Browsers open spare connections ahead of
        # time, and one that has not sent a request yet stays open and attached to this server.
        # A later login in the same process starts a new server with a new state, but the browser
        # may send that login's callback over the spare connection. This server would then handle
        # it and reject it with "OAuth state mismatch", and the new login would never see it.
        for connection in list(callback.connections):
            connection.transport.abort()
        callback.connections.clear()


async def _openaiChatGPTOAuth_toAuth(credential: OAuthCredential) -> ModelAuth:
    return ModelAuth(apiKey=credential.access)


openaiChatGPTOAuth = OAuthAuth(
    name="OpenAI (ChatGPT subscription)",
    isSubscription=True,
    loginLabel="Sign in with ChatGPT",
    login=_loginOpenAIChatGPT,
    refresh=_refreshAccessToken,
    toAuth=_openaiChatGPTOAuth_toAuth,
)

__all__ = ["openaiChatGPTOAuth"]

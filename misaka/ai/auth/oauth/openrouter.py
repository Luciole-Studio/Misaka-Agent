"""Translated from pi's ``packages/ai/src/auth/oauth/openrouter.ts`` (v1.1.0).

OpenRouter OAuth PKCE flow.

OpenRouter exchanges an authorization code for a permanent, user-controlled
API key rather than an expiring access/refresh token pair. The callback is
handled by a one-shot loopback server on an ephemeral port, raced against a
manual prompt so remote/headless sessions can paste the redirect URL when
the browser cannot reach the loopback server.

MISAKA fork (each is marked where it happens): ``fetch`` is ``_fetch.fetch``, the callback host variable is
``MISAKA_OAUTH_CALLBACK_HOST``, and the login takes the ``options`` every login is called with.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from misaka.ai.auth.oauth._fetch import fetch
from misaka.ai.auth.oauth.callback_server import (
    OAuthCallbackServerOptions,
    startOAuthCallbackServer,
    waitForCallbackOrManualInput,
)
from misaka.ai.auth.oauth.pkce import generatePKCE
from misaka.ai.auth.types import (
    AuthUrlEvent,
    LoginOptions,
    ModelAuth,
    OAuthAuth,
    OAuthCredential,
    ProgressEvent,
    ProviderAuthInteraction,
)
from misaka.ai.utils.provider_env import getProviderEnvValue
from misaka.utils.js import (
    AbortController,
    abortReason,
    addAbortListener,
    jsonStringify,
    jsTrim,
)
from misaka.utils.values import signal_aborted
from misaka.utils.whatwg_url import URL, URLSearchParams

_AUTHORIZE_URL = "https://openrouter.ai/auth"
_TOKEN_URL = "https://openrouter.ai/api/v1/auth/keys"
_LOGIN_TIMEOUT_MS = 5 * 60 * 1000
_TOKEN_EXCHANGE_TIMEOUT_MS = 30_000
# MISAKA fork: `Number.MAX_SAFE_INTEGER`.
_MAX_SAFE_INTEGER = 2**53 - 1


def _getCallbackHost() -> str:
    # MISAKA fork: MISAKA_OAUTH_CALLBACK_HOST for upstream's PI_OAUTH_CALLBACK_HOST.
    return getProviderEnvValue("MISAKA_OAUTH_CALLBACK_HOST") or "127.0.0.1"


_JsonObject = dict[str, Any]


def _parseAuthorizationInput(input: str) -> str | None:
    value = jsTrim(input)
    if not value:
        return None

    try:
        return URL(value).searchParams.get("code")
    except Exception:  # noqa: BLE001, S110 - not a URL
        pass

    if "code=" in value:
        return URLSearchParams(value).get("code")

    return value


def _errorDetail(body: _JsonObject) -> str | None:
    if isinstance(body.get("error_description"), str):
        return body["error_description"]
    if isinstance(body.get("message"), str):
        return body["message"]
    if isinstance(body.get("error"), str):
        return body["error"]
    if isinstance(body.get("error"), dict):
        message = body["error"].get("message")
        if isinstance(message, str):
            return message
    return None


async def _exchangeAuthorizationCode(code: str, verifier: str, signal: Any) -> OAuthCredential:
    if signal_aborted(signal):
        raise RuntimeError("Login cancelled")
    controller = AbortController()

    def onAbort() -> None:
        controller.abort(abortReason(signal))

    removeAbortListener = addAbortListener(signal, onAbort)
    timeout = asyncio.get_running_loop().call_later(
        _TOKEN_EXCHANGE_TIMEOUT_MS / 1000,
        lambda: controller.abort(RuntimeError("OpenRouter OAuth token exchange timed out")),
    )

    body: _JsonObject = {}
    try:
        response = await fetch(
            _TOKEN_URL,
            {
                "method": "POST",
                "headers": {"accept": "application/json", "content-type": "application/json"},
                "body": jsonStringify({"code": code, "code_verifier": verifier, "code_challenge_method": "S256"}),
                "signal": controller.signal,
            },
        )
        try:
            parsed = await response.json()
            if isinstance(parsed, dict):
                body = parsed
        except Exception as error:
            if response.ok:
                raise RuntimeError("OpenRouter OAuth returned invalid JSON") from error
    except Exception as error:
        if signal_aborted(signal):
            raise RuntimeError("Login cancelled") from error
        if controller.signal.aborted:
            raise RuntimeError("OpenRouter OAuth token exchange timed out") from error
        raise
    finally:
        timeout.cancel()
        removeAbortListener()

    if not response.ok:
        detail = _errorDetail(body)
        raise RuntimeError(f"OpenRouter OAuth key exchange failed (HTTP {response.status}){f': {detail}' if detail else ''}")

    if not isinstance(body.get("key"), str) or len(body["key"]) == 0:
        raise RuntimeError('OpenRouter OAuth response carries no "key"')

    return OAuthCredential(
        type="oauth",
        access=body["key"],
        refresh="",
        expires=_MAX_SAFE_INTEGER,
    )


async def _loginOpenRouter(interaction: ProviderAuthInteraction, options: LoginOptions | None = None) -> OAuthCredential:
    # MISAKA fork: `options` is accepted because every login is called with it.
    pkce = await generatePKCE()
    verifier, challenge = pkce.verifier, pkce.challenge
    # OpenRouter sends no `state`; the random path keeps stray requests from completing the sign-in.
    callback = await startOAuthCallbackServer(
        OAuthCallbackServerOptions(
            providerName="OpenRouter",
            host=_getCallbackHost(),
            port=0,
            path=f"/oauth/callback/{uuid.uuid4()}",
            complete=lambda code: _exchangeAuthorizationCode(code, verifier, interaction.signal),
            signal=interaction.signal,
            timeoutMs=_LOGIN_TIMEOUT_MS,
        )
    )

    try:
        authorizeUrl = URL(_AUTHORIZE_URL)
        authorizeUrl.search = URLSearchParams(
            {
                "callback_url": callback.redirectUri,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        ).toString()

        interaction.notify(
            ProgressEvent(
                type="progress",
                message=f"Listening for OpenRouter OAuth callback on {callback.redirectUri}",
            )
        )
        interaction.notify(
            AuthUrlEvent(
                type="auth_url",
                url=authorizeUrl.toString(),
                instructions="Complete sign-in in your browser. If the browser is on another machine, paste the final redirect URL here.",
            )
        )

        result = await waitForCallbackOrManualInput(
            interaction,
            callback,
            {
                "message": "Complete sign-in in your browser, or paste the authorization code / redirect URL here:",
                "placeholder": callback.redirectUri,
            },
        )
        if result["type"] == "callback":
            return result["value"]
        code = _parseAuthorizationInput(result["input"])
        if not code:
            raise RuntimeError("Missing authorization code")
        interaction.notify(ProgressEvent(type="progress", message="Exchanging authorization code for an API key..."))
        return await _exchangeAuthorizationCode(code, verifier, interaction.signal)
    finally:
        callback.close()


async def _openRouterOAuth_refresh(credential: OAuthCredential, _signal: Any) -> OAuthCredential:
    return credential


async def _openRouterOAuth_toAuth(credential: OAuthCredential) -> ModelAuth:
    return ModelAuth(apiKey=credential.access)


openRouterOAuth = OAuthAuth(
    name="OpenRouter OAuth",
    loginLabel="Sign in with OpenRouter",
    login=_loginOpenRouter,
    refresh=_openRouterOAuth_refresh,
    toAuth=_openRouterOAuth_toAuth,
)

__all__ = ["openRouterOAuth"]

"""Translated from pi's ``packages/ai/src/auth/oauth/anthropic.ts`` (v1.1.0).

Anthropic OAuth flow (Claude Pro/Max)

MISAKA fork (each is marked where it happens): ``fetch`` is ``_fetch.fetch``; the callback host variable is
``MISAKA_OAUTH_CALLBACK_HOST``; a separate random ``state`` keeps the PKCE verifier off the authorize URL; an error's
``stack`` is the Python traceback; and the browser login takes the ``options`` every login is called with.
"""

from __future__ import annotations

import base64
import secrets
import time
import traceback
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
    ManualCodePrompt,
    ModelAuth,
    OAuthAuth,
    OAuthCredential,
    ProgressEvent,
    ProviderAuthInteraction,
    SelectOption,
    SelectPrompt,
)
from misaka.ai.utils.provider_env import getProviderEnvValue
from misaka.utils.js import AbortSignal, jsonParse, jsonStringify, jsString, jsTrim
from misaka.utils.whatwg_url import URL, URLSearchParams


def _decode(s: str) -> str:
    return base64.b64decode(s).decode("latin-1")


_CLIENT_ID = _decode("OWQxYzI1MGEtZTYxYi00NGQ5LTg4ZWQtNTk0NGQxOTYyZjVl")
_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
# MISAKA fork: MISAKA_OAUTH_CALLBACK_HOST for upstream's PI_OAUTH_CALLBACK_HOST.
_CALLBACK_HOST = getProviderEnvValue("MISAKA_OAUTH_CALLBACK_HOST") or "127.0.0.1"
# Preferred so the port can be forwarded into containers or over SSH. Anthropic accepts any loopback port,
# so login falls back to a free port when this one cannot be bound (#10571).
_CALLBACK_PORT = 53692
_CALLBACK_PATH = "/callback"
_REDIRECT_URI = f"http://localhost:{_CALLBACK_PORT}{_CALLBACK_PATH}"
_COPY_CODE_REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
_ANTHROPIC_BROWSER_LOGIN_METHOD = "browser"
_ANTHROPIC_COPY_CODE_LOGIN_METHOD = "copy_code"
_SCOPES = (
    "org:create_api_key user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload"
)


def _createState() -> str:
    # MISAKA fork: upstream sends the PKCE verifier as `state`, which puts the secret half of PKCE into the
    # authorize URL (browser history, logs). A separate random state keeps the verifier on this machine.
    return secrets.token_hex(16)


def _parseAuthorizationInput(input: str) -> dict[str, str | None]:
    value = jsTrim(input)
    if not value:
        return {}

    try:
        url = URL(value)
        return {
            "code": url.searchParams.get("code"),
            "state": url.searchParams.get("state"),
        }
    except Exception:  # noqa: BLE001, S110 - not a URL
        pass

    if "#" in value:
        # `value.split("#", 2)`: JavaScript's limit drops whatever follows a second `#`.
        code, state = value.split("#")[:2]
        return {"code": code, "state": state}

    if "code=" in value:
        params = URLSearchParams(value)
        return {
            "code": params.get("code"),
            "state": params.get("state"),
        }

    return {"code": value}


def _formatErrorDetails(error: Any) -> str:
    if isinstance(error, BaseException):
        details: list[str] = [f"{getattr(error, 'name', type(error).__name__)}: {error}"]
        code = getattr(error, "code", None)
        if code:
            details.append(f"code={code}")
        errno = getattr(error, "errno", None)
        if errno is not None:
            details.append(f"errno={jsString(errno)}")
        if error.__cause__ is not None:
            details.append(f"cause={_formatErrorDetails(error.__cause__)}")
        # MISAKA fork: Python has no `error.stack`; the traceback stands in for it.
        stack = "".join(traceback.format_exception(error)).strip()
        if stack:
            details.append(f"stack={stack}")
        return "; ".join(details)
    return jsString(error)


async def _postJson(url: str, body: dict[str, str | int], signal: Any) -> str:
    response = await fetch(
        url,
        {
            "method": "POST",
            "headers": {
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            "body": jsonStringify(body),
            "signal": AbortSignal.any([signal, AbortSignal.timeout(30_000)]),
        },
    )

    responseBody = await response.text()

    if not response.ok:
        raise RuntimeError(f"HTTP request failed. status={response.status}; url={url}; body={responseBody}")

    return responseBody


async def _exchangeAuthorizationCode(
    code: str,
    state: str,
    verifier: str,
    redirectUri: str,
    signal: Any,
) -> OAuthCredential:
    try:
        responseBody = await _postJson(
            _TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "client_id": _CLIENT_ID,
                "code": code,
                "state": state,
                "redirect_uri": redirectUri,
                "code_verifier": verifier,
            },
            signal,
        )
    except Exception as error:
        raise RuntimeError(
            f"Token exchange request failed. url={_TOKEN_URL}; redirect_uri={redirectUri}; response_type=authorization_code; details={_formatErrorDetails(error)}"
        ) from error

    try:
        tokenData = jsonParse(responseBody)
    except Exception as error:
        raise RuntimeError(
            f"Token exchange returned invalid JSON. url={_TOKEN_URL}; body={responseBody}; details={_formatErrorDetails(error)}"
        ) from error

    return OAuthCredential(
        type="oauth",
        refresh=tokenData["refresh_token"],
        access=tokenData["access_token"],
        expires=time.time() * 1000 + tokenData["expires_in"] * 1000 - 5 * 60 * 1000,
    )


async def _loginAnthropic(interaction: ProviderAuthInteraction, options: LoginOptions | None = None) -> OAuthCredential:
    # MISAKA fork: `options` is accepted because every login is called with it.
    pkce = await generatePKCE()
    verifier, challenge = pkce.verifier, pkce.challenge
    state = _createState()

    async def complete(code: str) -> str:
        return code

    def startCallbackServer(port: int) -> Any:
        return startOAuthCallbackServer(
            OAuthCallbackServerOptions(
                providerName="Anthropic",
                host=_CALLBACK_HOST,
                port=port,
                path=_CALLBACK_PATH,
                redirectHost="localhost",
                state=state,
                complete=complete,
                signal=interaction.signal,
            )
        )

    # Without a callback server, login continues with the pasted redirect URL.
    try:
        callback = await startCallbackServer(_CALLBACK_PORT)
    except Exception:  # noqa: BLE001 - `.catch(() => startCallbackServer(0))`
        try:
            callback = await startCallbackServer(0)
        except Exception:  # noqa: BLE001 - `.catch(() => undefined)`
            callback = None
    redirectUri = callback.redirectUri if callback is not None else _REDIRECT_URI

    try:
        authParams = URLSearchParams(
            {
                "code": "true",
                "client_id": _CLIENT_ID,
                "response_type": "code",
                "redirect_uri": redirectUri,
                "scope": _SCOPES,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
            }
        )
        interaction.notify(
            AuthUrlEvent(
                type="auth_url",
                url=f"{_AUTHORIZE_URL}?{authParams.toString()}",
                instructions="Complete login in your browser. If the browser is on another machine, paste the final redirect URL here.",
            )
        )

        result = await waitForCallbackOrManualInput(
            interaction,
            callback,
            {
                "message": "Complete login in your browser, or paste the authorization code / redirect URL here:",
                "placeholder": redirectUri,
            },
        )
        code: str | None
        resultState = state
        if result["type"] == "callback":
            code = result["value"]
        else:
            parsed = _parseAuthorizationInput(result["input"])
            if parsed.get("state") and parsed.get("state") != state:
                raise RuntimeError("OAuth state mismatch")
            code = parsed.get("code")
            resultState = parsed["state"] if parsed.get("state") is not None else state

        if not code:
            raise RuntimeError("Missing authorization code")
        interaction.notify(ProgressEvent(type="progress", message="Exchanging authorization code for tokens..."))
        return await _exchangeAuthorizationCode(code, resultState, verifier, redirectUri, interaction.signal)
    finally:
        if callback is not None:
            callback.close()


async def _loginAnthropicCopyCode(interaction: ProviderAuthInteraction) -> OAuthCredential:
    pkce = await generatePKCE()
    verifier, challenge = pkce.verifier, pkce.challenge
    state = _createState()
    authParams = URLSearchParams(
        {
            "code": "true",
            "client_id": _CLIENT_ID,
            "response_type": "code",
            "redirect_uri": _COPY_CODE_REDIRECT_URI,
            "scope": _SCOPES,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    interaction.notify(
        AuthUrlEvent(
            type="auth_url",
            url=f"{_AUTHORIZE_URL}?{authParams.toString()}",
            instructions="Complete login in your browser, then copy the code Anthropic shows and paste it here.",
        )
    )

    input = await interaction.prompt(
        ManualCodePrompt(
            type="manual_code",
            message="Paste the code Anthropic shows after you sign in:",
            placeholder="code#state",
            signal=interaction.signal,
        )
    )
    parsed = _parseAuthorizationInput(input)
    if parsed.get("state") and parsed.get("state") != state:
        raise RuntimeError("OAuth state mismatch")
    if not parsed.get("code"):
        raise RuntimeError("Missing authorization code")
    interaction.notify(ProgressEvent(type="progress", message="Exchanging authorization code for tokens..."))
    return await _exchangeAuthorizationCode(
        parsed["code"],
        parsed["state"] if parsed.get("state") is not None else state,
        verifier,
        _COPY_CODE_REDIRECT_URI,
        interaction.signal,
    )


async def _refreshAnthropicToken(refreshToken: str, signal: Any) -> OAuthCredential:
    """Refresh Anthropic OAuth token"""
    try:
        responseBody = await _postJson(
            _TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "client_id": _CLIENT_ID,
                "refresh_token": refreshToken,
            },
            signal,
        )
    except Exception as error:
        raise RuntimeError(
            f"Anthropic token refresh request failed. url={_TOKEN_URL}; details={_formatErrorDetails(error)}"
        ) from error

    try:
        data = jsonParse(responseBody)
    except Exception as error:
        raise RuntimeError(
            f"Anthropic token refresh returned invalid JSON. url={_TOKEN_URL}; body={responseBody}; details={_formatErrorDetails(error)}"
        ) from error

    return OAuthCredential(
        type="oauth",
        refresh=data["refresh_token"],
        access=data["access_token"],
        expires=time.time() * 1000 + data["expires_in"] * 1000 - 5 * 60 * 1000,
    )


async def _anthropicOAuth_login(interaction: ProviderAuthInteraction, options: LoginOptions | None = None) -> OAuthCredential:
    method = await interaction.prompt(
        SelectPrompt(
            type="select",
            message="Select Anthropic login method:",
            options=[
                SelectOption(id=_ANTHROPIC_BROWSER_LOGIN_METHOD, label="Browser login (default)"),
                SelectOption(id=_ANTHROPIC_COPY_CODE_LOGIN_METHOD, label="Copy code login (headless)"),
            ],
        )
    )

    if method == _ANTHROPIC_COPY_CODE_LOGIN_METHOD:
        return await _loginAnthropicCopyCode(interaction)
    if method != _ANTHROPIC_BROWSER_LOGIN_METHOD:
        raise RuntimeError(f"Unknown Anthropic login method: {method}")

    return await _loginAnthropic(interaction)


async def _anthropicOAuth_toAuth(credential: OAuthCredential) -> ModelAuth:
    return ModelAuth(apiKey=credential.access)


anthropicOAuth = OAuthAuth(
    name="Anthropic (Claude Pro/Max)",
    isSubscription=True,
    login=_anthropicOAuth_login,
    refresh=lambda credential, signal: _refreshAnthropicToken(credential.refresh, signal),
    toAuth=_anthropicOAuth_toAuth,
)

__all__ = ["anthropicOAuth"]

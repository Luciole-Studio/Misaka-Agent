"""Translated from pi's ``packages/ai/src/auth/oauth/openai-codex.ts`` (v1.1.0).

OpenAI Codex (ChatGPT OAuth) flow

MISAKA fork (each is marked where it happens): ``fetch`` is ``_fetch.fetch``; the callback host variable is
``MISAKA_OAUTH_CALLBACK_HOST``; ``secrets`` stands in for the lazily imported ``node:crypto``; the JWT payload is decoded
as base64url (RFC 7519), where ``atob`` rejects ``-`` and ``_``.
"""

from __future__ import annotations

import base64
import math
import secrets
import time
from dataclasses import dataclass
from typing import Any, Literal

from misaka.ai.auth.oauth._fetch import Response, fetch
from misaka.ai.auth.oauth.callback_server import (
    OAuthCallbackServerOptions,
    startOAuthCallbackServer,
    waitForCallbackOrManualInput,
)
from misaka.ai.auth.oauth.device_code import pollOAuthDeviceCodeFlow
from misaka.ai.auth.oauth.pkce import generatePKCE
from misaka.ai.auth.types import (
    AuthUrlEvent,
    DeviceCodeEvent,
    LoginOptions,
    ModelAuth,
    OAuthAuth,
    OAuthCredential,
    ProviderAuthInteraction,
    SelectOption,
    SelectPrompt,
)
from misaka.ai.utils.provider_env import getProviderEnvValue
from misaka.utils.js import jsNumber, jsonParse, jsonStringify, jsTrim
from misaka.utils.values import signal_aborted
from misaka.utils.whatwg_url import URL, URLSearchParams

_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_AUTH_BASE_URL = "https://auth.openai.com"
_AUTHORIZE_URL = f"{_AUTH_BASE_URL}/oauth/authorize"
_TOKEN_URL = f"{_AUTH_BASE_URL}/oauth/token"
_REDIRECT_URI = "http://localhost:1455/auth/callback"
_DEVICE_USER_CODE_URL = f"{_AUTH_BASE_URL}/api/accounts/deviceauth/usercode"
_DEVICE_TOKEN_URL = f"{_AUTH_BASE_URL}/api/accounts/deviceauth/token"
_DEVICE_VERIFICATION_URI = f"{_AUTH_BASE_URL}/codex/device"
_DEVICE_REDIRECT_URI = f"{_AUTH_BASE_URL}/deviceauth/callback"
_DEVICE_CODE_TIMEOUT_SECONDS = 15 * 60
_OPENAI_CODEX_BROWSER_LOGIN_METHOD = "browser"
_OPENAI_CODEX_DEVICE_CODE_LOGIN_METHOD = "device_code"
_SCOPE = "openid profile email offline_access"
_JWT_CLAIM_PATH = "https://api.openai.com/auth"


@dataclass(slots=True)
class _OAuthToken:
    access: str
    refresh: str
    expires: float


_TokenOperation = Literal["exchange", "refresh"]


def _getCallbackHost() -> str:
    # MISAKA fork: MISAKA_OAUTH_CALLBACK_HOST for upstream's PI_OAUTH_CALLBACK_HOST.
    return getProviderEnvValue("MISAKA_OAUTH_CALLBACK_HOST") or "127.0.0.1"


@dataclass(slots=True)
class _DeviceAuthInfo:
    deviceAuthId: str
    userCode: str
    intervalSeconds: float


@dataclass(slots=True)
class _DeviceTokenSuccess:
    authorizationCode: str
    codeVerifier: str


def _createState() -> str:
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


def _decodeJwt(token: str) -> dict[str, Any] | None:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1] or ""
        # MISAKA fork: base64url with its padding restored (RFC 7519); `atob` throws on `-` and `_`.
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode("latin-1")
        return jsonParse(decoded)
    except Exception:  # noqa: BLE001 - `catch { return null; }`
        return None


async def _fetchWithLoginCancellation(input: str, init: Any) -> Response:
    try:
        return await fetch(input, init)
    except Exception as error:
        if signal_aborted(init.get("signal")):
            raise RuntimeError("Login cancelled") from error
        raise


async def _responseText(response: Response) -> str:
    """`response.text().catch(() => "")`."""
    try:
        return await response.text()
    except Exception:  # noqa: BLE001 - `.catch(() => "")`
        return ""


async def _readTokenResponse(response: Response, operation: _TokenOperation) -> _OAuthToken:
    if not response.ok:
        text = await _responseText(response)
        raise RuntimeError(f"OpenAI Codex token {operation} failed ({response.status}): {text or response.statusText}")

    json = await response.json()
    record = json if isinstance(json, dict) else {}
    expiresIn = record.get("expires_in")
    if (
        not record.get("access_token")
        or not record.get("refresh_token")
        or not isinstance(expiresIn, int | float)
        or isinstance(expiresIn, bool)
    ):
        raise RuntimeError(f"OpenAI Codex token {operation} response missing fields: {jsonStringify(json)}")

    return _OAuthToken(
        access=record["access_token"],
        refresh=record["refresh_token"],
        expires=time.time() * 1000 + expiresIn * 1000,
    )


async def _exchangeAuthorizationCode(code: str, verifier: str, redirectUri: str, signal: Any) -> _OAuthToken:
    response = await _fetchWithLoginCancellation(
        _TOKEN_URL,
        {
            "method": "POST",
            "headers": {"Content-Type": "application/x-www-form-urlencoded"},
            "body": URLSearchParams(
                {
                    "grant_type": "authorization_code",
                    "client_id": _CLIENT_ID,
                    "code": code,
                    "code_verifier": verifier,
                    "redirect_uri": redirectUri,
                }
            ),
            "signal": signal,
        },
    )

    return await _readTokenResponse(response, "exchange")


async def _refreshAccessToken(refreshToken: str, signal: Any) -> _OAuthToken:
    try:
        response = await fetch(
            _TOKEN_URL,
            {
                "method": "POST",
                "headers": {"Content-Type": "application/x-www-form-urlencoded"},
                "body": URLSearchParams(
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": refreshToken,
                        "client_id": _CLIENT_ID,
                    }
                ),
                "signal": signal,
            },
        )
    except Exception as error:
        raise RuntimeError(f"OpenAI Codex token refresh error: {error}") from error

    return await _readTokenResponse(response, "refresh")


async def _startOpenAICodexDeviceAuth(signal: Any) -> _DeviceAuthInfo:
    response = await _fetchWithLoginCancellation(
        _DEVICE_USER_CODE_URL,
        {
            "method": "POST",
            "headers": {"Content-Type": "application/json"},
            "body": jsonStringify({"client_id": _CLIENT_ID}),
            "signal": signal,
        },
    )

    if not response.ok:
        if response.status == 404:
            raise RuntimeError(
                "OpenAI Codex device code login is not enabled for this server. Use browser login or verify the server URL."
            )
        responseBody = await _responseText(response)
        raise RuntimeError(
            f"OpenAI Codex device code request failed with status {response.status}{f': {responseBody}' if responseBody else ''}"
        )

    json = await response.json()
    record = json if isinstance(json, dict) else {}
    interval = record.get("interval")
    intervalSeconds = jsNumber(jsTrim(interval)) if isinstance(interval, str) else interval
    if (
        not record.get("device_auth_id")
        or not record.get("user_code")
        or not isinstance(intervalSeconds, int | float)
        or isinstance(intervalSeconds, bool)
        or not math.isfinite(intervalSeconds)
        or intervalSeconds < 0
    ):
        raise RuntimeError(f"Invalid OpenAI Codex device code response: {jsonStringify(json)}")

    return _DeviceAuthInfo(
        deviceAuthId=record["device_auth_id"],
        userCode=record["user_code"],
        intervalSeconds=intervalSeconds,
    )


async def _pollOpenAICodexDeviceAuth(device: _DeviceAuthInfo, signal: Any) -> _DeviceTokenSuccess:
    async def poll() -> Any:
        response = await _fetchWithLoginCancellation(
            _DEVICE_TOKEN_URL,
            {
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
                "body": jsonStringify(
                    {
                        "device_auth_id": device.deviceAuthId,
                        "user_code": device.userCode,
                    }
                ),
                "signal": signal,
            },
        )

        if response.ok:
            json = await response.json()
            record = json if isinstance(json, dict) else {}
            if not record.get("authorization_code") or not record.get("code_verifier"):
                return {
                    "status": "failed",
                    "message": f"Invalid OpenAI Codex device auth token response: {jsonStringify(json)}",
                }
            return {
                "status": "complete",
                "value": _DeviceTokenSuccess(
                    authorizationCode=record["authorization_code"], codeVerifier=record["code_verifier"]
                ),
            }

        if response.status == 403 or response.status == 404:
            return {"status": "pending"}

        responseBody = await _responseText(response)
        errorCode: Any = None
        try:
            parsed = jsonParse(responseBody)
            error = parsed.get("error") if isinstance(parsed, dict) else None
            errorCode = error.get("code") if isinstance(error, dict) else error
        except Exception:  # noqa: BLE001, S110 - `catch {}`
            pass

        if errorCode == "deviceauth_authorization_pending":
            return {"status": "pending"}
        if errorCode == "slow_down":
            return {"status": "slow_down"}

        return {
            "status": "failed",
            "message": f"OpenAI Codex device auth failed with status {response.status}{f': {responseBody}' if responseBody else ''}",
        }

    return await pollOAuthDeviceCodeFlow(
        {
            "intervalSeconds": device.intervalSeconds,
            "expiresInSeconds": _DEVICE_CODE_TIMEOUT_SECONDS,
            "signal": signal,
            "poll": poll,
        }
    )


async def _createAuthorizationFlow(originator: str = "pi") -> dict[str, str]:
    pkce = await generatePKCE()
    verifier, challenge = pkce.verifier, pkce.challenge
    state = _createState()

    url = URL(_AUTHORIZE_URL)
    url.searchParams.set("response_type", "code")
    url.searchParams.set("client_id", _CLIENT_ID)
    url.searchParams.set("redirect_uri", _REDIRECT_URI)
    url.searchParams.set("scope", _SCOPE)
    url.searchParams.set("code_challenge", challenge)
    url.searchParams.set("code_challenge_method", "S256")
    url.searchParams.set("state", state)
    url.searchParams.set("id_token_add_organizations", "true")
    url.searchParams.set("codex_cli_simplified_flow", "true")
    url.searchParams.set("originator", originator)

    return {"verifier": verifier, "state": state, "url": url.toString()}


def _getAccountId(accessToken: str) -> str | None:
    payload = _decodeJwt(accessToken)
    auth = payload.get(_JWT_CLAIM_PATH) if isinstance(payload, dict) else None
    accountId = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
    return accountId if isinstance(accountId, str) and len(accountId) > 0 else None


def _credentialsFromToken(token: _OAuthToken) -> OAuthCredential:
    accountId = _getAccountId(token.access)
    if not accountId:
        raise RuntimeError("Failed to extract accountId from token")

    return OAuthCredential(
        type="oauth",
        access=token.access,
        refresh=token.refresh,
        expires=token.expires,
        accountId=accountId,
    )


async def _exchangeAuthorizationCodeForCredentials(
    code: str, verifier: str, redirectUri: str, signal: Any
) -> OAuthCredential:
    return _credentialsFromToken(await _exchangeAuthorizationCode(code, verifier, redirectUri, signal))


async def _loginOpenAICodexDeviceCode(interaction: ProviderAuthInteraction) -> OAuthCredential:
    device = await _startOpenAICodexDeviceAuth(interaction.signal)
    interaction.notify(
        DeviceCodeEvent(
            type="device_code",
            userCode=device.userCode,
            verificationUri=_DEVICE_VERIFICATION_URI,
            intervalSeconds=device.intervalSeconds,
            expiresInSeconds=_DEVICE_CODE_TIMEOUT_SECONDS,
        )
    )
    code = await _pollOpenAICodexDeviceAuth(device, interaction.signal)
    return await _exchangeAuthorizationCodeForCredentials(
        code.authorizationCode,
        code.codeVerifier,
        _DEVICE_REDIRECT_URI,
        interaction.signal,
    )


async def _loginOpenAICodex(interaction: ProviderAuthInteraction, options: LoginOptions | None = None) -> OAuthCredential:
    # `createAuthorizationFlow(options?.agentName)`: an undefined argument takes the default.
    flow = (
        await _createAuthorizationFlow(options.agentName)
        if options is not None and options.agentName is not None
        else await _createAuthorizationFlow()
    )
    verifier, state, url = flow["verifier"], flow["state"], flow["url"]

    async def complete(code: str) -> str:
        return code

    # Port 1455 is shared with the Codex CLI; when it is taken, fall back to the pasted redirect URL.
    try:
        callback = await startOAuthCallbackServer(
            OAuthCallbackServerOptions(
                providerName="OpenAI",
                host=_getCallbackHost(),
                port=1455,
                path="/auth/callback",
                state=state,
                complete=complete,
                signal=interaction.signal,
            )
        )
    except Exception:  # noqa: BLE001 - `.catch(() => undefined)`
        callback = None

    interaction.notify(
        AuthUrlEvent(
            type="auth_url",
            url=url,
            instructions="A browser window should open. Complete login to finish.",
        )
    )

    try:
        result = await waitForCallbackOrManualInput(
            interaction,
            callback,
            {
                "message": "Complete login in your browser, or paste the authorization code / redirect URL here:",
                "placeholder": _REDIRECT_URI,
            },
        )
        code: str | None
        if result["type"] == "callback":
            code = result["value"]
        else:
            parsed = _parseAuthorizationInput(result["input"])
            if parsed.get("state") and parsed.get("state") != state:
                raise RuntimeError("State mismatch")
            code = parsed.get("code")

        if not code:
            raise RuntimeError("Missing authorization code")
        return await _exchangeAuthorizationCodeForCredentials(code, verifier, _REDIRECT_URI, interaction.signal)
    finally:
        if callback is not None:
            callback.close()


async def _refreshOpenAICodexToken(refreshToken: str, signal: Any) -> OAuthCredential:
    """Refresh OpenAI Codex OAuth token"""
    return _credentialsFromToken(await _refreshAccessToken(refreshToken, signal))


async def _openaiCodexOAuth_login(
    interaction: ProviderAuthInteraction, options: LoginOptions | None = None
) -> OAuthCredential:
    method = await interaction.prompt(
        SelectPrompt(
            type="select",
            message="Select OpenAI Codex login method:",
            options=[
                SelectOption(id=_OPENAI_CODEX_BROWSER_LOGIN_METHOD, label="Browser login (default)"),
                SelectOption(id=_OPENAI_CODEX_DEVICE_CODE_LOGIN_METHOD, label="Device code login (headless)"),
            ],
        )
    )

    if method == _OPENAI_CODEX_DEVICE_CODE_LOGIN_METHOD:
        return await _loginOpenAICodexDeviceCode(interaction)
    if method != _OPENAI_CODEX_BROWSER_LOGIN_METHOD:
        raise RuntimeError(f"Unknown OpenAI Codex login method: {method}")

    return await _loginOpenAICodex(interaction, options)


async def _openaiCodexOAuth_toAuth(credential: OAuthCredential) -> ModelAuth:
    return ModelAuth(apiKey=credential.access)


openaiCodexOAuth = OAuthAuth(
    name="OpenAI (ChatGPT Plus/Pro)",
    isSubscription=True,
    login=_openaiCodexOAuth_login,
    refresh=lambda credential, signal: _refreshOpenAICodexToken(credential.refresh, signal),
    toAuth=_openaiCodexOAuth_toAuth,
)

__all__ = ["openaiCodexOAuth"]

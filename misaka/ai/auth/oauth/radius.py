"""Translated from pi's ``packages/ai/src/auth/oauth/radius.ts`` (v1.1.0).

Radius gateway OAuth flow.

Radius is a pi-messages gateway. OAuth client APIs live on the configured
gateway; only the interactive browser authorization endpoint is discovered.
Model catalog loading is owned by the Radius provider.

MISAKA fork (each is marked where it happens): ``fetch`` is ``_fetch.fetch``, and the login takes the ``options``
every login is called with.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, TypedDict

from misaka.ai.auth.oauth._fetch import Response, fetch
from misaka.ai.auth.oauth.callback_server import (
    OAuthCallbackServerOptions,
    startOAuthCallbackServer,
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
    ProgressEvent,
    ProviderAuthInteraction,
    SelectOption,
    SelectPrompt,
)
from misaka.ai.providers.radius_config import normalizeRadiusGatewayUrl
from misaka.utils.js import jsonParse
from misaka.utils.values import signal_aborted
from misaka.utils.whatwg_url import URL, URLSearchParams

_CALLBACK_HOST = "127.0.0.1"
_CALLBACK_PORT = 1456
_CALLBACK_PATH = "/oauth/callback"
_REDIRECT_URI = f"http://{_CALLBACK_HOST}:{_CALLBACK_PORT}{_CALLBACK_PATH}"
_TOKEN_EXPIRY_SKEW_MS = 60_000
_LOGIN_METHOD_BROWSER = "browser"
_LOGIN_METHOD_DEVICE_CODE = "device-code"
_OAUTH_CLIENT_ID = "pi-gateway"
_OAUTH_SCOPE = "gateway offline_access"
_OAUTH_DEVICE_CODE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"


class _RadiusOAuthDiscovery(TypedDict):
    authorizationEndpoint: str


class _DeviceAuthorizationResponse(TypedDict):
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: float
    interval: float | None


async def _loadRadiusOAuthDiscovery(gateway: str, signal: Any) -> _RadiusOAuthDiscovery:
    response = await fetch(
        URL("/v1/oauth", gateway),
        {
            "headers": {"accept": "application/json"},
            "signal": signal,
        },
    )

    if not response.ok:
        raise RuntimeError(f"Could not load Radius OAuth config from {gateway}: {response.status} {await response.text()}")

    discovery = await response.json()
    if not isinstance(discovery, dict) or not isinstance(discovery.get("authorizationEndpoint"), str):
        raise RuntimeError(f"Invalid Radius OAuth config from {gateway}")  # noqa: TRY004 - upstream throws Error
    return {"authorizationEndpoint": discovery["authorizationEndpoint"]}


class _OAuthResponseError(Exception):
    def __init__(self, status: int, oauthError: str | None, description: str | None, message: str) -> None:
        detail = (f"{oauthError}: {description}" if description else oauthError) if oauthError else description or str(status)
        super().__init__(f"{message}: {detail}")
        self.status = status
        self.oauthError = oauthError


async def _readOAuthResponseError(response: Response, message: str) -> _OAuthResponseError:
    try:
        text = await response.text()
    except Exception:  # noqa: BLE001 - `.catch(() => "")`
        text = ""
    oauthError: str | None = None
    description: str | None = None

    if text:
        try:
            data = jsonParse(text)
            oauthError = data.get("error") if isinstance(data, dict) and isinstance(data.get("error"), str) else None
            description = (
                data.get("error_description")
                if isinstance(data, dict) and isinstance(data.get("error_description"), str)
                else None
            )
        except Exception:  # noqa: BLE001 - not JSON: the text is the description
            description = text

    return _OAuthResponseError(response.status, oauthError, description, message)


async def _requestOAuthToken(gateway: str, body: URLSearchParams, signal: Any) -> OAuthCredential:
    try:
        response = await fetch(
            URL("/v1/oauth/token", gateway),
            {
                "method": "POST",
                "headers": {"accept": "application/json", "content-type": "application/x-www-form-urlencoded"},
                "body": body,
                "signal": signal,
            },
        )
    except Exception as error:
        if signal_aborted(signal):
            raise RuntimeError("Login cancelled") from error
        raise

    if not response.ok:
        raise await _readOAuthResponseError(response, "Radius OAuth token request failed")

    data = await response.json()

    return OAuthCredential(
        type="oauth",
        access=data.get("access_token"),
        refresh=data.get("refresh_token"),
        expires=time.time() * 1000 + data.get("expires_in") * 1000 - _TOKEN_EXPIRY_SKEW_MS,
        # `scope: data.scope`, which JSON drops when the response carried none.
        **({"scope": data["scope"]} if "scope" in data else {}),
    )


async def _loginWithBrowser(
    gateway: str,
    authorizationEndpoint: str,
    interaction: ProviderAuthInteraction,
) -> OAuthCredential:
    pkce = await generatePKCE()
    verifier, challenge = pkce.verifier, pkce.challenge
    state = str(uuid.uuid4())
    authorizeUrl = URL(authorizationEndpoint)
    authorizeUrl.search = URLSearchParams(
        {
            "response_type": "code",
            "client_id": _OAUTH_CLIENT_ID,
            "redirect_uri": _REDIRECT_URI,
            "scope": _OAUTH_SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "handoff": "url",
            "state": state,
        }
    ).toString()

    callback = await startOAuthCallbackServer(
        OAuthCallbackServerOptions(
            providerName="Radius",
            host=_CALLBACK_HOST,
            port=_CALLBACK_PORT,
            path=_CALLBACK_PATH,
            state=state,
            complete=lambda code: _requestOAuthToken(
                gateway,
                URLSearchParams(
                    {
                        "grant_type": "authorization_code",
                        "client_id": _OAUTH_CLIENT_ID,
                        "redirect_uri": _REDIRECT_URI,
                        "code": code,
                        "code_verifier": verifier,
                    }
                ),
                interaction.signal,
            ),
            signal=interaction.signal,
        )
    )
    interaction.notify(ProgressEvent(type="progress", message=f"Listening for OAuth callback on {_REDIRECT_URI}"))
    interaction.notify(
        AuthUrlEvent(
            type="auth_url",
            url=authorizeUrl.toString(),
            instructions="Continue in your browser.",
        )
    )

    try:
        credential = await callback.wait()
        if not credential:
            raise RuntimeError("OAuth callback did not complete.")
        return credential
    finally:
        callback.close()


async def _requestDeviceAuthorization(gateway: str, signal: Any) -> _DeviceAuthorizationResponse:
    try:
        response = await fetch(
            URL("/v1/oauth/device", gateway),
            {
                "method": "POST",
                "headers": {"accept": "application/json", "content-type": "application/x-www-form-urlencoded"},
                "body": URLSearchParams({"client_id": _OAUTH_CLIENT_ID, "scope": _OAUTH_SCOPE}),
                "signal": signal,
            },
        )
    except Exception as error:
        if signal_aborted(signal):
            raise RuntimeError("Login cancelled") from error
        raise

    if not response.ok:
        raise await _readOAuthResponseError(response, "Radius OAuth device authorization failed")

    data = await response.json()
    if not data.get("device_code") or not data.get("user_code") or not data.get("verification_uri") or not data.get("expires_in"):
        raise RuntimeError("Radius OAuth device authorization response is missing required fields")

    return {
        "device_code": data["device_code"],
        "user_code": data["user_code"],
        "verification_uri": data["verification_uri"],
        "expires_in": data["expires_in"],
        "interval": data.get("interval"),
    }


async def _loginWithDeviceCode(gateway: str, interaction: ProviderAuthInteraction) -> OAuthCredential:
    device = await _requestDeviceAuthorization(gateway, interaction.signal)
    interaction.notify(
        DeviceCodeEvent(
            type="device_code",
            userCode=device["user_code"],
            verificationUri=device["verification_uri"],
            intervalSeconds=device["interval"],
            expiresInSeconds=device["expires_in"],
        )
    )

    async def poll() -> Any:
        try:
            credentials = await _requestOAuthToken(
                gateway,
                URLSearchParams(
                    {
                        "grant_type": _OAUTH_DEVICE_CODE_GRANT_TYPE,
                        "client_id": _OAUTH_CLIENT_ID,
                        "device_code": device["device_code"],
                    }
                ),
                interaction.signal,
            )
            return {"status": "complete", "value": credentials}
        except _OAuthResponseError as error:
            match error.oauthError:
                case "authorization_pending":
                    return {"status": "pending"}
                case "slow_down":
                    return {"status": "slow_down"}
                case "expired_token":
                    return {"status": "failed", "message": "Device authorization expired."}
                case "access_denied":
                    return {"status": "failed", "message": "Device authorization was denied."}
                case _:
                    raise

    return await pollOAuthDeviceCodeFlow(
        {
            "intervalSeconds": device["interval"],
            "expiresInSeconds": device["expires_in"],
            "signal": interaction.signal,
            "poll": poll,
        }
    )


@dataclass(slots=True)
class RadiusOAuthOptions:
    name: str
    gateway: str


def createRadiusOAuth(options: RadiusOAuthOptions) -> OAuthAuth:
    gateway = normalizeRadiusGatewayUrl(options.gateway)

    async def login(interaction: ProviderAuthInteraction, _options: LoginOptions | None = None) -> OAuthCredential:
        # MISAKA fork: `_options` is accepted because every login is called with it.
        loginMethod = await interaction.prompt(
            SelectPrompt(
                type="select",
                message=f"Sign in to {options.name}:",
                options=[
                    SelectOption(id=_LOGIN_METHOD_BROWSER, label="Sign in with browser (recommended)"),
                    SelectOption(
                        id=_LOGIN_METHOD_DEVICE_CODE,
                        label="Sign in with device code (when signing in from another device)",
                    ),
                ],
            )
        )

        if loginMethod == _LOGIN_METHOD_DEVICE_CODE:
            return await _loginWithDeviceCode(gateway, interaction)
        if loginMethod == _LOGIN_METHOD_BROWSER:
            discovery = await _loadRadiusOAuthDiscovery(gateway, interaction.signal)
            return await _loginWithBrowser(gateway, discovery["authorizationEndpoint"], interaction)
        raise RuntimeError(f"Unknown {options.name} sign-in method: {loginMethod}")

    async def refresh(credential: OAuthCredential, signal: Any) -> OAuthCredential:
        refreshed = await _requestOAuthToken(
            gateway,
            URLSearchParams(
                {
                    "grant_type": "refresh_token",
                    "client_id": _OAUTH_CLIENT_ID,
                    "refresh_token": credential.refresh,
                }
            ),
            signal,
        )
        return refreshed

    async def toAuth(credential: OAuthCredential) -> ModelAuth:
        return ModelAuth(apiKey=credential.access)

    return OAuthAuth(
        name=options.name,
        login=login,
        refresh=refresh,
        toAuth=toAuth,
    )


__all__ = ["RadiusOAuthOptions", "createRadiusOAuth"]

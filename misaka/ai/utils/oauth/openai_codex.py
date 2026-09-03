"""OpenAI Codex OAuth helpers."""

from __future__ import annotations

import asyncio
import base64
import json
import math
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from misaka.ai.utils.oauth.device_code import poll_oauth_device_code_flow
from misaka.ai.utils.oauth.oauth_page import oauth_error_html, oauth_success_html
from misaka.ai.utils.oauth.pkce import generate_pkce
from misaka.ai.utils.oauth.types import (
    OAuthCredentials,
    OAuthDeviceCodeInfo,
    OAuthLoginCallbacks,
    OAuthSelectOption,
    OAuthSelectPrompt,
)
from misaka.ai.utils.provider_env import get_provider_env_value
from misaka.core.provider_attribution import CLIENT_NAME
from misaka.utils.values import signal_aborted

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTH_BASE_URL = "https://auth.openai.com"
AUTHORIZE_URL = f"{AUTH_BASE_URL}/oauth/authorize"
TOKEN_URL = f"{AUTH_BASE_URL}/oauth/token"
REDIRECT_URI = "http://localhost:1455/auth/callback"
DEVICE_USER_CODE_URL = f"{AUTH_BASE_URL}/api/accounts/deviceauth/usercode"
DEVICE_TOKEN_URL = f"{AUTH_BASE_URL}/api/accounts/deviceauth/token"
DEVICE_VERIFICATION_URI = f"{AUTH_BASE_URL}/codex/device"
# The device flow's authorization code is issued against OpenAI's own callback, not the
# loopback one the browser flow uses, so the exchange has to name that redirect instead.
DEVICE_REDIRECT_URI = f"{AUTH_BASE_URL}/deviceauth/callback"
DEVICE_CODE_TIMEOUT_SECONDS = 15 * 60
LOGIN_METHOD_BROWSER = "browser"
LOGIN_METHOD_DEVICE_CODE = "device_code"
SCOPE = "openid profile email offline_access"
_JWT_CLAIM_PATH = "https://api.openai.com/auth"


def get_callback_host() -> str:
    """Read per call, not once at import: upstream reads the variable at the call site,
    so a process that sets it after this module loads still gets the host it asked for."""
    return get_provider_env_value("MISAKA_OAUTH_CALLBACK_HOST") or "127.0.0.1"


def _create_state() -> str:
    return secrets.token_hex(16)


def _parse_authorization_input(input_text: str) -> dict[str, str | None]:
    value = input_text.strip()
    if not value:
        return {"code": None, "state": None}

    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
        if parsed.scheme and parsed.netloc and hostname and not any(character.isspace() for character in hostname):
            params = parse_qs(parsed.query)
            return {"code": params.get("code", [None])[0], "state": params.get("state", [None])[0]}
    except ValueError:
        pass

    if "#" in value:
        code, state = value.split("#", 1)
        return {"code": code, "state": state}

    if "code=" in value:
        params = parse_qs(value)
        return {"code": params.get("code", [None])[0], "state": params.get("state", [None])[0]}

    return {"code": value, "state": None}


def _decode_jwt(token: str) -> dict[str, Any] | None:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1]
        padding = "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload + padding)
        return json.loads(decoded)
    except ValueError:
        return None


async def _exchange_authorization_code(code: str, verifier: str, redirect_uri: str = REDIRECT_URI) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=None) as client:
        response = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
            },
        )

    if response.status_code >= 400:
        return {
            "type": "failed",
            "status": response.status_code,
            "message": f"OpenAI Codex token exchange failed ({response.status_code}): {response.text or response.reason_phrase}",
        }

    data = response.json()
    if not data.get("access_token") or not data.get("refresh_token") or not isinstance(data.get("expires_in"), (int, float)):
        return {"type": "failed", "message": f"OpenAI Codex token exchange response missing fields: {json.dumps(data)}"}

    return {
        "type": "success",
        "access": data["access_token"],
        "refresh": data["refresh_token"],
        "expires": int(time.time() * 1000) + data["expires_in"] * 1000,
    }


async def _refresh_access_token(refresh_token: str) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            response = await client.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": CLIENT_ID,
                },
            )
    except Exception as error:  # noqa: BLE001 - any refresh failure is reported as a failed credential
        return {"type": "failed", "message": f"OpenAI Codex token refresh error: {error}"}

    if response.status_code >= 400:
        return {
            "type": "failed",
            "status": response.status_code,
            "message": f"OpenAI Codex token refresh failed ({response.status_code}): {response.text or response.reason_phrase}",
        }

    data = response.json()
    if not data.get("access_token") or not data.get("refresh_token") or not isinstance(data.get("expires_in"), (int, float)):
        return {"type": "failed", "message": f"OpenAI Codex token refresh response missing fields: {json.dumps(data)}"}
    return {
        "type": "success",
        "access": data["access_token"],
        "refresh": data["refresh_token"],
        "expires": int(time.time() * 1000) + data["expires_in"] * 1000,
    }


async def _start_device_auth(signal: Any = None) -> dict[str, Any]:
    """Ask OpenAI for a user code the person types on another device."""
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            response = await client.post(DEVICE_USER_CODE_URL, json={"client_id": CLIENT_ID})
    except Exception as error:
        if signal_aborted(signal):
            raise RuntimeError("Login cancelled") from error
        raise

    if response.status_code >= 400:
        if response.status_code == 404:
            raise RuntimeError(
                "OpenAI Codex device code login is not enabled for this server. "
                "Use browser login or verify the server URL."
            )
        body = response.text or ""
        raise RuntimeError(
            f"OpenAI Codex device code request failed with status {response.status_code}"
            + (f": {body}" if body else "")
        )

    data = response.json()
    if not isinstance(data, dict):
        data = {}
    interval = data.get("interval")
    # The endpoint has been seen answering with the interval as a string; upstream coerces
    # rather than rejecting, because a usable code with an unparsed interval is still a
    # usable code.
    if isinstance(interval, str):
        try:
            interval = float(interval.strip())
        except ValueError:
            interval = None
    if (
        not data.get("device_auth_id")
        or not data.get("user_code")
        or not isinstance(interval, (int, float))
        or isinstance(interval, bool)
        or not math.isfinite(interval)
        or interval < 0
    ):
        raise RuntimeError(f"Invalid OpenAI Codex device code response: {json.dumps(data)}")

    return {
        "deviceAuthId": data["device_auth_id"],
        "userCode": data["user_code"],
        "intervalSeconds": interval,
    }


async def _poll_device_auth(device: dict[str, Any], signal: Any = None) -> dict[str, str]:
    """Wait for the person to approve, then take the authorization code they earned.

    Unlike every other device flow here, what comes back is not an access token: it is an
    authorization code plus the verifier OpenAI generated for it, which still has to be
    exchanged. That is why the poller carries a value rather than a token.
    """

    async def poll() -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                response = await client.post(
                    DEVICE_TOKEN_URL,
                    json={"device_auth_id": device["deviceAuthId"], "user_code": device["userCode"]},
                )
        except Exception as error:
            if signal_aborted(signal):
                raise RuntimeError("Login cancelled") from error
            raise

        if response.status_code < 400:
            data = response.json()
            if not isinstance(data, dict):
                data = {}
            if not data.get("authorization_code") or not data.get("code_verifier"):
                return {
                    "status": "failed",
                    "message": f"Invalid OpenAI Codex device auth token response: {json.dumps(data)}",
                }
            return {
                "status": "complete",
                "value": {
                    "authorizationCode": data["authorization_code"],
                    "codeVerifier": data["code_verifier"],
                },
            }

        # Before the user has typed the code the endpoint answers 403/404 rather than the
        # RFC's pending error, so these are waiting states, not failures.
        if response.status_code in (403, 404):
            return {"status": "pending"}

        body = response.text or ""
        error_code: Any = None
        try:
            parsed = json.loads(body)
            error = parsed.get("error") if isinstance(parsed, dict) else None
            error_code = error.get("code") if isinstance(error, dict) else error
        except ValueError:
            pass

        if error_code == "deviceauth_authorization_pending":
            return {"status": "pending"}
        if error_code == "slow_down":
            return {"status": "slow_down"}

        return {
            "status": "failed",
            "message": f"OpenAI Codex device auth failed with status {response.status_code}"
            + (f": {body}" if body else ""),
        }

    return await poll_oauth_device_code_flow(
        intervalSeconds=device["intervalSeconds"],
        expiresInSeconds=DEVICE_CODE_TIMEOUT_SECONDS,
        poll=poll,
        signal=signal,
    )


async def _credentials_from_exchange(code: str, verifier: str, redirect_uri: str) -> OAuthCredentials:
    result = await _exchange_authorization_code(code, verifier, redirect_uri)
    if result["type"] != "success":
        raise RuntimeError(result["message"])
    account_id = _get_account_id(result["access"])
    if not account_id:
        raise RuntimeError("Failed to extract accountId from token")
    return OAuthCredentials(
        access=result["access"],
        refresh=result["refresh"],
        expires=result["expires"],
        accountId=account_id,
    )


async def login_openai_codex_device_code(options: dict[str, Any]) -> OAuthCredentials:
    """Log in without a browser or a loopback port: the headless path.

    Without it a machine with no browser and no way to reach localhost:1455 -- a container,
    an SSH session, a CI runner -- has no way to sign in to Codex at all.
    """
    signal = options.get("signal")
    device = await _start_device_auth(signal)
    options["onDeviceCode"](
        OAuthDeviceCodeInfo(
            userCode=device["userCode"],
            verificationUri=DEVICE_VERIFICATION_URI,
            intervalSeconds=device["intervalSeconds"],
            expiresInSeconds=DEVICE_CODE_TIMEOUT_SECONDS,
        )
    )
    approved = await _poll_device_auth(device, signal)
    return await _credentials_from_exchange(
        approved["authorizationCode"], approved["codeVerifier"], DEVICE_REDIRECT_URI
    )


async def _create_authorization_flow(originator: str = CLIENT_NAME) -> dict[str, str]:
    pkce = await generate_pkce()
    verifier = pkce.verifier
    challenge = pkce.challenge
    state = _create_state()
    url = f"{AUTHORIZE_URL}?{urlencode({'response_type': 'code', 'client_id': CLIENT_ID, 'redirect_uri': REDIRECT_URI, 'scope': SCOPE, 'code_challenge': challenge, 'code_challenge_method': 'S256', 'state': state, 'id_token_add_organizations': 'true', 'codex_cli_simplified_flow': 'true', 'originator': originator})}"
    return {"verifier": verifier, "state": state, "url": url}


class _OAuthServerInfo:
    def __init__(
        self,
        server: asyncio.base_events.Server | None,
        future: asyncio.Future[dict[str, str] | None],
    ) -> None:
        self._server = server
        self._future = future

    def cancelWait(self) -> None:
        if not self._future.done():
            self._future.set_result(None)

    async def waitForCode(self) -> dict[str, str] | None:
        return await self._future

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()


async def _start_local_oauth_server(state: str) -> _OAuthServerInfo:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[dict[str, str] | None] = loop.create_future()

    def _status_line(status: int) -> str:
        reason = {
            200: "OK",
            400: "Bad Request",
            404: "Not Found",
        }.get(status, "OK")
        return f"HTTP/1.1 {status} {reason}\r\n"

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await reader.readline()
            path = request_line.decode("utf-8", "ignore").split(" ")[1]
            while True:
                line = await reader.readline()
                if not line or line in {b"\r\n", b"\n"}:
                    break
            parsed = urlparse(path)
            params = parse_qs(parsed.query)
            status = 200
            body = ""
            # Settled after the response is written: see the Anthropic flow for why.
            declined: Exception | None = None
            if parsed.path != "/auth/callback":
                status = 404
                body = oauth_error_html("Callback route not found.")
            elif params.get("error", [None])[0]:
                # As in the Anthropic flow: declining on the consent screen comes back here,
                # and pi 0.84.4 renders the page without settling, so the terminal waits for
                # a code the user already refused to give. Only an explicit `error` ends the
                # wait; anything else arriving on this port may not kill a live login.
                status = 400
                reason = params["error"][0]
                body = oauth_error_html("OpenAI authentication did not complete.", f"Error: {reason}")
                declined = RuntimeError(f"OpenAI authentication did not complete: {reason}")
            elif params.get("state", [None])[0] != state:
                status = 400
                body = oauth_error_html("State mismatch.")
            elif not params.get("code", [None])[0]:
                status = 400
                body = oauth_error_html("Missing authorization code.")
            else:
                body = oauth_success_html("OpenAI authentication completed. You can close this window.")
                if not future.done():
                    future.set_result({"code": params["code"][0]})
            response = (
                f"{_status_line(status)}"
                "Content-Type: text/html; charset=utf-8\r\n"
                f"Content-Length: {len(body.encode('utf-8'))}\r\n"
                "Connection: close\r\n\r\n"
                f"{body}"
            )
            writer.write(response.encode("utf-8"))
            await writer.drain()
            if declined is not None and not future.done():
                future.set_exception(declined)
        except Exception:  # noqa: BLE001 - a failing callback response must still get a 500 page
            body = oauth_error_html("Internal error while processing OAuth callback.")
            response = (
                "HTTP/1.1 500 Internal Server Error\r\n"
                "Content-Type: text/html; charset=utf-8\r\n"
                f"Content-Length: {len(body.encode('utf-8'))}\r\n"
                "Connection: close\r\n\r\n"
                f"{body}"
            )
            writer.write(response.encode("utf-8"))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    try:
        server = await asyncio.start_server(handler, get_callback_host(), 1455)
    except OSError:
        future.set_result(None)
        return _OAuthServerInfo(None, future)
    return _OAuthServerInfo(server, future)


def _get_account_id(access_token: str) -> str | None:
    payload = _decode_jwt(access_token)
    auth = payload.get(_JWT_CLAIM_PATH) if payload else None
    account_id = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
    return account_id if isinstance(account_id, str) and account_id else None


async def login_openai_codex(options: dict[str, Any]) -> OAuthCredentials:
    auth_flow = await _create_authorization_flow(options.get("originator", CLIENT_NAME))
    verifier = auth_flow["verifier"]
    state = auth_flow["state"]
    url = auth_flow["url"]
    server = await _start_local_oauth_server(state)
    options["onAuth"]({"url": url, "instructions": "A browser window should open. Complete login to finish."})

    code: str | None = None
    try:
        if options.get("onManualCodeInput") is not None:
            manual_code: str | None = None
            manual_error: Exception | None = None

            async def manual_worker() -> None:
                nonlocal manual_code, manual_error
                try:
                    manual_code = await options["onManualCodeInput"]()
                except BaseException as error:  # noqa: BLE001
                    manual_error = error if isinstance(error, Exception) else RuntimeError(str(error))
                finally:
                    server.cancelWait()

            manual_task = asyncio.create_task(manual_worker())
            result = await server.waitForCode()
            if manual_error is not None:
                raise manual_error
            if result and result.get("code"):
                code = result["code"]
            elif manual_code:
                parsed = _parse_authorization_input(manual_code)
                if parsed["state"] and parsed["state"] != state:
                    raise RuntimeError("State mismatch")
                code = parsed["code"]

            if not code:
                await manual_task
                if manual_error is not None:
                    raise manual_error
                if manual_code:
                    parsed = _parse_authorization_input(manual_code)
                    if parsed["state"] and parsed["state"] != state:
                        raise RuntimeError("State mismatch")
                    code = parsed["code"]
        else:
            result = await server.waitForCode()
            if result and result.get("code"):
                code = result["code"]

        if not code:
            input_text = await options["onPrompt"]({"message": "Paste the authorization code (or full redirect URL):"})
            parsed = _parse_authorization_input(input_text)
            if parsed["state"] and parsed["state"] != state:
                raise RuntimeError("State mismatch")
            code = parsed["code"]

        if not code:
            raise RuntimeError("Missing authorization code")

        token_result = await _exchange_authorization_code(code, verifier)
        if token_result["type"] != "success":
            raise RuntimeError(token_result["message"])

        account_id = _get_account_id(token_result["access"])
        if not account_id:
            raise RuntimeError("Failed to extract accountId from token")

        return OAuthCredentials(
            access=token_result["access"],
            refresh=token_result["refresh"],
            expires=token_result["expires"],
            accountId=account_id,
        )
    finally:
        await server.close()


async def refresh_openai_codex_token(refresh_token: str) -> OAuthCredentials:
    result = await _refresh_access_token(refresh_token)
    if result["type"] != "success":
        raise RuntimeError(result["message"])
    account_id = _get_account_id(result["access"])
    if not account_id:
        raise RuntimeError("Failed to extract accountId from token")
    return OAuthCredentials(
        access=result["access"],
        refresh=result["refresh"],
        expires=result["expires"],
        accountId=account_id,
    )


class _OpenAICodexOAuthProvider:
    id = "openai-codex"
    name = "ChatGPT Plus/Pro (Codex Subscription)"
    usesCallbackServer = True

    async def login(self, callbacks: OAuthLoginCallbacks) -> OAuthCredentials:
        method = await callbacks.onSelect(
            OAuthSelectPrompt(
                message="Select OpenAI Codex login method:",
                options=[
                    OAuthSelectOption(id=LOGIN_METHOD_BROWSER, label="Browser login (default)"),
                    OAuthSelectOption(id=LOGIN_METHOD_DEVICE_CODE, label="Device code login (headless)"),
                ],
            )
        )

        if method == LOGIN_METHOD_DEVICE_CODE:
            return await login_openai_codex_device_code(
                {"onDeviceCode": callbacks.onDeviceCode, "signal": getattr(callbacks, "signal", None)}
            )
        # `None` means the person backed out of the picker, a state upstream's prompt does
        # not have. It is a cancelled login, not an unknown method.
        if method is None:
            raise RuntimeError("Login cancelled")
        if method != LOGIN_METHOD_BROWSER:
            raise RuntimeError(f"Unknown OpenAI Codex login method: {method}")

        return await login_openai_codex(
            {
                "onAuth": callbacks.onAuth,
                "onPrompt": callbacks.onPrompt,
                "onProgress": callbacks.onProgress,
                "onManualCodeInput": callbacks.onManualCodeInput,
            }
        )

    async def refreshToken(self, credentials: OAuthCredentials, signal: Any | None = None) -> OAuthCredentials:
        del signal  # the built-in refresh has no cancellation point; the parameter is the upstream contract
        return await refresh_openai_codex_token(credentials.refresh)

    def getApiKey(self, credentials: OAuthCredentials) -> str:
        return credentials.access


openai_codex_oauth_provider = _OpenAICodexOAuthProvider()

loginOpenAICodex = login_openai_codex
loginOpenAICodexDeviceCode = login_openai_codex_device_code
refreshOpenAICodexToken = refresh_openai_codex_token
openaiCodexOAuthProvider = openai_codex_oauth_provider

__all__ = [
    "loginOpenAICodex",
    "loginOpenAICodexDeviceCode",
    "login_openai_codex",
    "login_openai_codex_device_code",
    "openaiCodexOAuthProvider",
    "openai_codex_oauth_provider",
    "refreshOpenAICodexToken",
    "refresh_openai_codex_token",
]

"""OpenAI Codex OAuth helpers."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from misaka.ai.utils.oauth.oauth_page import oauth_error_html, oauth_success_html
from misaka.ai.utils.oauth.pkce import generate_pkce
from misaka.ai.utils.oauth.types import OAuthCredentials, OAuthLoginCallbacks
from misaka.core.provider_attribution import CLIENT_NAME

CALLBACK_HOST = os.environ.get("MISAKA_OAUTH_CALLBACK_HOST", "127.0.0.1")
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
REDIRECT_URI = "http://localhost:1455/auth/callback"
SCOPE = "openid profile email offline_access"
_JWT_CLAIM_PATH = "https://api.openai.com/auth"


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
            if parsed.path != "/auth/callback":
                status = 404
                body = oauth_error_html("Callback route not found.")
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
        server = await asyncio.start_server(handler, CALLBACK_HOST, 1455)
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
refreshOpenAICodexToken = refresh_openai_codex_token
openaiCodexOAuthProvider = openai_codex_oauth_provider

__all__ = [
    "loginOpenAICodex",
    "login_openai_codex",
    "openaiCodexOAuthProvider",
    "openai_codex_oauth_provider",
    "refreshOpenAICodexToken",
    "refresh_openai_codex_token",
]

"""Anthropic OAuth flow helpers."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import traceback
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from misaka.ai.utils.oauth.oauth_page import oauth_error_html, oauth_success_html
from misaka.ai.utils.oauth.pkce import generate_pkce
from misaka.ai.utils.oauth.types import OAuthCredentials, OAuthLoginCallbacks

CLIENT_ID = base64.b64decode("OWQxYzI1MGEtZTYxYi00NGQ5LTg4ZWQtNTk0NGQxOTYyZjVl").decode("utf-8")
AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CALLBACK_HOST = os.environ.get("MISAKA_OAUTH_CALLBACK_HOST", "127.0.0.1")
CALLBACK_PORT = 53692
CALLBACK_PATH = "/callback"
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}"
SCOPES = (
    "org:create_api_key user:profile user:inference user:sessions:claude_code "
    "user:mcp_servers user:file_upload"
)


@dataclass(slots=True)
class _CallbackServerInfo:
    server: asyncio.base_events.Server
    redirect_uri: str
    future: asyncio.Future[dict[str, str] | None]

    def cancel_wait(self) -> None:
        if not self.future.done():
            self.future.set_result(None)

    async def wait_for_code(self) -> dict[str, str] | None:
        return await self.future

    async def close(self) -> None:
        self.server.close()
        await self.server.wait_closed()


def _parse_authorization_input(input_text: str) -> dict[str, str | None]:
    value = input_text.strip()
    if not value:
        return {"code": None, "state": None}

    try:
        parsed = urlparse(value)
        if parsed.scheme and parsed.netloc:
            params = parse_qs(parsed.query)
            return {
                "code": params.get("code", [None])[0],
                "state": params.get("state", [None])[0],
            }
    except Exception:
        pass

    if "#" in value:
        code, state = value.split("#", 1)
        return {"code": code, "state": state}

    if "code=" in value:
        params = parse_qs(value)
        return {
            "code": params.get("code", [None])[0],
            "state": params.get("state", [None])[0],
        }

    return {"code": value, "state": None}


def _format_error_details(error: Any) -> str:
    if isinstance(error, Exception):
        name = getattr(error, "name", error.__class__.__name__)
        details = [f"{name}: {error}"]
        code = getattr(error, "code", None)
        errno = getattr(error, "errno", None)
        cause = getattr(error, "cause", None)
        if cause is None:
            cause = getattr(error, "__cause__", None)
        stack = getattr(error, "stack", None)
        if code:
            details.append(f"code={code}")
        if errno is not None:
            details.append(f"errno={errno}")
        if cause is not None:
            details.append(f"cause={_format_error_details(cause)}")
        if isinstance(stack, str) and stack:
            details.append(f"stack={stack}")
        elif error.__traceback__ is not None:
            details.append(f"stack={''.join(traceback.format_exception(type(error), error, error.__traceback__)).rstrip()}")
        return "; ".join(details)
    return str(error)


async def _start_callback_server(expected_state: str) -> _CallbackServerInfo:
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
            if parsed.path != CALLBACK_PATH:
                status = 404
                body = oauth_error_html("Callback route not found.")
            elif "error" in params:
                status = 400
                body = oauth_error_html("Anthropic authentication did not complete.", f"Error: {params['error'][0]}")
            elif not params.get("code") or not params.get("state"):
                status = 400
                body = oauth_error_html("Missing code or state parameter.")
            elif params["state"][0] != expected_state:
                status = 400
                body = oauth_error_html("State mismatch.")
            else:
                body = oauth_success_html("Anthropic authentication completed. You can close this window.")
                if not future.done():
                    future.set_result({"code": params["code"][0], "state": params["state"][0]})

            response = (
                f"{_status_line(status)}"
                "Content-Type: text/html; charset=utf-8\r\n"
                f"Content-Length: {len(body.encode('utf-8'))}\r\n"
                "Connection: close\r\n\r\n"
                f"{body}"
            )
            writer.write(response.encode("utf-8"))
            await writer.drain()
        except Exception:
            response = (
                "HTTP/1.1 500 Internal Server Error\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n"
                "Content-Length: 14\r\n"
                "Connection: close\r\n\r\n"
                "Internal error"
            )
            writer.write(response.encode("utf-8"))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handler, CALLBACK_HOST, CALLBACK_PORT)
    return _CallbackServerInfo(server=server, redirect_uri=REDIRECT_URI, future=future)


async def _post_json(url: str, body: dict[str, str | int]) -> str:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, headers={"Content-Type": "application/json", "Accept": "application/json"}, json=body)
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP request failed. status={response.status_code}; url={url}; body={response.text}")
    return response.text


async def _exchange_authorization_code(code: str, state: str, verifier: str, redirect_uri: str) -> OAuthCredentials:
    try:
        response_body = await _post_json(
            TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": code,
                "state": state,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            },
        )
    except Exception as error:
        raise RuntimeError(
            f"Token exchange request failed. url={TOKEN_URL}; redirect_uri={redirect_uri}; "
            f"response_type=authorization_code; details={_format_error_details(error)}"
        ) from error

    try:
        token_data = json.loads(response_body)
    except Exception as error:
        raise RuntimeError(
            f"Token exchange returned invalid JSON. url={TOKEN_URL}; body={response_body}; details={_format_error_details(error)}"
        ) from error

    return OAuthCredentials(
        refresh=token_data["refresh_token"],
        access=token_data["access_token"],
        expires=int(time.time() * 1000) + int(token_data["expires_in"]) * 1000 - 5 * 60 * 1000,
    )


async def login_anthropic(options: dict[str, Any]) -> OAuthCredentials:
    pkce = await generate_pkce()
    verifier = pkce.verifier
    challenge = pkce.challenge
    server = await _start_callback_server(verifier)

    code: str | None = None
    state: str | None = None
    redirect_uri_for_exchange = REDIRECT_URI

    try:
        auth_params = urlencode(
            {
                "code": "true",
                "client_id": CLIENT_ID,
                "response_type": "code",
                "redirect_uri": REDIRECT_URI,
                "scope": SCOPES,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": verifier,
            }
        )
        options["onAuth"](
            {
                "url": f"{AUTHORIZE_URL}?{auth_params}",
                "instructions": (
                    "Complete login in your browser. If the browser is on another machine, "
                    "paste the final redirect URL here."
                ),
            }
        )

        if options.get("onManualCodeInput") is not None:
            manual_input: str | None = None
            manual_error: BaseException | None = None

            async def manual_worker() -> None:
                nonlocal manual_input, manual_error
                try:
                    manual_input = await options["onManualCodeInput"]()
                except Exception as error:
                    manual_error = error
                finally:
                    server.cancel_wait()

            manual_task = asyncio.create_task(manual_worker())
            result = await server.wait_for_code()

            if manual_error is not None:
                raise manual_error

            if result and result.get("code"):
                code = result["code"]
                state = result["state"]
            elif manual_input:
                parsed = _parse_authorization_input(manual_input)
                if parsed["state"] and parsed["state"] != verifier:
                    raise RuntimeError("OAuth state mismatch")
                code = parsed["code"]
                state = parsed["state"] if parsed["state"] is not None else verifier

            if not code:
                await manual_task
                if manual_error is not None:
                    raise manual_error
                if manual_input:
                    parsed = _parse_authorization_input(manual_input)
                    if parsed["state"] and parsed["state"] != verifier:
                        raise RuntimeError("OAuth state mismatch")
                    code = parsed["code"]
                    state = parsed["state"] if parsed["state"] is not None else verifier
        else:
            result = await server.wait_for_code()
            if result and result.get("code"):
                code = result["code"]
                state = result["state"]

        if not code:
            input_text = await options["onPrompt"]({"message": "Paste the authorization code or full redirect URL:", "placeholder": REDIRECT_URI})
            parsed = _parse_authorization_input(input_text)
            if parsed["state"] and parsed["state"] != verifier:
                raise RuntimeError("OAuth state mismatch")
            code = parsed["code"]
            state = parsed["state"] if parsed["state"] is not None else verifier

        if not code:
            raise RuntimeError("Missing authorization code")
        if not state:
            raise RuntimeError("Missing OAuth state")

        if options.get("onProgress") is not None:
            options["onProgress"]("Exchanging authorization code for tokens...")
        return await _exchange_authorization_code(code, state, verifier, redirect_uri_for_exchange)
    finally:
        await server.close()


async def refresh_anthropic_token(refresh_token: str) -> OAuthCredentials:
    try:
        response_body = await _post_json(
            TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": refresh_token,
            },
        )
    except Exception as error:
        raise RuntimeError(f"Anthropic token refresh request failed. url={TOKEN_URL}; details={_format_error_details(error)}") from error

    try:
        data = json.loads(response_body)
    except Exception as error:
        raise RuntimeError(
            f"Anthropic token refresh returned invalid JSON. url={TOKEN_URL}; body={response_body}; details={_format_error_details(error)}"
        ) from error

    return OAuthCredentials(
        refresh=data["refresh_token"],
        access=data["access_token"],
        expires=int(time.time() * 1000) + int(data["expires_in"]) * 1000 - 5 * 60 * 1000,
    )


class _AnthropicOAuthProvider:
    id = "anthropic"
    name = "Anthropic (Claude Pro/Max)"
    usesCallbackServer = True

    async def login(self, callbacks: OAuthLoginCallbacks) -> OAuthCredentials:
        return await login_anthropic(
            {
                "onAuth": callbacks.onAuth,
                "onPrompt": callbacks.onPrompt,
                "onProgress": callbacks.onProgress,
                "onManualCodeInput": callbacks.onManualCodeInput,
            }
        )

    async def refreshToken(self, credentials: OAuthCredentials) -> OAuthCredentials:
        return await refresh_anthropic_token(credentials.refresh)

    def getApiKey(self, credentials: OAuthCredentials) -> str:
        return credentials.access


anthropic_oauth_provider = _AnthropicOAuthProvider()

loginAnthropic = login_anthropic
refreshAnthropicToken = refresh_anthropic_token
anthropicOAuthProvider = anthropic_oauth_provider

__all__ = [
    "anthropicOAuthProvider",
    "anthropic_oauth_provider",
    "loginAnthropic",
    "login_anthropic",
    "refreshAnthropicToken",
    "refresh_anthropic_token",
]

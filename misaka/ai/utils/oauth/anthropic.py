"""Anthropic OAuth flow helpers."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import secrets
import time
import traceback
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from misaka.ai.utils.abort import wait_for_abort
from misaka.ai.utils.oauth.oauth_page import oauth_error_html, oauth_success_html
from misaka.ai.utils.oauth.pkce import generate_pkce
from misaka.ai.utils.oauth.types import (
    OAuthAuthInfo,
    OAuthCredentials,
    OAuthLoginCallbacks,
    OAuthPrompt,
)
from misaka.utils.values import signal_aborted

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
# Upstream's own wording for the paste box (auth/oauth/anthropic.ts): the box is the
# browser flow's second answer, not a fallback, so it says so.
MANUAL_PROMPT_MESSAGE = (
    "Complete login in your browser, or paste the authorization code / redirect URL here:"
)
CANCEL_MESSAGE = "Login cancelled"


@dataclass(slots=True)
class _CallbackServerInfo:
    server: asyncio.base_events.Server
    redirect_uri: str
    future: asyncio.Future[dict[str, str] | None]
    # Upstream registers `interaction.signal.addEventListener("abort", () =>
    # server.cancelWait())` before it starts waiting (auth/oauth/anthropic.ts:192-195).
    # Without it, a caller that offers no paste box -- `ai/cli.py` passes
    # `onManualCodeInput = None` -- has nothing that can end `wait_for_code`, and a
    # cancelled login keeps waiting for a redirect nobody will send. Watched on a task
    # rather than a listener, as `openrouter.py` and `radius.py` already do, because
    # misaka's signals are duck-typed rather than DOM events.
    aborting: asyncio.Task[None] | None = None

    def watch_abort(self, signal: Any) -> None:
        if signal is not None and self.aborting is None:
            self.aborting = asyncio.ensure_future(self._watch_abort(signal))

    async def _watch_abort(self, signal: Any) -> None:
        await wait_for_abort(signal)
        self.cancel_wait()

    def cancel_wait(self) -> None:
        if not self.future.done():
            self.future.set_result(None)

    async def wait_for_code(self) -> dict[str, str] | None:
        return await self.future

    async def close(self) -> None:
        if self.aborting is not None and not self.aborting.done():
            self.aborting.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.aborting
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
    except ValueError:
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


async def _start_callback_server(expected_state: str, signal: Any = None) -> _CallbackServerInfo:
    """The local page the browser is redirected back to.

    MISAKA fork of pi 0.84.4 ``auth/oauth/anthropic.js``: there, a callback carrying
    ``error`` renders the failure page and returns without settling the wait, so declining
    on Anthropic's consent screen leaves the login pending forever -- the browser says no,
    the terminal keeps waiting, and the dialog looks frozen. The wait is ended here
    instead. Only an explicit ``error`` ends it: a 404, a missing parameter or a stale
    state is some other request arriving on this port, and must not kill a live login.
    """
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
            # Settled after the response is written, never before: ending the wait resumes
            # the login, whose `finally` closes this server out from under the page the
            # browser is still waiting for.
            declined: Exception | None = None
            if parsed.path != CALLBACK_PATH:
                status = 404
                body = oauth_error_html("Callback route not found.")
            elif "error" in params:
                status = 400
                reason = params["error"][0]
                body = oauth_error_html("Anthropic authentication did not complete.", f"Error: {reason}")
                declined = RuntimeError(f"Anthropic authentication did not complete: {reason}")
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
            if declined is not None and not future.done():
                future.set_exception(declined)
        except Exception:  # noqa: BLE001 - a failing callback response must still get a 500 page
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
    info = _CallbackServerInfo(server=server, redirect_uri=REDIRECT_URI, future=future)
    info.watch_abort(signal)
    return info


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
    expected_state = secrets.token_hex(16)
    signal = options.get("signal")
    server = await _start_callback_server(expected_state, signal)

    code: str | None = None
    state: str | None = None
    redirect_uri_for_exchange = REDIRECT_URI
    # The paste-box worker, cancelled with the server: whichever of the two answers first,
    # the other is waiting on something nobody will deliver.
    manual_task: asyncio.Task[None] | None = None

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
                "state": expected_state,
            }
        )
        # The declared payload, not a bare dict: `types.py` says `Callable[[OAuthAuthInfo],
        # None]`, and a caller that reads it by attribute -- `ai/cli.py` prints `info.url` --
        # got an AttributeError before the login had drawn anything. `openrouter.py` and
        # `radius.py` already pass the object.
        options["onAuth"](
            OAuthAuthInfo(
                url=f"{AUTHORIZE_URL}?{auth_params}",
                instructions=(
                    "Complete login in your browser. If the browser is on another machine, "
                    "paste the final redirect URL here."
                ),
            )
        )

        if options.get("onManualCodeInput") is not None:
            manual_input: str | None = None
            manual_error: BaseException | None = None

            async def manual_worker() -> None:
                nonlocal manual_input, manual_error
                try:
                    manual_input = await options["onManualCodeInput"](
                        OAuthPrompt(message=MANUAL_PROMPT_MESSAGE, placeholder=REDIRECT_URI)
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - captured and re-raised on the main path
                    manual_error = error
                finally:
                    server.cancel_wait()

            manual_task = asyncio.create_task(manual_worker())
            result = await server.wait_for_code()   # may raise: the browser said no

            if manual_error is not None:
                raise manual_error

            # The wait can end three ways: the browser answered, the paste box answered,
            # or the login was cancelled. Only the first two have anything left to do.
            # Without this, a cancel that releases the callback wait falls through to
            # `await manual_task` and waits on a box the person has already dismissed --
            # the hang the release is supposed to end. `radius.py` guards the same spot.
            if signal_aborted(signal):
                raise RuntimeError(CANCEL_MESSAGE)

            if result and result.get("code"):
                code = result["code"]
                state = result["state"]
            elif manual_input:
                parsed = _parse_authorization_input(manual_input)
                if parsed["state"] and parsed["state"] != expected_state:
                    raise RuntimeError("OAuth state mismatch")
                code = parsed["code"]
                state = parsed["state"] if parsed["state"] is not None else expected_state

            if not code:
                await manual_task
                if manual_error is not None:
                    raise manual_error
                if manual_input:
                    parsed = _parse_authorization_input(manual_input)
                    if parsed["state"] and parsed["state"] != expected_state:
                        raise RuntimeError("OAuth state mismatch")
                    code = parsed["code"]
                    state = parsed["state"] if parsed["state"] is not None else expected_state
        else:
            result = await server.wait_for_code()
            if result and result.get("code"):
                code = result["code"]
                state = result["state"]
            if not code and signal_aborted(signal):
                raise RuntimeError(CANCEL_MESSAGE)
            if not code:
                # Only on this branch. The paste box the caller already put on screen is the
                # one upstream asks through, and asking a second time stacks a second box
                # under the first -- both live, the first still holding the cursor. Upstream
                # raises here instead (auth/oauth/anthropic.ts), and so does `openrouter.py`;
                # `onPrompt` stays for the caller that offered no box at all.
                input_text = await options["onPrompt"](
                    OAuthPrompt(message=MANUAL_PROMPT_MESSAGE, placeholder=REDIRECT_URI)
                )
                parsed = _parse_authorization_input(input_text)
                if parsed["state"] and parsed["state"] != expected_state:
                    raise RuntimeError("OAuth state mismatch")
                code = parsed["code"]
                state = parsed["state"] if parsed["state"] is not None else expected_state

        if not code:
            raise RuntimeError("Missing authorization code")
        if not state:
            raise RuntimeError("Missing OAuth state")

        if options.get("onProgress") is not None:
            options["onProgress"]("Exchanging authorization code for tokens...")
        return await _exchange_authorization_code(code, state, verifier, redirect_uri_for_exchange)
    finally:
        # Upstream aborts a second controller to retire the paste box once the callback has
        # answered; cancelling the task that awaits it is the same handoff for a callbacks
        # record that takes no signal. Awaited, so the worker's own `finally` has run before
        # the server closes underneath it.
        if manual_task is not None and not manual_task.done():
            manual_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await manual_task
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
                "signal": getattr(callbacks, "signal", None),
            }
        )

    async def refreshToken(self, credentials: OAuthCredentials, signal: Any | None = None) -> OAuthCredentials:
        del signal  # the built-in refresh has no cancellation point; the parameter is the upstream contract
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

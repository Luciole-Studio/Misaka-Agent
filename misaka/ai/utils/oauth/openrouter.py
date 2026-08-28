"""OpenRouter OAuth PKCE flow, translated from pi's ``auth/oauth/openrouter.ts``.

OpenRouter exchanges an authorization code for a permanent, user-controlled API key
rather than an expiring access/refresh token pair. That is why the credential written
here carries an empty ``refresh`` and an expiry of ``MAX_SAFE_INTEGER``: there is nothing
to rotate, so ``refreshToken`` hands the stored credential straight back and the refresh
window in ``oauth_credentials_expire_soon`` never opens.

The callback is handled by a one-shot loopback server on an ephemeral port, raced against
a manual paste prompt so a headless or remote session can finish the login by pasting the
final redirect URL. The random path segment in the callback URL is what upstream uses in
place of a ``state`` parameter -- upstream sends no ``state`` on the authorize URL
(``openrouter.ts:252-256``).

Deliberate departures from the TypeScript, none of which change behaviour:

* ``AbortSignal`` event listeners have no counterpart here. misaka passes duck-typed
  signals (an ``aborted`` flag, optionally an awaitable ``wait()``), so the abort is
  raced rather than subscribed to, and the manual prompt is cancelled by cancelling its
  task instead of by aborting a second controller. Upstream's ``addEventListener`` reacts
  the instant the abort fires (``openrouter.ts:217-218``); a signal carrying only the flag
  has nothing to subscribe to, so ``ai/utils/abort.wait_for_abort`` re-reads it every
  ``FLAG_POLL_INTERVAL_S`` (0.05s). Both waits below go through that one helper rather
  than a copy of it.
* The token-exchange body below is serialised with ``separators=(",", ":")``.
* Upstream's ``prompt({type: "manual_code"})`` is one call, retired by aborting a second
  controller. misaka splits the same question into ``onManualCodeInput`` (raced against
  the callback, retired by cancelling its task) and ``onPrompt`` (asked only after the
  callback wait has handed the login over). ``onPrompt`` is deliberately *not* raced:
  of the three ``onPrompt`` implementations in this repo, two go through a UI
  (``ui/tui/interactive/interactive_mode.py:3739``, ``ai/auth/oauth_bridge.py``), but the
  third -- ``ai/cli.py:19`` -- is ``asyncio.to_thread(input, ...)``, and cancelling that task
  cancels only the await: the non-daemon executor thread stays blocked in ``input()`` and
  ``asyncio.run`` then wedges in ``shutdown_default_executor``. (Checked by hand: a script
  that cancels an ``asyncio.to_thread`` over a long ``sleep`` returns from ``main`` and then
  never exits.) So a login that *succeeded* through the browser would hang the process.
  ``ai/utils/oauth/anthropic.py`` splits the two channels the same way.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from misaka.ai.utils.abort import race_with_abort_signal, wait_for_abort
from misaka.ai.utils.oauth.oauth_page import oauth_error_html, oauth_success_html
from misaka.ai.utils.oauth.pkce import generate_pkce
from misaka.ai.utils.oauth.types import (
    OAuthAuthInfo,
    OAuthCredentials,
    OAuthLoginCallbacks,
    OAuthPrompt,
)
from misaka.ai.utils.provider_env import get_provider_env_value
from misaka.utils.values import signal_aborted

AUTHORIZE_URL = "https://openrouter.ai/auth"
TOKEN_URL = "https://openrouter.ai/api/v1/auth/keys"
LOGIN_TIMEOUT_MS = 5 * 60 * 1000
TOKEN_EXCHANGE_TIMEOUT_MS = 30_000

# JavaScript's Number.MAX_SAFE_INTEGER, kept verbatim so a credential written by either
# project reads as the same "never expires" to the other.
MAX_SAFE_INTEGER = 9007199254740991

CANCEL_MESSAGE = "Login cancelled"
LOGIN_TIMEOUT_MESSAGE = "OpenRouter OAuth login timed out"
EXCHANGE_TIMEOUT_MESSAGE = "OpenRouter OAuth token exchange timed out"
INVALID_JSON_MESSAGE = "OpenRouter OAuth returned invalid JSON"
MISSING_KEY_MESSAGE = 'OpenRouter OAuth response carries no "key"'
MISSING_CODE_MESSAGE = "Missing authorization code"
AUTH_INSTRUCTIONS = (
    "Complete sign-in in your browser. If the browser is on another machine, "
    "paste the final redirect URL here."
)
MANUAL_PROMPT_MESSAGE = (
    "Complete sign-in in your browser, or paste the authorization code / redirect URL here:"
)
EXCHANGING_MESSAGE = "Exchanging authorization code for an API key..."

_STATUS_REASONS = {
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    409: "Conflict",
    502: "Bad Gateway",
}


def _callback_host() -> str:
    """Read per call, not at import: a test or a launcher may set this after import."""
    return get_provider_env_value("MISAKA_OAUTH_CALLBACK_HOST") or "127.0.0.1"


def _parse_authorization_input(input_text: str) -> str | None:
    """Upstream's ``parseAuthorizationInput``: a redirect URL, a query fragment, or a bare code.

    ``urlparse`` is not ``new URL``, and the two disagree on these inputs:

    * ``https:/host?code=x`` -- ``new URL`` normalises the single slash and reads the code;
      ``urlparse`` leaves ``netloc`` empty and this yields nothing, so the login reports
      MISSING_CODE_MESSAGE instead of redeeming the paste.
    * ``http://host:99999/?code=x`` -- ``new URL`` throws on the out-of-range port and gives
      up on the code; ``urlparse`` never validates the port and still reads it. More
      permissive, harmlessly: the code is redeemed against this login's own PKCE verifier.
    * Leading and trailing whitespace is stripped first, as upstream does, so it never
      reaches either parser.

    Input that parses as neither a URL nor a query string is returned as-is, on the
    assumption that it is the code itself -- so a typo like ``htttps:/foo`` is redeemed as
    a code and rejected by the token endpoint rather than caught here.
    """
    value = input_text.strip()
    if not value:
        return None

    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc:
        # A redirect URL that carries no `code` yields nothing rather than falling through
        # to the bare-code branch below.
        return parse_qs(parsed.query).get("code", [None])[0]

    if "code=" in value:
        # `URLSearchParams` drops a leading `?`; `parse_qs` does not, and would read the
        # first key as `"?code"`. Pasting the query string straight off the address bar is
        # the normal thing to do on a machine with no browser to catch the redirect.
        return parse_qs(value.removeprefix("?")).get("code", [None])[0]

    return value


def _error_detail(body: dict[str, Any]) -> str | None:
    """Upstream's error-shape probe, in upstream's order."""
    description = body.get("error_description")
    if isinstance(description, str):
        return description
    message = body.get("message")
    if isinstance(message, str):
        return message
    error = body.get("error")
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        nested = error.get("message")
        if isinstance(nested, str):
            return nested
    return None


async def _post_json(url: str, body: str, headers: dict[str, str], *, transport: Any = None) -> tuple[int, str]:
    """POST a pre-encoded body and return ``(status, text)``.

    The body arrives already encoded because the caller owns its exact bytes, and because
    the seam is what tests assert on. Not because httpx would spread the JSON out: on the
    pinned 0.28.1 `httpx._content.encode_json` serialises with `separators=(",", ":")` too,
    so a `json=` dict would come out compact as well.
    """
    async with httpx.AsyncClient(timeout=TOKEN_EXCHANGE_TIMEOUT_MS / 1000, transport=transport) as client:
        response = await client.post(url, content=body, headers=headers)
    return response.status_code, response.text


async def _exchange_authorization_code(
    code: str,
    verifier: str,
    signal: Any = None,
    *,
    post: Any = _post_json,
) -> OAuthCredentials:
    if signal_aborted(signal):
        raise RuntimeError(CANCEL_MESSAGE)

    payload = json.dumps(
        {"code": code, "code_verifier": verifier, "code_challenge_method": "S256"},
        separators=(",", ":"),
    )
    try:
        status, text = await post(
            TOKEN_URL,
            payload,
            {"accept": "application/json", "content-type": "application/json"},
        )
    except httpx.TimeoutException as error:
        # Upstream checks the caller's signal before its own timeout controller, so a
        # login cancelled while the request was in flight reports as a cancel either way.
        if signal_aborted(signal):
            raise RuntimeError(CANCEL_MESSAGE) from error
        raise RuntimeError(EXCHANGE_TIMEOUT_MESSAGE) from error
    except Exception as error:
        if signal_aborted(signal):
            raise RuntimeError(CANCEL_MESSAGE) from error
        raise

    # Upstream wires the caller's signal into fetch's own AbortController (openrouter.ts:86-92),
    # so an abort tears the request down and the flow ends in "Login cancelled". `post` here is
    # a plain awaitable that cannot be torn down, so the check is re-done once it returns: without
    # this, a request that completed after the cancel would be treated as a successful login and
    # its key written to the credential store.
    if signal_aborted(signal):
        raise RuntimeError(CANCEL_MESSAGE)

    ok = 200 <= status < 300
    body: dict[str, Any] = {}
    try:
        parsed = json.loads(text)
    except ValueError as error:
        # A failed parse on a successful response is a broken endpoint; on an error
        # response it just means the detail is unavailable, which upstream tolerates.
        if ok:
            raise RuntimeError(INVALID_JSON_MESSAGE) from error
    else:
        if isinstance(parsed, dict):
            body = parsed

    if not ok:
        detail = _error_detail(body)
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"OpenRouter OAuth key exchange failed (HTTP {status}){suffix}")

    key = body.get("key")
    if not isinstance(key, str) or not key:
        raise RuntimeError(MISSING_KEY_MESSAGE)

    return OAuthCredentials(refresh="", access=key, expires=MAX_SAFE_INTEGER)


async def _exchange_racing_abort(exchange: Any, code: str, verifier: str, signal: Any) -> OAuthCredentials:
    """Run the key exchange, but stop waiting for it the moment the login is cancelled.

    Upstream's abort reaches inside the request (openrouter.ts:86-92,109-112); here the
    exchange is opaque, so it is raced instead -- otherwise a cancel on the manual-paste path
    is felt only after TOKEN_EXCHANGE_TIMEOUT_MS. A failure that lands while the signal is
    already aborted reports as a cancel, which is upstream's own precedence (openrouter.ts:110,
    where `signal.aborted` is tested before the timeout controller's own abort).

    The signal goes in as-is: ``race_with_abort_signal`` handles every shape this repo
    passes, including the flag-only signal the interactive session actually sends down
    (``ui/tui/components/cancellable_loader.AbortSignal``), which it re-reads on an
    interval rather than awaiting.
    """
    if signal_aborted(signal):
        raise RuntimeError(CANCEL_MESSAGE)
    try:
        return await race_with_abort_signal(exchange(code, verifier, signal), signal)
    # Catching `Exception` on purpose: every failure is re-raised, only its wording changes
    # when the login was cancelled. (No BLE001 noqa -- checked with `ruff check --isolated
    # --select BLE`: a handler whose every branch re-raises is not flagged.)
    except Exception as error:
        if signal_aborted(signal):
            raise RuntimeError(CANCEL_MESSAGE) from error
        raise


@dataclass(frozen=True, slots=True)
class _CallbackDecision:
    """What one inbound callback request means, decided before any I/O."""

    action: Literal["ignore", "fail", "exchange"]
    status: int | None = None
    body: str | None = None
    code: str | None = None
    message: str | None = None


def _decide_callback(method: str, target: str, callback_path: str, *, used: bool) -> _CallbackDecision:
    parsed = urlparse(target)
    if method != "GET" or parsed.path != callback_path:
        return _CallbackDecision(
            action="ignore", status=404, body=oauth_error_html("OAuth callback route not found.")
        )
    if used:
        return _CallbackDecision(
            action="ignore", status=409, body=oauth_error_html("This OAuth callback has already been used.")
        )

    params = parse_qs(parsed.query)
    oauth_error = params.get("error", [None])[0]
    if oauth_error:
        description = params.get("error_description", [None])[0] or oauth_error
        return _CallbackDecision(
            action="fail",
            status=400,
            body=oauth_error_html("OpenRouter authorization was denied.", description),
            message=f"OpenRouter authorization failed: {description}",
        )

    code = params.get("code", [None])[0]
    if not code:
        # Left un-settled on purpose: a stray hit without a code must not end a login
        # that the real callback can still complete.
        return _CallbackDecision(
            action="ignore", status=400, body=oauth_error_html("OpenRouter returned no authorization code.")
        )

    return _CallbackDecision(action="exchange", code=code)


def _http_response(status: int, body: str) -> bytes:
    encoded = body.encode("utf-8")
    reason = _STATUS_REASONS.get(status, "OK")
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        "content-type: text/html; charset=utf-8\r\n"
        "cache-control: no-store\r\n"
        f"content-length: {len(encoded)}\r\n"
        "connection: close\r\n\r\n"
    )
    return head.encode("utf-8") + encoded


def _discard_future_outcome(future: asyncio.Future[Any]) -> None:
    """Read a settled future nobody awaits any more, so asyncio stays quiet about it."""
    if not future.cancelled():
        future.exception()


class _CallbackServer:
    """The one-shot loopback server, with its request handling separable from its socket.

    ``handle`` takes a reader/writer pair rather than owning the accept loop so the
    branch table above can be exercised without ever binding a port.
    """

    def __init__(
        self,
        *,
        callback_path: str,
        verifier: str,
        signal: Any = None,
        exchange: Any = _exchange_authorization_code,
    ) -> None:
        self.callback_path = callback_path
        self.callbackUrl = ""
        self._verifier = verifier
        self._signal = signal
        self._exchange = exchange
        self._future: asyncio.Future[OAuthCredentials | None] = asyncio.get_running_loop().create_future()
        self._claimed = False
        self._server: Any = None
        self._timer: asyncio.TimerHandle | None = None

    def _finish(self, credential: OAuthCredentials | None) -> None:
        if self._future.done():
            return
        self.close()
        self._future.set_result(credential)

    def _fail(self, error: Exception) -> None:
        if self._future.done():
            return
        self.close()
        self._future.set_exception(error)

    def attach(self, listener: Any, callback_url: str) -> None:
        """Adopt the bound listener, so ``close`` has something to stop."""
        self._server = listener
        self.callbackUrl = callback_url

    def arm_timeout(self, delay_seconds: float) -> None:
        self._timer = asyncio.get_running_loop().call_later(
            delay_seconds, self._fail, RuntimeError(LOGIN_TIMEOUT_MESSAGE)
        )

    def close(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._server is not None:
            self._server.close()
            self._server = None
        # After close nobody awaits the credential, but a callback already mid-exchange
        # can still fail into it; retrieve that so it is not reported as unhandled.
        self._future.add_done_callback(_discard_future_outcome)

    def cancel_wait(self) -> None:
        # A claimed callback is already exchanging its code: let that exchange settle the
        # login rather than handing it to the manual paste.
        if not self._claimed:
            self._finish(None)

    async def wait_for_credential(self) -> OAuthCredentials | None:
        if signal_aborted(self._signal):
            raise RuntimeError(CANCEL_MESSAGE)
        # `wait_for_abort` covers all three shapes that reach here: a signal with `wait()`,
        # a bare `aborted` flag (re-read on an interval), and `None` -- which waits forever,
        # leaving the callback future as the only thing that can settle the race.
        aborting = asyncio.ensure_future(wait_for_abort(self._signal))
        try:
            await asyncio.wait({self._future, aborting}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            aborting.cancel()
        if self._future.done():
            return self._future.result()
        raise RuntimeError(CANCEL_MESSAGE)

    async def handle(self, reader: Any, writer: Any) -> None:
        try:
            request_line = (await reader.readline()).decode("utf-8", "ignore")
            parts = request_line.split(" ")
            method = parts[0].strip() if parts else ""
            target = parts[1] if len(parts) > 1 else "/"
            while True:
                line = await reader.readline()
                if not line or line in {b"\r\n", b"\n"}:
                    break

            decision = _decide_callback(
                method, target, self.callback_path, used=self._claimed or self._future.done()
            )
            if decision.action == "exchange":
                self._claimed = True
                try:
                    credential = await self._exchange(decision.code, self._verifier, self._signal)
                except Exception as error:  # noqa: BLE001 - reported to the browser and to the login
                    message = str(error) or "Unknown token exchange error"
                    writer.write(_http_response(502, oauth_error_html("OpenRouter key exchange failed.", message)))
                    await writer.drain()
                    self._fail(error if isinstance(error, Exception) else RuntimeError(message))
                    return
                writer.write(
                    _http_response(
                        200, oauth_success_html("Signed in to OpenRouter. You may now close this page.")
                    )
                )
                await writer.drain()
                self._finish(credential)
                return

            writer.write(_http_response(decision.status or 400, decision.body or ""))
            await writer.drain()
            if decision.action == "fail":
                self._fail(RuntimeError(decision.message or "OpenRouter authorization failed"))
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


async def _start_callback_server(callback_path: str, verifier: str, signal: Any = None) -> _CallbackServer:
    if signal_aborted(signal):
        raise RuntimeError(CANCEL_MESSAGE)
    host = _callback_host()
    server = _CallbackServer(callback_path=callback_path, verifier=verifier, signal=signal)
    listener = await asyncio.start_server(server.handle, host, 0)
    sockets = listener.sockets or ()
    if not sockets:
        listener.close()
        raise RuntimeError("Could not determine the OpenRouter OAuth callback port")
    port = sockets[0].getsockname()[1]
    server.attach(listener, f"http://{host}:{port}{callback_path}")
    server.arm_timeout(LOGIN_TIMEOUT_MS / 1000)
    return server


def _build_authorize_url(callback_url: str, challenge: str) -> str:
    # No `state`: upstream sends none either (openrouter.ts:252-256), and the unguessable
    # callback path plays that role instead.
    query = urlencode(
        {
            "callback_url": callback_url,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


async def _prompt_for_manual_input(options: dict[str, Any], placeholder: str) -> str | None:
    """Ask ``onPrompt`` for the code, once the callback wait has handed the login over.

    Never raced against the callback: see the module docstring -- the repo's CLI ``onPrompt``
    blocks an executor thread in ``input()``, and cancelling the task that awaits it leaves
    that thread alive to wedge interpreter shutdown. ``anthropic.py`` reaches for ``onPrompt``
    at the same point.

    Called from ``login_openrouter`` below, on the branch where the callback wait returned
    no credential and no ``onManualCodeInput`` was offered.
    """
    prompt = options.get("onPrompt")
    if prompt is None:
        return None
    return await prompt(OAuthPrompt(message=MANUAL_PROMPT_MESSAGE, placeholder=placeholder))


def _notify_progress(options: dict[str, Any], message: str) -> None:
    progress = options.get("onProgress")
    if progress is not None:
        progress(message)


async def login_openrouter(
    options: dict[str, Any],
    *,
    start_server: Any = _start_callback_server,
    exchange: Any = _exchange_authorization_code,
) -> OAuthCredentials:
    """Run the OpenRouter login and return the credential holding the issued API key."""
    pkce = await generate_pkce()
    signal = options.get("signal")
    callback_path = f"/oauth/callback/{uuid.uuid4()}"
    server = await start_server(callback_path, pkce.verifier, signal)

    manual_input: str | None = None
    manual_error: BaseException | None = None
    manual_task: asyncio.Task[None] | None = None

    try:
        _notify_progress(options, f"Listening for OpenRouter OAuth callback on {server.callbackUrl}")
        authorize_url = _build_authorize_url(server.callbackUrl, pkce.challenge)
        options["onAuth"](OAuthAuthInfo(url=authorize_url, instructions=AUTH_INSTRUCTIONS))

        manual = options.get("onManualCodeInput")

        async def manual_worker() -> None:
            nonlocal manual_input, manual_error
            try:
                manual_input = await manual()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - re-raised on the main path
                manual_error = error
            finally:
                server.cancel_wait()

        # Only `onManualCodeInput` is raced against the callback. `onPrompt` is asked below,
        # after the wait has handed the login over, because a cancelled `onPrompt` task leaves
        # its `input()` thread running and the process cannot exit -- module docstring, and
        # `anthropic.py` for the same split.
        if manual is not None:
            manual_task = asyncio.create_task(manual_worker())

        credential = await server.wait_for_credential()
        if manual_error is not None:
            raise manual_error
        if credential is not None:
            return credential

        if manual_task is not None:
            await manual_task
            if manual_error is not None:
                raise manual_error
        else:
            manual_input = await _prompt_for_manual_input(options, server.callbackUrl)

        code = _parse_authorization_input(manual_input) if manual_input else None
        if not code:
            raise RuntimeError(MISSING_CODE_MESSAGE)
        _notify_progress(options, EXCHANGING_MESSAGE)
        # Raced against the signal: upstream's abort reaches into the request itself, so a
        # cancel here must not be swallowed for up to TOKEN_EXCHANGE_TIMEOUT_MS, and must not
        # end in a credential the caller has already given up on.
        return await _exchange_racing_abort(exchange, code, pkce.verifier, signal)
    finally:
        # Upstream aborts a second controller to retire the manual prompt; cancelling the
        # task that awaits it is the same handoff for a callbacks record that takes no signal.
        if manual_task is not None and not manual_task.done():
            manual_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await manual_task
        server.close()


class _OpenRouterOAuthProvider:
    id = "openrouter"
    name = "OpenRouter OAuth"
    loginLabel = "Sign in with OpenRouter"
    usesCallbackServer = True
    modifyModels = None

    async def login(self, callbacks: OAuthLoginCallbacks) -> OAuthCredentials:
        return await login_openrouter(
            {
                "onAuth": callbacks.onAuth,
                "onPrompt": getattr(callbacks, "onPrompt", None),
                "onProgress": getattr(callbacks, "onProgress", None),
                "onManualCodeInput": getattr(callbacks, "onManualCodeInput", None),
                "signal": getattr(callbacks, "signal", None),
            }
        )

    async def refreshToken(self, credentials: OAuthCredentials, signal: Any | None = None) -> OAuthCredentials:
        del signal  # nothing to cancel: the key is permanent, so this never leaves the process
        return credentials

    def getApiKey(self, credentials: OAuthCredentials) -> str:
        # The exchange stores OpenRouter's issued API key in `access`; upstream's `toAuth`
        # reads the same field, so a credential written by either side works in both.
        return credentials.access


openrouter_oauth_provider = _OpenRouterOAuthProvider()

loginOpenRouter = login_openrouter
openrouterOAuthProvider = openrouter_oauth_provider

__all__ = [
    "loginOpenRouter",
    "login_openrouter",
    "openrouterOAuthProvider",
    "openrouter_oauth_provider",
]

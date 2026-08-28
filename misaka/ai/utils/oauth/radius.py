"""Radius gateway OAuth flow, translated from pi's ``auth/oauth/radius.ts``.

Radius is a pi-messages *gateway*, not a single vendor: the OAuth client endpoints live on
whichever gateway the provider is configured for (``/v1/oauth``, ``/v1/oauth/token``,
``/v1/oauth/device``), and only the interactive browser authorization endpoint is
discovered at login time. That is why this module exports a *factory*
(``create_radius_oauth``) as well as a default flow object: upstream's
``loadRadiusOAuth({name, gateway})`` is parameterised, and a second gateway means a second
flow, not a second copy of this file.

Two sign-in methods, chosen by the user:

* **browser** -- PKCE authorization code against the discovered endpoint, with a one-shot
  loopback listener on ``127.0.0.1:1456`` catching the redirect.
* **device code** -- RFC 8628, for signing in from another machine.

What the translation had to change, and why:

* **The callback listener.** Upstream uses ``node:http`` behind a lazy import guarded on
  ``process.versions``; misaka is Python, so ``asyncio.start_server`` binds the port and
  the request handling lives in ``_RadiusCallbackServer.handle``, which takes a
  reader/writer pair. Binding and deciding are separate so the decision table can be
  tested without a socket, and ``login_with_browser`` takes the *starter*
  (``start_callback_server=``) as a parameter, so a test substitutes the whole listener
  and never binds at all.
* **Abort.** Upstream registers ``signal.addEventListener("abort", ...)``. misaka's
  signals are duck-typed -- some carry an awaitable ``wait()``, the one production passes
  down carries only an ``aborted`` flag -- so ``wait_for_abort`` is watched in a task that
  ``close()`` cancels. Same effect: an aborted login stops waiting for a callback that is
  never coming.
* **The select prompt.** ``interaction.prompt({type: "select"})`` becomes
  ``callbacks.onSelect``, which can answer ``None`` for "the user backed out" -- a state
  upstream's prompt does not have. It is treated as a cancelled login rather than as an
  unknown method, because that is what it means.
* **Token responses are checked.** Upstream casts the JSON body to its expected shape and
  a missing ``access_token`` becomes ``undefined`` inside a stored credential. Here the
  three fields are required by name, so a broken gateway fails at the response instead of
  at the next request with a token that reads ``None``.

The HTTP calls are parameters with defaults, so every branch here is reachable in a test
without a socket, a browser or a real sleep.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import httpx

from misaka.ai.providers.radius_config import (
    DEFAULT_RADIUS_GATEWAY,
    RadiusHttpResponse,
    normalize_radius_gateway_url,
)
from misaka.ai.utils.abort import race_with_abort_signal, wait_for_abort
from misaka.ai.utils.oauth.device_code import poll_oauth_device_code_flow
from misaka.ai.utils.oauth.oauth_page import oauth_error_html, oauth_success_html
from misaka.ai.utils.oauth.pkce import generate_pkce
from misaka.ai.utils.oauth.types import (
    OAuthAuthInfo,
    OAuthCredentials,
    OAuthDeviceCodeInfo,
    OAuthLoginCallbacks,
    OAuthSelectOption,
    OAuthSelectPrompt,
)
from misaka.utils.values import signal_aborted

# Loopback only, and not overridable, because these three constants are used twice over:
# `start_radius_callback_server` binds them and `_build_authorize_url` sends the same
# `REDIRECT_URI` to the gateway. Moving the bind without moving what is sent would leave
# the listener waiting on an address no callback is aimed at. Upstream fixes them the same
# way (radius.ts:26-29).
CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 1456
CALLBACK_PATH = "/oauth/callback"
REDIRECT_URI = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}{CALLBACK_PATH}"
TOKEN_EXPIRY_SKEW_MS = 60_000
LOGIN_METHOD_BROWSER = "browser"
LOGIN_METHOD_DEVICE_CODE = "device-code"
OAUTH_CLIENT_ID = "pi-gateway"
OAUTH_SCOPE = "gateway offline_access"
OAUTH_DEVICE_CODE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
DISCOVERY_PATH = "/v1/oauth"
TOKEN_PATH = "/v1/oauth/token"
DEVICE_PATH = "/v1/oauth/device"
CANCEL_MESSAGE = "Login cancelled"
# Upstream passes no timeout and leaves it to `fetch`. httpx does have one of its own --
# `httpx.AsyncClient().timeout` is `Timeout(timeout=5.0)` on the pinned 0.28.1 -- so this
# widens that 5s rather than adding a bound where there was none. A bound is still needed:
# the abort race below stops the *wait* without cancelling the request, so the timeout is
# what eventually closes the abandoned socket. 30s matches `xai.py` and `anthropic.py`.
REQUEST_TIMEOUT_MS = 30 * 1000

_STATUS_REASONS = {200: "OK", 400: "Bad Request", 404: "Not Found"}

PostForm = Callable[[str, dict[str, str], Any], Awaitable[RadiusHttpResponse]]
GetJson = Callable[[str, dict[str, str], Any], Awaitable[RadiusHttpResponse]]


class OAuthResponseError(RuntimeError):
    """A non-2xx OAuth response, carrying the ``error`` code the poller branches on."""

    def __init__(self, status: int, oauthError: str | None, description: str | None, message: str) -> None:
        if oauthError:
            detail = f"{oauthError}: {description}" if description else oauthError
        else:
            detail = description or str(status)
        super().__init__(f"{message}: {detail}")
        self.status = status
        self.oauthError = oauthError


def _read_oauth_response_error(response: RadiusHttpResponse, message: str) -> OAuthResponseError:
    """Upstream's ``readOAuthResponseError``: JSON error fields if present, else the text."""
    oauthError: str | None = None
    description: str | None = None
    if response.body:
        try:
            data = json.loads(response.body)
            if data is None:
                # `JSON.parse("null")` succeeds and the property read on `null` throws,
                # landing upstream in the same catch a non-JSON body lands in: the whole
                # text becomes the description.
                raise TypeError
        except (ValueError, TypeError):
            description = response.body
        else:
            # Anything else that is not an object (an array, a number) reads its
            # properties as `undefined` upstream, which is both fields left unset.
            if isinstance(data, dict):
                error = data.get("error")
                error_description = data.get("error_description")
                oauthError = error if isinstance(error, str) else None
                description = error_description if isinstance(error_description, str) else None
    return OAuthResponseError(response.status, oauthError, description, message)


async def _default_get_json(url: str, headers: dict[str, str], signal: Any) -> RadiusHttpResponse:
    async def request() -> RadiusHttpResponse:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_MS / 1000) as client:
            response = await client.get(url, headers=headers)
        return RadiusHttpResponse(status=response.status_code, body=response.text)

    return await race_with_abort_signal(request(), signal)


async def _default_post_form(url: str, fields: dict[str, str], signal: Any) -> RadiusHttpResponse:
    async def request() -> RadiusHttpResponse:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_MS / 1000) as client:
            response = await client.post(
                url,
                headers={"accept": "application/json", "content-type": "application/x-www-form-urlencoded"},
                # `urlencode` percent-encodes with `+` for spaces, matching
                # `new URLSearchParams(fields)` for the space-separated scope.
                content=urlencode(fields).encode(),
            )
        return RadiusHttpResponse(status=response.status_code, body=response.text)

    return await race_with_abort_signal(request(), signal)


def _now_ms() -> int:
    """``Date.now()``; a seam tests can freeze."""
    return int(time.time() * 1000)


def _gateway_url(gateway: str, path: str) -> str:
    """``new URL(path, gateway)``: an absolute path replaces the gateway's own.

    ``urljoin`` agrees with ``new URL`` on every well-formed absolute base, but not on a
    malformed one: ``new URL`` throws where ``urljoin`` quietly returns a host-less string
    like ``/v1/config``, which then goes out as a request to nowhere. The base is checked
    so a bad gateway fails at configuration rather than at an unexplained request.
    """
    parsed = urlparse(gateway)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Radius gateway must be an absolute URL: {gateway!r}")
    return urljoin(gateway, path)


async def load_radius_oauth_discovery(
    gateway: str,
    signal: Any = None,
    *,
    get_json: GetJson | None = None,
) -> str:
    """The gateway's interactive authorization endpoint."""
    send = get_json or _default_get_json
    response = await send(_gateway_url(gateway, DISCOVERY_PATH), {"accept": "application/json"}, signal)
    if not response.ok:
        raise RuntimeError(
            f"Could not load Radius OAuth config from {gateway}: {response.status} {response.body}"
        )
    try:
        discovery = json.loads(response.body)
    except ValueError:
        discovery = None
    endpoint = discovery.get("authorizationEndpoint") if isinstance(discovery, dict) else None
    if not isinstance(endpoint, str):
        # A gateway that answers with the wrong shape is a broken gateway, not a caller
        # type error; upstream throws a plain `Error` here too.
        raise RuntimeError(f"Invalid Radius OAuth config from {gateway}")  # noqa: TRY004 - a bad response is a runtime failure
    return endpoint


def _required_string(body: Any, field: str, message: str) -> str:
    value = body.get(field) if isinstance(body, dict) else None
    if not isinstance(value, str) or not value:
        raise RuntimeError(message)
    return value


async def request_oauth_token(
    gateway: str,
    fields: dict[str, str],
    signal: Any = None,
    *,
    post_form: PostForm | None = None,
) -> OAuthCredentials:
    """POST the token endpoint and turn the response into a stored credential."""
    send = post_form or _default_post_form
    try:
        response = await send(_gateway_url(gateway, TOKEN_PATH), fields, signal)
    except Exception as error:
        # Upstream distinguishes "the fetch failed because we aborted it" from a real
        # transport failure; `race_with_abort_signal` raises for both, so the flag decides.
        if signal_aborted(signal):
            raise RuntimeError(CANCEL_MESSAGE) from error
        raise

    if not response.ok:
        raise _read_oauth_response_error(response, "Radius OAuth token request failed")

    try:
        data = json.loads(response.body)
    except ValueError as error:
        raise RuntimeError(f"Radius OAuth token response is not JSON (HTTP {response.status})") from error

    missing = "Radius OAuth token response is missing required fields"
    access = _required_string(data, "access_token", missing)
    refresh = _required_string(data, "refresh_token", missing)
    expires_in = data.get("expires_in") if isinstance(data, dict) else None
    if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)) or not math.isfinite(expires_in):
        raise RuntimeError(missing)

    scope = data.get("scope")
    # `OAuthCredentials` allows extras, which is how upstream's optional `scope` survives
    # the round trip through storage; a missing one is left off rather than stored as None,
    # because `undefined` properties do not survive `JSON.stringify` either.
    extra = {"scope": scope} if isinstance(scope, str) else {}
    return OAuthCredentials(
        refresh=refresh,
        access=access,
        expires=int(_now_ms() + expires_in * 1000 - TOKEN_EXPIRY_SKEW_MS),
        **extra,
    )


def _http_response(status: int, body: str) -> bytes:
    encoded = body.encode("utf-8")
    reason = _STATUS_REASONS.get(status, "OK")
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        "content-type: text/html; charset=utf-8\r\n"
        f"content-length: {len(encoded)}\r\n"
        "connection: close\r\n\r\n"
    )
    return head.encode("utf-8") + encoded


@dataclass(slots=True)
class _CallbackDecision:
    """What one inbound request means, decided before any I/O."""

    status: int
    body: str
    # "wait" leaves the login running, "cancel" ends it with no code, "code" completes it.
    outcome: str
    code: str | None = None


def decide_radius_callback(target: str, expected_state: str) -> _CallbackDecision:
    """Upstream's request handler as a pure function, in upstream's order.

    The state check comes *before* the ``error`` check on purpose: that is upstream's
    order, and it means a callback carrying someone else's state is rejected as a
    mismatch even when it also carries an error.

    No method check, also on purpose: upstream's ``node:http`` handler answers any method
    on the callback path, and narrowing it here would silently change which callbacks
    complete a login.
    """
    parsed = urlparse(target)
    if parsed.path != CALLBACK_PATH:
        return _CallbackDecision(404, oauth_error_html("Callback route not found."), "wait")

    params = parse_qs(parsed.query)
    if params.get("state", [None])[0] != expected_state:
        return _CallbackDecision(400, oauth_error_html("OAuth state mismatch."), "wait")

    error = params.get("error", [None])[0]
    if error:
        description = params.get("error_description", [None])[0] or error
        return _CallbackDecision(400, oauth_error_html(description), "cancel")

    code = params.get("code", [None])[0]
    if not code:
        return _CallbackDecision(400, oauth_error_html("Missing authorization code."), "wait")

    return _CallbackDecision(
        200,
        oauth_success_html("Signed in to Radius. You may now close this page."),
        "code",
        code=code,
    )


class _RadiusCallbackServer:
    """The one-shot loopback listener, with its request handling separable from its socket."""

    def __init__(self, expected_state: str, signal: Any = None) -> None:
        self._expected_state = expected_state
        self._future: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
        self._listener: Any = None
        self._aborting: asyncio.Task[None] | None = None
        if signal is not None:
            self._aborting = asyncio.ensure_future(self._watch_abort(signal))

    async def _watch_abort(self, signal: Any) -> None:
        await wait_for_abort(signal)
        self._finish(None)

    def _finish(self, code: str | None) -> None:
        if not self._future.done():
            self._future.set_result(code)

    def attach(self, listener: Any) -> None:
        self._listener = listener

    async def wait_for_code(self) -> str | None:
        return await self._future

    def close(self) -> None:
        # Upstream's `close()` settles the wait first and then stops the server, so a
        # caller that gave up is not left awaiting a callback nobody will send.
        self._finish(None)
        if self._aborting is not None:
            self._aborting.cancel()
            self._aborting = None
        if self._listener is not None:
            self._listener.close()
            self._listener = None

    async def handle(self, reader: Any, writer: Any) -> None:
        try:
            request_line = (await reader.readline()).decode("utf-8", "ignore")
            parts = request_line.split(" ")
            target = parts[1] if len(parts) > 1 else "/"
            while True:
                line = await reader.readline()
                if not line or line in {b"\r\n", b"\n"}:
                    break

            decision = decide_radius_callback(target, self._expected_state)
            writer.write(_http_response(decision.status, decision.body))
            await writer.drain()
            if decision.outcome == "cancel":
                self._finish(None)
            elif decision.outcome == "code":
                self._finish(decision.code)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


class _UnboundCallbackServer:
    """What upstream resolves when ``listen`` errors: a login that cannot succeed.

    The port is fixed at 1456, so "already in use" is the ordinary case -- a second login
    started while the first is still waiting. Upstream reports it as a callback that never
    completed rather than as a crash, and so does this.
    """

    async def wait_for_code(self) -> str | None:
        return None

    def close(self) -> None:
        return None


async def start_radius_callback_server(expected_state: str, signal: Any = None) -> Any:
    server = _RadiusCallbackServer(expected_state, signal)
    try:
        listener = await asyncio.start_server(server.handle, CALLBACK_HOST, CALLBACK_PORT)
    except OSError:
        server.close()
        return _UnboundCallbackServer()
    server.attach(listener)
    return server


def _notify_progress(callbacks: OAuthLoginCallbacks, message: str) -> None:
    """``onProgress`` is optional in misaka's callbacks record; upstream's notify is not."""
    on_progress = getattr(callbacks, "onProgress", None)
    if callable(on_progress):
        on_progress(message)


def _build_authorize_url(authorization_endpoint: str, challenge: str, state: str) -> str:
    """``authorizeUrl.search = new URLSearchParams(...)``: the endpoint's own query is replaced."""
    parsed = urlparse(authorization_endpoint)
    query = urlencode(
        {
            "response_type": "code",
            "client_id": OAUTH_CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "scope": OAUTH_SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "handoff": "url",
            "state": state,
        }
    )
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, query, parsed.fragment))


async def login_with_browser(
    gateway: str,
    authorization_endpoint: str,
    callbacks: OAuthLoginCallbacks,
    *,
    post_form: PostForm | None = None,
    start_callback_server: Any = None,
) -> OAuthCredentials:
    """PKCE authorization code, with the redirect caught on loopback."""
    start = start_callback_server or start_radius_callback_server
    signal = getattr(callbacks, "signal", None)
    pkce = await generate_pkce()
    state = str(uuid.uuid4())
    authorize_url = _build_authorize_url(authorization_endpoint, pkce.challenge, state)

    server = await start(state, signal)
    _notify_progress(callbacks, f"Listening for OAuth callback on {REDIRECT_URI}")
    callbacks.onAuth(OAuthAuthInfo(url=authorize_url, instructions="Continue in your browser."))

    try:
        code = await server.wait_for_code()
        if not code:
            if signal_aborted(signal):
                raise RuntimeError(CANCEL_MESSAGE)
            raise RuntimeError("OAuth callback did not complete.")
        return await request_oauth_token(
            gateway,
            {
                "grant_type": "authorization_code",
                "client_id": OAUTH_CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "code": code,
                "code_verifier": pkce.verifier,
            },
            signal,
            post_form=post_form,
        )
    finally:
        server.close()


@dataclass(slots=True)
class _DeviceAuthorization:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int | float
    interval: int | float | None


async def request_device_authorization(
    gateway: str,
    signal: Any = None,
    *,
    post_form: PostForm | None = None,
) -> _DeviceAuthorization:
    send = post_form or _default_post_form
    try:
        response = await send(
            _gateway_url(gateway, DEVICE_PATH),
            {"client_id": OAUTH_CLIENT_ID, "scope": OAUTH_SCOPE},
            signal,
        )
    except Exception as error:
        if signal_aborted(signal):
            raise RuntimeError(CANCEL_MESSAGE) from error
        raise

    if not response.ok:
        raise _read_oauth_response_error(response, "Radius OAuth device authorization failed")

    try:
        data = json.loads(response.body)
    except ValueError:
        data = None
    missing = "Radius OAuth device authorization response is missing required fields"
    if not isinstance(data, dict):
        raise RuntimeError(missing)  # noqa: TRY004 - a bad response is a runtime failure
    expires_in = data.get("expires_in")
    # Upstream's guard is `!data.expires_in`, so 0 is missing too; a boolean is not a
    # number in JS and must not be one here either.
    if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)) or not expires_in:
        raise RuntimeError(missing)
    interval = data.get("interval")
    return _DeviceAuthorization(
        device_code=_required_string(data, "device_code", missing),
        user_code=_required_string(data, "user_code", missing),
        verification_uri=_required_string(data, "verification_uri", missing),
        expires_in=expires_in,
        interval=None if isinstance(interval, bool) or not isinstance(interval, (int, float)) else interval,
    )


async def login_with_device_code(
    gateway: str,
    callbacks: OAuthLoginCallbacks,
    *,
    post_form: PostForm | None = None,
    poll_device_code: Any = None,
) -> OAuthCredentials:
    """RFC 8628, for signing in from another device."""
    poll_flow = poll_device_code or poll_oauth_device_code_flow
    signal = getattr(callbacks, "signal", None)
    device = await request_device_authorization(gateway, signal, post_form=post_form)
    callbacks.onDeviceCode(
        OAuthDeviceCodeInfo(
            userCode=device.user_code,
            verificationUri=device.verification_uri,
            intervalSeconds=device.interval,
            expiresInSeconds=device.expires_in,
        )
    )

    credential: OAuthCredentials | None = None

    async def poll() -> Any:
        nonlocal credential
        try:
            credential = await request_oauth_token(
                gateway,
                {
                    "grant_type": OAUTH_DEVICE_CODE_GRANT_TYPE,
                    "client_id": OAUTH_CLIENT_ID,
                    "device_code": device.device_code,
                },
                signal,
                post_form=post_form,
            )
        except OAuthResponseError as error:
            if error.oauthError == "authorization_pending":
                return {"status": "pending"}
            if error.oauthError == "slow_down":
                # No `intervalSeconds` here: upstream sends none, so the poller applies
                # RFC 8628's five-second bump to whatever interval the gateway already gave.
                return {"status": "slow_down"}
            if error.oauthError == "expired_token":
                return {"status": "failed", "message": "Device authorization expired."}
            if error.oauthError == "access_denied":
                return {"status": "failed", "message": "Device authorization was denied."}
            raise
        return {"status": "complete", "accessToken": credential.access}

    await poll_flow(
        intervalSeconds=device.interval,
        expiresInSeconds=device.expires_in,
        poll=poll,
        signal=signal,
    )
    if credential is None:  # pragma: no cover - the poller only returns after `complete` set it
        raise RuntimeError("Radius OAuth polling completed without credentials")
    return credential


class _RadiusOAuthProvider:
    """One gateway's flow, in the shape ``ai/utils/oauth`` registers and the bridge wraps."""

    # False even though the browser login does run a callback server. The flag is not
    # descriptive in misaka: `ui/tui/interactive/interactive_mode.py` reads it and offers a
    # "paste the redirect URL" box, then feeds what was typed to `onManualCodeInput`. This
    # flow has no such callback, so a True here puts a box on screen whose input goes
    # nowhere -- worst on the port-already-bound path, where the login fails immediately
    # while the box is still waiting. The flows that set True (`anthropic`, `openai_codex`)
    # all consume `onManualCodeInput`.
    usesCallbackServer = False

    def __init__(
        self,
        *,
        id: str,
        name: str,
        gateway: str,
        post_form: PostForm | None = None,
        get_json: GetJson | None = None,
        start_callback_server: Any = None,
        poll_device_code: Any = None,
    ) -> None:
        self.id = id
        self.name = name
        self.gateway = normalize_radius_gateway_url(gateway)
        self._post_form = post_form
        self._get_json = get_json
        self._start_callback_server = start_callback_server
        self._poll_device_code = poll_device_code

    async def login(self, callbacks: OAuthLoginCallbacks) -> OAuthCredentials:
        method = await callbacks.onSelect(
            OAuthSelectPrompt(
                message=f"Sign in to {self.name}:",
                options=[
                    OAuthSelectOption(id=LOGIN_METHOD_BROWSER, label="Sign in with browser (recommended)"),
                    OAuthSelectOption(
                        id=LOGIN_METHOD_DEVICE_CODE,
                        label="Sign in with device code (when signing in from another device)",
                    ),
                ],
            )
        )

        if method == LOGIN_METHOD_DEVICE_CODE:
            return await login_with_device_code(
                self.gateway,
                callbacks,
                post_form=self._post_form,
                poll_device_code=self._poll_device_code,
            )
        if method == LOGIN_METHOD_BROWSER:
            endpoint = await load_radius_oauth_discovery(
                self.gateway, getattr(callbacks, "signal", None), get_json=self._get_json
            )
            return await login_with_browser(
                self.gateway,
                endpoint,
                callbacks,
                post_form=self._post_form,
                start_callback_server=self._start_callback_server,
            )
        if method is None:
            # misaka's selector can be dismissed; upstream's prompt cannot. Dismissing is
            # a cancelled login, not an unknown method.
            raise RuntimeError(CANCEL_MESSAGE)
        raise RuntimeError(f"Unknown {self.name} sign-in method: {method}")

    async def refreshToken(self, credentials: OAuthCredentials, signal: Any | None = None) -> OAuthCredentials:
        return await request_oauth_token(
            self.gateway,
            {
                "grant_type": "refresh_token",
                "client_id": OAUTH_CLIENT_ID,
                "refresh_token": credentials.refresh,
            },
            signal,
            post_form=self._post_form,
        )

    def getApiKey(self, credentials: OAuthCredentials) -> str:
        # Upstream's `toAuth` returns `{apiKey: credential.access}`.
        return credentials.access


def create_radius_oauth(
    *,
    id: str = "radius",
    name: str = "Radius",
    gateway: str = DEFAULT_RADIUS_GATEWAY,
    post_form: PostForm | None = None,
    get_json: GetJson | None = None,
    start_callback_server: Any = None,
    poll_device_code: Any = None,
) -> _RadiusOAuthProvider:
    """Upstream's ``createRadiusOAuth({name, gateway})``, plus the id misaka registers by."""
    return _RadiusOAuthProvider(
        id=id,
        name=name,
        gateway=gateway,
        post_form=post_form,
        get_json=get_json,
        start_callback_server=start_callback_server,
        poll_device_code=poll_device_code,
    )


# The default gateway's flow, so `ai/utils/oauth/__init__.py` has something to register.
radius_oauth_provider = create_radius_oauth()


__all__ = [
    "CALLBACK_PATH",
    "CANCEL_MESSAGE",
    "LOGIN_METHOD_BROWSER",
    "LOGIN_METHOD_DEVICE_CODE",
    "OAUTH_CLIENT_ID",
    "OAUTH_SCOPE",
    "REDIRECT_URI",
    "GetJson",
    "OAuthResponseError",
    "PostForm",
    "create_radius_oauth",
    "decide_radius_callback",
    "load_radius_oauth_discovery",
    "login_with_browser",
    "login_with_device_code",
    "radius_oauth_provider",
    "request_device_authorization",
    "request_oauth_token",
    "start_radius_callback_server",
]

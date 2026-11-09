"""Translated from pi's ``packages/ai/src/auth/oauth/callback-server.ts`` (v1.1.0).

Loopback OAuth redirect handler shared by the browser sign-in flows.

MISAKA fork: ``node:http`` becomes ``asyncio.start_server`` with a request-line reader in front of upstream's handler;
what the handler answers, and in which order it checks, is upstream's. Where the translation differs, and why:

* **Binding.** ``server.listen(port, host)`` binds the first address ``host`` resolves to;
  ``asyncio.start_server(host=...)`` binds every one, each on its own port when ``port`` is
  0. The host is resolved first and only that first address is bound.
* **Idle sockets.** A browser opens speculative preconnect sockets to the redirect origin
  and sends nothing on them. A request line that never comes, or comes empty, is dropped
  without an answer, and ``close()`` aborts every socket still waiting for one -- node's
  ``server.close()`` closes idle keep-alive sockets the same way. ``close()`` does not wait
  for the handlers, as upstream does not.
* **Abort.** misaka's signals are duck-typed, so the abort is watched on a task instead of
  an ``abort`` listener.
* **``wait()``** is shielded: a JavaScript promise cannot be cancelled by one of its
  awaiters, and an ``asyncio`` future can.
* **A prompt that ignores its signal.** ``waitForCallbackOrManualInput`` aborts the manual
  prompt's signal as upstream does, and also cancels the task awaiting it.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Literal, TypedDict

from misaka.ai.auth.types import ManualCodePrompt, ProviderAuthInteraction
from misaka.ai.utils.oauth_page import oauthErrorHtml, oauthSuccessHtml
from misaka.utils._abort_signal import wait_for_abort
from misaka.utils.js import AbortController
from misaka.utils.values import signal_aborted
from misaka.utils.whatwg_url import URL


@dataclass(slots=True, kw_only=True)
class OAuthCallbackServerOptions[T]:
    # Provider name used on the browser page, for example `OpenAI`.
    providerName: str
    # Address to listen on.
    host: str
    # Port to listen on; `0` picks a free port.
    port: int
    path: str
    # Host in `redirectUri` when it differs from `host`, for example `localhost`.
    redirectHost: str | None = None
    # Expected `state` parameter. Omit when the provider does not send one.
    state: str | None = None
    # Finishes the sign-in with the received code before the browser page is sent, so the page can
    # show exchange failures. Pass `async (code) => code` to exchange the code later.
    complete: Callable[[str], Awaitable[T]]
    signal: Any = None
    timeoutMs: float | None = None


@dataclass(slots=True, frozen=True)
class OAuthCallbackServer[T]:
    redirectUri: str
    # Resolves with the result of `complete`, or `undefined` after `cancel()`. Rejects when the provider
    # redirects with an error, `complete` fails, the signal aborts, or the timeout elapses.
    wait: Callable[[], Awaitable[T | None]]
    # Stop waiting for the browser unless a callback is already being completed.
    cancel: Callable[[], None]
    close: Callable[[], None]


class _OAuthCallbackResult[T](TypedDict):
    type: Literal["callback"]
    value: T


class _OAuthManualResult(TypedDict):
    type: Literal["manual"]
    input: str


def _sendPage(response: asyncio.StreamWriter, status: int, html: str) -> None:
    body = html.encode("utf-8")
    head = (
        f"HTTP/1.1 {status} {HTTPStatus(status).phrase}\r\n"
        "content-type: text/html; charset=utf-8\r\n"
        "cache-control: no-store\r\n"
        f"content-length: {len(body)}\r\n"
        "connection: close\r\n\r\n"
    )
    # MISAKA fork: the peer may be gone already; node's `response.end` does not throw for it.
    with contextlib.suppress(OSError, RuntimeError):
        response.write(head.encode("latin-1") + body)
        response.close()


async def startOAuthCallbackServer[T](options: OAuthCallbackServerOptions[T]) -> OAuthCallbackServer[T]:
    providerName = options.providerName
    signal = options.signal
    if signal_aborted(signal):
        raise RuntimeError("Login cancelled")

    loop = asyncio.get_running_loop()
    waitPromise: asyncio.Future[T | None] = loop.create_future()
    # A cancelled or closed wait may never be observed.
    waitPromise.add_done_callback(lambda done: done.cancelled() or done.exception())

    claimed = False
    settled = False
    timer: asyncio.TimerHandle | None = None
    aborting: asyncio.Future[None] | None = None
    idle: set[asyncio.StreamWriter] = set()

    def onAbort(_: Any = None) -> None:
        finish({"error": RuntimeError("Login cancelled")})

    def finish(result: dict[str, Any]) -> None:
        nonlocal settled
        if settled:
            return
        settled = True
        if timer is not None:
            timer.cancel()
        if aborting is not None:
            aborting.remove_done_callback(onAbort)
            aborting.cancel()
        if "error" in result:
            waitPromise.set_exception(result["error"])
        else:
            waitPromise.set_result(result["value"])

    async def handle(request: asyncio.StreamReader, response: asyncio.StreamWriter) -> None:
        nonlocal claimed
        idle.add(response)
        try:
            requestLine = await request.readline()
            while requestLine.strip():
                header = await request.readline()
                if not header or header in (b"\r\n", b"\n"):
                    break
        except (OSError, ValueError, asyncio.IncompleteReadError):
            requestLine = b""
        finally:
            idle.discard(response)
        parts = requestLine.decode("latin-1").split()
        if len(parts) < 2:
            # MISAKA fork: a peer that sent no request line (a preconnect, a port scan, or one
            # of `close()`'s own aborts) gets no answer.
            response.transport.abort()
            return
        method, target = parts[0], parts[1]

        url = URL(target or "/", "http://localhost")
        if method != "GET" or url.pathname != options.path:
            _sendPage(response, 404, oauthErrorHtml("Callback route not found."))
            return
        if options.state is not None and url.searchParams.get("state") != options.state:
            _sendPage(response, 400, oauthErrorHtml("State mismatch."))
            return
        if claimed or settled:
            _sendPage(response, 409, oauthErrorHtml("This sign-in has already been handled."))
            return
        error = url.searchParams.get("error")
        if error:
            description = url.searchParams.get("error_description")
            description = description if description is not None else error
            _sendPage(response, 400, oauthErrorHtml(f"{providerName} authorization failed.", description))
            finish({"error": RuntimeError(f"{providerName} authorization failed: {description}")})
            return
        code = url.searchParams.get("code")
        if not code:
            _sendPage(response, 400, oauthErrorHtml("Missing authorization code."))
            return
        claimed = True
        try:
            value = await options.complete(code)
            _sendPage(response, 200, oauthSuccessHtml(f"Signed in to {providerName}. You may now close this page."))
            finish({"value": value})
        except Exception as failure:  # noqa: BLE001 - shown on the page and raised from wait()
            _sendPage(response, 502, oauthErrorHtml(f"{providerName} sign-in failed.", str(failure)))
            finish({"error": failure})

    # MISAKA fork: bind only the first address the host resolves to, as `listen` does.
    addresses = await loop.getaddrinfo(options.host, options.port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
    family, _, _, _, sockaddr = addresses[0]
    server = await asyncio.start_server(handle, str(sockaddr[0]), options.port, family=family)
    address = server.sockets[0].getsockname() if server.sockets else None
    if not address:
        server.close()
        raise RuntimeError("OAuth callback server did not bind to TCP")

    if signal is not None:
        aborting = asyncio.ensure_future(wait_for_abort(signal))
        aborting.add_done_callback(onAbort)
    if options.timeoutMs is not None:
        timer = loop.call_later(
            options.timeoutMs / 1000,
            finish,
            {"error": RuntimeError(f"{providerName} sign-in timed out")},
        )
    redirectHost = options.redirectHost if options.redirectHost is not None else options.host

    def cancel() -> None:
        if not claimed:
            finish({"value": None})

    def close() -> None:
        finish({"error": RuntimeError("OAuth callback server closed")})
        server.close()
        for connection in list(idle):
            connection.transport.abort()
        idle.clear()

    return OAuthCallbackServer(
        redirectUri=f"http://{f'[{redirectHost}]' if ':' in redirectHost else redirectHost}:{address[1]}{options.path}",
        wait=lambda: asyncio.shield(waitPromise),
        cancel=cancel,
        close=close,
    )


async def waitForCallbackOrManualInput[T](
    interaction: ProviderAuthInteraction,
    callback: OAuthCallbackServer[T] | None,
    prompt: Mapping[str, str],
) -> _OAuthCallbackResult[T] | _OAuthManualResult:
    """Wait for the browser callback, or for the user to paste the code or redirect URL when the browser
    cannot reach the loopback server (for example over SSH). Without a callback server only the manual
    prompt is used.
    """
    manualAbort = AbortController()
    manualError: Exception | None = None

    async def manualPrompt() -> str | None:
        nonlocal manualError
        try:
            input = await interaction.prompt(ManualCodePrompt(**prompt, signal=manualAbort.signal))
        except Exception as error:  # noqa: BLE001 - re-raised on the main path
            manualError = error
            if callback is not None:
                callback.cancel()
            return None
        if callback is not None:
            callback.cancel()
        return input

    manual = asyncio.ensure_future(manualPrompt())
    try:
        value = await callback.wait() if callback is not None else None
        if manualError is not None:
            raise manualError
        if value is not None:
            return {"type": "callback", "value": value}
        input = await manual
        if manualError is not None:
            raise manualError
        return {"type": "manual", "input": input if input is not None else ""}
    finally:
        manualAbort.abort()
        # MISAKA fork: also stop awaiting a prompt that ignores its signal. `asyncio.wait`, so
        # a cancellation of this login itself still propagates.
        if not manual.done():
            manual.cancel()
            await asyncio.wait({manual})


__all__ = [
    "OAuthCallbackServer",
    "OAuthCallbackServerOptions",
    "startOAuthCallbackServer",
    "waitForCallbackOrManualInput",
]

"""Accepting WebSocket clients on a host.

Translated from pi's ``packages/chord/src/websocket.ts``. Chord does not depend on a WebSocket library: a server is
anything with ``on("connection", listener)`` that hands the listener a ``WebSocketLike`` socket and the request
(misaka's tests adapt ``websockets`` connections, as upstream's adapt ``ws``).
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

from misaka.chord._promise import background, isRejection
from misaka.chord.connection import AcceptOptions
from misaka.chord.link import WebSocketLike, webSocketLink
from misaka.utils.js import UNDEFINED

if TYPE_CHECKING:
    from misaka.chord.host import Host


class WebSocketServerLike(Protocol):
    """A WebSocket server, structurally: Chord does not depend on a WebSocket library."""

    def on(self, event: str, listener: Callable[[WebSocketLike, Any], None]) -> Any: ...


class WebSocketAcceptOptions(AcceptOptions, total=False):
    """WebSocket peers are always accepted as clients: no roots, no code versions, no reload votes, not addressable.
    ``principal`` is the authenticator: ``principal(request)`` is the principal of the connecting client (or an
    awaitable of it), or ``None`` to refuse it."""


def _unhandled(error: BaseException) -> None:
    """A rejection nobody handles: Node reports it as an unhandled rejection; asyncio's report is the loop's exception
    handler (where a task failure nobody retrieved goes)."""
    asyncio.get_running_loop().call_exception_handler(
        {"message": "Unhandled rejection in acceptWebSockets", "exception": error}
    )


def acceptWebSockets(server: WebSocketServerLike, host: Host, options: WebSocketAcceptOptions) -> None:
    """Accepts every connection of ``server`` on ``host`` as a client link stamped with the authenticated principal."""

    def onConnection(socket: WebSocketLike, request: Any) -> None:
        # Wrap at once so frames sent while authenticating are queued, not lost. Large payloads go out deflated;
        # clients may not send packed frames (a small message could inflate to anything).
        link = webSocketLink(socket, {"compress": True, "acceptPacked": False})

        authenticated = options["principal"](request)

        async def authenticate() -> None:
            try:
                if inspect.isawaitable(authenticated):
                    principal = await authenticated
                else:
                    # `Promise.resolve(principal).then(...)` runs on a later turn, never inline.
                    await asyncio.sleep(0)
                    principal = authenticated
            except GeneratorExit:
                raise
            except BaseException as error:
                # Any rejection refuses the client (a cancelled awaitable too); a cancellation of this task goes on.
                link.close("authentication failed")
                if isRejection(error):
                    return
                raise
            # MISAKA fork: upstream refuses `undefined` and accepts a `null` principal; a Python function returns None
            # for "no value", so None (or UNDEFINED) refuses the client and a client's principal is never None.
            if principal is None or principal is UNDEFINED:
                link.close("unauthorized")
            else:
                # A throw here rejects the promise nobody handles (`.then(onAccepted, onRejected)` does not catch its
                # own handler): it is reported as unhandled, not swallowed.
                host.accept(link, {**options, "principal": principal, "kind": "client"})

        background(authenticate(), _unhandled)

    server.on("connection", onConnection)


__all__ = ["WebSocketAcceptOptions", "WebSocketServerLike", "acceptWebSockets"]

"""The transport interface and the listener bookkeeping transports share.

Translated from pi's ``packages/mcp/src/transports/transport.ts``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final, Protocol, TypedDict

from misaka.mcp.protocol.jsonrpc import JsonRpcMessage, toError
from misaka.utils.js import liveKeys

DEFAULT_MAX_MESSAGE_BYTES: Final = 16 * 1024 * 1024

type McpTransportMessageListener = Callable[[JsonRpcMessage], None]
type McpTransportErrorListener = Callable[[Exception], None]
type McpTransportCloseListener = Callable[[], None]


class McpTransportSendOptions(TypedDict, total=False):
    # Aborted once the sender no longer waits for a reply (the request was cancelled, timed out, answered,
    # or the client closed). A transport must not dispatch the message after that, for example when
    # retrying it after an authorization refresh. It does not undo a message already dispatched.
    signal: Any


class McpTransport(Protocol):
    """A transport. ``setProtocolVersion(version)`` is optional; callers look it up with ``getattr``.

    MISAKA fork: every transport takes ``send``'s ``options``, also those that ignore it; a TypeScript
    implementation may omit a trailing parameter, a Python one raises ``TypeError`` when it is passed."""

    async def start(self) -> None: ...

    async def send(self, message: JsonRpcMessage, options: McpTransportSendOptions | None = None) -> None: ...

    async def close(self) -> None: ...

    def onMessage(self, listener: McpTransportMessageListener) -> Callable[[], None]: ...

    def onError(self, listener: McpTransportErrorListener) -> Callable[[], None]: ...

    def onClose(self, listener: McpTransportCloseListener) -> Callable[[], None]: ...


class TransportEvents:
    """Listener bookkeeping shared by transports. ``emitClose`` fires at most once per transport."""

    def __init__(self) -> None:
        # Dicts keep insertion order, as a JavaScript Set does.
        self._messageListeners: dict[McpTransportMessageListener, None] = {}
        self._errorListeners: dict[McpTransportErrorListener, None] = {}
        self._closeListeners: dict[McpTransportCloseListener, None] = {}
        self._closeEmitted = False

    def onMessage(self, listener: McpTransportMessageListener) -> Callable[[], None]:
        self._messageListeners[listener] = None
        return lambda: self._messageListeners.pop(listener, None)  # type: ignore[return-value]

    def onError(self, listener: McpTransportErrorListener) -> Callable[[], None]:
        self._errorListeners[listener] = None
        return lambda: self._errorListeners.pop(listener, None)  # type: ignore[return-value]

    def onClose(self, listener: McpTransportCloseListener) -> Callable[[], None]:
        self._closeListeners[listener] = None
        return lambda: self._closeListeners.pop(listener, None)  # type: ignore[return-value]

    def _emitMessage(self, message: JsonRpcMessage) -> None:
        for listener in liveKeys(self._messageListeners):
            listener(message)

    def _emitError(self, error: Any) -> None:
        normalized = toError(error)
        for listener in liveKeys(self._errorListeners):
            listener(normalized)

    def _emitClose(self) -> None:
        if self._closeEmitted:
            return
        self._closeEmitted = True
        for listener in liveKeys(self._closeListeners):
            listener()


__all__ = [
    "DEFAULT_MAX_MESSAGE_BYTES",
    "McpTransport",
    "McpTransportCloseListener",
    "McpTransportErrorListener",
    "McpTransportMessageListener",
    "McpTransportSendOptions",
    "TransportEvents",
]

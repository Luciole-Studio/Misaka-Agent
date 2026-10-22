"""A pair of transports connected in memory, for tests.

Translated from pi's ``packages/mcp/src/transports/in-memory.ts``.
"""

from __future__ import annotations

import asyncio
import copy
from typing import Any, TypedDict

from misaka.mcp.protocol.jsonrpc import JsonRpcMessage, McpConnectionClosedError
from misaka.mcp.transports.transport import McpTransportSendOptions, TransportEvents


class InMemoryTransport(TransportEvents):
    def __init__(self) -> None:
        super().__init__()
        self._peer: InMemoryTransport | None = None
        self._started = False
        self._closed = False

    def connectPeer(self, peer: InMemoryTransport) -> None:
        if self._peer is not None:
            raise RuntimeError("In-memory MCP transport already has a peer")
        self._peer = peer

    async def start(self) -> None:
        if self._closed:
            raise McpConnectionClosedError()
        self._started = True

    async def send(self, message: JsonRpcMessage, options: McpTransportSendOptions | None = None) -> None:
        # MISAKA fork: `options` is accepted and ignored, as upstream's `send(message)` ignores it (see `McpTransport`).
        if not self._started or self._closed:
            raise McpConnectionClosedError()
        peer = self._peer
        if peer is None or not peer._started or peer._closed:
            raise McpConnectionClosedError("In-memory MCP peer is not connected")
        clone = copy.deepcopy(message)
        # `queueMicrotask`: delivered after the current step, in send order.
        asyncio.get_running_loop().call_soon(peer._deliver, clone)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._emitClose()
        if self._peer is not None:
            await self._peer.close()

    def emitError(self, error: Any) -> None:
        """Exposed so tests can simulate transport-level failures."""
        self._emitError(error)

    def _deliver(self, message: JsonRpcMessage) -> None:
        if self._closed:
            return
        self._emitMessage(message)


class InMemoryTransportPair(TypedDict):
    client: InMemoryTransport
    server: InMemoryTransport


def createInMemoryTransportPair() -> InMemoryTransportPair:
    client = InMemoryTransport()
    server = InMemoryTransport()
    client.connectPeer(server)
    server.connectPeer(client)
    return {"client": client, "server": server}


__all__ = ["InMemoryTransport", "createInMemoryTransportPair"]

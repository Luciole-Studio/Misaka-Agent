"""CCB WebSocketTransport through SDK SessionMessage and native websockets.

The official Python SDK's websocket_client(url) has no headers argument. Keep
that transport's stream contract, adding CCB static/dynamic/IDE auth headers.
SDK ClientSession continues to own all JSON-RPC request/lifecycle semantics.
"""
from contextlib import asynccontextmanager

import anyio
from mcp import types
from mcp.shared.message import SessionMessage
from pydantic import ValidationError
from websockets.asyncio.client import connect


@asynccontextmanager
async def websocket_client(url, *, headers):
    incoming, read_stream = anyio.create_memory_object_stream(0)
    write_stream, outgoing = anyio.create_memory_object_stream(0)
    async with connect(url, subprotocols=['mcp'], additional_headers=headers) as socket:
        async def reader():
            async with incoming:
                async for raw in socket:
                    try:
                        value = SessionMessage(types.JSONRPCMessage.model_validate_json(raw))
                    except ValidationError as error:
                        value = error
                    await incoming.send(value)
            # A clean remote close is still a disconnected transport. Finish the
            # owner context too, so ensure_started can reconnect on the next use.
            group.cancel_scope.cancel()

        async def writer():
            async with outgoing:
                async for value in outgoing:
                    await socket.send(value.message.model_dump_json(by_alias=True, exclude_none=True))

        async with anyio.create_task_group() as group:
            group.start_soon(reader)
            group.start_soon(writer)
            try:
                yield read_stream, write_stream
            finally:
                group.cancel_scope.cancel()
                await read_stream.aclose()
                await write_stream.aclose()

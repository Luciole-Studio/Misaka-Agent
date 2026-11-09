"""pi's ``packages/mcp/test/client.test.ts`` and ``content.test.ts``, plus the MISAKA forks of the client
(cancellation, which a JavaScript promise does not have)."""

from __future__ import annotations

import asyncio
import re

import pytest

from misaka.mcp.client import McpClient
from misaka.mcp.protocol.content import toLlmContent
from misaka.mcp.protocol.jsonrpc import McpAbortError, McpError, McpTimeoutError
from misaka.mcp.protocol.types import LATEST_PROTOCOL_VERSION
from misaka.mcp.testing import createInMemoryTransportPair
from misaka.utils.js import UNDEFINED, AbortController
from misaka.utils.values import maybe_await


async def flush(times: int = 20) -> None:
    """``await new Promise((resolve) => setTimeout(resolve, 0))``: drain the callbacks and tasks
    queued so far (one ``asyncio.sleep(0)`` runs only one loop iteration)."""
    for _ in range(times):
        await asyncio.sleep(0)


class TestServer:
    __test__ = False

    def __init__(self, transport) -> None:
        self.transport = transport
        self.messages: list = []
        self.handlers: dict = {}
        self.tasks: set[asyncio.Task] = set()

    def setHandler(self, method, handler) -> None:
        self.handlers[method] = handler


@pytest.fixture
def servers():
    created: list[TestServer] = []
    yield created
    for server in created:
        for task in server.tasks:
            task.cancel()


def never():
    """``new Promise(() => {})``."""
    return asyncio.get_running_loop().create_future()


async def createServer(servers):
    pair = createInMemoryTransportPair()
    server = TestServer(pair["server"])
    servers.append(server)

    def onMessage(message) -> None:
        server.messages.append(message)
        if "id" not in message or "method" not in message:
            return
        handler = server.handlers.get(message["method"])

        async def answer() -> None:
            try:
                if handler is None:
                    raise McpError(-32601, f"Method not found: {message['method']}")
                result = await maybe_await(handler(message))
                await pair["server"].send({"jsonrpc": "2.0", "id": message["id"], "result": result})
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - answered as an error response, as upstream
                mcpError = error if isinstance(error, McpError) else McpError(-32603, str(error))
                await pair["server"].send({
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": mcpError.code, "message": mcpError.message, "data": mcpError.data},
                })

        task = asyncio.ensure_future(answer())
        server.tasks.add(task)
        task.add_done_callback(server.tasks.discard)

    pair["server"].onMessage(onMessage)
    await pair["server"].start()
    server.setHandler("initialize", lambda _request: {
        "protocolVersion": LATEST_PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": True}},
        "serverInfo": {"name": "test-server", "version": "1.0.0"},
        "instructions": "Use test tools.",
    })
    return pair["client"], server


async def connect(servers):
    clientTransport, server = await createServer(servers)
    client = McpClient({"name": "test-client", "version": "2.0.0"})
    await client.connect(clientTransport)
    return client, server


# -- client.test.ts --------------------------------------------------------------------------


async def test_initializes_the_connection_before_exposing_server_information(servers):
    client, server = await connect(servers)
    assert client.connectionState == "connected"
    assert client.protocolVersion == LATEST_PROTOCOL_VERSION
    assert client.serverInfo == {"name": "test-server", "version": "1.0.0"}
    assert client.serverCapabilities == {"tools": {"listChanged": True}}
    assert client.instructions == "Use test tools."
    await flush()
    assert server.messages == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "2.0.0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ]
    await client.close()


async def test_paginates_tools_and_preserves_protocol_tool_definitions(servers):
    client, server = await connect(servers)

    def listTools(request):
        cursor = (request.get("params") or {}).get("cursor")
        if cursor is None:
            return {
                "tools": [{"name": "search", "description": "Search", "inputSchema": {"type": "object"}}],
                "nextCursor": "page-2",
            }
        return {
            "tools": [{
                "name": "read",
                "inputSchema": {"type": "object"},
                "outputSchema": {"type": "object"},
                "annotations": {"readOnlyHint": True},
            }],
            # Some servers end pagination with "" instead of omitting the cursor.
            "nextCursor": "",
        }

    server.setHandler("tools/list", listTools)
    assert await client.listTools() == [
        {"name": "search", "description": "Search", "inputSchema": {"type": "object"}},
        {
            "name": "read",
            "inputSchema": {"type": "object"},
            "outputSchema": {"type": "object"},
            "annotations": {"readOnlyHint": True},
        },
    ]
    await client.close()


async def test_lists_and_reads_resources(servers):
    client, server = await connect(servers)
    server.setHandler("resources/list", lambda request: (
        {"resources": [{"uri": "file:///a", "name": "a", "mimeType": "text/plain"}], "nextCursor": "2"}
        if (request.get("params") or {}).get("cursor") is None
        else {"resources": [{"uri": "file:///b"}]}
    ))
    server.setHandler("resources/templates/list", lambda _request: {
        "resourceTemplates": [{"uriTemplate": "repo://{owner}/{repo}", "name": "repo"}],
    })
    server.setHandler("resources/read", lambda request: {
        "contents": [{"uri": request["params"]["uri"], "text": "hello"}],
    })
    # A missing name falls back to the URI.
    assert await client.listResources() == [
        {"uri": "file:///a", "name": "a", "mimeType": "text/plain"},
        {"uri": "file:///b", "name": "file:///b"},
    ]
    assert await client.listResourceTemplates() == [{"uriTemplate": "repo://{owner}/{repo}", "name": "repo"}]
    # Single pages pass the cursor through.
    assert await client.listResourcesPage() == {
        "resources": [{"uri": "file:///a", "name": "a", "mimeType": "text/plain"}],
        "nextCursor": "2",
    }
    assert await client.listResourcesPage("2") == {"resources": [{"uri": "file:///b", "name": "file:///b"}]}
    assert await client.readResource("file:///a") == {"contents": [{"uri": "file:///a", "text": "hello"}]}

    server.setHandler("resources/read", lambda _request: {"contents": [{"uri": "file:///a"}]})
    with pytest.raises(McpError, match=re.escape("Invalid contents in MCP resources/read result")):
        await client.readResource("file:///a")
    server.setHandler("resources/list", lambda _request: {"resources": [{"name": "no uri"}]})
    with pytest.raises(McpError, match=re.escape("Invalid entry in MCP resources/list result")):
        await client.listResources()
    await client.close()


async def test_returns_structured_tool_content_and_surfaces_json_rpc_errors(servers):
    client, server = await connect(servers)

    def callTool(request):
        params = request["params"]
        if params["name"] == "fail":
            raise McpError(1234, "tool failed", {"retryable": False})
        return {
            "content": [{"type": "text", "text": "ok"}],
            "structuredContent": {"count": (params.get("arguments") or {}).get("count")},
        }

    server.setHandler("tools/call", callTool)
    assert await client.callTool("count", {"count": 3}) == {
        "content": [{"type": "text", "text": "ok"}],
        "structuredContent": {"count": 3},
    }
    with pytest.raises(McpError) as failure:
        await client.callTool("fail")
    assert (failure.value.name, failure.value.code, failure.value.message, failure.value.data) == (
        "McpError", 1234, "tool failed", {"retryable": False},
    )
    await client.close()


async def test_renews_the_timeout_on_progress(servers):
    # Upstream runs this on fake timers (50 ms timeout, progress at 40, answer at 80); real time here.
    client, server = await connect(servers)

    async def slow(request):
        token = request["params"]["_meta"]["progressToken"]

        async def progress() -> None:
            await asyncio.sleep(0.4)
            await server.transport.send({
                "jsonrpc": "2.0",
                "method": "notifications/progress",
                "params": {"progressToken": token, "progress": 1, "total": 2},
            })

        task = asyncio.ensure_future(progress())
        server.tasks.add(task)
        await asyncio.sleep(0.8)
        return {"content": [{"type": "text", "text": "done"}]}

    server.setHandler("tools/call", slow)
    progress: list = []
    result = await client.callTool("slow", {}, {"timeoutMs": 500, "onProgress": progress.append})
    assert result == {"content": [{"type": "text", "text": "done"}]}
    assert progress == [{"progressToken": 2, "progress": 1, "total": 2}]
    await client.close()


async def test_cancels_aborted_and_timed_out_requests(servers):
    client, server = await connect(servers)
    server.setHandler("tools/call", lambda _request: never())
    controller = AbortController()
    aborted = asyncio.ensure_future(client.callTool("wait", {}, {"signal": controller.signal}))
    await flush()
    controller.abort("stop")
    with pytest.raises(McpAbortError):
        await aborted
    await flush()
    assert {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 2, "reason": "stop"}} in (
        server.messages
    )

    with pytest.raises(McpTimeoutError):
        await client.callTool("wait", {}, {"timeoutMs": 5})
    await client.close()


async def test_reports_transport_errors_without_failing_pending_requests(servers):
    clientTransport, server = await createServer(servers)
    client = McpClient({"name": "test-client", "version": "1.0.0"})
    await client.connect(clientTransport)
    errors: list = []
    client.onError(errors.append)
    answered = asyncio.get_running_loop().create_future()
    server.setHandler("tools/call", lambda _request: answered)
    call = asyncio.ensure_future(client.callTool("wait"))
    await flush()
    clientTransport.emitError(Exception("stray log line"))
    answered.set_result({"content": []})
    assert await call == {"content": []}
    assert [str(error) for error in errors] == ["stray log line"]
    await client.close()


async def test_accepts_servers_that_answer_with_an_older_protocol_version(servers):
    clientTransport, server = await createServer(servers)
    server.setHandler("initialize", lambda _request: {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "serverInfo": {"name": "old-server", "version": "0.1.0"},
    })
    client = McpClient({"name": "test-client", "version": "1.0.0"})
    await client.connect(clientTransport)
    assert client.protocolVersion == "2024-11-05"
    await client.close()

    unsupportedTransport, unsupported = await createServer(servers)
    unsupported.setHandler("initialize", lambda _request: {
        "protocolVersion": "1999-01-01",
        "capabilities": {},
        "serverInfo": {"name": "ancient-server", "version": "0.1.0"},
    })
    rejected = McpClient({"name": "test-client", "version": "1.0.0"})
    with pytest.raises(RuntimeError, match="unsupported protocol version"):
        await rejected.connect(unsupportedTransport)
    assert rejected.connectionState == "closed"


async def test_defaults_missing_tool_result_content_to_an_empty_list(servers):
    client, server = await connect(servers)
    server.setHandler("tools/call", lambda _request: {"structuredContent": {"ok": True}})
    assert await client.callTool("structured") == {"content": [], "structuredContent": {"ok": True}}
    server.setHandler("tools/call", lambda _request: {"content": "not a list"})
    with pytest.raises(McpError, match="Invalid MCP tools/call result"):
        await client.callTool("broken")
    await client.close()


async def test_the_in_memory_transport_takes_send_options():
    """MISAKA adaptation: the client passes ``send(message, {"signal": ...})`` to every transport (#10598); the
    in-memory pair takes the options and ignores them, as upstream's one-parameter ``send`` does."""
    pair = createInMemoryTransportPair()
    received: list = []
    pair["server"].onMessage(received.append)
    await pair["server"].start()
    await pair["client"].start()
    aborted = AbortController()
    aborted.abort()
    await pair["client"].send({"jsonrpc": "2.0", "method": "notifications/initialized"}, {"signal": aborted.signal})
    await pair["client"].send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    await flush()
    assert len(received) == 2
    await pair["client"].close()


# #10416: MCP 2026-07-28 allows any JSON value as structured content.
async def test_accepts_structured_content_that_is_not_an_object(servers):
    client, server = await connect(servers)
    for structuredContent in ([{"id": 1}, {"id": 2}], None, "text", 3, False):
        server.setHandler("tools/call", lambda _request, value=structuredContent: {"content": [], "structuredContent": value})
        assert await client.callTool("structured") == {"content": [], "structuredContent": structuredContent}
    await client.close()


async def test_does_not_send_notifications_cancelled_for_a_timed_out_initialize(servers):
    clientTransport, server = await createServer(servers)
    server.setHandler("initialize", lambda _request: never())
    client = McpClient({"name": "test-client", "version": "1.0.0", "requestTimeoutMs": 5})
    with pytest.raises(McpTimeoutError):
        await client.connect(clientTransport)
    await flush()
    assert not any(message.get("method") == "notifications/cancelled" for message in server.messages)


async def test_notifies_close_listeners_once_when_the_transport_drops(servers):
    client, server = await connect(servers)
    closed: list = []
    client.onClose(lambda: closed.append(True))
    server.setHandler("tools/call", lambda _request: never())
    pending = asyncio.ensure_future(client.callTool("wait"))
    await flush()
    await server.transport.close()
    with pytest.raises(Exception, match="MCP connection closed"):
        await pending
    assert client.connectionState == "closed"
    await client.close()
    assert len(closed) == 1


async def test_answers_roots_list_and_dispatches_notifications(servers):
    clientTransport, server = await createServer(servers)
    client = McpClient({
        "name": "test-client",
        "version": "1.0.0",
        "roots": [{"uri": "file:///workspace", "name": "workspace"}],
    })
    await client.connect(clientTransport)
    changed: list = []
    client.onNotification("notifications/tools/list_changed", changed.append)
    await server.transport.send({"jsonrpc": "2.0", "id": "roots", "method": "roots/list"})
    await server.transport.send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
    await flush()
    assert {
        "jsonrpc": "2.0",
        "id": "roots",
        "result": {"roots": [{"uri": "file:///workspace", "name": "workspace"}]},
    } in server.messages
    # A notification without params hands its listeners `undefined`, as upstream.
    assert changed == [UNDEFINED]
    await client.close()


# -- MISAKA forks ----------------------------------------------------------------------------


async def test_a_cancelled_caller_cancels_its_request(servers):
    client, server = await connect(servers)
    server.setHandler("tools/call", lambda _request: never())
    call = asyncio.ensure_future(client.callTool("wait", {}, {"timeoutMs": 60_000}))
    await flush()
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    await flush()
    assert {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 2, "reason": "Cancelled"}} in (
        server.messages
    )
    assert client._pending == {}
    await client.close()


async def test_a_cancelled_connect_closes_the_transport(servers):
    clientTransport, server = await createServer(servers)
    server.setHandler("initialize", lambda _request: never())
    client = McpClient({"name": "test-client", "version": "1.0.0"})
    connecting = asyncio.ensure_future(client.connect(clientTransport))
    await flush()
    connecting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await connecting
    assert client.connectionState == "closed"
    assert clientTransport._closed


async def test_a_boolean_id_or_progress_token_never_matches_a_request(servers):
    client, server = await connect(servers)
    answered = asyncio.get_running_loop().create_future()
    server.setHandler("tools/call", lambda _request: answered)
    progress: list = []
    errors: list = []
    client.onError(errors.append)
    call = asyncio.ensure_future(client.callTool("wait", {}, {"onProgress": progress.append}))
    await flush()
    await server.transport.send({"jsonrpc": "2.0", "method": "notifications/progress",
                                 "params": {"progressToken": True, "progress": 1}})
    await server.transport.send({"jsonrpc": "2.0", "id": True, "result": {"content": []}})
    await flush()
    assert progress == [] and not call.done()
    answered.set_result({"content": [{"type": "text", "text": "real"}]})
    assert (await call)["content"] == [{"type": "text", "text": "real"}]
    await client.close()


# -- content.test.ts -------------------------------------------------------------------------


def test_passes_text_and_images_through_and_replaces_other_blocks_with_text():
    blocks = [
        {"type": "text", "text": "hello", "annotations": {"priority": 1}},
        {"type": "image", "data": "aW1n", "mimeType": "image/png", "_meta": {"x": 1}},
        {"type": "audio", "data": "YXVk", "mimeType": "audio/wav"},
        {"type": "resource_link", "uri": "file:///a.txt", "name": "a.txt"},
        {"type": "resource", "resource": {"uri": "file:///b.txt", "text": "inline"}},
        {"type": "resource", "resource": {"uri": "file:///c.png", "mimeType": "image/png", "blob": "Yw=="}},
        {"type": "resource", "resource": {"uri": "file:///d.bin", "blob": "ZA=="}},
    ]
    assert toLlmContent({"content": blocks}) == [
        {"type": "text", "text": "hello"},
        {"type": "image", "data": "aW1n", "mimeType": "image/png"},
        {"type": "text", "text": "[audio audio/wav omitted]"},
        {"type": "text", "text": "a.txt: file:///a.txt"},
        {"type": "text", "text": "inline"},
        {"type": "image", "data": "Yw==", "mimeType": "image/png"},
        {"type": "text", "text": "[binary resource file:///d.bin (unknown type) omitted]"},
    ]


def test_falls_back_to_the_structured_content_as_json_when_there_are_no_blocks():
    # #10416: structured content may be any JSON value.
    assert toLlmContent({"content": [], "structuredContent": [1, 2]}) == [{"type": "text", "text": "[\n  1,\n  2\n]"}]
    assert toLlmContent({"content": [], "structuredContent": None}) == [{"type": "text", "text": "null"}]
    assert toLlmContent({"content": [], "structuredContent": {"n": 1}}) == [{"type": "text", "text": '{\n  "n": 1\n}'}]
    assert toLlmContent({"content": [{"type": "text", "text": "n=1"}], "structuredContent": {"n": 1}}) == [
        {"type": "text", "text": "n=1"},
    ]
    # JSON.stringify: integral numbers without a fraction, text kept as text.
    assert toLlmContent({"content": [], "structuredContent": {"n": 2.0, "s": "é"}}) == [
        {"type": "text", "text": '{\n  "n": 2,\n  "s": "é"\n}'},
    ]

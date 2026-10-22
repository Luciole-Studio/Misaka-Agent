"""pi's ``packages/mcp/test/streamable-http.test.ts``, plus the MISAKA forks of the fetch shim the
transport runs on."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from misaka.mcp.client import McpClient
from misaka.mcp.protocol.jsonrpc import McpAbortError, McpError, McpTimeoutError
from misaka.mcp.protocol.types import LATEST_PROTOCOL_VERSION
from misaka.mcp.transports.streamable_http import (
    McpAuthRequiredError,
    McpHttpError,
    McpSessionExpiredError,
    StreamableHttpTransport,
    consumeSseStream,
)
from misaka.utils import whatwg_fetch as _fetch
from misaka.utils.js import AbortController
from tests.mcp_helpers import closeServers, listen, readBody


@pytest.fixture(autouse=True)
def _noProxy(monkeypatch):
    """The shim honours the proxy environment, which would route the loopback servers elsewhere."""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
async def _servers():
    yield
    await closeServers()


async def startServer(handler):
    requests: list[dict] = []
    origin = await listen(lambda request, response, _origin: handler(request, response, requests))
    return {"url": f"{origin}/mcp", "requests": requests}


async def protocolHandler(request, response, requests, body=None):
    if request.method == "GET":
        requests.append({"method": "GET", "headers": request.headers})
        response.statusCode = 405
        response.end()
        return
    if request.method == "DELETE":
        requests.append({"method": "DELETE", "headers": request.headers})
        response.statusCode = 200
        response.end()
        return
    message = body if body is not None else json.loads(await readBody(request))
    requests.append({"method": request.method, "headers": request.headers, "message": message})
    if "id" not in message:
        response.statusCode = 202
        response.end()
        return
    if message["method"] == "initialize":
        response.writeHead(200, {"content-type": "application/json", "mcp-session-id": "session-1"})
        response.end(json.dumps({
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "http-fixture", "version": "1.0.0"},
            },
        }))
        return
    if message["method"] == "tools/list":
        response.setHeader("content-type", "application/json")
        response.end(json.dumps({
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]},
        }))
        return
    response.writeHead(200, {"content-type": "text/event-stream"})
    response.write("id: tool-result\n")
    result = {"jsonrpc": "2.0", "id": message["id"], "result": {"content": [{"type": "text", "text": "hello"}]}}
    response.end(f"data: {json.dumps(result)}\n\n")


async def chunks(*items: str):
    for item in items:
        yield item.encode("utf-8")


# -- consumeSseStream -------------------------------------------------------------------------


async def test_parses_chunked_crlf_events_comments_ids_and_multiline_data():
    events: list = []
    await consumeSseStream(
        chunks(': keepalive\r\nid: 7\r\ndata: {"one":\r\n', "data: 1}\r\n\r\n"),
        {"onEvent": events.append},
    )
    assert events == [{"id": "7", "data": '{"one":\n1}'}]


async def test_rejects_events_whose_data_lines_exceed_the_limit_without_a_blank_line():
    sent = 0

    async def stream():
        nonlocal sent
        while True:
            # Never sends a blank line, so the event is never dispatched.
            sent += 1
            if sent - 1 > 1000:
                return
            yield b"data: xxxxxxxxxxxxxxxx\n"

    with pytest.raises(RuntimeError, match="MCP SSE event exceeds 256 bytes"):
        await consumeSseStream(stream(), {"maxEventBytes": 256, "onEvent": lambda _event: None})
    assert sent < 100


async def test_a_retry_too_long_for_an_integer_is_infinity_as_number_reads_it():
    retries: list = []
    await consumeSseStream(chunks(f"retry: {'9' * 5000}\n", "data: x\n\n"), {"onEvent": lambda _event: None, "onRetry": retries.append})
    assert retries == [float("inf")]


def test_a_reconnect_delay_past_the_float_range_is_the_maximum_delay():
    transport = StreamableHttpTransport({"url": "http://127.0.0.1:1/mcp", "reconnect": {"initialDelayMs": 1.5, "maxDelayMs": 30_000}})
    assert transport._reconnectDelay(5000, None) == 30_000


# -- StreamableHttpTransport ------------------------------------------------------------------


async def test_handles_json_and_sse_responses_with_session_and_protocol_headers():
    started = await startServer(protocolHandler)
    url, requests = started["url"], started["requests"]
    transport = StreamableHttpTransport({"url": url})
    client = McpClient({"name": "http-test", "version": "1.0.0"})
    await client.connect(transport)
    assert transport.sessionId == "session-1"
    assert await client.listTools() == [{"name": "echo", "inputSchema": {"type": "object"}}]
    assert await client.callTool("echo", {"text": "hello"}) == {"content": [{"type": "text", "text": "hello"}]}
    await client.close()

    listRequest = next(entry for entry in requests if entry.get("message", {}).get("method") == "tools/list")
    assert listRequest["headers"]["mcp-session-id"] == "session-1"
    assert listRequest["headers"]["mcp-protocol-version"] == LATEST_PROTOCOL_VERSION
    assert any(entry["method"] == "GET" for entry in requests)
    assert any(entry["method"] == "DELETE" for entry in requests)


async def test_classifies_authentication_failures():
    async def handler(request, response, _requests):
        await readBody(request)
        response.writeHead(401, {"www-authenticate": 'Bearer resource_metadata="https://example.com/meta"'})
        response.end("login required")

    url = (await startServer(handler))["url"]
    client = McpClient({"name": "http-test", "version": "1.0.0"})
    with pytest.raises(McpAuthRequiredError) as failure:
        await client.connect(StreamableHttpTransport({"url": url}))
    error = failure.value
    assert (error.name, error.status, error.body, error.wwwAuthenticate) == (
        "McpAuthRequiredError",
        401,
        "login required",
        'Bearer resource_metadata="https://example.com/meta"',
    )


async def test_fails_only_the_request_whose_sse_stream_breaks():
    slowGate: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def handler(request, response, requests):
        if request.method != "POST":
            return await protocolHandler(request, response, requests)
        message = json.loads(await readBody(request))
        name = (message.get("params") or {}).get("name")
        if name == "broken":
            response.writeHead(200, {"content-type": "text/event-stream"})
            response.end("data: not json\n\n")
            return
        if name == "slow":
            await slowGate
        await protocolHandler(request, response, requests, message)

    url = (await startServer(handler))["url"]
    client = McpClient({"name": "http-test", "version": "1.0.0"})
    errors: list = []
    client.onError(errors.append)
    await client.connect(StreamableHttpTransport({"url": url, "openGetStream": False}))

    slow = asyncio.ensure_future(client.callTool("slow"))
    with pytest.raises(McpError, match="MCP response stream failed"):
        await client.callTool("broken")
    slowGate.set_result(None)
    assert await slow == {"content": [{"type": "text", "text": "hello"}]}
    assert len(errors) == 1
    await client.close()


async def test_opens_the_get_stream_after_initialization_and_sends_last_event_id_only_when_resuming():
    order: list[str] = []

    async def handler(request, response, requests):
        if request.method == "POST":
            message = json.loads(await readBody(request))
            order.append(str(message["method"]))
            return await protocolHandler(request, response, requests, message)
        order.append(request.method)
        return await protocolHandler(request, response, requests)

    started = await startServer(handler)
    url, requests = started["url"], started["requests"]
    client = McpClient({"name": "http-test", "version": "1.0.0"})
    await client.connect(StreamableHttpTransport({"url": url}))
    await client.callTool("echo")
    await client.listTools()
    await client.close()
    assert order.index("GET") > order.index("notifications/initialized")
    assert all("last-event-id" not in entry["headers"] for entry in requests)


async def test_resumes_a_response_stream_the_server_closed_before_answering():
    resumeHeaders: list = []

    async def handler(request, response, requests):
        if request.method == "GET" and request.headers.get("last-event-id"):
            resumeHeaders.append(request.headers["last-event-id"])
            response.writeHead(200, {"content-type": "text/event-stream"})
            resumed = {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": "resumed"}]}}
            response.end(f"id: 2\ndata: {json.dumps(resumed)}\n\n")
            return
        if request.method != "POST":
            return await protocolHandler(request, response, requests)
        message = json.loads(await readBody(request))
        if message["method"] != "tools/call":
            return await protocolHandler(request, response, requests, message)
        # Priming event (ID, no data) and a retry hint, then the server drops the stream.
        response.writeHead(200, {"content-type": "text/event-stream"})
        response.end("id: 1\nretry: 5\ndata:\n\n")

    url = (await startServer(handler))["url"]
    client = McpClient({"name": "http-test", "version": "1.0.0"})
    errors: list = []
    client.onError(errors.append)
    await client.connect(StreamableHttpTransport({"url": url, "openGetStream": False}))
    assert await client.callTool("echo") == {"content": [{"type": "text", "text": "resumed"}]}
    assert resumeHeaders == ["1"]
    assert errors == []
    await client.close()


async def test_fails_a_request_whose_response_stream_ends_without_an_answer():
    async def handler(request, response, requests):
        if request.method != "POST":
            return await protocolHandler(request, response, requests)
        message = json.loads(await readBody(request))
        if message["method"] != "tools/call":
            return await protocolHandler(request, response, requests, message)
        response.writeHead(200, {"content-type": "text/event-stream"})
        response.end(": nothing here\n\n")

    url = (await startServer(handler))["url"]
    client = McpClient({"name": "http-test", "version": "1.0.0"})
    await client.connect(StreamableHttpTransport({"url": url, "openGetStream": False}))
    with pytest.raises(McpError, match="MCP response stream failed: stream ended without a response"):
        await client.callTool("echo", {}, {"timeoutMs": 5_000})
    await client.close()


async def test_reconnects_the_get_stream_after_it_drops():
    gets = 0
    lastEventIds: list = []

    async def handler(request, response, requests):
        nonlocal gets
        if request.method != "GET":
            return await protocolHandler(request, response, requests)
        gets += 1
        lastEventIds.append(request.headers.get("last-event-id"))
        response.writeHead(200, {"content-type": "text/event-stream"})
        notification = {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
        if gets == 1:
            response.end(f"id: g1\ndata: {json.dumps(notification)}\n\n")
            return
        response.write(f"id: g2\ndata: {json.dumps(notification)}\n\n")

    url = (await startServer(handler))["url"]
    client = McpClient({"name": "http-test", "version": "1.0.0"})
    changes = 0
    secondChange: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def onChange(_params) -> None:
        nonlocal changes
        changes += 1
        if changes == 2 and not secondChange.done():
            secondChange.set_result(None)

    client.onNotification("notifications/tools/list_changed", onChange)
    await client.connect(StreamableHttpTransport({"url": url, "reconnect": {"initialDelayMs": 1}}))
    await asyncio.wait_for(secondChange, 5)
    assert lastEventIds == [None, "g1"]
    await client.close()


async def test_rejects_a_request_the_server_accepts_without_a_response():
    async def handler(request, response, requests):
        if request.method != "POST":
            return await protocolHandler(request, response, requests)
        message = json.loads(await readBody(request))
        if message["method"] != "tools/call":
            return await protocolHandler(request, response, requests, message)
        response.statusCode = 202
        response.end()

    url = (await startServer(handler))["url"]
    client = McpClient({"name": "http-test", "version": "1.0.0"})
    await client.connect(StreamableHttpTransport({"url": url, "openGetStream": False}))
    with pytest.raises(McpHttpError, match="without a response"):
        await client.callTool("echo")
    await client.close()


async def test_includes_the_response_body_in_http_errors():
    async def handler(request, response, _requests):
        await readBody(request)
        response.statusCode = 400
        response.end("Invalid Accept header")

    url = (await startServer(handler))["url"]
    client = McpClient({"name": "http-test", "version": "1.0.0"})
    with pytest.raises(McpHttpError, match="MCP HTTP request failed with status 400: Invalid Accept header"):
        await client.connect(StreamableHttpTransport({"url": url}))


async def test_hands_401_and_insufficient_scope_403_responses_to_the_auth_provider_with_the_rejected_token():
    seen: list = []
    token = "old"

    async def handler(request, response, requests):
        if request.method != "POST":
            return await protocolHandler(request, response, requests)
        message = json.loads(await readBody(request))
        if request.headers.get("authorization") == "Bearer old":
            response.writeHead(401, {"www-authenticate": "Bearer"}).end()
            return
        if message["method"] == "tools/call" and request.headers.get("authorization") == "Bearer new":
            response.writeHead(403, {"www-authenticate": 'Bearer error="insufficient_scope", scope="admin"'}).end()
            return
        return await protocolHandler(request, response, requests, message)

    async def currentToken():
        return token

    async def onUnauthorized(context):
        nonlocal token
        seen.append({"status": context["response"].status, "token": context.get("token")})
        token = "new" if context["response"].status == 401 else "admin"

    url = (await startServer(handler))["url"]
    client = McpClient({"name": "http-test", "version": "1.0.0"})
    await client.connect(StreamableHttpTransport({
        "url": url,
        "openGetStream": False,
        "authProvider": SimpleNamespace(token=currentToken, onUnauthorized=onUnauthorized),
    }))
    assert await client.callTool("echo") == {"content": [{"type": "text", "text": "hello"}]}
    assert seen == [{"status": 401, "token": "old"}, {"status": 403, "token": "new"}]
    await client.close()


# #10598: a request cancelled or timed out while waiting for a shared refresh must not be retried.
async def test_does_not_retry_requests_that_were_cancelled_while_authorization_was_recovering():
    token = "old"
    executed: list = []

    async def handler(request, response, requests):
        if request.method != "POST":
            return await protocolHandler(request, response, requests)
        message = json.loads(await readBody(request))
        if message.get("method") == "tools/call" and request.headers.get("authorization") == "Bearer old":
            response.writeHead(401, {"www-authenticate": "Bearer"}).end()
            return
        if message.get("method") == "tools/call":
            executed.append(message["params"]["name"])
        return await protocolHandler(request, response, requests, message)

    url = (await startServer(handler))["url"]
    loop = asyncio.get_running_loop()
    refreshGate: asyncio.Future[None] = loop.create_future()
    waitingGate: asyncio.Future[None] = loop.create_future()
    refresh: asyncio.Future[None] | None = None
    refreshes = 0
    waiting = 0

    async def currentToken():
        return token

    async def refreshed():
        nonlocal token
        await refreshGate
        token = "new"

    async def onUnauthorized(_context):
        nonlocal refresh, refreshes, waiting
        if refresh is None:
            refreshes += 1
            refresh = asyncio.ensure_future(refreshed())
        waiting += 1
        if waiting == 3:
            waitingGate.set_result(None)
        await asyncio.shield(refresh)

    client = McpClient({"name": "http-test", "version": "1.0.0"})
    await client.connect(StreamableHttpTransport({
        "url": url,
        "openGetStream": False,
        "authProvider": SimpleNamespace(token=currentToken, onUnauthorized=onUnauthorized),
    }))
    controller = AbortController()
    aborted = asyncio.ensure_future(client.callTool("aborted", {}, {"signal": controller.signal}))
    timedOut = asyncio.ensure_future(client.callTool("timed-out", {}, {"timeoutMs": 200}))
    kept = asyncio.ensure_future(client.callTool("kept"))
    await waitingGate
    controller.abort()
    with pytest.raises(McpAbortError) as abortedError:
        await aborted
    assert abortedError.value.name == "AbortError"
    with pytest.raises(McpTimeoutError) as timeoutError:
        await timedOut
    assert timeoutError.value.name == "McpTimeoutError"
    refreshGate.set_result(None)
    assert await kept == {"content": [{"type": "text", "text": "hello"}]}
    assert executed == ["kept"]
    assert refreshes == 1
    await client.close()


async def test_a_send_whose_signal_already_aborted_never_reaches_the_server():
    """MISAKA adaptation: the dispatch check sits after the token read, so a message whose sender gave up
    before ``send`` ran is not posted at all."""
    started = await startServer(protocolHandler)
    transport = StreamableHttpTransport({"url": started["url"], "openGetStream": False})
    client = McpClient({"name": "http-test", "version": "1.0.0"})
    await client.connect(transport)
    before = len(started["requests"])
    controller = AbortController()
    controller.abort()
    with pytest.raises(McpAbortError):
        await transport.send({"jsonrpc": "2.0", "method": "notifications/ping"}, {"signal": controller.signal})
    assert len(started["requests"]) == before
    await client.close()


# #10565: the auth provider may refresh over the network, which closing must not wait for.
async def test_closes_the_session_with_the_last_requests_token_without_asking_the_auth_provider():
    server = await startServer(protocolHandler)
    calls = 0

    async def token():
        nonlocal calls
        calls += 1
        return f"token-{calls}"

    client = McpClient({"name": "http-test", "version": "1.0.0"})
    await client.connect(StreamableHttpTransport({
        "url": server["url"],
        "openGetStream": False,
        "authProvider": SimpleNamespace(token=token),
    }))
    before = calls
    await client.close()
    assert calls == before
    deletes = [entry for entry in server["requests"] if entry["method"] == "DELETE"]
    assert [entry["headers"].get("authorization") for entry in deletes] == [f"Bearer token-{before}"]
    assert deletes[0]["headers"].get("mcp-session-id") == "session-1"


async def test_uses_the_injected_fetch_and_hands_it_to_the_auth_provider():
    """Upstream's "calls fetch without a receiver" (#10188) guards a JavaScript `this` problem Python
    does not have; the injected fetch and its reuse by the auth provider are what is left to check."""
    calls = 0

    async def countingFetch(input, init=None):
        nonlocal calls
        calls += 1
        return await _fetch.fetch(input, init)

    async def handler(request, response, requests):
        if request.method == "POST" and "authorization" not in request.headers:
            await readBody(request)
            response.writeHead(401, {"www-authenticate": "Bearer"}).end()
            return
        return await protocolHandler(request, response, requests)

    url = (await startServer(handler))["url"]
    token = None

    async def currentToken():
        return token

    async def onUnauthorized(context):
        nonlocal token
        response = await context["fetch"](url)
        if response.body is not None:
            await response.body.cancel()
        token = "token"

    client = McpClient({"name": "http-test", "version": "1.0.0"})
    await client.connect(StreamableHttpTransport({
        "url": url,
        "fetch": countingFetch,
        "openGetStream": False,
        "authProvider": SimpleNamespace(token=currentToken, onUnauthorized=onUnauthorized),
    }))
    assert await client.listTools() == [{"name": "echo", "inputSchema": {"type": "object"}}]
    await client.close()
    # initialize (401, the provider's own GET, the retry), notifications/initialized, tools/list, and
    # the DELETE that ends the session.
    assert calls == 6


async def test_classifies_an_expired_established_session():
    posts = 0

    async def handler(request, response, requests):
        nonlocal posts
        if request.method == "POST":
            posts += 1
            if posts - 1 >= 2:
                await readBody(request)
                response.statusCode = 404
                response.end("gone")
                return
        await protocolHandler(request, response, requests)

    url = (await startServer(handler))["url"]
    client = McpClient({"name": "http-test", "version": "1.0.0"})
    await client.connect(StreamableHttpTransport({"url": url, "openGetStream": False}))
    with pytest.raises(McpSessionExpiredError):
        await client.listTools()
    await client.close()


# -- MISAKA: the fetch shim ---------------------------------------------------------------------


async def test_a_network_failure_is_a_type_error_as_fetch_reports_it():
    with pytest.raises(TypeError, match="fetch failed"):
        await _fetch.fetch("http://127.0.0.1:1/mcp")


async def test_text_decodes_utf8_and_drops_the_byte_order_mark_whatever_the_charset_says():
    async def handler(request, response, _origin):
        response.writeHead(200, {"content-type": "text/plain; charset=latin-1"})
        response.end("﻿café".encode())

    origin = await listen(handler)
    response = await _fetch.fetch(f"{origin}/")
    assert await response.text() == "café"
    assert await _fetch.Response("﻿{}").json() == {}


async def test_a_constructed_response_reads_like_a_fetched_one():
    response = _fetch.Response(None, status=401, headers={"WWW-Authenticate": "Bearer"})
    assert (response.status, response.ok, response.body) == (401, False, None)
    assert response.headers.get("www-authenticate") == "Bearer"
    assert response.headers.get("missing") is None
    assert await response.text() == ""


async def test_close_does_not_wait_for_a_stream_reader_stuck_in_the_auth_provider():
    from misaka.utils.js import spawn

    closed: list = []
    transport = StreamableHttpTransport({"url": "http://127.0.0.1:1/mcp"})
    transport.onClose(lambda: closed.append(True))
    await transport.start()
    # A GET stream reader waiting on a token that never comes.
    spawn(asyncio.Event().wait(), transport._tasks)

    await asyncio.wait_for(transport.close(), 1)
    assert closed == [True]

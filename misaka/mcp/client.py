"""The MCP client: the initialize handshake, requests with timeouts, progress and cancellation,
paginated lists, tool calls, and the server-to-client requests a client answers.

Translated from pi's ``packages/mcp/src/client.ts``.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final, Literal, NotRequired, TypedDict

from misaka.mcp.protocol.content import CallToolResult
from misaka.mcp.protocol.jsonrpc import (
    JSON_RPC_ERROR_CODES,
    JsonRpcId,
    JsonRpcMessage,
    JsonRpcRequest,
    JsonRpcResponse,
    McpAbortError,
    McpConnectionClosedError,
    McpError,
    McpTimeoutError,
    isJsonRpcId,
    isJsonRpcNotification,
    isJsonRpcRequest,
    isJsonRpcResponse,
    isObject,
    toError,
)
from misaka.mcp.protocol.types import (
    LATEST_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    ClientCapabilities,
    Implementation,
    InitializeResult,
    ListResourcesResult,
    ListResourceTemplatesResult,
    ProgressNotification,
    ReadResourceResult,
    Resource,
    ResourceTemplate,
    Root,
    ServerCapabilities,
    SupportedProtocolVersion,
    Tool,
)
from misaka.mcp.transports.transport import McpTransport
from misaka.utils.js import (
    UNDEFINED,
    AbortController,
    Timeout,
    abortReason,
    addAbortListener,
    awaitValue,
    clearTimeout,
    eager,
    isAborted,
    isNumber,
    jsString,
    liveKeys,
    nullishCoalesce,
    setTimeout,
    spawn,
)

DEFAULT_REQUEST_TIMEOUT_MS: Final = 30_000
MAX_LIST_PAGES: Final = 1_000

type ClientState = Literal["idle", "connecting", "connected", "closed"]
type NotificationListener = Callable[[Any], None]
type ErrorListener = Callable[[Exception], None]
type CloseListener = Callable[[], None]
type RequestHandler = Callable[[Any, dict[str, Any]], Any]


class McpClientOptions(Implementation, total=False):
    capabilities: ClientCapabilities
    protocolVersion: SupportedProtocolVersion
    requestTimeoutMs: float
    roots: Sequence[Root] | Callable[[], Sequence[Root] | Awaitable[Sequence[Root]]]


class McpRequestOptions(TypedDict, total=False):
    signal: Any
    timeoutMs: float
    onProgress: Callable[[ProgressNotification], None]


@dataclass(slots=True)
class PendingRequest:
    resolve: Callable[[Any], None]
    reject: Callable[[BaseException], None]
    timeoutMs: float
    timer: Timeout | None
    signal: Any
    onAbort: Callable[[], None]
    # Aborted when the request settles, so the transport does not dispatch (or retry) it afterwards.
    sendController: AbortController
    cancellable: bool
    onProgress: Callable[[ProgressNotification], None] | None
    progressToken: JsonRpcId | None
    # MISAKA fork: `removeEventListener("abort", ...)`, which a signal without listeners (one of misaka's
    # flag-style signals) answers with the watcher that stands in for the listener.
    removeAbortListener: Callable[[], None] = field(default=lambda: None)


def validateInitializeResult(value: Any) -> InitializeResult:
    if (
        not isObject(value)
        or not isinstance(value.get("protocolVersion"), str)
        or not isObject(value.get("capabilities"))
        or not isObject(value.get("serverInfo"))
        or not isinstance(value["serverInfo"].get("name"), str)
        or not isinstance(value["serverInfo"].get("version"), str)
        or ("instructions" in value and not isinstance(value["instructions"], str))
    ):
        raise McpError(JSON_RPC_ERROR_CODES.invalidRequest, "Invalid MCP initialize result")
    return value  # type: ignore[return-value]


def invalid(message: str) -> McpError:
    return McpError(JSON_RPC_ERROR_CODES.invalidRequest, message)


class _ListPage(TypedDict):
    items: list[dict[str, Any]]
    nextCursor: NotRequired[str]


def validateListPage(
    method: str, key: str, value: Any, isItem: Callable[[dict[str, Any]], bool]
) -> _ListPage:
    """One page of a paginated list: the items under ``key``, each checked by ``isItem``."""
    items = value.get(key) if isObject(value) else None
    if not isObject(value) or not isinstance(items, list):
        raise invalid(f"Invalid MCP {method} result")
    for item in items:
        if not isObject(item) or not isItem(item):
            raise invalid(f"Invalid entry in MCP {method} result")
    # Some servers end pagination with `null` or `""` instead of omitting the cursor.
    nextCursor = value.get("nextCursor", UNDEFINED)
    if nextCursor is None or nextCursor == "":
        nextCursor = UNDEFINED
    if nextCursor is not UNDEFINED and not isinstance(nextCursor, str):
        raise invalid(f"Invalid MCP {method} cursor")
    page: _ListPage = {"items": items}
    if isinstance(nextCursor, str):
        page["nextCursor"] = nextCursor
    return page


def isTool(tool: dict[str, Any]) -> bool:
    return isinstance(tool.get("name"), str) and isObject(tool.get("inputSchema"))


# `name` is required by the spec, but some servers omit it; the URI stands in.
def isResource(resource: dict[str, Any]) -> bool:
    return isinstance(resource.get("uri"), str) and ("name" not in resource or isinstance(resource["name"], str))


def isResourceTemplate(template: dict[str, Any]) -> bool:
    return isinstance(template.get("uriTemplate"), str) and (
        "name" not in template or isinstance(template["name"], str)
    )


def toResource(item: dict[str, Any]) -> Resource:
    return {**item, "name": item["uri"] if item.get("name") is None else item["name"]}  # type: ignore[typeddict-item]


def toResourceTemplate(item: dict[str, Any]) -> ResourceTemplate:
    return {  # type: ignore[typeddict-item]
        **item,
        "name": item["uriTemplate"] if item.get("name") is None else item["name"],
    }


def pageCursor(page: _ListPage) -> dict[str, str]:
    return {"nextCursor": page["nextCursor"]} if "nextCursor" in page else {}


def validateReadResourceResult(value: Any) -> ReadResourceResult:
    if not isObject(value) or not isinstance(value.get("contents"), list):
        raise invalid("Invalid MCP resources/read result")
    for contents in value["contents"]:
        if (
            not isObject(contents)
            or not isinstance(contents.get("uri"), str)
            or (not isinstance(contents.get("text"), str) and not isinstance(contents.get("blob"), str))
        ):
            raise invalid("Invalid contents in MCP resources/read result")
    return value  # type: ignore[return-value]


def validateCallToolResult(value: Any) -> CallToolResult:
    """``content`` is required by the spec, but servers that only return ``structuredContent`` omit it
    (the SDK defaults it too). ``structuredContent`` may be any JSON value, as MCP 2026-07-28 allows."""
    if not isObject(value) or ("content" in value and not isinstance(value["content"], list)):
        raise McpError(JSON_RPC_ERROR_CODES.invalidRequest, "Invalid MCP tools/call result")
    return ({**value, "content": []} if "content" not in value else value)  # type: ignore[return-value]


class McpClient:
    def __init__(self, options: McpClientOptions) -> None:
        self.options = MappingProxyType(dict(options))
        self._state: ClientState = "idle"
        self._transport: McpTransport | None = None
        self._nextRequestId = 1
        self._serverInfoValue: Implementation | None = None
        self._serverCapabilitiesValue: ServerCapabilities | None = None
        self._instructionsValue: str | None = None
        self._protocolVersionValue: str | None = None
        self._pending: dict[JsonRpcId, PendingRequest] = {}
        self._progressRequests: dict[JsonRpcId, JsonRpcId] = {}
        self._incoming: dict[JsonRpcId, AbortController] = {}
        self._requestHandlers: dict[str, RequestHandler] = {}
        # Dicts keep insertion order, as a JavaScript Set does.
        self._notificationListeners: dict[str, dict[NotificationListener, None]] = {}
        self._errorListeners: dict[ErrorListener, None] = {}
        self._closeListeners: dict[CloseListener, None] = {}
        self._disposers: list[Callable[[], Any]] = []
        # MISAKA fork: asyncio holds tasks weakly; fire-and-forget sends live here until they finish.
        self._tasks: set[asyncio.Task[Any]] = set()
        self._requestHandlers["ping"] = lambda _params, _context: {}
        roots = options.get("roots")
        if roots is not None:

            async def listRoots(_params: Any, _context: dict[str, Any]) -> dict[str, Any]:
                return {"roots": list(await awaitValue(roots()) if callable(roots) else roots)}

            self._requestHandlers["roots/list"] = listRoots

    @property
    def connectionState(self) -> ClientState:
        return self._state

    @property
    def serverInfo(self) -> Implementation | None:
        return self._serverInfoValue

    @property
    def serverCapabilities(self) -> ServerCapabilities | None:
        return self._serverCapabilitiesValue

    @property
    def instructions(self) -> str | None:
        return self._instructionsValue

    @property
    def protocolVersion(self) -> str | None:
        return self._protocolVersionValue

    @eager
    async def connect(self, transport: McpTransport) -> InitializeResult:
        if self._state != "idle":
            raise RuntimeError(f"Cannot connect MCP client in {self._state} state")
        self._state = "connecting"
        self._transport = transport
        self._disposers = [
            transport.onMessage(lambda message: self._handleMessage(message)),
            # Transport errors are reported only. Pending requests fail when the transport closes.
            transport.onError(lambda error: self._emitError(error)),
            transport.onClose(lambda: self._handleTransportClose()),
        ]

        # MISAKA fork: a cancelled connect closes the transport too (a JavaScript promise cannot be
        # cancelled), or the stdio server it started keeps running.
        try:
            await transport.start()
            capabilities: ClientCapabilities = {**(self.options.get("capabilities") or {})}  # type: ignore[typeddict-item]
            if self.options.get("roots") is not None and "roots" not in capabilities:
                capabilities["roots"] = {}
            result = validateInitializeResult(
                await self._requestInternal(
                    "initialize",
                    {
                        "protocolVersion": nullishCoalesce(self.options.get("protocolVersion"), LATEST_PROTOCOL_VERSION),
                        "capabilities": capabilities,
                        "clientInfo": {
                            "name": self.options["name"],
                            "version": self.options["version"],
                            **({} if self.options.get("title") is None else {"title": self.options["title"]}),
                        },
                    },
                    {},
                    True,
                )
            )
            if result["protocolVersion"] not in SUPPORTED_PROTOCOL_VERSIONS:
                raise RuntimeError(f"MCP server selected unsupported protocol version {result['protocolVersion']}")
            self._protocolVersionValue = result["protocolVersion"]
            self._serverInfoValue = result["serverInfo"]
            self._serverCapabilitiesValue = result["capabilities"]
            self._instructionsValue = result.get("instructions")
            setProtocolVersion = getattr(transport, "setProtocolVersion", None)
            if setProtocolVersion is not None:
                setProtocolVersion(result["protocolVersion"])
            await self._notifyInternal("notifications/initialized", None, True)
            self._state = "connected"
            return result
        except BaseException:
            try:
                await self.close()
            except Exception:  # noqa: BLE001, S110 - upstream `close().catch(() => {})`
                pass
            raise

    def request(
        self, method: str, params: dict[str, Any] | None = None, options: McpRequestOptions | None = None
    ) -> asyncio.Future[Any]:
        return self._requestInternal(method, params, options if options is not None else {}, False)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> asyncio.Future[None]:
        return self._notifyInternal(method, params, False)

    def setRequestHandler(self, method: str, handler: RequestHandler) -> Callable[[], None]:
        self._requestHandlers[method] = handler

        def dispose() -> None:
            if self._requestHandlers.get(method) is handler:
                del self._requestHandlers[method]

        return dispose

    def onNotification(self, method: str, listener: NotificationListener) -> Callable[[], None]:
        listeners = self._notificationListeners.get(method)
        if listeners is None:
            listeners = {}
        self._notificationListeners[method] = listeners
        listeners[listener] = None

        def dispose() -> None:
            listeners.pop(listener, None)
            if len(listeners) == 0:
                self._notificationListeners.pop(method, None)

        return dispose

    def onError(self, listener: ErrorListener) -> Callable[[], None]:
        self._errorListeners[listener] = None
        return lambda: self._errorListeners.pop(listener, None)  # type: ignore[return-value]

    def onClose(self, listener: CloseListener) -> Callable[[], None]:
        """Called once when the connection closes, whether the transport dropped or ``close()`` was called."""
        self._closeListeners[listener] = None
        return lambda: self._closeListeners.pop(listener, None)  # type: ignore[return-value]

    @eager
    async def ping(self, options: McpRequestOptions | None = None) -> None:
        await self.request("ping", None, options)

    @eager
    async def listTools(self, options: McpRequestOptions | None = None) -> list[Tool]:
        return await self._listAll("tools/list", "tools", isTool, options if options is not None else {})  # type: ignore[return-value]

    @eager
    async def listResources(self, options: McpRequestOptions | None = None) -> list[Resource]:
        """Every resource, following ``nextCursor`` through all pages."""
        items = await self._listAll("resources/list", "resources", isResource, options if options is not None else {})
        return [toResource(item) for item in items]

    @eager
    async def listResourcesPage(
        self, cursor: str | None = None, options: McpRequestOptions | None = None
    ) -> ListResourcesResult:
        """One page of resources, starting at ``cursor``."""
        page = await self._listPage(
            "resources/list", "resources", isResource, cursor, options if options is not None else {}
        )
        return {"resources": [toResource(item) for item in page["items"]], **pageCursor(page)}  # type: ignore[typeddict-item]

    @eager
    async def listResourceTemplates(self, options: McpRequestOptions | None = None) -> list[ResourceTemplate]:
        """Every resource template, following ``nextCursor`` through all pages."""
        templates = await self._listAll(
            "resources/templates/list",
            "resourceTemplates",
            isResourceTemplate,
            options if options is not None else {},
        )
        return [toResourceTemplate(item) for item in templates]

    @eager
    async def listResourceTemplatesPage(
        self, cursor: str | None = None, options: McpRequestOptions | None = None
    ) -> ListResourceTemplatesResult:
        """One page of resource templates, starting at ``cursor``."""
        page = await self._listPage(
            "resources/templates/list",
            "resourceTemplates",
            isResourceTemplate,
            cursor,
            options if options is not None else {},
        )
        return {  # type: ignore[typeddict-item]
            "resourceTemplates": [toResourceTemplate(item) for item in page["items"]],
            **pageCursor(page),
        }

    @eager
    async def readResource(self, uri: str, options: McpRequestOptions | None = None) -> ReadResourceResult:
        return validateReadResourceResult(await self.request("resources/read", {"uri": uri}, options))

    async def _listPage(
        self,
        method: str,
        key: str,
        isItem: Callable[[dict[str, Any]], bool],
        cursor: str | None,
        options: McpRequestOptions,
    ) -> _ListPage:
        return validateListPage(
            method,
            key,
            await self.request(method, None if cursor is None else {"cursor": cursor}, options),
            isItem,
        )

    async def _listAll(
        self,
        method: str,
        key: str,
        isItem: Callable[[dict[str, Any]], bool],
        options: McpRequestOptions,
    ) -> list[dict[str, Any]]:
        """Every item of a paginated list method."""
        items: list[dict[str, Any]] = []
        cursors: set[str] = set()
        cursor: str | None = None
        for _pageNumber in range(MAX_LIST_PAGES):
            page = await self._listPage(method, key, isItem, cursor, options)
            items.extend(page["items"])
            if "nextCursor" not in page:
                return items
            if page["nextCursor"] in cursors:
                raise RuntimeError(f"MCP {method} returned duplicate cursor: {page['nextCursor']}")
            cursors.add(page["nextCursor"])
            cursor = page["nextCursor"]
        raise RuntimeError(f"MCP {method} exceeded {MAX_LIST_PAGES} pages")

    @eager
    async def callTool(
        self, name: str, args: dict[str, Any] | None = None, options: McpRequestOptions | None = None
    ) -> CallToolResult:
        return validateCallToolResult(
            await self.request("tools/call", {"name": name, **({} if args is None else {"arguments": args})}, options)
        )

    @eager
    async def close(self) -> None:
        transport = self._transport
        self._transport = None
        self._disposeTransportListeners()
        self._markClosed(McpConnectionClosedError())
        if transport is not None:
            await transport.close()

    @eager
    async def _requestInternal(
        self,
        method: str,
        params: dict[str, Any] | None,
        options: McpRequestOptions,
        allowConnecting: bool,
    ) -> Any:
        transport = self._requireTransport(allowConnecting)
        signal = options.get("signal")
        if isAborted(signal):
            raise McpAbortError()
        id = self._nextRequestId
        self._nextRequestId += 1
        onProgress = options.get("onProgress")
        progressToken = id if onProgress else None
        if progressToken is None:
            requestParams = params
        else:
            meta = params.get("_meta") if params is not None else None
            requestParams = {**(params or {}), "_meta": {**(meta if isObject(meta) else {}), "progressToken": progressToken}}
        message: JsonRpcRequest = {"jsonrpc": "2.0", "id": id, "method": method}
        if requestParams is not None:
            message["params"] = requestParams
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

        def resolve(value: Any) -> None:
            if not future.done():
                future.set_result(value)

        def reject(reason: BaseException) -> None:
            if not future.done():
                future.set_exception(reason)

        timeoutMs = options.get("timeoutMs")
        if timeoutMs is None:
            timeoutMs = self.options.get("requestTimeoutMs")
        if timeoutMs is None:
            timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS

        def abortRequest() -> None:
            reason = abortReason(signal)
            # MISAKA fork: misaka's own signals carry no reason, which upstream would print as the
            # DOMException; "Aborted" is upstream's fallback for an absent one.
            self._cancelPending(
                id, McpAbortError(), method != "initialize", jsString(reason if reason is not None else "Aborted")
            )

        entry = PendingRequest(
            resolve=resolve,
            reject=reject,
            timeoutMs=timeoutMs,
            timer=None,
            signal=signal,
            # The spec forbids cancelling `initialize`.
            onAbort=abortRequest,
            sendController=AbortController(),
            cancellable=method != "initialize",
            onProgress=onProgress,
            progressToken=progressToken,
        )
        self._pending[id] = entry
        if progressToken is not None:
            self._progressRequests[progressToken] = id
        entry.removeAbortListener = addAbortListener(signal, entry.onAbort) if signal is not None else (lambda: None)
        self._armTimeout(id, entry)
        sending = spawn(transport.send(message, {"signal": entry.sendController.signal}), self._tasks)
        sending.add_done_callback(
            lambda task: self._cancelPending(id, task.exception(), False)  # type: ignore[arg-type]
            if not task.cancelled() and task.exception() is not None
            else None
        )
        # MISAKA fork: a cancelled caller cancels the request (a JavaScript promise cannot be cancelled),
        # or its timer and progress mapping outlive it.
        try:
            return await future
        except asyncio.CancelledError:
            self._cancelPending(id, McpAbortError(), entry.cancellable, "Cancelled")
            raise

    @eager
    async def _notifyInternal(self, method: str, params: dict[str, Any] | None, allowConnecting: bool) -> None:
        await self._requireTransport(allowConnecting).send({  # type: ignore[typeddict-item]
            "jsonrpc": "2.0",
            "method": method,
            **({} if params is None else {"params": params}),
        })

    def _requireTransport(self, allowConnecting: bool) -> McpTransport:
        if self._transport is not None and (
            self._state == "connected" or (allowConnecting and self._state == "connecting")
        ):
            return self._transport
        raise McpConnectionClosedError(f"MCP client is {self._state}")

    def _handleMessage(self, message: JsonRpcMessage) -> None:
        if isJsonRpcResponse(message):
            self._handleResponse(message)
            return
        if isJsonRpcRequest(message):
            spawn(self._handleRequest(message), self._tasks)
            return
        if isJsonRpcNotification(message):
            self._handleNotification(message["method"], message.get("params", UNDEFINED))
            return
        self._emitError(McpError(JSON_RPC_ERROR_CODES.invalidRequest, "Received invalid JSON-RPC message"))

    def _handleResponse(self, message: JsonRpcResponse) -> None:
        entry = self._pending.get(message["id"])
        if entry is None:
            self._emitError(RuntimeError(f"Received response for unknown MCP request {jsString(message['id'])}"))
            return
        self._removePending(message["id"], entry)
        if "error" in message:
            error = message["error"]  # type: ignore[typeddict-item]
            entry.reject(McpError(error["code"], error["message"], error.get("data", UNDEFINED)))
        else:
            entry.resolve(message["result"])  # type: ignore[typeddict-item]

    async def _handleRequest(self, message: JsonRpcRequest) -> None:
        transport = self._transport
        if transport is None:
            return
        handler = self._requestHandlers.get(message["method"])
        if handler is None:
            try:
                await transport.send({
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {
                        "code": JSON_RPC_ERROR_CODES.methodNotFound,
                        "message": f"Method not found: {message['method']}",
                    },
                })
            except Exception as error:  # noqa: BLE001 - reported, as upstream's `.catch`
                self._emitError(error)
            return
        controller = AbortController()
        self._incoming[message["id"]] = controller
        try:
            result = await awaitValue(handler(message.get("params", UNDEFINED), {"signal": controller.signal}))
            await transport.send({"jsonrpc": "2.0", "id": message["id"], "result": result if result is not None else {}})
        except Exception as error:  # noqa: BLE001 - answered to the server, as upstream
            if isinstance(error, McpError):
                responseError: dict[str, Any] = {"code": error.code, "message": error.message}
                # `JSON.stringify` leaves out an undefined `data`.
                if error.data is not UNDEFINED:
                    responseError["data"] = error.data
            else:
                responseError = {"code": JSON_RPC_ERROR_CODES.internalError, "message": str(toError(error))}
            try:
                await transport.send({"jsonrpc": "2.0", "id": message["id"], "error": responseError})  # type: ignore[typeddict-item]
            except Exception as sendError:  # noqa: BLE001 - reported, as upstream's `.catch`
                self._emitError(sendError)
        finally:
            self._incoming.pop(message["id"], None)

    def _handleNotification(self, method: str, params: Any) -> None:
        if method == "notifications/progress":
            self._handleProgress(params)
        elif method == "notifications/cancelled":
            self._handleCancelled(params)
        listeners = self._notificationListeners.get(method)
        for listener in liveKeys(listeners) if listeners is not None else ():
            try:
                listener(params)
            except Exception as error:  # noqa: BLE001 - reported, as upstream
                self._emitError(error)

    def _handleProgress(self, params: Any) -> None:
        if not isObject(params) or not isJsonRpcId(params.get("progressToken")) or not isNumber(params.get("progress")):
            return
        requestId = self._progressRequests.get(params["progressToken"])
        entry = None if requestId is None else self._pending.get(requestId)
        if requestId is None or entry is None:
            return
        self._armTimeout(requestId, entry)
        try:
            if entry.onProgress is not None:
                entry.onProgress(params)  # type: ignore[arg-type]
        except Exception as error:  # noqa: BLE001 - reported, as upstream
            self._emitError(error)

    def _handleCancelled(self, params: Any) -> None:
        if isObject(params) and isJsonRpcId(params.get("requestId")):
            controller = self._incoming.get(params["requestId"])
            if controller is not None:
                controller.abort(params.get("reason", UNDEFINED))

    def _armTimeout(self, id: JsonRpcId, entry: PendingRequest) -> None:
        if entry.timer is not None:
            clearTimeout(entry.timer)
        if not isNumber(entry.timeoutMs) or not math.isfinite(entry.timeoutMs) or entry.timeoutMs <= 0:
            return
        entry.timer = setTimeout(
            lambda: self._cancelPending(id, McpTimeoutError(entry.timeoutMs), entry.cancellable, "Request timed out"),
            entry.timeoutMs,
        )

    def _cancelPending(self, id: JsonRpcId, error: BaseException, notifyServer: bool, reason: str | None = None) -> None:
        entry = self._pending.get(id)
        if entry is None:
            return
        self._removePending(id, entry)
        entry.reject(error)
        if notifyServer and self._transport is not None:
            transport = self._transport

            async def sendCancelled() -> None:
                try:
                    await transport.send({
                        "jsonrpc": "2.0",
                        "method": "notifications/cancelled",
                        "params": {"requestId": id, **({"reason": reason} if reason else {})},
                    })
                except Exception as sendError:  # noqa: BLE001 - reported, as upstream's `.catch`
                    self._emitError(sendError)

            spawn(sendCancelled(), self._tasks)

    def _removePending(self, id: JsonRpcId, entry: PendingRequest) -> None:
        self._pending.pop(id, None)
        if entry.timer is not None:
            clearTimeout(entry.timer)
        if entry.progressToken is not None:
            self._progressRequests.pop(entry.progressToken, None)
        entry.removeAbortListener()
        entry.sendController.abort()

    def _rejectPending(self, error: BaseException) -> None:
        for id, entry in list(self._pending.items()):
            self._removePending(id, entry)
            entry.reject(error)

    def _handleTransportClose(self) -> None:
        self._markClosed(McpConnectionClosedError())

    def _markClosed(self, error: Exception) -> None:
        """Idempotent: rejects in-flight requests, aborts server requests we are serving, and flips the state."""
        wasClosed = self._state == "closed"
        self._state = "closed"
        self._rejectPending(error)
        for controller in list(self._incoming.values()):
            controller.abort(error)
        self._incoming.clear()
        if wasClosed:
            return
        for listener in list(self._closeListeners):
            try:
                listener()
            except Exception as listenerError:  # noqa: BLE001 - reported, as upstream
                self._emitError(listenerError)

    def _emitError(self, error: Any) -> None:
        normalized = toError(error)
        for listener in liveKeys(self._errorListeners):
            listener(normalized)

    def _disposeTransportListeners(self) -> None:
        disposers, self._disposers = self._disposers, []
        for dispose in disposers:
            dispose()


__all__ = ["McpClient", "McpClientOptions", "McpRequestOptions"]

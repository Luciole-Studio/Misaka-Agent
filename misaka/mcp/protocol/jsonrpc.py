"""JSON-RPC 2.0 messages and the MCP client's errors.

Translated from pi's ``packages/mcp/src/protocol/jsonrpc.ts``.
"""

from __future__ import annotations

import math
from typing import Any, Literal, NamedTuple, NotRequired, TypedDict, TypeGuard

from misaka.utils.js import UNDEFINED, jsString

type JsonRpcId = str | int | float


class JsonRpcRequest(TypedDict):
    jsonrpc: Literal["2.0"]
    id: JsonRpcId
    method: str
    params: NotRequired[Any]


class JsonRpcNotification(TypedDict):
    jsonrpc: Literal["2.0"]
    method: str
    params: NotRequired[Any]


class JsonRpcErrorObject(TypedDict):
    code: int | float
    message: str
    data: NotRequired[Any]


class JsonRpcSuccessResponse(TypedDict):
    jsonrpc: Literal["2.0"]
    id: JsonRpcId
    result: Any


class JsonRpcErrorResponse(TypedDict):
    jsonrpc: Literal["2.0"]
    id: JsonRpcId
    error: JsonRpcErrorObject


type JsonRpcResponse = JsonRpcSuccessResponse | JsonRpcErrorResponse
type JsonRpcMessage = JsonRpcRequest | JsonRpcNotification | JsonRpcResponse


class _JsonRpcErrorCodes(NamedTuple):
    parseError: int = -32700
    invalidRequest: int = -32600
    methodNotFound: int = -32601
    invalidParams: int = -32602
    internalError: int = -32603


JSON_RPC_ERROR_CODES = _JsonRpcErrorCodes()


class McpError(Exception):
    def __init__(self, code: float, message: str, data: Any = UNDEFINED) -> None:
        super().__init__(message)
        self.message = message
        self.name = "McpError"
        self.code = code
        self.data = data


class McpConnectionClosedError(Exception):
    def __init__(self, message: str = "MCP connection closed") -> None:
        super().__init__(message)
        self.message = message
        self.name = "McpConnectionClosedError"


class McpTimeoutError(Exception):
    def __init__(self, timeoutMs: float) -> None:
        message = f"MCP request timed out after {jsString(timeoutMs)}ms"
        super().__init__(message)
        self.message = message
        self.name = "McpTimeoutError"
        self.timeoutMs = timeoutMs


class McpAbortError(Exception):
    def __init__(self, message: str = "MCP request aborted") -> None:
        super().__init__(message)
        self.message = message
        self.name = "AbortError"


def isObject(value: Any) -> TypeGuard[dict[str, Any]]:
    return isinstance(value, dict)


def toError(value: Any) -> Exception:
    if isinstance(value, Exception):
        return value
    # `new Error(String(value))`, with the `name` and `message` an Error carries.
    error = Exception(jsString(value))
    error.name = "Error"  # type: ignore[attr-defined]
    error.message = jsString(value)  # type: ignore[attr-defined]
    return error


def isJsonRpcId(value: Any) -> TypeGuard[JsonRpcId]:
    # A Python bool is an int; a JavaScript boolean is not a number.
    if isinstance(value, str):
        return True
    if not isinstance(value, int | float) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        # An integer past the double range, which `JSON.parse` reads as Infinity.
        return False


def isJsonRpcRequest(message: Any) -> TypeGuard[JsonRpcRequest]:
    return (
        isObject(message)
        and message.get("jsonrpc") == "2.0"
        and isJsonRpcId(message.get("id"))
        and isinstance(message.get("method"), str)
    )


def isJsonRpcNotification(message: Any) -> TypeGuard[JsonRpcNotification]:
    return (
        isObject(message)
        and message.get("jsonrpc") == "2.0"
        and "id" not in message
        and isinstance(message.get("method"), str)
    )


def isJsonRpcResponse(message: Any) -> TypeGuard[JsonRpcResponse]:
    if not isObject(message) or message.get("jsonrpc") != "2.0" or not isJsonRpcId(message.get("id")):
        return False
    if "result" in message:
        return "error" not in message
    if "error" not in message or not isObject(message["error"]):
        return False
    code = message["error"].get("code")
    return isinstance(code, int | float) and not isinstance(code, bool) and isinstance(message["error"].get("message"), str)


def parseJsonRpcMessage(value: Any) -> JsonRpcMessage:
    if isJsonRpcRequest(value) or isJsonRpcNotification(value) or isJsonRpcResponse(value):
        return value
    raise McpError(JSON_RPC_ERROR_CODES.invalidRequest, "Invalid JSON-RPC message")


__all__ = [
    "JSON_RPC_ERROR_CODES",
    "JsonRpcErrorObject",
    "JsonRpcErrorResponse",
    "JsonRpcId",
    "JsonRpcMessage",
    "JsonRpcNotification",
    "JsonRpcRequest",
    "JsonRpcResponse",
    "JsonRpcSuccessResponse",
    "McpAbortError",
    "McpConnectionClosedError",
    "McpError",
    "McpTimeoutError",
    "isJsonRpcId",
    "isJsonRpcNotification",
    "isJsonRpcRequest",
    "isJsonRpcResponse",
    "isObject",
    "parseJsonRpcMessage",
    "toError",
]

"""MCP protocol types.

Translated from pi's ``packages/mcp/src/protocol/types.ts``.
"""

from __future__ import annotations

from typing import Any, Final, Literal, NotRequired, TypedDict

from misaka.mcp.protocol.content import (
    BlobResourceContents,
    ContentAnnotations,
    TextResourceContents,
)
from misaka.mcp.protocol.jsonrpc import JsonRpcId

LATEST_PROTOCOL_VERSION: Final = "2025-11-25"
# Versions the client accepts from a server. Servers that do not support the requested version answer
# with their own latest one, so older versions stay accepted for servers built on older SDKs.
SUPPORTED_PROTOCOL_VERSIONS: Final = (LATEST_PROTOCOL_VERSION, "2025-06-18", "2025-03-26", "2024-11-05")
type SupportedProtocolVersion = Literal["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]


class Implementation(TypedDict):
    name: str
    version: str
    title: NotRequired[str]


class Root(TypedDict):
    uri: str
    name: NotRequired[str]


class _RootsCapability(TypedDict, total=False):
    listChanged: bool


class ClientCapabilities(TypedDict, total=False):
    experimental: dict[str, Any]
    roots: _RootsCapability
    sampling: dict[str, Any]
    elicitation: dict[str, Any]


class _ListChanged(TypedDict, total=False):
    listChanged: bool


class _ResourcesCapability(TypedDict, total=False):
    subscribe: bool
    listChanged: bool


class ServerCapabilities(TypedDict, total=False):
    experimental: dict[str, Any]
    logging: dict[str, Any]
    prompts: _ListChanged
    resources: _ResourcesCapability
    tools: _ListChanged
    completions: dict[str, Any]


class InitializeParams(TypedDict):
    protocolVersion: str
    capabilities: ClientCapabilities
    clientInfo: Implementation


class InitializeResult(TypedDict):
    protocolVersion: str
    capabilities: ServerCapabilities
    serverInfo: Implementation
    instructions: NotRequired[str]


class ProgressNotification(TypedDict):
    progressToken: str | int | float
    progress: float
    total: NotRequired[float]
    message: NotRequired[str]


class CancelledNotification(TypedDict):
    requestId: JsonRpcId
    reason: NotRequired[str]


class ToolAnnotations(TypedDict, total=False):
    title: str
    readOnlyHint: bool
    destructiveHint: bool
    idempotentHint: bool
    openWorldHint: bool


class ToolExecution(TypedDict, total=False):
    taskSupport: Literal["forbidden", "optional", "required"]


Tool = TypedDict(
    "Tool",
    {
        "name": str,
        "title": NotRequired[str],
        "description": NotRequired[str],
        "inputSchema": dict[str, Any],
        "outputSchema": NotRequired[dict[str, Any]],
        "annotations": NotRequired[ToolAnnotations],
        "execution": NotRequired[ToolExecution],
        "_meta": NotRequired[dict[str, Any]],
    },
)

ListToolsResult = TypedDict(
    "ListToolsResult",
    {"tools": list[Tool], "nextCursor": NotRequired[str], "_meta": NotRequired[dict[str, Any]]},
)

# A resource a server lists in `resources/list`.
Resource = TypedDict(
    "Resource",
    {
        "uri": str,
        "name": str,
        "title": NotRequired[str],
        "description": NotRequired[str],
        "mimeType": NotRequired[str],
        "size": NotRequired[float],
        "annotations": NotRequired[ContentAnnotations],
        "_meta": NotRequired[dict[str, Any]],
    },
)

# A family of resources, addressed by an RFC 6570 URI template, from `resources/templates/list`.
ResourceTemplate = TypedDict(
    "ResourceTemplate",
    {
        "uriTemplate": str,
        "name": str,
        "title": NotRequired[str],
        "description": NotRequired[str],
        "mimeType": NotRequired[str],
        "annotations": NotRequired[ContentAnnotations],
        "_meta": NotRequired[dict[str, Any]],
    },
)

ListResourcesResult = TypedDict(
    "ListResourcesResult",
    {"resources": list[Resource], "nextCursor": NotRequired[str], "_meta": NotRequired[dict[str, Any]]},
)

ListResourceTemplatesResult = TypedDict(
    "ListResourceTemplatesResult",
    {
        "resourceTemplates": list[ResourceTemplate],
        "nextCursor": NotRequired[str],
        "_meta": NotRequired[dict[str, Any]],
    },
)

ReadResourceResult = TypedDict(
    "ReadResourceResult",
    {"contents": list[TextResourceContents | BlobResourceContents], "_meta": NotRequired[dict[str, Any]]},
)


__all__ = [
    "LATEST_PROTOCOL_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "CancelledNotification",
    "ClientCapabilities",
    "Implementation",
    "InitializeParams",
    "InitializeResult",
    "ListResourceTemplatesResult",
    "ListResourcesResult",
    "ListToolsResult",
    "ProgressNotification",
    "ReadResourceResult",
    "Resource",
    "ResourceTemplate",
    "Root",
    "ServerCapabilities",
    "SupportedProtocolVersion",
    "Tool",
    "ToolAnnotations",
    "ToolExecution",
]

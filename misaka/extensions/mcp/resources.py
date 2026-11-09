"""MCP resources, through the tools Codex and opencode use: ``list_mcp_resources``,
``list_mcp_resource_templates``, and ``read_mcp_resource``. They take a ``server`` argument and cover
every connected server with resources, so models trained on those tools use them unchanged.

Listings are JSON, as in Codex: ``{ server?, resources: [{ server, ...resource }], nextCursor? }``.
With a ``server``, one page is listed and ``cursor`` continues it; without, every page of every server.
MCP App resources (``ui://`` URIs and ``profile=mcp-app`` HTML) are left out, since they are user
interfaces for hosts that render them, and so are icons. Read resources become text and images for
the model; binary resources are saved to temp files. Scripts get the JSON payloads.

Translated from pi's ``coding-agent/src/extensions/mcp/resources.ts``.
"""

from __future__ import annotations

import builtins
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Protocol, TypedDict

from misaka.agent.types import AgentToolResult
from misaka.ai.types import TextContent
from misaka.core.extensions.types import ToolAnnotations, ToolDefinition
from misaka.core.mcp_servers import (
    LIST_MCP_RESOURCE_TEMPLATES_TOOL,
    LIST_MCP_RESOURCES_TOOL,
    READ_MCP_RESOURCE_TOOL,
    McpExposure,
)
from misaka.mcp import (
    ContentBlock,
    ListResourcesResult,
    ListResourceTemplatesResult,
    McpRequestOptions,
    ReadResourceResult,
    Resource,
    ResourceTemplate,
)
from misaka.utils.collation import localeSortKey
from misaka.utils.js import (
    JS_WHITESPACE_CLASS,
    allSettled,
    errorMessage,
    jsonStringify,
    jsString,
    jsTrim,
)

from .tools import (
    McpToolDetails,
    limitMcpContent,
    toModelContent,
    toToolExposure,
)


class McpResourceServer(Protocol):
    """A connected server that offers resources."""

    @property
    def name(self) -> str: ...

    @property
    def timeoutMs(self) -> float: ...

    async def resourcesPage(self, cursor: str | None, options: McpRequestOptions) -> ListResourcesResult: ...

    async def resourceTemplatesPage(
        self, cursor: str | None, options: McpRequestOptions
    ) -> ListResourceTemplatesResult: ...

    async def allResources(self, options: McpRequestOptions) -> list[Resource]: ...

    async def allResourceTemplates(self, options: McpRequestOptions) -> list[ResourceTemplate]: ...

    async def readResource(self, uri: str, options: McpRequestOptions) -> ReadResourceResult: ...


def isMcpAppResource(item: Mapping[str, Any]) -> bool:
    """MCP App user interfaces, which only hosts that render them can use."""
    uri = item.get("uri")
    if uri is None:
        uri = item.get("uriTemplate")
    if uri is None:
        uri = ""
    mimeType = item.get("mimeType")
    # `test()` reads its argument as a string; JavaScript's `\s`, and case-insensitive within ASCII.
    return uri.startswith("ui://") or (
        re.search(
            rf'(?i:;[{JS_WHITESPACE_CLASS}]*profile[{JS_WHITESPACE_CLASS}]*=[{JS_WHITESPACE_CLASS}]*"?mcp-app"?)',
            jsString(mimeType if mimeType is not None else ""),
            re.ASCII,
        )
        is not None
    )


def listed(server: str, item: Mapping[str, Any]) -> dict[str, Any]:
    """A listed resource or template without ``_meta`` and icons, tagged with its server."""
    return {"server": server, **{key: value for key, value in item.items() if key not in ("_meta", "icons")}}


def stringProperty(description: str) -> dict[str, str]:
    return {"type": "string", "description": description}


SERVER_FILTER = stringProperty("MCP server name. Omit to list every server with resources.")
CURSOR = stringProperty("Opaque cursor from a previous call with the same server; omit for the first page.")
LIST_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {"server": SERVER_FILTER, "cursor": CURSOR},
    "additionalProperties": False,
}
READ_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "server": stringProperty(
            "MCP server name exactly as configured. Must match the 'server' field returned by list_mcp_resources.",
        ),
        "uri": stringProperty("Resource URI to read. Must be one of the URIs returned by list_mcp_resources."),
    },
    "required": ["server", "uri"],
    "additionalProperties": False,
}

optionalString = {"type": "string"}
LISTING_ERRORS: dict[str, Any] = {
    "type": "array",
    "description": "Servers that could not be listed",
    "items": {
        "type": "object",
        "properties": {"server": {"type": "string"}, "error": {"type": "string"}},
        "required": ["server", "error"],
    },
}
LIST_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "server": optionalString,
        "resources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "server": {"type": "string"},
                    "uri": {"type": "string"},
                    "name": {"type": "string"},
                    "title": optionalString,
                    "description": optionalString,
                    "mimeType": optionalString,
                    "size": {"type": "number"},
                },
                "required": ["server", "uri", "name"],
            },
        },
        "nextCursor": optionalString,
        "errors": LISTING_ERRORS,
    },
    "required": ["resources"],
}
LIST_TEMPLATES_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "server": optionalString,
        "resourceTemplates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "server": {"type": "string"},
                    "uriTemplate": {"type": "string", "description": "RFC 6570 URI template"},
                    "name": {"type": "string"},
                    "title": optionalString,
                    "description": optionalString,
                    "mimeType": optionalString,
                },
                "required": ["server", "uriTemplate", "name"],
            },
        },
        "nextCursor": optionalString,
        "errors": LISTING_ERRORS,
    },
    "required": ["resourceTemplates"],
}
READ_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "server": {"type": "string"},
        "uri": {"type": "string"},
        "contents": {
            "type": "array",
            "items": {
                "anyOf": [
                    {
                        "type": "object",
                        "properties": {"uri": {"type": "string"}, "mimeType": optionalString, "text": {"type": "string"}},
                        "required": ["uri", "text"],
                    },
                    {
                        "type": "object",
                        "properties": {
                            "uri": {"type": "string"},
                            "mimeType": optionalString,
                            "blob": {"type": "string", "description": "base64"},
                        },
                        "required": ["uri", "blob"],
                    },
                ],
            },
        },
    },
    "required": ["server", "uri", "contents"],
}


def stringArgument(params: Any, key: str) -> str | None:
    value = params.get(key) if isinstance(params, Mapping) else None
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeError(f"{key} must be a string")  # noqa: TRY004 - upstream raises a plain Error
    return jsTrim(value) or None


async def jsonResult(tool: str, server: str | None, payload: dict[str, Any]) -> AgentToolResult:
    limited = await limitMcpContent([TextContent(text=jsonStringify(payload))])
    details: McpToolDetails = {"server": server if server is not None else "", "tool": tool}
    if "fullOutputPath" in limited:
        details["fullOutputPath"] = limited["fullOutputPath"]
    return AgentToolResult(content=limited["content"], details=details, structuredContent=payload)


class CreateMcpResourceToolDefinitionsOptions(TypedDict):
    exposure: McpExposure
    servers: Callable[[], Sequence[McpResourceServer]]


class _Page(TypedDict, total=False):
    items: list[Any]
    nextCursor: str


def createMcpResourceToolDefinitions(
    options: CreateMcpResourceToolDefinitionsOptions,
) -> list[ToolDefinition[Any, McpToolDetails]]:
    """The three resource tools. ``servers`` returns the servers whose resources they reach, at call time."""
    readOnly: ToolAnnotations = {"readOnlyHint": True}

    def findServer(name: str) -> McpResourceServer:
        servers = options["servers"]()
        server = next((candidate for candidate in servers if candidate.name == name), None)
        if server is not None:
            return server
        available = ", ".join(candidate.name for candidate in servers)
        raise RuntimeError(
            f'MCP server "{name}" has no resources{f". Servers with resources: {available}" if available else ""}'
        )

    async def list(
        params: Any,
        signal: Any,
        key: str,
        page: Callable[[McpResourceServer, str | None, McpRequestOptions], Awaitable[_Page]],
        all: Callable[[McpResourceServer, McpRequestOptions], Awaitable[builtins.list[Any]]],
    ) -> dict[str, Any]:
        """One page of one server, or every page of every server."""
        serverName = stringArgument(params, "server")
        cursor = stringArgument(params, "cursor")

        def visible(item: Mapping[str, Any]) -> bool:
            return not isMcpAppResource(item)

        if serverName:
            server = findServer(serverName)
            result = await page(server, cursor, {"signal": signal, "timeoutMs": server.timeoutMs})
            payload: dict[str, Any] = {
                "server": server.name,
                key: [listed(server.name, item) for item in result["items"] if visible(item)],
            }
            if "nextCursor" in result:
                payload["nextCursor"] = result["nextCursor"]
            return payload
        if cursor:
            raise RuntimeError("cursor can only be used when a server is specified")
        servers = sorted(options["servers"](), key=lambda candidate: localeSortKey(candidate.name))
        results = await allSettled(all(server, {"signal": signal, "timeoutMs": server.timeoutMs}) for server in servers)
        items: builtins.list[dict[str, Any]] = []
        errors: builtins.list[dict[str, str]] = []
        for index, result in enumerate(results):
            serverName = servers[index].name
            if result.status == "fulfilled":
                items.extend(listed(serverName, item) for item in result.value if visible(item))
            else:
                errors.append({"server": serverName, "error": errorMessage(result.reason)})
        payload = {key: items}
        if len(errors) > 0:
            payload["errors"] = errors
        return payload

    async def resourcesPage(server: McpResourceServer, cursor: str | None, requestOptions: McpRequestOptions) -> _Page:
        result = await server.resourcesPage(cursor, requestOptions)
        page: _Page = {"items": result["resources"]}
        if "nextCursor" in result:
            page["nextCursor"] = result["nextCursor"]
        return page

    async def executeListResources(
        _toolCallId: str, params: Any, signal: Any = None, _onUpdate: Any = None, _ctx: Any = None
    ) -> AgentToolResult:
        payload = await list(
            params,
            signal,
            "resources",
            resourcesPage,
            lambda server, requestOptions: server.allResources(requestOptions),
        )
        return await jsonResult(LIST_MCP_RESOURCES_TOOL, stringArgument(params, "server"), payload)

    listResources: ToolDefinition[Any, McpToolDetails] = ToolDefinition(
        name=LIST_MCP_RESOURCES_TOOL,
        label=LIST_MCP_RESOURCES_TOOL,
        description=(
            "Lists resources provided by MCP servers. Resources allow servers to share data that provides "
            "context to language models, such as files, database schemas, or application-specific "
            "information. Prefer resources over web search when possible."
        ),
        parameters=LIST_PARAMETERS,
        outputSchema=LIST_OUTPUT_SCHEMA,
        exposure=toToolExposure(options["exposure"]),
        annotations=readOnly,
        execute=executeListResources,
    )

    async def templatesPage(
        server: McpResourceServer, cursor: str | None, requestOptions: McpRequestOptions
    ) -> _Page:
        result = await server.resourceTemplatesPage(cursor, requestOptions)
        page: _Page = {"items": result["resourceTemplates"]}
        if "nextCursor" in result:
            page["nextCursor"] = result["nextCursor"]
        return page

    async def executeListTemplates(
        _toolCallId: str, params: Any, signal: Any = None, _onUpdate: Any = None, _ctx: Any = None
    ) -> AgentToolResult:
        payload = await list(
            params,
            signal,
            "resourceTemplates",
            templatesPage,
            lambda server, requestOptions: server.allResourceTemplates(requestOptions),
        )
        return await jsonResult(LIST_MCP_RESOURCE_TEMPLATES_TOOL, stringArgument(params, "server"), payload)

    listTemplates: ToolDefinition[Any, McpToolDetails] = ToolDefinition(
        name=LIST_MCP_RESOURCE_TEMPLATES_TOOL,
        label=LIST_MCP_RESOURCE_TEMPLATES_TOOL,
        description=(
            "Lists resource templates provided by MCP servers. Parameterized resource templates allow servers "
            "to share data that takes parameters and provides context to language models, such as files, "
            "database schemas, or application-specific information. Prefer resource templates over web search "
            "when possible."
        ),
        parameters=LIST_PARAMETERS,
        outputSchema=LIST_TEMPLATES_OUTPUT_SCHEMA,
        exposure=toToolExposure(options["exposure"]),
        annotations=readOnly,
        execute=executeListTemplates,
    )

    async def executeReadResource(
        _toolCallId: str, params: Any, signal: Any = None, _onUpdate: Any = None, _ctx: Any = None
    ) -> AgentToolResult:
        serverName = stringArgument(params, "server")
        uri = stringArgument(params, "uri")
        if not serverName:
            raise RuntimeError("server must be provided")
        if not uri:
            raise RuntimeError("uri must be provided")
        server = findServer(serverName)
        result = await server.readResource(uri, {"signal": signal, "timeoutMs": server.timeoutMs})
        # Several contents (for example a directory) are labeled with their URIs.
        blocks: builtins.list[ContentBlock] = []
        for contents in result["contents"]:
            if len(result["contents"]) > 1:
                blocks.append({"type": "text", "text": f"{contents['uri']}:"})
            blocks.append({"type": "resource", "resource": contents})
        converted = await toModelContent(server.name, blocks)
        limited = await limitMcpContent(
            converted if len(converted) > 0 else [TextContent(text=f"Resource {uri} is empty.")],
        )
        contents = [{key: value for key, value in entry.items() if key != "_meta"} for entry in result["contents"]]
        details: McpToolDetails = {"server": server.name, "tool": READ_MCP_RESOURCE_TOOL}
        if "fullOutputPath" in limited:
            details["fullOutputPath"] = limited["fullOutputPath"]
        return AgentToolResult(
            content=limited["content"],
            details=details,
            structuredContent={"server": server.name, "uri": uri, "contents": contents},
        )

    readResource: ToolDefinition[Any, McpToolDetails] = ToolDefinition(
        name=READ_MCP_RESOURCE_TOOL,
        label=READ_MCP_RESOURCE_TOOL,
        description="Read a specific resource from an MCP server given the server name and resource URI.",
        parameters=READ_PARAMETERS,
        outputSchema=READ_OUTPUT_SCHEMA,
        exposure=toToolExposure(options["exposure"]),
        annotations=readOnly,
        execute=executeReadResource,
    )

    return [listResources, listTemplates, readResource]


__all__ = [
    "LIST_MCP_RESOURCES_TOOL",
    "LIST_MCP_RESOURCE_TEMPLATES_TOOL",
    "READ_MCP_RESOURCE_TOOL",
    "CreateMcpResourceToolDefinitionsOptions",
    "McpResourceServer",
    "createMcpResourceToolDefinitions",
]

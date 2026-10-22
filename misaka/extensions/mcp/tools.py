"""Adapts MCP tools to pi tool definitions.

Results map onto pi's model-facing content (text and images). Text over 20KB keeps its start and
end with the middle cut out, like Codex does, and the full text is saved to a temp file the model
can read. Binary resources other than images are saved to temp files too, and resource links name
the ``read_mcp_resource`` tool. Codemode scripts receive the whole ``CallToolResult`` without ``_meta``
(``content`` blocks as sent by the server, ``structuredContent``, ``isError``), never truncated: it is
the tool's ``structuredContent``, and every MCP tool declares a ``CallToolResult`` output schema. MCP
errors (``isError``) are error results for the model, but scripts still resolve to the result.

Translated from pi's ``coding-agent/src/extensions/mcp/tools.ts``.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, NotRequired, Protocol, TypedDict

from misaka.agent.types import AgentToolResult
from misaka.ai.types import ImageContent, TextContent
from misaka.core.extensions.types import (
    ToolAnnotations,
    ToolDefinition,
    ToolExposure,
    ToolNamespace,
    ToolRenderers,
)

# Tool that reads the resources named by resource links.
from misaka.core.mcp_servers import READ_MCP_RESOURCE_TOOL
from misaka.core.tools.render_utils import (
    formatToolCallWithArgs,
    getTextOutput,
    replaceTabs,
)
from misaka.core.tools.truncate import formatSize, truncateMiddle
from misaka.mcp import (
    CallToolResult,
    ContentBlock,
    LlmContent,
    McpRequestOptions,
    Tool,
    toLlmContent,
)
from misaka.modes.interactive.components.keybinding_hints import keyHint
from misaka.modes.interactive.components.visual_truncate import (
    VisualLinePreview,
    VisualLinePreviewOptions,
)
from misaka.ui.tui import Container, Spacer, Text
from misaka.utils.js import (
    UNDEFINED,
    bufferFromBase64,
    errorMessage,
    isNumber,
    jsNumber,
    jsString,
    jsTrim,
    jsTruthy,
    toUtf16Units,
    toWellFormed,
)
from misaka.utils.output_files import writeOutputFile
from misaka.utils.values import read_field
from misaka.utils.whatwg_url import URL

from .config import McpExposure


def toToolExposure(exposure: McpExposure) -> ToolExposure:
    """Tool exposure of an MCP exposure. ``codemode`` and ``deferred`` both leave tools out of the codemode
    description; they differ only in which tool the MCP extension activates to reach them."""
    return "deferred" if exposure == "codemode" else exposure


# Provider tool names are limited to 64 characters of `[A-Za-z0-9_-]`.
MAX_TOOL_NAME_LENGTH = 64
# Model-facing text of an MCP result beyond this is cut in the middle.
MCP_OUTPUT_MAX_BYTES = 20 * 1024
# Visual (wrapped) result lines shown before the output is expanded.
OUTPUT_PREVIEW_LINES = 5


class McpToolDetails(TypedDict):
    server: str
    tool: str
    # Temp file with the full text output, when the model-facing text was truncated.
    fullOutputPath: NotRequired[str]


# Saves the full text of a truncated result, or a binary resource, and returns the file path.
# `extension` includes the dot, for example `.txt`.
type McpOutputSaver = Callable[[str | bytes, str], Awaitable[str]]


async def saveToTempFile(data: str | bytes, extension: str) -> str:
    # MISAKA fork: `misaka-mcp` rather than `pi-mcp`, as misaka names its files.
    return await writeOutputFile("misaka-mcp", extension, data)


class McpToolCaller(Protocol):
    async def callTool(self, name: str, args: dict[str, Any], options: McpRequestOptions) -> CallToolResult: ...


def createMcpToolName(server: str, tool: str, isTaken: Callable[[str], bool] = lambda _name: False) -> str:
    """``mcp__<server>__<tool>``, sanitized and shortened with a hash suffix when too long. Like Codex,
    everything but ``[A-Za-z0-9_]`` becomes ``_``, so the name is also the identifier codemode scripts
    call it by. ``isTaken`` reports names used by a different MCP tool: sanitizing can map two tools to
    one name (``a-b`` and ``a_b``), which then get the hash suffix."""
    # Without the `u` flag the pattern replaces UTF-16 code units: a character outside the BMP becomes two `_`.
    name = re.sub(r"[^A-Za-z0-9_]", "_", toUtf16Units(f"mcp__{server}__{tool}"))
    if len(name) <= MAX_TOOL_NAME_LENGTH and not isTaken(name):
        return name
    hash = hashlib.sha256(toWellFormed(f"{server}\0{tool}").encode("utf-8")).hexdigest()[:8]
    return f"{name[: MAX_TOOL_NAME_LENGTH - len(hash) - 1]}_{hash}"


def textOf(content: Sequence[TextContent | ImageContent]) -> str:
    return "\n".join(block.text for block in content if isinstance(block, TextContent))


def _contentText(value: Any) -> str:
    # MISAKA fork: a content model's field cannot be `undefined`; a missing one is empty, as `join` prints it.
    if value is None or value is UNDEFINED:
        return ""
    return value if isinstance(value, str) else jsString(value)


def _toContent(blocks: Sequence[LlmContent]) -> list[TextContent | ImageContent]:
    """pi-mcp's plain content dicts as misaka's content models."""
    return [
        TextContent(text=_contentText(block["text"]))  # type: ignore[typeddict-item]
        if block["type"] == "text"
        else ImageContent(data=_contentText(block["data"]), mimeType=_contentText(block["mimeType"]))  # type: ignore[typeddict-item]
        for block in blocks
    ]


# MCP content blocks as one compact type with the fields of text and image blocks, which scripts read
# most (`image(block)` takes an image block as is). Rendered into every MCP tool's declaration.
CONTENT_BLOCK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {"enum": ["text", "image", "audio", "resource_link", "resource"]},
        "text": {"type": "string"},
        "data": {"type": "string"},
        "mimeType": {"type": "string"},
    },
    "required": ["type"],
    "additionalProperties": True,
}

# Keywords whose values are data, so a `$ref` key in them is not a reference.
SCHEMA_DATA_KEYWORDS: frozenset[str] = frozenset({"const", "enum", "default", "examples"})
# Keywords whose values map names to schemas, so their keys are names, not keywords.
SCHEMA_MAP_KEYWORDS: frozenset[str] = frozenset({
    "properties",
    "patternProperties",
    "dependentSchemas",
    "dependencies",
    "$defs",
    "definitions",
})


def relocateSchema(schema: Any, pointer: str) -> Any:
    """``schema`` as a subschema at the JSON pointer ``pointer`` (``#/properties/structuredContent``) of
    another schema: its local references (``#``, ``#/$defs/Item``) are rewritten to point below
    ``pointer``, so they resolve to the same subschemas as before."""
    if isinstance(schema, list):
        return [relocateSchema(item, pointer) for item in schema]
    if not isinstance(schema, dict):
        return schema
    relocated: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "$ref" and isinstance(value, str) and (value == "#" or value.startswith("#/")):
            relocated[key] = f"{pointer}{value[1:]}"
        elif key in SCHEMA_DATA_KEYWORDS:
            relocated[key] = value
        elif key in SCHEMA_MAP_KEYWORDS and isinstance(value, dict):
            relocated[key] = {name: relocateSchema(subschema, pointer) for name, subschema in value.items()}
        else:
            relocated[key] = relocateSchema(value, pointer)
    return relocated


def createMcpResultSchema(structuredContentSchema: dict[str, Any] | None) -> dict[str, Any]:
    """Output schema of every MCP tool: the ``CallToolResult`` scripts receive (without ``_meta``, which
    ``convertMcpResult`` drops), with the tool's own output schema as ``structuredContent``."""
    return {
        "type": "object",
        "properties": {
            "content": {"type": "array", "items": CONTENT_BLOCK_SCHEMA},
            # Servers may send structured content without declaring a schema for it, and it may be any JSON value.
            "structuredContent": (
                relocateSchema(structuredContentSchema, "#/properties/structuredContent")
                if jsTruthy(structuredContentSchema)
                else {}
            ),
            "isError": {"type": "boolean"},
        },
        "required": ["content"],
        # Fields newer protocol versions add.
        "additionalProperties": True,
    }


class LimitedMcpContent(TypedDict):
    content: list[TextContent | ImageContent]
    fullOutputPath: NotRequired[str]


async def limitMcpContent(
    content: list[TextContent | ImageContent], saveOutput: McpOutputSaver | None = None
) -> LimitedMcpContent:
    """Keep model-facing text within ``MCP_OUTPUT_MAX_BYTES``. Longer text becomes one text block in
    Codex's truncation format, followed by the path of the file with the full text; images follow it."""
    if saveOutput is None:
        saveOutput = saveToTempFile
    combined = textOf(content)
    truncation = truncateMiddle(combined, MCP_OUTPUT_MAX_BYTES)
    if not truncation.truncated:
        return {"content": content}
    fullOutputPath: str | None = None
    try:
        fullOutputPath = await saveOutput(combined, ".txt")
        where = f"[Full output: {fullOutputPath} (read it with offset/limit)]"
    except Exception as error:  # noqa: BLE001 - upstream `catch (error)`: reported in the text
        where = f"[Could not save the full output: {errorMessage(error)}]"
    tokens = math.ceil(truncation.totalBytes / 4)
    text = (
        f"Warning: truncated output (original token count: {tokens})\n"
        f"Total output lines: {truncation.totalLines}\n\n{truncation.content}\n\n{where}"
    )
    limited: LimitedMcpContent = {
        "content": [TextContent(text=text), *[block for block in content if isinstance(block, ImageContent)]],
    }
    if fullOutputPath:
        limited["fullOutputPath"] = fullOutputPath
    return limited


class ConvertMcpResultOptions(TypedDict, total=False):
    # Saves truncated text and binary resources. Default: a temp file.
    saveOutput: McpOutputSaver
    # Whether the server's resources can be read with `read_mcp_resource`, which resource links then name.
    readableResources: bool


def extensionOf(uri: str) -> str:
    """File extension for a saved binary resource: the one its URI ends in, else ``.bin``."""
    path = URL(uri).pathname if URL.canParse(uri) else uri
    match = re.search(r"\.[A-Za-z0-9]{1,8}\Z", path)
    return match.group(0) if match is not None else ".bin"


def isTextMimeType(mimeType: str | None) -> bool:
    """Blobs of these types are shown as text."""
    if not mimeType:
        return False
    type = jsTrim(mimeType.split(";", 1)[0]).lower()
    return type.startswith("text/") or type == "application/json" or type.endswith(("+json", "+xml"))


async def blockToContent(
    server: str, block: ContentBlock, options: ConvertMcpResultOptions
) -> list[TextContent | ImageContent]:
    """Model-facing content of one block of ``server``'s result."""
    # A block that is not an object, or lacks a field, goes on as JavaScript reads it: `toLlmContent` decides.
    fields: Any = block if isinstance(block, dict) else {}
    resource = fields.get("resource")
    if fields.get("type") == "resource_link":
        size = fields.get("size", UNDEFINED)
        details = [jsString(item) for item in (fields.get("mimeType"), UNDEFINED if size is UNDEFINED else _formatSize(size)) if jsTruthy(item)]
        read = f'. Read it with {READ_MCP_RESOURCE_TOOL} (server "{server}")' if options.get("readableResources") else ""
        description = f": {jsString(fields['description'])}" if jsTruthy(fields.get("description")) else ""
        title = fields["title"] if fields.get("title") is not None else fields.get("name", UNDEFINED)
        joined = f" ({', '.join(details)})" if len(details) > 0 else ""
        uri = jsString(fields.get("uri", UNDEFINED))
        return [TextContent(text=f'[Resource {uri} "{jsString(title)}"{joined}{description}{read}]')]
    if (
        fields.get("type") == "resource"
        and isinstance(resource, dict)
        and "blob" in resource
        # A mimeType that is not a string has no `startsWith`: `toLlmContent` throws for it, as upstream does here.
        and (resource.get("mimeType") is None or isinstance(resource.get("mimeType"), str))
        and not (resource.get("mimeType") or "").startswith("image/")
    ):
        uri, mimeType, blob = jsString(resource.get("uri", UNDEFINED)), resource.get("mimeType"), resource["blob"]
        data = bufferFromBase64(blob)
        if isTextMimeType(mimeType):
            return [TextContent(text=data.decode("utf-8", "replace"))]
        kind = f"{mimeType if mimeType is not None else 'unknown type'}, {formatSize(len(data))}"
        try:
            saveOutput = options.get("saveOutput")
            path = await (saveOutput if saveOutput is not None else saveToTempFile)(data, extensionOf(uri))
            return [TextContent(text=f"[Binary resource {uri} ({kind}) saved to {path}]")]
        except Exception as error:  # noqa: BLE001 - upstream `catch (error)`: reported in the text
            reason = errorMessage(error)
            return [TextContent(text=f"[Binary resource {uri} ({kind}) could not be saved: {reason}]")]
    return _toContent(toLlmContent({"content": [block]}))


def _formatSize(size: Any) -> str:
    # `formatSize(block.size)` compares and divides whatever the server sent, as JavaScript coerces it.
    if isNumber(size):
        return formatSize(size)
    return f"{jsString(size)}B" if jsNumber(size) < 1024 else formatSize(jsNumber(size))


async def toModelContent(
    server: str, blocks: Sequence[ContentBlock], options: ConvertMcpResultOptions | None = None
) -> list[TextContent | ImageContent]:
    """Model-facing content of ``server``'s content blocks, before the output limit."""
    options = options if options is not None else {}
    converted = await asyncio.gather(*(blockToContent(server, block, options) for block in blocks))
    return [item for items in converted for item in items]


async def convertMcpResult(
    server: str, tool: str, result: CallToolResult, options: ConvertMcpResultOptions | None = None
) -> AgentToolResult:
    """Convert an MCP result. ``isError`` results become error results that keep the structured result."""
    options = options if options is not None else {}
    # Without content blocks, toLlmContent falls back to the structured content as JSON.
    converted = (
        await toModelContent(server, result["content"], options)
        if len(result["content"]) > 0
        else _toContent(toLlmContent(result))
    )
    if jsTruthy(result.get("isError")) and textOf(converted) == "":
        converted.append(TextContent(text=f"MCP tool {server}/{tool} returned an error"))
    limited = await limitMcpContent(converted, options.get("saveOutput"))
    scriptResult = {key: value for key, value in result.items() if key != "_meta"}
    details: McpToolDetails = {"server": server, "tool": tool}
    if "fullOutputPath" in limited:
        details["fullOutputPath"] = limited["fullOutputPath"]
    return AgentToolResult(
        content=limited["content"],
        details=details,
        structuredContent=scriptResult,
        isError=True if jsTruthy(result.get("isError")) else None,
    )


def toParameters(schema: dict[str, Any]) -> dict[str, Any]:
    """Tool input schemas must be objects. MCP servers may omit ``type``, and some providers reject object
    schemas without ``properties``."""
    return {
        **schema,
        "type": schema["type"] if schema.get("type") is not None else "object",
        **({"properties": {}} if "properties" not in schema else {}),
    }


ANNOTATION_HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


def toToolAnnotations(tool: Tool) -> ToolAnnotations | None:
    """The boolean hints of an MCP tool's annotations, or ``None`` when it has none."""
    annotations: ToolAnnotations = {}
    hints = tool.get("annotations")
    for hint in ANNOTATION_HINTS:
        # `tool.annotations?.[hint]`: annotations that are not an object have no hints.
        value = hints.get(hint) if isinstance(hints, dict) else None
        if isinstance(value, bool):
            annotations[hint] = value  # type: ignore[literal-required]
    return annotations if len(annotations) > 0 else None


class CreateMcpToolDefinitionOptions(TypedDict):
    server: str
    tool: Tool
    name: str
    exposure: McpExposure
    namespace: ToolNamespace
    timeoutMs: float
    getClient: Callable[[], Awaitable[McpToolCaller]]
    # Whether `read_mcp_resource` can read the server's resources.
    readableResources: NotRequired[Callable[[], bool]]


def _toolDescription(tool: Any, title: Any, server: str) -> Any:
    """``tool.description?.trim() || title || `MCP tool ${tool.name} from server ${server}```."""
    description = tool.get("description")
    trimmed = jsTrim(description) if description is not None else None
    if jsTruthy(trimmed):
        return trimmed
    return title if jsTruthy(title) else f"MCP tool {tool['name']} from server {server}"


def createMcpToolDefinition(options: CreateMcpToolDefinitionOptions) -> ToolDefinition[Any, McpToolDetails]:
    server, tool = options["server"], options["tool"]
    hints = tool.get("annotations")
    title = tool["title"] if tool.get("title") is not None else (hints.get("title") if isinstance(hints, dict) else None)
    annotations = toToolAnnotations(tool)
    label = f"{server}/{tool['name']}"

    async def execute(
        _toolCallId: str, params: Any, signal: Any = None, onUpdate: Any = None, _ctx: Any = None
    ) -> AgentToolResult:
        client = await options["getClient"]()

        def onProgress(progress: Any) -> None:
            total = "" if progress.get("total", UNDEFINED) is UNDEFINED else f"/{jsString(progress['total'])}"
            text = (
                jsString(progress["message"])
                if progress.get("message") is not None
                else f"Progress {jsString(progress.get('progress', UNDEFINED))}{total}"
            )
            if onUpdate is not None:
                onUpdate(AgentToolResult(content=[TextContent(text=text)], details={"server": server, "tool": tool["name"]}))

        result = await client.callTool(
            tool["name"],
            params if params is not None else {},
            {"signal": signal, "timeoutMs": options["timeoutMs"], "onProgress": onProgress},
        )
        readableResources = options.get("readableResources")
        return await convertMcpResult(
            server,
            tool["name"],
            result,
            {"readableResources": readableResources()} if readableResources is not None else {},
        )

    return ToolDefinition(
        name=options["name"],
        label=label,
        description=_toolDescription(tool, title, server),
        parameters=toParameters(tool["inputSchema"]),
        outputSchema=createMcpResultSchema(tool.get("outputSchema")),
        exposure=toToolExposure(options["exposure"]),
        namespace=options["namespace"],
        annotations=annotations,
        **createMcpToolRenderers(label),
        execute=execute,
    )


def createMcpToolRenderers(label: str) -> ToolRenderers:
    """Renderers of calls to an MCP tool, labeled ``server/tool``, also used before the tool is registered."""

    def renderCall(args: Any, theme: Any, context: Any) -> Any:
        component = context.lastComponent if context.lastComponent is not None else Text("", 0, 0)  # type: ignore[abstract]
        component.setText(formatToolCallWithArgs(label, args, theme, context.expanded))
        return component

    def renderResult(result: Any, options: Any, theme: Any, context: Any) -> Any:
        component = context.lastComponent if context.lastComponent is not None else Container()
        component.clear()
        output = jsTrim(getTextOutput(result, context.showImages))
        if not output:
            return component
        color = "error" if context.isError else "toolOutput"
        styled = "\n".join(theme.fg(color, line) for line in replaceTabs(output).split("\n"))
        component.addChild(Spacer(1))  # type: ignore[abstract]
        if read_field(options, "expanded"):
            component.addChild(Text(styled, 0, 0))  # type: ignore[abstract]
        else:
            # Limit wrapped lines, not logical ones: MCP results are often one long JSON line.
            component.addChild(  # type: ignore[arg-type]
                VisualLinePreview(
                    VisualLinePreviewOptions(
                        text=styled,
                        maxVisualLines=OUTPUT_PREVIEW_LINES,
                        keep="start",
                        formatHint=lambda hidden: (
                            f"{theme.fg('muted', f'... ({hidden} more lines,')} "
                            f"{keyHint('app.tools.expand', 'to expand')}{theme.fg('muted', ')')}"
                        ),
                    )
                )
            )
            fullOutputPath = read_field(read_field(result, "details"), "fullOutputPath")
            if fullOutputPath:
                component.addChild(Text(theme.fg("muted", f"Full output: {fullOutputPath}"), 0, 0))  # type: ignore[abstract]
        return component

    return {"renderCall": renderCall, "renderResult": renderResult}


__all__ = [
    "MCP_OUTPUT_MAX_BYTES",
    "READ_MCP_RESOURCE_TOOL",
    "ConvertMcpResultOptions",
    "CreateMcpToolDefinitionOptions",
    "LimitedMcpContent",
    "McpOutputSaver",
    "McpToolCaller",
    "McpToolDetails",
    "convertMcpResult",
    "createMcpResultSchema",
    "createMcpToolDefinition",
    "createMcpToolName",
    "createMcpToolRenderers",
    "limitMcpContent",
    "saveToTempFile",
    "toModelContent",
    "toToolExposure",
]

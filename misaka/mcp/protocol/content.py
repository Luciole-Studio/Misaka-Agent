"""MCP tool result content, and its conversion to the content a model accepts.

Translated from pi's ``packages/mcp/src/protocol/content.ts``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, NotRequired, TypedDict

from misaka.utils.js import UNDEFINED, jsonStringify, jsString, nullishCoalesce


class ContentAnnotations(TypedDict, total=False):
    audience: list[Literal["user", "assistant"]]
    priority: float
    lastModified: str


TextContent = TypedDict(
    "TextContent",
    {
        "type": Literal["text"],
        "text": str,
        "annotations": NotRequired[ContentAnnotations],
        "_meta": NotRequired[dict[str, Any]],
    },
)

ImageContent = TypedDict(
    "ImageContent",
    {
        "type": Literal["image"],
        "data": str,
        "mimeType": str,
        "annotations": NotRequired[ContentAnnotations],
        "_meta": NotRequired[dict[str, Any]],
    },
)

AudioContent = TypedDict(
    "AudioContent",
    {
        "type": Literal["audio"],
        "data": str,
        "mimeType": str,
        "annotations": NotRequired[ContentAnnotations],
        "_meta": NotRequired[dict[str, Any]],
    },
)

ResourceLinkContent = TypedDict(
    "ResourceLinkContent",
    {
        "type": Literal["resource_link"],
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

TextResourceContents = TypedDict(
    "TextResourceContents",
    {"uri": str, "mimeType": NotRequired[str], "text": str, "_meta": NotRequired[dict[str, Any]]},
)

BlobResourceContents = TypedDict(
    "BlobResourceContents",
    {"uri": str, "mimeType": NotRequired[str], "blob": str, "_meta": NotRequired[dict[str, Any]]},
)

EmbeddedResourceContent = TypedDict(
    "EmbeddedResourceContent",
    {
        "type": Literal["resource"],
        "resource": TextResourceContents | BlobResourceContents,
        "annotations": NotRequired[ContentAnnotations],
        "_meta": NotRequired[dict[str, Any]],
    },
)

type ContentBlock = TextContent | ImageContent | AudioContent | ResourceLinkContent | EmbeddedResourceContent

CallToolResult = TypedDict(
    "CallToolResult",
    {
        "content": list[ContentBlock],
        # Any JSON value, including arrays and `null` (MCP 2026-07-28; earlier revisions allowed only objects).
        "structuredContent": NotRequired[Any],
        "isError": NotRequired[bool],
        "_meta": NotRequired[dict[str, Any]],
    },
)


class _LlmTextContent(TypedDict):
    type: Literal["text"]
    text: str


class _LlmImageContent(TypedDict):
    type: Literal["image"]
    data: str
    mimeType: str


# Tool result content in the shape LLM APIs accept: text and base64 images. Matches the
# `TextContent` and `ImageContent` types of `@earendil-works/pi-ai`.
# MISAKA fork: those are `misaka.ai.types`, which are models; the MCP extension converts these dicts to them.
type LlmContent = _LlmTextContent | _LlmImageContent


def _field(block: Any, key: str) -> Any:
    """``block.key``: a missing field is ``undefined``, and reading one of ``null`` or ``undefined`` throws."""
    if block is None or block is UNDEFINED:
        raise TypeError(f"Cannot read properties of {jsString(block)} (reading '{key}')")
    return block.get(key, UNDEFINED) if isinstance(block, Mapping) else UNDEFINED


def blockToLlmContent(block: ContentBlock) -> LlmContent:
    match _field(block, "type"):
        case "text":
            return {"type": "text", "text": _field(block, "text")}
        case "image":
            return {"type": "image", "data": _field(block, "data"), "mimeType": _field(block, "mimeType")}
        case "audio":
            return {"type": "text", "text": f"[audio {jsString(_field(block, 'mimeType'))} omitted]"}
        case "resource_link":
            return {"type": "text", "text": f"{jsString(_field(block, 'name'))}: {jsString(_field(block, 'uri'))}"}
        case "resource":
            resource = _field(block, "resource")
            # `"text" in resource` throws unless the resource is an object.
            if not isinstance(resource, Mapping | list):
                raise TypeError(f"Cannot use 'in' operator to search for 'text' in {jsString(resource)}")
            if isinstance(resource, Mapping) and "text" in resource:
                return {"type": "text", "text": resource["text"]}
            mimeType = _field(resource, "mimeType")
            if mimeType is not None and mimeType is not UNDEFINED:
                if not isinstance(mimeType, str):
                    raise TypeError("resource.mimeType?.startsWith is not a function")
                if mimeType.startswith("image/"):
                    return {"type": "image", "data": _field(resource, "blob"), "mimeType": mimeType}
            return {
                "type": "text",
                "text": (
                    f"[binary resource {jsString(_field(resource, 'uri'))} "
                    f"({jsString(nullishCoalesce(mimeType, 'unknown type'))}) omitted]"
                ),
            }
        case _:
            return {"type": "text", "text": f"[unsupported MCP content {jsString(_field(block, 'type'))}]"}


def toLlmContent(result: Mapping[str, Any]) -> list[LlmContent]:
    """Convert a tool result to text and image content for a model. Text and images pass through,
    embedded text resources become text, embedded image resources become images, and other blocks
    (audio, resource links, binary resources) become a short text placeholder. A result without
    content blocks but with ``structuredContent`` becomes its JSON, since servers should, but do not
    always, mirror structured results as text."""
    blocks = nullishCoalesce(result.get("content", UNDEFINED), [])
    if not isinstance(blocks, list):
        raise TypeError("(result.content ?? []).map is not a function")
    content = [blockToLlmContent(block) for block in blocks]
    if len(content) == 0 and "structuredContent" in result:
        content.append({"type": "text", "text": jsonStringify(result["structuredContent"], 2)})
    return content


__all__ = [
    "AudioContent",
    "BlobResourceContents",
    "CallToolResult",
    "ContentAnnotations",
    "ContentBlock",
    "EmbeddedResourceContent",
    "ImageContent",
    "LlmContent",
    "ResourceLinkContent",
    "TextContent",
    "TextResourceContents",
    "toLlmContent",
]

"""The ``tool_search`` tool, a copy of the coding agent's: the model searches the deferred tools it is not offered yet,
and the matches are loaded with ``control.addTools``, so the next request offers them. Loading writes the conversation's
``modelTools`` (its ``add``), so loaded tools stay offered on that branch, also after a compaction.

Translated from pi's ``packages/durable/src/tool-search/tool-search.ts`` (v1.3.0).
"""

from __future__ import annotations

import re
from typing import Any, TypedDict

from misaka.chord.context import Context
from misaka.utils.js import jsIsInteger, jsTrim

from ..harness.define import defineExtension, defineTool
from ..search import rankDocuments

TOOL_SEARCH_TOOL_NAME = "tool_search"
DEFAULT_TOOL_SEARCH_LIMIT = 8

TOOL_SEARCH_DESCRIPTION = f"# Tool discovery\n\nSearches over deferred tool metadata with BM25 and exposes matching tools for the next model call.\n\nSome of the tools, such as tools of MCP servers, may not have been provided to you upfront, and you should use this tool (`{TOOL_SEARCH_TOOL_NAME}`) to search for the required tools. For MCP tool discovery, always use `{TOOL_SEARCH_TOOL_NAME}`."
"""Fixed, so it does not change while deferred tools are registered, for example as MCP servers connect."""

toolSearchSchema: dict[str, Any] = {
    "type": "object",
    "required": ["query"],
    "properties": {
        "query": {"type": "string", "description": "Search query for deferred tools."},
        "limit": {
            "type": "number",
            "description": f"Maximum number of tools to return. Defaults to {DEFAULT_TOOL_SEARCH_LIMIT}.",
        },
    },
}


class ToolSearchToolDetails(TypedDict):
    """``tool_search`` result details: the tools it loaded."""

    loaded: list[str]


def isObject(value: Any) -> bool:
    return isinstance(value, dict)


def schemaText(schema: Any, parts: list[str]) -> None:
    """Schema descriptions and property names, recursively."""
    if not isObject(schema):
        return
    if isinstance(schema.get("description"), str):
        parts.append(schema["description"])
    if isObject(schema.get("properties")):
        for name, property in schema["properties"].items():
            parts.append(name)
            schemaText(property, parts)
    schemaText(schema.get("items"), parts)
    for key in ("anyOf", "oneOf", "allOf"):
        variants = schema.get(key)
        if isinstance(variants, list):
            for variant in variants:
                schemaText(variant, parts)


def searchText(tool: dict[str, Any]) -> str:
    """Search text of a tool: its name, the name with ``_`` as spaces, its description, and its parameters' text."""
    parts = [tool["name"], tool["name"].replace("_", " "), tool["description"]]
    schemaText(tool["parameters"], parts)
    return " ".join(part for part in parts if jsTrim(part) != "")


_LINE_BREAK = re.compile(r"\r?\n")


def createToolSearchTool() -> dict[str, Any]:
    async def execute(args: dict[str, Any], api: Any, context: Context) -> dict[str, Any]:
        query = args["query"]
        if jsTrim(query) == "":
            raise RuntimeError("query must not be empty")
        limit = args.get("limit")
        maximum = DEFAULT_TOOL_SEARCH_LIMIT if limit is None else limit
        if not jsIsInteger(maximum) or maximum <= 0:
            raise RuntimeError("limit must be a positive integer")
        candidates = (await api.agent(context))["deferred"]
        documents = [{"name": tool["name"], "text": searchText(tool)} for tool in candidates]
        loaded = rankDocuments(query, documents, int(maximum))  # type: ignore[arg-type]
        if len(loaded) == 0:
            text = "No matching tools found."
        else:
            lines = []
            for name in loaded:
                description = next((tool["description"] for tool in candidates if tool["name"] == name), "")
                lines.append(f"- {name}: {_LINE_BREAK.split(jsTrim(description))[0]}")
            text = f"Loaded {len(loaded)} tool{'' if len(loaded) == 1 else 's'}. They are available from your next call:\n" + "\n".join(lines)
        details: ToolSearchToolDetails = {"loaded": loaded}
        return {
            "output": [{"type": "text", "text": text}],
            "details": details,
            **({} if len(loaded) == 0 else {"control": {"addTools": loaded}}),
        }

    return defineTool(
        {
            "name": TOOL_SEARCH_TOOL_NAME,
            "description": TOOL_SEARCH_DESCRIPTION,
            "parameters": toolSearchSchema,
            # Searching changes what the model is offered; scripts have their own `searchTools()`.
            "callers": ["model"],
            "replay": "safe",
            "execute": execute,
        }
    )


def createToolSearchExtension() -> dict[str, Any]:
    """The ``tool_search`` extension: install it and select it where the model should find deferred tools."""
    return defineExtension({"name": "tool-search", "tools": [createToolSearchTool()]})


__all__ = [
    "DEFAULT_TOOL_SEARCH_LIMIT",
    "TOOL_SEARCH_DESCRIPTION",
    "TOOL_SEARCH_TOOL_NAME",
    "ToolSearchToolDetails",
    "createToolSearchExtension",
    "createToolSearchTool",
    "schemaText",
    "searchText",
]

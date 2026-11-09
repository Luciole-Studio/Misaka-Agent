"""Tool discovery: a BM25 ranker over tool metadata, shared by ``searchTools()`` in codemode scripts and
the optional ``tool_search`` tool.

Translated from pi's ``coding-agent/src/extensions/tool-search/tool.ts``.

``tool_search`` searches tools that are not declared to the model (``codemode`` and ``deferred``
exposure) and loads the matches, so they are declared for the next model call. Loading goes through
the active tool set, so it is recorded in the transcript like any other tool change and survives
``/tree``, resume, and fork on that branch.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypedDict

from misaka.agent.types import AgentToolResult
from misaka.ai.types import TextContent
from misaka.ai.utils.typebox_helpers import typeboxSchema
from misaka.core.extensions.types import (
    ToolDefinition,
    ToolExposure,
    ToolNamespace,
)
from misaka.utils.js import jsTrim
from misaka.utils.values import read_field

TOOL_SEARCH_TOOL_NAME = "tool_search"
DEFAULT_TOOL_SEARCH_LIMIT = 8


@dataclass(frozen=True, slots=True)
class ToolSearchDocument:
    """A tool as the ranker sees it: its name and the text built by :func:`createToolSearchDocument`."""

    name: str
    text: str


@dataclass(frozen=True, slots=True)
class ToolSearchMatch:
    name: str
    score: float


class ToolRanker(Protocol):
    """Ranks tools for a query. BM25 today; a hybrid ranker with embeddings can replace it."""

    def rank(self, query: str, documents: Sequence[ToolSearchDocument], limit: int) -> list[ToolSearchMatch]: ...


STOP_WORDS: frozenset[str] = frozenset({
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "with",
})

_ES_PLURAL = re.compile(r"(ches|shes|sses|xes|zes)\Z")


def _stem(term: str) -> str:
    """Naive singular form, so ``issues`` matches ``issue`` and ``searches`` matches ``search``."""
    if len(term) > 4 and term.endswith("ies"):
        return f"{term[:-3]}y"
    if len(term) > 4 and _ES_PLURAL.search(term):
        return term[:-2]
    if len(term) > 3 and term.endswith("s") and not term.endswith("ss"):
        return term[:-1]
    return term


def tokenize(text: str) -> list[str]:
    """Lowercase terms, split at camelCase boundaries and non-alphanumerics, without stop words."""
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    return [
        _stem(term)
        for term in re.split(r"[^a-z0-9]+", text.lower())
        if len(term) > 0 and term not in STOP_WORDS
    ]


def _isObject(value: Any) -> bool:
    return isinstance(value, Mapping)


def _schemaText(schema: Any, parts: list[str]) -> None:
    """Schema descriptions and property names, recursively."""
    if not _isObject(schema):
        return
    if isinstance(schema.get("description"), str):
        parts.append(schema["description"])
    properties = schema.get("properties")
    if _isObject(properties):
        for name, property in properties.items():
            parts.append(name)
            _schemaText(property, parts)
    _schemaText(schema.get("items"), parts)
    for key in ("anyOf", "oneOf", "allOf"):
        variants = schema.get(key)
        if isinstance(variants, list):
            for variant in variants:
                _schemaText(variant, parts)


def createToolSearchDocument(tool: Any, namespace: ToolNamespace | None = None) -> ToolSearchDocument:
    """Search text of a tool: the name, the name with ``_`` as spaces, the description, schema
    descriptions and property names, and the namespace with its description and instructions."""
    name = read_field(tool, "name")
    parts = [name, name.replace("_", " "), read_field(tool, "description")]
    parameters = read_field(tool, "parameters")
    # MISAKA fork: misaka tools may declare their parameters as a pydantic model class (pi's are
    # always TypeBox objects); index the JSON schema it produces.
    if isinstance(parameters, type) and hasattr(parameters, "model_json_schema"):
        parameters = typeboxSchema(parameters)
    _schemaText(parameters, parts)
    if namespace:
        parts.extend([namespace["name"], namespace.get("description") or "", namespace.get("instructions") or ""])
    return ToolSearchDocument(name=name, text=" ".join(part for part in parts if jsTrim(part)))


class Bm25Ranker:
    """Okapi BM25 with the usual parameters. Ties keep document order."""

    def __init__(self, options: Mapping[str, float] | None = None) -> None:
        options = options if options is not None else {}
        self._k1 = options.get("k1") if options.get("k1") is not None else 1.2
        self._b = options.get("b") if options.get("b") is not None else 0.75

    def rank(self, query: str, documents: Sequence[ToolSearchDocument], limit: int) -> list[ToolSearchMatch]:
        queryTerms = list(dict.fromkeys(tokenize(query)))
        if len(queryTerms) == 0 or len(documents) == 0 or limit <= 0:
            return []
        termCounts: list[dict[str, int]] = []
        for document in documents:
            counts: dict[str, int] = {}
            for term in tokenize(document.text):
                counts[term] = counts.get(term, 0) + 1
            termCounts.append(counts)
        lengths = [sum(counts.values()) for counts in termCounts]
        averageLength = sum(lengths) / len(documents) or 1
        idf = {
            term: math.log(1 + (len(documents) - frequency + 0.5) / (frequency + 0.5))
            for term in queryTerms
            for frequency in [sum(1 for counts in termCounts if term in counts)]
        }
        matches: list[ToolSearchMatch] = []
        for index, document in enumerate(documents):
            score = 0.0
            for term in queryTerms:
                count = termCounts[index].get(term)
                if not count:
                    continue
                norm = self._k1 * (1 - self._b + (self._b * lengths[index]) / averageLength)
                score += idf.get(term, 0) * ((count * (self._k1 + 1)) / (count + norm))
            if score > 0:
                matches.append(ToolSearchMatch(name=document.name, score=score))
        return sorted(matches, key=lambda match: -match.score)[:limit]


# `Type.Object(...)` as TypeBox emits it (key order `type`, `required`, `properties`): the bytes the model sees.
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


class ToolSearchInput(TypedDict, total=False):
    query: str
    limit: float


@dataclass(frozen=True, slots=True)
class ToolSearchResultTool:
    name: str
    description: str


class ToolSearchToolDetails(TypedDict):
    # Tools loaded by this call.
    loaded: list[str]


class _ToolSet(Protocol):
    def getAllTools(self) -> list[Any]: ...

    def getActiveTools(self) -> list[str]: ...

    def setActiveTools(self, toolNames: list[str]) -> None: ...


@dataclass(slots=True)
class ToolSearchToolOptions:
    # The session's tools. `tool_search` searches the tools that are not declared to the model and
    # activates the matches. Without it, the tool finds nothing. An `ExtensionAPI` fits.
    tools: _ToolSet | None = None


def _isSearchable(exposure: ToolExposure) -> bool:
    """Whether ``tool_search`` can load a tool with this exposure."""
    return exposure in ("codemode", "deferred")


def _searchAndLoad(
    tools: _ToolSet, query: str, limit: int, callable: set[str] | None = None
) -> list[ToolSearchResultTool]:
    """Rank the searchable tools that are not active yet and activate the matches, so the next model
    call declares them. Activation is recorded in the transcript like any tool change."""
    active = tools.getActiveTools()
    candidates = [
        tool
        for tool in tools.getAllTools()
        if _isSearchable(read_field(tool, "exposure"))
        and read_field(tool, "name") not in active
        # MISAKA fork: inside a tool scope (a research window) only the tools the run can reach;
        # outside one every codemode and deferred tool is callable, as upstream.
        and (callable is None or read_field(tool, "name") in callable)
    ]
    documents = [createToolSearchDocument(tool, read_field(tool, "namespace")) for tool in candidates]
    matches = Bm25Ranker().rank(query, documents, limit)
    if len(matches) > 0:
        tools.setActiveTools([*active, *(match.name for match in matches)])
    return [
        ToolSearchResultTool(
            name=match.name,
            description=next(
                (read_field(tool, "description") for tool in candidates if read_field(tool, "name") == match.name),
                "",
            )
            or "",
        )
        for match in matches
    ]


def _firstLine(text: str) -> str:
    return re.split(r"\r?\n", jsTrim(text))[0]


# The `tool_search` description. It does not list the searchable tools or their namespaces, so it
# stays the same while tools are registered, for example when MCP servers connect.
TOOL_SEARCH_DESCRIPTION = (
    "# Tool discovery\n\nSearches over deferred tool metadata with BM25 and exposes matching tools for the next "
    "model call.\n\nSome of the tools, such as tools of MCP servers, may not have been provided to you upfront, and "
    f"you should use this tool (`{TOOL_SEARCH_TOOL_NAME}`) to search for the required tools. For MCP tool discovery, "
    f"always use `{TOOL_SEARCH_TOOL_NAME}`."
)


def createToolSearchToolDefinition(
    options: ToolSearchToolOptions | None = None,
) -> ToolDefinition[ToolSearchInput, ToolSearchToolDetails]:
    options = options or ToolSearchToolOptions()

    async def execute(_toolCallId, params, _signal=None, _onUpdate=None, ctx=None) -> AgentToolResult:
        query = params["query"]
        limit = params.get("limit")
        if jsTrim(query) == "":
            raise RuntimeError("query must not be empty")
        max = DEFAULT_TOOL_SEARCH_LIMIT if limit is None else limit
        # `Number.isInteger`: a whole float counts, a bool is not a number.
        if isinstance(max, bool) or not (isinstance(max, int) or (isinstance(max, float) and max.is_integer())) or max <= 0:
            raise RuntimeError("limit must be a positive integer")
        reachable = read_field(ctx, "tools") if ctx is not None else None
        callable = {read_field(tool, "name") for tool in reachable} if reachable is not None else None
        tools = _searchAndLoad(options.tools, query, int(max), callable) if options.tools is not None else []
        if len(tools) == 0:
            text = "No matching tools found."
        else:
            listed = "\n".join(f"- {tool.name}: {_firstLine(tool.description)}" for tool in tools)
            text = (
                f"Loaded {len(tools)} tool{'' if len(tools) == 1 else 's'}. "
                f"They are available from your next call:\n{listed}"
            )
        details: ToolSearchToolDetails = {"loaded": [tool.name for tool in tools]}
        return AgentToolResult(content=[TextContent(text=text)], details=details)

    return ToolDefinition(
        name=TOOL_SEARCH_TOOL_NAME,
        label=TOOL_SEARCH_TOOL_NAME,
        description=TOOL_SEARCH_DESCRIPTION,
        promptSnippet="Search for tools that are not loaded yet and load the matches",
        parameters=toolSearchSchema,
        # Searching is not something scripts need; it changes what the model sees.
        exposure="model-only",
        # A query can match any tool that is not declared yet.
        reachesTools=lambda _input: "all",
        execute=execute,
    )


__all__ = [
    "DEFAULT_TOOL_SEARCH_LIMIT",
    "STOP_WORDS",
    "TOOL_SEARCH_DESCRIPTION",
    "TOOL_SEARCH_TOOL_NAME",
    "Bm25Ranker",
    "ToolRanker",
    "ToolSearchDocument",
    "ToolSearchInput",
    "ToolSearchMatch",
    "ToolSearchResultTool",
    "ToolSearchToolDetails",
    "ToolSearchToolOptions",
    "createToolSearchDocument",
    "createToolSearchToolDefinition",
    "tokenize",
    "toolSearchSchema",
]

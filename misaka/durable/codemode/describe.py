"""What the model reads about code mode, copied from the coding agent's code mode: the tool's fixed description (the
script environment and its globals), and the ``codemode`` prompt section, which lists the tools scripts can call as
TypeScript declarations, grouped by namespace and limited to a token budget. Tools that do not fit are found with
``searchTools()``. The section renders from the conversation's callable tools, so it changes with them; the tool's
description never does.

Translated from pi's ``packages/durable/src/codemode/describe.ts`` (v1.3.0). A namespace is a ``dict``
(``CodemodeNamespace``: ``name``, ``prefix``, optional ``description`` and ``instructions``).
"""

from __future__ import annotations

import functools
import math
from collections.abc import Sequence
from typing import Any, NotRequired, TypedDict

from misaka.codemode import renderToolSample, toCodemodeIdentifier
from misaka.utils.collation import localeCompare
from misaka.utils.js import jsLength, jsTrim

from .models import MODELS_DESCRIPTION


class CodemodeNamespace(TypedDict):
    """A group of tools named by a common prefix, such as an MCP server's ``mcp__github__``. Tool names starting with
    ``prefix`` belong to the namespace; the longest matching prefix wins."""

    name: str
    prefix: str
    description: NotRequired[str]
    instructions: NotRequired[str]


DEFAULT_CODEMODE_INLINE_BUDGET = 3000
"""Default token budget of the tool declarations in the ``codemode`` section, as in the coding agent."""
CHARS_PER_TOKEN = 4
"""Characters per token when estimating the cost of a tool section."""

TEXT_OUTPUT_SCHEMA: dict[str, Any] = {"type": "string"}

DESCRIPTION_INTRO = """Run JavaScript that calls other tools. The input is raw JavaScript (not JSON, no code fence), run as an async function body in a QuickJS sandbox: top-level `await` and `return` work. No Node, file system, network, or timers.
- `await tools.<name>({ ...args })` resolves to a string, or an object if the tool's declaration says so, and rejects with an Error on failure. A tool whose declaration returns an object resolves on failure too when it returned that object; check its fields. Calls still running when the script ends are cancelled.
- The script survives a restart: it resumes where it was, without running finished calls again.
- Optional first line: `// @options: {"max_output_tokens": 10000, "timeout_ms": 60000}`"""


def codemodeDescription(models: bool) -> str:
    """The code mode tool's description: the script environment and its globals, the same for every conversation."""
    globals = [
        "Globals:",
        "- `text(value)`, `image(dataUrlOrImageBlock)`, `console.log(...)`, and top-level `return` add output; `exit()` ends the script. With several text items, each starts with a `==> text N/M <==` line, and `console` lines follow the other output in one `<console_output>` block. `image()` also saves the image to a temp file and the result names its path.",
        "- `store(key, value)` and `load(key)` keep JSON values across codemode calls.",
        "- `ALL_TOOLS`, `await searchTools(query, { limit?, namespace? })`, `await describeTool(name)`, `await describeNamespace(name)`: find tools the `codemode` section does not list.",
    ]
    return "\n\n".join(
        [
            DESCRIPTION_INTRO,
            "\n".join(globals),
            *([MODELS_DESCRIPTION] if models else []),
            "The `codemode` section of the system prompt lists the tools scripts can call, by namespace, as far as space allows.",
        ]
    )


def declarationOf(tool: dict[str, Any]) -> dict[str, Any]:
    """What a script sees of a tool: its declaration. Tools without an output schema resolve to their text output."""
    structured = tool.get("structuredOutputSchema")
    return {
        "name": tool["name"],
        "description": tool["description"],
        "inputSchema": tool["parameters"],
        "outputSchema": TEXT_OUTPUT_SCHEMA if structured is None else structured,
    }


def sampleOf(tool: dict[str, Any]) -> str:
    """``toolSample`` of a tool: its description and TypeScript declaration."""
    return renderToolSample(declarationOf(tool))


def namespaceOf(name: str, namespaces: Sequence[CodemodeNamespace]) -> CodemodeNamespace | None:
    """The namespace of a tool: the one with the longest prefix its name starts with."""
    found: CodemodeNamespace | None = None
    for namespace in namespaces:
        if name.startswith(namespace["prefix"]) and (
            found is None or jsLength(namespace["prefix"]) > jsLength(found["prefix"])
        ):
            found = namespace
    return found


def renderToolSection(tool: dict[str, Any]) -> str:
    """``### `id` (`raw name`)`` followed by the tool's description and declaration."""
    id = toCodemodeIdentifier(tool["name"])
    heading = f"### `{id}`" if id == tool["name"] else f"### `{id}` (`{tool['name']}`)"
    return f"{heading}\n{jsTrim(sampleOf(tool))}"


class CatalogEntry(TypedDict):
    name: str
    section: str
    cost: int


class CatalogGroup(TypedDict):
    namespace: CodemodeNamespace | None
    entries: list[CatalogEntry]


def selectCatalog(groups: Sequence[CatalogGroup], budget: int) -> set[str]:
    """Pick the tool sections that fit the budget: in each round every group (tools without a namespace first, then
    namespaces by name) places its cheapest remaining tool; a group whose next tool does not fit drops out while the
    others continue. Every namespace is represented before any namespace is complete."""
    queues = [sorted(group["entries"], key=lambda entry: entry["cost"]) for group in groups]
    shown: set[str] = set()
    remaining = budget
    active = [queue for queue in queues if len(queue) > 0]
    while len(active) > 0:
        kept = []
        for queue in active:
            following = queue[0]
            if following["cost"] > remaining:
                continue
            remaining -= following["cost"]
            shown.add(following["name"])
            queue.pop(0)
            if len(queue) > 0:
                kept.append(queue)
        active = kept
    return shown


def codemodeSection(
    tools: Sequence[dict[str, Any]], namespaces: Sequence[CodemodeNamespace], budget: int
) -> str | None:
    """The ``codemode`` section: the tools scripts can call, grouped by namespace, with their declarations as far as the
    budget allows. ``None`` without callable tools."""
    if len(tools) == 0:
        return None
    groups: dict[str, CatalogGroup] = {"": {"namespace": None, "entries": []}}
    for tool in tools:
        namespace = namespaceOf(tool["name"], namespaces)
        key = f"ns:{namespace['name']}" if namespace else ""
        group = groups.get(key)
        if group is None:
            group = {"namespace": namespace, "entries": []}
        groups[key] = group
        section = renderToolSection(tool)
        group["entries"].append({"name": tool["name"], "section": section, "cost": math.ceil(jsLength(section) / CHARS_PER_TOKEN)})

    def compare(a: CatalogGroup, b: CatalogGroup) -> int:
        if a["namespace"] is None:
            return -1
        if b["namespace"] is None:
            return 1
        # MISAKA fork: `misaka.utils.collation` orders ASCII names as ICU's root collation does and puts other
        # characters after them by code point; upstream's `localeCompare` follows the process locale, so its own order
        # of non-ASCII names differs from one machine to another (ICU puts `ß` beside `ss`, `ja-JP` puts Han first).
        return localeCompare(a["namespace"]["name"], b["namespace"]["name"])

    ordered = sorted(groups.values(), key=functools.cmp_to_key(compare))
    shown = selectCatalog(ordered, budget)

    toolSections = ["Tools scripts can call (`await tools.<name>(args)`):"]
    for group in ordered:
        namespace, entries = group["namespace"], group["entries"]
        if len(entries) == 0:
            continue
        visible = [entry for entry in entries if entry["name"] in shown]
        if namespace:
            listing = (
                ""
                if len(visible) == len(entries)
                else " (tools not listed)"
                if len(visible) == 0
                else " (some tools not listed)"
            )
            description = jsTrim(namespace["description"]) if namespace.get("description") is not None else None
            toolSections.append(f"## {namespace['name']}{listing}{f'{chr(10)}{description}' if description else ''}")
        elif len(visible) < len(entries):
            toolSections.append("Some tools are not listed: find them with `searchTools()`.")
        for entry in visible:
            toolSections.append(entry["section"])
    return "\n\n".join(toolSections)


__all__ = [
    "CHARS_PER_TOKEN",
    "DEFAULT_CODEMODE_INLINE_BUDGET",
    "DESCRIPTION_INTRO",
    "TEXT_OUTPUT_SCHEMA",
    "CodemodeNamespace",
    "codemodeDescription",
    "codemodeSection",
    "declarationOf",
    "namespaceOf",
    "renderToolSection",
    "sampleOf",
    "selectCatalog",
]

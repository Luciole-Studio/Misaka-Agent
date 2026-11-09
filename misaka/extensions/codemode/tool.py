"""The ``codemode`` tool: the model writes JavaScript that calls other tools. Scripts use ``tools``,
``ALL_TOOLS``, ``text()``, ``image()``, ``exit()``, ``store()``/``load()``, ``console.*``, and
``return <value>``, may start with a ``// @options:`` line, and reach the model catalog, classifiers,
and image models through ``models.*``. Results start with a "Script completed" or "Script failed"
header.

Scripts can call the agent loop's nested tools: active ``direct`` tools and every ``codemode`` or
``deferred`` tool. Nested calls run through the agent loop's tool pipeline (``ctx.executeTool``), so
validation, ``tool_call``/``tool_result`` hooks, and permission checks apply exactly as for direct
calls. Only the script's output reaches the model; nested results do not.

Nested results are handed to the script as follows:
- A tool that declares ``outputSchema`` resolves to its ``structuredContent``, also for error results
  that carry one.
- Any other tool resolves to its text content as one string.
- A failed, blocked, or invalid call rejects with an Error carrying the tool's error text.

A script that fails returns a normal error result that keeps its partial output, followed by
"Script error:" and the error. ``store(key, value)`` and ``load(key)`` keep JSON values across calls;
successful scripts append their writes to the session as ``codemode-store`` custom entries, so each
branch sees the values written on its own path.

Translated from pi's ``coding-agent/src/extensions/codemode/tool.ts``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, NotRequired, Protocol, TypedDict

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.types import GrammarConstrainedSamplingConfig
from misaka.ai.utils.typebox_helpers import typeboxSchema
from misaka.codemode.declarations import (
    renderToolOutputType,
    renderToolSample,
    toCodemodeIdentifier,
)
from misaka.codemode.source import CODEMODE_SOURCE_GRAMMAR
from misaka.codemode.types import CodemodeJsonSchema
from misaka.core.extensions.types import (
    ToolDefinition,
    ToolLoadout,
    ToolLoadoutChanges,
    ToolNamespace,
)
from misaka.core.settings_defaults import SETTINGS_DEFAULTS
from misaka.core.settings_manager import CodemodeMode
from misaka.core.tools.tool_definition_wrapper import wrapToolDefinition
from misaka.utils.collation import localeSortKey
from misaka.utils.js import (
    jsCollapseWhitespace,
    jsLength,
    jsTrim,
    nullishCoalesce,
    objectEntries,
)
from misaka.utils.values import read_field

from .execute_lazy import loadCodemodeExecutor
from .renderer import codemodeRenderers

CODEMODE_TOOL_NAME = "codemode"

# Custom entry type holding one script's `store()` writes: `CodemodeStoreEntryData`.
CODEMODE_STORE_ENTRY_TYPE = "codemode-store"


class CodemodeStoreEntryData(TypedDict):
    set: dict[str, Any]
    delete: list[str]




class CodemodeModelRuntime(Protocol):
    """The part of the model registry that scripts reach through `models`."""

    def getModelsOfType(self, type: Any, provider: Any = None) -> Any: ...

    def getAvailableOfType(self, type: Any, provider: Any = None) -> Any: ...

    def getModelOfType(self, type: Any, provider: Any, modelId: Any) -> Any: ...

    def classify(self, model: Any, context: Any, options: Any = None) -> Any: ...

    def generateImages(self, model: Any, context: Any, options: Any = None) -> Any: ...


@dataclass(slots=True)
class CodemodeToolOptions:
    # Namespace of a tool, for `searchTools()` ranking and its `namespace` filter.
    getToolNamespace: Callable[[str], ToolNamespace | None] | None = None
    # Prompt guidelines of every tool, by tool name, shown with declarations by `describeTool()` and `ALL_TOOLS`.
    getToolGuidelines: Callable[[], Mapping[str, Sequence[str]]] | None = None
    # Expose the `models` namespace to scripts, backed by the session's model registry
    # (`ctx.modelRegistry`). Without it, `models` is not declared.
    models: bool | None = None
    # Persists `store()` writes as a session custom entry. Without it, writes last only for the
    # current script; `load()` still reads entries already on the branch.
    appendEntry: Callable[[str, CodemodeStoreEntryData], None] | None = None
    # How the tool presents the loadout while active (the `codemode.mode` setting). Default: `on`.
    getMode: Callable[[], CodemodeMode] | None = None
    # Token budget for tool declarations in the description. Default: `DEFAULT_CODEMODE_INLINE_BUDGET`.
    getInlineBudget: Callable[[], float | None] | None = None


TEXT_OUTPUT_SCHEMA: CodemodeJsonSchema = {"type": "string"}

# `Type.Object(...)` as TypeBox emits it (key order `type`, `required`, `properties`): the bytes the model sees.
codemodeSchema: dict[str, Any] = {
    "type": "object",
    "required": ["code"],
    "properties": {"code": {"type": "string", "description": "Raw JavaScript source."}},
}


class CodemodeToolInput(TypedDict):
    code: str


type CodemodeNestedCallStatus = Literal["running", "ok", "error", "cancelled"]


class CodemodeNestedCall(TypedDict):
    # Tool call id of the nested call, `<codemode call id>/<n>`.
    id: str
    name: str
    # Compact JSON of the arguments, truncated for display.
    args: str
    status: CodemodeNestedCallStatus
    durationMs: NotRequired[float]
    # Error text, truncated for display.
    error: NotRequired[str]
    # Cost in USD of a `models.*` call that reported usage.
    cost: NotRequired[float]


class CodemodeToolDetails(TypedDict):
    calls: list[CodemodeNestedCall]
    # Temp file with the full text output, when the output was truncated.
    fullOutputPath: NotRequired[str]


class _SystemPromptContribution(TypedDict):
    snippet: str
    guidelines: tuple[str, ...]


codemodeToolSystemPromptContribution: _SystemPromptContribution = {
    "snippet": "Run JavaScript that calls other tools",
    "guidelines": (
        "Use codemode to batch independent tool calls (Promise.allSettled), chain them, or filter large output, instead of many separate calls.",
    ),
}

# The reference for scripts: globals, tool results, `store()`, the `models` API, and limits.
# MISAKA fork: the file ships inside the package (pi reads it from its docs directory).
CODEMODE_DOCS_PATH = str(Path(__file__).with_name("codemode.md"))

DESCRIPTION_INTRO = (
    "Run JavaScript that calls other tools. The input is raw JavaScript (not JSON, no code fence), run as an async "
    "function body in a QuickJS sandbox: top-level `await` and `return` work. No Node, file system, network, or timers.\n"
    "- `await tools.<name>({ ...args })` resolves to a string, or an object if the tool's declaration says so, and "
    "rejects with an Error on failure. A tool whose declaration returns an object resolves on failure too when it "
    "returned that object; check its fields. Calls still running when the script ends are cancelled.\n"
    '- Optional first line: `// @options: {"max_output_tokens": 10000, "timeout_ms": 60000}`'
)


def describeGlobals(models: bool) -> str:
    """One line per global. The details live in `CODEMODE_DOCS_PATH`."""
    lines = [
        "Globals:",
        "- `text(value)`, `image(dataUrlOrImageBlock)`, `console.log(...)`, and top-level `return` add output; `exit()` ends the script. With several text items, each starts with a `==> text N/M <==` line, and `console` lines follow the other output in one `<console_output>` block. `image()` also saves the image to a temp file and the result names its path.",
        "- `store(key, value)` and `load(key)` keep JSON values across codemode calls.",
        "- `ALL_TOOLS`, `await searchTools(query, { limit?, namespace? })`, `await describeTool(name)`, `await describeNamespace(name)`: find unlisted tools, such as MCP tools.",
    ]
    if models:
        lines.append(f"- `models`: classifiers and image generation. Read {CODEMODE_DOCS_PATH} first.")
    return "\n".join(lines)


# Default for `CodemodeDescriptionOptions.inlineBudget`, in estimated tokens.
DEFAULT_CODEMODE_INLINE_BUDGET = SETTINGS_DEFAULTS["codemode"]["inlineBudget"]
# Characters per token when estimating the cost of a tool section.
CHARS_PER_TOKEN = 4


def _inputSchema(tool: Any) -> CodemodeJsonSchema:
    parameters = read_field(tool, "parameters")
    # MISAKA fork: misaka tools may declare their parameters as a pydantic model class (pi's are
    # always TypeBox objects); the declaration renders the JSON schema it produces.
    if isinstance(parameters, type) and hasattr(parameters, "model_json_schema"):
        return typeboxSchema(parameters)
    return parameters


def toCodemodeDeclaration(tool: Any, guidelines: Sequence[str] = ()) -> dict[str, Any]:
    """What a script sees of a tool: its description followed by its prompt guidelines, which the system
    prompt only has for declared tools. Tools without an output schema resolve to their text output."""
    outputSchema = read_field(tool, "outputSchema")
    bullets = [f"- {jsTrim(guideline)}" for guideline in guidelines if jsTrim(guideline)]
    description = read_field(tool, "description")
    return {
        "name": read_field(tool, "name"),
        "description": f"{jsTrim(description)}\n\n{'\n'.join(bullets)}" if len(bullets) > 0 else description,
        "inputSchema": _inputSchema(tool),
        "outputSchema": outputSchema if outputSchema is not None else TEXT_OUTPUT_SCHEMA,
    }


def getCodemodeCallableTools(tools: Sequence[Any]) -> list[Any]:
    """Tools a script may call: every given tool except the codemode tool itself."""
    return [tool for tool in tools if read_field(tool, "name") != CODEMODE_TOOL_NAME]


class CodemodeDescriptionOptions(TypedDict, total=False):
    # Declare the `models` namespace; only for tools created with model access.
    models: bool
    # Namespace of each tool, by tool name. Tools of one namespace are listed under one heading.
    namespaces: Mapping[str, ToolNamespace]
    # Tools that are callable but never listed with their declaration (`deferred` exposure).
    deferred: set[str]
    # Prompt guidelines of each tool, by tool name, listed after its description.
    guidelines: Mapping[str, Sequence[str]]
    # Estimated tokens (characters / 4) the tool sections may use. Tools that do not fit are left
    # out, like deferred tools. Unset lists every tool that is not deferred.
    inlineBudget: float | None


def renderToolSection(declaration: Mapping[str, Any]) -> str:
    """``### `id` (`raw name`)`` followed by the tool's description and declaration."""
    id = toCodemodeIdentifier(declaration["name"])
    heading = f"### `{id}`" if id == declaration["name"] else f"### `{id}` (`{declaration['name']}`)"
    return f"{heading}\n{jsTrim(renderToolSample(declaration))}"


@dataclass(slots=True)
class _CatalogEntry:
    name: str
    section: str
    cost: int


@dataclass(slots=True)
class _CatalogGroup:
    namespace: ToolNamespace | None
    entries: list[_CatalogEntry] = field(default_factory=list)


def selectCatalog(groups: Sequence[_CatalogGroup], budget: float | None) -> set[str]:
    """Pick the tool sections that fit the budget, like OpenCode's catalog: in each round every group
    (tools without a namespace first, then namespaces by name) places its cheapest remaining tool; a
    group whose next tool does not fit drops out while the others continue. Every namespace is
    represented before any namespace is complete."""
    if budget is None:
        return {entry.name for group in groups for entry in group.entries}
    queues = [sorted(group.entries, key=lambda entry: entry.cost) for group in groups]
    shown: set[str] = set()
    remaining = budget
    active = [queue for queue in queues if len(queue) > 0]
    while len(active) > 0:
        kept = []
        for queue in active:
            next_ = queue[0]
            if next_.cost > remaining:
                continue
            remaining -= next_.cost
            shown.add(next_.name)
            queue.pop(0)
            if len(queue) > 0:
                kept.append(queue)
        active = kept
    return shown


def createCodemodeDescription(tools: Sequence[Any], options: CodemodeDescriptionOptions | None = None) -> str:
    """Model-facing description: the helper list, guidance for finding tools that are not listed, the
    ``models`` API, and one section per listed tool, grouped by namespace. Deferred tools are never
    listed and do not affect the description at all, so it stays the same while tools are registered
    late (for example by MCP servers that connect). Tool sections are limited to ``inlineBudget``."""
    options = options or {}
    deferred = options.get("deferred") or set()
    namespaces = options.get("namespaces") or {}
    guidelines = options.get("guidelines") or {}
    declarations = [
        toCodemodeDeclaration(tool, guidelines.get(read_field(tool, "name"), ()))
        for tool in getCodemodeCallableTools(tools)
        if read_field(tool, "name") not in deferred
    ]
    groups: dict[str, _CatalogGroup] = {"": _CatalogGroup(namespace=None)}
    for declaration in declarations:
        namespace = namespaces.get(declaration["name"])
        key = f"ns:{namespace['name']}" if namespace else ""
        group = groups.setdefault(key, _CatalogGroup(namespace=namespace))
        section = renderToolSection(declaration)
        group.entries.append(_CatalogEntry(name=declaration["name"], section=section, cost=math.ceil(jsLength(section) / CHARS_PER_TOKEN)))
    ordered = sorted(
        groups.values(),
        key=lambda group: (0, ()) if group.namespace is None else (1, localeSortKey(group.namespace["name"])),
    )
    shown = selectCatalog(ordered, options.get("inlineBudget"))

    sections = [DESCRIPTION_INTRO, describeGlobals(options.get("models") is True)]
    if len(declarations) == 0:
        return "\n\n".join(sections)

    toolSections = ["Nested tools:"]
    for group in ordered:
        visible = [entry for entry in group.entries if entry.name in shown]
        if group.namespace:
            # Only tools that did not fit the budget are counted as not listed here.
            listing = (
                ""
                if len(visible) == len(group.entries)
                else " (tools not listed)" if len(visible) == 0 else " (some tools not listed)"
            )
            description = jsTrim(group.namespace.get("description") or "")
            toolSections.append(f"## {group.namespace['name']}{listing}{f'{chr(10)}{description}' if description else ''}")
        for entry in visible:
            toolSections.append(entry.section)
    sections.append("\n\n".join(toolSections))
    return "\n\n".join(sections)


def describeFields(schema: Any, depth: int) -> str | None:
    """The field names of an object schema, ``{ output, exit_code, full_output_path? }``, with the fields of
    object-typed fields ``depth`` levels deep: ``{ content, structuredContent?: { results } }``. ``None`` for
    other schemas."""
    # `typeof schema !== "object" || schema === null`: an array has no `type` and is left out below.
    if not isinstance(schema, dict | list):
        return None
    fields_ = schema if isinstance(schema, dict) else {}
    type, properties, required = fields_.get("type"), fields_.get("properties"), fields_.get("required")
    if type != "object" or not isinstance(properties, dict | list):
        return None
    # Only strings can name a property; the set skips anything else (and anything unhashable).
    requiredNames = {entry for entry in required if isinstance(entry, str)} if isinstance(required, list) else set()
    fields: list[str] = []
    # `Object.entries(properties)`: integer-like names first, ascending; an array's entries are its indexes.
    for name, field_ in objectEntries(properties) if isinstance(properties, dict) else ((str(index), item) for index, item in enumerate(properties)):
        nested = describeFields(field_, depth - 1) if depth > 0 else None
        fields.append(f"{name}{'' if name in requiredNames else '?'}{f': {nested}' if nested else ''}")
    return f"{{ {', '.join(fields)} }}"


def describeOutput(schema: CodemodeJsonSchema | None) -> str:
    """What a script call resolves to, in one line: ``a string``, the field names of an object (see
    ``describeFields``), or the rendered type for anything else."""
    type = renderToolOutputType(schema)
    if type == "string":
        return "a string"
    fields = describeFields(schema, 1)
    return f"`{fields if fields is not None else jsCollapseWhitespace(type)}`"


def describeScriptCall(tool: Any) -> str:
    """A declared tool's description followed by how scripts call it and what the call resolves to.
    The arguments are the tool's declared parameters, so they are not repeated."""
    return (
        f"{jsTrim(read_field(tool, 'description'))}\n\nCodemode: `tools.{toCodemodeIdentifier(read_field(tool, 'name'))}(args)` "
        f"resolves to {describeOutput(toCodemodeDeclaration(tool)['outputSchema'])}."
    )


def prepareCodemodeLoadout(loadout: ToolLoadout, options: CodemodeToolOptions) -> ToolLoadoutChanges:
    """How the codemode tool presents tools that are both declared and callable from scripts:
    - ``on``: their descriptions say how scripts call them, and the codemode description lists only
      the callable tools without ``direct`` exposure.
    - ``only``: the codemode description lists every callable tool, and requests leave out the
      declarations of active ``direct`` tools.

    Listed tools carry their prompt guidelines, which the system prompt only has for declared tools.

    Listing by exposure, not by the active set, keeps the codemode description unchanged when
    ``tool_search`` loads a tool, so loads do not redeclare codemode."""
    mode = nullishCoalesce(
        options.getMode() if options.getMode is not None else None, SETTINGS_DEFAULTS["codemode"]["mode"]
    )

    def isDirect(tool: Any) -> bool:
        return loadout.getExposure(tool.name) == "direct"

    callable = getCodemodeCallableTools(loadout.callable)
    callableNames = {tool.name for tool in callable}
    descriptions: dict[str, str] = {}
    if mode == "on":
        for tool in loadout.declared:
            if tool.name in callableNames:
                descriptions[tool.name] = describeScriptCall(tool)
    listed = callable if mode == "only" else [tool for tool in callable if not isDirect(tool)]
    namespaces: dict[str, ToolNamespace] = {}
    for tool in listed:
        namespace = loadout.getNamespace(tool.name)
        if namespace:
            namespaces[tool.name] = namespace
    guidelines = {tool.name: loadout.getPromptGuidelines(tool.name) for tool in listed}
    inlineBudget = options.getInlineBudget() if options.getInlineBudget is not None else None
    descriptions[CODEMODE_TOOL_NAME] = createCodemodeDescription(listed, {
        "models": options.models is True,
        "namespaces": namespaces,
        "guidelines": guidelines,
        "deferred": {tool.name for tool in listed if loadout.getExposure(tool.name) == "deferred"},
        "inlineBudget": inlineBudget if inlineBudget is not None else DEFAULT_CODEMODE_INLINE_BUDGET,
    })
    declaredNames = {tool.name for tool in loadout.declared}
    return {
        "descriptions": descriptions,
        "hiddenDeclarations": (
            [tool.name for tool in callable if isDirect(tool) and tool.name in declaredNames] if mode == "only" else []
        ),
    }


# Script globals that list or look up tools and namespaces, which the script may then call by any name.
# `re.ASCII`: JavaScript's `\b` and `\w` are ASCII-only.
DISCOVERY_GLOBALS = re.compile(r"\b(searchTools|describeNamespace|describeTool|ALL_TOOLS)\b", re.ASCII)


def getScriptReach(code: Any) -> Literal["all"] | list[str]:
    """The tools a script may call (``ToolDefinition.reachesTools``): every tool when it uses a discovery
    global, else the words of its source, so ``tools.mcp__docs__search`` and
    ``tools["mcp__docs__" + name]`` both name the ``mcp__docs`` namespace."""
    if not isinstance(code, str):
        return []
    return "all" if DISCOVERY_GLOBALS.search(code) else re.findall(r"\w+", code, re.ASCII)


def createCodemodeToolDefinition(options: CodemodeToolOptions | None = None) -> ToolDefinition[CodemodeToolInput, CodemodeToolDetails | None]:
    options = options or CodemodeToolOptions()

    async def execute(toolCallId: str, params: Any, signal: Any = None, onUpdate: Any = None, ctx: Any = None) -> AgentToolResult:
        # The sandbox (the engine, the prelude) loads on the first call, not at startup.
        executor = await loadCodemodeExecutor()
        return await executor.executeCodemode(toolCallId, params, signal, onUpdate, ctx, options)

    return ToolDefinition(
        name=CODEMODE_TOOL_NAME,
        label=CODEMODE_TOOL_NAME,
        # Replaced with the declarations of the callable tools when the tool is activated.
        description=createCodemodeDescription([], {"models": options.models is True}),
        promptSnippet=codemodeToolSystemPromptContribution["snippet"],
        promptGuidelines=list(codemodeToolSystemPromptContribution["guidelines"]),
        parameters=codemodeSchema,
        # Scripts must not start other scripts.
        exposure="model-only",
        prepareLoadout=lambda loadout: prepareCodemodeLoadout(loadout, options),
        reachesTools=lambda input: getScriptReach(read_field(input, "code")),
        # Capable models write the script as raw text instead of a JSON-escaped string.
        constrainedSampling=GrammarConstrainedSamplingConfig(variants={"openai_lark": CODEMODE_SOURCE_GRAMMAR}),
        execute=execute,
        **codemodeRenderers,
    )


def createCodemodeTool(tools: Sequence[Any] = (), options: CodemodeToolOptions | None = None) -> AgentTool:
    """Create the codemode tool as an AgentTool. The description lists the given tools; the script can
    call whatever tools the agent loop provides at execution time."""
    options = options or CodemodeToolOptions()
    definition = createCodemodeToolDefinition(options)
    tool = wrapToolDefinition(definition)
    object.__setattr__(tool, "description", createCodemodeDescription(tools, {"models": options.models is True}))
    object.__setattr__(tool, "promptSnippet", definition.promptSnippet)
    object.__setattr__(tool, "promptGuidelines", definition.promptGuidelines)
    return tool


__all__ = [
    "CODEMODE_DOCS_PATH",
    "CODEMODE_STORE_ENTRY_TYPE",
    "CODEMODE_TOOL_NAME",
    "DEFAULT_CODEMODE_INLINE_BUDGET",
    "CodemodeDescriptionOptions",
    "CodemodeModelRuntime",
    "CodemodeNestedCall",
    "CodemodeNestedCallStatus",
    "CodemodeStoreEntryData",
    "CodemodeToolDetails",
    "CodemodeToolInput",
    "CodemodeToolOptions",
    "codemodeSchema",
    "codemodeToolSystemPromptContribution",
    "createCodemodeDescription",
    "createCodemodeTool",
    "createCodemodeToolDefinition",
    "getCodemodeCallableTools",
    "getScriptReach",
    "toCodemodeDeclaration",
]

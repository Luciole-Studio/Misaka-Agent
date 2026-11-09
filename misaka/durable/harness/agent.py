"""The agent of a conversation: its stored state, settings resolution, and extension, tool and section selection.

Translated from pi's ``packages/durable/src/harness/agent.ts`` (v1.3.0). The agent state, a change, the settings and a resolved
agent are plain ``dict`` objects with the upstream keys; an absent key is ``undefined`` and ``None`` in a change clears
the stored field.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from misaka.chord.delta.tracker import DraftArray
from misaka.chord.json import copyJson
from misaka.utils.js import UNDEFINED, nullishCoalesce

from ..documents import defineDoc
from ..session.session import documentRetention
from ..types import ConversationId, ConversationRecord, Tx

DEFAULT_RETRY_POLICY: dict[str, Any] = {
    "enabled": True,
    "maxRetries": 3,
    "baseDelayMs": 2000,
    "maxAgentDelayMs": 60000,
}

DEFAULT_COMPACTION_POLICY: dict[str, Any] = {
    "enabled": True,
    "reserveTokens": 16384,
    "keepRecentTokens": 20000,
    "backgroundTokens": 32768,
}

DEFAULT_PROGRESS_POLICY: dict[str, Any] = {
    "partialIntervalMs": 100,
    "outputIntervalMs": 100,
}

INSTRUCTIONS_KEY = "instructions"
"""The reserved section key of the agent's ``instructions``."""

AgentDoc = defineDoc(
    {
        "kind": "pi.agent",
        "version": 1,
        "scope": "conversation",
        "history": "rewindable",
        "fork": "asOf",
        "initial": dict,
        "checkpointWhen": lambda *_: True,
    }
)
"""Built-in agent document; rewindable so forks start from the agent at their fork entry."""


DEFAULT_CONTEXT_RETENTION_MS = 600_000
"""Default ``settings.contextRetentionMs``: ten minutes."""


def resolveSettings(settings: dict[str, Any] | None) -> dict[str, Any]:
    """Resolve the host settings: every field over its built-in default, object fields merged."""
    settings = settings if settings is not None else {}
    extensions = settings.get("extensions")
    progress = settings.get("progress") or {}
    return {
        **({} if extensions is None else {"extensions": extensions}),
        "stream": {**(settings.get("stream") or {})},
        "retry": {**DEFAULT_RETRY_POLICY, **(settings.get("retry") or {})},
        "compaction": {**DEFAULT_COMPACTION_POLICY, **(settings.get("compaction") or {})},
        # Field by field, so an explicitly undefined interval keeps its default.
        "progress": {
            "partialIntervalMs": nullishCoalesce(
                progress.get("partialIntervalMs"), DEFAULT_PROGRESS_POLICY["partialIntervalMs"]
            ),
            "outputIntervalMs": nullishCoalesce(progress.get("outputIntervalMs"), DEFAULT_PROGRESS_POLICY["outputIntervalMs"]),
        },
        "toolExecution": settings["toolExecution"] if settings.get("toolExecution") is not None else "parallel",
        "steeringMode": settings["steeringMode"] if settings.get("steeringMode") is not None else "one-at-a-time",
        "followUpMode": settings["followUpMode"] if settings.get("followUpMode") is not None else "one-at-a-time",
        "contextRetentionMs": nullishCoalesce(settings.get("contextRetentionMs"), DEFAULT_CONTEXT_RETENTION_MS),
        "documentRetentionMs": documentRetention(settings.get("documentRetentionMs")),
    }


async def configure(tx: Tx, conversationId: ConversationId, change: dict[str, Any]) -> None:
    """Apply one change to ``pi.agent``: a given field replaces the stored one, ``None`` clears it, an absent field
    changes nothing."""
    state = await tx.doc(AgentDoc, conversationId)
    applyChange(state, change)


async def addTools(tx: Tx, conversationId: ConversationId, added: list[str]) -> None:
    """``addTools`` of a tool round, applied to ``tools`` and ``modelTools``, so the next request offers the tools: an
    array gets each name it lacks appended, and ``{ remove }`` loses the names. An unset ``tools`` already enables every
    tool, so nothing is written for it. An unset or object ``modelTools`` also gets each name appended to its ``add``,
    which loads deferred tools and has no effect for others. By name, without the registry: a tool that is not
    registered now, such as one of an MCP server that is reconnecting, is offered once it is again."""
    state = await tx.doc(AgentDoc, conversationId)
    tools = state["tools"]
    if isinstance(tools, DraftArray):
        for name in added:
            if tools.indexOf(name) < 0:
                tools.push(name)
    elif tools is not UNDEFINED and any(name in added for name in tools["remove"]):
        state["tools"] = {"remove": [name for name in tools["remove"] if name not in added]}
    modelTools = state["modelTools"]
    if isinstance(modelTools, DraftArray):
        for name in added:
            if modelTools.indexOf(name) < 0:
                modelTools.push(name)
        return
    # Assigned whole; a document write of an equal value records no change.
    stored: Any = {} if modelTools is UNDEFINED else modelTools
    remove = [name for name in stored.get("remove", None) or [] if name not in added]
    add = list(stored.get("add", None) or [])
    for name in added:
        if name not in add:
            add.append(name)
    state["modelTools"] = {**({} if len(remove) == 0 else {"remove": remove}), "add": add}


def applyChange(state: Any, change: dict[str, Any]) -> None:
    def set(key: str, value: Any) -> None:
        if value is UNDEFINED:
            return
        if value is None:
            if key in state:
                del state[key]
        else:
            state[key] = value

    model = change.get("model", UNDEFINED)
    set("model", model if model is UNDEFINED or model is None else {**model})
    set("thinkingLevel", change.get("thinkingLevel", UNDEFINED))
    extensions = change.get("extensions", UNDEFINED)
    if extensions is UNDEFINED or extensions is None:
        set("extensions", extensions)
    elif isList(extensions):
        set("extensions", names(extensions))
    else:
        set(
            "extensions",
            {
                **({} if extensions.get("add") is None else {"add": names(extensions["add"])}),
                **({} if extensions.get("remove") is None else {"remove": names(extensions["remove"])}),
            },
        )
    tools = change.get("tools", UNDEFINED)
    if tools is UNDEFINED or tools is None:
        set("tools", tools)
    elif isList(tools):
        set("tools", names(tools))
    else:
        set("tools", {"remove": names(tools["remove"])})
    modelTools = change.get("modelTools", UNDEFINED)
    if modelTools is UNDEFINED or modelTools is None:
        set("modelTools", modelTools)
    elif isList(modelTools):
        set("modelTools", names(modelTools))
    else:
        set(
            "modelTools",
            {
                **({} if modelTools.get("remove", UNDEFINED) is UNDEFINED else {"remove": names(modelTools["remove"])}),
                **({} if modelTools.get("add", UNDEFINED) is UNDEFINED else {"add": names(modelTools["add"])}),
            },
        )
    set("instructions", change.get("instructions", UNDEFINED))
    set("cwd", change.get("cwd", UNDEFINED))


def isList(value: Any) -> bool:
    return isinstance(value, list | tuple)


def names(items: Any) -> list[str]:
    return [item["name"] for item in items]


async def createAgent(tx: Tx, conversation: ConversationRecord) -> None:
    """Built-in part of every Harness commit that creates or forks a conversation, for ``pi.agent``: a fork keeps its
    ``asOf`` copy; a new task-owned conversation copies the stored agent of its owner task's conversation; a new
    ownerless one starts empty."""
    if conversation.get("parent") is not None:
        return
    agent = await tx.doc(AgentDoc, conversation["id"])
    if conversation.get("owner") is None:
        return
    owner = await tx.doc(AgentDoc, conversation["owner"]["conversationId"])
    copied: dict[str, Any] = copyJson(owner)  # type: ignore[assignment]
    for key, value in copied.items():
        agent[key] = value


def agentHooks(agent: dict[str, Any], taskName: str) -> list[Any]:
    """Handlers of the selected extensions' hooks for a task name, in extension order."""
    handlers: list[Any] = []
    for extension in agent["extensions"]:
        for hook in extension.get("hooks") or []:
            if hook["task"] == taskName:
                handlers.append(hook["handlers"])
    return handlers


def resolveAgent(
    state: dict[str, Any] | None, snapshot: Any, settings: dict[str, Any], report: Callable[[Any], None]
) -> dict[str, Any]:
    """Resolve an agent from its stored state (absent: every field unset), a registry snapshot, and resolved settings.
    A wrapper that throws or renames drops its target and is reported; a wrapper without a target does nothing."""
    state = state if state is not None else {}
    extensions = selectExtensions(state.get("extensions"), snapshot, settings)

    composed: dict[str, Any] = {}
    for extension in extensions:
        for tool in extension.get("tools") or []:
            composed[tool["name"]] = tool
    sections: dict[str, Any] = {}
    for extension in extensions:
        for section in extension.get("sections") or []:
            sections[section["key"]] = section
    for extension in extensions:
        for wrap in extension.get("wraps") or []:
            if "tool" in wrap:
                applyWrap(composed, wrap["tool"], wrap["wrap"], lambda tool: tool["name"], report)
            else:
                applyWrap(sections, wrap["section"], wrap["wrap"], lambda section: section["key"], report)

    enabled = filterTools(list(composed.values()), state.get("tools"))

    def callableBy(caller: str) -> Callable[[Any], bool]:
        return lambda tool: tool.get("callers") is None or caller in tool["callers"]

    modelCallable = [tool for tool in enabled if callableBy("model")(tool)]
    tools = offeredTools(modelCallable, state.get("modelTools"))
    offered = {id(tool) for tool in tools}
    deferred = [tool for tool in modelCallable if tool.get("deferred") is True and id(tool) not in offered]
    callable = [tool for tool in enabled if callableBy("tools")(tool)]

    instructions = state.get("instructions")
    agentSections = list(sections.values())
    if instructions is not None:
        agentSections.append({"key": INSTRUCTIONS_KEY, "render": lambda *_: instructions})

    return {
        **({} if state.get("model") is None else {"model": state["model"]}),
        "thinkingLevel": state["thinkingLevel"] if state.get("thinkingLevel") is not None else "off",
        "extensions": extensions,
        "tools": tools,
        "callable": callable,
        "deferred": deferred,
        "sections": agentSections,
        **({} if instructions is None else {"instructions": instructions}),
        **({} if state.get("cwd") is None else {"cwd": state["cwd"]}),
    }


def offeredTools(tools: list[Any], filter: Any) -> list[Any]:
    """The tools offered to the model, as ``AgentState.modelTools`` describes."""
    if isinstance(filter, list):
        return filterTools(tools, filter)
    removed = set((filter or {}).get("remove") or [])
    kept = [tool for tool in tools if tool.get("deferred") is not True and tool["name"] not in removed]
    # Loaded deferred tools follow in load order, so loading one appends to the declared tools.
    byName = {tool["name"]: tool for tool in tools}
    loaded: list[Any] = []
    for name in dict.fromkeys((filter or {}).get("add") or []):
        tool = byName.get(name)
        if tool is not None and tool.get("deferred") is True and name not in removed:
            loaded.append(tool)
    return [*kept, *loaded]


def filterTools(tools: list[Any], filter: Any) -> list[Any]:
    """``tools`` through a stored filter: an array selects exactly its names, in its order; ``{ remove }`` drops
    names."""
    if filter is None:
        return tools
    if isinstance(filter, list):
        byName = {tool["name"]: tool for tool in tools}
        return [byName[name] for name in dict.fromkeys(filter) if name in byName]
    removed = set(filter["remove"])
    return [tool for tool in tools if tool["name"] not in removed]


def selectExtensions(stored: Any, snapshot: Any, settings: dict[str, Any]) -> list[Any]:
    """Selected installed extensions: the stored array, or the default selection edited by ``{ add, remove }``."""
    selected: list[str]
    if isinstance(stored, list):
        selected = stored
    else:
        if settings.get("extensions") is not None:
            base = [extension["name"] for extension in settings["extensions"]]
        else:
            base = [extension["name"] for extension in snapshot.installed()]
        edit = stored if stored is not None else {}
        removed = set(edit.get("remove") or [])
        selected = [name for name in [*base, *(edit.get("add") or [])] if name not in removed]
    extensions: list[Any] = []
    for name in dict.fromkeys(selected):
        extension = snapshot.extension(name)
        if extension is not None:
            extensions.append(extension)
    return extensions


def applyWrap(
    items: dict[str, Any],
    target: str,
    wrap: Callable[[Any], Any],
    nameOf: Callable[[Any], str],
    report: Callable[[Any], None],
) -> None:
    item = items.get(target)
    if item is None:
        return
    try:
        wrapped = wrap(item)
        if nameOf(wrapped) != target:
            raise RuntimeError(f"Wrapper renamed {target} to {nameOf(wrapped)}")
        items[target] = wrapped
    except Exception as error:  # noqa: BLE001 - a failing wrapper drops its target and is reported
        del items[target]
        report(error)


__all__ = [
    "DEFAULT_COMPACTION_POLICY",
    "DEFAULT_RETRY_POLICY",
    "INSTRUCTIONS_KEY",
    "AgentDoc",
    "addTools",
    "agentHooks",
    "applyChange",
    "configure",
    "createAgent",
    "filterTools",
    "offeredTools",
    "resolveAgent",
    "resolveSettings",
]

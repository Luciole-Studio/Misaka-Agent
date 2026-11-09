"""Translated from pi's ``packages/ai/src/utils/transcript.ts`` (v1.1.0).

MISAKA fork: messages and tools may be plain mappings as well as models (hand-built histories, extensions, durable
records); an already-normalized ``TranscriptContext`` passes ``normalizeContext`` unchanged (the class is the brand);
a tool whose ``parameters`` is a pydantic class is declared with its JSON schema.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from misaka.ai.types import (
    Context,
    SystemMessage,
    Tool,
    ToolReference,
    TranscriptContext,
)
from misaka.ai.utils.text import contentText, getSystemMessageText
from misaka.utils.js import jsonParse, jsonStringify


def createInitialSystemMessage(systemPrompt: str | None, tools: Sequence[Tool] | None) -> SystemMessage | None:
    """Build the leading system message for a prompt and tool set. Returns undefined when
    both are empty, so an empty transcript stays empty."""
    hasSystemPrompt = systemPrompt is not None and len(systemPrompt) > 0
    hasTools = tools is not None and len(tools) > 0
    if not hasSystemPrompt and not hasTools:
        return None
    return SystemMessage(
        content=systemPrompt if systemPrompt is not None else "",
        toolsAdded=list(tools) if hasTools else None,  # type: ignore[arg-type]
        timestamp=0,
    )


def normalizeContext(context: Context | TranscriptContext | Mapping[str, Any]) -> TranscriptContext:
    """Fold `Context.systemPrompt` and `Context.tools` into a leading system message.
    This is the only entry point that produces a `TranscriptContext`; every
    provider-facing function expects the result."""
    if isinstance(context, TranscriptContext):
        return context
    initialMessage = createInitialSystemMessage(_field(context, "systemPrompt"), _field(context, "tools"))
    messages = list(_field(context, "messages") or [])
    return TranscriptContext(messages=[initialMessage, *messages] if initialMessage else messages)


# Any message list. The replay helpers only read entries whose role is `"system"`, so
# agent transcripts that carry custom message roles can be passed without filtering.
type TranscriptMessages = Sequence[Any]


def _isSystemMessage(message: Any) -> bool:
    return _field(message, "role") == "system"


def getInitialSystemMessage(messages: TranscriptMessages) -> Any | None:
    """Return the leading system message, if the transcript starts with one."""
    first = messages[0] if messages else None
    return first if first is not None and _isSystemMessage(first) else None


def withoutInitialSystemMessage(messages: Sequence[Any]) -> list[Any]:
    """Drop the leading system message for APIs that carry the prompt outside the message list."""
    return list(messages[1:]) if getInitialSystemMessage(messages) else list(messages)


def getCurrentTools(messages: TranscriptMessages) -> list[Tool]:
    """Resolve the tools available after applying every transcript delta in order."""
    tools: dict[str, Tool] = {}
    for message in messages:
        if not _isSystemMessage(message):
            continue
        for tool in _field(message, "toolsRemoved") or []:
            tools.pop(_field(tool, "name"), None)
        for tool in _field(message, "toolsAdded") or []:
            tools[_field(tool, "name")] = tool
    return list(tools.values())


def getCurrentSystemMessage(messages: TranscriptMessages) -> SystemMessage | None:
    """Replay every system message into one leading system message holding the current
    prompt and tools. Later `content` is appended to the base prompt, `sections` are
    patched by name, and tools are resolved with `getCurrentTools`."""
    content: list[str] = []
    sections: dict[str, str] = {}
    timestamp: int | None = None
    for message in messages:
        if not _isSystemMessage(message):
            continue
        if timestamp is None:
            timestamp = _field(message, "timestamp")
        text = contentText(_field(message, "content"))
        if len(text) > 0:
            content.append(text)
        for name, value in (_field(message, "sections") or {}).items():
            if value is None:
                sections.pop(name, None)
            else:
                sections[name] = value
    tools = getCurrentTools(messages)
    if timestamp is None and len(tools) == 0:
        return None
    return SystemMessage(
        content="\n\n".join(content),
        sections=dict(sections) if len(sections) > 0 else None,
        toolsAdded=tools if len(tools) > 0 else None,
        timestamp=timestamp if timestamp is not None else 0,
    )


def getCurrentSystemPrompt(messages: TranscriptMessages) -> str:
    """Render the current system prompt text after replaying every system message."""
    message = getCurrentSystemMessage(messages)
    return getSystemMessageText(message) if message else ""


def collapseSystemMessages(context: TranscriptContext) -> TranscriptContext:
    """Rebuild the transcript for APIs without mid-conversation system messages: the replayed
    system message leads, and every later system message is dropped."""
    head = getCurrentSystemMessage(context.messages)
    messages = [message for message in context.messages if _field(message, "role") != "system"]
    return TranscriptContext(messages=[head, *messages] if head else messages)


def resolveTranscript(context: TranscriptContext, supportsMidConvoSystemMessages: bool | None) -> TranscriptContext:
    """Keep later system messages in place when the model accepts them; otherwise collapse them."""
    return context if supportsMidConvoSystemMessages else collapseSystemMessages(context)


def toToolDeclaration(tool: Any) -> Tool:
    """Strip executable and display-only fields from a tool before transcript comparison or persistence."""
    constrainedSampling = _field(tool, "constrainedSampling")
    return Tool(
        name=_field(tool, "name"),
        description=_field(tool, "description"),
        parameters=jsonParse(jsonStringify(_parameters_json_schema(tool))),
        **({} if constrainedSampling is None else {"constrainedSampling": constrainedSampling}),
    )


def declarationsEqual(left: Any, right: Any) -> bool:
    """Whether two tools declare the same interface to the model.

    Both sides go through `toToolDeclaration` first: its JSON round-trip drops the
    typebox symbol keys and `undefined` fields that a structural comparison would see, and
    builds both objects with the same key order, so comparing the serialized declarations
    is exact. This avoids a deep-equal dependency in a browser-safe package.
    """
    return _serialize(toToolDeclaration(left)) == _serialize(toToolDeclaration(right))


@dataclass(slots=True)
class ToolStateChanges:
    toolsAdded: list[Tool]
    toolsRemoved: list[ToolReference]


def getToolStateChanges(previous: Sequence[Any], current: Sequence[Any]) -> ToolStateChanges:
    """Compare two complete tool states. A changed definition is a removal followed by an addition."""
    previousTools = {_field(tool, "name"): tool for tool in previous}
    currentTools = {_field(tool, "name"): tool for tool in current}

    def added(tool: Any) -> bool:
        previousTool = previousTools.get(_field(tool, "name"))
        return previousTool is None or not declarationsEqual(previousTool, tool)

    def removed(tool: Any) -> bool:
        currentTool = currentTools.get(_field(tool, "name"))
        return currentTool is None or not declarationsEqual(tool, currentTool)

    return ToolStateChanges(
        toolsAdded=[toToolDeclaration(tool) for tool in current if added(tool)],
        toolsRemoved=[ToolReference(name=_field(tool, "name")) for tool in previous if removed(tool)],
    )


def getDeclaredTools(messages: TranscriptMessages) -> list[Tool]:
    """Every definition referenced by transcript tool state, in first-declaration order."""
    definitions: dict[str, Tool] = {}
    for message in messages:
        if not _isSystemMessage(message):
            continue
        for tool in _field(message, "toolsAdded") or []:
            definitions[_field(tool, "name")] = tool
    return list(definitions.values())


def hasToolRedefinitions(messages: TranscriptMessages) -> bool:
    """Whether a tool name was declared twice with different definitions. A transport that can
    only reference previously declared tools by name cannot replay such a history.

    Deprecated: no built-in transport needs this anymore: Anthropic expresses redefinitions with
    inline `tool_definition` blocks. Kept for API compatibility and will be removed in a future release.
    """
    declared: dict[str, Any] = {}
    for message in messages:
        if not _isSystemMessage(message):
            continue
        for tool in _field(message, "toolsAdded") or []:
            name = _field(tool, "name")
            previous = declared.get(name)
            if previous is not None and not declarationsEqual(previous, tool):
                return True
            declared[name] = tool
    return False


def hasNonAdditiveToolChanges(messages: TranscriptMessages) -> bool:
    """Whether tool history contains a removal or same-name redeclaration that an addition-only transport cannot replay."""
    declared: set[str] = set()
    for message in messages:
        if not _isSystemMessage(message):
            continue
        if len(_field(message, "toolsRemoved") or []) > 0:
            return True
        for tool in _field(message, "toolsAdded") or []:
            name = _field(tool, "name")
            if name in declared:
                return True
            declared.add(name)
    return False


@dataclass(slots=True)
class TranscriptTools:
    # Tools sent in the top-level request field.
    requestTools: list[Tool]
    # Whether later system messages carry their own `toolsAdded` as in-place additions.
    # When false, `requestTools` already holds the complete current tool set.
    anchorsAdditions: bool


def resolveTranscriptTools(messages: TranscriptMessages, supportsToolAdditions: bool | None) -> TranscriptTools:
    """Split tool declarations between the top-level request field and in-place additions.
    Transports that can anchor additions at a system message keep the initial tools at the
    top and load later ones where they appear; that only works when no tool was removed or
    redeclared, so everything else sends the current tool list."""
    anchorsAdditions = bool(supportsToolAdditions) and not hasNonAdditiveToolChanges(messages)
    initial = getInitialSystemMessage(messages)
    return TranscriptTools(
        requestTools=(
            list((_field(initial, "toolsAdded") if initial else None) or [])
            if anchorsAdditions
            else getCurrentTools(messages)
        ),
        anchorsAdditions=anchorsAdditions,
    )


def _parameters_json_schema(tool: Any) -> Any:
    """MISAKA fork: a pydantic `parameters` class as its JSON schema."""
    schema = getattr(tool, "parameters_json_schema", None)
    if callable(schema):
        return schema()
    return _field(tool, "parameters")


def _serialize(tool: Tool) -> str:
    """MISAKA fork: ``JSON.stringify`` of a declaration; 0.0 and 0 print as one value, as a declaration read back from
    storage has it."""
    return jsonStringify(tool.model_dump(exclude_none=True))


def _field(value: Any, name: str) -> Any:
    """MISAKA fork: messages and tools arrive as models from the pipeline and as plain mappings from hand-built
    histories and extensions."""
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


__all__ = [
    "ToolStateChanges",
    "TranscriptContext",
    "TranscriptMessages",
    "TranscriptTools",
    "collapseSystemMessages",
    "createInitialSystemMessage",
    "declarationsEqual",
    "getCurrentSystemMessage",
    "getCurrentSystemPrompt",
    "getCurrentTools",
    "getDeclaredTools",
    "getInitialSystemMessage",
    "getToolStateChanges",
    "hasNonAdditiveToolChanges",
    "hasToolRedefinitions",
    "normalizeContext",
    "resolveTranscript",
    "resolveTranscriptTools",
    "toToolDeclaration",
    "withoutInitialSystemMessage",
]

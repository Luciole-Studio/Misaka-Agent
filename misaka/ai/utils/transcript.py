"""Transcript-carried system prompt and tool declarations, translated from pi's
``utils/transcript.ts``.

The prompt and the tool set are not request fields: they are system messages in the
transcript. The leading one holds the base prompt and the initial tools, later ones change
them, and replaying every system message in order yields the current state.
"""

from __future__ import annotations

import json
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
from misaka.ai.utils.text import content_text, get_system_message_text


def create_initial_system_message(system_prompt: str | None, tools: Sequence[Tool] | None) -> SystemMessage | None:
    """Build the leading system message for a prompt and tool set. Returns None when
    both are empty, so an empty transcript stays empty."""
    has_system_prompt = system_prompt is not None and len(system_prompt) > 0
    has_tools = tools is not None and len(tools) > 0
    if not has_system_prompt and not has_tools:
        return None
    return SystemMessage(
        content=system_prompt if system_prompt is not None else "",
        toolsAdded=list(tools) if has_tools else None,
        timestamp=0,
    )


def normalize_context(context: Context | TranscriptContext | Mapping[str, Any]) -> TranscriptContext:
    """Fold ``Context.systemPrompt`` and ``Context.tools`` into a leading system message.
    This is the only entry point that produces a :class:`TranscriptContext`; every
    provider-facing function expects the result."""
    if isinstance(context, TranscriptContext):
        # pi's branded type makes a second normalization a no-op at the type level; here the
        # class is the brand, so an already-normalized context passes through unchanged.
        return context
    system_prompt = _field(context, "systemPrompt")
    tools = _field(context, "tools")
    messages = list(_field(context, "messages") or [])
    initial_message = create_initial_system_message(system_prompt, tools)
    return TranscriptContext(messages=[initial_message, *messages] if initial_message else messages)


def is_system_message(message: Any) -> bool:
    return _field(message, "role") == "system"


def get_initial_system_message(messages: Sequence[Any]) -> Any | None:
    """Return the leading system message, if the transcript starts with one."""
    first = messages[0] if messages else None
    return first if first is not None and is_system_message(first) else None


def without_initial_system_message(messages: Sequence[Any]) -> list[Any]:
    """Drop the leading system message for APIs that carry the prompt outside the message list."""
    return list(messages[1:]) if get_initial_system_message(messages) else list(messages)


def get_current_tools(messages: Sequence[Any]) -> list[Tool]:
    """Resolve the tools available after applying every transcript delta in order."""
    tools: dict[str, Tool] = {}
    for message in messages:
        if not is_system_message(message):
            continue
        for tool in _field(message, "toolsRemoved") or []:
            tools.pop(_field(tool, "name"), None)
        for tool in _field(message, "toolsAdded") or []:
            name = _field(tool, "name")
            # A dict keeps insertion order, but re-setting an existing key keeps the old
            # position; JS `Map.set` does the same, so no reordering is needed here.
            tools[name] = tool
    return list(tools.values())


def get_current_system_message(messages: Sequence[Any]) -> SystemMessage | None:
    """Replay every system message into one leading system message holding the current
    prompt and tools. Later ``content`` is appended to the base prompt, ``sections`` are
    patched by name, and tools are resolved with :func:`get_current_tools`."""
    content: list[str] = []
    sections: dict[str, str] = {}
    timestamp: int | None = None
    for message in messages:
        if not is_system_message(message):
            continue
        if timestamp is None:
            timestamp = _field(message, "timestamp")
        text = content_text(_field(message, "content"))
        if len(text) > 0:
            content.append(text)
        for name, value in (_field(message, "sections") or {}).items():
            if value is None:
                sections.pop(name, None)
            else:
                sections[name] = value
    tools = get_current_tools(messages)
    if timestamp is None and len(tools) == 0:
        return None
    return SystemMessage(
        content="\n\n".join(content),
        sections=dict(sections) if len(sections) > 0 else None,
        toolsAdded=tools if len(tools) > 0 else None,
        timestamp=timestamp if timestamp is not None else 0,
    )


def get_current_system_prompt(messages: Sequence[Any]) -> str:
    """Render the current system prompt text after replaying every system message."""
    message = get_current_system_message(messages)
    return get_system_message_text(message) if message else ""


def collapse_system_messages(context: TranscriptContext) -> TranscriptContext:
    """Rebuild the transcript for APIs without mid-conversation system messages: the replayed
    system message leads, and every later system message is dropped."""
    head = get_current_system_message(context.messages)
    messages = [message for message in context.messages if _field(message, "role") != "system"]
    return TranscriptContext(messages=[head, *messages] if head else messages)


def resolve_transcript(context: TranscriptContext, supports_mid_convo_system_messages: bool | None) -> TranscriptContext:
    """Keep later system messages in place when the model accepts them; otherwise collapse them."""
    return context if supports_mid_convo_system_messages else collapse_system_messages(context)


def to_tool_declaration(tool: Any) -> Tool:
    """Strip executable and display-only fields from a tool before transcript comparison or persistence."""
    constrained_sampling = _field(tool, "constrainedSampling")
    return Tool(
        name=_field(tool, "name"),
        description=_field(tool, "description"),
        parameters=json.loads(json.dumps(_parameters_json_schema(tool))),
        **({} if constrained_sampling is None else {"constrainedSampling": constrained_sampling}),
    )


def declarations_equal(left: Any, right: Any) -> bool:
    """Whether two tools declare the same interface to the model.

    Both sides go through :func:`to_tool_declaration` first: its JSON round-trip drops the
    typebox symbol keys and ``undefined`` fields that a structural comparison would see, and
    builds both objects with the same key order, so comparing the serialized declarations
    is exact. This avoids a deep-equal dependency in a browser-safe package.
    """
    return _serialize(to_tool_declaration(left)) == _serialize(to_tool_declaration(right))


@dataclass(slots=True)
class ToolStateChanges:
    toolsAdded: list[Tool]
    toolsRemoved: list[ToolReference]


def get_tool_state_changes(previous: Sequence[Any], current: Sequence[Any]) -> ToolStateChanges:
    """Compare two complete tool states. A changed definition is a removal followed by an addition."""
    previous_tools = {_field(tool, "name"): tool for tool in previous}
    current_tools = {_field(tool, "name"): tool for tool in current}

    def added(tool: Any) -> bool:
        previous_tool = previous_tools.get(_field(tool, "name"))
        return previous_tool is None or not declarations_equal(previous_tool, tool)

    def removed(tool: Any) -> bool:
        current_tool = current_tools.get(_field(tool, "name"))
        return current_tool is None or not declarations_equal(tool, current_tool)

    return ToolStateChanges(
        toolsAdded=[to_tool_declaration(tool) for tool in current if added(tool)],
        toolsRemoved=[ToolReference(name=_field(tool, "name")) for tool in previous if removed(tool)],
    )


def get_declared_tools(messages: Sequence[Any]) -> list[Tool]:
    """Every definition referenced by transcript tool state, in first-declaration order."""
    definitions: dict[str, Tool] = {}
    for message in messages:
        if not is_system_message(message):
            continue
        for tool in _field(message, "toolsAdded") or []:
            definitions[_field(tool, "name")] = tool
    return list(definitions.values())


def has_tool_redefinitions(messages: Sequence[Any]) -> bool:
    """Whether a tool name was declared twice with different definitions. Transports that
    reference tools by name (Anthropic ``tool_addition``/``tool_removal``) cannot express that."""
    declared: dict[str, Any] = {}
    for message in messages:
        if not is_system_message(message):
            continue
        for tool in _field(message, "toolsAdded") or []:
            name = _field(tool, "name")
            previous = declared.get(name)
            if previous is not None and not declarations_equal(previous, tool):
                return True
            declared[name] = tool
    return False


def has_non_additive_tool_changes(messages: Sequence[Any]) -> bool:
    """Whether tool history contains a removal or same-name redeclaration that an addition-only
    transport cannot replay."""
    declared: set[str] = set()
    for message in messages:
        if not is_system_message(message):
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
    requestTools: list[Tool]
    anchorsAdditions: bool


def resolve_transcript_tools(messages: Sequence[Any], supports_tool_additions: bool | None) -> TranscriptTools:
    """Split tool declarations between the top-level request field and in-place additions.

    Transports that can anchor additions at a system message keep the initial tools at the
    top and load later ones where they appear; that only works when no tool was removed or
    redeclared, so everything else sends the current tool list.
    """
    anchors_additions = bool(supports_tool_additions) and not has_non_additive_tool_changes(messages)
    initial = get_initial_system_message(messages)
    return TranscriptTools(
        requestTools=(
            list((_field(initial, "toolsAdded") if initial else None) or [])
            if anchors_additions
            else get_current_tools(messages)
        ),
        anchorsAdditions=anchors_additions,
    )


def _parameters_json_schema(tool: Any) -> Any:
    schema = getattr(tool, "parameters_json_schema", None)
    if callable(schema):
        return schema()
    return _field(tool, "parameters")


def _serialize(tool: Tool) -> str:
    return json.dumps(tool.model_dump(exclude_none=True), sort_keys=False, separators=(",", ":"))


def _field(value: Any, name: str) -> Any:
    # Messages and tools arrive as models from the pipeline and as plain mappings from
    # hand-built histories and extensions, the same two shapes ``content_text`` accepts.
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


__all__ = [
    "ToolStateChanges",
    "TranscriptTools",
    "collapse_system_messages",
    "create_initial_system_message",
    "declarations_equal",
    "get_current_system_message",
    "get_current_system_prompt",
    "get_current_tools",
    "get_declared_tools",
    "get_initial_system_message",
    "get_tool_state_changes",
    "has_non_additive_tool_changes",
    "has_tool_redefinitions",
    "is_system_message",
    "normalize_context",
    "resolve_transcript",
    "resolve_transcript_tools",
    "to_tool_declaration",
    "without_initial_system_message",
]

"""Translated from pi's ``packages/ai/test/system-message-replay.test.ts`` (v1.1.0).

MISAKA fork: the transcript validates its messages, so the assistant turn is a complete message where upstream casts a
partial one; ``Type.Object({})`` is its JSON schema.
"""

from __future__ import annotations

from typing import Any

from misaka.ai.types import (
    AssistantMessage,
    SystemMessage,
    TextContent,
    Tool,
    ToolReference,
    TranscriptContext,
    Usage,
    UsageCost,
    UserMessage,
)
from misaka.ai.utils.text import getSystemMessageText, renderSystemMessageUpdate
from misaka.ai.utils.transcript import (
    collapseSystemMessages,
    declarationsEqual,
    getCurrentSystemMessage,
    getCurrentSystemPrompt,
    getToolStateChanges,
    hasNonAdditiveToolChanges,
    hasToolRedefinitions,
    normalizeContext,
)


def tool(name: str, description: str | None = None) -> Tool:
    return Tool(
        name=name,
        description=description if description is not None else f"{name} tool",
        parameters={"type": "object", "properties": {}},
    )


def _assistant(timestamp: int) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text="ok")],
        api="openai-responses",
        provider="openai",
        model="m",
        usage=Usage(
            input=0,
            output=0,
            cacheRead=0,
            cacheWrite=0,
            totalTokens=0,
            cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
        ),
        stopReason="stop",
        timestamp=timestamp,
    )


def _dumped(message: Any) -> Any:
    return None if message is None else message.model_dump(exclude_none=True)


transcript = normalizeContext(
    {
        "messages": [
            SystemMessage(
                content="base",
                sections={"a": "<a>1</a>", "b": "<b>1</b>"},
                toolsAdded=[tool("first")],
                timestamp=10,
            ),
            UserMessage(content="hello", timestamp=11),
            SystemMessage(content="also do this", timestamp=12),
            _assistant(13),
            SystemMessage(
                content="",
                sections={"a": "<a>2</a>", "b": None, "c": "<c>1</c>"},
                toolsRemoved=[ToolReference(name="first")],
                toolsAdded=[tool("second")],
                timestamp=14,
            ),
        ]
    }
)


def test_replays_content_sections_and_tools_into_one_leading_message():
    current = getCurrentSystemMessage(transcript.messages)
    assert _dumped(current) == _dumped(
        SystemMessage(
            content="base\n\nalso do this",
            sections={"a": "<a>2</a>", "c": "<c>1</c>"},
            toolsAdded=[tool("second")],
            timestamp=10,
        )
    )
    assert getCurrentSystemPrompt(transcript.messages) == "base\n\nalso do this\n\n<a>2</a>\n\n<c>1</c>"


def test_collapse_keeps_only_non_system_messages_after_the_replayed_head():
    collapsed = collapseSystemMessages(transcript)
    assert [message.role for message in collapsed.messages] == ["system", "user", "assistant"]
    assert collapseSystemMessages(collapsed) == collapsed


def test_replay_of_a_transcript_without_system_messages_is_empty():
    context = normalizeContext({"messages": [UserMessage(content="hi", timestamp=1)]})
    assert getCurrentSystemMessage(context.messages) is None
    assert getCurrentSystemPrompt(context.messages) == ""
    assert collapseSystemMessages(context).messages == context.messages


def test_a_late_full_patch_on_a_transcript_without_a_leading_message_replays_as_the_prompt():
    context: TranscriptContext = normalizeContext(
        {
            "messages": [
                UserMessage(content="old session", timestamp=1),
                SystemMessage(content="", sections={"preamble": "You are pi."}, toolsAdded=[tool("x")], timestamp=2),
            ]
        }
    )
    assert getCurrentSystemPrompt(context.messages) == "You are pi."
    head = collapseSystemMessages(context).messages[0]
    assert head.role == "system"
    assert head.toolsAdded == [tool("x")]


def test_renders_complete_prompts_and_framed_updates():
    leading = transcript.messages[0]
    update = transcript.messages[4]
    if leading.role != "system" or update.role != "system":
        raise RuntimeError("expected system messages")
    assert getSystemMessageText(leading) == "base\n\n<a>1</a>\n\n<b>1</b>"
    assert renderSystemMessageUpdate(update) == "\n\n".join(  # noqa: FLY002
        [
            'Updated system prompt section "a":\n\n<a>2</a>',
            'Removed system prompt section "b".',
            'Updated system prompt section "c":\n\n<c>1</c>',
        ]
    )


def test_normalizes_the_legacy_prompt_and_tool_fields_into_a_leading_system_message():
    messages = [UserMessage(content="hi", timestamp=1)]
    assert normalizeContext({"messages": messages}) == TranscriptContext(messages=messages)
    assert normalizeContext({"systemPrompt": "", "tools": [], "messages": messages}) == TranscriptContext(
        messages=messages
    )
    assert normalizeContext({"systemPrompt": "be brief", "tools": [tool("a")], "messages": messages}).messages == [
        SystemMessage(content="be brief", toolsAdded=[tool("a")], timestamp=0),
        *messages,
    ]


def test_compares_tool_declarations_without_executable_or_undefined_fields():
    executable = {**dict(tool("a")), "constrainedSampling": None, "execute": lambda: None}
    assert declarationsEqual(executable, tool("a")) is True
    assert declarationsEqual(tool("a"), tool("a", "changed")) is False
    assert declarationsEqual(tool("a"), tool("a").model_copy(update={"constrainedSampling": False})) is False


def test_tool_state_changes_treat_changed_definitions_as_removal_plus_addition():
    changes = getToolStateChanges([tool("a"), tool("b")], [tool("b", "changed"), tool("c")])
    assert changes.toolsAdded == [tool("b", "changed"), tool("c")]
    assert changes.toolsRemoved == [ToolReference(name="a"), ToolReference(name="b")]
    unchanged = getToolStateChanges([tool("a")], [tool("a")])
    assert unchanged.toolsAdded == []
    assert unchanged.toolsRemoved == []


def test_detects_non_additive_tool_history_and_redefinitions():
    assert hasNonAdditiveToolChanges(transcript.messages) is True
    assert hasToolRedefinitions(transcript.messages) is False
    additive = normalizeContext(
        {
            "messages": [
                SystemMessage(content="", toolsAdded=[tool("a")], timestamp=1),
                SystemMessage(content="", toolsAdded=[tool("b")], timestamp=2),
            ]
        }
    )
    assert hasNonAdditiveToolChanges(additive.messages) is False
    redeclared = normalizeContext(
        {
            "messages": [
                SystemMessage(content="", toolsAdded=[tool("a")], timestamp=1),
                SystemMessage(content="", toolsAdded=[tool("a", "changed")], timestamp=2),
            ]
        }
    )
    assert hasNonAdditiveToolChanges(redeclared.messages) is True
    assert hasToolRedefinitions(redeclared.messages) is True

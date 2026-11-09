"""Translated from pi's ``coding-agent/test/assistant-message.test.ts`` (v1.1.0).

MISAKA fork (D1): "collapses individual thinking runs when clicked" is not ported; only the fullscreen renderer delivers
clicks.
"""

from __future__ import annotations

import re
import time
from typing import Any

import pytest

from misaka.ai.types import AssistantMessage
from misaka.modes.interactive.components.assistant_message import (
    AssistantMessageComponent,
)
from misaka.modes.interactive.components.user_message import UserMessageComponent
from misaka.modes.interactive.theme.theme import initTheme
from misaka.utils.ansi import stripAnsi

OSC133_ZONE_START = "\x1b]133;A\x07"
OSC133_ZONE_END = "\x1b]133;B\x07"
OSC133_ZONE_FINAL = "\x1b]133;C\x07"


def createAssistantMessage(content: list[dict[str, Any]], stopReason: str = "stop") -> AssistantMessage:
    return AssistantMessage.model_validate(
        {
            "role": "assistant",
            "content": content,
            "api": "openai-responses",
            "provider": "openai",
            "model": "gpt-4o-mini",
            "usage": {
                "input": 0,
                "output": 0,
                "cacheRead": 0,
                "cacheWrite": 0,
                "totalTokens": 0,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
            },
            "stopReason": stopReason,
            "timestamp": int(time.time() * 1000),
        }
    )


@pytest.fixture(autouse=True)
def dark_theme():
    initTheme("dark")


def test_adds_osc_133_zone_markers_to_assistant_messages_without_tool_calls():
    component = AssistantMessageComponent(createAssistantMessage([{"type": "text", "text": "hello"}]))
    lines = component.render(40)

    assert len(lines) != 0
    assert OSC133_ZONE_START in lines[0]
    assert lines[len(lines) - 1].startswith(OSC133_ZONE_END + OSC133_ZONE_FINAL)


def test_does_not_add_osc_133_zone_markers_when_assistant_message_contains_tool_calls():
    component = AssistantMessageComponent(
        createAssistantMessage(
            [
                {"type": "text", "text": "calling tool"},
                {"type": "toolCall", "id": "tool-1", "name": "read", "arguments": {"path": "file.txt"}},
            ]
        )
    )
    rendered = "\n".join(component.render(60))

    assert OSC133_ZONE_START not in rendered
    assert OSC133_ZONE_END not in rendered
    assert OSC133_ZONE_FINAL not in rendered


def test_renders_length_stops_with_neutral_truncation_wording():
    component = AssistantMessageComponent(
        createAssistantMessage([{"type": "thinking", "thinking": "private reasoning"}], stopReason="length"),
        True,
    )
    rendered = "\n".join(component.render(80))

    assert "Thinking..." in rendered
    assert "Response was truncated before completion." in rendered


def test_coalesces_adjacent_thinking_blocks_into_one_hidden_thinking_label():
    component = AssistantMessageComponent(
        createAssistantMessage(
            [
                {"type": "thinking", "thinking": "first thought"},
                {"type": "thinking", "thinking": ""},
                {"type": "thinking", "thinking": "second thought"},
                {"type": "text", "text": "answer"},
            ]
        ),
        True,
    )
    rendered = stripAnsi("\n".join(component.render(80)))

    assert len(re.findall(r"Thinking\.\.\.", rendered)) == 1
    assert "answer" in rendered


def test_uses_configured_output_padding_for_text_and_thinking():
    component = AssistantMessageComponent(
        createAssistantMessage(
            [
                {"type": "text", "text": "hello"},
                {"type": "thinking", "thinking": "reasoning"},
            ]
        ),
        False,
        None,
        "Thinking...",
        1,
    )
    lines = [stripAnsi(line) for line in component.render(80)]

    assert any(" hello" in line for line in lines)
    assert any(" reasoning" in line for line in lines)

    component.setOutputPad(0)
    updatedLines = [stripAnsi(line) for line in component.render(80)]
    assert any(line.startswith("hello") for line in updatedLines)
    assert any(line.startswith("reasoning") for line in updatedLines)


def test_chains_markdown_transformers_in_registration_order():
    calls: list[str] = []
    message = createAssistantMessage([{"type": "text", "text": "The result is $x^2$."}])

    def formula(markdown: str, context: Any) -> str:
        calls.append("formula")
        assert context == {"messageType": "assistant", "isStreaming": False, "availableWidth": 78}
        return markdown.replace("$x^2$", "x²")

    def suffix(markdown: str, _context: Any) -> str:
        calls.append("suffix")
        return f"{markdown} Done."

    component = AssistantMessageComponent(message, False, None, "Thinking...", 1, [formula, suffix])

    assert "The result is x². Done." in stripAnsi("\n".join(component.render(80)))
    assert calls == ["formula", "suffix"]


def test_identifies_partial_assistant_markdown_as_streaming():
    streamingStates: list[bool] = []
    message = createAssistantMessage([{"type": "text", "text": "partial"}])

    def transform(markdown: str, context: Any) -> str:
        streamingStates.append(context["isStreaming"])
        return markdown if context["isStreaming"] else f"{markdown} transformed"

    component = AssistantMessageComponent(None, False, None, "Thinking...", 1, [transform])

    component.updateContent(message, True)
    assert "transformed" not in stripAnsi("\n".join(component.render(80)))

    component.updateContent(message, False)
    assert "partial transformed" in stripAnsi("\n".join(component.render(80)))
    assert streamingStates == [True, False]


def test_reapplies_markdown_transformers_when_available_width_changes():
    availableWidths: list[int] = []

    def transform(markdown: str, context: Any) -> str:
        availableWidths.append(context["availableWidth"])
        return f"{markdown} ({context['availableWidth']})"

    component = AssistantMessageComponent(
        createAssistantMessage([{"type": "text", "text": "answer"}]), False, None, "Thinking...", 1, [transform]
    )

    assert "answer (78)" in stripAnsi("\n".join(component.render(80)))
    component.render(80)
    assert "answer (58)" in stripAnsi("\n".join(component.render(60)))
    assert availableWidths == [78, 58]


def test_continues_the_markdown_transformer_chain_when_a_transformer_throws():
    calls: list[str] = []

    def first(markdown: str, _context: Any) -> str:
        calls.append("first")
        return markdown.replace("still", "remains")

    def broken(_markdown: str, _context: Any) -> str:
        calls.append("throw")
        raise RuntimeError("broken transformer")

    def last(markdown: str, _context: Any) -> str:
        calls.append("last")
        return f"{markdown} after error"

    component = AssistantMessageComponent(
        createAssistantMessage([{"type": "text", "text": "still visible"}]),
        False,
        None,
        "Thinking...",
        1,
        [first, broken, last],
    )

    assert "remains visible after error" in stripAnsi("\n".join(component.render(80)))
    assert calls == ["first", "throw", "last"]


def test_transforms_text_and_thinking_markdown_without_mutating_the_original_message():
    message = createAssistantMessage(
        [
            {"type": "text", "text": "answer"},
            {"type": "thinking", "thinking": "reasoning"},
        ]
    )
    component = AssistantMessageComponent(
        message, False, None, "Thinking...", 1, [lambda markdown, context: f"{context['messageType']}:{markdown}"]
    )

    rendered = stripAnsi("\n".join(component.render(80)))
    assert "assistant:answer" in rendered
    assert "assistant-thinking:reasoning" in rendered
    assert [block.model_dump(exclude_none=True) for block in message.content] == [
        {"type": "text", "text": "answer"},
        {"type": "thinking", "thinking": "reasoning"},
    ]


def test_uses_configured_output_padding_for_user_messages():
    paddedComponent = UserMessageComponent("hello", None, 1)
    paddedLines = [stripAnsi(line) for line in paddedComponent.render(40)]
    assert any(line.startswith(" hello") for line in paddedLines)

    unpaddedComponent = UserMessageComponent("hello", None, 0)
    unpaddedLines = [stripAnsi(line) for line in unpaddedComponent.render(40)]
    assert any(line.startswith("hello") for line in unpaddedLines)

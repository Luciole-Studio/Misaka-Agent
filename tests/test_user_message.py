"""Translated from pi's ``coding-agent/test/user-message.test.ts`` (v1.1.0)."""

from __future__ import annotations

from typing import Any

import pytest

from misaka.modes.interactive.components.user_message import UserMessageComponent
from misaka.modes.interactive.theme.theme import initTheme
from misaka.utils.ansi import stripAnsi

OSC133_ZONE_START = "\x1b]133;A\x07"
OSC133_ZONE_END = "\x1b]133;B\x07"
OSC133_ZONE_FINAL = "\x1b]133;C\x07"
BG_RESET = "\x1b[49m"


@pytest.fixture(autouse=True)
def dark_theme():
    initTheme("dark")


def test_keeps_user_message_height_stable_while_moving_closing_osc_markers_off_line_end():
    component = UserMessageComponent("hello")
    lines = component.render(20)

    assert len(lines) == 3
    assert OSC133_ZONE_START in lines[0]
    assert lines[0].endswith(BG_RESET)
    assert OSC133_ZONE_END not in lines[0]
    assert "hello" in lines[1]
    assert lines[2].startswith(OSC133_ZONE_END + OSC133_ZONE_FINAL)
    assert lines[2].endswith(BG_RESET)


def test_chains_markdown_transformers_with_user_message_context():
    calls: list[str] = []

    def formula(markdown: str, context: Any) -> str:
        calls.append("formula")
        assert context == {"messageType": "user", "isStreaming": False, "availableWidth": 78}
        return markdown.replace("$x^2$", "x²")

    def suffix(markdown: str, _context: Any) -> str:
        calls.append("suffix")
        return f"{markdown} Done."

    component = UserMessageComponent("The input is $x^2$.", None, 1, [formula, suffix])

    assert "The input is x². Done." in stripAnsi("\n".join(component.render(80)))
    assert calls == ["formula", "suffix"]


def test_reapplies_markdown_transformers_when_invalidated():
    suffix = ["before"]
    component = UserMessageComponent("Message", None, 1, [lambda markdown, _context: f"{markdown} {suffix[0]}"])

    assert "Message before" in stripAnsi("\n".join(component.render(80)))

    suffix[0] = "after"
    component.invalidate()

    assert "Message after" in stripAnsi("\n".join(component.render(80)))

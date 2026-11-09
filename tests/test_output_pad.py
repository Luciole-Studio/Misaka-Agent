"""Translated from pi's ``coding-agent/test/output-pad.test.ts`` (v1.1.0)."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pytest

from misaka.agent.types import AgentToolResult
from misaka.ai.types import TextContent
from misaka.core.extensions.types import ToolDefinition
from misaka.core.messages import CompactionSummaryMessage
from misaka.core.tools.edit import createEditToolDefinition
from misaka.modes.interactive.components.bash_execution import BashExecutionComponent
from misaka.modes.interactive.components.compaction_summary_message import (
    CompactionSummaryMessageComponent,
)
from misaka.modes.interactive.components.tool_execution import ToolExecutionComponent
from misaka.modes.interactive.theme.theme import initTheme
from misaka.ui.tui.tui_main_screen import TuiMainScreen
from misaka.utils.ansi import stripAnsi
from tests.tui_virtual_terminal import VirtualTerminal


async def _execute(*_args: Any) -> AgentToolResult:
    return AgentToolResult(content=[TextContent(text="ok")], details={})


tool: ToolDefinition[Any, Any] = ToolDefinition(
    name="custom_tool",
    label="custom_tool",
    description="custom tool",
    parameters={},
    execute=_execute,
)

_WORD = re.compile(r"[\w$(]")


def renderLines(component: Any) -> list[str]:
    """Text lines without ANSI codes or trailing fill. Blank lines and full-width borders are skipped."""
    return [line for line in (stripAnsi(line).rstrip() for line in component.render(60)) if _WORD.search(line)]


@pytest.fixture(scope="module")
def ui():
    initTheme("dark")
    screen = TuiMainScreen(VirtualTerminal(80, 24))
    yield screen
    screen.stop()


def createTool(ui: Any, definition: Any | None, outputPad: int) -> ToolExecutionComponent:
    component = ToolExecutionComponent("custom_tool", "id", {}, {"outputPad": outputPad}, definition, ui, "/")
    component.updateResult({"content": [{"type": "text", "text": "ok"}], "isError": False})
    return component


def _bash(ui: Any, outputPad: int) -> Any:
    component = BashExecutionComponent("pwd", ui, False, outputPad)
    component.appendOutput("/tmp")
    component.setComplete(1, False)
    return component


def _edit(ui: Any, outputPad: int) -> Any:
    component = ToolExecutionComponent(
        "edit",
        "id",
        {"path": "file.txt", "edits": [{"oldText": "old", "newText": "new"}]},
        {"outputPad": outputPad},
        createEditToolDefinition("/"),
        ui,
        "/",
    )
    component.updateResult({"content": [{"type": "text", "text": "Could not find old text"}], "isError": True})
    return component


components: list[tuple[str, Callable[[Any, int], Any]]] = [
    ("bash execution", _bash),
    ("tool execution", lambda ui, outputPad: createTool(ui, tool, outputPad)),
    ("tool execution without a definition", lambda ui, outputPad: createTool(ui, None, outputPad)),
    ("self-rendered edit result", _edit),
    (
        "compaction summary",
        lambda _ui, outputPad: CompactionSummaryMessageComponent(
            CompactionSummaryMessage(summary="summary", tokensBefore=10, timestamp=0), None, outputPad
        ),
    ),
]


@pytest.mark.parametrize(("name", "create"), components, ids=[name for name, _create in components])
def test_renders_at_output_pad_0_and_1(ui, name, create):
    component = create(ui, 0)
    lines = renderLines(component)
    assert [line for line in lines if line.startswith(" ")] == []
    component.setOutputPad(1)
    assert renderLines(component) == [f" {line}" for line in lines]

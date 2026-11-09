"""Translated from pi's ``coding-agent/test/suite/regressions/8611-thinking-toggle-pending-bash-output.test.ts``
(v1.1.0)."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import Mock

from misaka.modes.interactive.components.tool_execution import ToolExecutionComponent
from misaka.modes.interactive.interactive_mode import InteractiveMode
from misaka.modes.interactive.theme.theme import initTheme
from misaka.ui.tui import Container
from misaka.utils.ansi import stripAnsi


def renderChat(container: Container) -> str:
    return stripAnsi("\n".join(container.render(120)))


def test_thinking_visibility_preserves_partial_bash_output():
    initTheme("dark")
    ui = SimpleNamespace(requestRender=Mock())
    chatContainer = Container()
    component = ToolExecutionComponent(
        "bash", "tool-8611", {"command": "echo first; sleep 10"}, {"showImages": False}, None, ui, os.getcwd()
    )
    component.markExecutionStarted()
    component.updateResult({"content": [{"type": "text", "text": "first"}], "isError": False}, True)
    chatContainer.addChild(component)

    fakeThis = SimpleNamespace(
        hideThinkingBlock=False,
        settingsManager=SimpleNamespace(setHideThinkingBlock=Mock()),
        chatContainer=chatContainer,
        ui=ui,
        showStatus=Mock(),
    )
    fakeThis.updateThinkingBlockVisibility = lambda: InteractiveMode.updateThinkingBlockVisibility(fakeThis)

    assert "first" in renderChat(chatContainer)
    InteractiveMode.toggleThinkingBlockVisibility(fakeThis)

    fakeThis.settingsManager.setHideThinkingBlock.assert_called_with(True)
    assert component in chatContainer.children
    assert "first" in renderChat(chatContainer)

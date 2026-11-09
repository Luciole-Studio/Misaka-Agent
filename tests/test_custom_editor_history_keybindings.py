"""Translated from pi's ``coding-agent/test/custom-editor-history-keybindings.test.ts`` (v1.1.0)."""

from __future__ import annotations

import pytest

from misaka.core.keybindings import KeybindingsManager
from misaka.modes.interactive.components.custom_editor import CustomEditor
from misaka.ui.tui import setKeybindings
from misaka.ui.tui.tui_main_screen import TuiMainScreen
from tests.tui_test_themes import defaultEditorTheme
from tests.tui_virtual_terminal import VirtualTerminal


@pytest.fixture(autouse=True)
def resetKeybindings():
    yield
    setKeybindings(KeybindingsManager())


def test_gives_an_explicit_history_binding_precedence_over_model_cycling():
    keybindings = KeybindingsManager(
        {
            "tui.editor.historyPrevious": "ctrl+p",
            "tui.editor.historyNext": "ctrl+n",
        }
    )
    setKeybindings(keybindings)
    screen = TuiMainScreen(VirtualTerminal())
    editor = CustomEditor(screen, defaultEditorTheme, keybindings)
    modelCycles = 0

    def cycle() -> None:
        nonlocal modelCycles
        modelCycles += 1

    editor.onAction("app.model.cycleForward", cycle)
    editor.addToHistory("previous prompt")
    editor.setText("draft")

    editor.handleInput("\x10")  # Ctrl+P
    assert editor.getText() == "previous prompt"
    assert modelCycles == 0

    editor.handleInput("\x0e")  # Ctrl+N
    assert editor.getText() == "draft"

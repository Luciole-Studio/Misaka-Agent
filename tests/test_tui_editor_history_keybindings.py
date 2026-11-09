"""Translated from pi-tui's ``test/editor-history-keybindings.test.ts`` (v1.1.0)."""

from __future__ import annotations

import pytest

from misaka.ui.tui.components.editor import Editor
from misaka.ui.tui.keybindings import (
    TUI_KEYBINDINGS,
    KeybindingsManager,
    setKeybindings,
)
from misaka.ui.tui.tui_main_screen import TuiMainScreen
from tests.tui_test_themes import defaultEditorTheme
from tests.tui_virtual_terminal import VirtualTerminal


@pytest.fixture(autouse=True)
def _resetKeybindings():
    yield
    setKeybindings(KeybindingsManager(TUI_KEYBINDINGS))


def test_browses_history_directly_without_first_moving_the_cursor():
    setKeybindings(
        KeybindingsManager(
            TUI_KEYBINDINGS,
            {
                "tui.editor.historyPrevious": "ctrl+p",
                "tui.editor.historyNext": "ctrl+n",
            },
        )
    )
    editor = Editor(TuiMainScreen(VirtualTerminal()), defaultEditorTheme)
    editor.addToHistory("older prompt")
    editor.addToHistory("newer\nmultiline prompt")
    editor.setText("draft")
    editor.handleInput("\x1b[D")
    editor.handleInput("\x1b[D")

    editor.handleInput("\x10")  # Ctrl+P
    assert editor.getText() == "newer\nmultiline prompt"
    assert editor.getCursor() == {"line": 0, "col": 0}

    editor.handleInput("\x10")  # Ctrl+P
    assert editor.getText() == "older prompt"

    editor.handleInput("\x0e")  # Ctrl+N
    assert editor.getText() == "newer\nmultiline prompt"
    assert editor.getCursor() == {"line": 1, "col": 16}

    editor.handleInput("\x0e")  # Ctrl+N
    assert editor.getText() == "draft"
    assert editor.getCursor() == {"line": 0, "col": 3}

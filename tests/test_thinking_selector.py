"""Translated from pi's ``coding-agent/test/thinking-selector.test.ts`` (v1.1.0)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from misaka.core.keybindings import KeybindingsManager
from misaka.modes.interactive.components.thinking_selector import (
    ThinkingSelectorComponent,
)
from misaka.modes.interactive.theme.theme import initTheme
from misaka.ui.tui import setKeybindings
from misaka.utils.ansi import stripAnsi


@pytest.fixture(autouse=True, scope="module")
def darkTheme():
    initTheme("dark")


@pytest.fixture(autouse=True)
def keybindings():
    setKeybindings(KeybindingsManager())
    yield
    setKeybindings(KeybindingsManager())


def test_keeps_the_current_thinking_level_marked_while_browsing():
    selector = ThinkingSelectorComponent("medium", ["medium", "high"], lambda _level: None, lambda: None)

    def getLevelRow(level: str) -> str | None:
        return next(
            (line for line in (stripAnsi(line) for line in selector.getSelectList().render(80)) if level in line),
            None,
        )

    selected = selector.getSelectList().getSelectedItem()
    assert (selected.label if selected else None) == "✓ medium"
    assert (getLevelRow("medium") or "").startswith("→ ✓ medium")
    selector.handleInput("\x1b[B")
    assert (getLevelRow("medium") or "").startswith("  ✓ medium")
    assert (getLevelRow("high") or "").startswith("→   high")


def test_uses_the_configured_save_binding():
    setKeybindings(KeybindingsManager({"app.thinking.save": "ctrl+r"}))
    saveDefault = MagicMock()
    selector = ThinkingSelectorComponent("medium", ["medium", "high"], lambda _level: None, lambda: None, saveDefault)

    assert "Ctrl+R to set as default" in stripAnsi("\n".join(selector.render(80)))
    selector.handleInput("\x13")
    saveDefault.assert_not_called()
    selector.handleInput("\x12")
    saveDefault.assert_called_with("medium")

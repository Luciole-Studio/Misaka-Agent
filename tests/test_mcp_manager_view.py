"""pi's ``test/mcp-manager-view.test.ts``."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from misaka.core.keybindings import KeybindingsManager
from misaka.extensions.mcp.ui import McpManagerView
from misaka.modes.interactive.theme.theme import initTheme, theme
from misaka.ui.tui import keybindings as tui_keybindings
from misaka.utils.ansi import stripAnsi

ESCAPE = "\x1b"
tui = SimpleNamespace(requestRender=lambda: None)


def rendered(view: McpManagerView) -> str:
    return stripAnsi("\n".join(view.render(80)))


@pytest.fixture(autouse=True)
def _keybindings(monkeypatch):
    initTheme("dark")
    monkeypatch.setattr(tui_keybindings, "globalKeybindings", KeybindingsManager())


# #10565
def test_cancels_the_running_operation_with_the_cancel_key():
    view = McpManagerView(tui, theme, KeybindingsManager())
    calls: list[None] = []
    view.status("Sign in to issues", "Contacting the authorization server…", lambda: calls.append(None))
    assert "cancel" in rendered(view)
    view.handleInput(ESCAPE)
    assert len(calls) == 1


def test_ignores_the_cancel_key_for_operations_that_cannot_be_cancelled():
    view = McpManagerView(tui, theme, KeybindingsManager())
    view.status("Sign in to issues", "Connecting…")
    assert "cancel" not in rendered(view)
    view.handleInput(ESCAPE)

"""Translated from pi-tui's ``test/keybindings.test.ts`` (v1.1.0).

MISAKA fork (D1): "binds unmodified terminal viewport shortcuts to alternate-screen navigation" checks the
``tui.altScreen.*`` keybindings of the fullscreen renderer, which is not ported.
"""

from misaka.ui.tui.keybindings import (
    TUI_KEYBINDINGS,
    KeybindingConflict,
    KeybindingsManager,
)


def test_binds_ctrl_j_as_a_default_newline_alias():
    keybindings = KeybindingsManager(TUI_KEYBINDINGS)

    assert keybindings.getKeys("tui.input.newLine") == ["shift+enter", "ctrl+j"]
    assert keybindings.matches("\n", "tui.input.newLine") is True
    assert keybindings.matches("\x1b[106;5u", "tui.input.newLine") is True


def test_binds_modified_and_unmodified_editor_viewport_navigation():
    keybindings = KeybindingsManager(TUI_KEYBINDINGS)

    assert keybindings.getKeys("tui.editor.cursorLineStart") == ["home", "ctrl+a"]
    assert keybindings.getKeys("tui.editor.cursorLineEnd") == ["end", "ctrl+e"]
    assert keybindings.getKeys("tui.editor.pageUp") == ["pageUp", "ctrl+pageUp"]
    assert keybindings.getKeys("tui.editor.pageDown") == ["pageDown", "ctrl+pageDown"]


def test_leaves_dedicated_prompt_history_navigation_unbound_by_default():
    keybindings = KeybindingsManager(TUI_KEYBINDINGS)

    assert keybindings.getKeys("tui.editor.historyPrevious") == []
    assert keybindings.getKeys("tui.editor.historyNext") == []


def test_does_not_evict_selector_confirm_when_input_submit_is_rebound():
    keybindings = KeybindingsManager(
        TUI_KEYBINDINGS,
        {
            "tui.input.submit": ["enter", "ctrl+enter"],
        },
    )

    assert keybindings.getKeys("tui.input.submit") == ["enter", "ctrl+enter"]
    assert keybindings.getKeys("tui.select.confirm") == ["enter"]


def test_does_not_evict_cursor_bindings_when_another_action_reuses_the_same_key():
    keybindings = KeybindingsManager(
        TUI_KEYBINDINGS,
        {
            "tui.select.up": ["up", "ctrl+p"],
        },
    )

    assert keybindings.getKeys("tui.select.up") == ["up", "ctrl+p"]
    assert keybindings.getKeys("tui.editor.cursorUp") == ["up"]


def test_still_reports_direct_user_binding_conflicts_without_evicting_defaults():
    keybindings = KeybindingsManager(
        TUI_KEYBINDINGS,
        {
            "tui.input.submit": "ctrl+x",
            "tui.select.confirm": "ctrl+x",
        },
    )

    assert keybindings.getConflicts() == [
        KeybindingConflict(
            key="ctrl+x",
            keybindings=["tui.input.submit", "tui.select.confirm"],
        ),
    ]
    assert keybindings.getKeys("tui.editor.cursorLeft") == ["left", "ctrl+b"]


# --- MISAKA ---------------------------------------------------------------------------------------


def test_an_unknown_keybinding_has_no_definition():
    # `this.definitions[keybinding]` is `undefined` for an id nobody defined.
    assert KeybindingsManager(TUI_KEYBINDINGS).getDefinition("tui.unknown") is None

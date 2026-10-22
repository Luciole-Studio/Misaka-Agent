"""Translated from pi coding-agent's ``test/keybindings.test.ts`` (v1.3.0, unchanged since v1.1.0).

MISAKA fork (D1): the ``tui.altScreen.*`` expectations of "applies the detected defaults consistently" belong to the
fullscreen renderer, which is not ported.
"""

import sys
from typing import get_args

from misaka.core import keybindings
from misaka.core.keybindings import KEYBINDINGS, useWindowsKeybindings
from misaka.ui.tui import TUI_KEYBINDINGS

# --- Windows keybinding defaults ------------------------------------------------------------------


def test_uses_windows_keybindings_on_native_windows():
    assert useWindowsKeybindings("win32", {}) is True


def test_uses_windows_keybindings_in_wsl_without_relying_on_windows_terminal_detection():
    assert useWindowsKeybindings("linux", {"WSL_DISTRO_NAME": "Ubuntu"}) is True
    assert useWindowsKeybindings("linux", {"WSL_INTEROP": "/run/WSL/123_interop"}) is True


def test_does_not_use_windows_keybindings_from_wt_session_alone():
    assert useWindowsKeybindings("linux", {"WT_SESSION": "session"}) is False


def test_keeps_non_windows_defaults_on_other_platforms():
    assert useWindowsKeybindings("linux", {}) is False
    assert useWindowsKeybindings("darwin", {}) is False


def test_applies_the_detected_defaults_consistently():
    windowsKeybindings = useWindowsKeybindings()
    nativeWindows = sys.platform == "win32"

    assert KEYBINDINGS["app.clipboard.pasteImage"].defaultKeys == ("alt+v" if windowsKeybindings else "ctrl+v")
    assert KEYBINDINGS["app.message.followUp"].defaultKeys == ("ctrl+q" if windowsKeybindings else "alt+enter")
    assert KEYBINDINGS["app.model.cycleBackward"].defaultKeys == ("alt+p" if windowsKeybindings else "shift+ctrl+p")
    assert KEYBINDINGS["tui.editor.undo"].defaultKeys == (
        "ctrl+z" if nativeWindows else "alt+z" if windowsKeybindings else "ctrl+-"
    )
    assert KEYBINDINGS["app.message.dequeue"].defaultKeys == ("alt+q" if windowsKeybindings else "alt+up")


# --- MISAKA: the registry ---------------------------------------------------------------------------------------


def test_app_keybinding_names_the_app_table():
    # MISAKA fork: `AppKeybinding` is spelled out where TypeScript derives it from `APP_KEYBINDINGS`.
    assert list(get_args(keybindings.AppKeybinding.__value__)) == list(keybindings.APP_KEYBINDINGS)


def test_keybindings_are_the_tui_table_then_the_app_table():
    assert list(KEYBINDINGS) == [*TUI_KEYBINDINGS, *keybindings.APP_KEYBINDINGS]
    assert all(KEYBINDINGS[name] is definition for name, definition in keybindings.APP_KEYBINDINGS.items())
    # D1: no fullscreen renderer, so no `tui.altScreen.*` ids.
    assert not [name for name in KEYBINDINGS if name.startswith("tui.altScreen.")]

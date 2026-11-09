"""MISAKA regressions for the editor and input forks (upstream has no case for them)."""

from __future__ import annotations

import re

from misaka.ui.tui.components.editor import Editor, wordWrapLine
from misaka.ui.tui.components.input import Input
from misaka.ui.tui.tui_main_screen import TuiMainScreen
from tests.tui_test_themes import defaultEditorTheme
from tests.tui_virtual_terminal import VirtualTerminal

_SGR = re.compile(r"\x1b\[[0-9;]*m")


def _editor() -> Editor:
    return Editor(TuiMainScreen(VirtualTerminal(80, 24)), defaultEditorTheme)


def test_backspace_removes_a_whole_marker_left_of_a_shortened_renumbered_one():
    # Ten pastes registered; the line holds "#10" before "#2" (a paste made after moving the cursor left), cursor
    # after "#2". Deleting "#2" renumbers "#10" to the shorter "#9" left of the cursor.
    editor = _editor()
    editor.pastes = {n: "\n".join(f"p{n}-{i}" for i in range(12)) for n in range(1, 11)}
    editor.pasteCounter = 10
    editor.insertTextAtCursor("[paste #10 +12 lines][paste #2 +12 lines]")
    editor.handleInput("\x7f")
    assert editor.getText() == "[paste #9 +12 lines]"
    assert editor.getCursor() == {"line": 0, "col": len("[paste #9 +12 lines]")}


def test_word_wrap_overflows_a_single_wide_grapheme_instead_of_recursing():
    assert [(chunk.text, chunk.startIndex, chunk.endIndex) for chunk in wordWrapLine("中", 1)] == [("中", 0, 1)]


def test_input_paste_drops_control_characters():
    field = Input()
    field.handleInput("\x1b[200~a\x1b[2Jb\x07c\x9bd\x1b[201~")
    assert field.getValue() == "a[2Jbcd"

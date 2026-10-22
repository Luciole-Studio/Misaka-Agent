"""Translated from pi-tui's ``test/editor.test.ts`` (v1.3.0).

MISAKA forks: offsets and lengths count code points where upstream counts UTF-16 code units (the affected
expectations say so where they change); ``mock.timers`` is a patched ``loop.call_later``; word navigation has no ICU
dictionary, so the cases that need one are strict xfails.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from misaka.ui.tui.autocomplete import (
    AutocompleteItem,
    AutocompleteSuggestions,
    CombinedAutocompleteProvider,
)
from misaka.ui.tui.components.editor import Editor, EditorOptions, wordWrapLine
from misaka.ui.tui.tui import FAKE_CURSOR_START, renderFakeCursor
from misaka.ui.tui.tui_main_screen import TuiMainScreen
from misaka.ui.tui.utils import stripTerminalSequences, visibleWidth
from tests.tui_test_themes import defaultEditorTheme
from tests.tui_virtual_terminal import VirtualTerminal

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b_[^\x07\x1b]*(?:\x07|\x1b\\)")


def stripVTControlCharacters(text: str) -> str:
    """``util.stripVTControlCharacters``: CSI, OSC and APC sequences removed."""
    return _ANSI.sub("", text)


def createTestTUI(cols: int = 80, rows: int = 24) -> TuiMainScreen:
    """Create a TUI with a virtual terminal for testing"""
    return TuiMainScreen(VirtualTerminal(cols, rows))


def applyCompletion(lines: list[str], cursorLine: int, cursorCol: int, item: Any, prefix: str) -> dict[str, Any]:
    """Standard applyCompletion that replaces prefix with item.value"""
    line = lines[cursorLine] if cursorLine < len(lines) else ""
    before = line[: cursorCol - len(prefix)]
    after = line[cursorCol:]
    newLines = [*lines]
    newLines[cursorLine] = before + item.value + after
    return {
        "lines": newLines,
        "cursorLine": cursorLine,
        "cursorCol": cursorCol - len(prefix) + len(item.value),
    }


async def flushAutocomplete() -> None:
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)


class Provider:
    """MISAKA fork: an ``AutocompleteProvider`` object literal."""

    def __init__(
        self,
        getSuggestions: Callable[..., Any],
        applyCompletion: Callable[..., Any] = applyCompletion,
        **members: Any,
    ) -> None:
        self._getSuggestions = getSuggestions
        self.applyCompletion = applyCompletion
        for name, value in members.items():
            setattr(self, name, value)

    async def getSuggestions(self, *args: Any) -> Any:
        result = self._getSuggestions(*args)
        if asyncio.iscoroutine(result):
            return await result
        return result


def items(*values: tuple[str, ...] | str) -> list[AutocompleteItem]:
    out: list[AutocompleteItem] = []
    for value in values:
        if isinstance(value, str):
            out.append(AutocompleteItem(value=value, label=value))
        else:
            out.append(AutocompleteItem(*value))
    return out


class FakeTimers:
    """MISAKA fork: ``t.mock.timers.enable({ apis: ["setTimeout"] })`` for ``loop.call_later``."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        # Milliseconds, as integers: float seconds drift apart over many ticks.
        self.now = 0
        self.pending: list[tuple[int, int, asyncio.TimerHandle, Callable[..., Any], tuple[Any, ...]]] = []
        self._original = loop.call_later
        self._seq = 0

    def enable(self) -> None:
        def call_later(delay: float, callback: Callable[..., Any], *args: Any, **kwargs: Any) -> asyncio.TimerHandle:
            when = self.now + round(delay * 1000)
            handle = asyncio.TimerHandle(when / 1000, callback, args, self.loop)
            self._seq += 1
            self.pending.append((when, self._seq, handle, callback, args))
            return handle

        self.loop.call_later = call_later  # type: ignore[method-assign]

    def reset(self) -> None:
        self.loop.call_later = self._original  # type: ignore[method-assign]

    def tick(self, ms: int) -> None:
        target = self.now + ms
        while True:
            due = sorted((entry for entry in self.pending if entry[0] <= target), key=lambda entry: (entry[0], entry[1]))
            if not due:
                break
            when, _seq, handle, callback, args = due[0]
            self.pending.remove(due[0])
            self.now = when
            if not handle.cancelled():
                callback(*args)
        self.now = target


@contextlib.contextmanager
def mockTimers() -> Iterator[FakeTimers]:
    timers = FakeTimers(asyncio.get_running_loop())
    timers.enable()
    try:
        yield timers
    finally:
        timers.reset()


# --- Prompt history navigation --------------------------------------------------------------------


def test_does_nothing_on_up_arrow_when_history_is_empty():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.handleInput("\x1b[A")  # Up arrow

    assert editor.getText() == ""


def test_shows_most_recent_history_entry_on_up_arrow_when_editor_is_empty():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.addToHistory("first prompt")
    editor.addToHistory("second prompt")

    editor.handleInput("\x1b[A")  # Up arrow

    assert editor.getText() == "second prompt"


def test_cycles_through_history_entries_on_repeated_up_arrow():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.addToHistory("first")
    editor.addToHistory("second")
    editor.addToHistory("third")

    editor.handleInput("\x1b[A")  # Up - shows "third"
    assert editor.getText() == "third"

    editor.handleInput("\x1b[A")  # Up - shows "second"
    assert editor.getText() == "second"

    editor.handleInput("\x1b[A")  # Up - shows "first"
    assert editor.getText() == "first"

    editor.handleInput("\x1b[A")  # Up - stays at "first" (oldest)
    assert editor.getText() == "first"


def test_jumps_to_start_before_entering_history_from_a_non_empty_draft():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.addToHistory("prompt")
    editor.setText("draft")
    editor.handleInput("\x1b[D")
    editor.handleInput("\x1b[D")

    editor.handleInput("\x1b[A")  # Up - jumps to start before history browsing
    assert editor.getText() == "draft"
    assert editor.getCursor() == {"line": 0, "col": 0}

    editor.handleInput("\x1b[A")  # Up at start - shows "prompt"
    assert editor.getText() == "prompt"

    editor.handleInput("\x1b[B")  # Down - restores draft
    assert editor.getText() == "draft"
    assert editor.getCursor() == {"line": 0, "col": 0}


def test_navigates_forward_through_history_with_down_arrow():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.addToHistory("first")
    editor.addToHistory("second")
    editor.addToHistory("third")
    editor.setText("draft")

    # Go to oldest
    editor.handleInput("\x1b[A")  # start of draft
    editor.handleInput("\x1b[A")  # third
    editor.handleInput("\x1b[A")  # second
    editor.handleInput("\x1b[A")  # first

    # Navigate back
    editor.handleInput("\x1b[B")  # second
    assert editor.getText() == "second"

    editor.handleInput("\x1b[B")  # third
    assert editor.getText() == "third"

    editor.handleInput("\x1b[B")  # draft
    assert editor.getText() == "draft"


def test_exits_history_mode_when_typing_a_character():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.addToHistory("old prompt")

    editor.handleInput("\x1b[A")  # Up - shows "old prompt"
    editor.handleInput("x")  # Type a character - exits history mode

    assert editor.getText() == "xold prompt"


def test_exits_history_mode_on_settext():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.addToHistory("first")
    editor.addToHistory("second")

    editor.handleInput("\x1b[A")  # Up - shows "second"
    editor.setText("")  # External clear

    # Up should start fresh from most recent
    editor.handleInput("\x1b[A")
    assert editor.getText() == "second"


def test_does_not_add_empty_strings_to_history():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.addToHistory("")
    editor.addToHistory("   ")
    editor.addToHistory("valid")

    editor.handleInput("\x1b[A")
    assert editor.getText() == "valid"

    # Should not have more entries
    editor.handleInput("\x1b[A")
    assert editor.getText() == "valid"


def test_does_not_add_consecutive_duplicates_to_history():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.addToHistory("same")
    editor.addToHistory("same")
    editor.addToHistory("same")

    editor.handleInput("\x1b[A")  # "same"
    assert editor.getText() == "same"

    editor.handleInput("\x1b[A")  # stays at "same" (only one entry)
    assert editor.getText() == "same"


def test_allows_non_consecutive_duplicates_in_history():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.addToHistory("first")
    editor.addToHistory("second")
    editor.addToHistory("first")  # Not consecutive, should be added

    editor.handleInput("\x1b[A")  # "first"
    assert editor.getText() == "first"

    editor.handleInput("\x1b[A")  # "second"
    assert editor.getText() == "second"

    editor.handleInput("\x1b[A")  # "first" (older one)
    assert editor.getText() == "first"


def test_uses_cursor_movement_instead_of_history_when_editor_has_content():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.addToHistory("history item")
    editor.setText("line1\nline2")

    # Cursor is at end of line2, Up should move to line1
    editor.handleInput("\x1b[A")  # Up - cursor movement

    # Insert character to verify cursor position
    editor.handleInput("X")

    # X should be inserted in line1, not replace with history
    assert editor.getText() == "line1X\nline2"


def test_limits_history_to_100_entries():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    # Add 105 entries
    for i in range(105):
        editor.addToHistory(f"prompt {i}")

    # Navigate to oldest
    for _i in range(100):
        editor.handleInput("\x1b[A")

    # Should be at entry 5 (oldest kept), not entry 0
    assert editor.getText() == "prompt 5"

    # One more Up should not change anything
    editor.handleInput("\x1b[A")
    assert editor.getText() == "prompt 5"


def test_places_cursor_at_start_after_browsing_history_upward():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.addToHistory("older entry")
    editor.addToHistory("line1\nline2\nline3")

    editor.handleInput("\x1b[A")  # Up - shows multi-line entry at start
    assert editor.getText() == "line1\nline2\nline3"
    assert editor.getCursor() == {"line": 0, "col": 0}

    editor.handleInput("\x1b[A")  # Up again - immediately navigates to older entry
    assert editor.getText() == "older entry"
    assert editor.getCursor() == {"line": 0, "col": 0}


def test_places_cursor_at_end_after_browsing_history_downward():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.addToHistory("older entry")
    editor.addToHistory("line1\nline2\nline3")
    editor.addToHistory("newer entry")

    editor.handleInput("\x1b[A")  # newer entry
    editor.handleInput("\x1b[A")  # multi-line entry
    editor.handleInput("\x1b[A")  # older entry

    editor.handleInput("\x1b[B")  # Down - shows multi-line entry at end
    assert editor.getText() == "line1\nline2\nline3"
    assert editor.getCursor() == {"line": 2, "col": 5}

    editor.handleInput("\x1b[B")  # Down again - immediately navigates to newer entry
    assert editor.getText() == "newer entry"


def test_allows_opposite_direction_cursor_movement_within_multi_line_history_entry():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.addToHistory("line1\nline2\nline3")

    editor.handleInput("\x1b[A")  # Up - shows entry at start
    assert editor.getCursor() == {"line": 0, "col": 0}

    editor.handleInput("\x1b[B")  # Down - cursor moves to line2
    assert editor.getText() == "line1\nline2\nline3"
    assert editor.getCursor() == {"line": 1, "col": 0}

    editor.handleInput("\x1b[A")  # Up - cursor moves back to line1
    assert editor.getText() == "line1\nline2\nline3"
    assert editor.getCursor() == {"line": 0, "col": 0}


# --- public state accessors -----------------------------------------------------------------------


def test_returns_cursor_position():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    assert editor.getCursor() == {"line": 0, "col": 0}

    editor.handleInput("a")
    editor.handleInput("b")
    editor.handleInput("c")

    assert editor.getCursor() == {"line": 0, "col": 3}

    editor.handleInput("\x1b[D")  # Left
    assert editor.getCursor() == {"line": 0, "col": 2}


def test_returns_lines_as_a_defensive_copy():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    editor.setText("a\nb")

    lines = editor.getLines()
    assert lines == ["a", "b"]

    lines[0] = "mutated"
    assert editor.getLines() == ["a", "b"]


# --- Backslash+Enter newline workaround -----------------------------------------------------------


def test_inserts_backslash_immediately_no_buffering():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.handleInput("\\")

    # Backslash should be visible immediately, not buffered
    assert editor.getText() == "\\"


def test_converts_standalone_backslash_to_newline_on_enter():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.handleInput("\\")
    editor.handleInput("\r")

    assert editor.getText() == "\n"


def test_inserts_backslash_normally_when_followed_by_other_characters():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.handleInput("\\")
    editor.handleInput("x")

    assert editor.getText() == "\\x"


def test_does_not_trigger_newline_when_backslash_is_not_immediately_before_cursor():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    submitted = False

    def onSubmit(_text: str) -> None:
        nonlocal submitted
        submitted = True

    editor.onSubmit = onSubmit

    editor.handleInput("\\")
    editor.handleInput("x")
    editor.handleInput("\r")

    # Should submit, not insert newline (backslash not at cursor)
    assert submitted is True


def test_only_removes_one_backslash_when_multiple_are_present():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.handleInput("\\")
    editor.handleInput("\\")
    editor.handleInput("\\")
    assert editor.getText() == "\\\\\\"

    editor.handleInput("\r")
    # Only the last backslash is removed, newline inserted
    assert editor.getText() == "\\\\\n"


# --- Kitty CSI-u handling -------------------------------------------------------------------------


def test_ignores_printable_csi_u_sequences_with_unsupported_modifiers():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.handleInput("\x1b[99;9u")

    assert editor.getText() == ""


def test_inserts_shifted_csi_u_letters_as_text():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.handleInput("\x1b[69;2u")

    assert editor.getText() == "E"


def test_inserts_shifted_xterm_modifyotherkeys_letters_as_text():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.handleInput("\x1b[27;2;69~")

    assert editor.getText() == "E"


# --- Unicode text editing behavior ----------------------------------------------------------------

NO_ICU_DICTIONARY = pytest.mark.xfail(
    strict=True, reason="MISAKA fork: no ICU word dictionary, so a Han run segments per character"
)


def test_inserts_mixed_ascii_umlauts_and_emojis_as_literal_text():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.handleInput("H")
    editor.handleInput("e")
    editor.handleInput("l")
    editor.handleInput("l")
    editor.handleInput("o")
    editor.handleInput(" ")
    editor.handleInput("ä")
    editor.handleInput("ö")
    editor.handleInput("ü")
    editor.handleInput(" ")
    editor.handleInput("😀")

    text = editor.getText()
    assert text == "Hello äöü 😀"


def test_deletes_single_code_unit_unicode_characters_umlauts_with_backspace():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.handleInput("ä")
    editor.handleInput("ö")
    editor.handleInput("ü")

    # Delete the last character (ü)
    editor.handleInput("\x7f")  # Backspace

    text = editor.getText()
    assert text == "äö"


def test_deletes_multi_code_unit_emojis_with_single_backspace():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.handleInput("😀")
    editor.handleInput("👍")

    # Delete the last emoji (👍) - single backspace deletes whole grapheme cluster
    editor.handleInput("\x7f")  # Backspace

    text = editor.getText()
    assert text == "😀"


def test_inserts_characters_at_the_correct_position_after_cursor_movement_over_umlauts():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.handleInput("ä")
    editor.handleInput("ö")
    editor.handleInput("ü")

    # Move cursor left twice
    editor.handleInput("\x1b[D")  # Left arrow
    editor.handleInput("\x1b[D")  # Left arrow

    # Insert 'x' in the middle
    editor.handleInput("x")

    text = editor.getText()
    assert text == "äxöü"


def test_moves_cursor_across_multi_code_unit_emojis_with_single_arrow_key():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.handleInput("😀")
    editor.handleInput("👍")
    editor.handleInput("🎉")

    # Move cursor left over last emoji (🎉) - single arrow moves over whole grapheme
    editor.handleInput("\x1b[D")  # Left arrow

    # Move cursor left over second emoji (👍)
    editor.handleInput("\x1b[D")

    # Insert 'x' between first and second emoji
    editor.handleInput("x")

    text = editor.getText()
    assert text == "😀x👍🎉"


def test_preserves_umlauts_across_line_breaks():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.handleInput("ä")
    editor.handleInput("ö")
    editor.handleInput("ü")
    editor.handleInput("\n")  # new line
    editor.handleInput("Ä")
    editor.handleInput("Ö")
    editor.handleInput("Ü")

    text = editor.getText()
    assert text == "äöü\nÄÖÜ"


def test_replaces_the_entire_document_with_unicode_text_via_settext_paste_simulation():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    # Simulate bracketed paste / programmatic replacement
    editor.setText("Hällö Wörld! 😀 äöüÄÖÜß")

    text = editor.getText()
    assert text == "Hällö Wörld! 😀 äöüÄÖÜß"


def test_moves_cursor_to_document_start_on_ctrl_a_and_inserts_at_the_beginning():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.handleInput("a")
    editor.handleInput("b")
    editor.handleInput("\x01")  # Ctrl+A (move to start)
    editor.handleInput("x")  # Insert at start

    text = editor.getText()
    assert text == "xab"


def test_deletes_words_correctly_with_ctrl_w_and_alt_backspace():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    # Basic word deletion
    editor.setText("foo bar baz")
    editor.handleInput("\x17")  # Ctrl+W
    assert editor.getText() == "foo bar "

    # Trailing whitespace
    editor.setText("foo bar   ")
    editor.handleInput("\x17")
    assert editor.getText() == "foo "

    # Punctuation run
    editor.setText("foo bar...")
    editor.handleInput("\x17")
    assert editor.getText() == "foo bar"

    # ASCII punctuation inside Intl word-like segments preserves old boundaries
    editor.setText("foo.bar")
    editor.handleInput("\x17")
    assert editor.getText() == "foo."

    editor.setText("foo:bar")
    editor.handleInput("\x17")
    assert editor.getText() == "foo:"

    # Delete across multiple lines
    editor.setText("line one\nline two")
    editor.handleInput("\x17")
    assert editor.getText() == "line one\nline "

    # Delete empty line (merge)
    editor.setText("line one\n")
    editor.handleInput("\x17")
    assert editor.getText() == "line one"

    # Grapheme safety (emoji as a word)
    editor.setText("foo 😀😀 bar")
    editor.handleInput("\x17")
    assert editor.getText() == "foo 😀😀 "
    editor.handleInput("\x17")
    assert editor.getText() == "foo "

    # Alt+Backspace
    editor.setText("foo bar")
    editor.handleInput("\x1b\x7f")  # Alt+Backspace (legacy)
    assert editor.getText() == "foo "


def test_navigates_words_correctly_with_ctrl_left_right():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("foo bar... baz")
    # Cursor at end

    # Move left over baz
    editor.handleInput("\x1b[1;5D")  # Ctrl+Left
    assert editor.getCursor() == {"line": 0, "col": 11}  # after '...'

    # Move left over punctuation
    editor.handleInput("\x1b[1;5D")  # Ctrl+Left
    assert editor.getCursor() == {"line": 0, "col": 7}  # after 'bar'

    # Move left over bar
    editor.handleInput("\x1b[1;5D")  # Ctrl+Left
    assert editor.getCursor() == {"line": 0, "col": 4}  # after 'foo '

    # Move right over bar
    editor.handleInput("\x1b[1;5C")  # Ctrl+Right
    assert editor.getCursor() == {"line": 0, "col": 7}  # at end of 'bar'

    # Move right over punctuation run
    editor.handleInput("\x1b[1;5C")  # Ctrl+Right
    assert editor.getCursor() == {"line": 0, "col": 10}  # after '...'

    # Move right skips space and lands after baz
    editor.handleInput("\x1b[1;5C")  # Ctrl+Right
    assert editor.getCursor() == {"line": 0, "col": 14}  # end of line

    # Test forward from start with leading whitespace
    editor.setText("   foo bar")
    editor.handleInput("\x01")  # Ctrl+A to go to start
    editor.handleInput("\x1b[1;5C")  # Ctrl+Right
    assert editor.getCursor() == {"line": 0, "col": 6}  # after 'foo'

    # ASCII punctuation inside Intl word-like segments preserves old boundaries
    editor.setText("foo.bar baz")
    editor.handleInput("\x1b[1;5D")  # Ctrl+Left over baz
    assert editor.getCursor() == {"line": 0, "col": 8}
    editor.handleInput("\x1b[1;5D")  # Ctrl+Left over bar
    assert editor.getCursor() == {"line": 0, "col": 4}
    editor.handleInput("\x1b[1;5D")  # Ctrl+Left over .
    assert editor.getCursor() == {"line": 0, "col": 3}

    editor.handleInput("\x01")  # Ctrl+A
    editor.handleInput("\x1b[1;5C")  # Ctrl+Right over foo
    assert editor.getCursor() == {"line": 0, "col": 3}
    editor.handleInput("\x1b[1;5C")  # Ctrl+Right over .
    assert editor.getCursor() == {"line": 0, "col": 4}
    editor.handleInput("\x1b[1;5C")  # Ctrl+Right over bar
    assert editor.getCursor() == {"line": 0, "col": 7}


@NO_ICU_DICTIONARY
def test_stops_at_fullwidth_chinese_punctuation_issue_4972():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    # 你好，世界 = 你好(0-2) ，(2-3) 世界(3-5)
    editor.setText("你好，世界")
    # Cursor at end (col 5)

    # Move left over 世界
    editor.handleInput("\x1b[1;5D")  # Ctrl+Left
    assert editor.getCursor() == {"line": 0, "col": 3}  # after ，

    # Move left over ，
    editor.handleInput("\x1b[1;5D")  # Ctrl+Left
    assert editor.getCursor() == {"line": 0, "col": 2}  # after 你好

    # Move left over 你好
    editor.handleInput("\x1b[1;5D")  # Ctrl+Left
    assert editor.getCursor() == {"line": 0, "col": 0}  # start

    # Move right over 你好
    editor.handleInput("\x1b[1;5C")  # Ctrl+Right
    assert editor.getCursor() == {"line": 0, "col": 2}  # after 你好

    # Move right over ，
    editor.handleInput("\x1b[1;5C")  # Ctrl+Right
    assert editor.getCursor() == {"line": 0, "col": 3}  # after ，

    # Move right over 世界
    editor.handleInput("\x1b[1;5C")  # Ctrl+Right
    assert editor.getCursor() == {"line": 0, "col": 5}  # end


@NO_ICU_DICTIONARY
def test_handles_mixed_cjk_and_ascii_word_movement():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    # "hello你好，world世界" = hello(0-5) 你好(5-7) ，(7-8) world(8-13) 世界(13-15)
    editor.setText("hello你好，world世界")
    # Cursor at end (col 15)

    # Move left over 世界
    editor.handleInput("\x1b[1;5D")  # Ctrl+Left
    assert editor.getCursor() == {"line": 0, "col": 13}  # after 'world'

    # Move left over world
    editor.handleInput("\x1b[1;5D")  # Ctrl+Left
    assert editor.getCursor() == {"line": 0, "col": 8}  # after ，

    # Move left over ，
    editor.handleInput("\x1b[1;5D")  # Ctrl+Left
    assert editor.getCursor() == {"line": 0, "col": 7}  # after 你好

    # Move left over 你好
    editor.handleInput("\x1b[1;5D")  # Ctrl+Left
    assert editor.getCursor() == {"line": 0, "col": 5}  # after 'hello'

    # Move left over hello
    editor.handleInput("\x1b[1;5D")  # Ctrl+Left
    assert editor.getCursor() == {"line": 0, "col": 0}  # start

    # Forward from start
    editor.handleInput("\x1b[1;5C")  # Ctrl+Right
    assert editor.getCursor() == {"line": 0, "col": 5}  # after 'hello'

    editor.handleInput("\x1b[1;5C")  # Ctrl+Right
    assert editor.getCursor() == {"line": 0, "col": 7}  # after 你好

    editor.handleInput("\x1b[1;5C")  # Ctrl+Right
    assert editor.getCursor() == {"line": 0, "col": 8}  # after ，

    editor.handleInput("\x1b[1;5C")  # Ctrl+Right
    assert editor.getCursor() == {"line": 0, "col": 13}  # after 'world'

    editor.handleInput("\x1b[1;5C")  # Ctrl+Right
    assert editor.getCursor() == {"line": 0, "col": 15}  # end


# --- Scroll indicators ----------------------------------------------------------------------------


def test_centers_scroll_indicators_on_wide_borders():
    width = 40
    editor = Editor(createTestTUI(width), defaultEditorTheme)
    editor.setText("\n".join(f"line {index}" for index in range(20)))

    editor.render(width)
    for _index in range(10):
        editor.handleInput("\x1b[A")

    lines = editor.render(width)
    assert stripVTControlCharacters(lines[0]) == f"{'─' * 15} ↑ 9 more {'─' * 15}"
    assert stripVTControlCharacters(lines[-1]) == f"{'─' * 15} ↓ 4 more {'─' * 15}"


def test_keeps_truncated_scroll_indicators_within_width_and_preserves_their_color_issue_6962():
    import dataclasses

    width = 10

    def borderColor(text: str) -> str:
        return f"\x1b[35m{text}\x1b[39m"

    editor = Editor(createTestTUI(width), dataclasses.replace(defaultEditorTheme, borderColor=borderColor))
    editor.setText("\n".join(f"line {index}" for index in range(20)))

    # Render once to initialize wrapping, then move the cursor so content remains above and below the viewport.
    editor.render(width)
    for _index in range(10):
        editor.handleInput("\x1b[A")

    lines = editor.render(width)
    topBorder = lines[0]
    bottomBorder = lines[-1]

    assert re.search(r"^─── ↑", stripVTControlCharacters(topBorder))
    assert re.search(r"^─── ↓", stripVTControlCharacters(bottomBorder))
    assert topBorder == borderColor(stripVTControlCharacters(topBorder))
    assert bottomBorder == borderColor(stripVTControlCharacters(bottomBorder))
    for line in lines:
        assert visibleWidth(line) == width, f"line exceeds width {width}: {line!r}"


# --- Grapheme-aware text wrapping -----------------------------------------------------------------


def test_wraps_lines_correctly_when_text_contains_wide_emojis():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    width = 20

    # ✅ is 2 columns wide, so "Hello ✅ World" is 14 columns
    editor.setText("Hello ✅ World")
    lines = editor.render(width)

    # All content lines (between borders) should fit within width
    for i in range(1, len(lines) - 1):
        lineWidth = visibleWidth(lines[i])
        assert lineWidth == width, f"Line {i} has width {lineWidth}, expected {width}"


def test_wraps_long_text_with_emojis_at_correct_positions():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    width = 10

    # Each ✅ is 2 columns. "✅✅✅✅✅" = 10 columns, fits exactly
    # "✅✅✅✅✅✅" = 12 columns, needs wrap
    editor.setText("✅✅✅✅✅✅")
    lines = editor.render(width)

    # Should have 2 content lines (plus 2 border lines)
    # First line: 5 emojis (10 cols), second line: 1 emoji (2 cols) + padding
    for i in range(1, len(lines) - 1):
        lineWidth = visibleWidth(lines[i])
        assert lineWidth == width, f"Line {i} has width {lineWidth}, expected {width}"


def test_renders_isolated_thai_and_lao_am_clusters_without_width_drift():
    for text in ["ำabc", "ຳabc"]:
        editor = Editor(createTestTUI(), defaultEditorTheme)
        width = 8
        editor.setText(text)

        for line in editor.render(width):
            assert visibleWidth(line) == width, f"line width drift for {text!r}: {line}"


def test_wraps_cjk_characters_correctly_each_is_2_columns_wide():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    width = 10 + 1  # +1 col reserved for cursor

    # Each CJK char is 2 columns. "日本語テスト" = 6 chars = 12 columns
    editor.setText("日本語テスト")
    lines = editor.render(width)

    for i in range(1, len(lines) - 1):
        lineWidth = visibleWidth(lines[i])
        assert lineWidth == width, f"Line {i} has width {lineWidth}, expected {width}"

    # Verify content split correctly
    contentLines = [stripTerminalSequences(line).strip() for line in lines[1:-1]]
    assert len(contentLines) == 2
    assert contentLines[0] == "日本語テス"  # 5 chars = 10 columns
    assert contentLines[1] == "ト"  # 1 char = 2 columns (+ padding)


def test_handles_mixed_ascii_and_wide_characters_in_wrapping():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    width = 15 + 1  # +1 col reserved for cursor

    # "Test ✅ OK 日本" = 4 + 1 + 2 + 1 + 2 + 1 + 4 = 15 columns (fits in width-1=15)
    editor.setText("Test ✅ OK 日本")
    lines = editor.render(width)

    # Should fit in one content line
    contentLines = lines[1:-1]
    assert len(contentLines) == 1

    lineWidth = visibleWidth(contentLines[0])
    assert lineWidth == width


def test_renders_cursor_correctly_on_wide_characters():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    width = 20

    editor.setText("A✅B")
    # Cursor should be at end (after B)
    lines = editor.render(width)

    # The cursor (reverse video space) should be visible
    contentLine = lines[1]
    assert FAKE_CURSOR_START in contentLine, "Should have fake cursor"

    # Line should still be correct width
    assert visibleWidth(contentLine) == width


def test_does_not_exceed_terminal_width_with_emoji_at_wrap_boundary():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    width = 11

    # "0123456789✅" = 10 ASCII + 2-wide emoji = 12 columns
    # Should wrap before the emoji since it would exceed width
    editor.setText("0123456789✅")
    lines = editor.render(width)

    for i in range(1, len(lines) - 1):
        lineWidth = visibleWidth(lines[i])
        assert lineWidth <= width, f"Line {i} has width {lineWidth}, exceeds max {width}"


def test_shows_cursor_at_end_of_line_before_wrap_wraps_on_next_char():
    width = 10
    for paddingX in [0, 1]:
        editor = Editor(createTestTUI(width + paddingX), defaultEditorTheme, EditorOptions(paddingX=paddingX))

        # Type 9 chars → fills layoutWidth exactly, cursor at end on same line
        for ch in "aaaaaaaaa":
            editor.handleInput(ch)
        lines = editor.render(width + paddingX)
        contentLines = lines[1:-1]
        assert len(contentLines) == 1, "Should be 1 content line before wrap"
        assert contentLines[0].endswith(renderFakeCursor(" ")), "Cursor should be at end of line"

        # Type 1 more → text wraps to second line
        editor.handleInput("a")
        lines = editor.render(width + paddingX)
        contentLines = lines[1:-1]
        assert len(contentLines) == 2, "Should wrap to 2 content lines"


# --- Word wrapping --------------------------------------------------------------------------------


def _segments(line: str, *parts: tuple[str, int]) -> list[Any]:
    from misaka.ui.tui.utils import SegmentData

    return [SegmentData(segment=segment, index=index, input=line) for segment, index in parts]


def test_wraps_at_word_boundaries_instead_of_mid_word():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    width = 40

    editor.setText("Hello world this is a test of word wrapping functionality")
    lines = editor.render(width)

    # Get content lines (between borders)
    contentLines = [stripTerminalSequences(line).strip() for line in lines[1:-1]]

    # Should NOT break mid-word
    # Line 1 should end with a complete word
    assert not contentLines[0].endswith("-"), "Line should not end with hyphen (mid-word break)"

    # Each content line should be complete words
    for line in contentLines:
        # Words at end of line should be complete (no partial words)
        lastChar = line.rstrip()[-1:]
        assert lastChar == "" or re.search(r"[\w.,!?;:]", lastChar, re.ASCII), f'Line ends unexpectedly with: "{lastChar}"'


def test_does_not_start_lines_with_leading_whitespace_after_word_wrap():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    width = 20

    editor.setText("Word1 Word2 Word3 Word4 Word5 Word6")
    lines = editor.render(width)

    # Get content lines (between borders)
    contentLines = lines[1:-1]

    # No line should start with whitespace (except for padding at the end)
    for i in range(len(contentLines)):
        line = stripVTControlCharacters(contentLines[i])
        trimmedStart = line.lstrip()
        # The line should either be all padding or start with a word character
        if len(trimmedStart) > 0:
            assert not re.search(r"^\s+\S", line.rstrip()), f"Line {i} starts with unexpected whitespace before content"


def test_breaks_long_words_urls_at_character_level():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    width = 30

    editor.setText("Check https://example.com/very/long/path/that/exceeds/width here")
    lines = editor.render(width)

    # All lines should fit within width
    for i in range(1, len(lines) - 1):
        lineWidth = visibleWidth(lines[i])
        assert lineWidth == width, f"Line {i} has width {lineWidth}, expected {width}"


def test_preserves_multiple_spaces_within_words_on_same_line():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    width = 50

    editor.setText("Word1   Word2    Word3")
    lines = editor.render(width)

    contentLine = stripVTControlCharacters(lines[1]).strip()
    # Multiple spaces should be preserved
    assert "Word1   Word2" in contentLine, "Multiple spaces should be preserved"


def test_handles_empty_string():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    width = 40

    editor.setText("")
    lines = editor.render(width)

    # Should have border + empty content + border
    assert len(lines) == 3


def test_handles_single_word_that_fits_exactly():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    width = 10 + 1  # +1 col reserved for cursor

    editor.setText("1234567890")
    lines = editor.render(width)

    # Should have exactly 3 lines (top border, content, bottom border)
    assert len(lines) == 3
    contentLine = stripVTControlCharacters(lines[1])
    assert "1234567890" in contentLine, "Content should contain the word"


def test_wraps_word_to_next_line_when_it_ends_exactly_at_terminal_width():
    # "hello " (6) + "world" (5) = 11, but "world" is non-whitespace ending at width.
    # Thus, wrap it to next line. The trailing space stays with "hello" on line 1
    chunks = wordWrapLine("hello world test", 11)

    assert len(chunks) == 2
    assert chunks[0].text == "hello "
    assert chunks[1].text == "world test"


def test_keeps_whitespace_at_terminal_width_boundary_on_same_line():
    # "hello world " is exactly 12 chars (including trailing space)
    # The space at position 12 should stay on the first line
    chunks = wordWrapLine("hello world test", 12)

    assert len(chunks) == 2
    assert chunks[0].text == "hello world "
    assert chunks[1].text == "test"


def test_handles_unbreakable_word_filling_width_exactly_followed_by_space():
    chunks = wordWrapLine("aaaaaaaaaaaa aaaa", 12)

    assert len(chunks) == 2
    assert chunks[0].text == "aaaaaaaaaaaa"
    assert chunks[1].text == " aaaa"


def test_wraps_word_to_next_line_when_it_fits_width_but_not_remaining_space():
    chunks = wordWrapLine("      aaaaaaaaaaaa", 12)

    assert len(chunks) == 2
    assert chunks[0].text == "      "
    assert chunks[1].text == "aaaaaaaaaaaa"


def test_keeps_word_with_multi_space_and_following_word_together_when_they_fit():
    chunks = wordWrapLine("Lorem ipsum dolor sit amet,    consectetur", 30)

    assert len(chunks) == 2
    assert chunks[0].text == "Lorem ipsum dolor sit "
    assert chunks[1].text == "amet,    consectetur"


def test_keeps_word_with_multi_space_and_following_word_when_they_fill_width_exactly():
    chunks = wordWrapLine("Lorem ipsum dolor sit amet,              consectetur", 30)

    assert len(chunks) == 2
    assert chunks[0].text == "Lorem ipsum dolor sit "
    assert chunks[1].text == "amet,              consectetur"


def test_splits_when_word_plus_multi_space_plus_word_exceeds_width():
    chunks = wordWrapLine("Lorem ipsum dolor sit amet,               consectetur", 30)

    assert len(chunks) == 3
    assert chunks[0].text == "Lorem ipsum dolor sit "
    assert chunks[1].text == "amet,               "
    assert chunks[2].text == "consectetur"


def test_breaks_long_whitespace_at_line_boundary():
    chunks = wordWrapLine("Lorem ipsum dolor sit amet,                         consectetur", 30)

    assert len(chunks) == 3
    assert chunks[0].text == "Lorem ipsum dolor sit "
    assert chunks[1].text == "amet,                         "
    assert chunks[2].text == "consectetur"


def test_breaks_long_whitespace_at_line_boundary_2():
    chunks = wordWrapLine("Lorem ipsum dolor sit amet,                          consectetur", 30)

    assert len(chunks) == 3
    assert chunks[0].text == "Lorem ipsum dolor sit "
    assert chunks[1].text == "amet,                         "
    assert chunks[2].text == " consectetur"


def test_breaks_whitespace_spanning_full_lines():
    chunks = wordWrapLine("Lorem ipsum dolor sit amet,                                     consectetur", 30)

    assert len(chunks) == 3
    assert chunks[0].text == "Lorem ipsum dolor sit "
    assert chunks[1].text == "amet,                         "
    assert chunks[2].text == "            consectetur"


def test_force_breaks_when_wide_char_after_word_boundary_wrap_still_overflows():
    # " " (1) + "a"*186 (186) + "你" (2) = 189 visible width
    # maxWidth = 187: backtracking to the space would leave 186 + 2 = 188 > 187,
    # so the algorithm must force-break before the wide char instead.
    line = f" {'a' * 186}你"
    chunks = wordWrapLine(line, 187)

    for chunk in chunks:
        assert visibleWidth(chunk.text) <= 187, (
            f'chunk "{chunk.text[:20]}..." has visible width {visibleWidth(chunk.text)}, expected <= 187'
        )
    # Verify no content is lost
    reconstructed = "".join(line[c.startIndex : c.endIndex] for c in chunks)
    assert reconstructed == line


def test_splits_oversized_atomic_segment_across_multiple_chunks():
    # Simulate a paste marker wider than maxWidth by passing pre-segmented data
    marker = "[paste #1 +20 lines]"  # 21 chars
    line = f"A{marker}B"
    segments = _segments(line, ("A", 0), (marker, 1), ("B", 1 + len(marker)))

    chunks = wordWrapLine(line, 10, segments)

    # Every chunk must fit within maxWidth
    for chunk in chunks:
        assert visibleWidth(chunk.text) <= 10, (
            f'chunk "{chunk.text}" has visible width {visibleWidth(chunk.text)}, expected <= 10'
        )

    # Verify no content is lost
    reconstructed = "".join(line[c.startIndex : c.endIndex] for c in chunks)
    assert reconstructed == line


def test_splits_oversized_atomic_segment_at_start_of_line():
    marker = "[paste #1 +20 lines]"  # 21 chars
    line = f"{marker}B"
    segments = _segments(line, (marker, 0), ("B", len(marker)))

    chunks = wordWrapLine(line, 10, segments)

    for chunk in chunks:
        assert visibleWidth(chunk.text) <= 10
    # "B" ends up on the last line (either alone or with the marker tail)
    assert ("B" in chunks[len(chunks) - 1].text) is True

    reconstructed = "".join(line[c.startIndex : c.endIndex] for c in chunks)
    assert reconstructed == line


def test_splits_oversized_atomic_segment_at_end_of_line():
    marker = "[paste #1 +20 lines]"  # 21 chars
    line = f"A{marker}"
    segments = _segments(line, ("A", 0), (marker, 1))

    chunks = wordWrapLine(line, 10, segments)

    for chunk in chunks:
        assert visibleWidth(chunk.text) <= 10
    assert chunks[0].text == "A"

    reconstructed = "".join(line[c.startIndex : c.endIndex] for c in chunks)
    assert reconstructed == line


def test_splits_consecutive_oversized_atomic_segments():
    m1 = "[paste #1 +20 lines]"  # 21 chars
    m2 = "[paste #2 +30 lines]"  # 21 chars
    line = f"{m1}{m2}"
    segments = _segments(line, (m1, 0), (m2, len(m1)))

    chunks = wordWrapLine(line, 10, segments)

    for chunk in chunks:
        assert visibleWidth(chunk.text) <= 10, (
            f'chunk "{chunk.text}" has visible width {visibleWidth(chunk.text)}, expected <= 10'
        )

    reconstructed = "".join(line[c.startIndex : c.endIndex] for c in chunks)
    assert reconstructed == line


def test_wraps_normally_after_oversized_atomic_segment():
    marker = "[paste #1 +20 lines]"  # 21 chars
    line = f"{marker} hello world"
    n = len(marker)
    segments = _segments(
        line,
        (marker, 0),
        (" ", n),
        ("h", n + 1),
        ("e", n + 2),
        ("l", n + 3),
        ("l", n + 4),
        ("o", n + 5),
        (" ", n + 6),
        ("w", n + 7),
        ("o", n + 8),
        ("r", n + 9),
        ("l", n + 10),
        ("d", n + 11),
    )

    chunks = wordWrapLine(line, 10, segments)

    # All chunks must fit
    for chunk in chunks:
        assert visibleWidth(chunk.text) <= 10, (
            f'chunk "{chunk.text}" has visible width {visibleWidth(chunk.text)}, expected <= 10'
        )

    # Last chunk should contain "world" (normal wrapping resumes)
    assert chunks[len(chunks) - 1].text == "world"

    reconstructed = "".join(line[c.startIndex : c.endIndex] for c in chunks)
    assert reconstructed == line


# --- Kill ring ------------------------------------------------------------------------------------


def test_ctrl_w_saves_deleted_text_to_kill_ring_and_ctrl_y_yanks_it():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("foo bar baz")
    editor.handleInput("\x17")  # Ctrl+W - deletes "baz"
    assert editor.getText() == "foo bar "

    # Move to beginning and yank
    editor.handleInput("\x01")  # Ctrl+A
    editor.handleInput("\x19")  # Ctrl+Y
    assert editor.getText() == "bazfoo bar "


def test_ctrl_u_saves_deleted_text_to_kill_ring():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("hello world")
    # Move cursor to middle
    editor.handleInput("\x01")  # Ctrl+A (start)
    editor.handleInput("\x1b[C")  # Right 5 times
    editor.handleInput("\x1b[C")
    editor.handleInput("\x1b[C")
    editor.handleInput("\x1b[C")
    editor.handleInput("\x1b[C")
    editor.handleInput("\x1b[C")  # After "hello "

    editor.handleInput("\x15")  # Ctrl+U - deletes "hello "
    assert editor.getText() == "world"

    editor.handleInput("\x19")  # Ctrl+Y
    assert editor.getText() == "hello world"


def test_ctrl_k_saves_deleted_text_to_kill_ring():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("hello world")
    editor.handleInput("\x01")  # Ctrl+A (start)
    editor.handleInput("\x0b")  # Ctrl+K - deletes "hello world"

    assert editor.getText() == ""

    editor.handleInput("\x19")  # Ctrl+Y
    assert editor.getText() == "hello world"


def test_ctrl_y_does_nothing_when_kill_ring_is_empty():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("test")
    editor.handleInput("\x19")  # Ctrl+Y
    assert editor.getText() == "test"


def test_alt_y_cycles_through_kill_ring_after_ctrl_y():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    # Create kill ring with multiple entries
    editor.setText("first")
    editor.handleInput("\x17")  # Ctrl+W - deletes "first"
    editor.setText("second")
    editor.handleInput("\x17")  # Ctrl+W - deletes "second"
    editor.setText("third")
    editor.handleInput("\x17")  # Ctrl+W - deletes "third"

    # Kill ring now has: [first, second, third]
    assert editor.getText() == ""

    editor.handleInput("\x19")  # Ctrl+Y - yanks "third" (most recent)
    assert editor.getText() == "third"

    editor.handleInput("\x1by")  # Alt+Y - cycles to "second"
    assert editor.getText() == "second"

    editor.handleInput("\x1by")  # Alt+Y - cycles to "first"
    assert editor.getText() == "first"

    editor.handleInput("\x1by")  # Alt+Y - cycles back to "third"
    assert editor.getText() == "third"


def test_alt_y_does_nothing_if_not_preceded_by_yank():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("test")
    editor.handleInput("\x17")  # Ctrl+W - deletes "test"
    editor.setText("other")

    # Type something to break the yank chain
    editor.handleInput("x")
    assert editor.getText() == "otherx"

    # Alt+Y should do nothing
    editor.handleInput("\x1by")  # Alt+Y
    assert editor.getText() == "otherx"


def test_alt_y_does_nothing_if_kill_ring_has_at_most_1_entry():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("only")
    editor.handleInput("\x17")  # Ctrl+W - deletes "only"

    editor.handleInput("\x19")  # Ctrl+Y - yanks "only"
    assert editor.getText() == "only"

    editor.handleInput("\x1by")  # Alt+Y - should do nothing (only 1 entry)
    assert editor.getText() == "only"


def test_consecutive_ctrl_w_accumulates_into_one_kill_ring_entry():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("one two three")
    editor.handleInput("\x17")  # Ctrl+W - deletes "three"
    editor.handleInput("\x17")  # Ctrl+W - deletes "two " (prepended)
    editor.handleInput("\x17")  # Ctrl+W - deletes "one " (prepended)

    assert editor.getText() == ""

    # Should be one combined entry
    editor.handleInput("\x19")  # Ctrl+Y
    assert editor.getText() == "one two three"


def test_ctrl_u_accumulates_multiline_deletes_including_newlines():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    # Start with multiline text, cursor at end
    editor.setText("line1\nline2\nline3")
    # Cursor is at end of line3 (line 2, col 5)

    # Delete "line3"
    editor.handleInput("\x15")  # Ctrl+U
    assert editor.getText() == "line1\nline2\n"

    # Delete newline (at start of empty line 2, merges with line1)
    editor.handleInput("\x15")  # Ctrl+U
    assert editor.getText() == "line1\nline2"

    # Delete "line2"
    editor.handleInput("\x15")  # Ctrl+U
    assert editor.getText() == "line1\n"

    # Delete newline
    editor.handleInput("\x15")  # Ctrl+U
    assert editor.getText() == "line1"

    # Delete "line1"
    editor.handleInput("\x15")  # Ctrl+U
    assert editor.getText() == ""

    # All deletions accumulated into one entry: "line1\nline2\nline3"
    editor.handleInput("\x19")  # Ctrl+Y
    assert editor.getText() == "line1\nline2\nline3"


def test_backward_deletions_prepend_forward_deletions_append_during_accumulation():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("prefix|suffix")
    # Position cursor at |
    editor.handleInput("\x01")  # Ctrl+A
    for _i in range(6):
        editor.handleInput("\x1b[C")  # Move right 6 times

    editor.handleInput("\x0b")  # Ctrl+K - deletes "suffix" (forward)
    editor.handleInput("\x0b")  # Ctrl+K - deletes "|" (forward, appended)
    assert editor.getText() == "prefix"

    editor.handleInput("\x19")  # Ctrl+Y
    assert editor.getText() == "prefix|suffix"


def test_non_delete_actions_break_kill_accumulation():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    # Delete "baz", then type "x" to break accumulation, then delete "x"
    editor.setText("foo bar baz")
    editor.handleInput("\x17")  # Ctrl+W - deletes "baz"
    assert editor.getText() == "foo bar "

    editor.handleInput("x")  # Typing breaks accumulation
    assert editor.getText() == "foo bar x"

    editor.handleInput("\x17")  # Ctrl+W - deletes "x" (separate entry, not accumulated)
    assert editor.getText() == "foo bar "

    # Yank most recent - should be "x", not "xbaz"
    editor.handleInput("\x19")  # Ctrl+Y
    assert editor.getText() == "foo bar x"

    # Cycle to previous - should be "baz" (separate entry)
    editor.handleInput("\x1by")  # Alt+Y
    assert editor.getText() == "foo bar baz"


def test_non_yank_actions_break_alt_y_chain():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("first")
    editor.handleInput("\x17")  # Ctrl+W
    editor.setText("second")
    editor.handleInput("\x17")  # Ctrl+W
    editor.setText("")

    editor.handleInput("\x19")  # Ctrl+Y - yanks "second"
    assert editor.getText() == "second"

    editor.handleInput("x")  # Type breaks yank chain
    assert editor.getText() == "secondx"

    editor.handleInput("\x1by")  # Alt+Y - should do nothing
    assert editor.getText() == "secondx"


def test_kill_ring_rotation_persists_after_cycling():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("first")
    editor.handleInput("\x17")  # deletes "first"
    editor.setText("second")
    editor.handleInput("\x17")  # deletes "second"
    editor.setText("third")
    editor.handleInput("\x17")  # deletes "third"
    editor.setText("")

    # Ring: [first, second, third]

    editor.handleInput("\x19")  # Ctrl+Y - yanks "third"
    editor.handleInput("\x1by")  # Alt+Y - cycles to "second", ring rotates

    # Now ring is: [third, first, second]
    assert editor.getText() == "second"

    # Do something else
    editor.handleInput("x")
    editor.setText("")

    # New yank should get "second" (now at end after rotation)
    editor.handleInput("\x19")  # Ctrl+Y
    assert editor.getText() == "second"


def test_consecutive_deletions_across_lines_coalesce_into_one_entry():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    # "1\n2\n3" with cursor at end, delete everything with Ctrl+W
    editor.setText("1\n2\n3")
    editor.handleInput("\x17")  # Ctrl+W - deletes "3"
    assert editor.getText() == "1\n2\n"

    editor.handleInput("\x17")  # Ctrl+W - deletes newline (merge with prev line)
    assert editor.getText() == "1\n2"

    editor.handleInput("\x17")  # Ctrl+W - deletes "2"
    assert editor.getText() == "1\n"

    editor.handleInput("\x17")  # Ctrl+W - deletes newline
    assert editor.getText() == "1"

    editor.handleInput("\x17")  # Ctrl+W - deletes "1"
    assert editor.getText() == ""

    # All deletions should have accumulated into one entry
    editor.handleInput("\x19")  # Ctrl+Y
    assert editor.getText() == "1\n2\n3"


def test_ctrl_k_at_line_end_deletes_newline_and_coalesces():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    # "ab" on line 1, "cd" on line 2, cursor at end of line 1
    editor.setText("")
    editor.handleInput("a")
    editor.handleInput("b")
    editor.handleInput("\n")
    editor.handleInput("c")
    editor.handleInput("d")
    # Move to end of first line
    editor.handleInput("\x1b[A")  # Up arrow
    editor.handleInput("\x05")  # Ctrl+E - end of line

    # Now at end of "ab", Ctrl+K should delete newline (merge with "cd")
    editor.handleInput("\x0b")  # Ctrl+K - deletes newline
    assert editor.getText() == "abcd"

    # Continue deleting
    editor.handleInput("\x0b")  # Ctrl+K - deletes "cd"
    assert editor.getText() == "ab"

    # Both deletions should accumulate
    editor.handleInput("\x19")  # Ctrl+Y
    assert editor.getText() == "ab\ncd"


def test_handles_yank_in_middle_of_text():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("word")
    editor.handleInput("\x17")  # Ctrl+W - deletes "word"
    editor.setText("hello world")

    # Move to middle (after "hello ")
    editor.handleInput("\x01")  # Ctrl+A
    for _i in range(6):
        editor.handleInput("\x1b[C")

    editor.handleInput("\x19")  # Ctrl+Y
    assert editor.getText() == "hello wordworld"


def test_handles_yank_pop_in_middle_of_text():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    # Create two kill ring entries
    editor.setText("FIRST")
    editor.handleInput("\x17")  # Ctrl+W - deletes "FIRST"
    editor.setText("SECOND")
    editor.handleInput("\x17")  # Ctrl+W - deletes "SECOND"

    # Ring: ["FIRST", "SECOND"]

    # Set up "hello world" and position cursor after "hello "
    editor.setText("hello world")
    editor.handleInput("\x01")  # Ctrl+A - go to start of line
    for _i in range(6):
        editor.handleInput("\x1b[C")  # Move right 6

    # Yank "SECOND" in the middle
    editor.handleInput("\x19")  # Ctrl+Y
    assert editor.getText() == "hello SECONDworld"

    # Yank-pop replaces "SECOND" with "FIRST"
    editor.handleInput("\x1by")  # Alt+Y
    assert editor.getText() == "hello FIRSTworld"


def test_multiline_yank_and_yank_pop_in_middle_of_text():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    # Create single-line entry
    editor.setText("SINGLE")
    editor.handleInput("\x17")  # Ctrl+W - deletes "SINGLE"

    # Create multiline entry via consecutive Ctrl+U
    editor.setText("A\nB")
    editor.handleInput("\x15")  # Ctrl+U - deletes "B"
    editor.handleInput("\x15")  # Ctrl+U - deletes newline
    editor.handleInput("\x15")  # Ctrl+U - deletes "A"
    # Ring: ["SINGLE", "A\nB"]

    # Insert in middle of "hello world"
    editor.setText("hello world")
    editor.handleInput("\x01")  # Ctrl+A
    for _i in range(6):
        editor.handleInput("\x1b[C")

    # Yank multiline "A\nB"
    editor.handleInput("\x19")  # Ctrl+Y
    assert editor.getText() == "hello A\nBworld"

    # Yank-pop replaces with "SINGLE"
    editor.handleInput("\x1by")  # Alt+Y
    assert editor.getText() == "hello SINGLEworld"


def test_alt_d_deletes_word_forward_and_saves_to_kill_ring():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("hello world test")
    editor.handleInput("\x01")  # Ctrl+A - go to start

    editor.handleInput("\x1bd")  # Alt+D - deletes "hello"
    assert editor.getText() == " world test"

    editor.handleInput("\x1bd")  # Alt+D - deletes " world" (skips whitespace, then word)
    assert editor.getText() == " test"

    # Yank should get accumulated text
    editor.handleInput("\x19")  # Ctrl+Y
    assert editor.getText() == "hello world test"


def test_alt_d_at_end_of_line_deletes_newline():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("line1\nline2")
    # Move to start of document, then to end of first line
    editor.handleInput("\x1b[A")  # Up arrow - go to first line
    editor.handleInput("\x05")  # Ctrl+E - end of line

    editor.handleInput("\x1bd")  # Alt+D - deletes newline (merges lines)
    assert editor.getText() == "line1line2"

    editor.handleInput("\x19")  # Ctrl+Y
    assert editor.getText() == "line1\nline2"


# --- Undo -----------------------------------------------------------------------------------------

UNDO = "\x1b[45;5u"  # Ctrl+- (undo)


def _type(editor: Editor, text: str) -> None:
    for ch in text:
        editor.handleInput(ch)


def test_undo_does_nothing_when_undo_stack_is_empty():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.handleInput(UNDO)  # Ctrl+- (undo)
    assert editor.getText() == ""


def test_coalesces_consecutive_word_characters_into_one_undo_unit():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    _type(editor, "hello world")
    assert editor.getText() == "hello world"

    # Undo removes " world" (space captured state before it, so we restore to "hello")
    editor.handleInput(UNDO)  # Ctrl+- (undo)
    assert editor.getText() == "hello"

    # Undo removes "hello"
    editor.handleInput(UNDO)  # Ctrl+- (undo)
    assert editor.getText() == ""


def test_undoes_spaces_one_at_a_time():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    _type(editor, "hello  ")
    assert editor.getText() == "hello  "

    editor.handleInput(UNDO)  # Ctrl+- (undo) - removes second " "
    assert editor.getText() == "hello "

    editor.handleInput(UNDO)  # Ctrl+- (undo) - removes first " "
    assert editor.getText() == "hello"

    editor.handleInput(UNDO)  # Ctrl+- (undo) - removes "hello"
    assert editor.getText() == ""


def test_undoes_newlines_and_signals_next_word_to_capture_state():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    _type(editor, "hello\nworld")
    assert editor.getText() == "hello\nworld"

    editor.handleInput(UNDO)  # Ctrl+- (undo)
    assert editor.getText() == "hello\n"

    editor.handleInput(UNDO)  # Ctrl+- (undo)
    assert editor.getText() == "hello"

    editor.handleInput(UNDO)  # Ctrl+- (undo)
    assert editor.getText() == ""


def test_undoes_backspace():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    _type(editor, "hello")
    editor.handleInput("\x7f")  # Backspace
    assert editor.getText() == "hell"

    editor.handleInput(UNDO)  # Ctrl+- (undo)
    assert editor.getText() == "hello"


def test_undoes_forward_delete():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    _type(editor, "hello")
    editor.handleInput("\x01")  # Ctrl+A - go to start
    editor.handleInput("\x1b[C")  # Right arrow
    editor.handleInput("\x1b[3~")  # Delete key
    assert editor.getText() == "hllo"

    editor.handleInput(UNDO)  # Ctrl+- (undo)
    assert editor.getText() == "hello"


def test_undoes_ctrl_w_delete_word_backward():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    _type(editor, "hello world")
    assert editor.getText() == "hello world"

    editor.handleInput("\x17")  # Ctrl+W
    assert editor.getText() == "hello "

    editor.handleInput(UNDO)  # Ctrl+- (undo)
    assert editor.getText() == "hello world"


def test_undoes_ctrl_k_delete_to_line_end():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    _type(editor, "hello world")
    editor.handleInput("\x01")  # Ctrl+A - go to start
    for _i in range(6):
        editor.handleInput("\x1b[C")  # Move right 6 times

    editor.handleInput("\x0b")  # Ctrl+K
    assert editor.getText() == "hello "

    editor.handleInput(UNDO)  # Ctrl+- (undo)
    assert editor.getText() == "hello world"

    editor.handleInput("|")
    assert editor.getText() == "hello |world"


def test_undoes_ctrl_u_delete_to_line_start():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    _type(editor, "hello world")
    editor.handleInput("\x01")  # Ctrl+A - go to start
    for _i in range(6):
        editor.handleInput("\x1b[C")  # Move right 6 times

    editor.handleInput("\x15")  # Ctrl+U
    assert editor.getText() == "world"

    editor.handleInput(UNDO)  # Ctrl+- (undo)
    assert editor.getText() == "hello world"


def test_undoes_yank():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    _type(editor, "hello ")
    editor.handleInput("\x17")  # Ctrl+W - delete "hello "
    editor.handleInput("\x19")  # Ctrl+Y - yank
    assert editor.getText() == "hello "

    editor.handleInput(UNDO)  # Ctrl+- (undo)
    assert editor.getText() == ""


def test_undoes_single_line_paste_atomically():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("hello world")
    editor.handleInput("\x01")  # Ctrl+A - go to start
    for _i in range(5):
        editor.handleInput("\x1b[C")  # Move right 5 (after "hello", before space)

    # Simulate bracketed paste of "beep boop"
    editor.handleInput("\x1b[200~beep boop\x1b[201~")
    assert editor.getText() == "hellobeep boop world"

    # Single undo should restore entire pre-paste state
    editor.handleInput(UNDO)  # Ctrl+- (undo)
    assert editor.getText() == "hello world"

    editor.handleInput("|")
    assert editor.getText() == "hello| world"


async def test_does_not_trigger_autocomplete_during_single_line_paste():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    suggestionCalls = 0

    async def getSuggestions(*_args: Any) -> None:
        nonlocal suggestionCalls
        suggestionCalls += 1

    mockProvider = Provider(getSuggestions)

    editor.setAutocompleteProvider(mockProvider)  # type: ignore[arg-type]
    editor.handleInput("\x1b[200~look at @node_modules/react/index.js please\x1b[201~")

    assert editor.getText() == "look at @node_modules/react/index.js please"
    assert suggestionCalls == 0
    assert editor.isShowingAutocomplete() is False


def test_decodes_csi_u_ctrl_letter_sequences_inside_bracketed_paste_tmux_popup():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    # tmux popups with extended-keys-format=csi-u re-encode \n in pastes as
    # \x1b[106;5u (Ctrl+J). Without decoding, the per-char filter strips ESC
    # and leaks "[106;5u" between lines. See issue #3599.
    editor.handleInput("\x1b[200~line1\x1b[106;5uline2\x1b[106;5uline3\x1b[201~")
    assert editor.getText() == "line1\nline2\nline3"


def test_undoes_multi_line_paste_atomically():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("hello world")
    editor.handleInput("\x01")  # Ctrl+A - go to start
    for _i in range(5):
        editor.handleInput("\x1b[C")  # Move right 5 (after "hello", before space)

    # Simulate bracketed paste of multi-line text
    editor.handleInput("\x1b[200~line1\nline2\nline3\x1b[201~")
    assert editor.getText() == "helloline1\nline2\nline3 world"

    # Single undo should restore entire pre-paste state
    editor.handleInput(UNDO)  # Ctrl+- (undo)
    assert editor.getText() == "hello world"

    editor.handleInput("|")
    assert editor.getText() == "hello| world"


def test_undoes_inserttextatcursor_atomically():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("hello world")
    editor.handleInput("\x01")  # Ctrl+A - go to start
    for _i in range(5):
        editor.handleInput("\x1b[C")  # Move right 5 (after "hello", before space)

    # Programmatic insertion (e.g., clipboard image path)
    editor.insertTextAtCursor("/tmp/image.png")
    assert editor.getText() == "hello/tmp/image.png world"

    # Single undo should restore entire pre-insert state
    editor.handleInput(UNDO)  # Ctrl+- (undo)
    assert editor.getText() == "hello world"

    editor.handleInput("|")
    assert editor.getText() == "hello| world"


def test_inserttextatcursor_handles_multiline_text():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("hello world")
    editor.handleInput("\x01")  # Ctrl+A - go to start
    for _i in range(5):
        editor.handleInput("\x1b[C")  # Move right 5 (after "hello", before space)

    # Insert multiline text
    editor.insertTextAtCursor("line1\nline2\nline3")
    assert editor.getText() == "helloline1\nline2\nline3 world"

    # Cursor should be at end of inserted text (after "line3", before " world")
    cursor = editor.getCursor()
    assert cursor["line"] == 2
    assert cursor["col"] == 5  # "line3".length

    # Single undo should restore entire pre-insert state
    editor.handleInput(UNDO)  # Ctrl+- (undo)
    assert editor.getText() == "hello world"


def test_inserttextatcursor_normalizes_crlf_and_cr_line_endings():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("")

    # Insert text with CRLF
    editor.insertTextAtCursor("a\r\nb\r\nc")
    assert editor.getText() == "a\nb\nc"

    editor.handleInput(UNDO)  # Undo
    assert editor.getText() == ""

    # Insert text with CR only
    editor.insertTextAtCursor("x\ry\rz")
    assert editor.getText() == "x\ny\nz"


def test_undoes_settext_to_empty_string():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    _type(editor, "hello world")
    assert editor.getText() == "hello world"

    editor.setText("")
    assert editor.getText() == ""

    editor.handleInput(UNDO)  # Ctrl+- (undo)
    assert editor.getText() == "hello world"


def test_clears_undo_stack_on_submit():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    submitted = ""

    def onSubmit(text: str) -> None:
        nonlocal submitted
        submitted = text

    editor.onSubmit = onSubmit

    _type(editor, "hello")
    editor.handleInput("\r")  # Enter - submit

    assert submitted == "hello"
    assert editor.getText() == ""

    # Undo should do nothing - stack was cleared
    editor.handleInput(UNDO)  # Ctrl+- (undo)
    assert editor.getText() == ""


def test_exits_history_browsing_mode_on_undo():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    # Add "hello" to history
    editor.addToHistory("hello")
    assert editor.getText() == ""

    # Type "world"
    _type(editor, "world")
    assert editor.getText() == "world"

    # Ctrl+W - delete word
    editor.handleInput("\x17")  # Ctrl+W
    assert editor.getText() == ""

    # Press Up - enter history browsing, shows "hello"
    editor.handleInput("\x1b[A")  # Up arrow
    assert editor.getText() == "hello"

    # Undo should restore to "" (state before entering history browsing)
    editor.handleInput(UNDO)  # Ctrl+- (undo)
    assert editor.getText() == ""

    # Undo again should restore to "world" (state before Ctrl+W)
    editor.handleInput(UNDO)  # Ctrl+- (undo)
    assert editor.getText() == "world"


def test_undo_restores_to_pre_history_state_even_after_multiple_history_navigations():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    # Add history entries
    editor.addToHistory("first")
    editor.addToHistory("second")
    editor.addToHistory("third")

    # Type something
    _type(editor, "current")
    assert editor.getText() == "current"

    # Clear editor
    editor.handleInput("\x17")  # Ctrl+W
    assert editor.getText() == ""

    # Navigate through history multiple times
    editor.handleInput("\x1b[A")  # Up - "third"
    assert editor.getText() == "third"
    editor.handleInput("\x1b[A")  # Up - "second"
    assert editor.getText() == "second"
    editor.handleInput("\x1b[A")  # Up - "first"
    assert editor.getText() == "first"

    # Undo should go back to "" (state before we started browsing), not intermediate states
    editor.handleInput(UNDO)  # Ctrl+- (undo)
    assert editor.getText() == ""

    # Another undo goes back to "current"
    editor.handleInput(UNDO)  # Ctrl+- (undo)
    assert editor.getText() == "current"


def test_cursor_movement_starts_new_undo_unit():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    _type(editor, "hello world")
    assert editor.getText() == "hello world"

    # Move cursor left 5 (to after "hello ")
    for _i in range(5):
        editor.handleInput("\x1b[D")

    # Type "lol" in the middle
    _type(editor, "lol")
    assert editor.getText() == "hello lolworld"

    # Undo should restore to "hello world" (before inserting "lol")
    editor.handleInput(UNDO)  # Ctrl+- (undo)
    assert editor.getText() == "hello world"

    editor.handleInput("|")
    assert editor.getText() == "hello |world"


def test_no_op_delete_operations_do_not_push_undo_snapshots():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    _type(editor, "hello")
    assert editor.getText() == "hello"

    # Delete word on empty - multiple times (should be no-ops)
    editor.handleInput("\x17")  # Ctrl+W - deletes "hello"
    assert editor.getText() == ""
    editor.handleInput("\x17")  # Ctrl+W - no-op (nothing to delete)
    editor.handleInput("\x17")  # Ctrl+W - no-op

    # Single undo should restore "hello"
    editor.handleInput(UNDO)  # Ctrl+- (undo)
    assert editor.getText() == "hello"


async def test_undoes_autocomplete():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    # Create a mock autocomplete provider
    async def getSuggestions(lines: list[str], _cursorLine: int, cursorCol: int, _options: Any) -> Any:
        text = lines[0] or ""
        prefix = text[:cursorCol]
        if prefix == "di":
            return AutocompleteSuggestions(items=items("dist/"), prefix="di")
        return None

    mockProvider = Provider(getSuggestions)

    editor.setAutocompleteProvider(mockProvider)  # type: ignore[arg-type]

    # Type "di"
    editor.handleInput("d")
    editor.handleInput("i")
    assert editor.getText() == "di"

    # Press Tab to trigger autocomplete
    editor.handleInput("\t")
    await flushAutocomplete()
    assert editor.getText() == "dist/"
    assert editor.isShowingAutocomplete() is False

    # Undo should restore to "di"
    editor.handleInput(UNDO)  # Ctrl+- (undo)
    assert editor.getText() == "di"


# --- Autocomplete ---------------------------------------------------------------------------------


async def test_triggers_and_debounces_symbol_completion_after_cjk_punctuation():
    with mockTimers() as timers:
        for before in [
            "查看，",
            "　",
            *"，．：；！？（）［］｛｝“”‘’…—。、「」『』《》【】",
            "(",
            "see (",
            "`",
            "[",
        ]:
            for trigger in ["@", "#", "$", "-"]:
                editor = Editor(createTestTUI(), defaultEditorTheme)
                requests: list[str] = []

                def getSuggestions(lines: list[str], cursorLine: int, cursorCol: int, _options: Any, requests=requests) -> None:
                    requests.append(lines[cursorLine][:cursorCol])

                editor.setAutocompleteProvider(Provider(getSuggestions, triggerCharacters=["$", "-"]))  # type: ignore[arg-type]
                editor.setText(before)
                editor.handleInput(trigger)
                timers.tick(19)
                await flushAutocomplete()
                assert requests == []
                timers.tick(1)
                await flushAutocomplete()
                assert requests == [before + trigger]

                editor.handleInput("r")
                editor.handleInput("e")
                timers.tick(19)
                await flushAutocomplete()
                assert len(requests) == 1
                timers.tick(1)
                await flushAutocomplete()
                assert requests == [before + trigger, f"{before}{trigger}re"]


async def test_does_not_auto_trigger_after_cjk_letters_or_for_unprefixed_paths():
    with mockTimers() as timers:
        editor = Editor(createTestTUI(), defaultEditorTheme)
        requests = 0

        def getSuggestions(*_args: Any) -> None:
            nonlocal requests
            requests += 1

        editor.setAutocompleteProvider(Provider(getSuggestions))  # type: ignore[arg-type]
        for text in [
            "user@example.com",
            "张三@example.com",
            "查看@src",
            "あ@src",
            "カ@src",
            "한@src",
            "ㄅ@src",
            "𠮷@src",
            "が@src",
            "禰\U000e0100@src",
            "々@src",
            "Ａ@src",
            "文档@备份",
            "prefix#123",
            "foo(@src",
            "问题#123",
            "查看，/path/",
            "查看，./文档/",
            "src/index.ts",
            "./文档/说明.md",
            "文档/说明.md",
            "查看src/index.ts",
        ]:
            editor.setText("")
            for char in text:
                editor.handleInput(char)
            timers.tick(20)
            await flushAutocomplete()
            assert requests == 0, text


async def test_requests_path_completion_after_cjk_punctuation_only_on_tab():
    with mockTimers() as timers:
        editor = Editor(createTestTUI(), defaultEditorTheme)
        requests: list[dict[str, Any]] = []

        def getSuggestions(lines: list[str], cursorLine: int, cursorCol: int, options: Any) -> None:
            requests.append({"text": lines[cursorLine][:cursorCol], "force": options.get("force")})

        editor.setAutocompleteProvider(Provider(getSuggestions))  # type: ignore[arg-type]
        text = "查看，/path/"
        for char in text:
            editor.handleInput(char)
        timers.tick(20)
        await flushAutocomplete()
        assert requests == []
        editor.handleInput("\t")
        await flushAutocomplete()
        assert requests == [{"text": text, "force": True}]


async def test_completes_chinese_path_prefixes_after_whitespace_or_cjk_punctuation_with_tab():
    baseDir = tempfile.mkdtemp(prefix="pi-editor-autocomplete-")
    try:
        os.mkdir(os.path.join(baseDir, "文档"))
        Path(baseDir, "文档", "说明.md").write_text("text", encoding="utf-8")
        editor = Editor(createTestTUI(), defaultEditorTheme)
        editor.setAutocompleteProvider(CombinedAutocompleteProvider([], baseDir))
        for separator in [" ", "\t", "　", " ", "，", "。"]:
            editor.setText(f"查看{separator}")
            before = editor.getText()
            editor.handleInput("文")
            editor.handleInput("\t")
            await flushAutocomplete()
            assert editor.getText() == f"{before}文档/"
            editor.handleInput("说")
            editor.handleInput("\t")
            await flushAutocomplete()
            assert editor.getText() == f"{before}文档/说明.md"
            assert editor.getCursor() == {"line": 0, "col": len(editor.getText())}
    finally:
        shutil.rmtree(baseDir, ignore_errors=True)


async def test_ends_unquoted_trigger_and_debounce_contexts_at_whitespace_or_cjk_punctuation():
    with mockTimers() as timers:
        for separator in [" ", "　", "，", "。"]:
            for trigger in ["@", "#", "$"]:
                editor = Editor(createTestTUI(), defaultEditorTheme)
                requests: list[str] = []
                prefix = f"{trigger}src"

                def getSuggestions(
                    lines: list[str], cursorLine: int, cursorCol: int, _options: Any, requests=requests, prefix=prefix
                ) -> Any:
                    text = lines[cursorLine][:cursorCol]
                    requests.append(text)
                    return (
                        AutocompleteSuggestions(prefix=prefix, items=[AutocompleteItem(value=f"{prefix}/", label="src/")])
                        if text == prefix
                        else None
                    )

                editor.setAutocompleteProvider(Provider(getSuggestions, triggerCharacters=["$"]))  # type: ignore[arg-type]
                editor.setText(f"{trigger}sr")
                editor.handleInput("c")
                timers.tick(20)
                await flushAutocomplete()
                assert editor.isShowingAutocomplete() is True
                editor.handleInput(separator)
                await flushAutocomplete()
                assert requests == [prefix, prefix + separator]
                assert editor.isShowingAutocomplete() is False
                editor.handleInput("文")
                timers.tick(20)
                await flushAutocomplete()
                assert requests == [prefix, prefix + separator]


async def test_re_triggers_cjk_path_completion_after_accepting_directories_and_deleting():
    with mockTimers() as timers:
        provider = CombinedAutocompleteProvider([], os.getcwd())
        for directory in ["文档", "我的 文档", "资料，归档"]:
            quoted = directory != "文档"
            initial = f'@"{directory[:2]}' if quoted else "@文"
            directoryValue = f'@"{directory}/"' if quoted else f"@{directory}/"
            fileValue = f'@"{directory}/说明.md"' if quoted else f"@{directory}/说明.md"
            filePrefix = f'@"{directory}/说' if quoted else f"@{directory}/说"
            editor = Editor(createTestTUI(), defaultEditorTheme)

            def getSuggestions(
                lines: list[str],
                cursorLine: int,
                cursorCol: int,
                _options: Any,
                initial=initial,
                directory=directory,
                directoryValue=directoryValue,
                fileValue=fileValue,
                filePrefix=filePrefix,
            ) -> Any:
                before = lines[cursorLine][:cursorCol]
                prefix = before[before.find("@") :]
                if prefix == initial:
                    return AutocompleteSuggestions(
                        prefix=prefix, items=[AutocompleteItem(value=directoryValue, label=f"{directory}/")]
                    )
                return (
                    AutocompleteSuggestions(prefix=prefix, items=[AutocompleteItem(value=fileValue, label="说明.md")])
                    if prefix == filePrefix
                    else None
                )

            editor.setAutocompleteProvider(
                Provider(getSuggestions, applyCompletion=lambda *args: provider.applyCompletion(*args))  # type: ignore[arg-type]
            )
            editor.setText(f"查看：{initial}")
            editor.handleInput("\t")
            await flushAutocomplete()
            assert editor.getText() == f"查看：{directoryValue}"
            assert editor.isShowingAutocomplete() is False

            editor.handleInput("说")
            timers.tick(20)
            await flushAutocomplete()
            assert editor.isShowingAutocomplete() is True

            for deletion in ["\x7f", "\x1b[3~"]:
                editor.handleInput("错")
                timers.tick(20)
                await flushAutocomplete()
                assert editor.isShowingAutocomplete() is False
                if deletion == "\x1b[3~":
                    editor.handleInput("\x1b[D")
                editor.handleInput(deletion)
                timers.tick(20)
                await flushAutocomplete()
                assert editor.isShowingAutocomplete() is True

            editor.handleInput("\t")
            assert editor.getText() == f"查看：{fileValue} "
            assert editor.getCursor() == {"line": 0, "col": len(editor.getText())}


async def test_auto_applies_single_force_file_suggestion_without_showing_menu():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    def getSuggestions(lines: list[str], _cursorLine: int, cursorCol: int, options: Any) -> Any:
        if not options.get("force"):
            return None
        text = lines[0] or ""
        prefix = text[:cursorCol]
        if prefix == "Work":
            return AutocompleteSuggestions(items=items("Workspace/"), prefix="Work")
        return None

    editor.setAutocompleteProvider(Provider(getSuggestions))  # type: ignore[arg-type]

    # Type "Work"
    _type(editor, "Work")
    assert editor.getText() == "Work"

    # Press Tab - should auto-apply without showing menu
    editor.handleInput("\t")
    await flushAutocomplete()
    assert editor.getText() == "Workspace/"
    assert editor.isShowingAutocomplete() is False

    # Undo should restore to "Work"
    editor.handleInput(UNDO)  # Ctrl+- (undo)
    assert editor.getText() == "Work"


async def test_shows_menu_when_force_file_has_multiple_suggestions():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    def getSuggestions(lines: list[str], _cursorLine: int, cursorCol: int, options: Any) -> Any:
        if not options.get("force"):
            return None
        text = lines[0] or ""
        prefix = text[:cursorCol]
        if prefix == "src":
            return AutocompleteSuggestions(items=items("src/", "src.txt"), prefix="src")
        return None

    editor.setAutocompleteProvider(Provider(getSuggestions))  # type: ignore[arg-type]

    # Type "src"
    _type(editor, "src")
    assert editor.getText() == "src"

    # Press Tab - should show menu because there are multiple suggestions
    editor.handleInput("\t")
    await flushAutocomplete()
    assert editor.getText() == "src"
    assert editor.isShowingAutocomplete() is True

    # Press Tab again to accept first suggestion
    editor.handleInput("\t")
    assert editor.getText() == "src/"
    assert editor.isShowingAutocomplete() is False


async def test_keeps_suggestions_open_when_typing_in_force_mode_tab_triggered():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    allFiles = items("readme.md", "package.json", "src/", "dist/")

    def getSuggestions(lines: list[str], _cursorLine: int, cursorCol: int, options: Any) -> Any:
        text = lines[0] or ""
        prefix = text[:cursorCol]
        shouldMatch = options.get("force") or "/" in prefix or prefix.startswith(".")
        if not shouldMatch:
            return None
        filtered = [f for f in allFiles if f.value.lower().startswith(prefix.lower())]
        if len(filtered) > 0:
            return AutocompleteSuggestions(items=filtered, prefix=prefix)
        return None

    editor.setAutocompleteProvider(Provider(getSuggestions))  # type: ignore[arg-type]

    # Press Tab on empty prompt - should show all files (force mode)
    editor.handleInput("\t")
    await flushAutocomplete()
    assert editor.isShowingAutocomplete() is True

    # Type "r" - should narrow to "readme.md" (force mode keeps suggestions open)
    editor.handleInput("r")
    await flushAutocomplete()
    assert editor.getText() == "r"
    assert editor.isShowingAutocomplete() is True

    # Type "e" - should still show "readme.md"
    editor.handleInput("e")
    await flushAutocomplete()
    assert editor.getText() == "re"
    assert editor.isShowingAutocomplete() is True

    # Accept with Tab
    editor.handleInput("\t")
    assert editor.getText() == "readme.md"
    assert editor.isShowingAutocomplete() is False


async def test_debounces_at_autocomplete_while_typing():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    suggestionCalls = 0

    def getSuggestions(lines: list[str], _cursorLine: int, cursorCol: int, _options: Any) -> Any:
        nonlocal suggestionCalls
        suggestionCalls += 1
        text = (lines[0] or "")[:cursorCol]
        return AutocompleteSuggestions(items=[AutocompleteItem(value="@main.ts", label="main.ts")], prefix=text)

    editor.setAutocompleteProvider(Provider(getSuggestions))  # type: ignore[arg-type]

    editor.handleInput("@")
    editor.handleInput("m")
    editor.handleInput("a")
    editor.handleInput("i")

    assert suggestionCalls == 0
    assert editor.isShowingAutocomplete() is False

    await asyncio.sleep(0.05)
    await flushAutocomplete()

    assert suggestionCalls == 1
    assert editor.isShowingAutocomplete() is True


async def test_re_queries_the_autocomplete_picker_when_the_cursor_moves_back_into_the_command_name():
    # Regression for earendil-works/pi#5496: arrowing left out of a slash
    # command's argument region must re-query the picker, not leave the
    # stale argument list showing. Before the fix, moveCursor() never
    # called updateAutocomplete(), so `/cmd ` (argument menu) + Left kept
    # displaying the arguments against a `/cmd` prefix — and a Tab there
    # would concatenate the stale suggestion onto the partial command name.
    editor = Editor(createTestTUI(), defaultEditorTheme)

    def getSuggestions(lines: list[str], _cursorLine: int, cursorCol: int, _options: Any) -> Any:
        before = (lines[0] or "")[:cursorCol]
        if not before.startswith("/"):
            return None
        # Past the command name (a space before the cursor): offer arguments.
        if " " in before:
            return AutocompleteSuggestions(items=items("repo", "message", "help"), prefix=before[before.find(" ") + 1 :])
        # Inside the command name: offer the command name only.
        return AutocompleteSuggestions(items=items("cmd"), prefix=before)

    editor.setAutocompleteProvider(Provider(getSuggestions))  # type: ignore[arg-type]

    # Type `/cmd ` so the picker ends up showing the argument list.
    for ch in "/cmd ":
        editor.handleInput(ch)
        await flushAutocomplete()
    assert editor.getText() == "/cmd "
    assert editor.isShowingAutocomplete() is True
    atArg = "\n".join(stripVTControlCharacters(line) for line in editor.render(80))
    assert "repo" in atArg, "argument menu should be visible at `/cmd `"

    # Arrow Left back into the command name (`/cmd`).
    editor.handleInput("\x1b[D")
    await flushAutocomplete()

    # The picker must have re-queried: the stale argument items are gone
    # (replaced by the command-name suggestion, or the picker closed).
    afterMove = "\n".join(stripVTControlCharacters(line) for line in editor.render(80))
    assert "repo" not in afterMove, "stale argument menu must not survive the cursor move"
    assert "message" not in afterMove, "stale argument menu must not survive the cursor move"


async def test_debounces_hash_autocomplete_while_typing():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    suggestionCalls = 0

    def getSuggestions(lines: list[str], _cursorLine: int, cursorCol: int, _options: Any) -> Any:
        nonlocal suggestionCalls
        suggestionCalls += 1
        text = (lines[0] or "")[:cursorCol]
        return AutocompleteSuggestions(items=items("#2983"), prefix=text)

    editor.setAutocompleteProvider(Provider(getSuggestions))  # type: ignore[arg-type]

    editor.handleInput("#")
    editor.handleInput("2")
    editor.handleInput("9")
    editor.handleInput("8")

    assert suggestionCalls == 0
    assert editor.isShowingAutocomplete() is False

    await asyncio.sleep(0.05)
    await flushAutocomplete()

    assert suggestionCalls == 1
    assert editor.isShowingAutocomplete() is True


async def test_debounces_custom_triggercharacters_autocomplete_while_typing():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    suggestionCalls = 0

    def getSuggestions(lines: list[str], _cursorLine: int, cursorCol: int, _options: Any) -> Any:
        nonlocal suggestionCalls
        suggestionCalls += 1
        prefix = (lines[0] or "")[:cursorCol]
        return AutocompleteSuggestions(items=[AutocompleteItem(value="$skill-name", label="skill-name")], prefix=prefix)

    editor.setAutocompleteProvider(Provider(getSuggestions, triggerCharacters=["$"]))  # type: ignore[arg-type]

    editor.handleInput("$")
    editor.handleInput("s")
    editor.handleInput("k")

    assert suggestionCalls == 0
    await asyncio.sleep(0.05)
    await flushAutocomplete()

    assert suggestionCalls == 1
    assert editor.isShowingAutocomplete() is True


async def test_resets_custom_triggercharacters_when_provider_changes():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    suggestionCalls = 0

    editor.setAutocompleteProvider(
        Provider(  # type: ignore[arg-type]
            lambda *_args: AutocompleteSuggestions(
                items=[AutocompleteItem(value="$skill-name", label="skill-name")], prefix="$"
            ),
            triggerCharacters=["$"],
        )
    )

    def getSuggestions(*_args: Any) -> Any:
        nonlocal suggestionCalls
        suggestionCalls += 1
        return AutocompleteSuggestions(items=[AutocompleteItem(value="$skill-name", label="skill-name")], prefix="$")

    editor.setAutocompleteProvider(Provider(getSuggestions))  # type: ignore[arg-type]

    editor.handleInput("$")
    editor.handleInput("s")
    await asyncio.sleep(0.05)
    await flushAutocomplete()

    assert suggestionCalls == 0
    assert editor.isShowingAutocomplete() is False


async def test_aborts_active_at_autocomplete_when_typing_continues():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    aborts = 0

    async def getSuggestions(_lines: list[str], _cursorLine: int, _cursorCol: int, options: Any) -> Any:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()

        def resolve(value: Any) -> None:
            if not future.done():
                future.set_result(value)

        timeout = loop.call_later(
            0.5,
            lambda: resolve(AutocompleteSuggestions(items=[AutocompleteItem(value="@main.ts", label="main.ts")], prefix="@main")),
        )

        def onAbort() -> None:
            nonlocal aborts
            aborts += 1
            timeout.cancel()
            resolve(None)

        options["signal"].addEventListener("abort", onAbort, True)
        return await future

    editor.setAutocompleteProvider(Provider(getSuggestions))  # type: ignore[arg-type]

    editor.handleInput("@")
    editor.handleInput("m")
    editor.handleInput("a")
    editor.handleInput("i")
    await asyncio.sleep(0.25)
    editor.handleInput("n")
    await asyncio.sleep(0.05)

    assert aborts == 1


async def test_hides_autocomplete_when_backspacing_slash_command_to_empty():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    # Mock provider with slash commands
    def getSuggestions(lines: list[str], _cursorLine: int, cursorCol: int, _options: Any) -> Any:
        text = lines[0] or ""
        prefix = text[:cursorCol]
        # Only return slash command suggestions when line starts with /
        if prefix.startswith("/"):
            commands = [
                AutocompleteItem(value="/model", label="model", description="Change model"),
                AutocompleteItem(value="/help", label="help", description="Show help"),
            ]
            query = prefix[1:]  # Remove leading /
            filtered = [c for c in commands if c.value.startswith(query)]
            if len(filtered) > 0:
                return AutocompleteSuggestions(items=filtered, prefix=prefix)
        return None

    editor.setAutocompleteProvider(Provider(getSuggestions))  # type: ignore[arg-type]

    # Type "/" - should show slash command suggestions
    editor.handleInput("/")
    await flushAutocomplete()
    assert editor.getText() == "/"
    assert editor.isShowingAutocomplete() is True

    # Backspace to delete "/" - should hide autocomplete completely
    editor.handleInput("\x7f")  # Backspace
    await flushAutocomplete()
    assert editor.getText() == ""
    assert editor.isShowingAutocomplete() is False


def _argtestProvider(allArguments: list[AutocompleteItem], *, filterByPrefix: bool, command: str = "argtest") -> Provider:
    def getSuggestions(lines: list[str], _cursorLine: int, cursorCol: int, _options: Any) -> Any:
        text = lines[0] or ""
        beforeCursor = text[:cursorCol]

        # Check if we're in argument completion context: "/argtest <prefix>"
        argtestMatch = re.match(rf"^/{command}\s+(\S+)$", beforeCursor)
        if argtestMatch:
            argumentText = argtestMatch[1]
            if not filterByPrefix:
                # Return all items - provider does not filter
                return AutocompleteSuggestions(items=allArguments, prefix=argumentText)
            # Return all arguments that start with the typed prefix
            filtered = [arg for arg in allArguments if arg.value.startswith(argumentText)]
            if len(filtered) > 0:
                return AutocompleteSuggestions(items=filtered, prefix=argumentText)
        return None

    return Provider(getSuggestions)


async def test_applies_exact_typed_slash_argument_value_on_enter_even_when_first_item_is_highlighted():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    # Mock provider for /argtest command with argument completions
    editor.setAutocompleteProvider(_argtestProvider(items("one", "two", "three"), filterByPrefix=True))  # type: ignore[arg-type]

    # Type "/argtest two"
    _type(editor, "/argtest two")

    assert editor.getText() == "/argtest two"
    await flushAutocomplete()
    assert editor.isShowingAutocomplete() is True

    # Press Enter - should apply the exact typed value "two", not the first item
    editor.handleInput("\r")

    # The exact typed value "two" should be retained
    assert editor.getText() == "/argtest two"


async def test_selects_first_prefix_match_on_enter_when_typed_arg_is_not_exact_match():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    # Mock provider for /argtest command with argument completions
    editor.setAutocompleteProvider(_argtestProvider(items("two", "three", "twelve"), filterByPrefix=True))  # type: ignore[arg-type]

    # Type "/argtest t" - filtered to [two, three, twelve], prefix "t" matches "two" first
    _type(editor, "/argtest t")

    await flushAutocomplete()
    assert editor.isShowingAutocomplete() is True

    # Press Enter - "t" prefix matches "two" (first in list), so "two" is applied
    editor.handleInput("\r")
    assert editor.getText() == "/argtest two"


async def test_highlights_unique_prefix_match_as_user_types_before_full_exact_match():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    # Mock provider that returns all items unfiltered (like real extensions do)
    editor.setAutocompleteProvider(_argtestProvider(items("one", "two", "three"), filterByPrefix=False))  # type: ignore[arg-type]

    # Type "/argtest tw" - "tw" is a prefix of only "two"
    _type(editor, "/argtest tw")

    assert editor.getText() == "/argtest tw"
    await flushAutocomplete()
    assert editor.isShowingAutocomplete() is True

    # Press Enter - "tw" uniquely matches "two", so "two" should be applied
    editor.handleInput("\r")
    assert editor.getText() == "/argtest two"


async def test_selects_first_prefix_match_when_multiple_items_match():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    # Mock provider that returns all items unfiltered
    editor.setAutocompleteProvider(_argtestProvider(items("one", "two", "three"), filterByPrefix=False))  # type: ignore[arg-type]

    # Type "/argtest t" - "t" is a prefix of both "two" and "three"
    _type(editor, "/argtest t")

    await flushAutocomplete()
    assert editor.isShowingAutocomplete() is True

    # Press Enter - "t" matches "two" first, so "two" is selected
    editor.handleInput("\r")
    assert editor.getText() == "/argtest two"


async def test_works_for_built_in_style_command_argument_completion_path_model_like():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    # Mock provider for /model command with model completions
    # Use [^ ]+ to match any non-space characters (including hyphens)
    editor.setAutocompleteProvider(  # type: ignore[arg-type]
        _argtestProvider(items("gpt-4o", "gpt-4o-mini", "claude-sonnet"), filterByPrefix=True, command="model")
    )

    # Type "/model gpt-4o-mini" - exact match for second item in list
    _type(editor, "/model gpt-4o-mini")

    assert editor.getText() == "/model gpt-4o-mini"
    await flushAutocomplete()
    assert editor.isShowingAutocomplete() is True

    # Press Enter - should retain exact typed value, not apply first highlighted item
    editor.handleInput("\r")

    # The exact typed value should be retained
    assert editor.getText() == "/model gpt-4o-mini"


async def test_awaits_async_slash_command_argument_completions():
    from misaka.ui.tui.autocomplete import SlashCommand

    editor = Editor(createTestTUI(), defaultEditorTheme)

    async def getArgumentCompletions(prefix: str) -> Any:
        return items("skill-a") if prefix.startswith("s") else None

    provider = CombinedAutocompleteProvider(
        [SlashCommand(name="load-skills", description="Load skills", getArgumentCompletions=getArgumentCompletions)],
        os.getcwd(),
    )
    editor.setAutocompleteProvider(provider)
    editor.setText("/load-skills ")

    editor.handleInput("s")
    await flushAutocomplete()
    assert editor.isShowingAutocomplete() is True

    editor.handleInput("\t")
    assert editor.getText() == "/load-skills skill-a"
    assert editor.isShowingAutocomplete() is False


async def test_ignores_invalid_slash_command_argument_completion_results():
    from misaka.ui.tui.autocomplete import SlashCommand

    editor = Editor(createTestTUI(), defaultEditorTheme)
    provider = CombinedAutocompleteProvider(
        [
            SlashCommand(
                name="load-skills",
                description="Load skills",
                getArgumentCompletions=lambda _prefix: "not-an-array",  # type: ignore[arg-type,return-value]
            )
        ],
        os.getcwd(),
    )
    editor.setAutocompleteProvider(provider)
    editor.setText("/load-skills ")

    editor.handleInput("s")
    await flushAutocomplete()
    assert editor.isShowingAutocomplete() is False
    assert editor.getText() == "/load-skills s"


async def test_does_not_show_argument_completions_when_command_has_no_argument_completer():
    from misaka.ui.tui.autocomplete import SlashCommand

    editor = Editor(createTestTUI(), defaultEditorTheme)
    provider = CombinedAutocompleteProvider(
        [
            SlashCommand(name="help", description="Show help"),
            SlashCommand(
                name="model",
                description="Switch model",
                getArgumentCompletions=lambda _prefix: items("claude-opus"),
            ),
        ],
        os.getcwd(),
    )
    editor.setAutocompleteProvider(provider)

    editor.handleInput("/")
    editor.handleInput("h")
    editor.handleInput("e")
    await flushAutocomplete()
    assert editor.isShowingAutocomplete() is True

    editor.handleInput("\t")
    assert editor.getText() == "/help "
    assert editor.isShowingAutocomplete() is False


# --- Character jump (Ctrl+]) ----------------------------------------------------------------------


def test_jumps_forward_to_first_occurrence_of_character_on_same_line():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("hello world")
    editor.handleInput("\x01")  # Ctrl+A - go to start
    assert editor.getCursor() == {"line": 0, "col": 0}

    editor.handleInput("\x1d")  # Ctrl+] (legacy sequence for ctrl+])
    editor.handleInput("o")  # Jump to first 'o'

    assert editor.getCursor() == {"line": 0, "col": 4}  # 'o' in "hello"


def test_jumps_forward_to_next_occurrence_after_cursor():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("hello world")
    editor.handleInput("\x01")  # Ctrl+A - go to start
    # Move cursor to the 'o' in "hello" (col 4)
    for _i in range(4):
        editor.handleInput("\x1b[C")
    assert editor.getCursor() == {"line": 0, "col": 4}

    editor.handleInput("\x1d")  # Ctrl+]
    editor.handleInput("o")  # Jump to next 'o' (in "world")

    assert editor.getCursor() == {"line": 0, "col": 7}  # 'o' in "world"


def test_jumps_forward_across_multiple_lines():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("abc\ndef\nghi")
    # Cursor is at end (line 2, col 3). Move to line 0 via up arrows, then Ctrl+A
    editor.handleInput("\x1b[A")  # Up
    editor.handleInput("\x1b[A")  # Up - now on line 0
    editor.handleInput("\x01")  # Ctrl+A - go to start of line
    assert editor.getCursor() == {"line": 0, "col": 0}

    editor.handleInput("\x1d")  # Ctrl+]
    editor.handleInput("g")  # Jump to 'g' on line 3

    assert editor.getCursor() == {"line": 2, "col": 0}


def test_jumps_backward_to_first_occurrence_before_cursor_on_same_line():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("hello world")
    # Cursor at end (col 11)
    assert editor.getCursor() == {"line": 0, "col": 11}

    editor.handleInput("\x1b\x1d")  # Ctrl+Alt+] (ESC followed by Ctrl+])
    editor.handleInput("o")  # Jump to last 'o' before cursor

    assert editor.getCursor() == {"line": 0, "col": 7}  # 'o' in "world"


def test_jumps_backward_across_multiple_lines():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("abc\ndef\nghi")
    # Cursor at end of line 3
    assert editor.getCursor() == {"line": 2, "col": 3}

    editor.handleInput("\x1b\x1d")  # Ctrl+Alt+]
    editor.handleInput("a")  # Jump to 'a' on line 1

    assert editor.getCursor() == {"line": 0, "col": 0}


def test_does_nothing_when_character_is_not_found_forward():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("hello world")
    editor.handleInput("\x01")  # Ctrl+A - go to start
    assert editor.getCursor() == {"line": 0, "col": 0}

    editor.handleInput("\x1d")  # Ctrl+]
    editor.handleInput("z")  # 'z' doesn't exist

    assert editor.getCursor() == {"line": 0, "col": 0}  # Cursor unchanged


def test_does_nothing_when_character_is_not_found_backward():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("hello world")
    # Cursor at end
    assert editor.getCursor() == {"line": 0, "col": 11}

    editor.handleInput("\x1b\x1d")  # Ctrl+Alt+]
    editor.handleInput("z")  # 'z' doesn't exist

    assert editor.getCursor() == {"line": 0, "col": 11}  # Cursor unchanged


def test_is_case_sensitive():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("Hello World")
    editor.handleInput("\x01")  # Ctrl+A - go to start
    assert editor.getCursor() == {"line": 0, "col": 0}

    # Search for lowercase 'h' - should not find it (only 'H' exists)
    editor.handleInput("\x1d")  # Ctrl+]
    editor.handleInput("h")

    assert editor.getCursor() == {"line": 0, "col": 0}  # Cursor unchanged

    # Search for uppercase 'W' - should find it
    editor.handleInput("\x1d")  # Ctrl+]
    editor.handleInput("W")

    assert editor.getCursor() == {"line": 0, "col": 6}  # 'W' in "World"


def test_cancels_jump_mode_when_ctrl_bracket_is_pressed_again():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("hello world")
    editor.handleInput("\x01")  # Ctrl+A - go to start
    assert editor.getCursor() == {"line": 0, "col": 0}

    editor.handleInput("\x1d")  # Ctrl+] - enter jump mode
    editor.handleInput("\x1d")  # Ctrl+] again - cancel

    # Type 'o' normally - should insert, not jump
    editor.handleInput("o")
    assert editor.getText() == "ohello world"


def test_cancels_jump_mode_on_escape_and_processes_the_escape():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("hello world")
    editor.handleInput("\x01")  # Ctrl+A - go to start
    assert editor.getCursor() == {"line": 0, "col": 0}

    editor.handleInput("\x1d")  # Ctrl+] - enter jump mode
    editor.handleInput("\x1b")  # Escape - cancel jump mode

    # Cursor should be unchanged (Escape itself doesn't move cursor in editor)
    assert editor.getCursor() == {"line": 0, "col": 0}

    # Type 'o' normally - should insert, not jump
    editor.handleInput("o")
    assert editor.getText() == "ohello world"


def test_cancels_backward_jump_mode_when_ctrl_alt_bracket_is_pressed_again():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("hello world")
    # Cursor at end
    assert editor.getCursor() == {"line": 0, "col": 11}

    editor.handleInput("\x1b\x1d")  # Ctrl+Alt+] - enter backward jump mode
    editor.handleInput("\x1b\x1d")  # Ctrl+Alt+] again - cancel

    # Type 'o' normally - should insert, not jump
    editor.handleInput("o")
    assert editor.getText() == "hello worldo"


def test_searches_for_special_characters():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("foo(bar) = baz;")
    editor.handleInput("\x01")  # Ctrl+A - go to start
    assert editor.getCursor() == {"line": 0, "col": 0}

    # Jump to '('
    editor.handleInput("\x1d")  # Ctrl+]
    editor.handleInput("(")

    assert editor.getCursor() == {"line": 0, "col": 3}

    # Jump to '='
    editor.handleInput("\x1d")  # Ctrl+]
    editor.handleInput("=")

    assert editor.getCursor() == {"line": 0, "col": 9}


def test_handles_empty_text_gracefully():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("")
    assert editor.getCursor() == {"line": 0, "col": 0}

    editor.handleInput("\x1d")  # Ctrl+]
    editor.handleInput("x")

    assert editor.getCursor() == {"line": 0, "col": 0}  # Cursor unchanged


def test_resets_lastaction_when_jumping():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("hello world")
    editor.handleInput("\x01")  # Ctrl+A - go to start

    # Type to set lastAction to "type-word"
    editor.handleInput("x")
    assert editor.getText() == "xhello world"

    # Jump forward
    editor.handleInput("\x1d")  # Ctrl+]
    editor.handleInput("o")

    # Type more - should start a new undo unit (lastAction was reset)
    editor.handleInput("Y")
    assert editor.getText() == "xhellYo world"

    # Undo should only undo "Y", not "x" as well
    editor.handleInput(UNDO)  # Ctrl+- (undo)
    assert editor.getText() == "xhello world"


# --- Sticky column --------------------------------------------------------------------------------


def positionCursor(editor: Editor, line: int, col: int) -> None:
    """Helper: position cursor at a specific line and column"""
    # Go to line 0 first
    for _i in range(20):
        editor.handleInput("\x1b[A")
    # Go to target line
    for _i in range(line):
        editor.handleInput("\x1b[B")
    # Go to target col
    editor.handleInput("\x01")  # Ctrl+A
    for _i in range(col):
        editor.handleInput("\x1b[C")


def _right(editor: Editor, count: int) -> None:
    for _i in range(count):
        editor.handleInput("\x1b[C")


def test_preserves_target_column_when_moving_up_through_a_shorter_line():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    # Line 0: "2222222222x222" (x at col 10)
    # Line 1: "" (empty)
    # Line 2: "1111111111_111111111111" (_ at col 10)
    editor.setText("2222222222x222\n\n1111111111_111111111111")

    # Position cursor on _ (line 2, col 10)
    assert editor.getCursor() == {"line": 2, "col": 23}  # At end
    editor.handleInput("\x01")  # Ctrl+A - go to start of line
    _right(editor, 10)  # Move right to col 10
    assert editor.getCursor() == {"line": 2, "col": 10}

    # Press Up - should move to empty line (col clamped to 0)
    editor.handleInput("\x1b[A")  # Up arrow
    assert editor.getCursor() == {"line": 1, "col": 0}

    # Press Up again - should move to line 0 at col 10 (on 'x')
    editor.handleInput("\x1b[A")  # Up arrow
    assert editor.getCursor() == {"line": 0, "col": 10}


def test_preserves_target_column_when_moving_down_through_a_shorter_line():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("1111111111_111\n\n2222222222x222222222222")

    # Position cursor on _ (line 0, col 10)
    editor.handleInput("\x1b[A")  # Up to line 1
    editor.handleInput("\x1b[A")  # Up to line 0
    editor.handleInput("\x01")  # Ctrl+A
    _right(editor, 10)
    assert editor.getCursor() == {"line": 0, "col": 10}

    # Press Down - should move to empty line (col clamped to 0)
    editor.handleInput("\x1b[B")  # Down arrow
    assert editor.getCursor() == {"line": 1, "col": 0}

    # Press Down again - should move to line 2 at col 10 (on 'x')
    editor.handleInput("\x1b[B")  # Down arrow
    assert editor.getCursor() == {"line": 2, "col": 10}


def test_resets_sticky_column_on_horizontal_movement_left_arrow():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("1234567890\n\n1234567890")

    # Start at line 2, col 5
    editor.handleInput("\x01")  # Ctrl+A
    _right(editor, 5)
    assert editor.getCursor() == {"line": 2, "col": 5}

    # Move up through empty line
    editor.handleInput("\x1b[A")  # Up - line 1, col 0
    editor.handleInput("\x1b[A")  # Up - line 0, col 5 (sticky)
    assert editor.getCursor() == {"line": 0, "col": 5}

    # Move left - resets sticky column
    editor.handleInput("\x1b[D")  # Left
    assert editor.getCursor() == {"line": 0, "col": 4}

    # Move down twice
    editor.handleInput("\x1b[B")  # Down - line 1, col 0
    editor.handleInput("\x1b[B")  # Down - line 2, col 4 (new sticky from col 4)
    assert editor.getCursor() == {"line": 2, "col": 4}


def test_resets_sticky_column_on_horizontal_movement_right_arrow():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("1234567890\n\n1234567890")

    # Start at line 0, col 5
    editor.handleInput("\x1b[A")  # Up to line 1
    editor.handleInput("\x1b[A")  # Up to line 0
    editor.handleInput("\x01")  # Ctrl+A
    _right(editor, 5)
    assert editor.getCursor() == {"line": 0, "col": 5}

    # Move down through empty line
    editor.handleInput("\x1b[B")  # Down - line 1, col 0
    editor.handleInput("\x1b[B")  # Down - line 2, col 5 (sticky)
    assert editor.getCursor() == {"line": 2, "col": 5}

    # Move right - resets sticky column
    editor.handleInput("\x1b[C")  # Right
    assert editor.getCursor() == {"line": 2, "col": 6}

    # Move up twice
    editor.handleInput("\x1b[A")  # Up - line 1, col 0
    editor.handleInput("\x1b[A")  # Up - line 0, col 6 (new sticky from col 6)
    assert editor.getCursor() == {"line": 0, "col": 6}


def test_resets_sticky_column_on_typing():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("1234567890\n\n1234567890")

    # Start at line 2, col 8
    editor.handleInput("\x01")  # Ctrl+A
    _right(editor, 8)

    # Move up through empty line
    editor.handleInput("\x1b[A")  # Up
    editor.handleInput("\x1b[A")  # Up - line 0, col 8
    assert editor.getCursor() == {"line": 0, "col": 8}

    # Type a character - resets sticky column
    editor.handleInput("X")
    assert editor.getCursor() == {"line": 0, "col": 9}

    # Move down twice
    editor.handleInput("\x1b[B")  # Down - line 1, col 0
    editor.handleInput("\x1b[B")  # Down - line 2, col 9 (new sticky from col 9)
    assert editor.getCursor() == {"line": 2, "col": 9}


def test_resets_sticky_column_on_backspace():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("1234567890\n\n1234567890")

    # Start at line 2, col 8
    editor.handleInput("\x01")  # Ctrl+A
    _right(editor, 8)

    # Move up through empty line
    editor.handleInput("\x1b[A")  # Up
    editor.handleInput("\x1b[A")  # Up - line 0, col 8
    assert editor.getCursor() == {"line": 0, "col": 8}

    # Backspace - resets sticky column
    editor.handleInput("\x7f")  # Backspace
    assert editor.getCursor() == {"line": 0, "col": 7}

    # Move down twice
    editor.handleInput("\x1b[B")  # Down - line 1, col 0
    editor.handleInput("\x1b[B")  # Down - line 2, col 7 (new sticky from col 7)
    assert editor.getCursor() == {"line": 2, "col": 7}


def test_resets_sticky_column_on_ctrl_a_move_to_line_start():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("1234567890\n\n1234567890")

    # Start at line 2, col 8
    editor.handleInput("\x01")  # Ctrl+A
    _right(editor, 8)

    # Move up - establishes sticky col 8
    editor.handleInput("\x1b[A")  # Up - line 1, col 0

    # Ctrl+A - resets sticky column to 0
    editor.handleInput("\x01")  # Ctrl+A
    assert editor.getCursor() == {"line": 1, "col": 0}

    # Move up
    editor.handleInput("\x1b[A")  # Up - line 0, col 0 (new sticky from col 0)
    assert editor.getCursor() == {"line": 0, "col": 0}


def test_resets_sticky_column_on_ctrl_e_move_to_line_end():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("12345\n\n1234567890")

    # Start at line 2, col 3
    editor.handleInput("\x01")  # Ctrl+A
    _right(editor, 3)

    # Move up through empty line - establishes sticky col 3
    editor.handleInput("\x1b[A")  # Up - line 1, col 0
    editor.handleInput("\x1b[A")  # Up - line 0, col 3
    assert editor.getCursor() == {"line": 0, "col": 3}

    # Ctrl+E - resets sticky column to end
    editor.handleInput("\x05")  # Ctrl+E
    assert editor.getCursor() == {"line": 0, "col": 5}

    # Move down twice
    editor.handleInput("\x1b[B")  # Down - line 1, col 0
    editor.handleInput("\x1b[B")  # Down - line 2, col 5 (new sticky from col 5)
    assert editor.getCursor() == {"line": 2, "col": 5}


def test_resets_sticky_column_on_word_movement_ctrl_left():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("hello world\n\nhello world")

    # Start at end of line 2 (col 11)
    assert editor.getCursor() == {"line": 2, "col": 11}

    # Move up through empty line - establishes sticky col 11
    editor.handleInput("\x1b[A")  # Up - line 1, col 0
    editor.handleInput("\x1b[A")  # Up - line 0, col 11
    assert editor.getCursor() == {"line": 0, "col": 11}

    # Ctrl+Left - word movement resets sticky column
    editor.handleInput("\x1b[1;5D")  # Ctrl+Left
    assert editor.getCursor() == {"line": 0, "col": 6}  # Before "world"

    # Move down twice
    editor.handleInput("\x1b[B")  # Down - line 1, col 0
    editor.handleInput("\x1b[B")  # Down - line 2, col 6 (new sticky from col 6)
    assert editor.getCursor() == {"line": 2, "col": 6}


def test_resets_sticky_column_on_word_movement_ctrl_right():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("hello world\n\nhello world")

    # Start at line 0, col 0
    editor.handleInput("\x1b[A")  # Up
    editor.handleInput("\x1b[A")  # Up
    editor.handleInput("\x01")  # Ctrl+A
    assert editor.getCursor() == {"line": 0, "col": 0}

    # Move down through empty line - establishes sticky col 0
    editor.handleInput("\x1b[B")  # Down - line 1, col 0
    editor.handleInput("\x1b[B")  # Down - line 2, col 0
    assert editor.getCursor() == {"line": 2, "col": 0}

    # Ctrl+Right - word movement resets sticky column
    editor.handleInput("\x1b[1;5C")  # Ctrl+Right
    assert editor.getCursor() == {"line": 2, "col": 5}  # After "hello"

    # Move up twice
    editor.handleInput("\x1b[A")  # Up - line 1, col 0
    editor.handleInput("\x1b[A")  # Up - line 0, col 5 (new sticky from col 5)
    assert editor.getCursor() == {"line": 0, "col": 5}


def test_resets_sticky_column_on_undo():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("1234567890\n\n1234567890")

    # Go to line 0, col 8
    editor.handleInput("\x1b[A")  # Up to line 1
    editor.handleInput("\x1b[A")  # Up to line 0
    editor.handleInput("\x01")  # Ctrl+A
    _right(editor, 8)
    assert editor.getCursor() == {"line": 0, "col": 8}

    # Move down through empty line - establishes sticky col 8
    editor.handleInput("\x1b[B")  # Down - line 1, col 0
    editor.handleInput("\x1b[B")  # Down - line 2, col 8 (sticky)
    assert editor.getCursor() == {"line": 2, "col": 8}

    # Type something to create undo state - this clears sticky and sets col to 9
    editor.handleInput("X")
    assert editor.getText() == "1234567890\n\n12345678X90"
    assert editor.getCursor() == {"line": 2, "col": 9}

    # Move up - establishes new sticky col 9
    editor.handleInput("\x1b[A")  # Up - line 1, col 0
    editor.handleInput("\x1b[A")  # Up - line 0, col 9
    assert editor.getCursor() == {"line": 0, "col": 9}

    # Undo - resets sticky column and restores cursor to line 2, col 8
    editor.handleInput(UNDO)  # Ctrl+- (undo)
    assert editor.getText() == "1234567890\n\n1234567890"
    assert editor.getCursor() == {"line": 2, "col": 8}

    # Move up - should capture new sticky from restored col 8, not old col 9
    editor.handleInput("\x1b[A")  # Up - line 1, col 0
    editor.handleInput("\x1b[A")  # Up - line 0, col 8 (new sticky from restored position)
    assert editor.getCursor() == {"line": 0, "col": 8}


def test_handles_multiple_consecutive_up_down_movements():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("1234567890\nab\ncd\nef\n1234567890")

    # Start at line 4, col 7
    editor.handleInput("\x01")  # Ctrl+A
    _right(editor, 7)
    assert editor.getCursor() == {"line": 4, "col": 7}

    # Move up multiple times through short lines
    editor.handleInput("\x1b[A")  # Up - line 3, col 2 (clamped)
    editor.handleInput("\x1b[A")  # Up - line 2, col 2 (clamped)
    editor.handleInput("\x1b[A")  # Up - line 1, col 2 (clamped)
    editor.handleInput("\x1b[A")  # Up - line 0, col 7 (restored)
    assert editor.getCursor() == {"line": 0, "col": 7}

    # Move down multiple times - sticky should still be 7
    editor.handleInput("\x1b[B")  # Down - line 1, col 2
    editor.handleInput("\x1b[B")  # Down - line 2, col 2
    editor.handleInput("\x1b[B")  # Down - line 3, col 2
    editor.handleInput("\x1b[B")  # Down - line 4, col 7 (restored)
    assert editor.getCursor() == {"line": 4, "col": 7}


def test_moves_correctly_through_wrapped_visual_lines_without_getting_stuck():
    tui = createTestTUI(15, 24)  # Narrow terminal
    editor = Editor(tui, defaultEditorTheme)

    # Line 0: short
    # Line 1: 30 chars = wraps to 3 visual lines at width 10 (after padding)
    editor.setText("short\n123456789012345678901234567890")
    editor.render(15)  # This gives 14 layout width

    # Position at end of line 1 (col 30)
    assert editor.getCursor() == {"line": 1, "col": 30}

    # Move up repeatedly - should traverse all visual lines of the wrapped text
    # and eventually reach line 0
    editor.handleInput("\x1b[A")  # Up - to previous visual line within line 1
    assert editor.getCursor()["line"] == 1

    editor.handleInput("\x1b[A")  # Up - another visual line
    assert editor.getCursor()["line"] == 1

    editor.handleInput("\x1b[A")  # Up - should reach line 0
    assert editor.getCursor()["line"] == 0


def test_handles_settext_resetting_sticky_column():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    editor.setText("1234567890\n\n1234567890")

    # Establish sticky column
    editor.handleInput("\x01")  # Ctrl+A
    _right(editor, 8)
    editor.handleInput("\x1b[A")  # Up

    # setText should reset sticky column
    editor.setText("abcdefghij\n\nabcdefghij")
    assert editor.getCursor() == {"line": 2, "col": 10}  # At end

    # Move up - should capture new sticky from current position (10)
    editor.handleInput("\x1b[A")  # Up - line 1, col 0
    editor.handleInput("\x1b[A")  # Up - line 0, col 10
    assert editor.getCursor() == {"line": 0, "col": 10}


def test_sets_preferredvisualcol_when_pressing_right_at_end_of_prompt_last_line():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    # Line 0: 20 chars with 'x' at col 10
    # Line 1: empty
    # Line 2: 10 chars ending with '_'
    editor.setText("111111111x1111111111\n\n333333333_")

    # Go to line 0, press Ctrl+E (end of line) - col 20
    editor.handleInput("\x1b[A")  # Up to line 1
    editor.handleInput("\x1b[A")  # Up to line 0
    editor.handleInput("\x05")  # Ctrl+E - move to end of line
    assert editor.getCursor() == {"line": 0, "col": 20}

    # Move down to line 2 - cursor clamped to col 10 (end of line)
    editor.handleInput("\x1b[B")  # Down to line 1, col 0
    editor.handleInput("\x1b[B")  # Down to line 2, col 10 (clamped)
    assert editor.getCursor() == {"line": 2, "col": 10}

    # Press Right at end of prompt - nothing visible happens, but sets preferredVisualCol to 10
    editor.handleInput("\x1b[C")  # Right - can't move, but sets preferredVisualCol
    assert editor.getCursor() == {"line": 2, "col": 10}  # Still at same position

    # Move up twice to line 0 - should use preferredVisualCol (10) to land on 'x'
    editor.handleInput("\x1b[A")  # Up to line 1, col 0
    editor.handleInput("\x1b[A")  # Up to line 0, col 10 (on 'x')
    assert editor.getCursor() == {"line": 0, "col": 10}


def test_handles_editor_resizes_when_preferredvisualcol_is_on_the_same_line():
    # Create editor with wider terminal
    tui = createTestTUI(80, 24)
    editor = Editor(tui, defaultEditorTheme)

    editor.setText("12345678901234567890\n\n12345678901234567890")

    # Start at line 2, col 15
    editor.handleInput("\x01")  # Ctrl+A
    _right(editor, 15)

    # Move up through empty line - establishes sticky col 15
    editor.handleInput("\x1b[A")  # Up
    editor.handleInput("\x1b[A")  # Up - line 0, col 15
    assert editor.getCursor() == {"line": 0, "col": 15}

    # Render with narrower width to simulate resize
    editor.render(12)  # Width 12

    # Move down - sticky should be clamped to new width
    editor.handleInput("\x1b[B")  # Down - line 1
    editor.handleInput("\x1b[B")  # Down - line 2, col should be clamped
    assert editor.getCursor()["col"] == 4


def test_handles_editor_resizes_when_preferredvisualcol_is_on_a_different_line():
    tui = createTestTUI(80, 24)
    editor = Editor(tui, defaultEditorTheme)

    # Create a line that wraps into multiple visual lines at width 10
    # "12345678901234567890" = 20 chars, wraps to 2 visual lines at width 10
    editor.setText("short\n12345678901234567890")

    # Go to line 1, col 15
    editor.handleInput("\x01")  # Ctrl+A
    _right(editor, 15)
    assert editor.getCursor() == {"line": 1, "col": 15}

    # Move up to establish sticky col 15
    editor.handleInput("\x1b[A")  # Up to line 0
    # Line 0 has only 5 chars, so cursor at col 5
    assert editor.getCursor() == {"line": 0, "col": 5}

    # Narrow the editor
    editor.render(10)

    # Move down - preferredVisualCol was 15, but width is 10
    # Should land on line 1, clamped to width (visual col 9, which is logical col 9)
    editor.handleInput("\x1b[B")  # Down to line 1
    assert editor.getCursor() == {"line": 1, "col": 8}

    # Move up
    editor.handleInput("\x1b[A")  # Up - should go to line 0
    assert editor.getCursor() == {"line": 0, "col": 5}  # Line 0 only has 5 chars

    # Restore the original width
    editor.render(80)

    # Move down - preferredVisualCol was kept at 15
    editor.handleInput("\x1b[B")  # Down to line 1
    assert editor.getCursor() == {"line": 1, "col": 15}


def test_rewrapped_lines_target_fits_current_visual_column():
    tui = createTestTUI(80, 24)
    editor = Editor(tui, defaultEditorTheme)
    editor.setText("abcdefghijklmnopqr\n123456789012345678")

    positionCursor(editor, 0, 18)
    assert editor.getCursor() == {"line": 0, "col": 18}

    # Narrow to width 10 (layoutWidth = 9).
    # Line 0 last segment has visual col max 9, line 1 first segment max 8
    editor.render(10)

    # Move down: cursor clamps to 8
    editor.handleInput("\x1b[B")
    assert editor.getCursor() == {"line": 1, "col": 8}

    # Widen back. Move up, the current visual col wins
    editor.render(80)
    editor.handleInput("\x1b[A")
    assert editor.getCursor() == {"line": 0, "col": 8}

    # Preferred was cleared by the rewrapped branch
    editor.handleInput("\x1b[B")
    assert editor.getCursor() == {"line": 1, "col": 8}


def test_rewrapped_lines_target_shorter_than_current_visual_column():
    tui = createTestTUI(80, 24)
    editor = Editor(tui, defaultEditorTheme)
    editor.setText("abcdefghijklmnopqr\n123456789012345678\nab")

    positionCursor(editor, 0, 18)
    assert editor.getCursor() == {"line": 0, "col": 18}

    # Narrow to width 10 (layoutWidth = 9). Moving down clamps to col 8
    editor.render(10)
    editor.handleInput("\x1b[B")
    assert editor.getCursor() == {"line": 1, "col": 8}

    # Widen the editor
    editor.render(80)

    # Move down to short line "ab".
    # preferredVisualCol is replaced with current visual col (8), cursor clamps to 2
    editor.handleInput("\x1b[B")
    assert editor.getCursor() == {"line": 2, "col": 2}

    # Moving up restores to preferred col 8
    editor.handleInput("\x1b[A")
    assert editor.getCursor() == {"line": 1, "col": 8}


# --- Paste marker atomic behavior -----------------------------------------------------------------

MARKER_LINES = re.compile(r"\[paste #\d+ \+\d+ lines\]")


def pasteWithMarker(editor: Editor) -> str:
    """Helper: simulate a large paste that creates a marker"""
    bigContent = ("line\n" * 20).rstrip()  # 20 lines
    editor.handleInput(f"\x1b[200~{bigContent}\x1b[201~")
    # The editor replaces large pastes with a marker like "[paste #1 +20 lines]"
    return editor.getText()


def bigPaste(tag: str) -> str:
    """Helper: 12-line paste content with a distinguishing tag"""
    return "\n".join(f"{tag}{i}" for i in range(12))


def _submitted(editor: Editor) -> list[str]:
    box: list[str] = [""]

    def onSubmit(text: str) -> None:
        box[0] = text

    editor.onSubmit = onSubmit
    return box


def test_creates_a_paste_marker_for_large_pastes():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    text = pasteWithMarker(editor)
    assert MARKER_LINES.search(text)


def test_treats_paste_marker_as_single_unit_for_right_arrow():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    editor.handleInput("A")
    pasteWithMarker(editor)
    editor.handleInput("B")
    # Text: "A[paste #1 +20 lines]B", cursor at end

    # Go to start
    editor.handleInput("\x01")  # Ctrl+A
    assert editor.getCursor() == {"line": 0, "col": 0}

    # Right arrow: should move past "A"
    editor.handleInput("\x1b[C")
    assert editor.getCursor() == {"line": 0, "col": 1}

    # Right arrow: should skip the entire marker
    editor.handleInput("\x1b[C")
    marker = MARKER_LINES.search(editor.getText())[0]  # type: ignore[index]
    assert editor.getCursor() == {"line": 0, "col": 1 + len(marker)}

    # Right arrow: should move past "B"
    editor.handleInput("\x1b[C")
    assert editor.getCursor() == {"line": 0, "col": 1 + len(marker) + 1}


def test_treats_paste_marker_as_single_unit_for_left_arrow():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    editor.handleInput("A")
    pasteWithMarker(editor)
    editor.handleInput("B")
    # Cursor at end

    # Left arrow: past "B"
    editor.handleInput("\x1b[D")
    text = editor.getText()
    marker = MARKER_LINES.search(text)[0]  # type: ignore[index]
    assert editor.getCursor() == {"line": 0, "col": 1 + len(marker)}

    # Left arrow: skip the entire marker
    editor.handleInput("\x1b[D")
    assert editor.getCursor() == {"line": 0, "col": 1}

    # Left arrow: past "A"
    editor.handleInput("\x1b[D")
    assert editor.getCursor() == {"line": 0, "col": 0}


def test_treats_paste_marker_as_single_unit_for_backspace():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    editor.handleInput("A")
    pasteWithMarker(editor)
    editor.handleInput("B")

    text = editor.getText()
    marker = MARKER_LINES.search(text)[0]  # type: ignore[index]

    # Position cursor right after the marker (before "B")
    editor.handleInput("\x01")  # Ctrl+A
    # Move past "A" and the marker
    editor.handleInput("\x1b[C")  # past "A"
    editor.handleInput("\x1b[C")  # past marker
    assert editor.getCursor() == {"line": 0, "col": 1 + len(marker)}

    # Backspace: should delete the entire marker at once
    editor.handleInput("\x7f")
    assert editor.getText() == "AB"
    assert editor.getCursor() == {"line": 0, "col": 1}


def test_treats_paste_marker_as_single_unit_for_forward_delete():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    editor.handleInput("A")
    pasteWithMarker(editor)
    editor.handleInput("B")

    # Position cursor on "A" (col 0) then move right once to be just before marker
    editor.handleInput("\x01")  # Ctrl+A
    editor.handleInput("\x1b[C")  # past "A", now at col 1 (start of marker)

    # Forward delete: should delete the entire marker at once
    editor.handleInput("\x1b[3~")  # Delete key
    assert editor.getText() == "AB"
    assert editor.getCursor() == {"line": 0, "col": 1}


def test_treats_paste_marker_as_single_unit_for_word_movement():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    editor.handleInput("X")
    editor.handleInput(" ")
    pasteWithMarker(editor)
    editor.handleInput(" ")
    editor.handleInput("Y")
    # Text: "X [paste #1 +20 lines] Y"

    text = editor.getText()
    marker = MARKER_LINES.search(text)[0]  # type: ignore[index]

    # Go to start
    editor.handleInput("\x01")  # Ctrl+A

    # Ctrl+Right: skip "X"
    editor.handleInput("\x1b[1;5C")
    assert editor.getCursor() == {"line": 0, "col": 1}

    # Ctrl+Right: skip whitespace + marker (marker treated as single non-ws, non-punct unit)
    editor.handleInput("\x1b[1;5C")
    assert editor.getCursor() == {"line": 0, "col": 2 + len(marker)}


def test_undo_restores_marker_after_backspace_deletion():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    editor.handleInput("A")
    pasteWithMarker(editor)
    editor.handleInput("B")

    textBefore = editor.getText()

    # Position after marker
    editor.handleInput("\x01")
    editor.handleInput("\x1b[C")  # past A
    editor.handleInput("\x1b[C")  # past marker

    # Delete marker
    editor.handleInput("\x7f")
    assert editor.getText() == "AB"

    # Undo
    editor.handleInput(UNDO)
    assert editor.getText() == textBefore


def test_undo_after_paste_marker_deletion_restores_the_paste_registry():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    submitted = _submitted(editor)

    paste = bigPaste("alpha")
    editor.handleInput(f"\x1b[200~{paste}\x1b[201~")
    editor.handleInput("\x7f")  # delete the marker
    editor.handleInput(UNDO)  # undo: restores marker text and registry
    editor.handleInput("\r")
    assert submitted[0] == paste


def test_undo_after_deleting_the_first_of_two_paste_markers_restores_both_registry_entries():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    submitted = _submitted(editor)

    pasteA = bigPaste("alpha")
    pasteB = bigPaste("beta")
    editor.handleInput(f"\x1b[200~{pasteA}\x1b[201~")  # #1 = A
    editor.handleInput(f"\x1b[200~{pasteB}\x1b[201~")  # #2 = B, cursor at end
    editor.handleInput("\x01")  # Ctrl+A
    editor.handleInput("\x1b[C")  # right over marker #1
    editor.handleInput("\x7f")  # delete marker #1, renumbers #2 -> #1
    editor.handleInput(UNDO)  # undo
    editor.handleInput("\r")
    assert submitted[0] == pasteA + pasteB


def test_renumbers_the_paste_registry_in_ascending_id_order_when_markers_are_out_of_order_in_text():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    submitted = _submitted(editor)

    pasteA = bigPaste("alpha")
    pasteB = bigPaste("beta")
    pasteC = bigPaste("gamma")
    editor.handleInput(f"\x1b[200~{pasteA}\x1b[201~")  # #1 = A
    editor.handleInput("\x01")  # Ctrl+A
    editor.handleInput(f"\x1b[200~{pasteB}\x1b[201~")  # #2 = B, text: [#2][#1]
    editor.handleInput("\x01")  # Ctrl+A
    editor.handleInput(f"\x1b[200~{pasteC}\x1b[201~")  # #3 = C, text: [#3][#2][#1]
    editor.handleInput("\x05")  # Ctrl+E
    editor.handleInput("\x7f")  # delete marker #1, renumber #3 -> #2 and #2 -> #1
    editor.handleInput("\r")
    assert submitted[0] == pasteC + pasteB


def test_undo_after_settext_restores_paste_markers_and_registry():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    submitted = _submitted(editor)

    paste = bigPaste("alpha")
    editor.handleInput(f"\x1b[200~{paste}\x1b[201~")
    editor.setText("replacement")
    editor.handleInput(UNDO)  # undo
    editor.handleInput("\r")
    assert submitted[0] == paste


def test_handles_multiple_paste_markers_in_same_line():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    pasteWithMarker(editor)
    editor.handleInput(" ")
    pasteWithMarker(editor)

    text = editor.getText()
    markers = list(MARKER_LINES.finditer(text))
    assert len(markers) == 2

    # Go to start
    editor.handleInput("\x01")

    # Right arrow: should skip first marker atomically
    editor.handleInput("\x1b[C")
    assert editor.getCursor() == {"line": 0, "col": len(markers[0][0])}

    # Right arrow: past space
    editor.handleInput("\x1b[C")
    assert editor.getCursor() == {"line": 0, "col": len(markers[0][0]) + 1}

    # Right arrow: should skip second marker atomically
    editor.handleInput("\x1b[C")
    assert editor.getCursor() == {
        "line": 0,
        "col": len(markers[0][0]) + 1 + len(markers[1][0]),
    }


def test_does_not_treat_manually_typed_marker_like_text_as_atomic_no_valid_paste_id():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    # Type text that matches the pattern but was typed manually (no paste entry)
    fakeMarker = "[paste #99 +5 lines]"
    for ch in fakeMarker:
        editor.handleInput(ch)

    assert editor.getText() == fakeMarker

    # No paste with ID 99 exists, so the marker is NOT treated atomically.
    # Right arrow should move one grapheme at a time.
    editor.handleInput("\x01")  # Ctrl+A
    editor.handleInput("\x1b[C")  # Right
    assert editor.getCursor() == {"line": 0, "col": 1}  # Just past "["


def test_does_not_crash_when_paste_marker_is_wider_than_terminal_width():
    # Reproduce: terminal width 8, paste marker "[paste #1 +47 lines]" (21 chars)
    tui = createTestTUI()
    editor = Editor(tui, defaultEditorTheme)
    bigContent = ("line\n" * 47).rstrip()
    editor.handleInput(f"\x1b[200~{bigContent}\x1b[201~")

    text = editor.getText()
    marker = MARKER_LINES.search(text)
    assert marker, "paste marker should be created"
    assert visibleWidth(marker[0]) > 8, "marker should be wider than render width"

    # Render at very narrow width - should not throw
    lines = editor.render(8)
    # Every rendered line must fit within the width (marker is split)
    for line in lines:
        assert visibleWidth(line) <= 8, f"line exceeds width 8: visible={visibleWidth(line)} text={line!r}"


def test_does_not_crash_when_text_plus_paste_marker_exceeds_terminal_width_with_cursor_on_marker():
    # Reproduce: terminal width 54, text "b".repeat(35) + "[paste #1 +27 lines]" + "bbbb"
    # Cursor lands on the paste marker after word-wrap, causing the rendered line
    # to be 55 visible chars (1 over the width).
    tui = createTestTUI()
    editor = Editor(tui, defaultEditorTheme)

    # Type 35 'b' characters
    for _i in range(35):
        editor.handleInput("b")

    # Paste 27 lines
    bigContent = ("line\n" * 27).rstrip()
    editor.handleInput(f"\x1b[200~{bigContent}\x1b[201~")

    # Type a few more characters
    for _i in range(4):
        editor.handleInput("b")

    # Move cursor left to land on the paste marker
    editor.handleInput("\x1b[D")  # past last 'b'
    editor.handleInput("\x1b[D")  # past last 'b'
    editor.handleInput("\x1b[D")  # past last 'b'
    editor.handleInput("\x1b[D")  # past last 'b'
    editor.handleInput("\x1b[D")  # now on the paste marker

    # Render at width 54 - should not throw
    renderWidth = 54
    lines = editor.render(renderWidth)
    for line in lines:
        assert visibleWidth(line) <= renderWidth, (
            f"line exceeds width {renderWidth}: visible={visibleWidth(line)} text={line!r}"
        )


def test_wordwrapline_re_checks_overflow_after_backtracking_to_wrap_opportunity():
    # Reproduce crash #2: " " + "b".repeat(35) + atomic_marker(20 chars) + "bbbb"
    # layoutWidth=53. After wrapping at the space, the remaining 35 b's + marker = 55
    # must trigger a second force-break instead of silently overflowing.
    tui = createTestTUI()
    editor = Editor(tui, defaultEditorTheme)

    # Type a space, then 35 b's
    editor.handleInput(" ")
    for _i in range(35):
        editor.handleInput("b")

    # Paste 27 lines to create marker
    bigContent = ("line\n" * 27).rstrip()
    editor.handleInput(f"\x1b[200~{bigContent}\x1b[201~")

    # Type trailing chars
    for _i in range(4):
        editor.handleInput("b")

    # Render at width 54 (contentWidth=54, layoutWidth=53 with paddingX=0)
    renderWidth = 54
    lines = editor.render(renderWidth)
    for line in lines:
        assert visibleWidth(line) <= renderWidth, (
            f"line exceeds width {renderWidth}: visible={visibleWidth(line)} text={line!r}"
        )


TOKENS_PASTE = "\n".join(  # noqa: FLY002 - upstream join
    [
        "line 1",
        "line 2",
        "line 3",
        "line 4",
        "line 5",
        "line 6",
        "line 7",
        "line 8",
        "line 9",
        "line 10",
        "tokens $1 $2 $& $$ $` $' end",
    ]
)


def test_expands_large_pasted_content_literally_in_getexpandedtext():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    pastedText = TOKENS_PASTE

    editor.handleInput(f"\x1b[200~{pastedText}\x1b[201~")

    assert MARKER_LINES.search(editor.getText())
    assert editor.getExpandedText() == pastedText


def test_snaps_to_the_paste_marker_start_when_navigating_down_into_it():
    editor = Editor(createTestTUI(), defaultEditorTheme)

    # Line 0: long enough text to establish a sticky column
    editor.setText("12345678901234567890\n\nhello ")

    # Create a large paste to get a marker
    bigContent = "x" * 2000
    editor.handleInput(f"\x1b[200~{bigContent}\x1b[201~")
    editor.render(80)

    text = editor.getText()
    _marker = re.search(r"\[paste #\d+ \d+ chars\]", text)[0]  # type: ignore[index]
    # Line 0: "12345678901234567890"
    # Line 1: "" (empty)
    # Line 2: "hello [paste #1 2000 chars]"
    #         marker starts at col 6

    # Navigate to line 0, col 10
    editor.handleInput("\x1b[A")  # Up to line 1
    editor.handleInput("\x1b[A")  # Up to line 0
    editor.handleInput("\x01")  # Ctrl+A (start of line)
    _right(editor, 10)  # Right 10
    assert editor.getCursor() == {"line": 0, "col": 10}

    # Down to empty line
    editor.handleInput("\x1b[B")
    assert editor.getCursor() == {"line": 1, "col": 0}

    # Down to paste marker line - sticky col 10 falls inside marker (starts at col 6).
    # Cursor should snap to start of marker (col 6), not end (col 6 + marker.length).
    editor.handleInput("\x1b[B")
    assert editor.getCursor() == {"line": 2, "col": 6}


def test_preserves_sticky_column_when_navigating_through_paste_marker_line():
    tui = createTestTUI(30, 24)
    editor = Editor(tui, defaultEditorTheme)

    # Build:
    # Line 0: "1234567890123456" (16 chars)
    # Line 1: "" (empty)
    # Line 2: "[paste #1 2000 chars]" (22 chars, paste marker)
    # Line 3: "" (empty)
    # Line 4: "abcdefghijklmnop" (16 chars)
    for ch in "1234567890123456":
        editor.handleInput(ch)
    editor.handleInput("\n")
    editor.handleInput("\n")
    editor.handleInput(f"\x1b[200~{'x' * 2000}\x1b[201~")
    editor.handleInput("\n")
    editor.handleInput("\n")
    for ch in "abcdefghijklmnop":
        editor.handleInput(ch)
    editor.render(30)

    # Navigate to line 0, col 10
    for _i in range(4):
        editor.handleInput("\x1b[A")  # Up to line 0
    editor.handleInput("\x01")  # Ctrl+A
    _right(editor, 10)
    assert editor.getCursor() == {"line": 0, "col": 10}

    # Down to empty line - sticky col 10 established
    editor.handleInput("\x1b[B")
    assert editor.getCursor() == {"line": 1, "col": 0}

    # Down to paste marker - cursor snapped to col 0 (start of marker)
    editor.handleInput("\x1b[B")
    assert editor.getCursor() == {"line": 2, "col": 0}

    # Down to empty line
    editor.handleInput("\x1b[B")
    assert editor.getCursor() == {"line": 3, "col": 0}

    # Down to last line - should restore sticky col 10
    editor.handleInput("\x1b[B")
    assert editor.getCursor() == {"line": 4, "col": 10}


def _markerLayout(editor: Editor) -> None:
    for ch in "abcdefgh":
        editor.handleInput(ch)
    bigContent = ("line\n" * 100).rstrip()
    editor.handleInput(f"\x1b[200~{bigContent}\x1b[201~")
    for ch in "ijklmnopqr":
        editor.handleInput(ch)
    editor.handleInput("\n")
    for ch in "123456789012345678":
        editor.handleInput(ch)
    editor.render(20)


def test_does_not_get_stuck_moving_down_from_a_multi_visual_line_paste_marker():
    tui = createTestTUI(20, 24)
    editor = Editor(tui, defaultEditorTheme)

    # Build:
    # Logical line 0: "abcdefgh" + marker(21 chars) + "ijklmnopqr"
    # Logical line 1: "123456789012345678"
    #
    # Marker "[paste #1 +100 lines]" (21 chars) is wider than the
    # terminal (20). Word-wrap splits at the space before "lines",
    # producing:
    #   VL1: abcdefgh              (startCol 0,  len 8)
    #   VL2: [paste #1 +100        (startCol 8,  len 15) <- marker head
    #   VL3: lines]ijklmnopqr      (startCol 23, len 16) <- marker tail + content
    #   VL4: 123456789012345678    (line 1)
    #
    # On VL3 the marker tail "lines]" occupies visual cols 0-5.
    # Content ("i") starts at visual col 6 = logical col 29.
    _markerLayout(editor)

    text = editor.getText()
    markerMatch = re.search(r"\[paste #\d+ \+\d+ lines]", text)
    assert markerMatch, "paste marker should be created"
    markerLen = len(markerMatch[0])  # 21
    assert markerLen > 20, "marker should be wider than terminal"
    markerStart = 8
    markerEnd = markerStart + markerLen  # 29

    # Navigate to line 0, col 6 (on "g"). Preferred col 6 is past the
    # marker tail on VL3, so the cursor should land on content ("i" at
    # col 29) without snapping back.
    editor.handleInput("\x1b[A")  # Up to line 0
    editor.handleInput("\x01")  # Ctrl+A (start of line)
    _right(editor, 6)  # Right to col 6
    assert editor.getCursor() == {"line": 0, "col": 6}

    # Down: cursor lands on paste marker start
    editor.handleInput("\x1b[B")
    assert editor.getCursor() == {"line": 0, "col": markerStart}

    # Down again: preferred col 6 lands at VL3 col 29 ("i"), which is
    # past the marker. Cursor stays on line 0.
    editor.handleInput("\x1b[B")
    assert editor.getCursor()["line"] == 0
    assert editor.getCursor()["col"] == markerEnd  # col 29 = "i"

    # Up: back to paste marker
    editor.handleInput("\x1b[A")
    assert editor.getCursor() == {"line": 0, "col": markerStart}

    # Up again: back to col 6 ("g")
    editor.handleInput("\x1b[A")
    assert editor.getCursor() == {"line": 0, "col": 6}


def test_skips_marker_continuation_vls_when_preferred_col_falls_in_marker_tail():
    tui = createTestTUI(20, 24)
    editor = Editor(tui, defaultEditorTheme)

    # Same layout. Start at col 3 ("d"). Preferred col 3 maps to VL3
    # visual col 3 which is inside the "lines]" marker tail.
    # moveToVisualLine detects the continuation VL and skips to VL4
    # (line 1).
    #   VL1: abcdefgh              (startCol 0,  len 8)
    #   VL2: [paste #1 +100        (startCol 8,  len 15) <- marker head
    #   VL3: lines]ijklmnopqr      (startCol 23, len 16) <- marker tail + content
    #   VL4: 123456789012345678    (line 1)
    _markerLayout(editor)

    # Navigate to line 0, col 3 (on "d")
    editor.handleInput("\x1b[A")  # Up to line 0
    editor.handleInput("\x01")  # Ctrl+A
    _right(editor, 3)
    assert editor.getCursor() == {"line": 0, "col": 3}

    # Down: marker
    editor.handleInput("\x1b[B")
    assert editor.getCursor()["col"] == 8

    # Down: skips VL3 (col 3 in marker tail) and lands on line 1
    editor.handleInput("\x1b[B")
    assert editor.getCursor() == {"line": 1, "col": 3}

    # Round-trip back
    editor.handleInput("\x1b[A")
    assert editor.getCursor()["col"] == 8  # marker
    editor.handleInput("\x1b[A")
    assert editor.getCursor() == {"line": 0, "col": 3}


def test_submits_large_pasted_content_literally():
    editor = Editor(createTestTUI(), defaultEditorTheme)
    pastedText = TOKENS_PASTE
    submitted = _submitted(editor)

    editor.handleInput(f"\x1b[200~{pastedText}\x1b[201~")
    editor.handleInput("\r")

    assert submitted[0] == pastedText

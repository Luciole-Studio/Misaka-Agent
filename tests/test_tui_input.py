"""Translated from pi-tui's ``test/input.test.ts`` (v1.3.0).

MISAKA fork: word navigation has no ICU dictionary, so the two Han word-boundary cases are strict xfails.
"""

from __future__ import annotations

import pytest

from misaka.ui.tui.components.input import Input, InputOptions
from misaka.ui.tui.utils import stripTerminalSequences, visibleWidth

NO_ICU_DICTIONARY = pytest.mark.xfail(
    strict=True, reason="MISAKA fork: no ICU word dictionary, so a Han run segments per character"
)

UNDO = "\x1b[45;5u"  # Ctrl+- (undo)


def _type(input: Input, text: str) -> None:
    for ch in text:
        input.handleInput(ch)


def _right(input: Input, count: int) -> None:
    for _i in range(count):
        input.handleInput("\x1b[C")


def test_submits_value_including_backslash_on_enter():
    input = Input()
    submitted: str | None = None

    def onSubmit(value: str) -> None:
        nonlocal submitted
        submitted = value

    input.onSubmit = onSubmit

    # Type hello, then backslash, then Enter
    _type(input, "hello")
    input.handleInput("\\")
    input.handleInput("\r")

    # Input is single-line, no backslash+Enter workaround
    assert submitted == "hello\\"


def test_inserts_backslash_as_regular_character():
    input = Input()

    input.handleInput("\\")
    input.handleInput("x")

    assert input.getValue() == "\\x"


# --- render ---------------------------------------------------------------------------------------


def test_supports_a_custom_prompt_and_styled_placeholder():
    input = Input(
        InputOptions(
            prompt="",
            placeholder="Find transcript",
            placeholderStyle=lambda text: f"\x1b[2m{text}\x1b[22m",
        )
    )
    input.focused = True

    [empty] = input.render(20)
    assert "\x1b[2m" in empty
    assert stripTerminalSequences(empty or "").rstrip() == "Find transcript"

    input.handleInput("n")
    [populated] = input.render(20)
    assert stripTerminalSequences(populated or "").rstrip() == "n"


def test_does_not_overflow_with_wide_cjk_and_fullwidth_text():
    width = 93
    cases = [
        "가나다라마바사아자차카타파하 한글 텍스트가 터미널 너비를 초과하면 크래시가 발생합니다 이것은 재현용 테스트입니다",
        "これはテスト文章です。日本語のテキストが正しく表示されるかどうかを確認するためのサンプルテキストです。あいうえお",
        "这是一段测试文本，用于验证中文字符在终端中的显示宽度是否被正确计算，如果不正确就会导致用户界面崩溃的问题",
        "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ０１２３４５６７８９ａｂｃｄｅｆｇｈｉｊｋｌｍ",
    ]
    cursorPositions = [
        ("start", lambda _input: None),
        ("middle", lambda input: _right(input, 10)),
        ("end", lambda input: input.handleInput("\x05")),
    ]

    for text in cases:
        for label, move in cursorPositions:
            input = Input()
            input.setValue(text)
            input.focused = True
            move(input)

            [line] = input.render(width)
            assert line
            assert visibleWidth(line) <= width, f"rendered line overflowed for {text} at {label}"


def test_keeps_the_cursor_visible_when_horizontally_scrolling_wide_text():
    input = Input()
    width = 20
    text = "가나다라마바사아자차카타파하"
    input.setValue(text)
    input.focused = True
    input.handleInput("\x01")
    _right(input, 5)

    [line] = input.render(width)
    assert line
    assert visibleWidth(line) <= width


# --- Kill ring ------------------------------------------------------------------------------------


def test_ctrl_w_saves_deleted_text_to_kill_ring_and_ctrl_y_yanks_it():
    input = Input()

    input.setValue("foo bar baz")
    # Move cursor to end
    input.handleInput("\x05")  # Ctrl+E

    input.handleInput("\x17")  # Ctrl+W - deletes "baz"
    assert input.getValue() == "foo bar "

    # Move to beginning and yank
    input.handleInput("\x01")  # Ctrl+A
    input.handleInput("\x19")  # Ctrl+Y
    assert input.getValue() == "bazfoo bar "


def test_ctrl_w_preserves_ascii_punctuation_boundaries():
    input = Input()

    input.setValue("foo.bar")
    input.handleInput("\x05")  # Ctrl+E
    input.handleInput("\x17")  # Ctrl+W - deletes "bar"
    assert input.getValue() == "foo."

    input.setValue("foo:bar")
    input.handleInput("\x05")  # Ctrl+E
    input.handleInput("\x17")  # Ctrl+W - deletes "bar"
    assert input.getValue() == "foo:"


@NO_ICU_DICTIONARY
def test_ctrl_w_handles_unicode_word_boundaries():
    input = Input()

    # "你好世界。你好，世界" segments as: 你好|世界|。|你好|，|世界
    input.setValue("你好世界。你好，世界")
    input.handleInput("\x05")  # Ctrl+E
    input.handleInput("\x17")  # Ctrl+W - deletes "世界"
    assert input.getValue() == "你好世界。你好，"
    input.handleInput("\x17")  # Ctrl+W - deletes "，"
    assert input.getValue() == "你好世界。你好"
    input.handleInput("\x17")  # Ctrl+W - deletes "你好"
    assert input.getValue() == "你好世界。"
    input.handleInput("\x17")  # Ctrl+W - deletes "。"
    assert input.getValue() == "你好世界"
    input.handleInput("\x17")  # Ctrl+W - deletes "世界"
    assert input.getValue() == "你好"
    input.handleInput("\x17")  # Ctrl+W - deletes "你好"
    assert input.getValue() == ""


def test_ctrl_u_saves_deleted_text_to_kill_ring():
    input = Input()

    input.setValue("hello world")
    # Move cursor to after "hello "
    input.handleInput("\x01")  # Ctrl+A
    _right(input, 6)

    input.handleInput("\x15")  # Ctrl+U - deletes "hello "
    assert input.getValue() == "world"

    input.handleInput("\x19")  # Ctrl+Y
    assert input.getValue() == "hello world"


def test_ctrl_k_saves_deleted_text_to_kill_ring():
    input = Input()

    input.setValue("hello world")
    input.handleInput("\x01")  # Ctrl+A
    input.handleInput("\x0b")  # Ctrl+K - deletes "hello world"

    assert input.getValue() == ""

    input.handleInput("\x19")  # Ctrl+Y
    assert input.getValue() == "hello world"


def test_ctrl_y_does_nothing_when_kill_ring_is_empty():
    input = Input()

    input.setValue("test")
    input.handleInput("\x05")  # Ctrl+E
    input.handleInput("\x19")  # Ctrl+Y
    assert input.getValue() == "test"


def test_alt_y_cycles_through_kill_ring_after_ctrl_y():
    input = Input()

    # Create kill ring with multiple entries
    input.setValue("first")
    input.handleInput("\x05")  # Ctrl+E
    input.handleInput("\x17")  # Ctrl+W - deletes "first"
    input.setValue("second")
    input.handleInput("\x05")  # Ctrl+E
    input.handleInput("\x17")  # Ctrl+W - deletes "second"
    input.setValue("third")
    input.handleInput("\x05")  # Ctrl+E
    input.handleInput("\x17")  # Ctrl+W - deletes "third"

    assert input.getValue() == ""

    input.handleInput("\x19")  # Ctrl+Y - yanks "third"
    assert input.getValue() == "third"

    input.handleInput("\x1by")  # Alt+Y - cycles to "second"
    assert input.getValue() == "second"

    input.handleInput("\x1by")  # Alt+Y - cycles to "first"
    assert input.getValue() == "first"

    input.handleInput("\x1by")  # Alt+Y - cycles back to "third"
    assert input.getValue() == "third"


def test_alt_y_does_nothing_if_not_preceded_by_yank():
    input = Input()

    input.setValue("test")
    input.handleInput("\x05")  # Ctrl+E
    input.handleInput("\x17")  # Ctrl+W - deletes "test"
    input.setValue("other")
    input.handleInput("\x05")  # Ctrl+E

    # Type something to break the yank chain
    input.handleInput("x")
    assert input.getValue() == "otherx"

    input.handleInput("\x1by")  # Alt+Y - should do nothing
    assert input.getValue() == "otherx"


def test_alt_y_does_nothing_if_kill_ring_has_one_entry():
    input = Input()

    input.setValue("only")
    input.handleInput("\x05")  # Ctrl+E
    input.handleInput("\x17")  # Ctrl+W - deletes "only"

    input.handleInput("\x19")  # Ctrl+Y - yanks "only"
    assert input.getValue() == "only"

    input.handleInput("\x1by")  # Alt+Y - should do nothing
    assert input.getValue() == "only"


def test_consecutive_ctrl_w_accumulates_into_one_kill_ring_entry():
    input = Input()

    input.setValue("one two three")
    input.handleInput("\x05")  # Ctrl+E
    input.handleInput("\x17")  # Ctrl+W - deletes "three"
    input.handleInput("\x17")  # Ctrl+W - deletes "two "
    input.handleInput("\x17")  # Ctrl+W - deletes "one "

    assert input.getValue() == ""

    input.handleInput("\x19")  # Ctrl+Y
    assert input.getValue() == "one two three"


def test_non_delete_actions_break_kill_accumulation():
    input = Input()

    input.setValue("foo bar baz")
    input.handleInput("\x05")  # Ctrl+E
    input.handleInput("\x17")  # Ctrl+W - deletes "baz"
    assert input.getValue() == "foo bar "

    input.handleInput("x")  # Typing breaks accumulation
    assert input.getValue() == "foo bar x"

    input.handleInput("\x17")  # Ctrl+W - deletes "x" (separate entry)
    assert input.getValue() == "foo bar "

    input.handleInput("\x19")  # Ctrl+Y - most recent is "x"
    assert input.getValue() == "foo bar x"

    input.handleInput("\x1by")  # Alt+Y - cycle to "baz"
    assert input.getValue() == "foo bar baz"


def test_non_yank_actions_break_alt_y_chain():
    input = Input()

    input.setValue("first")
    input.handleInput("\x05")  # Ctrl+E
    input.handleInput("\x17")  # Ctrl+W
    input.setValue("second")
    input.handleInput("\x05")  # Ctrl+E
    input.handleInput("\x17")  # Ctrl+W
    input.setValue("")

    input.handleInput("\x19")  # Ctrl+Y - yanks "second"
    assert input.getValue() == "second"

    input.handleInput("x")  # Breaks yank chain
    assert input.getValue() == "secondx"

    input.handleInput("\x1by")  # Alt+Y - should do nothing
    assert input.getValue() == "secondx"


def test_kill_ring_rotation_persists_after_cycling():
    input = Input()

    input.setValue("first")
    input.handleInput("\x05")  # Ctrl+E
    input.handleInput("\x17")  # deletes "first"
    input.setValue("second")
    input.handleInput("\x05")  # Ctrl+E
    input.handleInput("\x17")  # deletes "second"
    input.setValue("third")
    input.handleInput("\x05")  # Ctrl+E
    input.handleInput("\x17")  # deletes "third"
    input.setValue("")

    input.handleInput("\x19")  # Ctrl+Y - yanks "third"
    input.handleInput("\x1by")  # Alt+Y - cycles to "second"
    assert input.getValue() == "second"

    # Break chain and start fresh
    input.handleInput("x")
    input.setValue("")

    # New yank should get "second" (now at end after rotation)
    input.handleInput("\x19")  # Ctrl+Y
    assert input.getValue() == "second"


def test_backward_deletions_prepend_forward_deletions_append_during_accumulation():
    input = Input()

    input.setValue("prefix|suffix")
    # Position cursor at "|"
    input.handleInput("\x01")  # Ctrl+A
    _right(input, 6)  # Move right 6

    input.handleInput("\x0b")  # Ctrl+K - deletes "|suffix" (forward)
    assert input.getValue() == "prefix"

    input.handleInput("\x19")  # Ctrl+Y
    assert input.getValue() == "prefix|suffix"


def test_alt_d_deletes_word_forward_and_saves_to_kill_ring():
    input = Input()

    input.setValue("hello world test")
    input.handleInput("\x01")  # Ctrl+A

    input.handleInput("\x1bd")  # Alt+D - deletes "hello"
    assert input.getValue() == " world test"

    input.handleInput("\x1bd")  # Alt+D - deletes " world"
    assert input.getValue() == " test"

    # Yank should get accumulated text
    input.handleInput("\x19")  # Ctrl+Y
    assert input.getValue() == "hello world test"


def test_alt_d_preserves_ascii_punctuation_boundaries():
    input = Input()

    input.setValue("foo.bar baz")
    input.handleInput("\x01")  # Ctrl+A
    input.handleInput("\x1bd")  # Alt+D - deletes "foo"
    assert input.getValue() == ".bar baz"
    input.handleInput("\x1bd")  # Alt+D - deletes "."
    assert input.getValue() == "bar baz"
    input.handleInput("\x1bd")  # Alt+D - deletes "bar"
    assert input.getValue() == " baz"


@NO_ICU_DICTIONARY
def test_alt_d_handles_unicode_word_boundaries():
    input = Input()

    # "你好世界。你好，世界" segments as: 你好|世界|。|你好|，|世界
    input.setValue("你好世界。你好，世界")
    input.handleInput("\x01")  # Ctrl+A
    input.handleInput("\x1bd")  # Alt+D - deletes "你好"
    assert input.getValue() == "世界。你好，世界"
    input.handleInput("\x1bd")  # Alt+D - deletes "世界"
    assert input.getValue() == "。你好，世界"
    input.handleInput("\x1bd")  # Alt+D - deletes "。"
    assert input.getValue() == "你好，世界"
    input.handleInput("\x1bd")  # Alt+D - deletes "你好"
    assert input.getValue() == "，世界"
    input.handleInput("\x1bd")  # Alt+D - deletes "，"
    assert input.getValue() == "世界"
    input.handleInput("\x1bd")  # Alt+D - deletes "世界"
    assert input.getValue() == ""


def test_handles_yank_in_middle_of_text():
    input = Input()

    input.setValue("word")
    input.handleInput("\x05")  # Ctrl+E
    input.handleInput("\x17")  # Ctrl+W - deletes "word"
    input.setValue("hello world")
    # Move to middle (after "hello ")
    input.handleInput("\x01")  # Ctrl+A
    _right(input, 6)

    input.handleInput("\x19")  # Ctrl+Y
    assert input.getValue() == "hello wordworld"


def test_handles_yank_pop_in_middle_of_text():
    input = Input()

    # Create two kill ring entries
    input.setValue("FIRST")
    input.handleInput("\x05")  # Ctrl+E
    input.handleInput("\x17")  # Ctrl+W - deletes "FIRST"
    input.setValue("SECOND")
    input.handleInput("\x05")  # Ctrl+E
    input.handleInput("\x17")  # Ctrl+W - deletes "SECOND"

    # Set up "hello world" and position cursor after "hello "
    input.setValue("hello world")
    input.handleInput("\x01")  # Ctrl+A
    _right(input, 6)

    input.handleInput("\x19")  # Ctrl+Y - yanks "SECOND"
    assert input.getValue() == "hello SECONDworld"

    input.handleInput("\x1by")  # Alt+Y - replaces with "FIRST"
    assert input.getValue() == "hello FIRSTworld"


# --- Undo -----------------------------------------------------------------------------------------


def test_undo_does_nothing_when_undo_stack_is_empty():
    input = Input()

    input.handleInput(UNDO)  # Ctrl+- (undo)
    assert input.getValue() == ""


def test_coalesces_consecutive_word_characters_into_one_undo_unit():
    input = Input()

    _type(input, "hello world")
    assert input.getValue() == "hello world"

    # Undo removes " world"
    input.handleInput(UNDO)  # Ctrl+- (undo)
    assert input.getValue() == "hello"

    # Undo removes "hello"
    input.handleInput(UNDO)  # Ctrl+- (undo)
    assert input.getValue() == ""


def test_undoes_spaces_one_at_a_time():
    input = Input()

    _type(input, "hello  ")
    assert input.getValue() == "hello  "

    input.handleInput(UNDO)  # Ctrl+- (undo) - removes second " "
    assert input.getValue() == "hello "

    input.handleInput(UNDO)  # Ctrl+- (undo) - removes first " "
    assert input.getValue() == "hello"

    input.handleInput(UNDO)  # Ctrl+- (undo) - removes "hello"
    assert input.getValue() == ""


def test_undoes_backspace():
    input = Input()

    _type(input, "hello")
    input.handleInput("\x7f")  # Backspace
    assert input.getValue() == "hell"

    input.handleInput(UNDO)  # Ctrl+- (undo)
    assert input.getValue() == "hello"


def test_undoes_forward_delete():
    input = Input()

    _type(input, "hello")
    input.handleInput("\x01")  # Ctrl+A - go to start
    input.handleInput("\x1b[C")  # Right arrow
    input.handleInput("\x1b[3~")  # Delete key
    assert input.getValue() == "hllo"

    input.handleInput(UNDO)  # Ctrl+- (undo)
    assert input.getValue() == "hello"


def test_undoes_ctrl_w_delete_word_backward():
    input = Input()

    _type(input, "hello world")
    assert input.getValue() == "hello world"

    input.handleInput("\x17")  # Ctrl+W
    assert input.getValue() == "hello "

    input.handleInput(UNDO)  # Ctrl+- (undo)
    assert input.getValue() == "hello world"


def test_undoes_ctrl_k_delete_to_line_end():
    input = Input()

    _type(input, "hello world")
    input.handleInput("\x01")  # Ctrl+A
    _right(input, 6)

    input.handleInput("\x0b")  # Ctrl+K
    assert input.getValue() == "hello "

    input.handleInput(UNDO)  # Ctrl+- (undo)
    assert input.getValue() == "hello world"


def test_undoes_ctrl_u_delete_to_line_start():
    input = Input()

    _type(input, "hello world")
    input.handleInput("\x01")  # Ctrl+A
    _right(input, 6)

    input.handleInput("\x15")  # Ctrl+U
    assert input.getValue() == "world"

    input.handleInput(UNDO)  # Ctrl+- (undo)
    assert input.getValue() == "hello world"


def test_undoes_yank():
    input = Input()

    _type(input, "hello ")
    input.handleInput("\x17")  # Ctrl+W - delete "hello "
    input.handleInput("\x19")  # Ctrl+Y - yank
    assert input.getValue() == "hello "

    input.handleInput(UNDO)  # Ctrl+- (undo)
    assert input.getValue() == ""


def test_undoes_paste_atomically():
    input = Input()

    input.setValue("hello world")
    input.handleInput("\x01")  # Ctrl+A
    _right(input, 5)

    # Simulate bracketed paste
    input.handleInput("\x1b[200~beep boop\x1b[201~")
    assert input.getValue() == "hellobeep boop world"

    # Single undo should restore entire pre-paste state
    input.handleInput(UNDO)  # Ctrl+- (undo)
    assert input.getValue() == "hello world"


def test_undoes_alt_d_delete_word_forward():
    input = Input()

    input.setValue("hello world")
    input.handleInput("\x01")  # Ctrl+A

    input.handleInput("\x1bd")  # Alt+D - deletes "hello"
    assert input.getValue() == " world"

    input.handleInput(UNDO)  # Ctrl+- (undo)
    assert input.getValue() == "hello world"


def test_cursor_movement_starts_new_undo_unit():
    input = Input()

    input.handleInput("a")
    input.handleInput("b")
    input.handleInput("c")
    input.handleInput("\x01")  # Ctrl+A - movement breaks coalescing
    input.handleInput("\x05")  # Ctrl+E
    input.handleInput("d")
    input.handleInput("e")
    assert input.getValue() == "abcde"

    # Undo removes "de" (typed after movement)
    input.handleInput(UNDO)  # Ctrl+- (undo)
    assert input.getValue() == "abc"

    # Undo removes "abc"
    input.handleInput(UNDO)  # Ctrl+- (undo)
    assert input.getValue() == ""

"""Translated from pi-tui's ``test/wrap-ansi.test.ts`` (v1.1.0)."""

from misaka.ui.tui.utils import visibleWidth, wrapTextWithAnsi
from misaka.utils import js_regexp
from misaka.utils.js import jsTrim
from misaka.utils.js_regexp import RegExp

# --- wrapTextWithAnsi: underline styling ----------------------------------------------------------


def test_should_not_apply_underline_style_before_the_styled_text():
    underlineOn = "\x1b[4m"
    underlineOff = "\x1b[24m"
    url = "https://example.com/very/long/path/that/will/wrap"
    text = f"read this thread {underlineOn}{url}{underlineOff}"

    wrapped = wrapTextWithAnsi(text, 40)

    # First line should NOT contain underline code - it's just "read this thread"
    assert wrapped[0] == "read this thread"

    # Second line should start with underline, have URL content
    assert wrapped[1].startswith(underlineOn) is True
    assert "https://" in wrapped[1]


def test_should_not_have_whitespace_before_underline_reset_code():
    underlineOn = "\x1b[4m"
    underlineOff = "\x1b[24m"
    textWithUnderlinedTrailingSpace = f"{underlineOn}underlined text here {underlineOff}more"

    wrapped = wrapTextWithAnsi(textWithUnderlinedTrailingSpace, 18)

    assert f" {underlineOff}" not in wrapped[0]


def test_should_not_bleed_underline_to_padding_each_line_should_end_with_reset_for_underline_only():
    underlineOn = "\x1b[4m"
    underlineOff = "\x1b[24m"
    url = "https://example.com/very/long/path/that/will/definitely/wrap"
    text = f"prefix {underlineOn}{url}{underlineOff} suffix"

    wrapped = wrapTextWithAnsi(text, 30)

    # Middle lines (with underlined content) should end with underline-off, not full reset
    # Line 1 and 2 contain underlined URL parts
    for i in range(1, len(wrapped) - 1):
        line = wrapped[i]
        if underlineOn in line:
            # Should end with underline off, NOT full reset
            assert line.endswith(underlineOff) is True
            assert line.endswith("\x1b[0m") is False


# --- wrapTextWithAnsi: background color preservation ----------------------------------------------


def test_should_preserve_background_color_across_wrapped_lines_without_full_reset():
    bgBlue = "\x1b[44m"
    reset = "\x1b[0m"
    text = f"{bgBlue}hello world this is blue background text{reset}"

    wrapped = wrapTextWithAnsi(text, 15)

    # Each line should have background color
    for line in wrapped:
        assert bgBlue in line

    # Middle lines should NOT end with full reset (kills background for padding)
    for i in range(len(wrapped) - 1):
        assert wrapped[i].endswith("\x1b[0m") is False


def test_should_reset_underline_but_preserve_background_when_wrapping_underlined_text_inside_background():
    underlineOn = "\x1b[4m"
    underlineOff = "\x1b[24m"
    reset = "\x1b[0m"

    text = f"\x1b[41mprefix {underlineOn}UNDERLINED_CONTENT_THAT_WRAPS{underlineOff} suffix{reset}"

    wrapped = wrapTextWithAnsi(text, 20)

    # All lines should have background color 41 (either as \x1b[41m or combined like \x1b[4;41m)
    for line in wrapped:
        hasBgColor = "[41m" in line or ";41m" in line or "[41;" in line
        assert hasBgColor

    # Lines with underlined content should use underline-off at end, not full reset
    for i in range(len(wrapped) - 1):
        line = wrapped[i]
        # If this line has underline on, it should end with underline off (not full reset)
        if ("[4m" in line or "[4;" in line or ";4m" in line) and underlineOff not in line:
            assert line.endswith(underlineOff) is True
            assert line.endswith("\x1b[0m") is False


# --- wrapTextWithAnsi: basic wrapping -------------------------------------------------------------


def test_should_handle_lf_crlf_and_cr_line_endings():
    assert wrapTextWithAnsi("first\nsecond\r\nthird\rfourth", 80) == [
        "first",
        "second",
        "third",
        "fourth",
    ]


def test_should_preserve_ansi_state_across_crlf_and_cr_line_endings():
    red = "\x1b[31m"
    reset = "\x1b[0m"

    assert wrapTextWithAnsi(f"{red}first\r\nsecond\rthird{reset}", 80) == [
        f"{red}first",
        f"{red}second",
        f"{red}third{reset}",
    ]


def test_should_wrap_plain_text_correctly():
    text = "hello world this is a test"
    wrapped = wrapTextWithAnsi(text, 10)

    assert len(wrapped) > 1
    for line in wrapped:
        assert visibleWidth(line) <= 10


def test_should_break_cjk_runs_at_grapheme_boundaries_after_latin_text():
    text = "This is an example 中文汉字测试段落内容中文汉字测试段落内容."
    wrapped = wrapTextWithAnsi(text, 40)

    assert wrapped == ["This is an example 中文汉字测试段落内容", "中文汉字测试段落内容."]
    for line in wrapped:
        assert visibleWidth(line) <= 40


def test_should_preserve_color_codes_when_wrapping_cjk_runs():
    red = "\x1b[31m"
    reset = "\x1b[0m"
    text = f"{red}This is an example 中文汉字测试段落内容中文汉字测试段落内容.{reset}"
    wrapped = wrapTextWithAnsi(text, 40)

    assert len(wrapped) == 2
    assert wrapped[0] == f"{red}This is an example 中文汉字测试段落内容"
    assert wrapped[1] == f"{red}中文汉字测试段落内容.{reset}"
    for line in wrapped:
        assert visibleWidth(line) <= 40


def test_should_ignore_osc_133_semantic_markers_in_visible_width():
    text = "\x1b]133;A\x07hello\x1b]133;B\x07"
    assert visibleWidth(text) == 5


def test_should_ignore_osc_sequences_terminated_with_st_in_visible_width():
    text = "\x1b]133;A\x1b\\hello\x1b]133;B\x1b\\"
    assert visibleWidth(text) == 5


def test_should_treat_isolated_regional_indicators_as_width_2():
    assert visibleWidth("🇨") == 2
    assert visibleWidth("🇨🇳") == 2


def test_should_truncate_trailing_whitespace_that_exceeds_width():
    twoSpacesWrappedToWidth1 = wrapTextWithAnsi("  ", 1)
    assert visibleWidth(twoSpacesWrappedToWidth1[0]) <= 1


def test_should_preserve_color_codes_across_wraps():
    red = "\x1b[31m"
    reset = "\x1b[0m"
    text = f"{red}hello world this is red{reset}"

    wrapped = wrapTextWithAnsi(text, 10)

    # Each continuation line should start with red code
    for i in range(1, len(wrapped)):
        assert wrapped[i].startswith(red) is True

    # Middle lines should not end with full reset
    for i in range(len(wrapped) - 1):
        assert wrapped[i].endswith("\x1b[0m") is False


# --- wrapTextWithAnsi with OSC 8 hyperlinks -------------------------------------------------------


def test_re_emits_osc_8_open_at_the_start_of_continuation_lines():
    # A hyperlink whose text is long enough to wrap
    url = "https://example.com"
    # OSC 8 open + text that is 10 visible chars + OSC 8 close
    input_ = f"\x1b]8;;{url}\x1b\\0123456789\x1b]8;;\x1b\\"
    lines = wrapTextWithAnsi(input_, 6)

    # Every line that contains visible text from inside the hyperlink
    # should start with the OSC 8 open sequence (or be preceded by it).
    for line in lines:
        # If the line has visible content it must begin with the OSC 8 re-open
        # OR it is the line where the close appeared with no following content.
        stripped = js_regexp.replace(
            js_regexp.replace(line, RegExp(r"\x1b\]8;;[^\x1b\x07]*\x1b\\", "g"), ""), RegExp(r"\x1b\[[0-9;]*m", "g"), ""
        )
        if len(jsTrim(stripped)) > 0:
            assert line.startswith(f"\x1b]8;;{url}\x1b\\") or f"\x1b]8;;{url}\x1b\\" in line, (
                f'Line "{line}" has visible text but no OSC 8 re-open'
            )


def test_closes_osc_8_before_each_line_break():
    url = "https://example.com"
    input_ = f"\x1b]8;;{url}\x1b\\0123456789\x1b]8;;\x1b\\"
    lines = wrapTextWithAnsi(input_, 6)

    for i in range(len(lines) - 1):
        line = lines[i]
        # Every non-final line that is inside a hyperlink should end with the close
        if f"\x1b]8;;{url}\x1b\\" in line:
            assert line.endswith("\x1b]8;;\x1b\\"), f'Non-final line "{line}" is inside a hyperlink but does not close it'


def test_preserves_bel_terminators_when_wrapping_oauth_style_hyperlinks():
    url = f"https://example.com/oauth/{'a' * 32}"
    input_ = f"\x1b]8;;{url}\x07{url}\x1b]8;;\x07"
    lines = wrapTextWithAnsi(input_, 20)

    assert len(lines) > 1
    for line in lines:
        assert f"\x1b]8;;{url}\x07" in line, f'Line "{line}" does not reopen the hyperlink with BEL'
        assert f"\x1b]8;;{url}\x1b\\" not in line, f'Line "{line}" reopens the hyperlink with ST'
    for line in lines[:-1]:
        assert line.endswith("\x1b]8;;\x07"), f'Line "{line}" does not close the hyperlink with BEL'


def test_does_not_emit_osc_8_sequences_on_lines_that_are_outside_the_hyperlink():
    url = "https://example.com"
    input_ = f"before \x1b]8;;{url}\x1b\\link\x1b]8;;\x1b\\ after"
    lines = wrapTextWithAnsi(input_, 80)

    # With width 80 everything fits on one line; there should be exactly one
    # OSC 8 open and one OSC 8 close.
    assert len(lines) == 1
    openCount = len(js_regexp.match(lines[0], RegExp(r"\x1b\]8;;https:[^\x1b]+\x1b\\", "g")) or [])
    closeCount = len(js_regexp.match(lines[0], RegExp(r"\x1b\]8;;\x1b\\", "g")) or [])
    assert openCount == 1
    assert closeCount == 1

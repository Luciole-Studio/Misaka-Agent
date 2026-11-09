"""Translated from pi-tui's ``test/truncated-text.test.ts`` (v1.1.0).

MISAKA fork: chalk (level 3) is written out as its SGR codes: red ``ESC[31m``/``ESC[39m``, blue ``ESC[34m``/``ESC[39m``.
"""

from __future__ import annotations

import re

from misaka.ui.tui.components.truncated_text import TruncatedText
from misaka.ui.tui.utils import visibleWidth


def red(text: str) -> str:
    return f"\x1b[31m{text}\x1b[39m"


def blue(text: str) -> str:
    return f"\x1b[34m{text}\x1b[39m"


SGR = re.compile(r"\x1b\[[0-9;]*m")


def test_pads_output_lines_to_exactly_match_width():
    text = TruncatedText("Hello world", 1, 0)
    lines = text.render(50)

    # Should have exactly one content line (no vertical padding)
    assert len(lines) == 1

    # Line should be exactly 50 visible characters
    visibleLen = visibleWidth(lines[0])
    assert visibleLen == 50


def test_pads_output_with_vertical_padding_lines_to_width():
    text = TruncatedText("Hello", 0, 2)
    lines = text.render(40)

    # Should have 2 padding lines + 1 content line + 2 padding lines = 5 total
    assert len(lines) == 5

    # All lines should be exactly 40 characters
    for line in lines:
        assert visibleWidth(line) == 40


def test_truncates_long_text_and_pads_to_width():
    longText = "This is a very long piece of text that will definitely exceed the available width"
    text = TruncatedText(longText, 1, 0)
    lines = text.render(30)

    assert len(lines) == 1

    # Should be exactly 30 characters
    assert visibleWidth(lines[0]) == 30

    # Should contain ellipsis
    stripped = SGR.sub("", lines[0])
    assert "..." in stripped


def test_preserves_ansi_codes_in_output_and_pads_correctly():
    styledText = f"{red('Hello')} {blue('world')}"
    text = TruncatedText(styledText, 1, 0)
    lines = text.render(40)

    assert len(lines) == 1

    # Should be exactly 40 visible characters (ANSI codes don't count)
    assert visibleWidth(lines[0]) == 40

    # Should preserve the color codes
    assert "\x1b[" in lines[0]


def test_truncates_styled_text_and_adds_reset_code_before_ellipsis():
    longStyledText = red("This is a very long red text that will be truncated")
    text = TruncatedText(longStyledText, 1, 0)
    lines = text.render(20)

    assert len(lines) == 1

    # Should be exactly 20 visible characters
    assert visibleWidth(lines[0]) == 20

    # Should contain reset code before ellipsis
    assert "\x1b[0m..." in lines[0]


def test_handles_text_that_fits_exactly():
    # With paddingX=1, available width is 30-2=28
    # "Hello world" is 11 chars, fits comfortably
    text = TruncatedText("Hello world", 1, 0)
    lines = text.render(30)

    assert len(lines) == 1
    assert visibleWidth(lines[0]) == 30

    # Should NOT contain ellipsis
    stripped = SGR.sub("", lines[0])
    assert "..." not in stripped


def test_handles_empty_text():
    text = TruncatedText("", 1, 0)
    lines = text.render(30)

    assert len(lines) == 1
    assert visibleWidth(lines[0]) == 30


def test_stops_at_newline_and_only_shows_first_line():
    multilineText = "First line\nSecond line\nThird line"
    text = TruncatedText(multilineText, 1, 0)
    lines = text.render(40)

    assert len(lines) == 1
    assert visibleWidth(lines[0]) == 40

    # Should only contain "First line"
    stripped = SGR.sub("", lines[0]).strip()
    assert "First line" in stripped
    assert "Second line" not in stripped
    assert "Third line" not in stripped


def test_truncates_first_line_even_with_newlines_in_text():
    longMultilineText = "This is a very long first line that needs truncation\nSecond line"
    text = TruncatedText(longMultilineText, 1, 0)
    lines = text.render(25)

    assert len(lines) == 1
    assert visibleWidth(lines[0]) == 25

    # Should contain ellipsis and not second line
    stripped = SGR.sub("", lines[0])
    assert "..." in stripped
    assert "Second line" not in stripped

"""Translated from pi-tui's ``test/regression-slice-by-column-ansi-order.test.ts``, the ``extractSegments`` cases of
``test/regression-overlay-cjk-boundary.test.ts`` and the helper cases of ``test/tab-width.test.ts`` (v1.1.0).

The compositing cases of those two files exercise ``compositeTuiLine`` and ``TuiMainScreen``; they live with the
TUI core tests."""

from misaka.ui.tui.utils import (
    extractSegments,
    normalizeTerminalOutput,
    sliceByColumn,
    sliceWithWidth,
    visibleWidth,
)

# --- sliceByColumn ANSI order regression (https://github.com/earendil-works/pi/issues/10169) -------


def test_keeps_a_reset_at_the_slice_start_after_earlier_style_codes():
    line = "\x1b[32mfoo\x1b[39m bar"
    assert sliceByColumn(line, 3, 4, True) == "\x1b[32m\x1b[39m bar"


def test_does_not_leak_color_into_text_after_a_highlighted_token():
    line = "Another \x1b[35malpha\x1b[39m line with \x1b[35mbeta\x1b[39m later."
    after = sliceByColumn(line, 13, 100, True)
    assert after == "\x1b[35m\x1b[39m line with \x1b[35mbeta\x1b[39m later."


# --- overlay CJK boundary regression --------------------------------------------------------------


def test_excludes_a_wide_grapheme_from_before_when_overlay_starts_inside_it():
    segments = extractSegments("abcd让EFGH", 5, 9, 11, True)

    assert segments.before == "abcd"
    assert segments.beforeWidth == 4
    assert visibleWidth(segments.before) == segments.beforeWidth
    assert segments.after == "H"
    assert segments.afterWidth == 1


def test_keeps_ascii_before_segment_behavior_at_the_same_boundary():
    segments = extractSegments("abcdG EFGH", 5, 9, 11, True)

    assert segments.before == "abcdG"
    assert segments.beforeWidth == 5
    assert visibleWidth(segments.before) == segments.beforeWidth


# --- tab width accounting -------------------------------------------------------------------------


def test_keeps_slice_helper_widths_consistent_with_visible_width():
    text = "out 192M\t.pi/skill-tests/results-ha"
    slice_ = sliceWithWidth(text, 0, 10, True)

    assert slice_.text == "out 192M"
    assert slice_.width == 8
    assert visibleWidth(slice_.text) == slice_.width


def test_keeps_overlay_segment_widths_consistent_with_visible_width():
    text = "out 192M\t.pi/skill-tests/results-ha"
    segments = extractSegments(text, 10, 13, 10, True)

    assert segments.before == "out 192M"
    assert segments.beforeWidth == 8
    assert visibleWidth(segments.before) == segments.beforeWidth

    tabFits = extractSegments(text, 11, 13, 10, True)
    assert tabFits.before == "out 192M\t"
    assert tabFits.beforeWidth == 11
    assert visibleWidth(tabFits.before) == tabFits.beforeWidth


def test_keeps_tabs_inside_terminal_control_sequences_byte_identical():
    controlSequences = [
        "\x1b]8;;https://example.test/a\tb\x07",
        "\x1b]0;window\ttitle\x1b\\",
        "\x1b_payload\tdata\x1b\\",
    ]

    for controlSequence in controlSequences:
        assert normalizeTerminalOutput(f"{controlSequence}label\ttext") == f"{controlSequence}label   text"

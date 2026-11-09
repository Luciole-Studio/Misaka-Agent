"""Translated from pi-tui's ``test/visible-width.test.ts`` and ``test/regression-regional-indicator-width.test.ts``
(v1.1.0)."""

from misaka.ui.tui.utils import visibleWidth, wrapTextWithAnsi

# --- visibleWidth ---------------------------------------------------------------------------------


def test_measures_styled_ascii_without_counting_escape_sequences():
    assert visibleWidth("\x1b[38;5;4mhello\x1b[39m world") == 11
    assert visibleWidth("\x1b]8;;https://example.com\x07link\x1b]8;;\x07") == 4
    assert visibleWidth("\x1b]133;A\x1b\\prompt") == 6
    assert visibleWidth("\x1b_pi:c\x07cursor") == 6


def test_counts_tabs_as_three_columns_in_styled_text():
    assert visibleWidth("\x1b[1ma\tb\x1b[22m") == 5


def test_measures_styled_non_ascii_text():
    assert visibleWidth("\x1b[31m日本\x1b[39m ok") == 7
    assert visibleWidth("\x1b[31m─→\x1b[39m") == 2


def test_treats_unterminated_escape_sequences_as_zero_width_control_characters():
    assert visibleWidth("\x1b[31") == 3
    assert visibleWidth("a\x1b") == 1


# --- regional indicator width regression ----------------------------------------------------------


def test_treats_partial_flag_grapheme_as_full_width_to_avoid_streaming_render_drift():
    # Repro context:
    # During streaming, "🇨🇳" often appears as an intermediate "🇨" first.
    # If "🇨" is measured as width 1 while terminal renders it as width 2,
    # differential rendering can drift and leave stale characters on screen.
    partialFlag = "🇨"
    listLine = "      - 🇨"

    assert visibleWidth(partialFlag) == 2
    assert visibleWidth(listLine) == 10


def test_wraps_intermediate_partial_flag_list_line_before_overflow():
    # Width 9 cannot fit "      - 🇨" if 🇨 is width 2 (8 + 2 = 10).
    # This must wrap to avoid terminal auto-wrap mismatch.
    wrapped = wrapTextWithAnsi("      - 🇨", 9)

    assert len(wrapped) == 2
    assert visibleWidth(wrapped[0] or "") == 7
    assert visibleWidth(wrapped[1] or "") == 2


def test_treats_all_regional_indicator_singleton_graphemes_as_width_2():
    for cp in range(0x1F1E6, 0x1F1FF + 1):
        regionalIndicator = chr(cp)
        assert visibleWidth(regionalIndicator) == 2, f"Expected {regionalIndicator} (U+{cp:X}) to be width 2"


def test_keeps_full_flag_pairs_at_width_2():
    samples = ["🇯🇵", "🇺🇸", "🇬🇧", "🇨🇳", "🇩🇪", "🇫🇷"]
    for flag in samples:
        assert visibleWidth(flag) == 2, f"Expected {flag} to be width 2"


def test_keeps_common_streaming_emoji_intermediates_at_stable_width():
    samples = ["👍", "👍🏻", "✅", "⚡", "⚡️", "👨", "👨‍💻", "🏳️‍🌈"]
    for sample in samples:
        assert visibleWidth(sample) == 2, f"Expected {sample} to be width 2"

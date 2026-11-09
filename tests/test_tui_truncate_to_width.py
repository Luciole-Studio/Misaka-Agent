"""Translated from pi-tui's ``test/truncate-to-width.test.ts`` (v1.1.0)."""

from misaka.ui.tui.utils import normalizeTerminalOutput, truncateToWidth, visibleWidth

# --- truncateToWidth ------------------------------------------------------------------------------


def test_keeps_output_within_width_for_very_large_unicode_input():
    text = "🙂界" * 100_000
    truncated = truncateToWidth(text, 40, "…")

    assert visibleWidth(truncated) <= 40
    assert truncated.endswith("…\x1b[0m") is True


def test_preserves_ansi_styling_for_kept_text_and_resets_before_and_after_ellipsis():
    text = f"\x1b[31m{'hello ' * 1000}\x1b[0m"
    truncated = truncateToWidth(text, 20, "…")

    assert visibleWidth(truncated) <= 20
    assert ("\x1b[31m" in truncated) is True
    assert truncated.endswith("\x1b[0m…\x1b[0m") is True


def test_closes_a_bel_terminated_osc_8_link_when_truncating_its_label():
    open_ = "\x1b]8;;https://example.com\x07"
    close = "\x1b]8;;\x07"
    text = f"{open_}some-longer-label-here{close}"

    assert truncateToWidth(text, 15) == f"{open_}some-longer-{close}\x1b[0m...\x1b[0m"


def test_handles_malformed_ansi_escape_prefixes_without_hanging():
    text = f"abc\x1bnot-ansi {'🙂' * 1000}"
    truncated = truncateToWidth(text, 20, "…")

    assert visibleWidth(truncated) <= 20


def test_clips_wide_ellipsis_safely_and_brackets_it_with_resets():
    assert truncateToWidth("abcdef", 1, "🙂") == ""
    assert truncateToWidth("abcdef", 2, "🙂") == "\x1b[0m🙂\x1b[0m"
    assert visibleWidth(truncateToWidth("abcdef", 2, "🙂")) <= 2


def test_returns_the_original_text_when_it_already_fits_even_if_ellipsis_is_too_wide():
    assert truncateToWidth("a", 2, "🙂") == "a"
    assert truncateToWidth("界", 2, "🙂") == "界"


def test_pads_truncated_output_to_requested_width():
    truncated = truncateToWidth("🙂界🙂界🙂界", 8, "…", True)
    assert visibleWidth(truncated) == 8


def test_adds_a_trailing_reset_when_truncating_without_an_ellipsis():
    truncated = truncateToWidth(f"\x1b[31m{'hello' * 100}", 10, "")
    assert visibleWidth(truncated) <= 10
    assert truncated.endswith("\x1b[0m") is True


def test_keeps_a_contiguous_prefix_instead_of_skipping_a_wide_grapheme_and_resuming_later():
    truncated = truncateToWidth("🙂\t界 \x1b_abc\x07", 7, "…", True)
    assert truncated == "🙂\t\x1b[0m…\x1b[0m "


# --- visibleWidth ---------------------------------------------------------------------------------


def test_counts_tabs_inline_and_skips_ansi_inline():
    assert visibleWidth("\t\x1b[31m界\x1b[0m") == 5


def test_counts_indic_conjunct_spacing_code_points_within_grapheme_clusters():
    assert visibleWidth("र्क") == 2
    assert visibleWidth("नेटवर्क") == 5
    assert visibleWidth("सर्वाधिकार सुरक्षित। ऑर्डर पर क्लिक करें") == 33
    assert visibleWidth("র্ক") == 2
    assert visibleWidth("ર્ક") == 2
    assert visibleWidth("ର୍କ") == 2
    assert visibleWidth("ర్క") == 2
    assert visibleWidth("ര്‍ക") == 2


def test_keeps_ordinary_combining_marks_zero_width():
    assert visibleWidth("é") == 1
    assert visibleWidth("čřžůú") == 5
    assert visibleWidth("שָׁ") == 1
    assert visibleWidth("بّ") == 1
    assert visibleWidth("རྐ") == 1
    assert visibleWidth("ᜠ᜴") == 1
    assert visibleWidth("가〮") == 2
    assert visibleWidth("가〯") == 2


def test_keeps_cjk_and_japanese_width_accounting_unchanged():
    assert visibleWidth("网络") == 4
    assert visibleWidth("ネットワーク") == 12
    assert visibleWidth("が") == 2
    assert visibleWidth("が") == 2


def test_counts_myanmar_marks_that_terminals_allocate_cells_for():
    assert visibleWidth("ကာ") == 2
    assert visibleWidth("ကေ") == 2
    assert visibleWidth("က်") == 2
    assert visibleWidth("ကျ") == 2
    assert visibleWidth("ကြ") == 2
    assert visibleWidth("ကဳ") == 2
    assert visibleWidth("ကဴ") == 2
    assert visibleWidth("ကဵ") == 2
    assert visibleWidth("ကး") == 2
    assert visibleWidth("ကို") == 1
    assert visibleWidth("က္") == 1


def test_keeps_thai_and_lao_am_clusters_at_their_normal_cell_width():
    assert visibleWidth("ำ") == 1
    assert visibleWidth("ຳ") == 1
    assert visibleWidth("กำ") == 2
    assert visibleWidth("ກຳ") == 2


def test_normalizes_thai_and_lao_am_vowels_only_for_terminal_output():
    assert normalizeTerminalOutput("ำ") == "ํา"
    assert normalizeTerminalOutput("ຳ") == "ໍາ"
    assert visibleWidth(normalizeTerminalOutput("ำabc")) == visibleWidth("ำabc")
    assert visibleWidth(normalizeTerminalOutput("ຳabc")) == visibleWidth("ຳabc")

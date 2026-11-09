"""Translated from pi-tui's ``test/word-navigation.test.ts`` (v1.1.0)."""

import pytest

from misaka.ui.tui.utils import SegmentData
from misaka.ui.tui.word_navigation import (
    WordNavigationOptions,
    findWordBackward,
    findWordForward,
)

# --- findWordBackward -----------------------------------------------------------------------------


def test_backward_basic_words_hello_world():
    text = "hello world"
    assert findWordBackward(text, 11) == 6
    assert findWordBackward(text, 6) == 0


def test_backward_dotted_foo_bar():
    text = "foo.bar"
    assert findWordBackward(text, 7) == 4
    assert findWordBackward(text, 4) == 3
    assert findWordBackward(text, 3) == 0


def test_backward_colon_foo_bar():
    text = "foo:bar"
    assert findWordBackward(text, 7) == 4
    assert findWordBackward(text, 4) == 3
    assert findWordBackward(text, 3) == 0


def test_backward_path_path_to_file():
    text = "path/to/file"
    assert findWordBackward(text, 12) == 8
    assert findWordBackward(text, 8) == 7
    # "/to" is one word-like segment with "/" as punctuation boundary
    assert findWordBackward(text, 7) == 5
    assert findWordBackward(text, 5) == 4
    assert findWordBackward(text, 4) == 0


# MISAKA fork: ICU groups `你好` and `世界` as words with its CJK dictionary; the Unicode default word boundaries the
# `regex` module offers break between every ideograph, so a word step over a Han run moves one character.
@pytest.mark.xfail(strict=True, reason="no ICU dictionary segmentation for Han (each ideograph is a word)")
def test_backward_cjk_mixed():
    text = "你好世界 test"
    assert findWordBackward(text, len(text)) == 5
    # Intl.Segmenter treats each CJK char as a separate word-like segment
    assert findWordBackward(text, 5) == 2
    assert findWordBackward(text, 2) == 0


def test_backward_whitespace_at_boundaries():
    text = "  hello  "
    assert findWordBackward(text, 9) == 2
    assert findWordBackward(text, 2) == 0


def test_backward_punctuation_run_foo_bar():
    text = "foo...bar"
    assert findWordBackward(text, 9) == 6
    assert findWordBackward(text, 6) == 3
    assert findWordBackward(text, 3) == 0


def test_backward_cursor_at_0_returns_0():
    assert findWordBackward("hello", 0) == 0


# --- findWordForward ------------------------------------------------------------------------------


def test_forward_basic_words_hello_world():
    text = "hello world"
    assert findWordForward(text, 0) == 5
    assert findWordForward(text, 5) == 11


def test_forward_dotted_foo_bar():
    text = "foo.bar"
    assert findWordForward(text, 0) == 3
    assert findWordForward(text, 3) == 4
    assert findWordForward(text, 4) == 7


def test_forward_colon_foo_bar():
    text = "foo:bar"
    assert findWordForward(text, 0) == 3
    assert findWordForward(text, 3) == 4
    assert findWordForward(text, 4) == 7


def test_forward_path_path_to_file():
    text = "path/to/file"
    assert findWordForward(text, 0) == 4
    assert findWordForward(text, 4) == 5
    assert findWordForward(text, 5) == 7
    assert findWordForward(text, 7) == 8
    assert findWordForward(text, 8) == 12


def test_forward_cjk_mixed():
    text = "你好世界 test"
    firstEnd = findWordForward(text, 0)
    assert firstEnd > 0
    assert firstEnd <= 4
    # Walk to end
    pos = 0
    while pos < len(text):
        next_ = findWordForward(text, pos)
        if next_ == pos:
            break
        pos = next_
    assert pos == len(text)


def test_forward_whitespace_at_boundaries():
    text = "  hello  "
    assert findWordForward(text, 0) == 7
    assert findWordForward(text, 7) == 9


def test_forward_punctuation_run_foo_bar():
    text = "foo...bar"
    assert findWordForward(text, 0) == 3
    assert findWordForward(text, 3) == 6
    assert findWordForward(text, 6) == 9


def test_forward_cursor_at_end_returns_end():
    assert findWordForward("hello", 5) == 5


# --- atomic segments ------------------------------------------------------------------------------

marker = "[paste #1 +5 lines]"
text = f"hello {marker} world"


def isAtomic(s: str) -> bool:
    return s == marker


# The functions slice text before calling segment(), so we map each expected
# substring to its pre-split segments.
segmentMap: dict[str, list[SegmentData]] = {
    # full text (not used but for clarity); backward from end: slice(0, 31) = full text
    text: [
        SegmentData(segment="hello", index=0, input=text, isWordLike=True),
        SegmentData(segment=" ", index=5, input=text, isWordLike=False),
        SegmentData(segment=marker, index=6, input=text, isWordLike=True),
        SegmentData(segment=" ", index=25, input=text, isWordLike=False),
        SegmentData(segment="world", index=26, input=text, isWordLike=True),
    ],
    # backward from 26: slice(0, 26) = "hello [paste #1 +5 lines] "
    text[:26]: [
        SegmentData(segment="hello", index=0, input=text, isWordLike=True),
        SegmentData(segment=" ", index=5, input=text, isWordLike=False),
        SegmentData(segment=marker, index=6, input=text, isWordLike=True),
        SegmentData(segment=" ", index=25, input=text, isWordLike=False),
    ],
    # forward from 6: slice(6) = "[paste #1 +5 lines] world"
    text[6:]: [
        SegmentData(segment=marker, index=0, input=text, isWordLike=True),
        SegmentData(segment=" ", index=19, input=text, isWordLike=False),
        SegmentData(segment="world", index=20, input=text, isWordLike=True),
    ],
}

opts = WordNavigationOptions(segment=lambda input_: segmentMap.get(input_, []), isAtomicSegment=isAtomic)


def test_backward_skips_word_then_stops_before_atomic_marker():
    assert findWordBackward(text, len(text), opts) == 26


def test_backward_skips_whitespace_then_atomic_marker_as_one_unit():
    assert findWordBackward(text, 26, opts) == 6


def test_forward_skips_atomic_marker_as_one_unit():
    assert findWordForward(text, 6, opts) == 6 + len(marker)

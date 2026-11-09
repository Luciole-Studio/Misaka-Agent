"""Translated from pi-tui's ``packages/tui/src/word-navigation.ts`` (v1.1.0).

Offsets count code points (Python strings), where upstream counts UTF-16 code units.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from misaka.utils import js_regexp
from misaka.utils.js_regexp import RegExp

from .utils import PUNCTUATION_REGEX, SegmentData, getWordSegmenter, isWhitespaceChar

wordSegmenter = getWordSegmenter()


@dataclass(slots=True)
class WordNavigationOptions:
    """Options for word navigation functions.
    When omitted, uses the default Intl.Segmenter word segmentation.
    """

    # Custom segmenter returning word segments for the given text.
    segment: Callable[[str], Iterable[SegmentData]] | None = None
    # Predicate identifying atomic segments that should be treated as single units (e.g. paste markers).
    isAtomicSegment: Callable[[str], bool] | None = None


def findWordBackward(text: str, cursor: int, options: WordNavigationOptions | None = None) -> int:
    """Find the cursor position after moving one word backward from `cursor` in `text`.
    Skips trailing whitespace, then stops at the next word/punctuation boundary.

    Pure function - does not mutate any state.
    """
    if cursor <= 0:
        return 0

    textBeforeCursor = text[:cursor]
    segmentFn = options.segment if options is not None else None
    isAtomic = options.isAtomicSegment if options is not None else None
    segments = list(segmentFn(textBeforeCursor)) if segmentFn else list(wordSegmenter.segment(textBeforeCursor))
    newCursor = cursor

    # Skip trailing whitespace
    while (
        len(segments) > 0
        and not (isAtomic is not None and isAtomic(segments[-1].segment or ""))
        and isWhitespaceChar(segments[-1].segment or "")
    ):
        newCursor -= len(segments.pop().segment)

    if len(segments) == 0:
        return newCursor

    last = segments[-1]

    if isAtomic is not None and isAtomic(last.segment):
        # Skip one atomic segment.
        newCursor -= len(last.segment)
    elif last.isWordLike:
        # Skip inside one word-like segment, preserving ASCII punctuation boundaries.
        segment = last.segment
        matches = list(js_regexp.matchAll(segment, RegExp(PUNCTUATION_REGEX, "g")))
        if len(matches) <= 0:
            newCursor -= len(segment)
        else:
            lastMatch = matches[-1]
            newCursor -= len(segment) - (lastMatch.index + len(lastMatch[0]))
    else:
        # Skip non-word non-whitespace run (punctuation)
        while (
            len(segments) > 0
            and not (isAtomic is not None and isAtomic(segments[-1].segment or ""))
            and not segments[-1].isWordLike
            and not isWhitespaceChar(segments[-1].segment or "")
        ):
            newCursor -= len(segments.pop().segment)

    return newCursor


def findWordForward(text: str, cursor: int, options: WordNavigationOptions | None = None) -> int:
    """Find the cursor position after moving one word forward from `cursor` in `text`.
    Skips leading whitespace, then stops at the next word/punctuation boundary.

    Pure function - does not mutate any state.
    """
    if cursor >= len(text):
        return len(text)

    textAfterCursor = text[cursor:]
    segmentFn = options.segment if options is not None else None
    isAtomic = options.isAtomicSegment if options is not None else None
    segments = segmentFn(textAfterCursor) if segmentFn else wordSegmenter.segment(textAfterCursor)
    iterator = iter(segments)
    next_ = next(iterator, None)
    newCursor = cursor

    # Skip leading whitespace
    while next_ is not None and not (isAtomic is not None and isAtomic(next_.segment)) and isWhitespaceChar(
        next_.segment
    ):
        newCursor += len(next_.segment)
        next_ = next(iterator, None)

    if next_ is None:
        return newCursor

    if isAtomic is not None and isAtomic(next_.segment):
        # Skip one atomic segment.
        newCursor += len(next_.segment)
    elif next_.isWordLike:
        # Skip inside one word-like segment, preserving ASCII punctuation boundaries.
        punctuation = PUNCTUATION_REGEX.exec(next_.segment)
        newCursor += punctuation.index if punctuation is not None else len(next_.segment)
    else:
        # Skip non-word non-whitespace run (punctuation)
        while (
            next_ is not None
            and not (isAtomic is not None and isAtomic(next_.segment))
            and not next_.isWordLike
            and not isWhitespaceChar(next_.segment)
        ):
            newCursor += len(next_.segment)
            next_ = next(iterator, None)

    return newCursor


__all__ = ["WordNavigationOptions", "findWordBackward", "findWordForward"]

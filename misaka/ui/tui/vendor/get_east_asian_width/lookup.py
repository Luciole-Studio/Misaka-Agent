"""Translated from get-east-asian-width 1.6.0 ``lookup.js`` (MIT)."""

from __future__ import annotations

from collections.abc import Sequence

from .lookup_data import (
    ambiguousMaximumCodePoint,
    ambiguousMinimalCodePoint,
    ambiguousRanges,
    fullwidthMaximumCodePoint,
    fullwidthMinimalCodePoint,
    fullwidthRanges,
    halfwidthMaximumCodePoint,
    halfwidthMinimalCodePoint,
    halfwidthRanges,
    narrowMaximumCodePoint,
    narrowMinimalCodePoint,
    narrowRanges,
    wideMaximumCodePoint,
    wideMinimalCodePoint,
    wideRanges,
)
from .utilities import isInRange

commonCjkCodePoint = 0x4E_00


# Use a hot-path range so common `isWide` calls can skip binary search.
# The range containing U+4E00 covers common CJK ideographs;
# fallback to the largest range for resilience to Unicode table changes.
def findWideFastPathRange(ranges: Sequence[int]) -> tuple[int, int]:
    fastPathStart = ranges[0]
    fastPathEnd = ranges[1]

    for index in range(0, len(ranges), 2):
        start = ranges[index]
        end = ranges[index + 1]

        if commonCjkCodePoint >= start and commonCjkCodePoint <= end:
            return (start, end)

        if (end - start) > (fastPathEnd - fastPathStart):
            fastPathStart = start
            fastPathEnd = end

    return (fastPathStart, fastPathEnd)


wideFastPathStart, wideFastPathEnd = findWideFastPathRange(wideRanges)


def isAmbiguous(codePoint: int) -> bool:
    if codePoint < ambiguousMinimalCodePoint or codePoint > ambiguousMaximumCodePoint:
        return False

    return isInRange(ambiguousRanges, codePoint)


def isFullWidth(codePoint: int) -> bool:
    if codePoint < fullwidthMinimalCodePoint or codePoint > fullwidthMaximumCodePoint:
        return False

    return isInRange(fullwidthRanges, codePoint)


def isHalfWidth(codePoint: int) -> bool:
    if codePoint < halfwidthMinimalCodePoint or codePoint > halfwidthMaximumCodePoint:
        return False

    return isInRange(halfwidthRanges, codePoint)


def isNarrow(codePoint: int) -> bool:
    if codePoint < narrowMinimalCodePoint or codePoint > narrowMaximumCodePoint:
        return False

    return isInRange(narrowRanges, codePoint)


def isWide(codePoint: int) -> bool:
    if codePoint >= wideFastPathStart and codePoint <= wideFastPathEnd:
        return True

    if codePoint < wideMinimalCodePoint or codePoint > wideMaximumCodePoint:
        return False

    return isInRange(wideRanges, codePoint)


def getCategory(codePoint: int) -> str:
    if isAmbiguous(codePoint):
        return "ambiguous"

    if isFullWidth(codePoint):
        return "fullwidth"

    if isHalfWidth(codePoint):
        return "halfwidth"

    if isNarrow(codePoint):
        return "narrow"

    if isWide(codePoint):
        return "wide"

    return "neutral"


__all__ = ["getCategory", "isAmbiguous", "isFullWidth", "isWide"]

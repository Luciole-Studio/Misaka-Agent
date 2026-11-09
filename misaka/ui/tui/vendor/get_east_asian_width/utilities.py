"""Translated from get-east-asian-width 1.6.0 ``utilities.js`` (MIT)."""

from __future__ import annotations

from collections.abc import Sequence


def isInRange(ranges: Sequence[int], codePoint: int) -> bool:
    """Binary search on a sorted flat array of [start, end] pairs.

    :param ranges: Flat array of inclusive [start, end] range pairs, e.g. [0, 5, 10, 20].
    :param codePoint: The value to search for.
    :returns: Whether the value falls within any of the ranges.
    """
    low = 0
    high = len(ranges) // 2 - 1
    while low <= high:
        mid = (low + high) // 2
        i = mid * 2
        if codePoint < ranges[i]:
            high = mid - 1
        elif codePoint > ranges[i + 1]:
            low = mid + 1
        else:
            return True

    return False


__all__ = ["isInRange"]

"""Display width, measured in grapheme clusters.

A cluster is the unit both of measuring and of painting, so a box is always
sized for exactly what gets drawn into it. Splitting those two — sizing by
cluster but painting by code point — is what makes `👨‍👩‍👧` overflow its
border in the Rust original.

Clustering comes from ``Intl.Segmenter`` (UAX #29), which already handles ZWJ
sequences, skin-tone modifiers, variation selectors, keycaps, flags and
Hangul. Per-code-point widths are generated from the ``unicode-width`` crate.

Translated from grok-mermaid 0.2.3's ``src/width.ts``. MISAKA fork: clusters are the ``regex`` module's ``\\X``, the
extended grapheme clusters ICU's segmenter yields.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Final

import regex

from .width_data import WIDTHS

_GRAPHEME: Final = regex.compile(r"\X")

VS16: Final = 0xFE0F


def isRegionalIndicator(cp: int) -> bool:
    return 0x1F1E6 <= cp <= 0x1F1FF


def codePointWidth(cp: int) -> int:
    """Width of one code point; the table covers the whole code point space."""
    lo = 0
    hi = len(WIDTHS) - 1
    while lo <= hi:
        mid = (lo + hi) >> 1
        run = WIDTHS[mid]
        if cp < run[0]:
            hi = mid - 1
        elif cp > run[1]:
            lo = mid + 1
        else:
            return run[2]
    return 1


def clusterWidth(cluster: str) -> int:
    """Columns occupied by one grapheme cluster.

    The widest code point wins, so a base plus its combining marks measures as
    the base. Two adjustments: a variation selector requesting emoji
    presentation forces two columns, as does a regional indicator pair (a flag).

    Zero is a real answer — a soft hyphen or zero-width space occupies nothing,
    and callers skip painting such a cluster rather than reserving a cell.
    """
    w = 0
    vs16 = False
    regional = 0
    for ch in cluster:
        cp = ord(ch)
        if cp == VS16:
            vs16 = True
        if isRegionalIndicator(cp):
            regional += 1
        cw = codePointWidth(cp)
        w = max(w, cw)
    return 2 if vs16 or regional >= 2 else w


def clusters(s: str) -> Iterator[str]:
    """Iterate grapheme clusters, so no loop can split one."""
    for match in _GRAPHEME.finditer(s):
        yield match.group()


def measured(s: str) -> Iterator[tuple[str, int]]:
    """Iterate clusters paired with their display width."""
    for match in _GRAPHEME.finditer(s):
        segment = match.group()
        yield segment, clusterWidth(segment)


def stringWidth(s: str) -> int:
    """Display columns of a string."""
    return sum(clusterWidth(match.group()) for match in _GRAPHEME.finditer(s))


__all__ = ["clusterWidth", "clusters", "measured", "stringWidth"]

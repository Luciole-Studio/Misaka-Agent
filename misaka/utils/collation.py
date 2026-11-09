"""JavaScript's ``String.prototype.localeCompare``, as pi's sorts call it.

Node answers it with ICU collation. This covers what model ids, provider names and the
like hold: ASCII characters in ICU's order (spaces, punctuation, symbols, digits, then
letters), compared first without accents or case, then by accents, then by case with
lower before upper. Characters outside ASCII sort by code point after all of those.
"""

from __future__ import annotations

import unicodedata

# ICU's root order of printable ASCII, case folded.
_ASCII_ORDER = " _-,;:!?.'\"()[]{}@*/\\&#%`^+<=>|~$0123456789abcdefghijklmnopqrstuvwxyz"
_PRIMARY = {char: index for index, char in enumerate(_ASCII_ORDER)}


def _primary(char: str) -> int:
    return _PRIMARY.get(char, len(_ASCII_ORDER) + ord(char))


def localeSortKey(value: str) -> tuple[tuple[int, ...], tuple[tuple[int, ...], ...], tuple[int, ...]]:
    """A key that orders strings as ``a.localeCompare(b)`` does."""
    primary: list[int] = []
    secondary: list[tuple[int, ...]] = []
    tertiary: list[int] = []
    for char in value:
        decomposed = unicodedata.normalize("NFD", char)
        base = decomposed[0]
        folded = base.lower()
        primary.append(_primary(folded))
        secondary.append(tuple(ord(mark) for mark in decomposed[1:]))
        tertiary.append(0 if base == folded else 1)
    return tuple(primary), tuple(secondary), tuple(tertiary)


def localeCompare(a: str, b: str) -> int:
    """``a.localeCompare(b)``: negative, zero or positive."""
    keyA, keyB = localeSortKey(a), localeSortKey(b)
    return (keyA > keyB) - (keyA < keyB)


__all__ = ["localeCompare", "localeSortKey"]

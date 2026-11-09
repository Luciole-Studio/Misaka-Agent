"""get-east-asian-width 1.6.0 (https://github.com/sindresorhus/get-east-asian-width, MIT;
``LICENSES/get-east-asian-width.txt``), the width table pi's TUI measures text with, ported to Python.

Translated from its ``index.js``.
"""

from __future__ import annotations

from typing import Any

from .lookup import getCategory, isAmbiguous, isFullWidth, isWide


def validate(codePoint: Any) -> None:
    if not isinstance(codePoint, int) or isinstance(codePoint, bool) or abs(codePoint) > 2**53 - 1:
        raise TypeError(f"Expected a code point, got `{type(codePoint).__name__}`.")


def eastAsianWidthType(codePoint: int) -> str:
    validate(codePoint)

    return getCategory(codePoint)


def eastAsianWidth(codePoint: int, *, ambiguousAsWide: bool = False) -> int:
    validate(codePoint)

    if isFullWidth(codePoint) or isWide(codePoint) or (ambiguousAsWide and isAmbiguous(codePoint)):
        return 2

    return 1


# Private exports for https://github.com/sindresorhus/is-fullwidth-code-point
_isFullWidth = isFullWidth
_isWide = isWide

__all__ = ["_isFullWidth", "_isWide", "eastAsianWidth", "eastAsianWidthType"]

"""Translated from pi-tui's ``packages/tui/src/colors.ts`` (v1.1.0), with ``rgbToOklab`` from ``oklab.ts``.

MISAKA fork: only what ordinary themes use is ported. OKLCH and OKHSL colors (``OklchColorValue``,
``oklchColor``, ``okhslColor``, ``colorToOkhsl``, the ``oklch()``/``okhsl()`` syntax of ``parseColor``, gamut
mapping and the OKLCH space of ``mixColors``) belong to pi's system theme and its theme-JSON color syntax, which
are not ported (D11). Oklab is kept for ``colorToOklch``, which themes use to detect their appearance.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, TypedDict

from misaka.ui.tui.terminal_colors import RgbColor
from misaka.utils.js import jsIsInteger, jsRound
from misaka.utils.js_regexp import RegExp


@dataclass(slots=True, frozen=True)
class IndexedColor:
    index: int
    kind: Literal["indexed"] = "indexed"


@dataclass(slots=True, frozen=True)
class RgbColorValue:
    r: float
    g: float
    b: float
    kind: Literal["rgb"] = "rgb"


# A concrete color. Every color can be converted to sRGB, so color math never fails.
type Color = IndexedColor | RgbColorValue
type TerminalColorMode = Literal["256color", "truecolor"]
# MISAKA fork: no "oklch" space (D11).
type ColorMixSpace = Literal["srgb"]


@dataclass(slots=True)
class OklchChannels:
    l: float
    c: float
    h: float


class TextAttributes(TypedDict, total=False):
    bold: bool
    dim: bool
    italic: bool
    underline: bool
    inverse: bool
    strikethrough: bool


class TextStyle(TextAttributes, total=False):
    fg: Color
    bg: Color


def requireFinite(value: float, name: str) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")


def indexedColor(index: float) -> IndexedColor:
    if not jsIsInteger(index) or index < 0 or index > 255:
        raise ValueError(f"ANSI color index must be an integer from 0 to 255: {index}")
    return IndexedColor(index=int(index))


def rgbColor(r: float, g: float, b: float) -> RgbColorValue:
    for name, value in (("r", r), ("g", g), ("b", b)):
        requireFinite(value, name)
        if value < 0 or value > 255:
            raise ValueError(f"{name} must be between 0 and 255: {value}")
    return RgbColorValue(r=r, g=g, b=b)


_HEX_PATTERN = RegExp(r"^#([\da-f]{3}|[\da-f]{6})$", "i")


def parseColor(value: str | float) -> Color:
    if not isinstance(value, str):
        return indexedColor(value)

    hex = _HEX_PATTERN.exec(value)
    if hex:
        digits = "".join(digit + digit for digit in hex[1]) if len(hex[1]) == 3 else hex[1]
        return rgbColor(
            int(digits[0:2], 16),
            int(digits[2:4], 16),
            int(digits[4:6], 16),
        )

    # MISAKA fork: no oklch() / okhsl() values (D11).
    raise ValueError(f"Invalid color value: {value}")


BASIC_COLORS: tuple[RgbColor, ...] = (
    RgbColor(r=0, g=0, b=0),
    RgbColor(r=128, g=0, b=0),
    RgbColor(r=0, g=128, b=0),
    RgbColor(r=128, g=128, b=0),
    RgbColor(r=0, g=0, b=128),
    RgbColor(r=128, g=0, b=128),
    RgbColor(r=0, g=128, b=128),
    RgbColor(r=192, g=192, b=192),
    RgbColor(r=128, g=128, b=128),
    RgbColor(r=255, g=0, b=0),
    RgbColor(r=0, g=255, b=0),
    RgbColor(r=255, g=255, b=0),
    RgbColor(r=0, g=0, b=255),
    RgbColor(r=255, g=0, b=255),
    RgbColor(r=0, g=255, b=255),
    RgbColor(r=255, g=255, b=255),
)
CUBE_VALUES = (0, 95, 135, 175, 215, 255)
GRAY_VALUES = tuple(8 + index * 10 for index in range(24))


def indexedToRgb(index: int) -> RgbColor:
    if index < 16:
        return BASIC_COLORS[index]
    if index < 232:
        cubeIndex = index - 16
        return RgbColor(
            r=CUBE_VALUES[cubeIndex // 36],
            g=CUBE_VALUES[(cubeIndex % 36) // 6],
            b=CUBE_VALUES[cubeIndex % 6],
        )
    gray = 8 + (index - 232) * 10
    return RgbColor(r=gray, g=gray, b=gray)


def colorToRgb(color: Color) -> RgbColor:
    match color.kind:
        case "indexed":
            return indexedToRgb(color.index)
        case "rgb":
            return RgbColor(r=color.r, g=color.g, b=color.b)


# Oklab (oklab.ts), Björn Ottosson's color space: https://bottosson.github.io/posts/oklab/
# A port of his reference implementation, Copyright (c) 2021 Björn Ottosson, used under the MIT license:
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
# associated documentation files (the "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the
# following conditions: The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY
# KIND, EXPRESS OR IMPLIED.

type _Vector = tuple[float, float, float]
type _Matrix = tuple[_Vector, _Vector, _Vector]


def _multiply(m: _Matrix, vector: _Vector) -> _Vector:
    x, y, z = vector
    return (
        m[0][0] * x + m[0][1] * y + m[0][2] * z,
        m[1][0] * x + m[1][1] * y + m[1][2] * z,
        m[2][0] * x + m[2][1] * y + m[2][2] * z,
    )


LINEAR_SRGB_TO_LMS: _Matrix = (
    (0.4122214694707629, 0.5363325372617349, 0.0514459932675022),
    (0.2119034958178251, 0.6806995506452344, 0.1073969535369405),
    (0.0883024591900564, 0.2817188391361215, 0.6299787016738222),
)
LMS_TO_LAB: _Matrix = (
    (0.210454268309314, 0.793617774702305, -0.0040720430116193),
    (1.9779985324311684, -2.42859224204858, 0.450593709617411),
    (0.0259040424655478, 0.7827717124575296, -0.8086757549230774),
)


def srgbToLinear(value: float) -> float:
    """Inverse sRGB transfer function: encoded to linear channel, both 0-1."""
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def linearSrgbToOklab(rgb: _Vector) -> _Vector:
    """Linear sRGB [r, g, b] (0-1) to Oklab [L, a, b]."""
    l, m, s = _multiply(LINEAR_SRGB_TO_LMS, rgb)
    return _multiply(LMS_TO_LAB, (math.cbrt(l), math.cbrt(m), math.cbrt(s)))


def rgbToOklab(color: RgbColor) -> _Vector:
    """sRGB channels (0-255) to Oklab [L, a, b]."""
    return linearSrgbToOklab((srgbToLinear(color.r / 255), srgbToLinear(color.g / 255), srgbToLinear(color.b / 255)))


def colorToOklch(color: Color) -> OklchChannels:
    l, a, b = rgbToOklab(colorToRgb(color))
    return OklchChannels(l=l, c=math.hypot(a, b), h=((math.atan2(b, a) * 180) / math.pi + 360) % 360)


def colorToHex(color: Color) -> str:
    rgb = colorToRgb(color)

    def channel(value: float) -> str:
        return format(jsRound(value), "x").rjust(2, "0")

    return f"#{channel(rgb.r)}{channel(rgb.g)}{channel(rgb.b)}"


def mixColors(first: Color, second: Color, amount: float, space: ColorMixSpace) -> Color:
    """MISAKA fork: ``space`` has no "oklch" default (D11); only "srgb" mixing is ported."""
    requireFinite(amount, "amount")
    if amount < 0 or amount > 1:
        raise ValueError(f"amount must be between 0 and 1: {amount}")

    a = colorToRgb(first)
    b = colorToRgb(second)
    return rgbColor(a.r + (b.r - a.r) * amount, a.g + (b.g - a.g) * amount, a.b + (b.b - a.b) * amount)


def findClosest(values: tuple[int, ...], target: float) -> int:
    closestIndex = 0
    closestDistance = math.inf
    for index in range(len(values)):
        distance = abs(target - values[index])
        if distance < closestDistance:
            closestIndex = index
            closestDistance = distance
    return closestIndex


def colorDistance(first: RgbColor, second: RgbColor) -> float:
    dr = first.r - second.r
    dg = first.g - second.g
    db = first.b - second.b
    return dr * dr * 0.299 + dg * dg * 0.587 + db * db * 0.114


def rgbToAnsi256(color: RgbColor) -> int:
    rIndex = findClosest(CUBE_VALUES, color.r)
    gIndex = findClosest(CUBE_VALUES, color.g)
    bIndex = findClosest(CUBE_VALUES, color.b)
    cubeColor = RgbColor(r=CUBE_VALUES[rIndex], g=CUBE_VALUES[gIndex], b=CUBE_VALUES[bIndex])
    cubeIndex = 16 + 36 * rIndex + 6 * gIndex + bIndex

    gray = jsRound(0.299 * color.r + 0.587 * color.g + 0.114 * color.b)
    grayOffset = findClosest(GRAY_VALUES, gray)
    grayValue = GRAY_VALUES[grayOffset]
    spread = max(color.r, color.g, color.b) - min(color.r, color.g, color.b)
    if spread < 10 and colorDistance(color, RgbColor(r=grayValue, g=grayValue, b=grayValue)) < colorDistance(
        color, cubeColor
    ):
        return 232 + grayOffset
    return cubeIndex


def colorAnsi(color: Color, mode: TerminalColorMode, background: bool) -> str:
    if color.kind == "indexed":
        return f"\x1b[{48 if background else 38};5;{color.index}m"

    rgb = colorToRgb(color)
    if mode == "truecolor":
        return f"\x1b[{48 if background else 38};2;{jsRound(rgb.r)};{jsRound(rgb.g)};{jsRound(rgb.b)}m"
    return f"\x1b[{48 if background else 38};5;{rgbToAnsi256(rgb)}m"


def foregroundAnsi(color: Color, mode: TerminalColorMode) -> str:
    return colorAnsi(color, mode, False)


def backgroundAnsi(color: Color, mode: TerminalColorMode) -> str:
    return colorAnsi(color, mode, True)


def styleText(text: str, options: TextStyle, mode: TerminalColorMode) -> str:
    fg = options.get("fg")
    bg = options.get("bg")
    return styleTextWithAnsi(
        text,
        foregroundAnsi(fg, mode) if fg else None,
        backgroundAnsi(bg, mode) if bg else None,
        options,
    )


def styleTextWithAnsi(
    text: str,
    fgAnsi: str | None,
    bgAnsi: str | None,
    options: TextAttributes,
) -> str:
    """Like ``styleText()``, but with precomputed color escape sequences, e.g. cached theme colors.
    Colors in ``options`` are ignored.
    """
    # Resets are prepended so they close in reverse order of the opening sequences.
    prefix = ""
    suffix = ""
    if fgAnsi:
        prefix += fgAnsi
        suffix = "\x1b[39m"
    if bgAnsi:
        prefix += bgAnsi
        suffix = f"\x1b[49m{suffix}"
    if options.get("bold"):
        prefix += "\x1b[1m"
    if options.get("dim"):
        prefix += "\x1b[2m"
    if options.get("bold") or options.get("dim"):
        suffix = f"\x1b[22m{suffix}"
    if options.get("italic"):
        prefix += "\x1b[3m"
        suffix = f"\x1b[23m{suffix}"
    if options.get("underline"):
        prefix += "\x1b[4m"
        suffix = f"\x1b[24m{suffix}"
    if options.get("inverse"):
        prefix += "\x1b[7m"
        suffix = f"\x1b[27m{suffix}"
    if options.get("strikethrough"):
        prefix += "\x1b[9m"
        suffix = f"\x1b[29m{suffix}"
    return f"{prefix}{text}{suffix}"

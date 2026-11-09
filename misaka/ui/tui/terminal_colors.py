"""Translated from pi-tui's ``packages/tui/src/terminal-colors.ts`` (v1.1.0)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, NotRequired, TypedDict

from misaka.utils import js_regexp
from misaka.utils.js import jsRound, jsTrim
from misaka.utils.js_regexp import RegExp


@dataclass(slots=True, frozen=True)
class RgbColor:
    r: float
    g: float
    b: float


type TerminalColorScheme = Literal["dark", "light"]


class TerminalColors(TypedDict):
    """Colors the terminal reports for its current theme."""

    # Default foreground (OSC 10).
    foreground: NotRequired[RgbColor]
    # Default background (OSC 11).
    background: NotRequired[RgbColor]
    # ANSI colors 0-15 (OSC 4). Only set when the terminal reported all 16.
    palette: NotRequired[list[RgbColor]]


def hexToRgb(hex: str) -> RgbColor:
    normalized = hex.removeprefix("#")
    r = int(normalized[0:2], 16)
    g = int(normalized[2:4], 16)
    b = int(normalized[4:6], 16)
    return RgbColor(r=r, g=g, b=b)


def parseOscHexChannel(channel: str) -> int | None:
    if not RegExp(r"^[0-9a-f]+$", "i").test(channel):
        return None
    max_ = 16 ** len(channel) - 1
    if max_ <= 0:
        return None
    return jsRound((int(channel, 16) / max_) * 255)


# What an OSC color reply reports: the default foreground (OSC 10), background (OSC 11), or a palette index (OSC 4).
type OscColorTarget = Literal["foreground", "background"] | int

OSC_COLOR_RESPONSE_PATTERN = RegExp(r"^\x1b\](?:(1[01])|4;(\d{1,3}));([^\x07\x1b]*)(?:\x07|\x1b\\)$", "i")
COLOR_SCHEME_REPORT_PATTERN = RegExp(r"^(?:\x1b\[\?997;(1|2)n)+$")


class OscColorResponse(TypedDict):
    """The ``{ target, rgb }`` object ``parseOscColorResponse`` returns."""

    target: OscColorTarget
    rgb: RgbColor | None


def parseOscColorResponse(data: str) -> OscColorResponse | None:
    """Parse an OSC 10, 11, or 4 color reply. Returns undefined when `data` is not such a reply;
    `rgb` is undefined when it is a reply with an unparseable color.
    """
    match = js_regexp.match(data, OSC_COLOR_RESPONSE_PATTERN)
    if not match:
        return None
    target: OscColorTarget = (
        "foreground" if match[1] == "10" else "background" if match[1] == "11" else int(match[2], 10)
    )
    return {"target": target, "rgb": parseOscColorValue(match[3])}


def parseOscColorValue(rawValue: str) -> RgbColor | None:
    value = jsTrim(rawValue)
    if value.startswith("#"):
        hex = value[1:]
        if RegExp(r"^[0-9a-f]{6}$", "i").test(hex):
            return hexToRgb(value)
        if RegExp(r"^[0-9a-f]{12}$", "i").test(hex):
            r = parseOscHexChannel(hex[0:4])
            g = parseOscHexChannel(hex[4:8])
            b = parseOscHexChannel(hex[8:12])
            return RgbColor(r=r, g=g, b=b) if r is not None and g is not None and b is not None else None
        return None

    rgbValue = js_regexp.replace(value, RegExp(r"^rgba?:", "i"), "")
    channels = rgbValue.split("/")
    red = channels[0] if len(channels) > 0 else None
    green = channels[1] if len(channels) > 1 else None
    blue = channels[2] if len(channels) > 2 else None
    if red is None or green is None or blue is None:
        return None
    r = parseOscHexChannel(red)
    g = parseOscHexChannel(green)
    b = parseOscHexChannel(blue)
    return RgbColor(r=r, g=g, b=b) if r is not None and g is not None and b is not None else None


def parseTerminalColorSchemeReport(data: str) -> TerminalColorScheme | None:
    match = js_regexp.match(data, COLOR_SCHEME_REPORT_PATTERN)
    if not match:
        return None
    return "light" if match[1] == "2" else "dark"


# MISAKA fork: MISAKA picks its light or dark palette from the terminal's background (OSC 11, mode 2031), which
# pi 1.x replaced with its system theme (not ported). These two are pi 0.87.1's coding-agent theme helpers
# (`theme.ts`), kept here beside the parsers that feed them.
def getRgbColorLuminance(rgb: RgbColor) -> float:
    def toLinear(channel: float) -> float:
        value = channel / 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    return 0.2126 * toLinear(rgb.r) + 0.7152 * toLinear(rgb.g) + 0.0722 * toLinear(rgb.b)


def getThemeForRgbColor(rgb: RgbColor) -> TerminalColorScheme:
    return "light" if getRgbColorLuminance(rgb) >= 0.5 else "dark"


__all__ = [
    "OscColorTarget",
    "RgbColor",
    "TerminalColorScheme",
    "TerminalColors",
    "getRgbColorLuminance",
    "getThemeForRgbColor",
    "parseOscColorResponse",
    "parseTerminalColorSchemeReport",
]

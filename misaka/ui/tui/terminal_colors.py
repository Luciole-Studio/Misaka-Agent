# Ported from: pi packages/tui/src/terminal-colors.ts @686f193e
# Parses terminal background color (OSC 11 response) and color scheme reports (CSI ?997;n n).
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

TerminalColorScheme = Literal["dark", "light"]


@dataclass(slots=True, frozen=True)
class RgbColor:
    r: int
    g: int
    b: int


OSC11_BACKGROUND_COLOR_RESPONSE_PATTERN = re.compile(r"^\x1b\]11;([^\x07\x1b]*)(?:\x07|\x1b\\)$", re.I)
COLOR_SCHEME_REPORT_PATTERN = re.compile(r"^(?:\x1b\[\?997;(1|2)n)+$")

_HEX_RE = re.compile(r"^[0-9a-f]+$", re.I)
_HEX6_RE = re.compile(r"^[0-9a-f]{6}$", re.I)
_HEX12_RE = re.compile(r"^[0-9a-f]{12}$", re.I)
_RGB_PREFIX_RE = re.compile(r"^rgba?:", re.I)


def _hex_to_rgb(hex_value: str) -> RgbColor:
    normalized = hex_value[1:] if hex_value.startswith("#") else hex_value
    return RgbColor(
        r=int(normalized[0:2], 16),
        g=int(normalized[2:4], 16),
        b=int(normalized[4:6], 16),
    )


def parse_osc_hex_channel(channel: str) -> int | None:
    if not _HEX_RE.match(channel):
        return None
    max_value = 16 ** len(channel) - 1
    if max_value <= 0:
        return None
    # JS Math.round is half-up; Python round() is banker's rounding, so do half-up explicitly
    return int((int(channel, 16) / max_value) * 255 + 0.5)


def is_osc11_background_color_response(data: str) -> bool:
    return OSC11_BACKGROUND_COLOR_RESPONSE_PATTERN.match(data) is not None


def parse_osc11_background_color(data: str) -> RgbColor | None:
    match = OSC11_BACKGROUND_COLOR_RESPONSE_PATTERN.match(data)
    if match is None:
        return None

    value = match.group(1).strip()
    if value.startswith("#"):
        hex_value = value[1:]
        if _HEX6_RE.match(hex_value):
            return _hex_to_rgb(value)
        if _HEX12_RE.match(hex_value):
            r = parse_osc_hex_channel(hex_value[0:4])
            g = parse_osc_hex_channel(hex_value[4:8])
            b = parse_osc_hex_channel(hex_value[8:12])
            if r is None or g is None or b is None:
                return None
            return RgbColor(r=r, g=g, b=b)
        return None

    rgb_value = _RGB_PREFIX_RE.sub("", value)
    channels = rgb_value.split("/")
    if len(channels) < 3:
        return None
    r = parse_osc_hex_channel(channels[0])
    g = parse_osc_hex_channel(channels[1])
    b = parse_osc_hex_channel(channels[2])
    if r is None or g is None or b is None:
        return None
    return RgbColor(r=r, g=g, b=b)


def parse_terminal_color_scheme_report(data: str) -> TerminalColorScheme | None:
    match = COLOR_SCHEME_REPORT_PATTERN.match(data)
    if match is None:
        return None
    return "light" if match.group(1) == "2" else "dark"


parseOsc11BackgroundColor = parse_osc11_background_color
parseTerminalColorSchemeReport = parse_terminal_color_scheme_report

__all__ = [
    "RgbColor",
    "TerminalColorScheme",
    "is_osc11_background_color_response",
    "parseOsc11BackgroundColor",
    "parse_osc11_background_color",
    "parseTerminalColorSchemeReport",
    "parse_terminal_color_scheme_report",
]

"""ANSI escape code to HTML conversion helpers."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

ANSI_COLORS = [
    "#000000",
    "#800000",
    "#008000",
    "#808000",
    "#000080",
    "#800080",
    "#008080",
    "#c0c0c0",
    "#808080",
    "#ff0000",
    "#00ff00",
    "#ffff00",
    "#0000ff",
    "#ff00ff",
    "#00ffff",
    "#ffffff",
]

ANSI_REGEX = re.compile(r"\x1b\[([0-9;]*)m")


@dataclass(slots=True)
class TextStyle:
    fg: str | None = None
    bg: str | None = None
    bold: bool = False
    dim: bool = False
    italic: bool = False
    underline: bool = False


def color256_to_hex(index: int) -> str:
    if index < 16:
        return ANSI_COLORS[index]
    if index < 232:
        cube_index = index - 16
        r = cube_index // 36
        g = (cube_index % 36) // 6
        b = cube_index % 6

        def to_component(value: int) -> int:
            return 0 if value == 0 else 55 + value * 40

        return f"#{to_component(r):02x}{to_component(g):02x}{to_component(b):02x}"

    gray = 8 + (index - 232) * 10
    return f"#{gray:02x}{gray:02x}{gray:02x}"


def escape_html(text: str) -> str:
    return html.escape(text, quote=True).replace("&#x27;", "&#039;")


def style_to_inline_css(style: TextStyle) -> str:
    parts: list[str] = []
    if style.fg:
        parts.append(f"color:{style.fg}")
    if style.bg:
        parts.append(f"background-color:{style.bg}")
    if style.bold:
        parts.append("font-weight:bold")
    if style.dim:
        parts.append("opacity:0.6")
    if style.italic:
        parts.append("font-style:italic")
    if style.underline:
        parts.append("text-decoration:underline")
    return ";".join(parts)


def has_style(style: TextStyle) -> bool:
    return any((style.fg, style.bg, style.bold, style.dim, style.italic, style.underline))


def apply_sgr_code(params: list[int], style: TextStyle) -> None:
    index = 0
    while index < len(params):
        code = params[index]
        if code == 0:
            style.fg = None
            style.bg = None
            style.bold = False
            style.dim = False
            style.italic = False
            style.underline = False
        elif code == 1:
            style.bold = True
        elif code == 2:
            style.dim = True
        elif code == 3:
            style.italic = True
        elif code == 4:
            style.underline = True
        elif code == 22:
            style.bold = False
            style.dim = False
        elif code == 23:
            style.italic = False
        elif code == 24:
            style.underline = False
        elif 30 <= code <= 37:
            style.fg = ANSI_COLORS[code - 30]
        elif code == 38:
            if index + 2 < len(params) and params[index + 1] == 5:
                style.fg = color256_to_hex(params[index + 2])
                index += 2
            elif index + 4 < len(params) and params[index + 1] == 2:
                style.fg = f"rgb({params[index + 2]},{params[index + 3]},{params[index + 4]})"
                index += 4
        elif code == 39:
            style.fg = None
        elif 40 <= code <= 47:
            style.bg = ANSI_COLORS[code - 40]
        elif code == 48:
            if index + 2 < len(params) and params[index + 1] == 5:
                style.bg = color256_to_hex(params[index + 2])
                index += 2
            elif index + 4 < len(params) and params[index + 1] == 2:
                style.bg = f"rgb({params[index + 2]},{params[index + 3]},{params[index + 4]})"
                index += 4
        elif code == 49:
            style.bg = None
        elif 90 <= code <= 97:
            style.fg = ANSI_COLORS[code - 90 + 8]
        elif 100 <= code <= 107:
            style.bg = ANSI_COLORS[code - 100 + 8]
        index += 1


def ansi_to_html(text: str) -> str:
    style = TextStyle()
    result = ""
    last_index = 0
    in_span = False

    for match in ANSI_REGEX.finditer(text):
        before_text = text[last_index : match.start()]
        if before_text:
            result += escape_html(before_text)

        if in_span:
            result += "</span>"
            in_span = False

        param_str = match.group(1)
        params = [int(part) if part.isascii() and part.isdigit() else 0 for part in param_str.split(";")] if param_str else [0]
        apply_sgr_code(params, style)

        if has_style(style):
            result += f'<span style="{style_to_inline_css(style)}">'
            in_span = True

        last_index = match.end()

    remaining = text[last_index:]
    if remaining:
        result += escape_html(remaining)

    if in_span:
        result += "</span>"

    return result


def ansi_lines_to_html(lines: list[str]) -> str:
    return "".join(f'<div class="ansi-line">{ansi_to_html(line) or "&nbsp;"}</div>' for line in lines)


ansiLinesToHtml = ansi_lines_to_html
ansiToHtml = ansi_to_html
color256ToHex = color256_to_hex
escapeHtml = escape_html

__all__ = [
    "ansiLinesToHtml",
    "ansiToHtml",
]

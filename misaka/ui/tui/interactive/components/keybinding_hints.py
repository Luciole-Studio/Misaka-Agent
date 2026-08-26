"""Utilities for formatting interactive keybinding hints."""

from __future__ import annotations

import sys
from dataclasses import dataclass

from misaka.ui.tui import getKeybindings
from misaka.ui.tui.interactive.theme.theme import theme
from misaka.ui.tui.keybindings import Keybinding


@dataclass(slots=True)
class KeyTextFormatOptions:
    capitalize: bool = False


def _format_key_part(part: str, options: KeyTextFormatOptions) -> str:
    display_part = "option" if sys.platform == "darwin" and part.lower() == "alt" else part
    if not options.capitalize:
        return display_part
    return display_part[:1].upper() + display_part[1:]


def format_key_text(key: str, options: KeyTextFormatOptions | None = None) -> str:
    resolved = options or KeyTextFormatOptions()
    return "/".join(
        "+".join(_format_key_part(part, resolved) for part in option.split("+"))
        for option in key.split("/")
    )


def _format_keys(keys: list[str], options: KeyTextFormatOptions | None = None) -> str:
    if not keys:
        return ""
    return format_key_text("/".join(keys), options)


def key_text(keybinding: Keybinding) -> str:
    return _format_keys(getKeybindings().getKeys(keybinding))


def key_display_text(keybinding: Keybinding) -> str:
    return _format_keys(getKeybindings().getKeys(keybinding), KeyTextFormatOptions(capitalize=True))


def key_hint(keybinding: Keybinding, description: str) -> str:
    return theme.fg("dim", key_text(keybinding)) + theme.fg("text", f" {description}")


def raw_key_hint(key: str, description: str) -> str:
    return theme.fg("dim", format_key_text(key)) + theme.fg("text", f" {description}")


keyHint = key_hint
keyText = key_text
rawKeyHint = raw_key_hint

__all__ = [
    "KeyTextFormatOptions",
    "keyHint",
    "keyText",
    "rawKeyHint",
]

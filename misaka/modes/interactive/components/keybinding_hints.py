"""Translated from pi's ``modes/interactive/components/keybinding-hints.ts`` (v1.1.0).

Utilities for formatting keybinding hints in the UI.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from misaka.modes.interactive.theme.theme import theme
from misaka.ui.tui.keybindings import Keybinding, getKeybindings
from misaka.ui.tui.keys import KeyId


@dataclass(slots=True)
class KeyTextFormatOptions:
    capitalize: bool | None = None


def formatKeyPart(part: str, options: KeyTextFormatOptions) -> str:
    displayPart = "option" if sys.platform == "darwin" and part.lower() == "alt" else part
    return displayPart[0:1].upper() + displayPart[1:] if options.capitalize else displayPart


def formatKeyText(key: str, options: KeyTextFormatOptions | None = None) -> str:
    if options is None:
        options = KeyTextFormatOptions()
    return "/".join("+".join(formatKeyPart(part, options) for part in k.split("+")) for k in key.split("/"))


def formatKeys(keys: list[KeyId], options: KeyTextFormatOptions | None = None) -> str:
    if len(keys) == 0:
        return ""
    return formatKeyText("/".join(keys), options)


def keyText(keybinding: Keybinding) -> str:
    return formatKeys(getKeybindings().getKeys(keybinding))


def keyDisplayText(keybinding: Keybinding) -> str:
    return formatKeys(getKeybindings().getKeys(keybinding), KeyTextFormatOptions(capitalize=True))


def keyHint(keybinding: Keybinding, description: str) -> str:
    return theme.fg("dim", keyText(keybinding)) + theme.fg("muted", f" {description}")


def rawKeyHint(key: str, description: str) -> str:
    return theme.fg("dim", formatKeyText(key)) + theme.fg("muted", f" {description}")

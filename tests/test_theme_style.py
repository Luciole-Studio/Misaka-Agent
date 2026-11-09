"""Translated from pi's ``coding-agent/test/theme-style.test.ts`` (v1.1.0).

MISAKA fork (D11): OKLCH and OKHSL theme values are not ported ("loads OKLCH theme values", "loads OKHSL theme
values"), and the terminal never reports its colors, so the ``setTerminalColors()`` halves of the appearance and
empty-token cases are not ported either. The base themes are MISAKA's own dark.json and light.json.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from misaka.config import get_themes_dir
from misaka.modes.interactive.theme.theme import Theme, loadThemeFromPath
from misaka.ui.tui.colors import colorToHex, styleText


def loadTheme(tmp_path: Path, base: str, edit: Callable[[dict[str, Any]], None] = lambda _theme: None) -> Theme:
    """Load a copy of a built-in theme, modified by ``edit``."""
    themeJson = json.loads((Path(get_themes_dir()) / f"{base}.json").read_text(encoding="utf-8"))
    edit(themeJson)
    directory = tmp_path / f"pi-theme-style-{len(list(tmp_path.iterdir()))}"
    directory.mkdir()
    path = directory / f"{themeJson['name']}.json"
    path.write_text(json.dumps(themeJson), encoding="utf-8")
    return loadThemeFromPath(str(path), "truecolor")


def test_renders_theme_tokens_the_same_as_the_generic_text_styler(tmp_path):
    theme = loadTheme(tmp_path, "dark")
    assert theme.style("Ready", {"fg": "success", "bg": "toolSuccessBg", "bold": True}) == styleText(
        "Ready", {"fg": theme.colors["success"], "bg": theme.colors["toolSuccessBg"], "bold": True}, "truecolor"
    )


def test_rejects_unknown_tokens_and_tokens_in_the_wrong_slot(tmp_path):
    theme = loadTheme(tmp_path, "dark")
    with pytest.raises(KeyError, match="Unknown theme color: notAToken"):
        theme.style("x", {"fg": "notAToken"})  # type: ignore[typeddict-item]
    # background tokens are not foreground colors; use theme.colors["userMessageBg"]
    with pytest.raises(KeyError, match="Unknown theme color: userMessageBg"):
        theme.style("x", {"fg": "userMessageBg"})  # type: ignore[typeddict-item]


def test_detects_the_appearance_unless_it_is_declared(tmp_path):
    assert loadTheme(tmp_path, "dark").appearance == "dark"
    assert loadTheme(tmp_path, "light").appearance == "light"
    # Without a declaration, the appearance is detected from the theme's own colors.
    for base in ("dark", "light"):
        assert loadTheme(tmp_path, base, lambda json: json.pop("appearance")).appearance == base

    def declareLight(json: dict[str, Any]) -> None:
        json["appearance"] = "light"

    assert loadTheme(tmp_path, "dark", declareLight).appearance == "light"

    # Palette colors 0-15 follow the terminal palette, so such themes follow the terminal background.
    def paletteOnly(json: dict[str, Any]) -> None:
        json.pop("appearance")
        for key in json["colors"]:
            json["colors"][key] = 0 if key.endswith("Bg") else 7

    assert loadTheme(tmp_path, "dark", paletteOnly).appearance == "dark"


def test_renders_empty_tokens_as_terminal_defaults_and_reports_concrete_colors_for_them(tmp_path):
    def emptyTokens(json: dict[str, Any]) -> None:
        json["colors"]["text"] = ""
        json["colors"]["userMessageBg"] = ""

    theme = loadTheme(tmp_path, "dark", emptyTokens)
    assert theme.fg("text", "x") == "\x1b[39mx\x1b[39m"
    assert theme.bg("userMessageBg", "x") == "\x1b[49mx\x1b[49m"
    assert colorToHex(theme.colors["text"]) == "#e5e5e7"
    assert colorToHex(theme.colors["userMessageBg"]) == "#000000"

"""Translated from pi's ``coding-agent/test/scrollbar-theme.test.ts`` (v1.3.0).

The scrollbar and search tokens are only drawn by pi's fullscreen renderer (D1), but themes carry them as data.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from misaka.config import get_themes_dir
from misaka.modes.interactive.theme import theme as theme_module
from misaka.modes.interactive.theme.theme import (
    loadThemeFromPath,
    setThemeJsonValidator,
)
from misaka.modes.interactive.theme.theme_schema import validateThemeJson


@pytest.fixture(autouse=True)
def validated_themes(monkeypatch):
    monkeypatch.setattr(theme_module, "themeJsonValidator", None)
    setThemeJsonValidator(validateThemeJson)


def loadDarkTheme() -> dict[str, Any]:
    return json.loads((Path(get_themes_dir()) / "dark.json").read_text(encoding="utf-8"))


def writeTheme(tmp_path: Path, theme: dict[str, Any]) -> str:
    themePath = tmp_path / f"{theme['name']}.json"
    themePath.write_text(json.dumps(theme), encoding="utf-8")
    return str(themePath)


@pytest.mark.parametrize(("token", "fallback"), [("scrollbarTrack", "muted"), ("scrollbarThumb", "text")])
def test_falls_back_when_omitted(tmp_path, token, fallback):
    themeJson = loadDarkTheme()
    themeJson["name"] = f"missing-{token}-theme"
    themeJson["colors"].pop(token, None)

    loadedTheme = loadThemeFromPath(writeTheme(tmp_path, themeJson), "truecolor")
    assert loadedTheme.getFgAnsi(token) == loadedTheme.getFgAnsi(fallback)


def test_uses_explicitly_configured_scrollbar_colors(tmp_path):
    themeJson = loadDarkTheme()
    themeJson["name"] = "custom-scrollbar-theme"
    themeJson["colors"]["scrollbarTrack"] = "#654321"
    themeJson["colors"]["scrollbarThumb"] = "#123456"

    loadedTheme = loadThemeFromPath(writeTheme(tmp_path, themeJson), "truecolor")
    assert loadedTheme.getFgAnsi("scrollbarTrack") == "\x1b[38;2;101;67;33m"
    assert loadedTheme.getFgAnsi("scrollbarThumb") == "\x1b[38;2;18;52;86m"


def test_falls_back_to_existing_selection_and_text_colors_for_search_highlights(tmp_path):
    themeJson = loadDarkTheme()
    themeJson["name"] = "legacy-search-theme"
    themeJson["colors"].pop("searchMatchBg", None)
    themeJson["colors"].pop("searchMatchText", None)

    loadedTheme = loadThemeFromPath(writeTheme(tmp_path, themeJson), "truecolor")
    assert loadedTheme.getBgAnsi("searchMatchBg") == loadedTheme.getBgAnsi("selectedBg")
    assert loadedTheme.getFgAnsi("searchMatchText") == loadedTheme.getFgAnsi("text")


def test_uses_explicitly_configured_search_highlight_colors(tmp_path):
    themeJson = loadDarkTheme()
    themeJson["name"] = "custom-search-theme"
    themeJson["colors"]["searchMatchBg"] = "#112233"
    themeJson["colors"]["searchMatchText"] = "#223344"

    loadedTheme = loadThemeFromPath(writeTheme(tmp_path, themeJson), "truecolor")
    assert loadedTheme.getBgAnsi("searchMatchBg") == "\x1b[48;2;17;34;51m"
    assert loadedTheme.getFgAnsi("searchMatchText") == "\x1b[38;2;34;51;68m"

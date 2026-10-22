"""MISAKA: ``modes/interactive/theme/theme_tokens.py`` (pi ``theme-tokens.ts``, v1.3.0) and the token fallbacks of
``theme.py``. Upstream has no test file of its own for these; TypeScript derives the token types from the table, so
here the spelled-out types are checked against it, and MISAKA's own tokens are pinned."""

from __future__ import annotations

import json
from pathlib import Path
from typing import get_args

import pytest

from misaka.config import get_themes_dir
from misaka.modes.interactive.theme import theme as theme_module
from misaka.modes.interactive.theme.theme import (
    ThemeBg,
    ThemeColor,
    ThemeToken,
    loadThemeFromPath,
    setThemeJsonValidator,
    splitThemeColors,
    withThemeTokenFallbacks,
)
from misaka.modes.interactive.theme.theme_schema import validateThemeJson
from misaka.modes.interactive.theme.theme_tokens import THEME_TOKENS, defineThemeTokens

MISAKA_TOKENS = {"sectionTitle": "accent", "appTitle": "text", "appTitleOnLight": "text"}


@pytest.fixture(autouse=True)
def validated_themes(monkeypatch):
    monkeypatch.setattr(theme_module, "themeJsonValidator", None)
    setThemeJsonValidator(validateThemeJson)


def loadDarkTheme() -> dict:
    return json.loads((Path(get_themes_dir()) / "dark.json").read_text(encoding="utf-8"))


def writeTheme(tmp_path: Path, theme: dict) -> str:
    themePath = tmp_path / f"{theme['name']}.json"
    themePath.write_text(json.dumps(theme), encoding="utf-8")
    return str(themePath)


def test_the_token_types_name_the_table():
    foreground = [name for name, token in THEME_TOKENS.items() if token["slot"] == "foreground"]
    background = [name for name, token in THEME_TOKENS.items() if token["slot"] == "background"]
    assert sorted(get_args(ThemeColor.__value__)) == sorted(foreground)
    assert sorted(get_args(ThemeBg.__value__)) == sorted(background)
    assert [alias.__value__ for alias in get_args(ThemeToken.__value__)] == [ThemeColor.__value__, ThemeBg.__value__]


def test_rejects_fallbacks_to_unknown_tokens_or_the_other_slot():
    with pytest.raises(ValueError, match="Theme token a has unknown fallback missing"):
        defineThemeTokens({"a": {"slot": "foreground", "description": "", "fallback": "missing"}})
    with pytest.raises(ValueError, match="Theme token a has fallback b in a different color slot"):
        defineThemeTokens({
            "a": {"slot": "foreground", "description": "", "fallback": "b"},
            "b": {"slot": "background", "description": ""},
        })


def test_misakas_tokens_are_optional_foregrounds_with_fallbacks():
    for name, fallback in MISAKA_TOKENS.items():
        assert THEME_TOKENS[name]["slot"] == "foreground"
        assert THEME_TOKENS[name]["fallback"] == fallback


def test_fills_only_missing_tokens_from_their_resolved_fallback():
    values = {name: f"#{index:06x}" for index, (name, token) in enumerate(THEME_TOKENS.items()) if "fallback" not in token}
    resolved = withThemeTokenFallbacks({**values, "appTitle": "#123456"})
    assert resolved["appTitle"] == "#123456"
    assert resolved["appTitleOnLight"] == values["text"]
    assert resolved["sectionTitle"] == values["accent"]
    assert resolved["searchMatchBg"] == values["selectedBg"]
    assert resolved["thinkingMax"] == values["thinkingXhigh"]
    assert set(resolved) == set(THEME_TOKENS)

    del values["thinkingXhigh"]
    with pytest.raises(ValueError, match="Theme token thinkingMax has unresolved fallback thinkingXhigh"):
        withThemeTokenFallbacks(values)


def test_splits_by_the_tokens_slot():
    split = splitThemeColors({"accent": "#111111", "selectedBg": "#222222", "sectionTitle": "#333333"})
    assert split == {"fgColors": {"accent": "#111111", "sectionTitle": "#333333"}, "bgColors": {"selectedBg": "#222222"}}


@pytest.mark.parametrize(("token", "fallback"), MISAKA_TOKENS.items())
def test_a_theme_without_misakas_tokens_loads_and_uses_their_fallbacks(tmp_path, token, fallback):
    # Before 1.3 a theme without `appTitle` raised KeyError in the startup header of every "MISAKA · …" window.
    themeJson = loadDarkTheme()
    themeJson["name"] = f"missing-{token}-theme"
    for name in MISAKA_TOKENS:
        themeJson["colors"].pop(name)

    loadedTheme = loadThemeFromPath(writeTheme(tmp_path, themeJson), "truecolor")
    assert loadedTheme.getFgAnsi(token) == loadedTheme.getFgAnsi(fallback)
    assert loadedTheme.fg(token, "x") == loadedTheme.fg(fallback, "x")


def test_a_misaka_token_set_by_a_variable_resolves_before_its_fallback(tmp_path):
    themeJson = loadDarkTheme()
    themeJson["name"] = "var-title-theme"
    themeJson["vars"]["title"] = "#abcdef"
    themeJson["colors"]["appTitle"] = "title"
    del themeJson["colors"]["appTitleOnLight"]
    themeJson["colors"]["text"] = "title"

    loadedTheme = loadThemeFromPath(writeTheme(tmp_path, themeJson), "truecolor")
    assert loadedTheme.getFgAnsi("appTitle") == "\x1b[38;2;171;205;239m"
    assert loadedTheme.getFgAnsi("appTitleOnLight") == "\x1b[38;2;171;205;239m"


def test_the_uninitialized_theme_has_every_token():
    # MISAKA fork: `theme` exists from import on; its fallback palette is built from the table.
    split = splitThemeColors(dict.fromkeys(THEME_TOKENS, ""))
    uninitialized = theme_module.Theme(split["fgColors"], split["bgColors"], "256color", {"name": "uninitialized"})
    for name, token in THEME_TOKENS.items():
        if token["slot"] == "foreground":
            assert uninitialized.getFgAnsi(name) == "\x1b[39m"
        else:
            assert uninitialized.getBgAnsi(name) == "\x1b[49m"

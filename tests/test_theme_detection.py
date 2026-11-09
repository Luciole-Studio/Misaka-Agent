"""Translated from pi's ``coding-agent/test/theme-detection.test.ts`` (v1.1.0).

MISAKA fork (D11): the terminal never reports its colors, so the background assertion of the priority case and
"follows the foreground when text is readable that way" (the system theme's ``terminalAppearance``) are not ported.
``MISAKA_THEME`` pins the answer first.
"""

from __future__ import annotations

import re

import pytest

from misaka.modes.interactive.theme.theme import (
    detectColorFgBgTheme,
    detectTerminalTheme,
    getThemeByName,
    parseAutoThemeSetting,
    resolveThemeSetting,
)
from misaka.ui.tui.terminal_image import (
    TerminalCapabilities,
    resetCapabilitiesCache,
    setCapabilities,
)


@pytest.fixture(autouse=True)
def reset_capabilities():
    yield
    resetCapabilitiesCache()


def test_classifies_the_last_field_by_palette_index_like_vim():
    assert detectColorFgBgTheme({"COLORFGBG": "15;0"}) == "dark"
    assert detectColorFgBgTheme({"COLORFGBG": "0;7;15"}) == "light"
    # Solarized Dark's background is bright black.
    assert detectColorFgBgTheme({"COLORFGBG": "12;8"}) == "dark"
    # rxvt writes "default" when the background is not a palette color.
    assert detectColorFgBgTheme({"COLORFGBG": "15;default"}) is None
    assert detectColorFgBgTheme({}) is None


def test_prefers_the_reported_scheme_then_colorfgbg_then_dark():
    env = {"COLORFGBG": "0;15"}
    assert detectTerminalTheme({}, "dark", env) == "dark"
    assert detectTerminalTheme({}, None, env) == "light"
    assert detectTerminalTheme({}, None, {}) == "dark"


def test_misaka_theme_pins_the_terminal_theme():
    # MISAKA: the panel sets MISAKA_THEME for its panes.
    assert detectTerminalTheme({}, "dark", {"MISAKA_THEME": "light"}) == "light"
    assert detectTerminalTheme({}, None, {"MISAKA_THEME": "dark", "COLORFGBG": "0;15"}) == "dark"
    assert detectTerminalTheme({}, None, {"MISAKA_THEME": "other", "COLORFGBG": "0;15"}) == "light"


def test_uses_terminal_capabilities():
    setCapabilities(TerminalCapabilities(images=None, trueColor=False, hyperlinks=False))
    ansi256Theme = getThemeByName("dark")
    assert ansi256Theme is not None, "dark theme not found"
    assert ansi256Theme.getColorMode() == "256color"
    assert re.match(r"^\x1b\[38;5;\d+m$", ansi256Theme.getFgAnsi("accent"))

    setCapabilities(TerminalCapabilities(images=None, trueColor=True, hyperlinks=False))
    truecolorTheme = getThemeByName("dark")
    assert truecolorTheme is not None, "dark theme not found"
    assert truecolorTheme.getColorMode() == "truecolor"
    assert re.match(r"^\x1b\[38;2;\d+;\d+;\d+m$", truecolorTheme.getFgAnsi("accent"))


def test_parses_and_resolves_automatic_theme_settings():
    assert parseAutoThemeSetting("light/dark") == {"lightTheme": "light", "darkTheme": "dark"}
    assert resolveThemeSetting("dark", "light") == "dark"
    assert resolveThemeSetting("light/dark", "light") == "light"
    assert resolveThemeSetting("light/dark", "dark") == "dark"
    assert resolveThemeSetting("light/dark/extra", "dark") is None

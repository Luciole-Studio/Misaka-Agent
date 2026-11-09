"""Translated from pi's ``coding-agent/test/theme-controller.test.ts`` (v1.1.0).

MISAKA fork (D11): the controller does not query the terminal's colors or follow its light/dark reports, so
"applies the theme immediately and lets startup wait for the colors", "falls back to palette indices, then applies
colors that arrive after the timeout", "re-queries the colors on appearance changes and lets them decide", "uses the
reported scheme for the system theme when the terminal reports no colors", "re-renders only when the reported colors
change" and "disables terminal appearance updates when disposed" are not ported, nor the color-query assertion of the
first case.
"""

from __future__ import annotations

from collections.abc import Callable
from unittest import mock

import pytest

from misaka.core.settings_manager import SettingsManager
from misaka.modes.interactive.theme import theme as theme_module
from misaka.modes.interactive.theme.theme import initTheme
from misaka.modes.interactive.theme.theme_controller import InteractiveThemeController


def createUi() -> mock.Mock:
    return mock.Mock(spec_set=["invalidate", "requestRender"])


def createController(
    ui: mock.Mock, getSettingsManager: Callable[[], SettingsManager], initialThemeSetting: str | None = None
) -> InteractiveThemeController:
    return InteractiveThemeController(ui, {"getSettingsManager": getSettingsManager, "showError": mock.Mock(), "onChanged": mock.Mock(), "initialThemeSetting": initialThemeSetting})


@pytest.fixture(autouse=True)
def reset_theme():
    yield
    initTheme("dark")


async def test_uses_the_initial_theme_without_persisting_it():
    ui = createUi()
    manager = SettingsManager.inMemory({"theme": "dark"})
    with mock.patch.object(manager, "setTheme") as setTheme, mock.patch.object(manager, "flush") as flushSettings:
        controller = createController(ui, lambda: manager, "light")

        assert theme_module.theme.name == "light"
        assert controller.getThemeSelection() == "light"
        controller.applyFromSettings()

        setTheme.assert_not_called()
        flushSettings.assert_not_called()


def test_lets_an_explicit_selection_replace_the_initial_theme():
    ui = createUi()
    firstManager = SettingsManager.inMemory({"theme": "dark"})
    secondManager = SettingsManager.inMemory({"theme": "light"})
    manager = firstManager
    controller = createController(ui, lambda: manager, "light")
    controller.applyFromSettings()

    assert controller.setThemeName("dark") == {"success": True}
    manager = secondManager
    controller.applyFromSettings()

    assert controller.getThemeSelection() == "dark"
    assert theme_module.theme.name == "dark"


def test_reloads_theme_settings_when_no_initial_theme_was_supplied():
    ui = createUi()
    firstManager = SettingsManager.inMemory({"theme": "dark"})
    secondManager = SettingsManager.inMemory({"theme": "light"})
    manager = firstManager
    controller = createController(ui, lambda: manager)
    controller.applyFromSettings()

    firstManager.applyOverrides({"theme": "light"})
    controller.applyFromSettings()
    assert theme_module.theme.name == "light"

    secondManager.applyOverrides({"theme": "dark"})
    manager = secondManager
    controller.applyFromSettings()
    assert theme_module.theme.name == "dark"

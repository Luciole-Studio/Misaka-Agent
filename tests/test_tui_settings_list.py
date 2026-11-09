"""Translated from pi-tui's ``test/settings-list.test.ts`` (v1.1.0)."""

from __future__ import annotations

import dataclasses
import re

from misaka.ui.tui.components.settings_list import (
    SettingItem,
    SettingsList,
    SettingsListOptions,
    SettingsListTheme,
)

testTheme = SettingsListTheme(
    label=lambda text, _selected: text,
    value=lambda text, _selected: text,
    description=lambda text: text,
    cursor="> ",
    hint=lambda text: text,
)

items = [
    SettingItem(
        id="tui-mode",
        label="TUI mode",
        currentValue="regular",
        values=["regular", "fullscreen"],
    ),
]


def test_includes_spaces_in_an_active_search_instead_of_changing_the_selected_setting():
    changes: list[dict[str, str]] = []
    settingsList = SettingsList(
        [dataclasses.replace(item) for item in items],
        10,
        testTheme,
        lambda id, value: changes.append({"id": id, "value": value}),
        lambda: None,
        SettingsListOptions(enableSearch=True),
    )

    for character in "TUI mode":
        settingsList.handleInput(character)

    assert changes == []
    assert re.search(r"TUI mode", (settingsList.render(80) or [""])[0])

    settingsList.handleInput("\r")
    assert changes == [{"id": "tui-mode", "value": "fullscreen"}]


def test_keeps_space_as_a_change_shortcut_before_a_search_query_is_entered():
    changes: list[dict[str, str]] = []
    settingsList = SettingsList(
        [dataclasses.replace(item) for item in items],
        10,
        testTheme,
        lambda id, value: changes.append({"id": id, "value": value}),
        lambda: None,
        SettingsListOptions(enableSearch=True),
    )

    settingsList.handleInput(" ")

    assert changes == [{"id": "tui-mode", "value": "fullscreen"}]

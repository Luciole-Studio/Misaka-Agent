"""Translated from pi's ``coding-agent/test/max-thinking.test.ts`` (v1.1.0)."""

from __future__ import annotations

import json
from pathlib import Path

from misaka.cli.args import isValidThinkingLevel
from misaka.config import get_themes_dir
from misaka.core.settings_manager import SettingsManager
from misaka.modes.interactive.theme.theme import loadThemeFromPath


async def test_is_accepted_by_cli_and_settings():
    assert isValidThinkingLevel("max") is True

    settings = SettingsManager.inMemory()
    settings.setDefaultThinkingLevel("max")
    await settings.flush()
    assert settings.getDefaultThinkingLevel() == "max"


def test_falls_back_to_thinking_xhigh_for_legacy_themes(tmp_path):
    darkTheme = json.loads((Path(get_themes_dir()) / "dark.json").read_text(encoding="utf-8"))
    darkTheme["name"] = "legacy-theme"
    darkTheme["colors"].pop("thinkingMax", None)
    themePath = tmp_path / "legacy-theme.json"
    themePath.write_text(json.dumps(darkTheme), encoding="utf-8")

    legacyTheme = loadThemeFromPath(str(themePath))
    assert legacyTheme.getThinkingBorderColor("max")("border") == legacyTheme.getThinkingBorderColor("xhigh")("border")

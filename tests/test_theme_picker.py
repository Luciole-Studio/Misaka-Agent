"""Translated from pi's ``coding-agent/test/theme-picker.test.ts`` (v1.1.0).

MISAKA fork: custom themes live in the test's ``MISAKA_HOME`` (pi: ``PI_CODING_AGENT_DIR``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from misaka.config import get_custom_themes_dir, get_themes_dir
from misaka.modes.interactive.theme.theme import (
    getAvailableThemes,
    getAvailableThemesWithPaths,
    setRegisteredThemes,
)


@pytest.fixture(autouse=True)
def no_registered_themes():
    Path(get_custom_themes_dir()).mkdir(parents=True, exist_ok=True)
    setRegisteredThemes([])
    yield
    setRegisteredThemes([])


def test_uses_custom_theme_content_names_instead_of_file_names():
    darkTheme = json.loads((Path(get_themes_dir()) / "dark.json").read_text(encoding="utf-8"))
    customTheme = {**darkTheme, "name": "bar"}

    themePath = str(Path(get_custom_themes_dir()) / "foo.json")
    Path(themePath).write_text(json.dumps(customTheme, indent=2), encoding="utf-8")

    assert "bar" in getAvailableThemes()
    assert "foo" not in getAvailableThemes()
    assert {"name": "bar", "path": themePath} in getAvailableThemesWithPaths()
    assert not any(theme["name"] == "foo" for theme in getAvailableThemesWithPaths())

"""Translated from pi's ``coding-agent/test/theme-export.test.ts`` (v1.1.0).

MISAKA forks: custom themes live in the test's ``MISAKA_HOME`` (pi: ``PI_CODING_AGENT_DIR``); OKHSL export colors are
not ported (D11, "converts OKHSL export colors to hex because CSS does not support them").
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from misaka.config import get_custom_themes_dir, get_themes_dir
from misaka.modes.interactive.theme.theme import getThemeExportColors


def _darkTheme() -> dict[str, Any]:
    return json.loads((Path(get_themes_dir()) / "dark.json").read_text(encoding="utf-8"))


def _write(name: str, theme: dict[str, Any]) -> None:
    themes = Path(get_custom_themes_dir())
    themes.mkdir(parents=True, exist_ok=True)
    (themes / f"{name}.json").write_text(json.dumps(theme, indent=2), encoding="utf-8")


def test_resolves_export_variable_references_using_the_same_syntax_as_colors():
    darkTheme = _darkTheme()
    customTheme = {
        **darkTheme,
        "name": "custom-export-vars",
        "vars": {
            **(darkTheme.get("vars") or {}),
            "pageBgVar": "#112233",
            "pageBgAlias": "pageBgVar",
            "infoBgVar": "#445566",
            "cardBgVar": "#223344",
        },
        "export": {
            "pageBg": "pageBgAlias",
            "cardBg": "cardBgVar",
            "infoBg": "infoBgVar",
        },
    }
    _write("custom-export-vars", customTheme)

    assert getThemeExportColors("custom-export-vars") == {
        "pageBg": "#112233",
        "cardBg": "#223344",
        "infoBg": "#445566",
    }


def test_resolves_recursive_vars_and_converts_256_color_export_values_to_hex():
    darkTheme = _darkTheme()
    customTheme = {
        **darkTheme,
        "name": "custom-export-recursive",
        "vars": {
            **(darkTheme.get("vars") or {}),
            "deepPageBg": "#abcdef",
            "pageBgAlias": "deepPageBg",
            "cardBgAnsi": 24,
        },
        "export": {
            "pageBg": "pageBgAlias",
            "cardBg": "cardBgAnsi",
            "infoBg": "",
        },
    }
    _write("custom-export-recursive", customTheme)

    assert getThemeExportColors("custom-export-recursive") == {
        "pageBg": "#abcdef",
        "cardBg": "#005f87",
        "infoBg": None,
    }

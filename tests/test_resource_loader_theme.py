"""Translated from pi's ``packages/coding-agent/test/resource-loader-theme.test.ts`` (v1.3.0).

MISAKA forks: the environment override is ``MISAKA_TRUE_COLOR`` (pi: ``PI_TRUE_COLOR``), and the loader has no
``noSkills``/``noContextFiles`` options (skills and context files are not resource-loader resources here).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import misaka.modes.interactive.theme as theme_package
from misaka.core.resource_loader import DefaultResourceLoader
from misaka.core.settings_manager import SettingsManager
from misaka.modes.interactive.theme import theme as theme_module
from misaka.modes.interactive.theme.theme import setThemeJsonValidator
from misaka.modes.interactive.theme.theme_schema import validateThemeJson
from misaka.ui.tui.terminal_image import resetCapabilitiesCache, setCapabilityOverrides


@pytest.fixture(autouse=True)
def validated_themes(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(theme_module, "themeJsonValidator", None)
    setThemeJsonValidator(validateThemeJson)


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    agentDir = tmp_path / "agent"
    cwd = tmp_path / "project"
    agentDir.mkdir(parents=True)
    cwd.mkdir(parents=True)

    themeJson = json.loads((Path(theme_package.__file__).parent / "dark.json").read_text(encoding="utf-8"))
    themeJson["name"] = "capability-test"
    themeJson["colors"]["userMessageBg"] = "#3c3544"
    themePath = tmp_path / "capability-test.json"
    themePath.write_text(json.dumps(themeJson), encoding="utf-8")
    yield SimpleNamespace(agentDir=str(agentDir), cwd=str(cwd), themePath=str(themePath), themeJson=themeJson)
    monkeypatch.undo()
    setCapabilityOverrides({})
    resetCapabilitiesCache()


def loaderOptions(dirs: SimpleNamespace, settingsManager: SettingsManager) -> dict[str, object]:
    return {
        "cwd": dirs.cwd,
        "agentDir": dirs.agentDir,
        "settingsManager": settingsManager,
        "additionalThemePaths": [dirs.themePath],
        "noExtensions": True,
        "noPromptTemplates": True,
    }


def capabilityTestTheme(loader: DefaultResourceLoader):
    return next((theme for theme in loader.getThemes()["themes"] if theme.name == "capability-test"), None)


# Regression test for #9973.
@pytest.mark.parametrize(
    ("environment", "environmentOverride", "setting", "expected"),
    [
        ("256-color", "0", True, "\x1b[48;2;60;53;68mx\x1b[49m"),
        ("truecolor", "1", False, "\x1b[48;5;59mx\x1b[49m"),
    ],
)
async def test_uses_the_setting_over_the_environment(
    environment: str,
    environmentOverride: str,
    setting: bool,
    expected: str,
    dirs: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("MISAKA_TRUE_COLOR", environmentOverride)
    setCapabilityOverrides({})
    resetCapabilitiesCache()

    loader = DefaultResourceLoader(
        loaderOptions(dirs, SettingsManager.inMemory({"terminal": {"trueColor": setting}}))
    )
    await loader.reload()

    loadedTheme = capabilityTestTheme(loader)
    assert loadedTheme is not None
    assert loadedTheme.bg("userMessageBg", "x") == expected


async def test_returns_to_automatic_detection_after_an_explicit_setting_is_removed(
    dirs: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MISAKA_TRUE_COLOR", "1")
    setCapabilityOverrides({})
    resetCapabilitiesCache()

    settingsPath = Path(dirs.agentDir) / "settings.json"
    settingsPath.write_text(json.dumps({"terminal": {"trueColor": False}}), encoding="utf-8")
    settingsManager = SettingsManager.create(dirs.cwd, dirs.agentDir)
    loader = DefaultResourceLoader(loaderOptions(dirs, settingsManager))
    await loader.reload()
    setCapabilityOverrides(settingsManager.getTerminalCapabilityOverrides())

    settingsPath.write_text("{}", encoding="utf-8")
    await loader.reload()

    loadedTheme = capabilityTestTheme(loader)
    assert loadedTheme is not None
    assert loadedTheme.bg("userMessageBg", "x") == "\x1b[48;2;60;53;68mx\x1b[49m"


async def test_applies_strict_validation_on_initial_load_and_reload(dirs: SimpleNamespace):
    loader = DefaultResourceLoader(loaderOptions(dirs, SettingsManager.inMemory()))
    themeJson = dirs.themeJson
    themePath = Path(dirs.themePath)

    themeJson["unsupported"] = True
    themePath.write_text(json.dumps(themeJson), encoding="utf-8")
    await loader.reload()
    initialDiagnostics = loader.getThemes()["diagnostics"]
    assert len(initialDiagnostics) == 1
    assert initialDiagnostics[0].type == "warning"
    assert initialDiagnostics[0].path == dirs.themePath
    assert "/unsupported" in initialDiagnostics[0].message

    del themeJson["unsupported"]
    themePath.write_text(json.dumps(themeJson), encoding="utf-8")
    await loader.reload()
    assert loader.getThemes()["diagnostics"] == []
    assert any(theme.name == themeJson["name"] for theme in loader.getThemes()["themes"])

    themeJson["unsupported"] = True
    themePath.write_text(json.dumps(themeJson), encoding="utf-8")
    await loader.reload()
    assert loader.getThemes()["diagnostics"] == initialDiagnostics

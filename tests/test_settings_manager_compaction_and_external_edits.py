"""Translated from pi's ``packages/coding-agent/test/settings-manager-compaction.test.ts`` and
``settings-manager-bug.test.ts`` (v1.1.0)."""

from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.config.home import DIR_NAME
from misaka.core.settings_manager import InMemorySettingsStorage, SettingsManager
from misaka.utils.js import jsString

model = SimpleNamespace(provider="provider", id="family/model")
modelKey = "provider/family/model"
defaults = {"enabled": True, "reserveTokens": 16384, "keepRecentTokens": 20000}
MAX_SAFE_INTEGER = 2**53 - 1


def fromJson(global_: Any = None, project: Any = None) -> SettingsManager:
    storage = InMemorySettingsStorage()
    if global_ is not None:
        storage.withLock("global", lambda _current: json.dumps(global_))
    if project is not None:
        storage.withLock("project", lambda _current: json.dumps(project))
    return SettingsManager.fromStorage(storage)


# --- compaction model overrides (regression coverage for #8133) ---------------------------------


def test_uses_defaults_without_compaction_settings():
    manager = SettingsManager.inMemory()
    assert manager.getCompactionSettings() == defaults
    assert manager.getCompactionSettings(model) == defaults


def test_resolves_each_field_independently_and_keeps_individual_getters_consistent():
    manager = SettingsManager.inMemory({
        "compaction": {
            "reserveTokens": 8192,
            "keepRecentTokens": 10000,
            "modelOverrides": {modelKey: {"reserveTokens": 400000}},
        }
    })
    assert manager.getCompactionSettings(model) == {"enabled": True, "reserveTokens": 400000, "keepRecentTokens": 10000}
    assert manager.getCompactionReserveTokens(model) == 400000
    assert manager.getCompactionKeepRecentTokens(model) == 10000
    assert manager.getCompactionSettings() == {"enabled": True, "reserveTokens": 8192, "keepRecentTokens": 10000}

    manager.applyOverrides({"compaction": {"modelOverrides": {modelKey: {"keepRecentTokens": 30000}}}})
    assert manager.getCompactionKeepRecentTokens(model) == 30000
    assert manager.getCompactionReserveTokens(model) == 400000


def test_falls_back_to_built_in_defaults_for_missing_fields():
    manager = SettingsManager.inMemory({"compaction": {"modelOverrides": {modelKey: {"keepRecentTokens": 1024}}}})
    assert manager.getCompactionSettings(model) == {**defaults, "keepRecentTokens": 1024}


def test_matches_exact_provider_model_ids_including_ids_containing_slashes():
    manager = SettingsManager.inMemory({
        "compaction": {
            "modelOverrides": {
                modelKey: {"reserveTokens": 400000},
                "provider/*": {"reserveTokens": 1},
                "family/model": {"reserveTokens": 2},
            }
        }
    })
    assert manager.getCompactionReserveTokens(model) == 400000
    for other in [
        SimpleNamespace(provider="other", id=model.id),
        SimpleNamespace(provider=model.provider, id="other"),
        SimpleNamespace(provider=model.provider, id="family/Model"),
    ]:
        assert manager.getCompactionSettings(other) == defaults


async def test_merges_project_model_overrides_per_field_before_resolving_fallbacks():
    manager = fromJson(
        {
            "compaction": {
                "reserveTokens": 8192,
                "modelOverrides": {
                    modelKey: {"reserveTokens": 400000, "keepRecentTokens": 30000},
                    "provider/other": {"keepRecentTokens": 4096},
                },
            }
        },
        {"compaction": {"reserveTokens": 1024, "modelOverrides": {modelKey: {"keepRecentTokens": 2000}}}},
    )
    assert manager.getCompactionSettings(model) == {"enabled": True, "reserveTokens": 400000, "keepRecentTokens": 2000}
    assert manager.getCompactionSettings(SimpleNamespace(provider="provider", id="other")) == {
        "enabled": True,
        "reserveTokens": 1024,
        "keepRecentTokens": 4096,
    }
    await manager.reload()
    assert manager.getCompactionKeepRecentTokens(model) == 2000
    manager.setProjectTrusted(False)
    assert manager.getCompactionKeepRecentTokens(model) == 30000


async def test_keeps_enabled_global_and_preserves_overrides_when_saving_the_toggle():
    manager = fromJson({"compaction": {"modelOverrides": {modelKey: {"enabled": False, "reserveTokens": 400000}}}})
    assert manager.getCompactionSettings(model)["enabled"] is True
    manager.setCompactionEnabled(False)
    await manager.flush()
    await manager.reload()
    assert manager.getCompactionSettings(model) == {**defaults, "enabled": False, "reserveTokens": 400000}


INVALID_TOKEN_VALUES = [None, -1, 1.5, "400000", True, {}, [], MAX_SAFE_INTEGER + 1]


@pytest.mark.parametrize("field", ["reserveTokens", "keepRecentTokens"])
@pytest.mark.parametrize("value", INVALID_TOKEN_VALUES, ids=[json.dumps(value) for value in INVALID_TOKEN_VALUES])
def test_model_override_reports_invalid_token_values(field: str, value: Any):
    manager = fromJson({"compaction": {"modelOverrides": {modelKey: {field: value}}}})
    with pytest.raises(Exception) as error:
        manager.getCompactionSettings(model)
    assert str(error.value) == (
        f'Invalid compaction.modelOverrides["{modelKey}"].{field} setting: {jsString(value)}. '
        "Expected a non-negative safe integer."
    )
    assert manager.getCompactionSettings() == defaults
    assert manager.getCompactionSettings(SimpleNamespace(provider="other", id=model.id)) == defaults


@pytest.mark.parametrize("field", ["reserveTokens", "keepRecentTokens"])
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf], ids=["NaN", "Infinity", "-Infinity"])
def test_model_override_reports_non_finite_runtime_values(field: str, value: float):
    manager = SettingsManager.inMemory()
    manager.applyOverrides({"compaction": {"modelOverrides": {modelKey: {field: value}}}})
    with pytest.raises(Exception) as error:
        manager.getCompactionSettings(model)
    assert f'Invalid compaction.modelOverrides["{modelKey}"].{field} setting: {jsString(value)}' in str(error.value)


@pytest.mark.parametrize("field", ["reserveTokens", "keepRecentTokens"])
@pytest.mark.parametrize("value", INVALID_TOKEN_VALUES, ids=[json.dumps(value) for value in INVALID_TOKEN_VALUES])
def test_ordinary_setting_reports_invalid_values_even_when_a_valid_model_override_exists(field: str, value: Any):
    manager = fromJson({"compaction": {field: value, "modelOverrides": {modelKey: {field: 4096}}}})
    error = f"Invalid compaction.{field} setting: {jsString(value)}. Expected a non-negative safe integer."
    with pytest.raises(Exception) as first:
        manager.getCompactionSettings()
    assert str(first.value) == error
    with pytest.raises(Exception) as second:
        manager.getCompactionSettings(model)
    assert str(second.value) == error


@pytest.mark.parametrize("field", ["reserveTokens", "keepRecentTokens"])
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf], ids=["NaN", "Infinity", "-Infinity"])
def test_ordinary_setting_reports_non_finite_runtime_values(field: str, value: float):
    manager = SettingsManager.inMemory()
    manager.applyOverrides({"compaction": {field: value}})
    with pytest.raises(Exception) as error:
        manager.getCompactionSettings()
    assert f"Invalid compaction.{field} setting: {jsString(value)}" in str(error.value)


MALFORMED_ENTRIES = [None, False, 42, "invalid", []]


@pytest.mark.parametrize("entry", MALFORMED_ENTRIES, ids=[json.dumps(entry) for entry in MALFORMED_ENTRIES])
def test_reports_malformed_model_entries(entry: Any):
    with pytest.raises(Exception) as error:
        fromJson({"compaction": {"modelOverrides": {modelKey: entry}}}).getCompactionSettings(model)
    assert str(error.value) == (
        f'Invalid compaction.modelOverrides["{modelKey}"] setting: {jsString(entry)}. Expected an object.'
    )


def test_accepts_zero_in_ordinary_settings_and_model_overrides():
    manager = SettingsManager.inMemory({"compaction": {"reserveTokens": 0, "keepRecentTokens": 0}})
    assert manager.getCompactionSettings(model) == {"enabled": True, "reserveTokens": 0, "keepRecentTokens": 0}
    manager.applyOverrides({
        "compaction": {
            "reserveTokens": 1000,
            "keepRecentTokens": 1000,
            "modelOverrides": {modelKey: {"reserveTokens": 0, "keepRecentTokens": 0}},
        }
    })
    assert manager.getCompactionSettings(model) == {"enabled": True, "reserveTokens": 0, "keepRecentTokens": 0}


# --- SettingsManager - External Edit Preservation -----------------------------------------------
#
# Tests for the fix to a bug where external file changes to arrays were overwritten: the manager tracks which fields
# were explicitly modified during the session, and only those fields override file values during save().
#
# MISAKA fork: pi's `packages` case is not translated (MISAKA's package manager has no `packages` sources), and the two
# project cases use project prompt paths where pi uses project extension paths (MISAKA never enables extensions from
# project settings).


@pytest.fixture
def dirs(tmp_path: Path) -> SimpleNamespace:
    agentDir = tmp_path / "agent"
    projectDir = tmp_path / "project"
    agentDir.mkdir(parents=True)
    (projectDir / DIR_NAME).mkdir(parents=True)
    return SimpleNamespace(agentDir=agentDir, projectDir=projectDir)


def readJson(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


async def test_should_preserve_file_changes_to_extensions_array_when_changing_unrelated_setting(dirs: SimpleNamespace):
    settingsPath = dirs.agentDir / "settings.json"
    settingsPath.write_text(json.dumps({"theme": "dark", "extensions": ["/old/extension.py"]}), encoding="utf-8")

    manager = SettingsManager.create(str(dirs.projectDir), str(dirs.agentDir))

    # User externally updates extensions
    currentSettings = readJson(settingsPath)
    currentSettings["extensions"] = ["/new/extension.py"]
    settingsPath.write_text(json.dumps(currentSettings, indent=2), encoding="utf-8")

    # Change unrelated setting
    manager.setDefaultThinkingLevel("high")
    await manager.flush()

    # With the fix, extensions should be preserved (not reverted to startup value)
    assert readJson(settingsPath)["extensions"] == ["/new/extension.py"]


async def test_should_preserve_external_project_settings_changes_when_updating_unrelated_project_field(
    dirs: SimpleNamespace,
):
    projectSettingsPath = dirs.projectDir / DIR_NAME / "settings.json"
    projectSettingsPath.write_text(
        json.dumps({"themes": ["./old-theme.json"], "prompts": ["./old-prompt.md"]}), encoding="utf-8"
    )

    manager = SettingsManager.create(str(dirs.projectDir), str(dirs.agentDir))
    manager.setProjectTrusted(True)

    currentProjectSettings = readJson(projectSettingsPath)
    currentProjectSettings["themes"] = ["./new-theme.json"]
    projectSettingsPath.write_text(json.dumps(currentProjectSettings, indent=2), encoding="utf-8")

    manager.setProjectPromptTemplatePaths(["./updated-prompt.md"])
    await manager.flush()

    savedProjectSettings = readJson(projectSettingsPath)
    assert savedProjectSettings["themes"] == ["./new-theme.json"]
    assert savedProjectSettings["prompts"] == ["./updated-prompt.md"]


async def test_should_let_in_memory_project_changes_override_external_changes_for_the_same_project_field(
    dirs: SimpleNamespace,
):
    projectSettingsPath = dirs.projectDir / DIR_NAME / "settings.json"
    projectSettingsPath.write_text(json.dumps({"prompts": ["./initial-prompt.md"]}), encoding="utf-8")

    manager = SettingsManager.create(str(dirs.projectDir), str(dirs.agentDir))
    manager.setProjectTrusted(True)

    currentProjectSettings = readJson(projectSettingsPath)
    currentProjectSettings["prompts"] = ["./external-prompt.md"]
    projectSettingsPath.write_text(json.dumps(currentProjectSettings, indent=2), encoding="utf-8")

    manager.setProjectPromptTemplatePaths(["./in-memory-prompt.md"])
    await manager.flush()

    assert readJson(projectSettingsPath)["prompts"] == ["./in-memory-prompt.md"]

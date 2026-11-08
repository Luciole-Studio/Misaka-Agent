"""`misaka config`: pi v1.5.0 ``test/package-command-paths.test.ts`` (the built-in extension cases of the config
selector; the package case has no MISAKA counterpart), and MISAKA's own forks."""

from __future__ import annotations

import json

import pytest

from misaka.config import home
from misaka.core.package_manager import DefaultPackageManager
from misaka.core.settings_manager import InMemorySettingsStorage, SettingsManager
from misaka.modes.interactive.components.config_selector import ConfigSelectorComponent
from misaka.modes.interactive.theme.theme import initTheme
from misaka.utils.ansi import stripAnsi
from misaka.utils.paths import shortenHomePath


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    agentDir = tmp_path / "agent"
    projectDir = tmp_path / "project"
    agentDir.mkdir()
    projectDir.mkdir()
    monkeypatch.setenv(home.ENV_HOME, str(tmp_path / "home"))
    return projectDir, agentDir


async def _selector(projectDir, agentDir, settingsManager, builtinExtensions, writeScope):
    resolvedPaths = await DefaultPackageManager({
        "cwd": str(projectDir),
        "agentDir": str(agentDir),
        "settingsManager": settingsManager,
        "builtinExtensions": builtinExtensions,
    }).resolve()
    return ConfigSelectorComponent(
        {"global": resolvedPaths, "project": resolvedPaths},
        settingsManager,
        str(projectDir),
        str(agentDir),
        lambda: None,
        lambda: None,
        lambda: None,
        24,
        writeScope,
    )


async def test_toggles_built_in_extensions_in_config_global_mode(dirs):
    projectDir, agentDir = dirs
    settingsManager = SettingsManager.fromStorage(InMemorySettingsStorage(), {"projectTrusted": True})
    selector = await _selector(projectDir, agentDir, settingsManager, ["llama.cpp", "mcp"], "global")

    resourceList = selector.getResourceList()
    initTheme("dark")
    assert "Built-in" in "\n".join(resourceList.render(80))
    assert "llama.cpp" in "\n".join(resourceList.render(80))
    resourceList.handleInput(" ")
    assert settingsManager.getGlobalSettings()["extensions"] == ["-builtin:llama.cpp"]
    resourceList.handleInput(" ")
    assert settingsManager.getGlobalSettings()["extensions"] == ["+builtin:llama.cpp"]


async def test_cycles_project_built_in_extension_overrides_in_config_local_mode(dirs):
    projectDir, agentDir = dirs
    storage = InMemorySettingsStorage()
    storage.withLock("global", lambda _current: json.dumps({"extensions": ["-builtin:mcp"]}))
    settingsManager = SettingsManager.fromStorage(storage, {"projectTrusted": True})
    selector = await _selector(projectDir, agentDir, settingsManager, ["mcp"], "project")

    resourceList = selector.getResourceList()
    resourceList.handleInput(" ")
    assert settingsManager.getProjectSettings()["extensions"] == ["+builtin:mcp"]
    resourceList.handleInput(" ")
    assert settingsManager.getProjectSettings()["extensions"] == ["-builtin:mcp"]
    resourceList.handleInput(" ")
    assert settingsManager.getProjectSettings()["extensions"] == []


# MISAKA forks


async def test_a_toggle_leaves_misakas_skills_object_alone_and_lists_no_skills(dirs):
    projectDir, agentDir = dirs
    storage = InMemorySettingsStorage()
    skills = {"disabled": ["old-skill"], "external_dirs": ["~/more-skills"]}
    storage.withLock("global", lambda _current: json.dumps({"skills": skills}))
    settingsManager = SettingsManager.fromStorage(storage, {"projectTrusted": True})
    selector = await _selector(projectDir, agentDir, settingsManager, ["mcp"], "global")

    resourceList = selector.getResourceList()
    initTheme("dark")
    assert "Skills" not in stripAnsi("\n".join(resourceList.render(80)))
    resourceList.handleInput(" ")
    assert settingsManager.getGlobalSettings()["skills"] == skills
    assert settingsManager.getGlobalSettings()["extensions"] == ["-builtin:mcp"]


async def test_lists_no_project_python_extension(dirs):
    projectDir, agentDir = dirs
    extension = projectDir / home.DIR_NAME / "extensions" / "project_tool.py"
    extension.parent.mkdir(parents=True)
    extension.write_text("def default(pi):\n    pass\n", encoding="utf-8")
    settingsManager = SettingsManager.create(str(projectDir), str(agentDir), {"projectTrusted": True})
    selector = await _selector(projectDir, agentDir, settingsManager, ["mcp"], "project")

    initTheme("dark")
    rendered = stripAnsi("\n".join(selector.getResourceList().render(120)))
    assert "mcp" in rendered
    assert "project_tool" not in rendered


async def test_the_global_header_names_the_homes_settings_file(dirs):
    projectDir, agentDir = dirs
    settingsManager = SettingsManager.fromStorage(InMemorySettingsStorage(), {"projectTrusted": True})
    selector = await _selector(projectDir, agentDir, settingsManager, ["mcp"], "global")

    initTheme("dark")
    rendered = stripAnsi("\n".join(selector.render(400)))
    assert "Global Resources" in rendered
    assert shortenHomePath(str(agentDir / "settings.json")) in rendered


def test_builtin_names_lists_every_bundled_built_in_extension():
    from misaka.extensions import builtin_names

    assert set(builtin_names()) == {
        "llama.cpp", "codemode", "tool-search", "mcp", "coverage", "misaka_lcm", "reading", "vision",
    }


async def test_config_command_refuses_local_mode_in_an_untrusted_project(dirs, monkeypatch, capsys):
    from misaka.cli.package_manager import handleConfigCommand

    projectDir, _agentDir = dirs
    monkeypatch.chdir(projectDir)
    assert await handleConfigCommand(["config", "-l", "-na"]) == 1
    assert "Project is not trusted. Use --approve to modify local resource config." in capsys.readouterr().err


async def test_config_command_help_and_argument_errors(capsys):
    from misaka.cli.package_manager import handleConfigCommand

    assert await handleConfigCommand(["other"]) is None
    assert await handleConfigCommand(["config", "--help"]) == 0
    assert "config [-l] [--approve|--no-approve]" in capsys.readouterr().out
    assert await handleConfigCommand(["config", "--bogus"]) == 1
    assert 'Unknown option --bogus for "config".' in capsys.readouterr().err
    assert await handleConfigCommand(["config", "extra"]) == 1
    assert "Unexpected argument extra." in capsys.readouterr().err

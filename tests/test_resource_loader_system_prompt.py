"""Translated from pi's ``packages/coding-agent/test/resource-loader.test.ts`` (v1.1.0): the SYSTEM.md / APPEND_SYSTEM.md
discovery cases and the system prompt sources.

MISAKA fork: a project's config directory is ``home.project_dir`` (``<cwd>/.misaka``), where pi's is ``<cwd>/.pi``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from misaka.config import home
from misaka.core.resource_loader import DefaultResourceLoader
from misaka.core.settings_manager import SettingsManager


@pytest.fixture
def dirs(tmp_path: Path):
    agentDir = tmp_path / "agent"
    cwd = tmp_path / "project"
    agentDir.mkdir(parents=True)
    cwd.mkdir(parents=True)
    projectDir = home.project_dir(cwd)
    assert projectDir is not None
    return SimpleNamespace(tempDir=tmp_path, agentDir=agentDir, cwd=cwd, projectDir=projectDir)


def loader(dirs: SimpleNamespace, **options: object) -> DefaultResourceLoader:
    return DefaultResourceLoader({"cwd": str(dirs.cwd), "agentDir": str(dirs.agentDir), "noExtensions": True, **options})


async def test_should_discover_system_md_from_the_project_dir(dirs):
    dirs.projectDir.mkdir(parents=True)
    (dirs.projectDir / "SYSTEM.md").write_text("You are a helpful assistant.", encoding="utf-8")

    resourceLoader = loader(dirs)
    await resourceLoader.reload()

    assert resourceLoader.getSystemPrompt() == "You are a helpful assistant."


async def test_should_skip_the_project_system_prompt_when_project_is_not_trusted(dirs):
    dirs.projectDir.mkdir(parents=True)
    (dirs.projectDir / "SYSTEM.md").write_text("Project system prompt.", encoding="utf-8")
    (dirs.projectDir / "APPEND_SYSTEM.md").write_text("Project append prompt.", encoding="utf-8")
    (dirs.agentDir / "SYSTEM.md").write_text("Global system prompt.", encoding="utf-8")
    settingsManager = SettingsManager.create(str(dirs.cwd), str(dirs.agentDir), {"projectTrusted": False})

    resourceLoader = loader(dirs, settingsManager=settingsManager)
    await resourceLoader.reload()

    assert resourceLoader.getSystemPrompt() == "Global system prompt."
    assert resourceLoader.getAppendSystemPrompt() == []


async def test_should_discover_append_system_md(dirs):
    dirs.projectDir.mkdir(parents=True)
    (dirs.projectDir / "APPEND_SYSTEM.md").write_text("Additional instructions.", encoding="utf-8")

    resourceLoader = loader(dirs)
    await resourceLoader.reload()

    assert "Additional instructions." in resourceLoader.getAppendSystemPrompt()


async def test_exposes_discovered_project_system_md_as_the_system_prompt_source(dirs):
    systemPromptPath = dirs.projectDir / "SYSTEM.md"
    dirs.projectDir.mkdir(parents=True)
    systemPromptPath.write_text("Project system prompt.", encoding="utf-8")

    resourceLoader = loader(dirs)
    await resourceLoader.reload()

    assert resourceLoader.getSystemPrompt() == "Project system prompt."
    assert resourceLoader.getSystemPromptSource() == {"path": str(systemPromptPath)}


async def test_exposes_discovered_global_system_md_as_the_system_prompt_source(dirs):
    systemPromptPath = dirs.agentDir / "SYSTEM.md"
    systemPromptPath.write_text("Global system prompt.", encoding="utf-8")

    resourceLoader = loader(dirs)
    await resourceLoader.reload()

    assert resourceLoader.getSystemPrompt() == "Global system prompt."
    assert resourceLoader.getSystemPromptSource() == {"path": str(systemPromptPath)}


async def test_exposes_discovered_append_system_md_as_an_append_system_prompt_source(dirs):
    appendSystemPromptPath = dirs.projectDir / "APPEND_SYSTEM.md"
    dirs.projectDir.mkdir(parents=True)
    appendSystemPromptPath.write_text("Project append prompt.", encoding="utf-8")

    resourceLoader = loader(dirs)
    await resourceLoader.reload()

    assert resourceLoader.getAppendSystemPrompt() == ["Project append prompt."]
    assert resourceLoader.getAppendSystemPromptSources() == [{"path": str(appendSystemPromptPath)}]


def test_classifies_a_path_under_a_project_root_as_project_scope(dirs):
    promptsDir = dirs.projectDir / "prompts"
    promptsDir.mkdir(parents=True)
    promptPath = promptsDir / "project.md"
    promptPath.write_text("Project prompt", encoding="utf-8")

    info = loader(dirs).getDefaultSourceInfoForPath(str(promptPath))

    assert (info.scope, info.baseDir) == ("project", str(promptsDir))

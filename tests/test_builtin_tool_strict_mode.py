"""Translated from pi's ``packages/coding-agent/test/builtin-tool-strict-mode.test.ts`` (v1.1.0).

MISAKA fork: the resource loader has no ``noSkills`` option (skills live in ``misaka/core/skills``), so it is not passed.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Any

import pytest

from misaka.ai.compat import getModel
from misaka.core.resource_loader import DefaultResourceLoader
from misaka.core.sdk import createAgentSession
from misaka.core.session_manager import SessionManager
from misaka.core.settings_manager import SettingsManager
from misaka.core.tools import createAllToolDefinitions, createAllTools
from misaka.core.tools.tool_definition_wrapper import wrapToolDefinition
from misaka.utils.values import read_field

strictToolNames = ["read", "bash", "powershell", "edit", "write"]


def sampling(value: Any) -> Any:
    # AgentTool is a pydantic model and holds the setting as a model; compare the plain shape.
    return value.model_dump(exclude_none=True) if hasattr(value, "model_dump") else value


def requiredOf(parameters: Any) -> list[str]:
    schema = parameters if isinstance(parameters, dict) else parameters.model_json_schema()
    return list(schema.get("required", []))


@pytest.mark.parametrize("experimental", [None, "0", "1"])
def test_prefers_strict_sampling_with_pi_experimental(experimental: str | None, monkeypatch: pytest.MonkeyPatch):
    if experimental is None:
        monkeypatch.delenv("PI_EXPERIMENTAL", raising=False)
    else:
        monkeypatch.setenv("PI_EXPERIMENTAL", experimental)
    definitions = createAllToolDefinitions(os.getcwd())
    tools = createAllTools(os.getcwd())
    for name in strictToolNames:
        assert definitions[name].constrainedSampling == {"type": "json_schema", "strict": "prefer"}
        assert sampling(tools[name].constrainedSampling) == definitions[name].constrainedSampling
    for name in ("grep", "find", "ls"):
        assert definitions[name].constrainedSampling is None
    # Strictness is a provider-side conversion, not a change to the execution schema.
    assert requiredOf(definitions["read"].parameters) == ["path"]
    assert requiredOf(definitions["bash"].parameters) == ["command"]


def test_preserves_explicit_opt_outs_when_wrapping_definitions_for_execution():
    definitions = createAllToolDefinitions(os.getcwd())
    for name in strictToolNames:
        definition = definitions[name]
        override = dataclasses.replace(definition, constrainedSampling=False)
        assert wrapToolDefinition(override).constrainedSampling is False
        assert override.execute is definition.execute
        assert override.prepareArguments is definition.prepareArguments
        assert override.renderCall is definition.renderCall
        assert override.renderResult is definition.renderResult
        assert override.promptGuidelines is definition.promptGuidelines
        assert definition.constrainedSampling == {"type": "json_schema", "strict": "prefer"}


@pytest.mark.parametrize("activeTools", [[], ["read"], list(strictToolNames)])
async def test_allows_extensions_to_re_register_tools_without_strict_sampling(activeTools: list[str], tmp_path: Path):
    cwd = str(tmp_path)
    agentDir = str(tmp_path / "agent")
    settingsManager = SettingsManager.inMemory({"defaultTools": activeTools})

    def extension(pi: Any) -> None:
        def onSessionStart(*_: Any) -> None:
            definitions = createAllToolDefinitions(cwd)
            for name in strictToolNames:
                pi.registerTool(dataclasses.replace(definitions[name], constrainedSampling=False))
            pi.setActiveTools(activeTools)

        pi.on("session_start", onSessionStart)

    resourceLoader = DefaultResourceLoader({
        "cwd": cwd,
        "agentDir": agentDir,
        "settingsManager": settingsManager,
        "noExtensions": True,
        "noPromptTemplates": True,
        "noThemes": True,
        "extensionFactories": [extension],
    })
    await resourceLoader.reload()
    session = (
        await createAgentSession({
            "cwd": cwd,
            "agentDir": agentDir,
            "model": getModel("anthropic", "claude-sonnet-4-5"),
            "settingsManager": settingsManager,
            "sessionManager": SessionManager.inMemory(cwd),
            "resourceLoader": resourceLoader,
        })
    )["session"]
    try:
        originalPrompt = session.systemPrompt
        await session.bindExtensions({})
        assert session.getActiveToolNames() == activeTools
        assert session.systemPrompt == originalPrompt
        for name in strictToolNames:
            definition = session.getToolDefinition(name)
            assert read_field(definition, "constrainedSampling") is False
        for tool in session.agent.state.tools:
            assert tool.constrainedSampling is False
        if "read" in activeTools:
            (tmp_path / "sample.txt").write_text("still works", encoding="utf-8")
            read = next(tool for tool in session.agent.state.tools if tool.name == "read")
            result = await read.execute("read-test", {"path": "sample.txt"})
            assert [
                {"type": read_field(block, "type"), "text": read_field(block, "text")} for block in result.content
            ] == [{"type": "text", "text": "still works"}]
    finally:
        session.dispose()

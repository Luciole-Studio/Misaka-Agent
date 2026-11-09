"""Translated from pi's ``packages/coding-agent/test/tool-system-prompt-contributions.test.ts`` (v1.1.0): built-in
tool system prompt contributions."""

from __future__ import annotations

from typing import Any

import pytest

from misaka.core.tools.bash import (
    bashToolSystemPromptContribution,
    createBashToolDefinition,
)
from misaka.core.tools.edit import (
    createEditToolDefinition,
    editToolSystemPromptContribution,
)
from misaka.core.tools.find import (
    createFindToolDefinition,
    findToolSystemPromptContribution,
)
from misaka.core.tools.grep import (
    createGrepToolDefinition,
    grepToolSystemPromptContribution,
)
from misaka.core.tools.ls import createLsToolDefinition, lsToolSystemPromptContribution
from misaka.core.tools.powershell import (
    createPowerShellToolDefinition,
    powershellToolSystemPromptContribution,
)
from misaka.core.tools.read import (
    createReadToolDefinition,
    readToolSystemPromptContribution,
)
from misaka.core.tools.write import (
    createWriteToolDefinition,
    writeToolSystemPromptContribution,
)

cases = [
    ("read", readToolSystemPromptContribution, createReadToolDefinition),
    ("bash", bashToolSystemPromptContribution, createBashToolDefinition),
    ("powershell", powershellToolSystemPromptContribution, createPowerShellToolDefinition),
    ("edit", editToolSystemPromptContribution, createEditToolDefinition),
    ("write", writeToolSystemPromptContribution, createWriteToolDefinition),
    ("grep", grepToolSystemPromptContribution, createGrepToolDefinition),
    ("find", findToolSystemPromptContribution, createFindToolDefinition),
    ("ls", lsToolSystemPromptContribution, createLsToolDefinition),
]


@pytest.mark.parametrize(("_name", "contribution", "createDefinition"), cases, ids=[case[0] for case in cases])
def test_keeps_the_tool_definition_aligned_with_its_contribution(_name: str, contribution: Any, createDefinition: Any):
    definition = createDefinition("/workspace")

    assert definition.promptSnippet == contribution["snippet"]
    assert list(definition.promptGuidelines or []) == list(contribution["guidelines"])


@pytest.mark.parametrize(
    ("_name", "createDefinition"),
    [("bash", createBashToolDefinition), ("powershell", createPowerShellToolDefinition)],
    ids=["bash", "powershell"],
)
def test_keeps_session_environment_guidance_conditional(_name: str, createDefinition: Any):
    definition = createDefinition("/workspace", {"exposeSessionEnvironment": False})

    assert definition.promptGuidelines is None

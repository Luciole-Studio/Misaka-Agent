"""Translated from pi's ``packages/coding-agent/test/sdk-session-manager.test.ts`` (v1.1.0).

MISAKA forks: a cwd's session bucket is ``encode_cwd`` (``--<slug>-<sha256>--``; pi: the path with separators turned
into dashes), so the expected directory is built with ``get_session_dir_for_cwd``. On Windows the bash tool runs Git
Bash, whose ``pwd`` prints ``/c/...``; ``pwd -W`` prints the Windows form that the path comparison needs.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.compat import getModel
from misaka.core.sdk import createAgentSession
from misaka.core.session_manager import SessionManager, get_session_dir_for_cwd
from misaka.utils.values import read_field


@pytest.fixture
def dirs(tmp_path: Path) -> SimpleNamespace:
    cwd = tmp_path / "project"
    agentDir = tmp_path / "agent"
    cwd.mkdir(parents=True)
    agentDir.mkdir(parents=True)
    return SimpleNamespace(tempDir=tmp_path, cwd=str(cwd), agentDir=str(agentDir))


def textOutput(result: Any) -> str:
    return "".join(
        read_field(item, "text") for item in read_field(result, "content") if read_field(item, "type") == "text"
    )


async def test_uses_agent_dir_for_the_default_persisted_session_path(dirs: SimpleNamespace):
    model = getModel("anthropic", "claude-sonnet-4-5")
    assert model

    session = (await createAgentSession({"cwd": dirs.cwd, "agentDir": dirs.agentDir, "model": model}))["session"]
    try:
        expectedSessionDir = get_session_dir_for_cwd(dirs.cwd, os.path.join(dirs.agentDir, "sessions"))
        sessionDir = session.sessionManager.getSessionDir()
        sessionFile = session.sessionManager.getSessionFile()

        assert sessionDir == expectedSessionDir
        assert sessionFile is not None and sessionFile.startswith(f"{expectedSessionDir}{os.sep}")
    finally:
        session.dispose()


async def test_keeps_an_explicit_session_manager_override(dirs: SimpleNamespace):
    model = getModel("anthropic", "claude-sonnet-4-5")
    assert model

    sessionManager = SessionManager.inMemory(dirs.cwd)
    session = (
        await createAgentSession({
            "cwd": dirs.cwd,
            "agentDir": dirs.agentDir,
            "model": model,
            "sessionManager": sessionManager,
        })
    )["session"]
    try:
        assert session.sessionManager is sessionManager
        assert session.sessionManager.isPersisted() is False
    finally:
        session.dispose()


async def test_derives_cwd_from_an_explicit_session_manager_when_cwd_is_omitted(dirs: SimpleNamespace):
    model = getModel("anthropic", "claude-sonnet-4-5")
    assert model

    sessionCwd = str(dirs.tempDir / "session-project")
    os.makedirs(sessionCwd)
    sessionManager = SessionManager.inMemory(sessionCwd)
    session = (
        await createAgentSession({"agentDir": dirs.agentDir, "model": model, "sessionManager": sessionManager})
    )["session"]
    try:
        assert session.sessionManager is sessionManager
        # The prompt renders the cwd with forward slashes (pi system-prompt.ts).
        promptCwd = sessionCwd.replace("\\", "/")
        assert f"<cwd>\n{promptCwd}\n</cwd>" in session.systemPrompt

        bashTool = next((tool for tool in session.agent.state.tools if tool.name == "bash"), None)
        assert bashTool
        result = await bashTool.execute("test", {"command": "pwd -W" if sys.platform == "win32" else "pwd"})
        output = textOutput(result)

        assert os.path.realpath(output.strip()) == os.path.realpath(sessionCwd)
    finally:
        session.dispose()


async def test_exposes_current_session_state_to_the_built_in_bash_tool(dirs: SimpleNamespace):
    model = getModel("anthropic", "claude-sonnet-4-5")
    assert model

    session = (
        await createAgentSession({
            "cwd": dirs.cwd,
            "agentDir": dirs.agentDir,
            "model": model,
            "thinkingLevel": "high",
        })
    )["session"]
    try:
        assert session.sessionFile
        assert (
            "You can inspect PI_* environment variables for current model and session details." in session.systemPrompt
        )

        bashTool = next((tool for tool in session.agent.state.tools if tool.name == "bash"), None)
        assert bashTool
        result = await bashTool.execute(
            "test",
            {
                "command": (
                    "printf '%s\\n' \"$PI_SESSION_ID\" \"$PI_SESSION_FILE\" \"$PI_PROVIDER\" \"$PI_MODEL\" "
                    "\"$PI_REASONING_LEVEL\""
                )
            },
        )
        output = textOutput(result)

        assert output.strip().split("\n") == [
            session.sessionId,
            session.sessionFile,
            model.provider,
            model.id,
            session.thinkingLevel,
        ]
    finally:
        session.dispose()

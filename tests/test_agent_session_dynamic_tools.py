"""Translated from pi's ``packages/coding-agent/test/agent-session-dynamic-tools.test.ts`` (v1.1.0)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.agent.types import AgentToolResult
from misaka.ai.compat import getModel
from misaka.ai.types import TextContent
from misaka.core.extensions.types import ToolDefinition
from misaka.core.resource_loader import DefaultResourceLoader
from misaka.core.sdk import createAgentSession
from misaka.core.session_manager import NewSessionOptions, SessionManager
from misaka.core.settings_manager import SettingsManager
from misaka.core.tools.bash import createBashTool
from misaka.utils.values import read_field


@pytest.fixture
def dirs(tmp_path: Path) -> SimpleNamespace:
    agentDir = tmp_path / "agent"
    agentDir.mkdir(parents=True)
    return SimpleNamespace(tempDir=str(tmp_path), agentDir=str(agentDir))


async def _ok(*_args: Any, **_kwargs: Any) -> AgentToolResult:
    return AgentToolResult(content=[TextContent(text="ok")], details={})


def sourceInfo(tool: Any) -> dict[str, Any]:
    info = read_field(tool, "sourceInfo")
    return {key: read_field(info, key) for key in ("path", "source", "scope", "origin")}


async def test_exposes_session_state_before_custom_bash_spawn_hooks_and_supports_opting_out(dirs: SimpleNamespace):
    settingsManager = SettingsManager.create(dirs.tempDir, dirs.agentDir)
    sessionManager = SessionManager.create(
        dirs.tempDir, str(Path(dirs.agentDir) / "sessions"), NewSessionOptions(id="bash-env-test")
    )
    seen: dict[str, Any] = {}

    def sessionHook(ctx: Any) -> Any:
        seen["sessionEnv"] = ctx.env
        return ctx

    def optedOutHook(ctx: Any) -> Any:
        seen["optedOutEnv"] = ctx.env
        return ctx

    def extension(pi: Any) -> None:
        pi.registerTool(createBashTool(dirs.tempDir, {"spawnHook": sessionHook}))
        pi.registerTool(
            createBashTool(dirs.tempDir, {"exposeSessionEnvironment": False, "spawnHook": optedOutHook}).model_copy(
                update={"name": "bash_without_session_env", "label": "bash without session env"}
            )
        )

    resourceLoader = DefaultResourceLoader({
        "cwd": dirs.tempDir,
        "agentDir": dirs.agentDir,
        "settingsManager": settingsManager,
        "extensionFactories": [extension],
    })
    await resourceLoader.reload()

    model = getModel("anthropic", "claude-sonnet-4-5")
    result = await createAgentSession({
        "cwd": dirs.tempDir,
        "agentDir": dirs.agentDir,
        "model": model,
        "thinkingLevel": "high",
        "settingsManager": settingsManager,
        "sessionManager": sessionManager,
        "resourceLoader": resourceLoader,
    })
    session = result["session"]
    try:
        bashTool = next(tool for tool in session.agent.state.tools if tool.name == "bash")
        assert "You can inspect PI_* environment variables for current model and session details." in session.systemPrompt
        await bashTool.execute("bash-env", {"command": "printf ok"}, None, None)
        sessionEnv = seen["sessionEnv"]
        assert {key: sessionEnv.get(key) for key in (
            "PI_SESSION_ID", "PI_SESSION_FILE", "PI_PROVIDER", "PI_MODEL", "PI_REASONING_LEVEL"
        )} == {
            "PI_SESSION_ID": session.sessionId,
            "PI_SESSION_FILE": session.sessionFile,
            "PI_PROVIDER": model.provider,
            "PI_MODEL": model.id,
            "PI_REASONING_LEVEL": session.thinkingLevel,
        }

        optedOutBashTool = next(tool for tool in session.agent.state.tools if tool.name == "bash_without_session_env")
        await optedOutBashTool.execute("bash-no-env", {"command": "printf ok"}, None, None)
        optedOutEnv = seen["optedOutEnv"]
        for key in ("PI_SESSION_ID", "PI_SESSION_FILE", "PI_PROVIDER", "PI_MODEL", "PI_REASONING_LEVEL"):
            assert key not in optedOutEnv
    finally:
        session.dispose()


async def test_refreshes_tool_registry_when_tools_are_registered_after_initialization(dirs: SimpleNamespace):
    settingsManager = SettingsManager.create(dirs.tempDir, dirs.agentDir)
    sessionManager = SessionManager.inMemory()

    def extension(pi: Any) -> None:
        def onSessionStart(_event: Any, _ctx: Any) -> None:
            pi.registerTool(ToolDefinition(
                name="dynamic_tool",
                label="Dynamic Tool",
                description="Tool registered from session_start",
                promptSnippet="Run dynamic test behavior",
                promptGuidelines=["Use dynamic_tool when the user asks for dynamic behavior tests."],
                parameters={"type": "object", "properties": {}},
                execute=_ok,
            ))

        pi.on("session_start", onSessionStart)

    resourceLoader = DefaultResourceLoader({
        "cwd": dirs.tempDir,
        "agentDir": dirs.agentDir,
        "settingsManager": settingsManager,
        "extensionFactories": [extension],
    })
    await resourceLoader.reload()

    result = await createAgentSession({
        "cwd": dirs.tempDir,
        "agentDir": dirs.agentDir,
        "model": getModel("anthropic", "claude-sonnet-4-5"),
        "settingsManager": settingsManager,
        "sessionManager": sessionManager,
        "resourceLoader": resourceLoader,
    })
    session = result["session"]
    try:
        assert "dynamic_tool" not in [tool.name for tool in session.getAllTools()]

        await session.bindExtensions({})

        allTools = session.getAllTools()
        dynamicTool = next((tool for tool in allTools if tool.name == "dynamic_tool"), None)
        readTool = next((tool for tool in allTools if tool.name == "read"), None)

        assert "dynamic_tool" in [tool.name for tool in allTools]
        assert list(read_field(dynamicTool, "promptGuidelines")) == [
            "Use dynamic_tool when the user asks for dynamic behavior tests."
        ]
        assert sourceInfo(dynamicTool) == {
            "path": "<inline:1>",
            "source": "inline",
            "scope": "temporary",
            "origin": "top-level",
        }
        assert sourceInfo(readTool) == {
            "path": "builtin:read",
            "source": "builtin",
            "scope": "temporary",
            "origin": "top-level",
        }
        assert "dynamic_tool" in session.getActiveToolNames()
        assert "- dynamic_tool: Run dynamic test behavior" in session.systemPrompt
        assert "- Use dynamic_tool when the user asks for dynamic behavior tests." in session.systemPrompt
    finally:
        session.dispose()


async def test_returns_source_metadata_for_sdk_custom_tools(dirs: SimpleNamespace):
    settingsManager = SettingsManager.create(dirs.tempDir, dirs.agentDir)
    sessionManager = SessionManager.inMemory()
    resourceLoader = DefaultResourceLoader({
        "cwd": dirs.tempDir,
        "agentDir": dirs.agentDir,
        "settingsManager": settingsManager,
    })
    await resourceLoader.reload()

    result = await createAgentSession({
        "cwd": dirs.tempDir,
        "agentDir": dirs.agentDir,
        "model": getModel("anthropic", "claude-sonnet-4-5"),
        "settingsManager": settingsManager,
        "sessionManager": sessionManager,
        "resourceLoader": resourceLoader,
        "customTools": [
            ToolDefinition(
                name="sdk_tool",
                label="SDK Tool",
                description="Tool registered through createAgentSession",
                parameters={"type": "object", "properties": {}},
                execute=_ok,
            )
        ],
    })
    session = result["session"]
    try:
        sdkTool = next((tool for tool in session.getAllTools() if tool.name == "sdk_tool"), None)
        assert sourceInfo(sdkTool) == {
            "path": "<sdk:sdk_tool>",
            "source": "sdk",
            "scope": "temporary",
            "origin": "top-level",
        }
        assert "sdk_tool" in session.getActiveToolNames()
    finally:
        session.dispose()


async def test_keeps_custom_tools_active_but_omits_them_from_available_tools_when_prompt_snippet_is_not_provided(
    dirs: SimpleNamespace,
):
    settingsManager = SettingsManager.create(dirs.tempDir, dirs.agentDir)
    sessionManager = SessionManager.inMemory()

    def extension(pi: Any) -> None:
        def onSessionStart(_event: Any, _ctx: Any) -> None:
            pi.registerTool(ToolDefinition(
                name="hidden_tool",
                label="Hidden Tool",
                description="Description should not appear in available tools",
                parameters={"type": "object", "properties": {}},
                execute=_ok,
            ))

        pi.on("session_start", onSessionStart)

    resourceLoader = DefaultResourceLoader({
        "cwd": dirs.tempDir,
        "agentDir": dirs.agentDir,
        "settingsManager": settingsManager,
        "extensionFactories": [extension],
    })
    await resourceLoader.reload()

    result = await createAgentSession({
        "cwd": dirs.tempDir,
        "agentDir": dirs.agentDir,
        "model": getModel("anthropic", "claude-sonnet-4-5"),
        "settingsManager": settingsManager,
        "sessionManager": sessionManager,
        "resourceLoader": resourceLoader,
    })
    session = result["session"]
    try:
        await session.bindExtensions({})

        assert "hidden_tool" in [tool.name for tool in session.getAllTools()]
        assert "hidden_tool" in session.getActiveToolNames()
        assert "hidden_tool" not in session.systemPrompt
        assert "Description should not appear in available tools" not in session.systemPrompt
    finally:
        session.dispose()

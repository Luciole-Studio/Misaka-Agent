"""Translated from pi's ``packages/coding-agent/test/suite/regressions/3317-network-connection-lost-retry.test.ts`` and
``3592-no-builtin-tools-keeps-extension-tools.test.ts`` (v1.1.0)."""

from __future__ import annotations

from typing import Any

from misaka.agent.types import AgentToolResult
from misaka.ai.compat import getModel
from misaka.ai.providers.faux import fauxAssistantMessage
from misaka.ai.types import TextContent
from misaka.core.agent_session_services import (
    createAgentSessionFromServices,
    createAgentSessionServices,
)
from misaka.core.extensions.types import ToolDefinition
from misaka.core.resource_loader import DefaultResourceLoader
from misaka.core.sdk import createAgentSession
from misaka.core.session_manager import SessionManager
from misaka.core.settings_manager import SettingsManager
from misaka.utils.values import read_field
from tests.suite_harness import createHarness, getAssistantTexts

# --- issue #3317 network connection lost retry --------------------------------------------------


async def test_retries_transient_network_connection_lost_failures():
    async with createHarness(settings={"retry": {"enabled": True, "maxRetries": 3, "baseDelayMs": 1}}) as harness:
        harness.setResponses([
            fauxAssistantMessage("", {"stopReason": "error", "errorMessage": "Network connection lost."}),
            fauxAssistantMessage("recovered after reconnect"),
        ])

        await harness.session.prompt("test")

        assert harness.faux.state.callCount == 2
        assert [read_field(event, "errorMessage") for event in harness.eventsOfType("auto_retry_start")] == [
            "Network connection lost."
        ]
        assert [read_field(event, "success") for event in harness.eventsOfType("auto_retry_end")] == [True]
        assert "recovered after reconnect" in getAssistantTexts(harness)


# --- regression #3592: no-builtin-tools keeps extension tools enabled ---------------------------


def _dynamicTool(pi: Any) -> None:
    def onSessionStart(_event: Any, _ctx: Any) -> None:
        async def execute(*_args: Any, **_kwargs: Any) -> AgentToolResult:
            return AgentToolResult(content=[TextContent(text="ok")], details={})

        pi.registerTool(ToolDefinition(
            name="dynamic_tool",
            label="Dynamic Tool",
            description="Tool registered from session_start",
            promptSnippet="Run dynamic test behavior",
            parameters={"type": "object", "properties": {}},
            execute=execute,
        ))

    pi.on("session_start", onSessionStart)


async def createSession(tmp_path: Any, noTools: str | None = None, tools: list[str] | None = None) -> Any:
    tempDir, agentDir = str(tmp_path), str(tmp_path / "agent")
    (tmp_path / "agent").mkdir(exist_ok=True)
    settingsManager = SettingsManager.create(tempDir, agentDir)
    resourceLoader = DefaultResourceLoader(
        {"cwd": tempDir, "agentDir": agentDir, "settingsManager": settingsManager, "extensionFactories": [_dynamicTool]}
    )
    await resourceLoader.reload()
    result = await createAgentSession({
        "cwd": tempDir,
        "agentDir": agentDir,
        "model": getModel("anthropic", "claude-sonnet-4-5"),
        "settingsManager": settingsManager,
        "sessionManager": SessionManager.inMemory(tempDir),
        "resourceLoader": resourceLoader,
        "noTools": noTools,
        "tools": tools,
    })
    session = result["session"]
    await session.bindExtensions({})
    return session


async def test_keeps_extension_tools_active_when_built_in_defaults_are_disabled(tmp_path):
    session = await createSession(tmp_path, noTools="builtin")
    try:
        # MISAKA fork: `office` is one of MISAKA's built-in tools.
        assert sorted(tool.name for tool in session.getAllTools()) == [
            "bash",
            "dynamic_tool",
            "edit",
            "find",
            "grep",
            "ls",
            "office",
            "powershell",
            "read",
            "write",
        ]
        assert session.getActiveToolNames() == ["dynamic_tool"]
        assert "- dynamic_tool: Run dynamic test behavior" in session.systemPrompt
        assert "- read:" not in session.systemPrompt
        assert "- bash:" not in session.systemPrompt
    finally:
        session.dispose()


async def test_still_disables_all_tools_when_no_tools_is_all(tmp_path):
    session = await createSession(tmp_path, noTools="all")
    try:
        assert session.getAllTools() == []
        assert session.getActiveToolNames() == []
        assert "<tools>\n(none)\n" in session.systemPrompt
    finally:
        session.dispose()


async def test_propagates_no_tools_through_service_based_session_creation(tmp_path):
    tempDir, agentDir = str(tmp_path), str(tmp_path / "agent")
    (tmp_path / "agent").mkdir(exist_ok=True)
    settingsManager = SettingsManager.create(tempDir, agentDir)
    services = await createAgentSessionServices({"cwd": tempDir, "agentDir": agentDir, "settingsManager": settingsManager})

    result = await createAgentSessionFromServices({
        "services": services,
        "sessionManager": SessionManager.inMemory(tempDir),
        "model": getModel("anthropic", "claude-sonnet-4-5"),
        "noTools": "builtin",
    })
    session = result["session"]
    try:
        assert session.getActiveToolNames() == []
        assert "<tools>\n(none)\n" in session.systemPrompt
        assert "- read:" not in session.systemPrompt
    finally:
        session.dispose()

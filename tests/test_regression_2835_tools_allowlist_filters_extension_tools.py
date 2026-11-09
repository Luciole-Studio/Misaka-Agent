"""Translated from pi's ``packages/coding-agent/test/suite/regressions/2835-tools-allowlist-filters-extension-tools.test.ts``
(v1.1.0)."""

from __future__ import annotations

from typing import Any

from misaka.agent.types import AgentToolResult
from misaka.ai.compat import getModel
from misaka.ai.types import TextContent
from misaka.core.extensions.types import ToolDefinition
from misaka.core.resource_loader import DefaultResourceLoader
from misaka.core.sdk import createAgentSession
from misaka.core.session_manager import SessionManager
from misaka.core.settings_manager import SettingsManager


async def createSession(tmp_path: Any, allowedToolNames: list[str] | None = None) -> Any:
    tempDir = str(tmp_path)
    agentDir = str(tmp_path / "agent")
    (tmp_path / "agent").mkdir()
    settingsManager = SettingsManager.create(tempDir, agentDir)
    sessionManager = SessionManager.inMemory(tempDir)

    def extension(pi: Any) -> None:
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

    resourceLoader = DefaultResourceLoader({
        "cwd": tempDir,
        "agentDir": agentDir,
        "settingsManager": settingsManager,
        "extensionFactories": [extension],
    })
    await resourceLoader.reload()

    result = await createAgentSession({
        "cwd": tempDir,
        "agentDir": agentDir,
        "model": getModel("anthropic", "claude-sonnet-4-5"),
        "settingsManager": settingsManager,
        "sessionManager": sessionManager,
        "resourceLoader": resourceLoader,
        "tools": allowedToolNames,
    })
    session = result["session"]
    await session.bindExtensions({})
    return session


# --- regression #2835: tool allowlists filter extension tools -----------------------------------


async def test_allows_only_explicitly_listed_built_in_and_extension_tools(tmp_path):
    session = await createSession(tmp_path, ["read", "dynamic_tool"])
    try:
        assert sorted(tool.name for tool in session.getAllTools()) == ["dynamic_tool", "read"]
        assert sorted(session.getActiveToolNames()) == ["dynamic_tool", "read"]
        assert "- read: Read file contents" in session.systemPrompt
        assert "- dynamic_tool: Run dynamic test behavior" in session.systemPrompt
        assert "- bash:" not in session.systemPrompt
        assert "- edit:" not in session.systemPrompt
    finally:
        session.dispose()


async def test_disables_all_tools_when_the_allowlist_is_empty(tmp_path):
    session = await createSession(tmp_path, [])
    try:
        assert session.getAllTools() == []
        assert session.getActiveToolNames() == []
        assert "<tools>\n(none)\n" in session.systemPrompt
        assert "dynamic_tool" not in session.systemPrompt
    finally:
        session.dispose()

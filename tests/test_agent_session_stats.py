"""Translated from pi's ``packages/coding-agent/test/agent-session-stats.test.ts`` (v1.1.0).

The credentialed suites beside it (``agent-session-branching``, ``agent-session-compaction``,
``agent-session-tree-navigation``) call real models and are not translated.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from misaka.agent import Agent
from misaka.ai.auth.types import ApiKeyCredential
from misaka.ai.compat import getModel, streamSimple
from misaka.core.agent_session import AgentSession, AgentSessionConfig
from misaka.core.auth_storage import AuthStorage
from misaka.core.session_manager import SessionManager
from misaka.core.settings_manager import SettingsManager
from misaka.core.usage_totals import getUsageCostBreakdown
from misaka.utils.values import read_field
from tests.model_runtime_test_utils import createInMemoryModelRegistry, getModelRuntime
from tests.suite_harness import createTestResourceLoader

model = getModel("anthropic", "claude-sonnet-4-5")


def createUsage(totalTokens: int) -> dict[str, Any]:
    return {
        "input": totalTokens,
        "output": 0,
        "cacheRead": 0,
        "cacheWrite": 0,
        "totalTokens": totalTokens,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
    }


def createAssistantMessage(text: str, totalTokens: int, timestamp: int) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "api": model.api,
        "provider": model.provider,
        "model": model.id,
        "usage": createUsage(totalTokens),
        "stopReason": "stop",
        "timestamp": timestamp,
    }


def createUserMessage(text: str, timestamp: int) -> dict[str, Any]:
    return {"role": "user", "content": text, "timestamp": timestamp}


def createToolResultMessage(usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "toolResult",
        "toolCallId": "tool-call-1",
        "toolName": "test_tool",
        "content": [{"type": "text", "text": "tool result"}],
        "usage": usage,
        "isError": False,
        "timestamp": 1,
    }


SUMMARY_USAGE = {
    "input": 10,
    "output": 20,
    "cacheRead": 30,
    "cacheWrite": 40,
    "totalTokens": 100,
    "cost": {"input": 0.1, "output": 0.2, "cacheRead": 0.3, "cacheWrite": 0.4, "total": 1},
}


@asynccontextmanager
async def createSession() -> Any:
    settingsManager = SettingsManager.inMemory()
    sessionManager = SessionManager.inMemory()
    authStorage = AuthStorage.inMemory()

    async def apiKey(_current: Any) -> ApiKeyCredential:
        return ApiKeyCredential(type="api_key", key="test-key")

    await authStorage.modify("anthropic", apiKey)
    session = AgentSession(AgentSessionConfig(
        agent=Agent(
            getApiKey=lambda *_: "test-key",
            streamFn=streamSimple,
            initialState={
                "model": model,
                "systemPrompt": "You are a helpful assistant.",
                "tools": [],
                "thinkingLevel": "high",
            },
        ),
        sessionManager=sessionManager,
        settingsManager=settingsManager,
        cwd=os.getcwd(),
        modelRuntime=getModelRuntime(await createInMemoryModelRegistry(authStorage)),
        resourceLoader=createTestResourceLoader(),
    ))
    try:
        yield session, sessionManager
    finally:
        session.dispose()


def syncAgentMessages(session: AgentSession, sessionManager: SessionManager) -> None:
    session.agent.state.messages = sessionManager.buildSessionContext().messages


def tokens(stats: Any) -> dict[str, Any]:
    return {key: read_field(stats.tokens, key) for key in ("input", "output", "cacheRead", "cacheWrite", "total")}


async def test_exposes_the_current_context_usage_alongside_token_totals():
    async with createSession() as (session, sessionManager):
        sessionManager.appendMessage(createUserMessage("hello", 1))
        sessionManager.appendMessage(createAssistantMessage("hi", 200, 2))
        syncAgentMessages(session, sessionManager)

        stats = session.getSessionStats()
        assert stats.contextUsage == session.getContextUsage()
        assert read_field(stats.contextUsage, "tokens") == 200
        assert read_field(stats.contextUsage, "contextWindow") == model.contextWindow
        assert read_field(stats.contextUsage, "percent") == (200 / model.contextWindow) * 100


def _seedCompactedHistory(sessionManager: SessionManager) -> None:
    sessionManager.appendMessage(createUserMessage("first", 1))
    sessionManager.appendMessage(createAssistantMessage("response1", 180_000, 2))
    keptUserId = sessionManager.appendMessage(createUserMessage("second", 3))
    sessionManager.appendMessage(createAssistantMessage("response2", 195_000, 4))
    sessionManager.appendCompaction("summary", keptUserId, 195_000)
    sessionManager.appendMessage(createUserMessage("third", 5))


async def test_reports_unknown_current_context_usage_immediately_after_compaction():
    async with createSession() as (session, sessionManager):
        _seedCompactedHistory(sessionManager)
        syncAgentMessages(session, sessionManager)

        stats = session.getSessionStats()
        # Totals cover ALL entries, including history compacted away (180k + 195k).
        assert stats.tokens.input == 375_000
        assert stats.contextUsage is not None
        assert read_field(stats.contextUsage, "tokens") is None
        assert read_field(stats.contextUsage, "percent") is None


async def test_uses_post_compaction_usage_for_current_context_instead_of_stale_kept_usage():
    async with createSession() as (session, sessionManager):
        _seedCompactedHistory(sessionManager)
        sessionManager.appendMessage(createAssistantMessage("response3", 25_000, 6))
        syncAgentMessages(session, sessionManager)

        stats = session.getSessionStats()
        # Totals cover ALL entries, including history compacted away (180k + 195k + 25k).
        assert stats.tokens.input == 400_000
        assert stats.contextUsage is not None
        assert read_field(stats.contextUsage, "tokens") == 25_000
        assert read_field(stats.contextUsage, "percent") == (25_000 / model.contextWindow) * 100


async def test_includes_branch_summary_usage_in_session_totals():
    async with createSession() as (session, sessionManager):
        sessionManager.branchWithSummary(None, "summary", None, False, SUMMARY_USAGE)
        syncAgentMessages(session, sessionManager)

        stats = session.getSessionStats()
        assert tokens(stats) == {"input": 10, "output": 20, "cacheRead": 30, "cacheWrite": 40, "total": 100}
        assert stats.cost == 1


async def test_includes_compaction_usage_in_session_totals():
    async with createSession() as (session, sessionManager):
        firstKeptEntryId = sessionManager.appendMessage(createUserMessage("hello", 1))
        sessionManager.appendCompaction("summary", firstKeptEntryId, 100, None, False, SUMMARY_USAGE)
        syncAgentMessages(session, sessionManager)

        stats = session.getSessionStats()
        assert tokens(stats) == {"input": 10, "output": 20, "cacheRead": 30, "cacheWrite": 40, "total": 100}
        assert stats.cost == 1


async def test_includes_cache_warming_usage_exactly_once_without_adding_messages():
    async with createSession() as (session, sessionManager):
        sessionManager.appendUsage(
            "cache_warm",
            "anthropic",
            model.id,
            {
                "input": 2,
                "output": 1,
                "cacheRead": 97,
                "cacheWrite": 0,
                "totalTokens": 100,
                "cost": {"input": 0.001, "output": 0.002, "cacheRead": 0.007, "cacheWrite": 0, "total": 0.01},
            },
            "extension override",
        )

        entry = sessionManager.getEntries()[0]
        assert {key: read_field(entry, key) for key in ("type", "kind", "note")} == {
            "type": "usage",
            "kind": "cache_warm",
            "note": "extension override",
        }
        stats = session.getSessionStats()
        assert tokens(stats) == {"input": 2, "output": 1, "cacheRead": 97, "cacheWrite": 0, "total": 100}
        assert stats.totalMessages == 0
        assert sessionManager.buildSessionContext().messages == []
        assert [
            {"key": read_field(row, "key"), "cost": read_field(row, "cost"), "tokens": read_field(row, "tokens")}
            for row in getUsageCostBreakdown(sessionManager.getEntries())
        ] == [{"key": f"anthropic/{model.id}", "cost": 0.01, "tokens": 100}]


async def test_includes_tool_result_usage_in_session_totals():
    async with createSession() as (session, sessionManager):
        sessionManager.appendMessage(createToolResultMessage(SUMMARY_USAGE))
        syncAgentMessages(session, sessionManager)

        stats = session.getSessionStats()
        assert tokens(stats) == {"input": 10, "output": 20, "cacheRead": 30, "cacheWrite": 40, "total": 100}
        assert stats.cost == 1


def _usageWithCost(total: float) -> dict[str, Any]:
    usage = createUsage(100)
    return {**usage, "cost": {**usage["cost"], "total": total}}


def test_groups_tool_and_summary_usage_separately_from_model_attributed_usage():
    sessionManager = SessionManager.inMemory()
    rootId = sessionManager.appendMessage(createUserMessage("hello", 1))
    sessionManager.appendMessage({**createAssistantMessage("response", 100, 2), "usage": _usageWithCost(0.5)})
    sessionManager.appendMessage(createToolResultMessage(_usageWithCost(1)))
    sessionManager.appendCompaction("summary", rootId, 100, None, False, _usageWithCost(2))
    sessionManager.branchWithSummary(None, "branch summary", None, False, _usageWithCost(3))

    assert [
        {"key": read_field(row, "key"), "cost": read_field(row, "cost"), "tokens": read_field(row, "tokens")}
        for row in getUsageCostBreakdown(sessionManager.getEntries())
    ] == [
        {"key": "Tools/summaries", "cost": 6, "tokens": 300},
        {"key": f"{model.provider}/{model.id}", "cost": 0.5, "tokens": 100},
    ]


async def test_ignores_zero_usage_messages_when_checking_for_post_compaction_context_usage():
    async with createSession() as (session, sessionManager):
        _seedCompactedHistory(sessionManager)
        sessionManager.appendMessage(createAssistantMessage("response3", 25_000, 6))
        sessionManager.appendMessage(createUserMessage("continue", 7))
        sessionManager.appendMessage(createAssistantMessage("partial", 0, 8))
        syncAgentMessages(session, sessionManager)

        stats = session.getSessionStats()
        assert stats.contextUsage is not None
        assert read_field(stats.contextUsage, "tokens") is not None
        assert (read_field(stats.contextUsage, "tokens") or 0) > 25_000

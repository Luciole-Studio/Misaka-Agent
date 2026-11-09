"""Translated from pi's ``packages/coding-agent/test/agent-session-retry.test.ts`` (v1.1.0)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from misaka.agent import Agent
from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.auth.types import ApiKeyCredential
from misaka.ai.compat import getModel
from misaka.ai.types import (
    AssistantMessage,
    DoneEvent,
    ErrorEvent,
    StartEvent,
    TextContent,
)
from misaka.ai.utils.event_stream import createAssistantMessageEventStream
from misaka.core.agent_session import AgentSession, AgentSessionConfig
from misaka.core.auth_storage import AuthStorage
from misaka.core.session_manager import SessionManager
from misaka.core.settings_manager import SettingsManager
from misaka.utils.values import read_field
from tests.model_runtime_test_utils import createModelRegistry, getModelRuntime
from tests.suite_harness import createTestResourceLoader


def createAssistantMessage(text: str, **overrides: Any) -> AssistantMessage:
    return AssistantMessage.model_validate({
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "api": "anthropic-messages",
        "provider": "anthropic",
        "model": "mock",
        "usage": {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 0,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
        },
        "stopReason": "stop",
        "timestamp": int(time.time() * 1000),
        **overrides,
    })


def scriptedStreamFn(respond: Any) -> tuple[Any, dict[str, int]]:
    """A stream function that answers call `n` (1-based) with `respond(n)`: (message, error?)."""
    state = {"callCount": 0}

    def streamFn(*_: Any) -> Any:
        state["callCount"] += 1
        callCount = state["callCount"]
        stream = createAssistantMessageEventStream()

        def push() -> None:
            message, isError = respond(callCount)
            stream.push(StartEvent(partial=message))
            if isError:
                stream.push(ErrorEvent(reason="error", error=message))
            else:
                stream.push(DoneEvent(reason=message.stopReason, message=message))

        asyncio.get_running_loop().call_soon(push)
        return stream

    return streamFn, state


def failing(failCount: int, errorMessage: str = "overloaded_error", success: str = "Success") -> Any:
    def respond(callCount: int) -> tuple[AssistantMessage, bool]:
        if callCount <= failCount:
            return createAssistantMessage("", stopReason="error", errorMessage=errorMessage), True
        return createAssistantMessage(success), False

    return respond


@pytest.fixture
async def sessions():
    created: list[AgentSession] = []
    yield created
    for session in created:
        session.dispose()


async def createSession(
    sessions: list[AgentSession],
    tempDir: Path,
    streamFn: Any,
    *,
    maxRetries: int = 3,
    maxAgentDelayMs: int | None = 60000,
    baseToolsOverride: dict[str, Any] | None = None,
) -> AgentSession:
    model = getModel("anthropic", "claude-sonnet-4-5")
    agent = Agent(
        getApiKey=lambda *_: "test-key",
        initialState={"model": model, "systemPrompt": "Test", "tools": []},
        streamFn=streamFn,
    )
    sessionManager = SessionManager.inMemory()
    settingsManager = SettingsManager.create(str(tempDir), str(tempDir))
    authStorage = AuthStorage.create(str(tempDir / "auth.json"))
    modelRegistry = await createModelRegistry(authStorage, str(tempDir / "models.json"))

    async def apiKey(_current: Any) -> ApiKeyCredential:
        return ApiKeyCredential(type="api_key", key="test-key")

    await authStorage.modify("anthropic", apiKey)
    retry: dict[str, Any] = {"enabled": True, "maxRetries": maxRetries, "baseDelayMs": 1}
    if maxAgentDelayMs is not None:
        retry["maxAgentDelayMs"] = maxAgentDelayMs
    settingsManager.applyOverrides({"retry": retry})

    session = AgentSession(AgentSessionConfig(
        agent=agent,
        sessionManager=sessionManager,
        settingsManager=settingsManager,
        cwd=str(tempDir),
        modelRuntime=getModelRuntime(modelRegistry),
        resourceLoader=createTestResourceLoader(),
        baseToolsOverride=baseToolsOverride,
    ))
    sessions.append(session)
    return session


def retryEvents(session: AgentSession) -> list[str]:
    events: list[str] = []

    def onEvent(event: Any) -> None:
        if read_field(event, "type") == "auto_retry_start":
            events.append(f"start:{read_field(event, 'attempt')}")
        if read_field(event, "type") == "auto_retry_end":
            events.append(f"end:success={'true' if read_field(event, 'success') else 'false'}")

    session.subscribe(onEvent)
    return events


async def test_retries_after_a_transient_error_and_succeeds(sessions, tmp_path: Path):
    streamFn, calls = scriptedStreamFn(failing(1))
    session = await createSession(sessions, tmp_path, streamFn)
    events = retryEvents(session)

    await session.prompt("Test")

    assert calls["callCount"] == 2
    assert events == ["start:1", "end:success=true"]
    assert session.isRetrying is False


async def test_exhausts_max_retries_and_emits_failure(sessions, tmp_path: Path):
    streamFn, calls = scriptedStreamFn(failing(99))
    session = await createSession(sessions, tmp_path, streamFn, maxRetries=2)
    events = retryEvents(session)

    await session.prompt("Test")

    assert calls["callCount"] == 3
    assert "start:1" in events
    assert "start:2" in events
    assert "end:success=false" in events
    assert session.isRetrying is False


async def test_caps_agent_retry_delay(sessions, tmp_path: Path):
    # Regression for #8826.
    streamFn, _calls = scriptedStreamFn(failing(4))
    session = await createSession(sessions, tmp_path, streamFn, maxRetries=5, maxAgentDelayMs=5)
    delays: list[Any] = []
    session.subscribe(
        lambda event: delays.append(read_field(event, "delayMs")) if read_field(event, "type") == "auto_retry_start" else None
    )

    await session.prompt("Test")

    assert delays == [1, 2, 4, 5]


async def test_prompt_waits_for_retry_completion_even_when_assistant_message_end_handling_is_delayed(
    sessions, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    streamFn, calls = scriptedStreamFn(failing(1))
    session = await createSession(sessions, tmp_path, streamFn)
    original = session._emitExtensionEvent

    async def delayed(event: Any) -> None:
        if read_field(event, "type") == "message_end" and read_field(read_field(event, "message"), "role") == "assistant":
            await asyncio.sleep(0.04)
        await original(event)

    monkeypatch.setattr(session, "_emitExtensionEvent", delayed)

    await session.prompt("Test")

    assert calls["callCount"] == 2
    assert session.isRetrying is False


async def test_retries_provider_network_error_failures(sessions, tmp_path: Path):
    streamFn, calls = scriptedStreamFn(
        failing(1, "Provider finish_reason: network_error", "Recovered after retry")
    )
    session = await createSession(sessions, tmp_path, streamFn, maxAgentDelayMs=None)
    events = retryEvents(session)

    await session.prompt("Test")

    assert calls["callCount"] == 2
    assert events == ["start:1", "end:success=true"]


async def test_prompt_waits_for_full_agent_loop_when_retry_produces_tool_calls(sessions, tmp_path: Path):
    # Regression: when auto-retry fires and the retry response includes tool_use,
    # session.prompt() must wait for the entire tool loop to finish before returning.
    toolExecuted = {"value": False}

    async def execute(*_args: Any, **_kwargs: Any) -> AgentToolResult:
        toolExecuted["value"] = True
        return AgentToolResult(content=[TextContent(text="echoed")], details=None)

    echoTool = AgentTool(
        name="echo",
        label="Echo",
        description="Echo text back",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        execute=execute,
    )

    def respond(callCount: int) -> tuple[AssistantMessage, bool]:
        if callCount == 1:
            # First call: overloaded error
            return createAssistantMessage("", stopReason="error", errorMessage="overloaded_error"), True
        if callCount == 2:
            # Second call (retry): text + tool_use
            return createAssistantMessage(
                "Looking that up now.",
                stopReason="toolUse",
                content=[
                    {"type": "text", "text": "Looking that up now."},
                    {"type": "toolCall", "id": "call_1", "name": "echo", "arguments": {"text": "hello"}},
                ],
            ), False
        # Third call (after tool result): final response
        return createAssistantMessage("Final answer."), False

    streamFn, calls = scriptedStreamFn(respond)
    session = await createSession(sessions, tmp_path, streamFn, maxAgentDelayMs=None, baseToolsOverride={"echo": echoTool})

    await session.prompt("Test")

    # All three LLM calls must have completed
    assert calls["callCount"] == 3
    # Tool must have been executed
    assert toolExecuted["value"] is True
    # Agent must not be streaming after prompt returns
    assert session.isStreaming is False
    # A follow-up prompt must work (no "Agent is already processing" error)
    await session.prompt("Follow-up")
    assert calls["callCount"] == 4



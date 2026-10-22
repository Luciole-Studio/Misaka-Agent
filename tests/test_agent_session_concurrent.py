"""Translated from pi's ``packages/coding-agent/test/agent-session-concurrent.test.ts`` (v1.1.0): the AgentSession
concurrent prompt guard."""

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
from misaka.core.system_prompt import normalizeBuildSystemPromptOptions
from misaka.utils.values import read_field
from tests.model_runtime_test_utils import createModelRegistry, getModelRuntime
from tests.suite_harness import createTestExtensionsResult, createTestResourceLoader

model = getModel("anthropic", "claude-sonnet-4-5")


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


def abortableStream(options: Any) -> Any:
    """Starts a response that ends only when the request is aborted."""
    stream = createAssistantMessageEventStream()
    loop = asyncio.get_running_loop()

    def start() -> None:
        stream.push(StartEvent(partial=createAssistantMessage("")))

        def checkAbort() -> None:
            signal = read_field(options, "signal")
            if signal is not None and signal.aborted:
                stream.push(ErrorEvent(reason="aborted", error=createAssistantMessage("Aborted")))
            else:
                loop.call_later(0.005, checkAbort)

        checkAbort()

    loop.call_soon(start)
    return stream


def start(awaitable: Any) -> asyncio.Task[Any]:
    """A JavaScript async call: it runs at once, up to its first suspension."""
    return asyncio.Task(awaitable, loop=asyncio.get_running_loop(), eager_start=True)


@pytest.fixture
async def sessions():
    created: list[AgentSession] = []
    yield created
    for session in created:
        session.dispose()


async def createSession(
    sessions: list[AgentSession],
    tmp_path: Path,
    agent: Agent,
    *,
    resourceLoader: Any = None,
    baseToolsOverride: dict[str, Any] | None = None,
) -> AgentSession:
    sessionManager = SessionManager.inMemory()
    settingsManager = SettingsManager.create(str(tmp_path), str(tmp_path))
    authStorage = AuthStorage.create(str(tmp_path / "auth.json"))
    modelRegistry = await createModelRegistry(authStorage, str(tmp_path / "models.json"))

    # Set a runtime API key so validation passes
    async def apiKey(_current: Any) -> ApiKeyCredential:
        return ApiKeyCredential(type="api_key", key="test-key")

    await authStorage.modify("anthropic", apiKey)
    session = AgentSession(AgentSessionConfig(
        agent=agent,
        sessionManager=sessionManager,
        settingsManager=settingsManager,
        cwd=str(tmp_path),
        modelRuntime=getModelRuntime(modelRegistry),
        resourceLoader=resourceLoader if resourceLoader is not None else createTestResourceLoader(),
        baseToolsOverride=baseToolsOverride,
    ))
    sessions.append(session)
    return session


def abortableAgent() -> Agent:
    # Use a stream function that responds to abort
    return Agent(
        getApiKey=lambda *_: "test-key",
        initialState={"model": model, "systemPrompt": "Test", "tools": []},
        streamFn=lambda _model, _context, options=None: abortableStream(options),
    )


# A fixed sleep is not enough under full-suite load; wait until the first prompt is streaming.
async def waitForStreaming(session: AgentSession, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not session.isStreaming:
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("Timed out waiting for streaming")
        await asyncio.sleep(0.005)


async def _cleanup(session: AgentSession, firstPrompt: asyncio.Task[Any]) -> None:
    await session.abort()
    # Ignore abort error
    await asyncio.gather(firstPrompt, return_exceptions=True)


async def test_should_throw_when_prompt_called_while_streaming(sessions, tmp_path: Path):
    session = await createSession(sessions, tmp_path, abortableAgent())

    # Start first prompt (don't await, it will block until abort)
    firstPrompt = start(session.prompt("First message"))

    await waitForStreaming(session)

    # Verify we're streaming
    assert session.isStreaming is True

    # Second prompt should reject
    with pytest.raises(
        Exception,
        match=r"Agent is already processing\. Specify streamingBehavior \('steer' or 'followUp'\) to queue the message\.",
    ):
        await session.prompt("Second message")

    await _cleanup(session, firstPrompt)


async def test_should_allow_steer_while_streaming(sessions, tmp_path: Path):
    session = await createSession(sessions, tmp_path, abortableAgent())

    firstPrompt = start(session.prompt("First message"))
    await waitForStreaming(session)

    # steer should work while streaming
    assert await session.steer("Steering message") == "queued"
    assert session.pendingMessageCount == 1

    await _cleanup(session, firstPrompt)


async def test_should_allow_follow_up_while_streaming(sessions, tmp_path: Path):
    session = await createSession(sessions, tmp_path, abortableAgent())

    firstPrompt = start(session.prompt("First message"))
    await waitForStreaming(session)

    # followUp should work while streaming
    assert await session.followUp("Follow-up message") == "queued"
    assert session.pendingMessageCount == 1

    await _cleanup(session, firstPrompt)


def userTexts(context: Any) -> list[str]:
    texts: list[str] = []
    for message in context.messages:
        if read_field(message, "role") != "user":
            continue
        content = read_field(message, "content")
        if isinstance(content, str):
            texts.append(content)
        else:
            texts.append("\n".join(read_field(part, "text") for part in content if read_field(part, "type") == "text"))
    return texts


async def test_should_queue_extension_origin_steering_messages_while_streaming(sessions, tmp_path: Path):
    state: dict[str, Any] = {"sawSteeringMessage": False, "lastInputSource": None}
    queueEvents: list[dict[str, Any]] = []

    def streamFn(_model: Any, context: Any, options: Any = None) -> Any:
        if "Steer from extension" in userTexts(context):
            state["sawSteeringMessage"] = True
            stream = createAssistantMessageEventStream()

            def respond() -> None:
                stream.push(StartEvent(partial=createAssistantMessage("")))
                stream.push(DoneEvent(reason="stop", message=createAssistantMessage("Steered")))

            asyncio.get_running_loop().call_soon(respond)
            return stream
        return abortableStream(options)

    agent = Agent(
        getApiKey=lambda *_: "test-key",
        initialState={"model": model, "systemPrompt": "Test", "tools": []},
        streamFn=streamFn,
    )

    def captureApi(pi: Any) -> None:
        state["pi"] = pi

    def recordInput(pi: Any) -> None:
        async def onInput(event: Any, _ctx: Any) -> None:
            state["lastInputSource"] = read_field(event, "source")

        pi.on("input", onInput)

    extensionsResult = await createTestExtensionsResult([captureApi, recordInput], str(tmp_path))
    session = await createSession(sessions, tmp_path, agent, resourceLoader=createTestResourceLoader(extensionsResult))

    def onEvent(event: Any) -> None:
        if read_field(event, "type") == "queue_update":
            queueEvents.append({"steering": read_field(event, "steering"), "followUp": read_field(event, "followUp")})

    session.subscribe(onEvent)

    firstPrompt = start(session.prompt("First message"))
    await waitForStreaming(session)
    assert session.isStreaming is True

    pi = state.get("pi")
    assert pi is not None

    pi.sendUserMessage("Steer from extension", {"deliverAs": "steer"})
    await asyncio.sleep(0.025)

    assert session.pendingMessageCount == 1
    assert "Steer from extension" in session.getSteeringMessages()
    assert state["lastInputSource"] == "extension"
    assert any("Steer from extension" in event["steering"] for event in queueEvents)

    await _cleanup(session, firstPrompt)

    assert state["sawSteeringMessage"] is True


async def test_should_allow_prompt_after_previous_completes(sessions, tmp_path: Path):
    # Create session with a stream that completes immediately
    def streamFn(*_: Any) -> Any:
        stream = createAssistantMessageEventStream()

        def respond() -> None:
            stream.push(StartEvent(partial=createAssistantMessage("")))
            stream.push(DoneEvent(reason="stop", message=createAssistantMessage("Done")))

        asyncio.get_running_loop().call_soon(respond)
        return stream

    agent = Agent(
        getApiKey=lambda *_: "test-key",
        initialState={"model": model, "systemPrompt": "Test", "tools": []},
        streamFn=streamFn,
    )
    session = await createSession(sessions, tmp_path, agent)

    # First prompt completes
    await session.prompt("First message")

    # Should not be streaming anymore
    assert session.isStreaming is False

    # Second prompt should work
    await session.prompt("Second message")


def dummyTool() -> AgentTool:
    async def execute(_toolCallId: str, params: Any, *_: Any) -> AgentToolResult:
        q = read_field(params, "q")
        return AgentToolResult(content=[TextContent(text=f"result:{'' if q is None else q}")], details={})

    return AgentTool(
        name="dummy",
        description="Dummy tool",
        label="dummy",
        parameters={"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
        execute=execute,
    )


USAGE = {
    "input": 1,
    "output": 1,
    "cacheRead": 0,
    "cacheWrite": 0,
    "totalTokens": 2,
    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
}


def toolLoopStreamFn(firstContent: list[dict[str, Any]]) -> Any:
    async def streamFn(_model: Any, context: Any, *_: Any) -> Any:
        stream = createAssistantMessageEventStream()
        hasToolResult = any(read_field(message, "role") == "toolResult" for message in context.messages)
        if hasToolResult:
            message = createAssistantMessage("done", usage=USAGE)
        else:
            message = createAssistantMessage("", content=firstContent, usage=USAGE, stopReason="toolUse")

        def respond() -> None:
            stream.push(StartEvent(partial=message.model_copy(update={"content": []})))
            stream.push(DoneEvent(reason=message.stopReason, message=message))

        asyncio.get_running_loop().call_soon(respond)
        return stream

    return streamFn


class RunnerStub:
    """pi's test replaces the session's extension runner with an object holding only the methods it needs.

    MISAKA fork: the session reaches more runner methods than pi's does, so anything the stub does not define is
    the real runner's."""

    def __init__(self, real: Any, **overrides: Any) -> None:
        self._real = real
        self.__dict__.update(overrides)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


async def _beforeAgentStart(_trigger: Any, systemPromptOptions: Any, *_: Any) -> Any:
    return {"messages": [], "systemPromptOptions": normalizeBuildSystemPromptOptions(systemPromptOptions)}


async def _continueInput(*_: Any, **__: Any) -> Any:
    return {"action": "continue"}


async def _nothing(*_: Any, **__: Any) -> None:
    return None


async def test_should_wait_for_queued_agent_events_before_emitting_tool_call(sessions, tmp_path: Path):
    tool = dummyTool()
    agent = Agent(
        getApiKey=lambda *_: "test-key",
        initialState={"model": model, "systemPrompt": "Test", "tools": [tool]},
        streamFn=toolLoopStreamFn([
            {"type": "toolCall", "id": "toolu_1", "name": "dummy", "arguments": {"q": "x"}},
            {"type": "toolCall", "id": "toolu_2", "name": "dummy", "arguments": {"q": "y"}},
        ]),
    )
    session = await createSession(sessions, tmp_path, agent, baseToolsOverride={"dummy": tool})

    snapshots: list[list[str]] = []

    async def emitToolCall(*_: Any, **__: Any) -> None:
        snapshots.append([
            read_field(read_field(entry, "message"), "role")
            for entry in session.sessionManager.getEntries()
            if read_field(entry, "type") == "message"
        ])

    session._extensionRunner = RunnerStub(
        session._extensionRunner,
        hasHandlers=lambda eventType: eventType == "tool_call",
        emit=_nothing,
        emitMessageEnd=_nothing,
        emitToolCall=emitToolCall,
        emitInput=_continueInput,
        emitBeforeAgentStart=_beforeAgentStart,
        invalidate=lambda *_: None,
    )

    await session.prompt("hi")
    await session.agent.waitForIdle()

    assert snapshots == [["system", "user", "assistant"], ["system", "user", "assistant"]]


async def test_should_persist_message_end_events_in_order_with_slow_extension_handlers(sessions, tmp_path: Path):
    tool = dummyTool()
    agent = Agent(
        getApiKey=lambda *_: "test-key",
        initialState={"model": model, "systemPrompt": "Test", "tools": [tool]},
        streamFn=toolLoopStreamFn([
            {"type": "text", "text": "calling tool"},
            {"type": "toolCall", "id": "toolu_1", "name": "dummy", "arguments": {"q": "x"}},
        ]),
    )
    session = await createSession(sessions, tmp_path, agent, baseToolsOverride={"dummy": tool})

    async def emitMessageEnd(event: Any, *_: Any) -> None:
        if read_field(event, "type") == "message_end" and read_field(read_field(event, "message"), "role") == "assistant":
            await asyncio.sleep(0.04)

    session._extensionRunner = RunnerStub(
        session._extensionRunner,
        hasHandlers=lambda _eventType: False,
        emit=_nothing,
        emitMessageEnd=emitMessageEnd,
        emitInput=_continueInput,
        emitBeforeAgentStart=_beforeAgentStart,
        invalidate=lambda *_: None,
    )

    await session.prompt("hi")
    await session.agent.waitForIdle()
    await asyncio.sleep(0.1)

    assert [
        read_field(read_field(entry, "message"), "role")
        for entry in session.sessionManager.getEntries()
        if read_field(entry, "type") == "message"
    ] == ["system", "user", "assistant", "toolResult", "system", "assistant"]

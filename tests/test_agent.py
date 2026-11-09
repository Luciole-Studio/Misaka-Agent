"""Translated from pi's ``packages/agent/test/agent.test.ts`` (v1.1.0).

MISAKA fork: ``queueMicrotask`` is ``loop.call_soon`` (an async callback becomes a task), ``setTimeout`` is
``loop.call_later``, ``Type.Object({})`` is its JSON schema, and ``agent.continue()`` is ``agent.continue_()``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from misaka.agent import Agent, setDefaultStreamFn
from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.compat import getModel
from misaka.ai.types import (
    AssistantMessage,
    DoneEvent,
    ErrorEvent,
    StartEvent,
    SystemMessage,
    TextContent,
    ToolResultMessage,
    UserMessage,
)
from misaka.ai.utils.event_stream import createAssistantMessageEventStream
from misaka.ai.utils.transcript import getCurrentSystemMessage, toToolDeclaration
from misaka.utils.js import AbortSignal, startTask

EMPTY_OBJECT = {"type": "object", "properties": {}}


def _now() -> int:
    return int(time.time() * 1000)


def createUserMessage(text: str) -> UserMessage:
    return UserMessage(content=text, timestamp=_now())


def _usage() -> dict[str, Any]:
    return {
        "input": 0,
        "output": 0,
        "cacheRead": 0,
        "cacheWrite": 0,
        "totalTokens": 0,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
    }


def createAssistantMessage(text: str) -> AssistantMessage:
    return AssistantMessage.model_validate(
        {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "api": "openai-responses",
            "provider": "openai",
            "model": "mock",
            "usage": _usage(),
            "stopReason": "stop",
            "timestamp": _now(),
        }
    )


def createTool(name: str) -> AgentTool:
    async def execute(*_args: Any) -> AgentToolResult:
        return AgentToolResult(content=[TextContent(text=name)], details={})

    return AgentTool(name=name, label=name, description=f"{name} tool", parameters=EMPTY_OBJECT, execute=execute)


def createAssistantToolUseMessage(content: list[dict[str, Any]]) -> AssistantMessage:
    return AssistantMessage.model_validate(
        {
            "role": "assistant",
            "content": content,
            "api": "openai-responses",
            "provider": "openai",
            "model": "mock",
            "usage": _usage(),
            "stopReason": "toolUse",
            "timestamp": _now(),
        }
    )


def unusedStreamFunction(*_args: Any) -> Any:
    raise RuntimeError("Unexpected stream call")


def queueMicrotask(callback: Any) -> None:
    loop = asyncio.get_running_loop()
    if asyncio.iscoroutinefunction(callback):
        loop.call_soon(lambda: asyncio.ensure_future(callback()))
    else:
        loop.call_soon(callback)


def setTimeout(callback: Any, ms: float) -> None:
    asyncio.get_running_loop().call_later(ms / 1000, callback)


def doneStream(message: AssistantMessage, reason: str = "stop") -> Any:
    stream = createAssistantMessageEventStream()
    queueMicrotask(lambda: stream.push(DoneEvent(reason=reason, message=message)))
    return stream


def abortableStream(options: Any) -> Any:
    """A response that starts and then waits for the request's abort signal."""
    stream = createAssistantMessageEventStream()

    def start() -> None:
        stream.push(StartEvent(partial=createAssistantMessage("")))

        def checkAbort() -> None:
            signal = getattr(options, "signal", None)
            if signal is not None and signal.aborted:
                stream.push(ErrorEvent(reason="aborted", error=createAssistantMessage("Aborted")))
            else:
                setTimeout(checkAbort, 5)

        checkAbort()

    queueMicrotask(start)
    return stream


def userTexts(context: Any) -> list[str]:
    return [message.content for message in context.messages if message.role == "user" and isinstance(message.content, str)]


def dump(message: Any) -> Any:
    return message.model_dump(exclude_none=True)


async def test_uses_the_configured_default_when_a_legacy_caller_omits_streamFn():
    calls = 0

    def fallback(*_args: Any) -> Any:
        nonlocal calls
        calls += 1
        return doneStream(createAssistantMessage("fallback"))

    setDefaultStreamFn(fallback)
    try:
        agent = Agent({})
        await agent.prompt("Hello")
        assert calls == 1
    finally:
        from misaka.ai.compat import streamSimple

        # MISAKA fork: restores the fallback `misaka.core.sdk` installs, which other tests in the process rely on.
        setDefaultStreamFn(streamSimple)


def test_should_create_an_agent_instance_with_default_state():
    agent = Agent(streamFn=unusedStreamFunction)

    assert agent.state is not None
    assert agent.state.model is not None
    assert agent.state.thinkingLevel == "off"
    assert agent.state.tools == []
    assert agent.state.messages == []
    assert agent.state.isStreaming is False
    assert agent.state.streamingMessage is None
    assert agent.state.pendingToolCalls == set()
    assert agent.state.errorMessage is None


def test_should_create_an_agent_instance_with_custom_initial_state():
    customModel = getModel("openai", "gpt-4o-mini")
    agent = Agent(
        streamFn=unusedStreamFunction,
        initialState={"systemPrompt": "You are a helpful assistant.", "model": customModel, "thinkingLevel": "low"},
    )

    assert [dump(message) for message in agent.state.messages] == [
        {"role": "system", "content": "You are a helpful assistant.", "timestamp": 0}
    ]
    assert agent.state.model is customModel
    assert agent.state.thinkingLevel == "low"


def test_converts_initial_prompt_and_tools_into_transcript_state():
    tool = createTool("echo")
    agent = Agent(initialState={"systemPrompt": "You are helpful.", "tools": [tool]}, streamFn=unusedStreamFunction)

    initial = agent.state.messages[0]
    assert initial.role == "system"
    assert initial.content == "You are helpful."
    assert [value.name for value in initial.toolsAdded or []] == ["echo"]


async def test_declares_tool_loadout_changes_to_the_model_before_the_next_request():
    first = createTool("first")
    second = createTool("second")
    requests: list[list[str]] = []

    def stream(_model: Any, context: Any, *_rest: Any) -> Any:
        requests.append(
            [
                part
                for message in context.messages
                if message.role == "system"
                for part in (
                    f"+{','.join(tool.name for tool in message.toolsAdded or [])}",
                    f"-{','.join(tool.name for tool in message.toolsRemoved or [])}",
                )
            ]
        )
        return doneStream(createAssistantMessage("done"))

    agent = Agent(initialState={"systemPrompt": "You are helpful.", "tools": [first]}, streamFn=stream)

    await agent.prompt("one")
    agent.state.tools = [second]
    await agent.prompt("two")
    await agent.prompt("three")

    assert requests == [
        ["+first", "-"],
        ["+first", "-", "+second", "-first"],
        ["+first", "-", "+second", "-first"],
    ]
    update = next(message for message in agent.state.messages if message.role == "system" and message.toolsRemoved)
    dumped = dump(update)
    assert isinstance(dumped.pop("timestamp"), int)
    assert dumped == {
        "role": "system",
        "content": "",
        "toolsAdded": [{"name": "second", "description": "second tool", "parameters": EMPTY_OBJECT}],
        "toolsRemoved": [{"name": "first"}],
    }
    initial = agent.state.messages[0]
    assert initial.role == "system"
    assert "execute" not in dump(initial.toolsAdded[0])


async def test_merges_tool_changes_into_a_pending_system_message():
    tool = createTool("echo")
    tool = tool.model_copy(update={"description": "Echo input"})

    def stream(_model: Any, context: Any, *_rest: Any) -> Any:
        assert len([message for message in context.messages if message.role == "system"]) == 2
        return doneStream(createAssistantMessage("done"))

    agent = Agent(initialState={"systemPrompt": "You are helpful."}, streamFn=stream)

    agent.state.tools = [tool]
    await agent.prompt(
        [
            SystemMessage(content="", sections={"skills": "<skills>x</skills>"}, timestamp=1),
            UserMessage(content="hi", timestamp=2),
        ]
    )

    assert dump(agent.state.messages[1]) == {
        "role": "system",
        "content": "",
        "sections": {"skills": "<skills>x</skills>"},
        "toolsAdded": [{"name": "echo", "description": "Echo input", "parameters": EMPTY_OBJECT}],
        "timestamp": 1,
    }


async def test_rewrites_pending_tool_declarations_to_match_the_executable_set():
    agent = Agent(
        initialState={"systemPrompt": "You are helpful.", "tools": [createTool("first")]},
        streamFn=lambda *_args: doneStream(createAssistantMessage("done")),
    )

    # The pending message claims to add `second` and remove `first`, but the executable
    # set still has `first` and lacks `second`: the executable set wins.
    await agent.prompt(
        [
            SystemMessage.model_validate(
                {
                    "role": "system",
                    "content": "",
                    "sections": {"note": "<note>x</note>"},
                    "toolsAdded": [dump(toToolDeclaration(createTool("second")))],
                    "toolsRemoved": [{"name": "first"}],
                    "timestamp": 1,
                }
            ),
            UserMessage(content="hi", timestamp=2),
        ]
    )

    assert dump(agent.state.messages[1]) == {
        "role": "system",
        "content": "",
        "sections": {"note": "<note>x</note>"},
        "timestamp": 1,
    }
    assert [tool.name for tool in getCurrentSystemMessage(agent.state.messages).toolsAdded] == ["first"]


def test_restores_the_transcript_baseline_when_reset():
    agent = Agent(
        initialState={
            "systemPrompt": "You are helpful.",
            "tools": [createTool("echo")],
            "messages": [UserMessage(content="old", timestamp=1)],
        },
        streamFn=unusedStreamFunction,
    )

    agent.reset()

    assert len(agent.state.messages) == 1
    initial = agent.state.messages[0]
    assert initial.role == "system"
    assert initial.content == "You are helpful."
    assert [value.name for value in initial.toolsAdded] == ["echo"]


def test_should_subscribe_to_events():
    agent = Agent(streamFn=unusedStreamFunction)

    eventCount = 0

    def listener(_event: Any, _signal: Any) -> None:
        nonlocal eventCount
        eventCount += 1

    unsubscribe = agent.subscribe(listener)

    # No initial event on subscribe
    assert eventCount == 0

    # State mutators don't emit events
    agent.state.thinkingLevel = "low"
    assert eventCount == 0
    assert agent.state.thinkingLevel == "low"

    # Unsubscribe should work
    unsubscribe()
    agent.state.thinkingLevel = "high"
    assert eventCount == 0  # Should not increase


async def test_emits_full_lifecycle_events_for_thrown_run_failures():
    def explode(*_args: Any) -> Any:
        raise RuntimeError("provider exploded")

    agent = Agent(streamFn=explode)
    events: list[str] = []
    agent.subscribe(lambda event, _signal: events.append(event.type))

    await agent.prompt("hello")

    assert events == [
        "agent_start",
        "turn_start",
        "message_start",
        "message_end",
        "message_start",
        "message_end",
        "turn_end",
        "agent_end",
    ]
    lastMessage = agent.state.messages[-1]
    assert lastMessage.role == "assistant"
    assert lastMessage.stopReason == "error"
    assert lastMessage.errorMessage == "provider exploded"
    assert agent.state.errorMessage == "provider exploded"


async def test_should_await_async_subscribers_before_prompt_resolves():
    barrier = asyncio.Event()
    agent = Agent(streamFn=lambda *_args: doneStream(createAssistantMessage("ok")))

    listenerFinished = False

    async def listener(event: Any, _signal: Any) -> None:
        nonlocal listenerFinished
        if event.type == "agent_end":
            await barrier.wait()
            listenerFinished = True

    agent.subscribe(listener)

    promptResolved = False

    async def run() -> None:
        nonlocal promptResolved
        await agent.prompt("hello")
        promptResolved = True

    promptPromise = startTask(run())

    await asyncio.sleep(0.01)
    assert promptResolved is False
    assert listenerFinished is False
    assert agent.state.isStreaming is True

    barrier.set()
    await promptPromise

    assert listenerFinished is True
    assert promptResolved is True
    assert agent.state.isStreaming is False


async def test_waitForIdle_should_wait_for_async_subscribers():
    barrier = asyncio.Event()
    agent = Agent(streamFn=lambda *_args: doneStream(createAssistantMessage("ok")))

    async def listener(event: Any, _signal: Any) -> None:
        if event.type == "message_end" and event.message.role == "assistant":
            await barrier.wait()

    agent.subscribe(listener)

    promptPromise = startTask(agent.prompt("hello"))
    idleResolved = False

    async def idle() -> None:
        nonlocal idleResolved
        await agent.waitForIdle()
        idleResolved = True

    idlePromise = startTask(idle())

    await asyncio.sleep(0.01)
    assert idleResolved is False
    assert agent.state.isStreaming is True

    barrier.set()
    await asyncio.gather(promptPromise, idlePromise)

    assert idleResolved is True
    assert agent.state.isStreaming is False


async def test_should_pass_the_active_abort_signal_to_subscribers():
    receivedSignal: Any = None
    agent = Agent(streamFn=lambda _model, _context, options=None: abortableStream(options))

    def listener(event: Any, signal: Any) -> None:
        nonlocal receivedSignal
        if event.type == "agent_start":
            receivedSignal = signal

    agent.subscribe(listener)

    promptPromise = startTask(agent.prompt("hello"))
    await asyncio.sleep(0.01)

    assert receivedSignal is not None
    assert receivedSignal.aborted is False

    agent.abort()
    await promptPromise

    assert receivedSignal.aborted is True


async def test_should_ignore_tool_updates_after_the_tool_execution_settles():
    delayedUpdate: Any = None
    events: list[Any] = []
    unhandled: list[Any] = []
    loop = asyncio.get_running_loop()
    previousHandler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))

    async def execute(_toolCallId: Any, _params: Any, _signal: Any, onUpdate: Any) -> AgentToolResult:
        nonlocal delayedUpdate
        delayedUpdate = onUpdate
        onUpdate(AgentToolResult(content=[TextContent(text="running")], details={"status": "running"}))
        return AgentToolResult(content=[TextContent(text="ok")], details={"status": "done"}, terminate=True)

    tool = AgentTool(
        name="delayed_tool",
        label="Delayed Tool",
        description="Captures progress callbacks",
        parameters=EMPTY_OBJECT,
        execute=execute,
    )
    agent = Agent(
        initialState={"tools": [tool]},
        streamFn=lambda *_args: doneStream(
            createAssistantToolUseMessage([{"type": "toolCall", "id": "call-1", "name": "delayed_tool", "arguments": {}}]),
            "toolUse",
        ),
    )
    agent.subscribe(lambda event, _signal: events.append(event))

    try:
        await agent.prompt("run tool")
        eventCountAfterPrompt = len(events)

        delayedUpdate(AgentToolResult(content=[TextContent(text="late")], details={"status": "late"}))
        await asyncio.sleep(0)

        assert len([event for event in events if event.type == "tool_execution_update"]) == 1
        assert len(events) == eventCountAfterPrompt
        assert unhandled == []
    finally:
        loop.set_exception_handler(previousHandler)


async def test_should_ignore_a_settled_parallel_tool_update_while_another_tool_is_still_running():
    slowStarted = asyncio.Event()
    settledToolEnded = asyncio.Event()
    releaseSlow = asyncio.Event()
    settledToolUpdate: Any = None
    events: list[Any] = []

    async def settledExecute(_toolCallId: Any, _params: Any, _signal: Any, onUpdate: Any) -> AgentToolResult:
        nonlocal settledToolUpdate
        settledToolUpdate = onUpdate
        return AgentToolResult(content=[TextContent(text="done")], details={"status": "done"}, terminate=True)

    async def slowExecute(*_args: Any) -> AgentToolResult:
        slowStarted.set()
        await releaseSlow.wait()
        return AgentToolResult(content=[TextContent(text="done")], details={"status": "done"}, terminate=True)

    settledTool = AgentTool(
        name="settled_tool",
        label="Settled Tool",
        description="Captures progress callbacks",
        parameters=EMPTY_OBJECT,
        execute=settledExecute,
    )
    slowTool = AgentTool(
        name="slow_tool",
        label="Slow Tool",
        description="Keeps the agent run active",
        parameters=EMPTY_OBJECT,
        execute=slowExecute,
    )
    agent = Agent(
        initialState={"tools": [settledTool, slowTool]},
        streamFn=lambda *_args: doneStream(
            createAssistantToolUseMessage(
                [
                    {"type": "toolCall", "id": "call-1", "name": "settled_tool", "arguments": {}},
                    {"type": "toolCall", "id": "call-2", "name": "slow_tool", "arguments": {}},
                ]
            ),
            "toolUse",
        ),
    )

    def listener(event: Any, _signal: Any) -> None:
        events.append(event)
        if event.type == "tool_execution_end" and event.toolCallId == "call-1":
            settledToolEnded.set()

    agent.subscribe(listener)

    promptPromise = startTask(agent.prompt("run tools"))
    await asyncio.gather(slowStarted.wait(), settledToolEnded.wait())
    eventCountBeforeLateUpdate = len(events)

    settledToolUpdate(AgentToolResult(content=[TextContent(text="late")], details={"status": "late"}))
    await asyncio.sleep(0)
    assert len(events) == eventCountBeforeLateUpdate

    releaseSlow.set()
    await promptPromise
    assert len([event for event in events if event.type == "tool_execution_update"]) == 0


def test_should_update_state_with_mutators():
    agent = Agent(streamFn=unusedStreamFunction)

    # Test setModel
    newModel = getModel("google", "gemini-2.5-flash")
    agent.state.model = newModel
    assert agent.state.model is newModel

    # Test setThinkingLevel
    agent.state.thinkingLevel = "high"
    assert agent.state.thinkingLevel == "high"

    # Test setTools
    tools = [{"name": "test", "description": "test tool"}]
    agent.state.tools = tools
    assert agent.state.tools == tools
    assert agent.state.tools is not tools  # Should be a copy

    # Test replaceMessages
    messages = [UserMessage(content="Hello", timestamp=_now())]
    agent.state.messages = messages
    assert agent.state.messages == messages
    assert agent.state.messages is not messages  # Should be a copy

    # Test appendMessage
    newMessage = {"role": "assistant", "content": [{"type": "text", "text": "Hi"}]}
    agent.state.messages.append(newMessage)
    assert len(agent.state.messages) == 2
    assert agent.state.messages[1] is newMessage

    # Test clearMessages
    agent.state.messages = []
    assert agent.state.messages == []


def test_should_support_steering_message_queue():
    agent = Agent(streamFn=unusedStreamFunction)

    message = UserMessage(content="Steering message", timestamp=_now())
    agent.steer(message)

    # The message is queued but not yet in state.messages
    assert message not in agent.state.messages


def test_should_support_follow_up_message_queue():
    agent = Agent(streamFn=unusedStreamFunction)

    message = UserMessage(content="Follow-up message", timestamp=_now())
    agent.followUp(message)

    # The message is queued but not yet in state.messages
    assert message not in agent.state.messages


def test_should_handle_abort_controller():
    agent = Agent(streamFn=unusedStreamFunction)

    # Should not throw even if nothing is running
    agent.abort()


async def test_should_reject_reset_while_processing_without_corrupting_the_transcript():
    streamStarted = asyncio.Event()
    releaseResponse = asyncio.Event()

    def stream(*_args: Any) -> Any:
        response = createAssistantMessageEventStream()

        async def run() -> None:
            response.push(StartEvent(partial=createAssistantMessage("")))
            streamStarted.set()
            await releaseResponse.wait()
            response.push(DoneEvent(reason="stop", message=createAssistantMessage("Done")))

        queueMicrotask(run)
        return response

    agent = Agent(streamFn=stream)

    promptPromise = startTask(agent.prompt("Hello"))
    await streamStarted.wait()

    try:
        assert agent.state.isStreaming is True
        assert [message.role for message in agent.state.messages] == ["user"]
        with pytest.raises(Exception, match="Agent is already processing. Wait for completion before resetting."):
            agent.reset()
        assert agent.state.isStreaming is True
        assert [message.role for message in agent.state.messages] == ["user"]
    finally:
        releaseResponse.set()
        await promptPromise

    assert agent.state.isStreaming is False
    assert [message.role for message in agent.state.messages] == ["user", "assistant"]


async def test_should_throw_when_prompt_called_while_streaming():
    agent = Agent(streamFn=lambda _model, _context, options=None: abortableStream(options))

    # Start first prompt (don't await, it will block until abort)
    firstPrompt = startTask(agent.prompt("First message"))

    # Wait a tick for isStreaming to be set
    await asyncio.sleep(0.01)
    assert agent.state.isStreaming is True

    # Second prompt should reject
    with pytest.raises(
        Exception,
        match=r"Agent is already processing a prompt\. Use steer\(\) or followUp\(\) to queue messages, or wait for "
        r"completion\.",
    ):
        await agent.prompt("Second message")

    # Cleanup - abort to stop the stream
    agent.abort()
    await asyncio.gather(firstPrompt, return_exceptions=True)  # Ignore abort error


async def test_should_throw_when_continue_called_while_streaming():
    agent = Agent(streamFn=lambda _model, _context, options=None: abortableStream(options))

    # Start first prompt
    firstPrompt = startTask(agent.prompt("First message"))
    await asyncio.sleep(0.01)
    assert agent.state.isStreaming is True

    # continue() should reject
    with pytest.raises(Exception, match="Agent is already processing. Wait for completion before continuing."):
        await agent.continue_()

    # Cleanup
    agent.abort()
    await asyncio.gather(firstPrompt, return_exceptions=True)


async def test_continue_should_process_queued_follow_up_messages_after_an_assistant_turn():
    agent = Agent(streamFn=lambda *_args: doneStream(createAssistantMessage("Processed")))

    agent.state.messages = [
        UserMessage.model_validate({"role": "user", "content": [{"type": "text", "text": "Initial"}], "timestamp": _now() - 10}),
        createAssistantMessage("Initial response"),
    ]

    agent.followUp(
        UserMessage.model_validate({"role": "user", "content": [{"type": "text", "text": "Queued follow-up"}], "timestamp": _now()})
    )

    assert await agent.continue_() is None

    def isQueuedFollowUp(message: Any) -> bool:
        if message.role != "user":
            return False
        if isinstance(message.content, str):
            return message.content == "Queued follow-up"
        return any(part.type == "text" and part.text == "Queued follow-up" for part in message.content)

    assert any(isQueuedFollowUp(message) for message in agent.state.messages)
    assert agent.state.messages[-1].role == "assistant"


@pytest.mark.parametrize(("mode", "expectedRequests"), [("one-at-a-time", 2), ("all", 1)])
async def test_continue_keeps_steering_semantics_for_assistant_tail_fallback(mode, expectedRequests):
    requests: list[list[str]] = []

    def stream(_model: Any, context: Any, *_rest: Any) -> Any:
        requests.append(userTexts(context))
        return doneStream(createAssistantMessage("Processed"))

    agent = Agent(steeringMode=mode, streamFn=stream)
    agent.state.messages = [createUserMessage("Initial"), createAssistantMessage("Initial response")]
    agent.steer(createUserMessage("Steering 1"))
    agent.steer(createUserMessage("Steering 2"))

    assert await agent.continue_() is None

    assert len(requests) == expectedRequests
    assert "Steering 1" in requests[0]
    if mode == "one-at-a-time":
        assert "Steering 2" not in requests[0]
        assert "Steering 2" in requests[1]
    else:
        assert "Steering 2" in requests[0]


def _noopTool(text: str = "ok") -> AgentTool:
    async def execute(*_args: Any) -> AgentToolResult:
        return AgentToolResult(content=[TextContent(text=text)], details={})

    return AgentTool(name="noop", label="Noop", description="Noop tool", parameters=EMPTY_OBJECT, execute=execute)


def _toolThenStop(stopText: str) -> tuple[Any, list[int]]:
    requestCount = [0]

    def stream(*_args: Any) -> Any:
        requestCount[0] += 1
        if requestCount[0] == 1:
            return doneStream(
                createAssistantToolUseMessage([{"type": "toolCall", "id": "tool-1", "name": "noop", "arguments": {}}]),
                "toolUse",
            )
        return doneStream(createAssistantMessage(stopText))

    return stream, requestCount


async def test_keeps_legacy_prepareNextTurn_signal_callback_behavior():
    sawAbortSignal = False

    async def prepareNextTurn(signal: Any) -> None:
        nonlocal sawAbortSignal
        sawAbortSignal = isinstance(signal, AbortSignal)

    stream, requestCount = _toolThenStop("done")
    agent = Agent(initialState={"tools": [_noopTool()]}, prepareNextTurn=prepareNextTurn, streamFn=stream)

    await agent.prompt("start")

    assert requestCount[0] == 2
    assert sawAbortSignal is True


async def test_forwards_finishTurn_through_AgentOptions_with_the_active_abort_signal():
    sawAbortSignal = False
    callbackContextRoles: list[str] = []

    def finishTurn(context: Any, signal: Any) -> dict[str, str]:
        nonlocal sawAbortSignal, callbackContextRoles
        sawAbortSignal = isinstance(signal, AbortSignal)
        callbackContextRoles = [message.role for message in context.context.messages]
        return {"action": "end"}

    stream, requestCount = _toolThenStop("should not run")
    agent = Agent(initialState={"tools": [_noopTool("tool complete")]}, finishTurn=finishTurn, streamFn=stream)

    await agent.prompt("start")

    assert requestCount[0] == 1
    assert sawAbortSignal is True
    assert callbackContextRoles == ["system", "user", "assistant", "toolResult"]


@pytest.mark.parametrize(
    "messages",
    [[], [SystemMessage(content="system only", timestamp=1)]],
    ids=["empty", "system-only"],
)
async def test_rejects_a_queued_continuation_without_draining_queues(messages):
    agent = Agent(initialState={"messages": messages}, streamFn=unusedStreamFunction)
    steering = createUserMessage("steering")
    followUp = createUserMessage("follow-up")
    agent.steer(steering)
    agent.followUp(followUp)

    with pytest.raises(Exception, match="No messages to continue from"):
        await agent.continue_()
    assert agent.peekQueuedMessages() == [steering]
    agent.clearSteeringQueue()
    assert agent.peekQueuedMessages() == [followUp]


@pytest.mark.parametrize(
    "messages",
    [
        [createUserMessage("existing user")],
        [
            createUserMessage("existing user"),
            createAssistantToolUseMessage([{"type": "toolCall", "id": "call-1", "name": "noop", "arguments": {}}]),
            ToolResultMessage.model_validate(
                {
                    "role": "toolResult",
                    "toolCallId": "call-1",
                    "toolName": "noop",
                    "content": [{"type": "text", "text": "done"}],
                    "isError": False,
                    "timestamp": 1,
                }
            ),
        ],
    ],
    ids=["user", "toolResult"],
)
async def test_defers_follow_up_input_on_the_first_continuation_request(messages):
    requests: list[list[str]] = []

    def stream(_model: Any, context: Any, *_rest: Any) -> Any:
        requests.append(userTexts(context))
        return doneStream(createAssistantMessage("done"))

    agent = Agent(initialState={"messages": messages}, streamFn=stream)
    agent.followUp(createUserMessage("follow-up"))

    await agent.continue_()

    assert len(requests) == 2
    assert "follow-up" not in requests[0]
    assert "follow-up" in requests[1]


@pytest.mark.parametrize(("mode", "expectedRequests"), [("one-at-a-time", 2), ("all", 1)])
async def test_polls_steering_at_continuation_startup(mode, expectedRequests):
    requests: list[list[str]] = []

    def stream(_model: Any, context: Any, *_rest: Any) -> Any:
        requests.append(userTexts(context))
        return doneStream(createAssistantMessage("done"))

    agent = Agent(initialState={"messages": [createUserMessage("existing")]}, steeringMode=mode, streamFn=stream)
    agent.steer(createUserMessage("first"))
    agent.steer(createUserMessage("second"))

    await agent.continue_()

    assert len(requests) == expectedRequests
    assert "first" in requests[0]
    if mode == "one-at-a-time":
        assert "second" not in requests[0]
        assert "second" in requests[1]
    else:
        assert "second" in requests[0]


async def test_keeps_steering_ahead_of_follow_up_from_a_non_assistant_continuation_tail():
    requests: list[list[str]] = []

    def stream(_model: Any, context: Any, *_rest: Any) -> Any:
        requests.append(userTexts(context))
        return doneStream(createAssistantMessage("done"))

    agent = Agent(initialState={"messages": [createUserMessage("existing")]}, streamFn=stream)
    agent.steer(createUserMessage("steering"))
    agent.followUp(createUserMessage("follow-up"))

    await agent.continue_()

    assert len(requests) == 2
    assert "steering" in requests[0]
    assert "follow-up" not in requests[0]
    assert "follow-up" in requests[1]


@pytest.mark.parametrize("stopReason", ["error", "aborted"])
async def test_keeps_queues_on_an_error_response_even_when_finishTurn_requests_continuation(stopReason):
    queuedDuringResponse = createUserMessage("steering")
    followUp = createUserMessage("follow-up")

    def stream(*_args: Any) -> Any:
        response = createAssistantMessageEventStream()
        error = createAssistantMessage(stopReason).model_copy(update={"stopReason": stopReason, "errorMessage": stopReason})
        queueMicrotask(lambda: response.push(ErrorEvent(reason=stopReason, error=error)))
        return response

    agent = Agent(finishTurn=lambda *_args: {"action": "continue"}, streamFn=stream)
    agent.followUp(followUp)

    def listener(event: Any, _signal: Any) -> None:
        if event.type == "message_end" and event.message.role == "assistant":
            agent.steer(queuedDuringResponse)

    agent.subscribe(listener)

    await agent.prompt("start")

    assert agent.peekQueuedMessages() == [queuedDuringResponse]
    agent.clearSteeringQueue()
    assert agent.peekQueuedMessages() == [followUp]


async def test_keeps_queues_when_finishTurn_ends_the_run():
    queuedDuringResponse = createUserMessage("steering")
    followUp = createUserMessage("follow-up")
    agent = Agent(
        finishTurn=lambda *_args: {"action": "end"},
        streamFn=lambda *_args: doneStream(createAssistantMessage("done")),
    )
    agent.followUp(followUp)

    def listener(event: Any, _signal: Any) -> None:
        if event.type == "message_end" and event.message.role == "assistant":
            agent.steer(queuedDuringResponse)

    agent.subscribe(listener)

    await agent.prompt("start")

    assert agent.peekQueuedMessages() == [queuedDuringResponse]
    agent.clearSteeringQueue()
    assert agent.peekQueuedMessages() == [followUp]


def test_previews_the_next_selected_queued_messages_without_consuming_them():
    agent = Agent(
        steeringMode="one-at-a-time",
        followUpMode="all",
        streamFn=lambda *_args: createAssistantMessageEventStream(),
    )
    first = createUserMessage("first steering")
    second = createUserMessage("second steering")
    followUp = createUserMessage("follow-up")
    agent.steer(first)
    agent.steer(second)
    agent.followUp(followUp)

    assert agent.peekQueuedMessages() == [first]
    assert agent.peekQueuedMessages() == [first]
    agent.clearSteeringQueue()
    assert agent.peekQueuedMessages() == [followUp]


async def test_forwards_provider_stream_event_observers_through_AgentOptions():
    providerEvents: list[Any] = []

    def stream(model: Any, _context: Any, options: Any = None) -> Any:
        response = createAssistantMessageEventStream()

        async def run() -> None:
            observer = getattr(options, "onProviderStreamEvent", None)
            if observer is not None:
                result = observer({"request_cost": 0.01}, model)
                if asyncio.iscoroutine(result):
                    await result
            response.push(DoneEvent(reason="stop", message=createAssistantMessage("ok")))

        queueMicrotask(run)
        return response

    agent = Agent(onProviderStreamEvent=lambda data, *_rest: providerEvents.append(data), streamFn=stream)

    await agent.prompt("hello")

    assert providerEvents == [{"request_cost": 0.01}]


async def test_forwards_sessionId_to_streamFunction_options():
    receivedSessionId: Any = None

    def stream(_model: Any, _context: Any, options: Any = None) -> Any:
        nonlocal receivedSessionId
        receivedSessionId = getattr(options, "sessionId", None)
        return doneStream(createAssistantMessage("ok"))

    agent = Agent(sessionId="session-abc", streamFn=stream)

    await agent.prompt("hello")
    assert receivedSessionId == "session-abc"

    # Test setter
    agent.sessionId = "session-def"
    assert agent.sessionId == "session-def"

    await agent.prompt("hello again")
    assert receivedSessionId == "session-def"

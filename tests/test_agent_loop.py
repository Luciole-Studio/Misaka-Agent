"""Translated from pi's ``packages/agent/test/agent-loop.test.ts`` (v1.1.0).

MISAKA fork: ``queueMicrotask`` is ``loop.call_soon``, ``setTimeout`` is ``loop.call_later``, TypeBox schemas are
their JSON schemas, and ``toMatchObject`` checks become field asserts.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from misaka.agent import setDefaultStreamFn
from misaka.agent.agent_loop import (
    RunToolCallOptions,
    agentLoop,
    agentLoopContinue,
    runAgentLoop,
    runToolCall,
)
from misaka.agent.types import AgentContext, AgentLoopConfig, AgentTool, AgentToolResult
from misaka.ai.types import (
    AssistantMessage,
    DoneEvent,
    ErrorEvent,
    Model,
    SystemMessage,
    TextContent,
    ToolCall,
    Usage,
    UserMessage,
)
from misaka.ai.utils.event_stream import createAssistantMessageEventStream


def createUsage() -> dict[str, Any]:
    return {
        "input": 0,
        "output": 0,
        "cacheRead": 0,
        "cacheWrite": 0,
        "totalTokens": 0,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
    }


def createModel() -> Model:
    return Model.model_validate(
        {
            "id": "mock",
            "name": "mock",
            "api": "openai-responses",
            "provider": "openai",
            "baseUrl": "https://example.invalid",
            "reasoning": False,
            "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 8192,
            "maxTokens": 2048,
        }
    )


def createAssistantMessage(content: list[dict[str, Any]], stopReason: str = "stop") -> AssistantMessage:
    return AssistantMessage.model_validate(
        {
            "role": "assistant",
            "content": content,
            "api": "openai-responses",
            "provider": "openai",
            "model": "mock",
            "usage": createUsage(),
            "stopReason": stopReason,
            "timestamp": int(time.time() * 1000),
        }
    )


def createUserMessage(text: str) -> UserMessage:
    return UserMessage(content=text, timestamp=int(time.time() * 1000))


# Simple identity converter for tests - just passes through standard messages
def identityConverter(messages: list[Any]) -> list[Any]:
    return [m for m in messages if m.role in ("system", "user", "assistant", "toolResult")]


def queueMicrotask(callback: Any) -> None:
    asyncio.get_running_loop().call_soon(callback)


def setTimeout(callback: Any, ms: float) -> None:
    asyncio.get_running_loop().call_later(ms / 1000, callback)


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


def toolCall(id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"type": "toolCall", "id": id, "name": name, "arguments": arguments}


def respond(message: AssistantMessage, reason: str | None = None) -> Any:
    stream = createAssistantMessageEventStream()
    queueMicrotask(lambda: stream.push(DoneEvent(reason=reason or message.stopReason, message=message)))
    return stream


def valueSchema() -> dict[str, Any]:
    return {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}


EMPTY_OBJECT = {"type": "object", "properties": {}}


def config(**fields: Any) -> AgentLoopConfig:
    return AgentLoopConfig(model=fields.pop("model", createModel()), convertToLlm=fields.pop("convertToLlm", identityConverter), **fields)


def echoTool(execute: Any, **fields: Any) -> AgentTool:
    return AgentTool(name="echo", label="Echo", description="Echo tool", parameters=valueSchema(), execute=execute, **fields)


def toolThenDone(calls: list[dict[str, Any]]) -> tuple[Any, list[int]]:
    """A stream function answering the first request with `calls` and the next with "done"."""
    callIndex = [0]

    def streamFn(*_args: Any) -> Any:
        stream = createAssistantMessageEventStream()

        def push() -> None:
            if callIndex[0] == 0:
                stream.push(DoneEvent(reason="toolUse", message=createAssistantMessage(calls, "toolUse")))
            else:
                stream.push(DoneEvent(reason="stop", message=createAssistantMessage([text("done")])))
            callIndex[0] += 1

        queueMicrotask(push)
        return stream

    return streamFn, callIndex


async def drain(stream: Any) -> list[Any]:
    return [event async for event in stream]


# --- default stream function compatibility ----------------------------------------------------


async def test_uses_the_configured_default_when_a_legacy_caller_omits_streamFn():
    calls = 0

    def fallback(*_args: Any) -> Any:
        nonlocal calls
        calls += 1
        return respond(createAssistantMessage([text("fallback")]))

    setDefaultStreamFn(fallback)
    try:
        context = AgentContext(messages=[], tools=[])
        stream = agentLoop([createUserMessage("Hello")], context, config(), None, None)

        await stream.result()
        assert calls == 1
    finally:
        from misaka.ai.compat import streamSimple

        # MISAKA fork: restores the fallback `misaka.core.sdk` installs, which other tests in the process rely on.
        setDefaultStreamFn(streamSimple)


# --- agentLoop with AgentMessage ---------------------------------------------------------------


async def test_should_emit_events_with_AgentMessage_types():
    context = AgentContext(messages=[], tools=[])
    userPrompt = createUserMessage("Hello")

    def streamFn(*_args: Any) -> Any:
        return respond(createAssistantMessage([text("Hi there!")]))

    stream = agentLoop([userPrompt], context, config(), None, streamFn)
    events = await drain(stream)
    messages = await stream.result()

    # Should have user message and assistant message
    assert len(messages) == 2
    assert messages[0].role == "user"
    assert messages[1].role == "assistant"

    # Verify event sequence
    eventTypes = [e.type for e in events]
    for expected in ("agent_start", "turn_start", "message_start", "message_end", "turn_end", "agent_end"):
        assert expected in eventTypes


async def test_should_build_provider_context_exclusively_from_transcript_messages():
    initialSystem = SystemMessage(content="Transcript prompt", toolsAdded=[], timestamp=1)
    context = AgentContext(messages=[], tools=[])

    def streamFn(_model: Any, providerContext: Any, *_rest: Any) -> Any:
        # The provider receives a transcript: no top-level prompt or tool fields.
        assert list(providerContext.model_dump(exclude_none=True)) == ["messages"]
        assert providerContext.messages[0] is initialSystem
        return respond(createAssistantMessage([text("done")]))

    stream = agentLoop([initialSystem, createUserMessage("Hello")], context, config(), None, streamFn)

    await stream.result()


async def test_should_handle_custom_message_types_via_convertToLlm():
    # Create a custom message type
    notification = {"role": "notification", "text": "This is a notification", "timestamp": int(time.time() * 1000)}

    context = AgentContext(messages=[notification], tools=[])  # Custom message in context
    userPrompt = createUserMessage("Hello")

    convertedMessages: list[Any] = []

    def convertToLlm(messages: list[Any]) -> list[Any]:
        nonlocal convertedMessages
        # Filter out notifications, convert rest
        convertedMessages = [
            m
            for m in messages
            if _role(m) != "notification" and _role(m) in ("user", "assistant", "toolResult")
        ]
        return convertedMessages

    def streamFn(*_args: Any) -> Any:
        return respond(createAssistantMessage([text("Response")]))

    stream = agentLoop([userPrompt], context, config(convertToLlm=convertToLlm), None, streamFn)
    await drain(stream)

    # The notification should have been filtered out in convertToLlm
    assert len(convertedMessages) == 1  # Only user message
    assert convertedMessages[0].role == "user"


def _role(message: Any) -> Any:
    return message.get("role") if isinstance(message, dict) else message.role


async def test_should_apply_transformContext_before_convertToLlm():
    context = AgentContext(
        messages=[
            createUserMessage("old message 1"),
            createAssistantMessage([text("old response 1")]),
            createUserMessage("old message 2"),
            createAssistantMessage([text("old response 2")]),
        ],
        tools=[],
    )
    userPrompt = createUserMessage("new message")

    transformedMessages: list[Any] = []
    convertedMessages: list[Any] = []

    async def transformContext(messages: list[Any], _signal: Any = None) -> list[Any]:
        nonlocal transformedMessages
        # Keep only last 2 messages (prune old ones)
        transformedMessages = messages[-2:]
        return transformedMessages

    def convertToLlm(messages: list[Any]) -> list[Any]:
        nonlocal convertedMessages
        convertedMessages = [m for m in messages if m.role in ("user", "assistant", "toolResult")]
        return convertedMessages

    def streamFn(*_args: Any) -> Any:
        return respond(createAssistantMessage([text("Response")]))

    stream = agentLoop(
        [userPrompt], context, config(transformContext=transformContext, convertToLlm=convertToLlm), None, streamFn
    )
    await drain(stream)

    # transformContext should have been called first, keeping only last 2
    assert len(transformedMessages) == 2
    # Then convertToLlm receives the pruned messages
    assert len(convertedMessages) == 2


async def test_should_handle_tool_calls_and_results():
    executed: list[str] = []
    toolUsage = Usage.model_validate(
        {
            "input": 1,
            "output": 2,
            "cacheRead": 3,
            "cacheWrite": 4,
            "totalTokens": 10,
            "cost": {"input": 0.1, "output": 0.2, "cacheRead": 0.3, "cacheWrite": 0.4, "total": 1},
        }
    )
    patchedToolUsage = Usage.model_validate(
        {
            "input": 5,
            "output": 6,
            "cacheRead": 7,
            "cacheWrite": 8,
            "totalTokens": 26,
            "cost": {"input": 0.5, "output": 0.6, "cacheRead": 0.7, "cacheWrite": 0.8, "total": 2.6},
        }
    )
    observedToolUsage: Any = None

    async def execute(_toolCallId: str, params: dict[str, Any], *_rest: Any) -> AgentToolResult:
        executed.append(params["value"])
        return AgentToolResult(
            content=[TextContent(text=f"echoed: {params['value']}")], details={"value": params["value"]}, usage=toolUsage
        )

    context = AgentContext(messages=[], tools=[echoTool(execute)])
    userPrompt = createUserMessage("echo something")

    async def afterToolCall(context: Any, _signal: Any = None) -> dict[str, Any]:
        nonlocal observedToolUsage
        observedToolUsage = context.result.usage
        return {"usage": patchedToolUsage}

    streamFn, _ = toolThenDone([toolCall("tool-1", "echo", {"value": "hello"})])
    stream = agentLoop([userPrompt], context, config(afterToolCall=afterToolCall), None, streamFn)
    events = await drain(stream)

    # Tool should have been executed
    assert executed == ["hello"]

    # Should have tool execution events
    toolStart = next((e for e in events if e.type == "tool_execution_start"), None)
    toolEnd = next((e for e in events if e.type == "tool_execution_end"), None)
    assert toolStart is not None
    assert toolEnd is not None
    assert toolEnd.isError is False
    assert observedToolUsage == toolUsage
    messages = await stream.result()
    toolResult = next(message for message in messages if message.role == "toolResult")
    assert toolResult.usage == patchedToolUsage


# #10549
async def test_records_how_long_execute_took_on_the_tool_result_excluding_hooks():
    async def execute(_toolCallId: str, params: dict[str, Any], *_rest: Any) -> AgentToolResult:
        await asyncio.sleep(0.03)
        return AgentToolResult(content=[TextContent(text=params["value"])], details={"value": params["value"]})

    async def beforeToolCall(context: Any, _signal: Any = None) -> Any:
        await asyncio.sleep(0.1)
        return {"block": True, "reason": "no"} if context.toolCall.id == "blocked" else None

    streamFn, _ = toolThenDone(
        [toolCall("ran", "echo", {"value": "a"}), toolCall("blocked", "echo", {"value": "b"})]
    )
    stream = agentLoop(
        [createUserMessage("go")],
        AgentContext(messages=[], tools=[echoTool(execute)]),
        config(beforeToolCall=beforeToolCall),
        None,
        streamFn,
    )
    await drain(stream)
    results = [message for message in await stream.result() if message.role == "toolResult"]
    ran, blocked = results
    assert ran.durationMs >= 25
    assert ran.durationMs < 100
    assert blocked.isError is True
    assert "durationMs" not in blocked.model_dump(exclude_none=True)


async def test_should_not_execute_tool_calls_from_a_length_truncated_assistant_message():
    executed: list[str] = []

    async def execute(_toolCallId: str, params: dict[str, Any], *_rest: Any) -> AgentToolResult:
        executed.append(params["value"])
        return AgentToolResult(content=[TextContent(text=f"echoed: {params['value']}")], details={"value": params["value"]})

    context = AgentContext(messages=[], tools=[echoTool(execute)])

    callIndex = 0

    def streamFn(*_args: Any) -> Any:
        stream = createAssistantMessageEventStream()

        def push() -> None:
            nonlocal callIndex
            if callIndex == 0:
                # Output hit the token limit mid tool call. The salvage parser can
                # produce arguments that validate but are silently truncated, so
                # nothing in this message may execute.
                message = createAssistantMessage([toolCall("tool-1", "echo", {"value": "hel"})], "length")
                stream.push(DoneEvent(reason="length", message=message))
            else:
                stream.push(DoneEvent(reason="stop", message=createAssistantMessage([text("done")])))
            callIndex += 1

        queueMicrotask(push)
        return stream

    stream = agentLoop([createUserMessage("echo something")], context, config(), None, streamFn)
    events = await drain(stream)

    # The tool must never execute with potentially truncated arguments.
    assert executed == []

    toolEnd = next((e for e in events if e.type == "tool_execution_end"), None)
    assert toolEnd is not None
    assert toolEnd.isError is True
    textBlock = next(c for c in toolEnd.result.content if c.type == "text")
    assert "output token limit" in textBlock.text

    # The loop continues so the model can re-issue the tool call.
    assert callIndex == 2
    messages = await stream.result()
    assert messages[-1].role == "assistant"


async def test_should_execute_mutated_beforeToolCall_args_without_revalidation():
    executed: list[Any] = []

    async def execute(_toolCallId: str, params: dict[str, Any], *_rest: Any) -> AgentToolResult:
        executed.append(params["value"])
        return AgentToolResult(content=[TextContent(text=f"echoed: {params['value']}")], details={"value": params["value"]})

    async def beforeToolCall(context: Any, _signal: Any = None) -> None:
        context.args["value"] = 123

    streamFn, _ = toolThenDone([toolCall("tool-1", "echo", {"value": "hello"})])
    stream = agentLoop(
        [createUserMessage("echo something")],
        AgentContext(messages=[], tools=[echoTool(execute)]),
        config(beforeToolCall=beforeToolCall),
        None,
        streamFn,
    )
    await drain(stream)

    assert executed == [123]


async def test_should_prepare_tool_arguments_for_validation():
    replaceSchema = {
        "type": "object",
        "properties": {"oldText": {"type": "string"}, "newText": {"type": "string"}},
        "required": ["oldText", "newText"],
    }
    toolSchema = {"type": "object", "properties": {"edits": {"type": "array", "items": replaceSchema}}, "required": ["edits"]}
    executed: list[Any] = []

    def prepareArguments(args: Any) -> Any:
        if not args or not isinstance(args, dict):
            return args
        if not isinstance(args.get("oldText"), str) or not isinstance(args.get("newText"), str):
            return args
        return {"edits": [*(args.get("edits") or []), {"oldText": args["oldText"], "newText": args["newText"]}]}

    async def execute(_toolCallId: str, params: dict[str, Any], *_rest: Any) -> AgentToolResult:
        executed.append(params["edits"])
        return AgentToolResult(
            content=[TextContent(text=f"edited {len(params['edits'])}")], details={"count": len(params["edits"])}
        )

    tool = AgentTool(
        name="edit", label="Edit", description="Edit tool", parameters=toolSchema, prepareArguments=prepareArguments,
        execute=execute,
    )
    streamFn, _ = toolThenDone([toolCall("tool-1", "edit", {"oldText": "before", "newText": "after"})])
    stream = agentLoop([createUserMessage("edit something")], AgentContext(messages=[], tools=[tool]), config(), None, streamFn)
    await drain(stream)

    assert executed == [[{"oldText": "before", "newText": "after"}]]


def _firstSecondTool(name: str, executionMode: str | None, state: dict[str, Any], firstDone: asyncio.Event) -> AgentTool:
    async def execute(_toolCallId: str, params: dict[str, Any], *_rest: Any) -> AgentToolResult:
        if params["value"] == "first":
            await firstDone.wait()
            state["firstResolved"] = True
        if params["value"] == "second" and not state["firstResolved"]:
            state["parallelObserved"] = True
        return AgentToolResult(content=[TextContent(text=f"echoed: {params['value']}")], details={"value": params["value"]})

    return AgentTool(
        name=name, label=name.title(), description=f"{name.title()} tool", parameters=valueSchema(), execute=execute,
        executionMode=executionMode,
    )


def _releasingStream(calls: list[dict[str, Any]], release: asyncio.Event) -> Any:
    callIndex = [0]

    def streamFn(*_args: Any) -> Any:
        mockStream = createAssistantMessageEventStream()

        def push() -> None:
            if callIndex[0] == 0:
                mockStream.push(DoneEvent(reason="toolUse", message=createAssistantMessage(calls, "toolUse")))
                setTimeout(release.set, 20)
            else:
                mockStream.push(DoneEvent(reason="stop", message=createAssistantMessage([text("done")])))
            callIndex[0] += 1

        queueMicrotask(push)
        return mockStream

    return streamFn


async def test_should_emit_tool_execution_end_in_completion_order_but_persist_tool_results_in_source_order():
    state = {"firstResolved": False, "parallelObserved": False}
    firstDone = asyncio.Event()
    tool = _firstSecondTool("echo", None, state, firstDone)

    stream = agentLoop(
        [createUserMessage("echo both")],
        AgentContext(messages=[], tools=[tool]),
        config(toolExecution="parallel"),
        None,
        _releasingStream(
            [toolCall("tool-1", "echo", {"value": "first"}), toolCall("tool-2", "echo", {"value": "second"})], firstDone
        ),
    )
    events = await drain(stream)

    toolExecutionEndIds = [event.toolCallId for event in events if event.type == "tool_execution_end"]
    toolResultIds = [
        event.message.toolCallId for event in events if event.type == "message_end" and event.message.role == "toolResult"
    ]
    turnToolResultIds = [
        toolResult.toolCallId for event in events if event.type == "turn_end" for toolResult in event.toolResults
    ]

    assert state["parallelObserved"] is True
    assert toolExecutionEndIds == ["tool-2", "tool-1"]
    assert toolResultIds == ["tool-1", "tool-2"]
    assert turnToolResultIds == ["tool-1", "tool-2"]


async def test_should_inject_queued_messages_after_all_tool_calls_complete():
    executed: list[str] = []

    async def execute(_toolCallId: str, params: dict[str, Any], *_rest: Any) -> AgentToolResult:
        executed.append(params["value"])
        return AgentToolResult(content=[TextContent(text=f"ok:{params['value']}")], details={"value": params["value"]})

    context = AgentContext(messages=[], tools=[echoTool(execute)])
    userPrompt = createUserMessage("start")
    queuedUserMessage = createUserMessage("interrupt")

    queuedDelivered = False
    callIndex = 0
    sawInterruptInContext = False

    async def getSteeringMessages() -> list[Any]:
        nonlocal queuedDelivered
        # Return steering message after tool execution has started.
        if len(executed) >= 1 and not queuedDelivered:
            queuedDelivered = True
            return [queuedUserMessage]
        return []

    def streamFn(_model: Any, ctx: Any, _options: Any = None) -> Any:
        nonlocal sawInterruptInContext
        # Check if interrupt message is in context on second call
        if callIndex == 1:
            sawInterruptInContext = any(m.role == "user" and m.content == "interrupt" for m in ctx.messages)

        mockStream = createAssistantMessageEventStream()

        def push() -> None:
            nonlocal callIndex
            if callIndex == 0:
                # First call: return two tool calls
                message = createAssistantMessage(
                    [toolCall("tool-1", "echo", {"value": "first"}), toolCall("tool-2", "echo", {"value": "second"})],
                    "toolUse",
                )
                mockStream.push(DoneEvent(reason="toolUse", message=message))
            else:
                # Second call: return final response
                mockStream.push(DoneEvent(reason="stop", message=createAssistantMessage([text("done")])))
            callIndex += 1

        queueMicrotask(push)
        return mockStream

    stream = agentLoop(
        [userPrompt],
        context,
        config(toolExecution="sequential", getSteeringMessages=getSteeringMessages),
        None,
        streamFn,
    )
    events = await drain(stream)

    # Both tools should execute before steering is injected
    assert executed == ["first", "second"]

    toolEnds = [e for e in events if e.type == "tool_execution_end"]
    assert len(toolEnds) == 2
    assert toolEnds[0].isError is False
    assert toolEnds[1].isError is False

    # Queued message should appear in events after both tool result messages
    eventSequence: list[str] = []
    for event in events:
        if event.type != "message_start":
            continue
        if event.message.role == "toolResult":
            eventSequence.append(f"tool:{event.message.toolCallId}")
        elif event.message.role == "user" and isinstance(event.message.content, str):
            eventSequence.append(event.message.content)
    assert "interrupt" in eventSequence
    assert eventSequence.index("tool:tool-1") < eventSequence.index("interrupt")
    assert eventSequence.index("tool:tool-2") < eventSequence.index("interrupt")

    # Interrupt message should be in context when second LLM call is made
    assert sawInterruptInContext is True


async def test_should_force_sequential_execution_when_a_tool_has_executionMode_sequential_even_with_default_parallel():
    state = {"firstResolved": False, "parallelObserved": False}
    firstDone = asyncio.Event()
    slowTool = _firstSecondTool("slow", "sequential", state, firstDone)

    # config is parallel (default), but tool forces sequential
    stream = agentLoop(
        [createUserMessage("run both")],
        AgentContext(messages=[], tools=[slowTool]),
        config(),
        None,
        _releasingStream(
            [toolCall("tool-1", "slow", {"value": "first"}), toolCall("tool-2", "slow", {"value": "second"})], firstDone
        ),
    )
    events = await drain(stream)

    # With sequential execution, second tool should NOT start before first finishes
    assert state["parallelObserved"] is False

    toolResultIds = [
        event.message.toolCallId for event in events if event.type == "message_end" and event.message.role == "toolResult"
    ]
    assert toolResultIds == ["tool-1", "tool-2"]


async def test_should_force_sequential_execution_when_one_of_multiple_tools_has_executionMode_sequential():
    executionOrder: list[str] = []
    slowDone = asyncio.Event()

    async def slowExecute(_toolCallId: str, params: dict[str, Any], *_rest: Any) -> AgentToolResult:
        executionOrder.append(f"slow:{params['value']}")
        if params["value"] == "a":
            await slowDone.wait()
        return AgentToolResult(content=[TextContent(text=f"slow: {params['value']}")], details={"value": params["value"]})

    async def fastExecute(_toolCallId: str, params: dict[str, Any], *_rest: Any) -> AgentToolResult:
        executionOrder.append(f"fast:{params['value']}")
        return AgentToolResult(content=[TextContent(text=f"fast: {params['value']}")], details={"value": params["value"]})

    slowTool = AgentTool(
        name="slow", label="Slow", description="Slow tool", parameters=valueSchema(), executionMode="sequential",
        execute=slowExecute,
    )
    # no executionMode = defaults to parallel
    fastTool = AgentTool(name="fast", label="Fast", description="Fast tool", parameters=valueSchema(), execute=fastExecute)

    # parallel by default, but slowTool forces sequential
    stream = agentLoop(
        [createUserMessage("run both")],
        AgentContext(messages=[], tools=[slowTool, fastTool]),
        config(),
        None,
        _releasingStream([toolCall("tool-1", "slow", {"value": "a"}), toolCall("tool-2", "fast", {"value": "b"})], slowDone),
    )
    await drain(stream)

    # Fast tool should NOT run before slow tool finishes
    assert executionOrder[0] == "slow:a"
    assert "fast:b" in executionOrder


async def test_should_allow_parallel_execution_when_all_tools_have_executionMode_parallel():
    state = {"firstResolved": False, "parallelObserved": False}
    firstDone = asyncio.Event()
    tool = _firstSecondTool("echo", "parallel", state, firstDone)

    stream = agentLoop(
        [createUserMessage("echo both")],
        AgentContext(messages=[], tools=[tool]),
        config(),
        None,
        _releasingStream(
            [toolCall("tool-1", "echo", {"value": "first"}), toolCall("tool-2", "echo", {"value": "second"})], firstDone
        ),
    )
    await drain(stream)

    # With executionMode=parallel, second tool should start before first finishes
    assert state["parallelObserved"] is True


async def test_runs_finishTurn_after_tool_result_messages_and_before_turn_end():
    async def execute(_toolCallId: str, params: dict[str, Any], *_rest: Any) -> AgentToolResult:
        return AgentToolResult(content=[TextContent(text=params["value"])], details={"value": params["value"]}, terminate=True)

    ordering: list[str] = []

    def finishTurn(turn: Any, _signal: Any = None) -> None:
        ordering.append("finishTurn")
        assert len(turn.toolResults) == 1
        assert turn.context.messages[-1].role == "toolResult"

    def emit(event: Any) -> None:
        if event.type == "message_end":
            ordering.append(f"message_end:{event.message.role}")
        if event.type == "turn_end":
            ordering.append("turn_end")

    await runAgentLoop(
        [createUserMessage("echo")],
        AgentContext(messages=[], tools=[echoTool(execute)]),
        config(finishTurn=finishTurn),
        emit,
        None,
        lambda *_args: respond(createAssistantMessage([toolCall("tool-1", "echo", {"value": "hello"})], "toolUse")),
    )

    assert ordering[-3:] == ["message_end:toolResult", "finishTurn", "turn_end"]


@pytest.mark.parametrize("reason", ["error", "aborted"])
async def test_runs_finishTurn_for_an_error_assistant_before_turn_end_without_changing_the_hard_exit(reason):
    ordering: list[str] = []
    providerCalls = 0
    steeringPolls = 0
    followUpPolls = 0

    def finishTurn(turn: Any, _signal: Any = None) -> dict[str, str]:
        assert turn.message.stopReason == reason
        ordering.append("finishTurn")
        return {"action": "continue"}

    async def getSteeringMessages() -> list[Any]:
        nonlocal steeringPolls
        steeringPolls += 1
        return []

    async def getFollowUpMessages() -> list[Any]:
        nonlocal followUpPolls
        followUpPolls += 1
        return [createUserMessage("queued")]

    def streamFn(*_args: Any) -> Any:
        nonlocal providerCalls
        providerCalls += 1
        stream = createAssistantMessageEventStream()
        error = createAssistantMessage([], reason).model_copy(update={"errorMessage": reason})
        queueMicrotask(lambda: stream.push(ErrorEvent(reason=reason, error=error)))
        return stream

    await runAgentLoop(
        [createUserMessage("run")],
        AgentContext(messages=[], tools=[]),
        config(finishTurn=finishTurn, getSteeringMessages=getSteeringMessages, getFollowUpMessages=getFollowUpMessages),
        lambda event: ordering.append("turn_end") if event.type == "turn_end" else None,
        None,
        streamFn,
    )

    assert ordering == ["finishTurn", "turn_end"]
    assert providerCalls == 1
    assert steeringPolls == 1
    assert followUpPolls == 0


def _noopTool() -> AgentTool:
    async def execute(*_args: Any) -> AgentToolResult:
        return AgentToolResult(content=[TextContent(text="done")], details=None)

    return AgentTool(name="noop", label="Noop", description="Noop tool", parameters=EMPTY_OBJECT, execute=execute)


async def test_action_end_skips_queue_polling_and_next_turn_preparation():
    providerCalls = 0
    steeringPolls = 0
    followUpPolls = 0
    prepareNextTurnCalls = 0

    def prepareNextTurn(_context: Any) -> None:
        nonlocal prepareNextTurnCalls
        prepareNextTurnCalls += 1

    async def getSteeringMessages() -> list[Any]:
        nonlocal steeringPolls
        steeringPolls += 1
        return []

    async def getFollowUpMessages() -> list[Any]:
        nonlocal followUpPolls
        followUpPolls += 1
        return [createUserMessage("queued")]

    def streamFn(*_args: Any) -> Any:
        nonlocal providerCalls
        providerCalls += 1
        return respond(createAssistantMessage([toolCall("tool-1", "noop", {})], "toolUse"))

    stream = agentLoop(
        [createUserMessage("run")],
        AgentContext(messages=[], tools=[_noopTool()]),
        config(
            finishTurn=lambda *_args: {"action": "end"},
            prepareNextTurn=prepareNextTurn,
            getSteeringMessages=getSteeringMessages,
            getFollowUpMessages=getFollowUpMessages,
        ),
        None,
        streamFn,
    )
    await stream.result()

    assert providerCalls == 1
    assert steeringPolls == 1
    assert followUpPolls == 0
    assert prepareNextTurnCalls == 0


async def test_makes_exactly_one_context_only_request_when_no_natural_request_satisfies_continuation():
    providerCalls = 0
    finishCalls = 0

    def finishTurn(*_args: Any) -> Any:
        nonlocal finishCalls
        finishCalls += 1
        return {"action": "continue"} if finishCalls == 1 else None

    def streamFn(*_args: Any) -> Any:
        nonlocal providerCalls
        providerCalls += 1
        return respond(createAssistantMessage([text(f"response {providerCalls}")]))

    stream = agentLoop([createUserMessage("run")], AgentContext(messages=[], tools=[]), config(finishTurn=finishTurn), None, streamFn)
    await stream.result()

    assert providerCalls == 2
    assert finishCalls == 2


async def test_lets_a_natural_tool_result_request_satisfy_continuation():
    providerCalls = 0
    finishCalls = 0

    def finishTurn(*_args: Any) -> Any:
        nonlocal finishCalls
        finishCalls += 1
        return {"action": "continue"} if finishCalls == 1 else None

    def streamFn(*_args: Any) -> Any:
        nonlocal providerCalls
        providerCalls += 1
        message = (
            createAssistantMessage([toolCall("tool-1", "noop", {})], "toolUse")
            if providerCalls == 1
            else createAssistantMessage([text("done")])
        )
        return respond(message)

    stream = agentLoop(
        [createUserMessage("run")], AgentContext(messages=[], tools=[_noopTool()]), config(finishTurn=finishTurn), None, streamFn
    )
    await stream.result()

    assert providerCalls == 2
    assert finishCalls == 2


@pytest.mark.parametrize("queueKind", ["steering", "follow-up"])
async def test_lets_a_natural_queued_request_satisfy_continuation(queueKind):
    queuedMessage = createUserMessage(queueKind)
    providerCalls = 0
    finishCalls = 0
    steeringPolls = 0
    followUpDelivered = False
    secondRequestUsers: list[str] = []

    def finishTurn(*_args: Any) -> Any:
        nonlocal finishCalls
        finishCalls += 1
        return {"action": "continue"} if finishCalls == 1 else None

    async def getSteeringMessages() -> list[Any]:
        nonlocal steeringPolls
        steeringPolls += 1
        return [queuedMessage] if queueKind == "steering" and steeringPolls == 2 else []

    async def getFollowUpMessages() -> list[Any]:
        nonlocal followUpDelivered
        if queueKind != "follow-up" or followUpDelivered:
            return []
        followUpDelivered = True
        return [queuedMessage]

    def streamFn(_model: Any, context: Any, *_rest: Any) -> Any:
        nonlocal providerCalls
        providerCalls += 1
        if providerCalls == 2:
            secondRequestUsers.extend(
                message.content for message in context.messages if message.role == "user" and isinstance(message.content, str)
            )
        return respond(createAssistantMessage([text("done")]))

    stream = agentLoop(
        [createUserMessage("run")],
        AgentContext(messages=[], tools=[]),
        config(finishTurn=finishTurn, getSteeringMessages=getSteeringMessages, getFollowUpMessages=getFollowUpMessages),
        None,
        streamFn,
    )
    await stream.result()

    assert providerCalls == 2
    assert finishCalls == 2
    assert queueKind in secondRequestUsers


async def test_prepares_the_initial_request_after_pending_messages_and_can_replace_request_state():
    replacementModel = createModel().model_copy(update={"id": "replacement", "name": "replacement"})
    canonicalMessage = createUserMessage("canonical projection")
    steeringMessage = createUserMessage("steering")
    completedMessages: list[Any] = []
    steeringDelivered = False
    prepareCalls = 0

    async def getSteeringMessages() -> list[Any]:
        nonlocal steeringDelivered
        if steeringDelivered:
            return []
        steeringDelivered = True
        return [steeringMessage]

    def prepareRequest(request: Any, _signal: Any = None) -> Any:
        nonlocal prepareCalls
        prepareCalls += 1
        assert steeringMessage in completedMessages
        assert steeringMessage in request.context.messages
        from misaka.agent.types import AgentRequestUpdate

        return AgentRequestUpdate(
            context=AgentContext(messages=[canonicalMessage], tools=request.context.tools),
            model=replacementModel,
            thinkingLevel="high",
        )

    def streamFn(model: Any, context: Any, options: Any = None) -> Any:
        assert model is replacementModel
        assert context.messages == [canonicalMessage]
        assert options.reasoning == "high"
        return respond(createAssistantMessage([text("done")]))

    await runAgentLoop(
        [createUserMessage("prompt")],
        AgentContext(messages=[], tools=[]),
        config(getSteeringMessages=getSteeringMessages, prepareRequest=prepareRequest),
        lambda event: completedMessages.append(event.message) if event.type == "message_end" else None,
        None,
        streamFn,
    )

    assert prepareCalls == 1


async def test_does_not_poll_steering_after_prepareRequest():
    queued: list[Any] = []
    lateSteering = createUserMessage("late steering")
    requestIncludedSteering: list[bool] = []
    requestPreparations = 0
    steeringPolls = 0

    async def getSteeringMessages() -> list[Any]:
        nonlocal steeringPolls
        steeringPolls += 1
        drained = queued[:]
        queued.clear()
        return drained

    def prepareRequest(*_args: Any) -> None:
        nonlocal requestPreparations
        requestPreparations += 1
        if requestPreparations == 1:
            queued.append(lateSteering)

    def streamFn(_model: Any, context: Any, *_rest: Any) -> Any:
        requestIncludedSteering.append(any(message is lateSteering for message in context.messages))
        return respond(createAssistantMessage([text("done")]))

    stream = agentLoop(
        [createUserMessage("run")],
        AgentContext(messages=[], tools=[]),
        config(getSteeringMessages=getSteeringMessages, prepareRequest=prepareRequest),
        None,
        streamFn,
    )
    await stream.result()

    assert requestIncludedSteering == [False, True]
    assert requestPreparations == 2
    # Startup, post-turn delivery, then the final natural-stop check.
    assert steeringPolls == 3


async def test_should_use_prepareNextTurn_snapshot_before_continuing():
    async def execute(_toolCallId: str, params: dict[str, Any], *_rest: Any) -> AgentToolResult:
        return AgentToolResult(content=[TextContent(text=f"echoed: {params['value']}")], details={"value": params["value"]})

    context = AgentContext(messages=[], tools=[echoTool(execute)])
    convertedSecondTurnHasUpdate = False
    prepareCalls = 0
    prepared = False

    async def prepareNextTurn(turn: Any) -> Any:
        nonlocal prepareCalls, prepared
        prepareCalls += 1
        if prepared:
            return None
        prepared = True
        from misaka.agent.types import AgentLoopTurnUpdate

        return AgentLoopTurnUpdate(
            context=AgentContext(messages=turn.context.messages[:], tools=turn.context.tools),
            messages=[SystemMessage(content="updated guidance", timestamp=1)],
        )

    llmCalls = 0

    def streamFn(_model: Any, ctx: Any, *_rest: Any) -> Any:
        nonlocal llmCalls, convertedSecondTurnHasUpdate
        llmCalls += 1
        if llmCalls == 2:
            convertedSecondTurnHasUpdate = any(
                message.role == "system" and message.content == "updated guidance" for message in ctx.messages
            )
        if llmCalls == 1:
            return respond(createAssistantMessage([toolCall("tool-1", "echo", {"value": "hello"})], "toolUse"))
        return respond(createAssistantMessage([text("done")]))

    stream = agentLoop([createUserMessage("echo something")], context, config(prepareNextTurn=prepareNextTurn), None, streamFn)
    await drain(stream)

    assert llmCalls == 2
    assert prepareCalls == 1
    assert convertedSecondTurnHasUpdate is True


async def test_picks_up_steering_queued_during_prepareNextTurn_before_the_next_request():
    queued: list[Any] = []
    lateSteering = createUserMessage("late steering")
    providerCalls = 0
    secondRequestIncludedSteering = False

    def prepareNextTurn(_turn: Any) -> None:
        queued.append(lateSteering)

    async def getSteeringMessages() -> list[Any]:
        drained = queued[:]
        queued.clear()
        return drained

    def streamFn(_model: Any, context: Any, *_rest: Any) -> Any:
        nonlocal providerCalls, secondRequestIncludedSteering
        providerCalls += 1
        if providerCalls == 2:
            secondRequestIncludedSteering = any(message is lateSteering for message in context.messages)
        message = (
            createAssistantMessage([toolCall("tool-1", "noop", {})], "toolUse")
            if providerCalls == 1
            else createAssistantMessage([text("done")])
        )
        return respond(message)

    stream = agentLoop(
        [createUserMessage("run")],
        AgentContext(messages=[], tools=[_noopTool()]),
        config(prepareNextTurn=prepareNextTurn, getSteeringMessages=getSteeringMessages),
        None,
        streamFn,
    )
    await stream.result()

    assert providerCalls == 2
    assert secondRequestIncludedSteering is True


async def test_action_end_receives_finalized_turn_context_and_stops_before_queue_polling():
    executed: list[str] = []

    async def execute(_toolCallId: str, params: dict[str, Any], *_rest: Any) -> AgentToolResult:
        executed.append(params["value"])
        return AgentToolResult(content=[TextContent(text=f"echoed: {params['value']}")], details={"value": params["value"]})

    context = AgentContext(messages=[], tools=[echoTool(execute)])

    steeringPolls = 0
    followUpPolls = 0
    callbackToolResultIds: list[str] = []
    callbackContextRoles: list[str] = []

    async def finishTurn(turn: Any, _signal: Any = None) -> dict[str, str]:
        nonlocal callbackToolResultIds, callbackContextRoles
        assert turn.message.role == "assistant"
        callbackToolResultIds = [toolResult.toolCallId for toolResult in turn.toolResults]
        callbackContextRoles = [contextMessage.role for contextMessage in turn.context.messages]
        return {"action": "end"}

    async def getSteeringMessages() -> list[Any]:
        nonlocal steeringPolls
        steeringPolls += 1
        return []

    async def getFollowUpMessages() -> list[Any]:
        nonlocal followUpPolls
        followUpPolls += 1
        return [createUserMessage("follow up should stay queued")]

    llmCalls = 0

    def streamFn(*_args: Any) -> Any:
        nonlocal llmCalls
        llmCalls += 1
        if llmCalls == 1:
            return respond(createAssistantMessage([toolCall("tool-1", "echo", {"value": "hello"})], "toolUse"))
        return respond(createAssistantMessage([text("should not run")]))

    stream = agentLoop(
        [createUserMessage("echo something")],
        context,
        config(finishTurn=finishTurn, getSteeringMessages=getSteeringMessages, getFollowUpMessages=getFollowUpMessages),
        None,
        streamFn,
    )
    events = await drain(stream)

    messages = await stream.result()
    assert llmCalls == 1
    assert executed == ["hello"]
    assert steeringPolls == 1
    assert followUpPolls == 0
    assert callbackToolResultIds == ["tool-1"]
    assert callbackContextRoles == ["system", "user", "assistant", "toolResult"]
    # The context declares no tools, so the loop announces the loadout with a system message.
    assert [message.role for message in messages] == ["system", "user", "assistant", "toolResult"]
    assert [event.type for event in events] == [
        "agent_start",
        "turn_start",
        "message_start",
        "message_end",
        "message_start",
        "message_end",
        "message_start",
        "message_end",
        "tool_execution_start",
        "tool_execution_end",
        "message_start",
        "message_end",
        "turn_end",
        "agent_end",
    ]


async def test_should_stop_after_a_tool_batch_when_every_tool_result_sets_terminate_true():
    async def execute(_toolCallId: str, params: dict[str, Any], *_rest: Any) -> AgentToolResult:
        return AgentToolResult(
            content=[TextContent(text=f"echoed: {params['value']}")], details={"value": params["value"]}, terminate=True
        )

    llmCalls = 0

    def streamFn(*_args: Any) -> Any:
        nonlocal llmCalls
        llmCalls += 1
        return respond(createAssistantMessage([toolCall("tool-1", "echo", {"value": "hello"})], "toolUse"))

    stream = agentLoop(
        [createUserMessage("echo something")], AgentContext(messages=[], tools=[echoTool(execute)]), config(), None, streamFn
    )
    events = await drain(stream)

    messages = await stream.result()
    assert llmCalls == 1
    assert [message.role for message in messages] == ["system", "user", "assistant", "toolResult"]
    assert len([event for event in events if event.type == "turn_end"]) == 1


async def test_should_stop_after_a_blocked_tool_call_when_beforeToolCall_sets_terminate_true():
    executed = False

    async def execute(*_args: Any) -> AgentToolResult:
        nonlocal executed
        executed = True
        return AgentToolResult(content=[TextContent(text="should not execute")], details={"value": "unexpected"})

    async def beforeToolCall(*_args: Any) -> dict[str, Any]:
        return {"block": True, "reason": "Blocked by policy", "terminate": True}

    llmCalls = 0

    def streamFn(*_args: Any) -> Any:
        nonlocal llmCalls
        llmCalls += 1
        if llmCalls == 1:
            return respond(createAssistantMessage([toolCall("tool-1", "echo", {"value": "hello"})], "toolUse"))
        return respond(createAssistantMessage([text("should not run")]))

    stream = agentLoop(
        [createUserMessage("echo something")],
        AgentContext(messages=[], tools=[echoTool(execute)]),
        config(beforeToolCall=beforeToolCall),
        None,
        streamFn,
    )
    await drain(stream)

    messages = await stream.result()
    toolResult = next(message for message in messages if message.role == "toolResult")
    assert executed is False
    assert llmCalls == 1
    assert toolResult.isError is True
    assert {"type": "text", "text": "Blocked by policy"} in [block.model_dump(exclude_none=True) for block in toolResult.content]


async def test_should_continue_after_a_mixed_batch_with_one_terminating_blocked_call():
    executed: list[str] = []

    async def execute(_toolCallId: str, params: dict[str, Any], *_rest: Any) -> AgentToolResult:
        executed.append(params["value"])
        return AgentToolResult(content=[TextContent(text=f"echoed: {params['value']}")], details={"value": params["value"]})

    async def beforeToolCall(context: Any, _signal: Any = None) -> Any:
        value = context.args["value"]
        return {"block": True, "reason": "Blocked first", "terminate": True} if value == "first" else None

    llmCalls = 0

    def streamFn(*_args: Any) -> Any:
        nonlocal llmCalls
        llmCalls += 1
        if llmCalls == 1:
            return respond(
                createAssistantMessage(
                    [toolCall("tool-1", "echo", {"value": "first"}), toolCall("tool-2", "echo", {"value": "second"})],
                    "toolUse",
                )
            )
        return respond(createAssistantMessage([text("done")]))

    stream = agentLoop(
        [createUserMessage("echo both")],
        AgentContext(messages=[], tools=[echoTool(execute)]),
        config(toolExecution="parallel", beforeToolCall=beforeToolCall),
        None,
        streamFn,
    )
    await drain(stream)

    assert executed == ["second"]
    assert llmCalls == 2


async def test_should_continue_after_parallel_tool_calls_when_not_all_tool_results_terminate():
    async def execute(_toolCallId: str, params: dict[str, Any], *_rest: Any) -> AgentToolResult:
        return AgentToolResult(
            content=[TextContent(text=f"echoed: {params['value']}")],
            details={"value": params["value"]},
            terminate=params["value"] == "first",
        )

    streamFn, callIndex = toolThenDone(
        [toolCall("tool-1", "echo", {"value": "first"}), toolCall("tool-2", "echo", {"value": "second"})]
    )
    stream = agentLoop(
        [createUserMessage("echo both")],
        AgentContext(messages=[], tools=[echoTool(execute)]),
        config(toolExecution="parallel"),
        None,
        streamFn,
    )
    await drain(stream)

    messages = await stream.result()
    assert callIndex[0] == 2
    assert [message.role for message in messages] == [
        "system",
        "user",
        "assistant",
        "toolResult",
        "toolResult",
        "assistant",
    ]


async def test_should_allow_afterToolCall_to_mark_a_tool_batch_as_terminating():
    async def execute(_toolCallId: str, params: dict[str, Any], *_rest: Any) -> AgentToolResult:
        return AgentToolResult(content=[TextContent(text=f"echoed: {params['value']}")], details={"value": params["value"]})

    async def afterToolCall(*_args: Any) -> dict[str, Any]:
        return {"terminate": True}

    llmCalls = 0

    def streamFn(*_args: Any) -> Any:
        nonlocal llmCalls
        llmCalls += 1
        return respond(createAssistantMessage([toolCall("tool-1", "echo", {"value": "hello"})], "toolUse"))

    stream = agentLoop(
        [createUserMessage("echo something")],
        AgentContext(messages=[], tools=[echoTool(execute)]),
        config(afterToolCall=afterToolCall),
        None,
        streamFn,
    )
    await drain(stream)

    assert llmCalls == 1


# --- agentLoopContinue with AgentMessage -------------------------------------------------------


def test_agentLoopContinue_should_throw_when_context_has_no_messages():
    def unexpected(*_args: Any) -> Any:
        raise RuntimeError("Unexpected stream call")

    with pytest.raises(Exception, match="Cannot continue: no messages in context"):
        agentLoopContinue(AgentContext(messages=[], tools=[]), config(), None, unexpected)


async def test_should_continue_from_existing_context_without_emitting_user_message_events():
    userMessage = createUserMessage("Hello")
    context = AgentContext(messages=[userMessage], tools=[])

    stream = agentLoopContinue(context, config(), None, lambda *_args: respond(createAssistantMessage([text("Response")])))
    events = await drain(stream)
    messages = await stream.result()

    # Should only return the new assistant message (not the existing user message)
    assert len(messages) == 1
    assert messages[0].role == "assistant"

    # Should NOT have user message events (that's the key difference from agentLoop)
    messageEndEvents = [e for e in events if e.type == "message_end"]
    assert len(messageEndEvents) == 1
    assert messageEndEvents[0].message.role == "assistant"


async def test_should_allow_custom_message_types_as_last_message_caller_responsibility():
    # Custom message that will be converted to user message by convertToLlm
    customMessage = {"role": "custom", "text": "Hook content", "timestamp": int(time.time() * 1000)}
    context = AgentContext(messages=[customMessage], tools=[])

    def convertToLlm(messages: list[Any]) -> list[Any]:
        # Convert custom to user message
        converted = [
            UserMessage(content=m["text"], timestamp=m["timestamp"]) if _role(m) == "custom" else m for m in messages
        ]
        return [m for m in converted if m.role in ("user", "assistant", "toolResult")]

    # Should not throw - the custom message will be converted to user message
    stream = agentLoopContinue(
        context,
        config(convertToLlm=convertToLlm),
        None,
        lambda *_args: respond(createAssistantMessage([text("Response to custom message")])),
    )
    await drain(stream)

    messages = await stream.result()
    assert len(messages) == 1
    assert messages[0].role == "assistant"


# --- runToolCall --------------------------------------------------------------------------------


async def _echoExecute(_toolCallId: str, params: dict[str, Any], _signal: Any, onUpdate: Any) -> AgentToolResult:
    if onUpdate is not None:
        onUpdate(AgentToolResult(content=[TextContent(text="partial")], details={}))
    return AgentToolResult(
        content=[TextContent(text=params["value"])], details={}, structuredContent={"value": params["value"]}
    )


async def _failingExecute(*_args: Any) -> AgentToolResult:
    return AgentToolResult(content=[TextContent(text="bad")], details={"partial": True}, isError=True)


echo = AgentTool(
    name="echo",
    label="Echo",
    description="Echo tool",
    parameters=valueSchema(),
    outputSchema={"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
    execute=_echoExecute,
)
failing = AgentTool(
    name="failing", label="Failing", description="Returns an error result", parameters=EMPTY_OBJECT, execute=_failingExecute
)


def call(id: str, name: str, args: dict[str, Any]) -> ToolCall:
    return ToolCall(id=id, name=name, arguments=args)


async def test_validates_runs_the_hooks_and_reports_failures_as_error_outcomes():
    assistantMessage = createAssistantMessage([])
    hookCalls: list[str] = []
    updates: list[Any] = []

    async def beforeToolCall(context: Any, _signal: Any = None) -> Any:
        hookCalls.append(f"before {context.toolCall.id}")
        if context.args.get("value") == "blocked":
            return {"block": True, "reason": "nope"}
        return None

    async def afterToolCall(context: Any, _signal: Any = None) -> None:
        hookCalls.append(f"after {context.toolCall.id}")

    options = RunToolCallOptions(
        tools=[echo, failing],
        assistantMessage=assistantMessage,
        context=AgentContext(messages=[]),
        beforeToolCall=beforeToolCall,
        afterToolCall=afterToolCall,
        onUpdate=updates.append,
    )

    first = await runToolCall(call("a", "echo", {"value": "a"}), options)
    assert (first.toolCall.id, first.result.structuredContent, first.isError) == ("a", {"value": "a"}, False)
    assert (await runToolCall(call("b", "echo", {"value": {"nested": True}}), options)).isError is True
    blocked = await runToolCall(call("c", "echo", {"value": "blocked"}), options)
    assert ([block.model_dump() for block in blocked.result.content], blocked.isError) == ([{"type": "text", "text": "nope", "textSignature": None}], True)
    missing = await runToolCall(call("d", "missing", {}), options)
    assert (missing.result.content[0].text, missing.isError) == ("Tool missing not found", True)
    # Error results keep their details.
    failingOutcome = await runToolCall(call("e", "failing", {}), options)
    assert (failingOutcome.result.details, failingOutcome.isError) == ({"partial": True}, True)
    assert [(update.content[0].text, update.details) for update in updates] == [("partial", {})]
    # Validation failures and unknown tools never reach the hooks; blocked calls skip afterToolCall.
    assert hookCalls == ["before a", "after a", "before c", "before e", "after e"]


async def test_lets_afterToolCall_replace_structured_content_and_drops_it_when_only_content_is_replaced():
    assistantMessage = createAssistantMessage([])
    redacted = [{"type": "text", "text": "redacted"}]
    results = [
        {"content": redacted},
        {"structuredContent": {"value": "replaced"}},
        {"content": redacted, "structuredContent": {"value": "both"}},
        {"details": {"note": "kept"}},
    ]
    seen: list[Any] = []
    for afterResult in results:

        async def afterToolCall(_context: Any, _signal: Any = None, afterResult: Any = afterResult) -> Any:
            return afterResult

        outcome = await runToolCall(
            call("x", "echo", {"value": "original"}),
            RunToolCallOptions(
                tools=[echo], assistantMessage=assistantMessage, context=AgentContext(messages=[]), afterToolCall=afterToolCall
            ),
        )
        seen.append(outcome.result.structuredContent)
    assert seen == [None, {"value": "replaced"}, {"value": "both"}, {"value": "original"}]

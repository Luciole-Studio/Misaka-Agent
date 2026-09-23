import time

from misaka.agent import agent_loop
from misaka.agent.agent import Agent
from misaka.agent.types import (
    AgentContext,
    AgentLoopConfig,
    AgentTool,
    AgentToolResult,
    MessageEndEvent,
    MessageStartEvent,
)
from misaka.ai.types import AssistantMessage, TextContent, ToolCall, Usage, UserMessage

USAGE = Usage(
    input=1,
    output=1,
    cacheRead=0,
    cacheWrite=0,
    totalTokens=2,
    cost={"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
)


def _assistant(content, stop_reason):
    return AssistantMessage(
        content=content,
        api="test",
        provider="test",
        model="test",
        usage=USAGE,
        stopReason=stop_reason,
        timestamp=int(time.time() * 1000),
    )


async def test_length_truncated_tool_calls_are_failed_without_execution(monkeypatch):
    executions = []
    hook_calls = []
    events = []

    async def execute(call_id, args, _signal, _on_update):
        executions.append((call_id, args))
        return AgentToolResult(content=[TextContent(text="executed")], details={})

    async def hook(*args):
        hook_calls.append(args)

    tool = AgentTool(
        name="write",
        label="write",
        description="fixture",
        parameters={"type": "object"},
        execute=execute,
    )
    replies = [
        _assistant(
            [
                ToolCall(id="c1", name="write", arguments={"path": "PARTIAL"}),
                ToolCall(id="c2", name="write", arguments={"path": "ALSO_PARTIAL"}),
            ],
            "length",
        ),
        _assistant([TextContent(text="recovered")], "stop"),
    ]

    async def fake_stream(context, _config, _signal, emit, _stream_fn):
        message = replies.pop(0)
        context.messages.append(message)
        await emit(MessageStartEvent(message=message.model_copy(deep=True)))
        await emit(MessageEndEvent(message=message))
        return message

    monkeypatch.setattr(agent_loop, "stream_assistant_response", fake_stream)
    config = AgentLoopConfig(
        model=Agent().state.model,
        convertToLlm=lambda messages: messages,
        beforeToolCall=hook,
        afterToolCall=hook,
    )

    async def emit(event):
        events.append(event)

    messages = await agent_loop.run_agent_loop(
        [UserMessage(content="fixture", timestamp=1)],
        AgentContext(messages=[], tools=[tool]),
        config,
        emit,
    )

    tool_results = [
        message
        for message in messages
        if getattr(message, "role", None) == "toolResult"
    ]
    assert executions == []
    assert hook_calls == []
    assert replies == []
    assert [message.toolCallId for message in tool_results] == ["c1", "c2"]
    assert all(message.isError for message in tool_results)
    assert all(
        "output token limit" in message.content[0].text for message in tool_results
    )
    # pi 0.87: the loop declares the executable tools to the model with a system message
    # before the first request, since the context's transcript did not yet declare them.
    assert [getattr(message, "role", None) for message in messages] == [
        "system",
        "user",
        "assistant",
        "toolResult",
        "toolResult",
        "assistant",
    ]
    assert [tool.name for tool in messages[0].toolsAdded] == [tool.name]
    assert [event.type for event in events].count("turn_start") == 2
    assert [event.type for event in events].count("tool_execution_start") == 2
    assert [event.type for event in events].count("tool_execution_end") == 2

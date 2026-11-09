"""Translated from pi's ``packages/coding-agent/test/test-harness.test.ts`` (v1.1.0).

Tests for the test harness itself.
Validates that the faux provider and session factory work correctly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.types import TextContent
from misaka.utils.values import read_field
from tests.session_test_harness import (
    Harness,
    createHarness,
    createHarnessWithExtensions,
)


@pytest.fixture
async def harnesses() -> AsyncIterator[list[Harness]]:
    created: list[Harness] = []
    yield created
    for harness in created:
        harness.cleanup()


def assistantMessages(harness: Harness) -> list[Any]:
    return [message for message in harness.session.messages if read_field(message, "role") == "assistant"]


def firstText(message: Any) -> str | None:
    return next((block.text for block in message.content if block.type == "text"), None)


def echoTool(onExecute: Any = None) -> AgentTool:
    async def execute(*_: Any) -> AgentToolResult:
        if onExecute is not None:
            onExecute()
        return AgentToolResult(content=[TextContent(text="echoed")], details={})

    return AgentTool(
        name="echo",
        label="Echo",
        description="Echo back",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        execute=execute,
    )


async def test_simple_text_response(harnesses: list[Harness]):
    harness = await createHarness(responses=["hello world"])
    harnesses.append(harness)

    await harness.session.prompt("hi")

    assert harness.faux.callCount == 1

    messages = assistantMessages(harness)
    assert len(messages) == 1

    msg = messages[0]
    assert [block.model_dump(exclude_none=True) for block in msg.content] == [{"type": "text", "text": "hello world"}]
    assert msg.stopReason == "stop"


async def test_response_sequence(harnesses: list[Harness]):
    harness = await createHarness(responses=["first", "second", "third"])
    harnesses.append(harness)

    await harness.session.prompt("a")
    await harness.session.prompt("b")
    await harness.session.prompt("c")

    assert harness.faux.callCount == 3
    assert [firstText(message) for message in assistantMessages(harness)] == ["first", "second", "third"]


async def test_tool_call_response_triggers_tool_execution(harnesses: list[Harness]):
    state = {"toolExecuted": False}
    tool = echoTool(lambda: state.update(toolExecuted=True))

    harness = await createHarness(
        responses=[{"toolCalls": [{"name": "echo", "args": {"text": "hi"}}]}, "done after tool"],
        tools=[tool],
        baseToolsOverride={"echo": tool},
    )
    harnesses.append(harness)

    await harness.session.prompt("use the tool")

    assert state["toolExecuted"] is True
    assert harness.faux.callCount == 2

    toolResults = [message for message in harness.session.messages if read_field(message, "role") == "toolResult"]
    assert len(toolResults) == 1


async def test_error_response(harnesses: list[Harness]):
    harness = await createHarness(responses=[{"error": "something broke"}])
    harnesses.append(harness)

    await harness.session.prompt("hi")

    messages = assistantMessages(harness)
    assert len(messages) == 1
    assert messages[0].stopReason == "error"
    assert messages[0].errorMessage == "something broke"


async def test_turns_a_pending_terminal_response_into_an_error(harnesses: list[Harness]):
    harness = await createHarness(
        responses=[{"text": "partial", "stopReason": "pending"}],
        settings={"retry": {"enabled": False}},
    )
    harnesses.append(harness)

    await harness.session.prompt("hi")

    messages = assistantMessages(harness)
    assert len(messages) == 1
    assert messages[0].stopReason == "error"
    assert messages[0].errorMessage == "Faux response ended without a stop reason"


async def test_retry_on_transient_error(harnesses: list[Harness]):
    harness = await createHarness(
        responses=[{"error": "overloaded_error"}, "recovered"],
        settings={"retry": {"enabled": True, "maxRetries": 3, "baseDelayMs": 1}},
    )
    harnesses.append(harness)

    await harness.session.prompt("hi")

    assert harness.faux.callCount == 2

    retryStarts = harness.eventsOfType("auto_retry_start")
    assert len(retryStarts) == 1

    retryEnds = harness.eventsOfType("auto_retry_end")
    assert len(retryEnds) == 1
    assert read_field(retryEnds[0], "success") is True


async def test_custom_usage_numbers(harnesses: list[Harness]):
    harness = await createHarness(responses=[{"text": "big response", "usage": {"input": 100000, "output": 5000}}])
    harnesses.append(harness)

    await harness.session.prompt("hi")

    msg = assistantMessages(harness)[0]
    assert msg.usage.input == 100000
    assert msg.usage.output == 5000


async def test_event_capture(harnesses: list[Harness]):
    harness = await createHarness(responses=["hello"])
    harnesses.append(harness)

    await harness.session.prompt("hi")

    assert len(harness.eventsOfType("agent_start")) == 1
    assert len(harness.eventsOfType("agent_end")) == 1
    assert len(harness.eventsOfType("message_end")) >= 2  # user + assistant


async def test_context_capture(harnesses: list[Harness]):
    harness = await createHarness(responses=["reply"])
    harnesses.append(harness)

    await harness.session.prompt("my question")

    assert len(harness.faux.contexts) == 1
    ctx = harness.faux.contexts[0]
    userMsg = next((message for message in read_field(ctx, "messages") if read_field(message, "role") == "user"), None)
    assert userMsg is not None


async def test_wraps_around_when_more_calls_than_responses(harnesses: list[Harness]):
    harness = await createHarness(responses=["a", "b"])
    harnesses.append(harness)

    await harness.session.prompt("1")
    await harness.session.prompt("2")
    await harness.session.prompt("3")

    assert harness.faux.callCount == 3
    assert [firstText(message) for message in assistantMessages(harness)] == ["a", "b", "a"]


def streamEvents(harness: Harness) -> list[Any]:
    return [read_field(event, "assistantMessageEvent") for event in harness.eventsOfType("message_update")]


async def test_streams_text_deltas(harnesses: list[Harness]):
    harness = await createHarness(responses=["hello world"])
    harnesses.append(harness)

    await harness.session.prompt("hi")

    textDeltas = [event for event in streamEvents(harness) if event.type == "text_delta"]
    assert len(textDeltas) > 0

    # Deltas should reconstruct the full text
    assert "".join(event.delta for event in textDeltas) == "hello world"


async def test_streams_thinking_deltas(harnesses: list[Harness]):
    harness = await createHarness(responses=[{"thinking": "let me think about this", "text": "answer"}])
    harnesses.append(harness)

    await harness.session.prompt("hi")

    events = streamEvents(harness)
    thinkingStarts = [event for event in events if event.type == "thinking_start"]
    thinkingDeltas = [event for event in events if event.type == "thinking_delta"]
    thinkingEnds = [event for event in events if event.type == "thinking_end"]

    assert len(thinkingStarts) == 1
    assert len(thinkingDeltas) > 0
    assert len(thinkingEnds) == 1
    assert "".join(event.delta for event in thinkingDeltas) == "let me think about this"


async def test_streams_tool_call_deltas(harnesses: list[Harness]):
    tool = echoTool()
    harness = await createHarness(
        responses=[{"toolCalls": [{"name": "echo", "args": {"text": "hi"}}]}, "done"],
        tools=[tool],
        baseToolsOverride={"echo": tool},
    )
    harnesses.append(harness)

    await harness.session.prompt("use tool")

    events = streamEvents(harness)
    assert len([event for event in events if event.type == "toolcall_start"]) == 1
    assert len([event for event in events if event.type == "toolcall_delta"]) > 0
    assert len([event for event in events if event.type == "toolcall_end"]) == 1


async def test_streams_thinking_then_text_then_tool_call_in_order(harnesses: list[Harness]):
    tool = echoTool()
    harness = await createHarness(
        responses=[
            {"thinking": "hmm", "text": "I will call a tool", "toolCalls": [{"name": "echo", "args": {"text": "x"}}]},
            "final",
        ],
        tools=[tool],
        baseToolsOverride={"echo": tool},
    )
    harnesses.append(harness)

    await harness.session.prompt("do it")

    streamTypes = [event.type for event in streamEvents(harness)]

    # Thinking events should come before text events, text before toolcall
    firstThinking = streamTypes.index("thinking_start")
    firstText_ = streamTypes.index("text_start")
    firstToolcall = streamTypes.index("toolcall_start")

    assert firstThinking < firstText_
    assert firstText_ < firstToolcall


async def test_loads_inline_extension_factories_and_disambiguates_duplicate_commands(harnesses: list[Harness]):
    calls: list[str] = []

    def alpha(pi: Any) -> None:
        async def handler(args: str, *_: Any) -> None:
            calls.append(f"alpha:{args}")

        pi.registerCommand("shared-cmd", {"description": "Alpha command", "handler": handler})

    def beta(pi: Any) -> None:
        async def handler(args: str, *_: Any) -> None:
            calls.append(f"beta:{args}")

        pi.registerCommand("shared-cmd", {"description": "Beta command", "handler": handler})

    harness = await createHarnessWithExtensions(
        extensionFactories=[{"path": "<alpha>", "factory": alpha}, {"path": "<beta>", "factory": beta}]
    )
    harnesses.append(harness)

    runner = harness.session.extensionRunner
    assert runner is not None

    commands = runner.getRegisteredCommands()
    assert [
        {
            "name": read_field(command, "name"),
            "invocationName": read_field(command, "invocationName"),
            "description": read_field(command, "description"),
            "path": read_field(read_field(command, "sourceInfo"), "path"),
        }
        for command in commands
    ] == [
        {"name": "shared-cmd", "invocationName": "shared-cmd:1", "description": "Alpha command", "path": "<alpha>"},
        {"name": "shared-cmd", "invocationName": "shared-cmd:2", "description": "Beta command", "path": "<beta>"},
    ]

    await read_field(runner.getCommand("shared-cmd:1"), "handler")("first", runner.createCommandContext())
    await read_field(runner.getCommand("shared-cmd:2"), "handler")("second", runner.createCommandContext())

    assert calls == ["alpha:first", "beta:second"]


async def test_session_persistence_works(harnesses: list[Harness]):
    harness = await createHarness(responses=["persisted"])
    harnesses.append(harness)

    await harness.session.prompt("hi")

    entries = harness.sessionManager.getEntries()
    messageEntries = [entry for entry in entries if read_field(entry, "type") == "message"]
    assert len(messageEntries) >= 2  # user + assistant

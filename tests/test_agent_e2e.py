"""Translated from pi's ``packages/agent/test/e2e.test.ts`` (v1.1.0)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from misaka.agent import Agent
from misaka.ai.compat import registerFauxProvider, streamSimple
from misaka.ai.providers.faux import (
    fauxAssistantMessage,
    fauxText,
    fauxThinking,
    fauxToolCall,
)
from misaka.ai.types import AssistantMessage, Model, ToolResultMessage, UserMessage
from misaka.utils.js import startTask
from tests.agent_test_utils import calculateTool

registrations: list[Any] = []


def createFauxRegistration(options: dict[str, Any] | None = None) -> Any:
    registration = registerFauxProvider(options or {})
    registrations.append(registration)
    return registration


def getTextContent(message: AssistantMessage | ToolResultMessage) -> str:
    return "\n".join(block.text for block in message.content if block.type == "text")


@pytest.fixture(autouse=True)
def _unregister():
    yield
    while len(registrations) > 0:
        registrations.pop().unregister()


def _usage() -> dict[str, Any]:
    return {
        "input": 0,
        "output": 0,
        "cacheRead": 0,
        "cacheWrite": 0,
        "totalTokens": 0,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
    }


async def basicPrompt(model: Model) -> None:
    agent = Agent(
        streamFn=streamSimple,
        initialState={
            "systemPrompt": "You are a helpful assistant. Keep your responses concise.",
            "model": model,
            "thinkingLevel": "off",
            "tools": [],
        },
    )

    await agent.prompt("What is 2+2? Answer with just the number.")

    assert agent.state.isStreaming is False
    assert len(agent.state.messages) == 3
    assert agent.state.messages[0].role == "system"
    assert agent.state.messages[1].role == "user"
    assert agent.state.messages[2].role == "assistant"

    assistantMessage = agent.state.messages[2]
    assert "4" in getTextContent(assistantMessage)


async def toolExecution(model: Model) -> None:
    agent = Agent(
        streamFn=streamSimple,
        initialState={
            "systemPrompt": "You are a helpful assistant. Always use the calculator tool for math.",
            "model": model,
            "thinkingLevel": "off",
            "tools": [calculateTool],
        },
    )

    pendingToolCallsDuringEvents: list[dict[str, Any]] = []

    def listener(event: Any, _signal: Any) -> None:
        if event.type in ("tool_execution_start", "tool_execution_end"):
            pendingToolCallsDuringEvents.append({"type": event.type, "ids": [*agent.state.pendingToolCalls]})

    agent.subscribe(listener)

    await agent.prompt("Calculate 123 * 456 using the calculator tool.")

    assert agent.state.isStreaming is False
    assert len(agent.state.messages) >= 4
    toolResultMsg = next((message for message in agent.state.messages if message.role == "toolResult"), None)
    assert toolResultMsg is not None
    assert "123 * 456 = 56088" in getTextContent(toolResultMsg)

    finalMessage = agent.state.messages[-1]
    assert finalMessage.role == "assistant"
    assert "56088" in getTextContent(finalMessage)
    assert len(agent.state.pendingToolCalls) == 0
    assert pendingToolCallsDuringEvents == [
        {"type": "tool_execution_start", "ids": ["calc-1"]},
        {"type": "tool_execution_end", "ids": []},
    ]


async def abortExecution(model: Model) -> None:
    agent = Agent(
        streamFn=streamSimple,
        initialState={"systemPrompt": "You are a helpful assistant.", "model": model, "thinkingLevel": "off", "tools": []},
    )

    promptPromise = startTask(agent.prompt("Count slowly from 1 to 20."))
    asyncio.get_running_loop().call_later(0.03, agent.abort)

    await promptPromise

    assert agent.state.isStreaming is False
    assert len(agent.state.messages) >= 2

    lastMessage = agent.state.messages[-1]
    assert lastMessage.role == "assistant"
    assert lastMessage.stopReason == "aborted"
    assert lastMessage.errorMessage is not None
    assert agent.state.errorMessage == lastMessage.errorMessage


async def stateUpdates(model: Model) -> None:
    agent = Agent(
        streamFn=streamSimple,
        initialState={"systemPrompt": "You are a helpful assistant.", "model": model, "thinkingLevel": "off", "tools": []},
    )

    events: list[str] = []
    agent.subscribe(lambda event, _signal: events.append(event.type))

    await agent.prompt("Count from 1 to 5.")

    for expected in ("agent_start", "turn_start", "message_start", "message_update", "message_end", "turn_end", "agent_end"):
        assert expected in events
    assert events.index("agent_start") < events.index("message_start")
    assert events.index("message_start") < events.index("message_end")
    assert events.index("message_end") < len(events) - 1 - events[::-1].index("agent_end")

    assert agent.state.isStreaming is False
    assert len(agent.state.messages) == 3


async def multiTurnConversation(model: Model) -> None:
    agent = Agent(
        streamFn=streamSimple,
        initialState={"systemPrompt": "You are a helpful assistant.", "model": model, "thinkingLevel": "off", "tools": []},
    )

    await agent.prompt("My name is Alice.")
    assert len(agent.state.messages) == 3

    await agent.prompt("What is my name?")
    assert len(agent.state.messages) == 5

    lastMessage = agent.state.messages[4]
    assert lastMessage.role == "assistant"
    assert "alice" in getTextContent(lastMessage).lower()


# --- Agent integration with faux provider ---------------------------------------------------


async def test_handles_a_basic_text_prompt():
    faux = createFauxRegistration()
    faux.setResponses([fauxAssistantMessage("4")])
    await basicPrompt(faux.getModel())


async def test_executes_tools_and_tracks_pending_tool_calls():
    faux = createFauxRegistration()
    faux.setResponses(
        [
            fauxAssistantMessage(
                [
                    fauxText("Let me calculate that."),
                    fauxToolCall("calculate", {"expression": "123 * 456"}, {"id": "calc-1"}),
                ],
                {"stopReason": "toolUse"},
            ),
            fauxAssistantMessage("The result is 56088."),
        ]
    )
    await toolExecution(faux.getModel())


async def test_handles_abort_during_streaming():
    faux = createFauxRegistration({"tokensPerSecond": 20, "tokenSize": {"min": 2, "max": 2}})
    faux.setResponses(
        [fauxAssistantMessage("one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen")]
    )
    await abortExecution(faux.getModel())


async def test_emits_lifecycle_updates_while_streaming():
    faux = createFauxRegistration({"tokenSize": {"min": 1, "max": 1}})
    faux.setResponses([fauxAssistantMessage("1 2 3 4 5")])
    await stateUpdates(faux.getModel())


async def test_maintains_context_across_multiple_turns():
    faux = createFauxRegistration()

    def respond(context: Any, *_rest: Any) -> AssistantMessage:
        def mentionsAlice(message: Any) -> bool:
            if message.role != "user":
                return False
            if isinstance(message.content, str):
                return "Alice" in message.content
            return any(block.type == "text" and "Alice" in block.text for block in message.content)

        hasAlice = any(mentionsAlice(message) for message in context.messages)
        return fauxAssistantMessage("Your name is Alice." if hasAlice else "I do not know your name.")

    faux.setResponses([fauxAssistantMessage("Nice to meet you, Alice."), respond])
    await multiTurnConversation(faux.getModel())


async def test_preserves_thinking_content_blocks():
    faux = createFauxRegistration({"models": [{"id": "faux-reasoning", "reasoning": True}]})
    faux.setResponses([fauxAssistantMessage([fauxThinking("step by step"), fauxText("4")])])

    agent = Agent(
        streamFn=streamSimple,
        initialState={
            "systemPrompt": "You are a helpful assistant.",
            "model": faux.getModel(),
            "thinkingLevel": "low",
            "tools": [],
        },
    )

    await agent.prompt("What is 2+2?")

    assistantMessage = agent.state.messages[2]
    assert assistantMessage.role == "assistant"
    assert [block.model_dump(exclude_none=True) for block in assistantMessage.content] == [
        {"type": "thinking", "thinking": "step by step"},
        {"type": "text", "text": "4"},
    ]


# --- Agent.continue() with faux provider ----------------------------------------------------


async def test_continue_throws_when_no_messages_in_context():
    faux = createFauxRegistration()
    agent = Agent(streamFn=streamSimple, initialState={"systemPrompt": "Test", "model": faux.getModel()})

    with pytest.raises(Exception, match="No messages to continue from"):
        await agent.continue_()


async def test_continue_throws_when_last_message_is_assistant():
    faux = createFauxRegistration()
    model = faux.getModel()
    agent = Agent(streamFn=streamSimple, initialState={"systemPrompt": "Test", "model": model})

    assistantMessage = AssistantMessage.model_validate(
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "Hello"}],
            "api": model.api,
            "provider": model.provider,
            "model": model.id,
            "usage": _usage(),
            "stopReason": "stop",
            "timestamp": 0,
        }
    )
    agent.state.messages = [assistantMessage]

    with pytest.raises(Exception, match="Cannot continue from message role: assistant"):
        await agent.continue_()


async def test_continues_and_gets_a_response_when_last_message_is_user():
    faux = createFauxRegistration()
    faux.setResponses([fauxAssistantMessage("HELLO WORLD")])
    agent = Agent(
        streamFn=streamSimple,
        initialState={
            "systemPrompt": "You are a helpful assistant. Follow instructions exactly.",
            "model": faux.getModel(),
            "thinkingLevel": "off",
            "tools": [],
        },
    )

    userMessage = UserMessage.model_validate(
        {"role": "user", "content": [{"type": "text", "text": "Say exactly: HELLO WORLD"}], "timestamp": 0}
    )
    agent.state.messages = [userMessage]

    await agent.continue_()

    assert agent.state.isStreaming is False
    assert len(agent.state.messages) == 2
    assert agent.state.messages[0].role == "user"
    assert agent.state.messages[1].role == "assistant"

    assistantMsg = agent.state.messages[1]
    assert "HELLO WORLD" in getTextContent(assistantMsg).upper()


async def test_continues_and_processes_tool_results():
    faux = createFauxRegistration()
    model = faux.getModel()
    faux.setResponses([fauxAssistantMessage("The answer is 8.")])
    agent = Agent(
        streamFn=streamSimple,
        initialState={
            "systemPrompt": "You are a helpful assistant. After getting a calculation result, state the answer clearly.",
            "model": model,
            "thinkingLevel": "off",
            "tools": [calculateTool],
        },
    )

    userMessage = UserMessage.model_validate(
        {"role": "user", "content": [{"type": "text", "text": "What is 5 + 3?"}], "timestamp": 0}
    )
    assistantMessage = AssistantMessage.model_validate(
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me calculate that."},
                {"type": "toolCall", "id": "calc-1", "name": "calculate", "arguments": {"expression": "5 + 3"}},
            ],
            "api": model.api,
            "provider": model.provider,
            "model": model.id,
            "usage": _usage(),
            "stopReason": "toolUse",
            "timestamp": 0,
        }
    )
    toolResult = ToolResultMessage.model_validate(
        {
            "role": "toolResult",
            "toolCallId": "calc-1",
            "toolName": "calculate",
            "content": [{"type": "text", "text": "5 + 3 = 8"}],
            "isError": False,
            "timestamp": 0,
        }
    )

    agent.state.messages = [userMessage, assistantMessage, toolResult]

    await agent.continue_()

    assert agent.state.isStreaming is False
    assert len(agent.state.messages) >= 4

    lastMessage = agent.state.messages[-1]
    assert lastMessage.role == "assistant"
    assert "8" in getTextContent(lastMessage)

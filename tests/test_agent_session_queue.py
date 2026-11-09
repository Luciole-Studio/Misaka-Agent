"""Translated from pi's ``packages/coding-agent/test/suite/agent-session-queue.test.ts`` (v1.1.0)."""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.providers.faux import fauxAssistantMessage, fauxToolCall
from misaka.ai.types import TextContent
from misaka.utils.values import read_field
from tests.suite_harness import (
    createHarness,
    getAssistantTexts,
    getMessageText,
    getUserTexts,
)


def _waitTool(released: asyncio.Event) -> AgentTool:
    async def execute(*_args: Any, **_kwargs: Any) -> AgentToolResult:
        await released.wait()
        return AgentToolResult(content=[TextContent(text="released")], details={})

    return AgentTool(
        name="wait",
        label="Wait",
        description="Wait for release",
        parameters={"type": "object", "properties": {}},
        execute=execute,
    )


@asynccontextmanager
async def createWaitingHarness(*, tools: list[Any] | None = None, extensionFactories: list[Any] | None = None):
    released = asyncio.Event()
    started = asyncio.Event()
    async with createHarness(tools=[_waitTool(released), *(tools or [])], extensionFactories=extensionFactories) as harness:

        def onEvent(event: Any) -> None:
            if read_field(event, "type") == "tool_execution_start" and read_field(event, "toolName") == "wait":
                started.set()

        unsubscribe = harness.session.subscribe(onEvent)
        waiting = SimpleNamespace(
            harness=harness,
            releaseToolExecution=released.set,
            waitForToolStart=started.wait,
            promptPromise=None,
        )
        try:
            yield waiting
        finally:
            unsubscribe()
            released.set()
            if waiting.promptPromise is not None and not waiting.promptPromise.done():
                await asyncio.gather(waiting.promptPromise, return_exceptions=True)


def _start(waiting: SimpleNamespace) -> asyncio.Future[Any]:
    """``promptPromise: harness.session.prompt("start")``, started once the responses are set."""
    waiting.promptPromise = asyncio.ensure_future(waiting.harness.session.prompt("start"))
    return waiting.promptPromise


def _userTexts(context: Any) -> list[str]:
    return [getMessageText(message) for message in context.messages if read_field(message, "role") == "user"]


def _hasUserText(context: Any, text: str) -> bool:
    return any(
        read_field(message, "role") == "user"
        and not isinstance(read_field(message, "content"), str)
        and any(read_field(part, "type") == "text" and read_field(part, "text") == text for part in read_field(message, "content"))
        for message in context.messages
    )


def _command(pi: Any, commandRuns: list[str] | None = None) -> None:
    async def handler(args: str, _ctx: Any = None) -> None:
        if commandRuns is not None:
            commandRuns.append(args)

    pi.registerCommand("testcmd", {"description": "Test command", "handler": handler})


# --- AgentSession queue characterization --------------------------------------------------------


async def test_dispatches_extension_commands_immediately_when_prompted_while_idle():
    commandRuns: list[str] = []
    async with createHarness(extensionFactories=[lambda pi: _command(pi, commandRuns)]) as harness:
        await harness.session.prompt("/testcmd hello world")

        assert commandRuns == ["hello world"]
        assert harness.getPendingResponseCount() == 0
        assert harness.session.messages == []


async def test_delivers_extension_origin_steering_messages_before_the_next_llm_call():
    api: dict[str, Any] = {}
    async with createWaitingHarness(extensionFactories=[lambda pi: api.setdefault("pi", pi)]) as waiting:
        harness = waiting.harness

        def second(context: Any, *_: Any) -> Any:
            sawSteer = any(
                read_field(message, "role") == "user" and getMessageText(message) == "steer now"
                for message in context.messages
            )
            return fauxAssistantMessage("saw steer" if sawSteer else "missing steer")

        harness.setResponses([fauxAssistantMessage(fauxToolCall("wait", {}), {"stopReason": "toolUse"}), second])
        promptPromise = _start(waiting)

        await waiting.waitForToolStart()
        await asyncio.sleep(0)

        api["pi"].sendUserMessage("steer now", {"deliverAs": "steer"})
        waiting.releaseToolExecution()
        await promptPromise

        assert getUserTexts(harness) == ["start", "steer now"]
        assert "saw steer" in getAssistantTexts(harness)


async def test_delivers_follow_up_messages_only_after_the_current_run_finishes():
    async with createWaitingHarness() as waiting:
        harness = waiting.harness
        assistantSeenBeforeFollowUp: list[str] = []

        def second(context: Any, *_: Any) -> Any:
            assistantSeenBeforeFollowUp.extend(
                getMessageText(message) for message in context.messages if read_field(message, "role") == "assistant"
            )
            return fauxAssistantMessage("follow-up response")

        harness.setResponses([fauxAssistantMessage(fauxToolCall("wait", {}), {"stopReason": "toolUse"}), second])
        promptPromise = _start(waiting)

        await waiting.waitForToolStart()
        await harness.session.followUp("after current run")
        waiting.releaseToolExecution()
        await promptPromise

        assert getUserTexts(harness) == ["start", "after current run"]
        assert "" in assistantSeenBeforeFollowUp
        assert "follow-up response" in getAssistantTexts(harness)


# Regression test for #8718.
async def test_runs_direct_steering_and_follow_up_messages_through_input_handlers():
    inputEvents: list[dict[str, Any]] = []

    def extension(pi: Any) -> None:
        def onInput(event: Any, _ctx: Any) -> Any:
            inputEvents.append({
                "text": read_field(event, "text"),
                "source": read_field(event, "source"),
                "streamingBehavior": read_field(event, "streamingBehavior"),
            })
            if read_field(event, "text").startswith("handle"):
                return {"action": "handled"}
            return {"action": "transform", "text": f"transformed: {read_field(event, 'text')}"}

        pi.on("input", onInput)

    async with createWaitingHarness(extensionFactories=[extension]) as waiting:
        harness = waiting.harness
        harness.setResponses([
            fauxAssistantMessage(fauxToolCall("wait", {}), {"stopReason": "toolUse"}),
            fauxAssistantMessage("steered"),
            fauxAssistantMessage("followed up"),
        ])
        promptPromise = _start(waiting)

        await waiting.waitForToolStart()
        inputEvents.clear()
        try:
            await harness.session.steer("steer me", None, {"source": "rpc"})
            await harness.session.steer("handle steer", None, {"source": "rpc"})
            await harness.session.followUp("follow me", None, {"source": "rpc"})
            await harness.session.followUp("handle follow", None, {"source": "rpc"})

            assert inputEvents == [
                {"text": "steer me", "source": "rpc", "streamingBehavior": "steer"},
                {"text": "handle steer", "source": "rpc", "streamingBehavior": "steer"},
                {"text": "follow me", "source": "rpc", "streamingBehavior": "followUp"},
                {"text": "handle follow", "source": "rpc", "streamingBehavior": "followUp"},
            ]
            assert harness.session.getSteeringMessages() == ["transformed: steer me"]
            assert harness.session.getFollowUpMessages() == ["transformed: follow me"]
        finally:
            waiting.releaseToolExecution()
        await promptPromise


async def test_delivers_multiple_steering_messages_in_order_in_one_at_a_time_mode():
    async with createWaitingHarness() as waiting:
        harness = waiting.harness
        harness.setResponses([
            fauxAssistantMessage(fauxToolCall("wait", {}), {"stopReason": "toolUse"}),
            fauxAssistantMessage("handled steer 1"),
            fauxAssistantMessage("handled steer 2"),
        ])
        promptPromise = _start(waiting)

        await waiting.waitForToolStart()
        await harness.session.steer("steer 1")
        await harness.session.steer("steer 2")
        waiting.releaseToolExecution()
        await promptPromise

        assert getUserTexts(harness) == ["start", "steer 1", "steer 2"]
        assert getAssistantTexts(harness) == ["", "handled steer 1", "handled steer 2"]


async def test_delivers_multiple_follow_up_messages_in_order_in_one_at_a_time_mode():
    async with createWaitingHarness() as waiting:
        harness = waiting.harness
        harness.setResponses([
            fauxAssistantMessage(fauxToolCall("wait", {}), {"stopReason": "toolUse"}),
            fauxAssistantMessage("original turn complete"),
            fauxAssistantMessage("handled follow-up 1"),
            fauxAssistantMessage("handled follow-up 2"),
        ])
        promptPromise = _start(waiting)

        await waiting.waitForToolStart()
        await harness.session.followUp("follow-up 1")
        await harness.session.followUp("follow-up 2")
        waiting.releaseToolExecution()
        await promptPromise

        assert getUserTexts(harness) == ["start", "follow-up 1", "follow-up 2"]
        assert getAssistantTexts(harness) == ["", "original turn complete", "handled follow-up 1", "handled follow-up 2"]


async def test_delivers_all_steering_messages_in_one_batch_in_all_mode():
    async with createWaitingHarness() as waiting:
        harness = waiting.harness
        harness.session.setSteeringMode("all")
        batched: list[str] = []

        def second(context: Any, *_: Any) -> Any:
            batched[:] = _userTexts(context)
            return fauxAssistantMessage("batched steer response")

        harness.setResponses([fauxAssistantMessage(fauxToolCall("wait", {}), {"stopReason": "toolUse"}), second])
        promptPromise = _start(waiting)

        await waiting.waitForToolStart()
        await harness.session.steer("steer 1")
        await harness.session.steer("steer 2")
        waiting.releaseToolExecution()
        await promptPromise

        assert batched == ["start", "steer 1", "steer 2"]
        assert getAssistantTexts(harness) == ["", "batched steer response"]


async def test_delivers_all_follow_up_messages_in_one_batch_in_all_mode():
    async with createWaitingHarness() as waiting:
        harness = waiting.harness
        harness.session.setFollowUpMode("all")
        batched: list[str] = []

        def third(context: Any, *_: Any) -> Any:
            batched[:] = _userTexts(context)
            return fauxAssistantMessage("batched follow-up response")

        harness.setResponses([
            fauxAssistantMessage(fauxToolCall("wait", {}), {"stopReason": "toolUse"}),
            fauxAssistantMessage("original turn complete"),
            third,
        ])
        promptPromise = _start(waiting)

        await waiting.waitForToolStart()
        await harness.session.followUp("follow-up 1")
        await harness.session.followUp("follow-up 2")
        waiting.releaseToolExecution()
        await promptPromise

        assert batched == ["start", "follow-up 1", "follow-up 2"]
        assert getAssistantTexts(harness) == ["", "original turn complete", "batched follow-up response"]


async def test_queues_custom_messages_with_deliver_as_steer_while_streaming():
    async with createWaitingHarness() as waiting:
        harness = waiting.harness
        seen: dict[str, bool] = {"custom": False}

        def second(context: Any, *_: Any) -> Any:
            seen["custom"] = _hasUserText(context, "steer custom")
            return fauxAssistantMessage("done")

        harness.setResponses([fauxAssistantMessage(fauxToolCall("wait", {}), {"stopReason": "toolUse"}), second])
        promptPromise = _start(waiting)

        await waiting.waitForToolStart()
        await harness.session.sendCustomMessage(
            {"customType": "queue-test", "content": "steer custom", "display": True, "details": {"value": 1}},
            {"deliverAs": "steer"},
        )
        waiting.releaseToolExecution()
        await promptPromise

        assert seen["custom"] is True
        assert any(
            read_field(message, "role") == "custom" and read_field(message, "customType") == "queue-test"
            for message in harness.session.messages
        )


async def test_queues_custom_messages_with_deliver_as_follow_up_while_streaming():
    async with createWaitingHarness() as waiting:
        harness = waiting.harness
        seen: dict[str, bool] = {"custom": False}

        def third(context: Any, *_: Any) -> Any:
            seen["custom"] = _hasUserText(context, "follow-up custom")
            return fauxAssistantMessage("done")

        harness.setResponses([
            fauxAssistantMessage(fauxToolCall("wait", {}), {"stopReason": "toolUse"}),
            fauxAssistantMessage("original turn complete"),
            third,
        ])
        promptPromise = _start(waiting)

        await waiting.waitForToolStart()
        await harness.session.sendCustomMessage(
            {"customType": "queue-test", "content": "follow-up custom", "display": True, "details": {"value": 1}},
            {"deliverAs": "followUp"},
        )
        waiting.releaseToolExecution()
        await promptPromise

        assert seen["custom"] is True
        assert any(
            read_field(message, "role") == "custom" and read_field(message, "customType") == "queue-test"
            for message in harness.session.messages
        )


async def test_injects_next_turn_custom_messages_into_the_next_prompt():
    async with createHarness() as harness:
        seen: dict[str, bool] = {"custom": False}

        await harness.session.sendCustomMessage(
            {"customType": "next-turn", "content": "carry this", "display": True, "details": {}},
            {"deliverAs": "nextTurn"},
        )

        def first(context: Any, *_: Any) -> Any:
            seen["custom"] = _hasUserText(context, "carry this")
            return fauxAssistantMessage("done")

        harness.setResponses([first])

        await harness.session.prompt("normal prompt")

        assert seen["custom"] is True
        assert [read_field(message, "role") for message in harness.session.messages] == [
            "system",
            "user",
            "custom",
            "assistant",
        ]


async def test_updates_pending_message_count_and_removes_queued_text_before_message_start_is_emitted():
    async with createWaitingHarness() as waiting:
        harness = waiting.harness
        countsAtQueuedMessageStart: list[int] = []
        harness.setResponses([
            fauxAssistantMessage(fauxToolCall("wait", {}), {"stopReason": "toolUse"}),
            fauxAssistantMessage("done"),
        ])

        def onEvent(event: Any) -> None:
            message = read_field(event, "message")
            if (
                read_field(event, "type") == "message_start"
                and read_field(message, "role") == "user"
                and getMessageText(message) == "queued"
            ):
                countsAtQueuedMessageStart.append(harness.session.pendingMessageCount)

        harness.session.subscribe(onEvent)
        promptPromise = _start(waiting)

        await waiting.waitForToolStart()
        await harness.session.steer("queued")
        assert harness.session.pendingMessageCount == 1
        waiting.releaseToolExecution()
        await promptPromise

        assert countsAtQueuedMessageStart == [0]
        assert harness.session.pendingMessageCount == 0


QUEUE_ERROR = 'Extension command "/testcmd" cannot be queued. Use prompt() or execute the command when not streaming.'


async def test_throws_when_queueing_an_extension_command_with_steer():
    async with createHarness(extensionFactories=[_command]) as harness:
        with pytest.raises(Exception, match=re.escape(QUEUE_ERROR)):
            await harness.session.steer("/testcmd queued")


async def test_throws_when_queueing_an_extension_command_with_follow_up():
    async with createHarness(extensionFactories=[_command]) as harness:
        with pytest.raises(Exception, match=re.escape(QUEUE_ERROR)):
            await harness.session.followUp("/testcmd queued")


async def test_delivers_follow_ups_queued_during_agent_end():
    state = {"sent": False}

    def extension(pi: Any) -> None:
        async def onAgentEnd(_event: Any, _ctx: Any) -> None:
            if state["sent"]:
                return
            state["sent"] = True
            pi.sendUserMessage("conflict report", {"deliverAs": "followUp"})

        pi.on("agent_end", onAgentEnd)

    async with createHarness(extensionFactories=[extension]) as harness:
        harness.setResponses([fauxAssistantMessage("reply"), fauxAssistantMessage("follow-up reply")])

        await harness.session.prompt("hello")
        await harness.session.agent.waitForIdle()

        assert getUserTexts(harness) == ["hello", "conflict report"]

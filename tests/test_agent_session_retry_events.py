"""Translated from pi's ``packages/coding-agent/test/suite/agent-session-retry-events.test.ts`` (v1.1.0)."""

from __future__ import annotations

import asyncio
from typing import Any

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.providers.faux import fauxAssistantMessage, fauxThinking, fauxToolCall
from misaka.ai.types import TextContent
from misaka.utils.js import spawn
from misaka.utils.values import read_field
from tests.suite_harness import createHarness

RETRY = {"retry": {"enabled": True, "maxRetries": 3, "baseDelayMs": 1}}
_background: set[asyncio.Task[Any]] = set()


def normalizeEventOrder(events: list[Any]) -> list[str]:
    normalized: list[str] = []
    for event in events:
        type = read_field(event, "type")
        if type in ("message_start", "message_end"):
            label = f"{type}:{read_field(read_field(event, 'message'), 'role')}"
        elif type in ("tool_execution_start", "tool_execution_end"):
            label = f"{type}:{read_field(event, 'toolName')}"
        else:
            label = type
        if label == "message_update" and normalized and normalized[-1] == "message_update":
            continue
        normalized.append(label)
    return normalized


def _overloaded() -> Any:
    return fauxAssistantMessage("", {"stopReason": "error", "errorMessage": "overloaded_error"})


def _retryEvents(harness: Any) -> list[str]:
    events: list[str] = []

    def onEvent(event: Any) -> None:
        if read_field(event, "type") == "auto_retry_start":
            events.append(f"start:{read_field(event, 'attempt')}")
        if read_field(event, "type") == "auto_retry_end":
            events.append(f"end:{str(read_field(event, 'success')).lower()}")

    harness.session.subscribe(onEvent)
    return events


def _echoTool(toolRuns: list[str]) -> AgentTool:
    async def execute(_toolCallId: Any, params: Any, *_args: Any, **_kwargs: Any) -> AgentToolResult:
        text = str(read_field(params, "text") or "")
        toolRuns.append(text)
        return AgentToolResult(content=[TextContent(text=f"echo:{text}")], details={"text": text})

    return AgentTool(
        name="echo",
        label="Echo",
        description="Echo text back",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}},
        execute=execute,
    )


# --- AgentSession retry and event characterization ----------------------------------------------


async def test_retries_after_a_transient_error_and_succeeds():
    async with createHarness(settings=RETRY) as harness:
        retryEvents = _retryEvents(harness)
        harness.setResponses([_overloaded(), fauxAssistantMessage("recovered")])

        await harness.session.prompt("test")

        assert retryEvents == ["start:1", "end:true"]
        assert [read_field(event, "willRetry") for event in harness.eventsOfType("agent_end")] == [True, False]
        assert harness.faux.state.callCount == 2
        assert harness.session.isRetrying is False


async def test_retries_multiple_transient_failures_and_succeeds_on_the_final_attempt():
    async with createHarness(settings=RETRY) as harness:
        retryEvents = _retryEvents(harness)
        harness.setResponses([_overloaded(), _overloaded(), fauxAssistantMessage("success")])

        await harness.session.prompt("test")

        assert retryEvents == ["start:1", "start:2", "end:true"]
        assert harness.faux.state.callCount == 3


# Regression test for #9340.
async def test_finalizes_retry_state_when_abort_is_requested_after_a_retry_attempt_fails():
    async with createHarness(settings={"retry": {"enabled": True, "maxRetries": 3, "baseDelayMs": 0}}) as harness:
        harness.setResponses([_overloaded(), _overloaded()])
        errorCount = {"n": 0}

        def onEvent(event: Any) -> None:
            message = read_field(event, "message")
            if read_field(event, "type") != "message_end" or read_field(message, "role") != "assistant":
                return
            if read_field(message, "stopReason") == "error":
                errorCount["n"] += 1
                if errorCount["n"] == 2:
                    spawn(harness.session.abort(), _background)

        harness.session.subscribe(onEvent)

        await harness.session.prompt("test")

        assert harness.session.retryAttempt == 0
        assert read_field(harness.eventsOfType("agent_end")[-1], "willRetry") is False
        last = harness.eventsOfType("auto_retry_end")[-1]
        assert (read_field(last, "success"), read_field(last, "attempt"), read_field(last, "finalError")) == (
            False,
            1,
            "Retry cancelled",
        )


async def test_exhausts_max_retries_and_emits_a_failure_event():
    async with createHarness(settings={"retry": {"enabled": True, "maxRetries": 2, "baseDelayMs": 1}}) as harness:
        retryEvents = _retryEvents(harness)
        harness.setResponses([_overloaded(), _overloaded(), _overloaded()])

        await harness.session.prompt("test")

        assert retryEvents == ["start:1", "start:2", "end:false"]
        assert [read_field(event, "willRetry") for event in harness.eventsOfType("agent_end")] == [True, True, False]
        assert harness.faux.state.callCount == 3
        assert harness.session.isRetrying is False


async def test_prompt_waits_for_retry_completion_even_when_assistant_message_end_handling_is_delayed():
    def extension(pi: Any) -> None:
        async def onMessageEnd(event: Any, _ctx: Any) -> None:
            if read_field(read_field(event, "message"), "role") == "assistant":
                await asyncio.sleep(0.04)

        pi.on("message_end", onMessageEnd)

    async with createHarness(settings=RETRY, extensionFactories=[extension]) as harness:
        harness.setResponses([_overloaded(), fauxAssistantMessage("recovered")])

        await harness.session.prompt("test")

        assert harness.faux.state.callCount == 2
        assert harness.session.isRetrying is False


async def test_does_not_retry_when_retry_is_disabled():
    async with createHarness(settings={"retry": {"enabled": False}}) as harness:
        harness.setResponses([_overloaded()])

        await harness.session.prompt("test")

        assert harness.faux.state.callCount == 1
        assert harness.eventsOfType("auto_retry_start") == []


async def test_does_not_retry_non_retryable_errors():
    async with createHarness(settings=RETRY) as harness:
        harness.setResponses([fauxAssistantMessage("", {"stopReason": "error", "errorMessage": "invalid_api_key"})])

        await harness.session.prompt("test")

        assert harness.faux.state.callCount == 1
        assert harness.eventsOfType("auto_retry_start") == []


async def test_cancels_retry_sleep_when_abort_retry_is_called():
    async with createHarness(settings={"retry": {"enabled": True, "maxRetries": 3, "baseDelayMs": 100}}) as harness:
        harness.setResponses([_overloaded()])
        sawRetryStart = asyncio.Event()
        unsubscribe = harness.session.subscribe(
            lambda event: sawRetryStart.set() if read_field(event, "type") == "auto_retry_start" else None
        )

        promptPromise = asyncio.ensure_future(harness.session.prompt("test"))
        await sawRetryStart.wait()
        unsubscribe()
        harness.session.abortRetry()
        await promptPromise

        assert harness.session.isRetrying is False
        assert "Retry cancelled" in [read_field(event, "finalError") for event in harness.eventsOfType("auto_retry_end")]
        assert harness.faux.state.callCount == 1


async def test_waits_for_the_full_loop_when_retry_recovery_produces_tool_calls():
    toolRuns: list[str] = []
    async with createHarness(tools=[_echoTool(toolRuns)], settings=RETRY) as harness:
        harness.setResponses([
            _overloaded(),
            fauxAssistantMessage([fauxToolCall("echo", {"text": "hello"})], {"stopReason": "toolUse"}),
            fauxAssistantMessage("final answer"),
        ])

        await harness.session.prompt("test")

        assert harness.faux.state.callCount == 3
        assert toolRuns == ["hello"]
        assert harness.session.isStreaming is False
        await harness.session.prompt("follow-up")
        assert harness.faux.state.callCount == 4


async def test_emits_extension_events_before_public_event_subscribers():
    order: list[str] = []

    def extension(pi: Any) -> None:
        async def record(event: Any, _ctx: Any) -> None:
            order.append(f"extension:{read_field(event, 'type')}:{read_field(read_field(event, 'message'), 'role')}")

        pi.on("message_start", record)
        pi.on("message_end", record)

    async with createHarness(extensionFactories=[extension]) as harness:

        def onEvent(event: Any) -> None:
            if read_field(event, "type") in ("message_start", "message_end"):
                order.append(f"public:{read_field(event, 'type')}:{read_field(read_field(event, 'message'), 'role')}")

        harness.session.subscribe(onEvent)
        harness.setResponses([fauxAssistantMessage("done")])

        await harness.session.prompt("hi")

        assert order == [
            "extension:message_start:system",
            "public:message_start:system",
            "extension:message_end:system",
            "public:message_end:system",
            "extension:message_start:user",
            "public:message_start:user",
            "extension:message_end:user",
            "public:message_end:user",
            "extension:message_start:assistant",
            "public:message_start:assistant",
            "extension:message_end:assistant",
            "public:message_end:assistant",
        ]


async def test_emits_the_expected_event_order_for_a_single_prompt():
    async with createHarness() as harness:
        harness.setResponses([fauxAssistantMessage("hello")])

        await harness.session.prompt("hi")

        assert normalizeEventOrder(harness.events) == [
            "agent_start",
            "turn_start",
            "message_start:system",
            "message_end:system",
            "message_start:user",
            "message_end:user",
            "message_start:assistant",
            "message_update",
            "message_end:assistant",
            "turn_end",
            "agent_end",
            "agent_settled",
        ]


async def test_emits_the_expected_event_order_for_a_tool_call_turn():
    toolRuns: list[str] = []
    async with createHarness(tools=[_echoTool(toolRuns)]) as harness:
        harness.setResponses([
            fauxAssistantMessage([fauxToolCall("echo", {"text": "hello"})], {"stopReason": "toolUse"}),
            fauxAssistantMessage("done"),
        ])

        await harness.session.prompt("hi")

        assert toolRuns == ["hello"]
        assert normalizeEventOrder(harness.events) == [
            "agent_start",
            "turn_start",
            "message_start:system",
            "message_end:system",
            "message_start:user",
            "message_end:user",
            "message_start:assistant",
            "message_update",
            "message_end:assistant",
            "tool_execution_start:echo",
            "tool_execution_end:echo",
            "message_start:toolResult",
            "message_end:toolResult",
            "turn_end",
            "turn_start",
            "message_start:assistant",
            "message_update",
            "message_end:assistant",
            "turn_end",
            "agent_end",
            "agent_settled",
        ]


async def test_emits_streaming_deltas_for_text_thinking_and_tool_calls_in_message_update_events():
    async with createHarness() as harness:
        harness.setResponses([
            fauxAssistantMessage(
                [fauxThinking("plan"), TextContent(text="answer"), fauxToolCall("echo", {"text": "hello"})],
                {"stopReason": "toolUse"},
            )
        ])

        try:
            await harness.session.prompt("hi")
        except Exception:  # noqa: BLE001, S110 - upstream's `.catch(() => {})`
            pass

        updateTypes = [
            read_field(read_field(event, "assistantMessageEvent"), "type") for event in harness.eventsOfType("message_update")
        ]
        assert "thinking_delta" in updateTypes
        assert "text_delta" in updateTypes
        assert "toolcall_delta" in updateTypes


async def test_emits_agent_end_for_error_responses():
    async with createHarness() as harness:
        harness.setResponses([fauxAssistantMessage("", {"stopReason": "error", "errorMessage": "broken"})])

        await harness.session.prompt("hi")

        assert len(harness.eventsOfType("agent_end")) == 1
        assert read_field(harness.events[-1], "type") == "agent_settled"


async def test_emits_agent_end_for_aborted_runs_and_persists_the_aborted_assistant_message():
    async with createHarness() as harness:
        harness.setResponses([fauxAssistantMessage("x" * 20_000)])
        sawMessageUpdate = asyncio.Event()
        unsubscribe = harness.session.subscribe(
            lambda event: sawMessageUpdate.set() if read_field(event, "type") == "message_update" else None
        )

        promptPromise = asyncio.ensure_future(harness.session.prompt("hi"))
        await sawMessageUpdate.wait()
        unsubscribe()
        await harness.session.abort()
        await promptPromise

        assert len(harness.eventsOfType("agent_end")) == 1
        assert read_field(harness.events[-1], "type") == "agent_settled"
        lastMessage = harness.session.messages[-1]
        assert read_field(lastMessage, "role") == "assistant"
        assert read_field(lastMessage, "stopReason") == "aborted"

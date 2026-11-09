"""Translated from pi's ``packages/coding-agent/test/suite/regressions/6363-agent-settled-event.test.ts`` and the
#10607 case of ``test/suite/agent-session-boundaries.test.ts`` (v1.1.0)."""

from __future__ import annotations

import asyncio
from typing import Any

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.providers.faux import fauxAssistantMessage, fauxToolCall
from misaka.ai.types import TextContent
from misaka.utils.values import read_field
from tests.suite_harness import createHarness, getUserTexts


def createWaitTool(released: asyncio.Event) -> AgentTool:
    async def execute(*_args: Any, **_kwargs: Any) -> AgentToolResult:
        await released.wait()
        return AgentToolResult(content=[TextContent(text="released")], details={})

    return AgentTool(
        name="wait",
        label="Wait",
        description="Wait until released",
        parameters={"type": "object", "properties": {}},
        execute=execute,
    )


# --- regression #6363: agent settled event and idle waiting ---------------------------------------


async def test_emits_one_agent_settled_event_after_automatic_retry_finishes():
    extensionEvents: list[str] = []
    publicEvents: list[str] = []

    def extension(pi: Any) -> None:
        pi.on("agent_end", lambda _event, _ctx: extensionEvents.append("agent_end"))
        pi.on("agent_settled", lambda _event, ctx: extensionEvents.append(f"agent_settled:{str(ctx.isIdle()).lower()}"))

    async with createHarness(
        settings={"retry": {"enabled": True, "maxRetries": 3, "baseDelayMs": 1}},
        extensionFactories=[extension],
    ) as harness:
        harness.session.subscribe(
            lambda event: publicEvents.append("agent_settled") if read_field(event, "type") == "agent_settled" else None
        )
        harness.setResponses([
            fauxAssistantMessage("", {"stopReason": "error", "errorMessage": "overloaded_error"}),
            fauxAssistantMessage("recovered"),
        ])

        await harness.session.prompt("test")

        assert [read_field(event, "willRetry") for event in harness.eventsOfType("agent_end")] == [True, False]
        assert harness.eventsOfType("agent_settled") == [{"type": "agent_settled", "aborted": False}]
        assert extensionEvents == ["agent_end", "agent_end", "agent_settled:true"]
        assert publicEvents == ["agent_settled"]


async def test_settles_only_after_follow_ups_queued_by_agent_end_handlers_run():
    queuedFollowUp = False
    settledIdleStates: list[bool] = []

    def extension(pi: Any) -> None:
        def onAgentEnd(_event: Any, _ctx: Any) -> None:
            nonlocal queuedFollowUp
            if queuedFollowUp:
                return
            queuedFollowUp = True
            pi.sendUserMessage("status follow-up", {"deliverAs": "followUp"})

        pi.on("agent_end", onAgentEnd)
        pi.on("agent_settled", lambda _event, ctx: settledIdleStates.append(ctx.isIdle()))

    async with createHarness(extensionFactories=[extension]) as harness:
        harness.setResponses([fauxAssistantMessage("first"), fauxAssistantMessage("second")])

        await harness.session.prompt("hello")

        assert getUserTexts(harness) == ["hello", "status follow-up"]
        assert len(harness.eventsOfType("agent_end")) == 2
        assert len(harness.eventsOfType("agent_settled")) == 1
        assert settledIdleStates == [True]


async def test_extension_command_wait_for_idle_waits_for_session_level_settlement():
    released = asyncio.Event()
    commandStarted = asyncio.Event()
    commandResults: list[bool] = []

    def extension(pi: Any) -> None:
        async def handler(_args: Any, ctx: Any) -> None:
            commandStarted.set()
            await ctx.waitForIdle()
            commandResults.append(ctx.isIdle())

        pi.registerCommand("after-idle", {"description": "Wait for idle", "handler": handler})

    async with createHarness(tools=[createWaitTool(released)], extensionFactories=[extension]) as harness:

        async def cancelledFalse(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"cancelled": False}

        async def reload(*_args: Any, **_kwargs: Any) -> None:
            return None

        await harness.session.bindExtensions({
            "commandContextActions": {
                "waitForIdle": lambda: harness.session.waitForIdle(),
                "newSession": cancelledFalse,
                "fork": cancelledFalse,
                "navigateTree": cancelledFalse,
                "switchSession": cancelledFalse,
                "reload": reload,
            },
        })
        toolStarted = asyncio.Event()

        def onEvent(event: Any) -> None:
            if read_field(event, "type") == "tool_execution_start" and read_field(event, "toolName") == "wait":
                toolStarted.set()

        unsubscribe = harness.session.subscribe(onEvent)
        harness.setResponses([
            fauxAssistantMessage(fauxToolCall("wait", {}), {"stopReason": "toolUse"}),
            fauxAssistantMessage("done"),
        ])

        promptTask = asyncio.ensure_future(harness.session.prompt("start"))
        await toolStarted.wait()
        unsubscribe()
        commandTask = asyncio.ensure_future(harness.session.prompt("/after-idle"))
        await commandStarted.wait()
        await asyncio.sleep(0)
        assert commandTask.done() is False

        released.set()
        await asyncio.gather(promptTask, commandTask)

        assert commandResults == [True]
        assert len(harness.eventsOfType("agent_settled")) == 1


# --- AgentSession actionable boundaries -------------------------------------------------------------


async def test_commits_pre_settlement_drafts_but_suppresses_continuation_when_aborted_during_the_hook():
    started = asyncio.Event()
    release = asyncio.Event()

    def extension(pi: Any) -> None:
        async def beforeSettle(_event: Any, _ctx: Any) -> dict[str, Any]:
            started.set()
            await release.wait()
            return {
                "entries": [{"type": "custom", "customType": "committed-after-abort", "data": True}],
                "continue": True,
            }

        pi.on("agent_before_settle", beforeSettle)

    async with createHarness(extensionFactories=[extension]) as harness:
        harness.setResponses([fauxAssistantMessage("first"), fauxAssistantMessage("must not run")])

        promptTask = asyncio.ensure_future(harness.session.prompt("start"))
        await started.wait()
        abortTask = asyncio.ensure_future(harness.session.abort())
        release.set()
        await asyncio.gather(promptTask, abortTask)

        assert harness.faux.state.callCount == 1
        assert any(
            read_field(entry, "type") == "custom"
            and read_field(entry, "customType") == "committed-after-abort"
            and read_field(entry, "data") is True
            for entry in harness.sessionManager.getEntries()
        )
        # #10607: settlement reports the abort although the last response succeeded.
        assert harness.eventsOfType("agent_settled") == [{"type": "agent_settled", "aborted": True}]

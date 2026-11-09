"""Translated from pi's ``packages/coding-agent/test/suite/regressions/`` ``8423-extension-factory-failure.test.ts``,
``8537-custom-message-tool-result-ordering.test.ts`` and ``8724-in-memory-fork-active-tool.test.ts`` (v1.1.0)."""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.providers.faux import fauxAssistantMessage, fauxToolCall
from misaka.ai.types import TextContent
from misaka.core.agent_session_runtime import (
    AgentSessionRuntime,
    CreateAgentSessionRuntimeResult,
)
from misaka.core.agent_session_services import (
    AgentSessionServices,
    createAgentSessionFromServices,
)
from misaka.core.event_bus import createEventBus
from misaka.core.extensions.loader import (
    createExtensionRuntime,
    loadExtensionFromFactory,
)
from misaka.core.messages import convertToLlm
from misaka.utils.values import read_field
from tests.suite_harness import createHarness

# --- issue #8423 extension factory failure ------------------------------------------------------

providerConfig = {"baseUrl": "https://provider.test/v1", "apiKey": "provider-test-key"}


def _pendingNames(runtime: Any) -> list[str]:
    return [registration.name for registration in runtime.pendingProviderRegistrations]


async def test_discards_runtime_changes_and_disables_the_failed_api():
    runtime = createExtensionRuntime()
    eventBus = createEventBus()
    state: dict[str, Any] = {"eventCalls": 0}

    await loadExtensionFromFactory(
        lambda pi: pi.registerProvider("working-provider", providerConfig), os.getcwd(), eventBus, runtime, "<working>"
    )

    def failing(pi: Any) -> None:
        state["capturedApi"] = pi
        pi.events.on("factory-failure", lambda *_: state.__setitem__("eventCalls", state["eventCalls"] + 1))
        pi.registerFlag("failed-flag", {"type": "boolean", "default": True})
        state["flagDuringLoad"] = pi.getFlag("failed-flag")
        pi.unregisterProvider("working-provider")
        pi.registerProvider("failed-provider", providerConfig)
        raise RuntimeError("factory failed")

    with pytest.raises(Exception, match="factory failed"):
        await loadExtensionFromFactory(failing, os.getcwd(), eventBus, runtime, "<failing>")

    eventBus.emit("factory-failure", None)
    assert state["flagDuringLoad"] is True
    assert "failed-flag" not in runtime.flagValues
    assert _pendingNames(runtime) == ["working-provider"]
    assert state["eventCalls"] == 0
    assert state.get("capturedApi") is not None
    with pytest.raises(Exception, match='Extension "<failing>" failed to load and its API is no longer active.'):
        state["capturedApi"].registerFlag("late-flag", {"type": "boolean", "default": True})


async def test_does_not_discard_a_concurrently_loaded_factorys_provider():
    runtime = createExtensionRuntime()
    eventBus = createEventBus()
    waitBeforeFailure: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def failing(pi: Any) -> None:
        pi.registerProvider("failed-provider", providerConfig)
        await waitBeforeFailure
        raise RuntimeError("factory failed")

    # The load starts at once, as a JavaScript async call does, so the failing factory registers first.
    failingLoad = asyncio.Task(
        loadExtensionFromFactory(failing, os.getcwd(), eventBus, runtime, "<failing>"),
        loop=asyncio.get_running_loop(),
        eager_start=True,
    )

    await loadExtensionFromFactory(
        lambda pi: pi.registerProvider("working-provider", providerConfig), os.getcwd(), eventBus, runtime, "<working>"
    )
    waitBeforeFailure.set_result(None)

    with pytest.raises(Exception, match="factory failed"):
        await failingLoad
    assert _pendingNames(runtime) == ["working-provider"]


# --- #8537 custom messages injected during tool execution ---------------------------------------


def roles(messages: list[Any]) -> list[str]:
    return [read_field(message, "role") for message in messages]


def createSlowTool(notify: dict[str, Any]) -> AgentTool:
    async def execute(*_args: Any, **_kwargs: Any) -> AgentToolResult:
        # A background task (e.g. a subagent reply) notifies the session while the
        # tool is still running.
        if notify.get("call") is not None:
            await notify["call"]()
        return AgentToolResult(content=[TextContent(text="tool done")], details={})

    return AgentTool(
        name="wait",
        label="Wait",
        description="Wait for a background task",
        parameters={"type": "object", "properties": {}},
        execute=execute,
    )


def _notifier(harness: Any, notify: dict[str, Any]) -> None:
    notify["call"] = lambda: harness.session.sendCustomMessage(
        {"customType": "subagent-reply", "content": "subagent replied", "display": True}, {"triggerTurn": False}
    )


async def test_appends_the_message_after_the_turns_tool_results_instead_of_between_call_and_result():
    notify: dict[str, Any] = {}
    async with createHarness(tools=[createSlowTool(notify)]) as harness:
        _notifier(harness, notify)
        harness.setResponses([
            fauxAssistantMessage([fauxToolCall("wait", {})], {"stopReason": "toolUse"}),
            fauxAssistantMessage("done"),
        ])

        await harness.session.prompt("hi")

        assert roles(harness.session.messages) == ["system", "user", "assistant", "toolResult", "custom", "assistant"]


async def test_keeps_session_entries_and_message_events_in_the_same_order_as_agent_state():
    notify: dict[str, Any] = {}
    async with createHarness(tools=[createSlowTool(notify)]) as harness:
        _notifier(harness, notify)
        harness.setResponses([
            fauxAssistantMessage([fauxToolCall("wait", {})], {"stopReason": "toolUse"}),
            fauxAssistantMessage("done"),
        ])

        await harness.session.prompt("hi")

        entryKinds = [
            kind
            for entry in harness.sessionManager.getBranch()
            for kind in (
                [read_field(read_field(entry, "message"), "role")]
                if read_field(entry, "type") == "message"
                else ["custom"]
                if read_field(entry, "type") == "custom_message"
                else []
            )
        ]
        assert entryKinds == ["system", "user", "assistant", "toolResult", "custom", "assistant"]

        # message events must never describe a message the session tree does not contain yet
        messageStarts = [
            read_field(read_field(event, "message"), "role")
            for event in harness.events
            if read_field(event, "type") == "message_start"
        ]
        assert messageStarts == ["system", "user", "assistant", "toolResult", "custom", "assistant"]


async def test_produces_an_llm_history_where_every_tool_result_follows_its_tool_call():
    notify: dict[str, Any] = {}
    async with createHarness(tools=[createSlowTool(notify)]) as harness:
        _notifier(harness, notify)
        harness.setResponses([
            fauxAssistantMessage([fauxToolCall("wait", {})], {"stopReason": "toolUse"}),
            fauxAssistantMessage("done"),
            fauxAssistantMessage("second turn"),
        ])

        await harness.session.prompt("hi")
        await harness.session.prompt("and now?")

        llmMessages = convertToLlm(harness.session.messages)
        openToolCallIds: set[str] = set()
        for message in llmMessages:
            role = read_field(message, "role")
            if role == "assistant":
                openToolCallIds.clear()
                for block in read_field(message, "content"):
                    if read_field(block, "type") == "toolCall":
                        openToolCallIds.add(read_field(block, "id"))
                continue
            if role == "toolResult":
                assert read_field(message, "toolCallId") in openToolCallIds
                openToolCallIds.discard(read_field(message, "toolCallId"))
                continue
            openToolCallIds.clear()


# --- regression #8724: in-memory fork during an active tool turn --------------------------------


async def test_does_not_append_the_aborted_turn_to_the_replacement_session():
    toolStarted: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def execute(_toolCallId: str, _params: Any, signal: Any = None, *_: Any) -> AgentToolResult:
        aborted: asyncio.Future[AgentToolResult] = asyncio.get_running_loop().create_future()
        if not toolStarted.done():
            toolStarted.set_result(None)
        if signal is not None:
            signal.addEventListener(
                "abort",
                lambda: aborted.done()
                or aborted.set_result(AgentToolResult(content=[TextContent(text="tool aborted")], details={})),
                once=True,
            )
        return await aborted

    blockingTool = AgentTool(
        name="block",
        label="Block",
        description="Wait until aborted",
        parameters={"type": "object", "properties": {}},
        execute=execute,
    )
    async with createHarness(tools=[blockingTool]) as harness:
        services = AgentSessionServices(
            cwd=harness.tempDir,
            agentDir=harness.tempDir,
            modelRuntime=harness.session.modelRuntime,
            settingsManager=harness.settingsManager,
            resourceLoader=harness.session.resourceLoader,
            diagnostics=[],
        )

        async def createRuntime(options: Any) -> CreateAgentSessionRuntimeResult:
            result = await createAgentSessionFromServices({
                "services": services,
                "sessionManager": options["sessionManager"],
                "sessionStartEvent": options.get("sessionStartEvent"),
                "model": harness.getModel(),
                "noTools": "all",
            })
            return CreateAgentSessionRuntimeResult(
                session=result["session"],
                services=services,
                diagnostics=[],
                extensionsResult=result.get("extensionsResult"),
                modelFallbackMessage=result.get("modelFallbackMessage"),
            )

        runtime = AgentSessionRuntime(harness.session, services, createRuntime)
        try:
            harness.setResponses([
                fauxAssistantMessage("first response"),
                fauxAssistantMessage(fauxToolCall("block", {}), {"stopReason": "toolUse"}),
                fauxAssistantMessage("unused after abort"),
            ])
            await runtime.session.prompt("first prompt")
            forking = runtime.session.getUserMessagesForForking()
            firstUserEntryId = read_field(forking[0], "entryId") if forking else None
            assert firstUserEntryId is not None

            outgoingPrompt = asyncio.ensure_future(runtime.session.prompt("start blocking tool"))
            await toolStarted
            forkResult = await runtime.fork(firstUserEntryId)
            await outgoingPrompt
            await runtime.session.bindExtensions({})

            assert read_field(forkResult, "cancelled") is False
            assert read_field(forkResult, "selectedText") == "first prompt"
            assert roles(runtime.session.messages) == ["system"]
            assert [
                read_field(read_field(entry, "message"), "role")
                for entry in runtime.session.sessionManager.getEntries()
                if read_field(entry, "type") == "message"
            ] == ["system"]

            captured: dict[str, list[str]] = {"roles": []}

            def nextResponse(context: Any, *_: Any) -> Any:
                captured["roles"] = roles(context.messages)
                return fauxAssistantMessage("next response")

            harness.setResponses([nextResponse])
            await runtime.session.prompt("next prompt")

            assert captured["roles"] == ["system", "system", "user"]
        finally:
            if runtime.session is not harness.session:
                await runtime.dispose()

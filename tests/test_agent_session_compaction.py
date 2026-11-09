"""Translated from pi's ``packages/coding-agent/test/suite/agent-session-compaction.test.ts`` (v1.1.0).

MISAKA fork: ``_checkCompaction`` hands every threshold candidate to ``_runAutoCompaction``, which applies the threshold
itself after asking an installed context engine. Where pi spies on ``_runAutoCompaction`` and expects no call because
the threshold was not reached, these tests run it and check that no compaction starts.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.auth.types import ApiKeyAuth, AuthResult, ModelAuth, ProviderAuth
from misaka.ai.providers.faux import fauxAssistantMessage, fauxToolCall
from misaka.ai.types import AssistantMessage, DoneEvent, TextContent, Usage
from misaka.ai.utils.event_stream import createAssistantMessageEventStream
from misaka.ai.utils.transcript import getCurrentSystemPrompt, getCurrentTools
from misaka.core.compaction import estimateTokens
from misaka.modes.json_event import to_jsonable
from misaka.utils.js import jsonStringify
from misaka.utils.values import read_field
from tests.suite_harness import createHarness, getUserTexts


def nowMs() -> int:
    return int(time.time() * 1000)


def stringify(messages: Any) -> str:
    """`JSON.stringify(messages)`: the messages as the plain objects pi's are."""
    return jsonStringify(to_jsonable(messages))


def createUsage(totalTokens: int) -> Usage:
    return Usage.model_validate({
        "input": totalTokens,
        "output": 0,
        "cacheRead": 0,
        "cacheWrite": 0,
        "totalTokens": totalTokens,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
    })


def createAssistant(
    harness: Any,
    *,
    stopReason: str | None = None,
    errorMessage: str | None = None,
    totalTokens: int | None = None,
    timestamp: int | None = None,
) -> AssistantMessage:
    model = harness.getModel()
    return fauxAssistantMessage(
        "", {"stopReason": stopReason, "errorMessage": errorMessage, "timestamp": timestamp}
    ).model_copy(update={
        "api": model.api,
        "provider": model.provider,
        "model": model.id,
        "usage": createUsage(totalTokens if totalTokens is not None else 0),
    })


def useSummaryStreamFn(harness: Any, summary: str, onRequest: Any = None) -> Any:
    state = {"callCount": 0}

    def streamFunction(model: Any, context: Any, options: Any = None) -> Any:
        state["callCount"] += 1
        if onRequest is not None:
            onRequest(context, options)
        stream = createAssistantMessageEventStream()

        def respond() -> None:
            message = fauxAssistantMessage(summary).model_copy(update={
                "api": model.api,
                "provider": model.provider,
                "model": model.id,
                "usage": createUsage(10),
            })
            stream.push(DoneEvent(reason="stop", message=message))

        asyncio.get_running_loop().call_soon(respond)
        return stream

    harness.session.agent.streamFunction = streamFunction
    return lambda: state["callCount"]


def seedCompactableSession(harness: Any) -> None:
    harness.settingsManager.applyOverrides({"compaction": {"keepRecentTokens": 1}})
    now = nowMs()
    harness.sessionManager.appendMessage({
        "role": "user",
        "content": [{"type": "text", "text": "message to compact"}],
        "timestamp": now - 1000,
    })
    assistant = createAssistant(harness, stopReason="stop", totalTokens=100, timestamp=now - 500)
    assistant.content = [TextContent(text="assistant response to compact")]
    harness.sessionManager.appendMessage(assistant)
    harness.session.agent.state.messages = harness.sessionManager.buildSessionContext().messages


def abortableCompaction(started: asyncio.Future[None]) -> Any:
    def extension(pi: Any) -> None:
        async def onBeforeCompact(event: Any, _ctx: Any) -> Any:
            cancelled: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            read_field(event, "signal").addEventListener(
                "abort", lambda: cancelled.done() or cancelled.set_result({"cancel": True}), once=True
            )
            if not started.done():
                started.set_result(None)
            return await cancelled

        pi.on("session_before_compact", onBeforeCompact)

    return extension


def extensionSummary(summary: str, **extra: Any) -> Any:
    def extension(pi: Any) -> None:
        async def onBeforeCompact(event: Any, _ctx: Any) -> Any:
            preparation = read_field(event, "preparation")
            return {
                "compaction": {
                    "summary": summary,
                    "firstKeptEntryId": read_field(preparation, "firstKeptEntryId"),
                    "tokensBefore": read_field(preparation, "tokensBefore"),
                    "details": {},
                    **extra,
                }
            }

        pi.on("session_before_compact", onBeforeCompact)

    return extension


def spyOnRunAutoCompaction(monkeypatch: pytest.MonkeyPatch, session: Any) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []

    async def runAutoCompaction(*args: Any, **_kwargs: Any) -> bool:
        calls.append(args)
        return False

    monkeypatch.setattr(session, "_runAutoCompaction", runAutoCompaction)
    return calls


def compactionEntries(harness: Any) -> list[Any]:
    return [entry for entry in harness.sessionManager.getEntries() if read_field(entry, "type") == "compaction"]


def start(awaitable: Any) -> asyncio.Task[Any]:
    """A JavaScript async call: it runs at once, up to its first suspension."""
    return asyncio.Task(awaitable, loop=asyncio.get_running_loop(), eager_start=True)


# --- AgentSession compaction characterization ---------------------------------------------------


async def test_manually_compacts_using_an_extension_provided_summary():
    summaryUsage = {
        "input": 10,
        "output": 20,
        "cacheRead": 30,
        "cacheWrite": 40,
        "totalTokens": 100,
        "cost": {"input": 0.1, "output": 0.2, "cacheRead": 0.3, "cacheWrite": 0.4, "total": 1},
    }

    def extension(pi: Any) -> None:
        async def onBeforeCompact(event: Any, _ctx: Any) -> Any:
            preparation = read_field(event, "preparation")
            return {
                "compaction": {
                    "summary": "summary from extension",
                    "firstKeptEntryId": read_field(preparation, "firstKeptEntryId"),
                    "tokensBefore": read_field(preparation, "tokensBefore"),
                    "usage": summaryUsage,
                    "details": {"source": "extension"},
                }
            }

        pi.on("session_before_compact", onBeforeCompact)

    async with createHarness(settings={"compaction": {"keepRecentTokens": 1}}, extensionFactories=[extension]) as harness:
        await harness.session.prompt("one")
        await harness.session.prompt("two")
        statsBefore = harness.session.getSessionStats()

        result = await harness.session.compact()
        entries = compactionEntries(harness)
        estimatedTokensAfter = sum(estimateTokens(message) for message in harness.session.messages)

        def usageDict(usage: Any) -> Any:
            return usage.model_dump(exclude_none=True) if hasattr(usage, "model_dump") else usage

        assert result.summary == "summary from extension"
        assert usageDict(result.usage) == summaryUsage
        assert result.estimatedTokensAfter == estimatedTokensAfter
        assert len(entries) == 1
        assert usageDict(read_field(entries[0], "usage")) == summaryUsage
        statsAfter = harness.session.getSessionStats()
        assert statsAfter.tokens.input == statsBefore.tokens.input + summaryUsage["input"]
        assert statsAfter.tokens.output == statsBefore.tokens.output + summaryUsage["output"]
        assert statsAfter.tokens.cacheRead == statsBefore.tokens.cacheRead + summaryUsage["cacheRead"]
        assert statsAfter.tokens.cacheWrite == statsBefore.tokens.cacheWrite + summaryUsage["cacheWrite"]
        assert statsAfter.cost == statsBefore.cost + summaryUsage["cost"]["total"]
        assert read_field(harness.session.messages[0], "role") == "system"
        assert read_field(harness.session.messages[1], "role") == "compactionSummary"


async def test_checkpoints_the_replayed_system_state_and_folds_summarized_and_retained_system_patches_into_it():
    async with createHarness() as harness:
        harness.setResponses([fauxAssistantMessage("declared")])
        await harness.session.prompt("declare the prompt")
        declared = harness.session.messages[0]
        assert read_field(declared, "role") == "system", "expected declared system message"

        harness.sessionManager.appendMessage({
            "role": "system",
            "content": "summarized instruction",
            "sections": {"early": "<early>1</early>"},
            "toolsRemoved": [{"name": "bash"}],
            "timestamp": nowMs(),
        })
        firstKeptEntryId = harness.sessionManager.appendMessage({
            "role": "user",
            "content": [{"type": "text", "text": "kept before patch"}],
            "timestamp": nowMs(),
        })
        harness.sessionManager.appendMessage({
            "role": "system",
            "content": "retained instruction",
            "sections": {"extra": "<extra>late</extra>"},
            "toolsRemoved": [{"name": "read"}],
            "timestamp": nowMs(),
        })
        harness.sessionManager.appendMessage({
            "role": "user",
            "content": [{"type": "text", "text": "kept after patch"}],
            "timestamp": nowMs(),
        })
        harness.sessionManager.appendCompaction("compacted", firstKeptEntryId, 100)

        messages = harness.sessionManager.buildSessionContext().messages
        assert [read_field(message, "role") for message in messages] == ["system", "compactionSummary", "user", "user"]
        checkpoint = messages[0]
        assert read_field(checkpoint, "content") == "summarized instruction\n\nretained instruction"
        assert read_field(checkpoint, "sections") == {
            **(read_field(declared, "sections") or {}),
            "early": "<early>1</early>",
            "extra": "<extra>late</extra>",
        }
        assert [read_field(tool, "name") for tool in read_field(checkpoint, "toolsAdded") or []] == [
            name for name in harness.session.getActiveToolNames() if name not in ("read", "bash")
        ]


async def test_allows_a_queued_prompt_to_start_when_manual_compaction_ends():
    async with createHarness(
        settings={"compaction": {"keepRecentTokens": 1}}, extensionFactories=[extensionSummary("manual compacted")]
    ) as harness:
        seedCompactableSession(harness)
        harness.setResponses([fauxAssistantMessage("queued response")])
        queued: dict[str, Any] = {}

        def onEvent(event: Any) -> None:
            if (
                read_field(event, "type") == "compaction_end"
                and read_field(event, "reason") == "manual"
                and read_field(event, "result")
            ):
                assert harness.session.isCompacting is False
                queued["prompt"] = start(harness.session.prompt("queued after compaction"))

        harness.session.subscribe(onEvent)

        await harness.session.compact()
        assert "prompt" in queued, "compaction_end did not start the queued prompt"
        await queued["prompt"]

        assert "queued after compaction" in getUserTexts(harness)
        assert harness.session.getLastAssistantText() == "queued response"


async def test_throws_when_compacting_without_a_model():
    async with createHarness() as harness:
        harness.session.agent.state.model = None

        with pytest.raises(Exception, match="No model selected"):
            await harness.session.compact()


async def test_throws_when_compacting_without_configured_auth():
    async with createHarness(withConfiguredAuth=False) as harness:
        # Auth is resolved only when Pi summarizes itself, after checking there is something to compact.
        seedCompactableSession(harness)

        with pytest.raises(Exception, match=f"No API key found for {harness.getModel().provider}."):
            await harness.session.compact()


async def test_manually_compacts_with_a_custom_stream_fn_when_registry_auth_is_absent():
    async with createHarness(withConfiguredAuth=False) as harness:
        seedCompactableSession(harness)
        getStreamCallCount = useSummaryStreamFn(harness, "summary from custom stream")

        result = await harness.session.compact()

        assert "summary from custom stream" in result.summary
        assert getStreamCallCount() == 1


async def test_manually_compacts_with_provider_resolved_bearer_auth():
    async with createHarness(withConfiguredAuth=False) as harness:
        model = harness.getModel()

        async def resolve(**_: Any) -> AuthResult:
            return AuthResult(
                auth=ModelAuth(headers={"Authorization": "Bearer ambient-token"}), source="ambient bearer token"
            )

        harness.session.modelRuntime.registerNativeProvider(SimpleNamespace(
            id=model.provider,
            name="Faux bearer provider",
            auth=ProviderAuth(apiKey=ApiKeyAuth(name="Faux bearer token", resolve=resolve)),
            getModels=lambda: harness.models,
            stream=lambda *_: createAssistantMessageEventStream(),
            streamSimple=lambda *_: createAssistantMessageEventStream(),
        ))
        seedCompactableSession(harness)
        seen: dict[str, Any] = {}

        def summaryResponse(_context: Any, options: Any = None, *_: Any) -> Any:
            seen["apiKey"] = read_field(options, "apiKey")
            seen["headers"] = read_field(options, "headers")
            return fauxAssistantMessage("summary with bearer auth")

        harness.setResponses([summaryResponse])

        result = await harness.session.compact()

        assert seen["apiKey"] is None
        assert seen["headers"] == {"Authorization": "Bearer ambient-token"}
        assert "summary with bearer auth" in result.summary
        assert harness.faux.state.callCount == 1


async def test_uses_the_standalone_compaction_request_context():
    async with createHarness(settings={"compaction": {"keepRecentTokens": 1}}) as harness:
        seedCompactableSession(harness)

        transformCalls: list[Any] = []

        async def transformContext(messages: Any, *_: Any) -> Any:
            transformCalls.append(messages)
            return messages

        harness.session.agent.transformContext = transformContext
        harness.session.agent.sessionId = "active-routing-session"
        harness.session.agent.transport = "websocket"

        captured: dict[str, Any] = {}

        def onRequest(context: Any, options: Any) -> None:
            captured["context"] = context
            captured["options"] = options

        useSummaryStreamFn(harness, "standalone summary", onRequest)

        await harness.session.compact()

        requestMessages = captured["context"].messages if "context" in captured else []
        assert transformCalls == []
        assert getCurrentSystemPrompt(requestMessages) != harness.session.agent.state.systemPrompt
        assert getCurrentTools(requestMessages) == []
        # Regression test for #9652: split-turn summaries use a clear Markdown conversation boundary.
        assert "# Conversation\\n[User]: message to compact" in stringify(requestMessages)
        requestOptions = captured["options"]
        assert read_field(requestOptions, "cacheRetention") == "none"
        assert read_field(requestOptions, "sessionId") != "active-routing-session"
        assert read_field(requestOptions, "transport") is None


async def test_persists_usage_from_pi_generated_manual_compaction():
    async with createHarness(withConfiguredAuth=False) as harness:
        seedCompactableSession(harness)
        useSummaryStreamFn(harness, "summary from custom stream")

        result = await harness.session.compact()

        entries = compactionEntries(harness)
        assert Usage.model_validate(to_jsonable(result.usage)) == createUsage(10)
        assert len(entries) == 1
        assert Usage.model_validate(to_jsonable(read_field(entries[0], "usage"))) == createUsage(10)


async def test_auto_compacts_with_a_custom_stream_fn_when_registry_auth_is_absent():
    async with createHarness(withConfiguredAuth=False) as harness:
        seedCompactableSession(harness)
        getStreamCallCount = useSummaryStreamFn(harness, "auto summary from custom stream")

        await harness.session._runAutoCompaction("threshold", False)

        compactionEnd = harness.eventsOfType("compaction_end")[-1]
        assert len(compactionEntries(harness)) == 1
        assert read_field(read_field(compactionEnd, "result"), "estimatedTokensAfter") > 0
        assert getStreamCallCount() == 1


async def test_notifies_extensions_when_auto_compaction_fails():
    failedEvents: list[Any] = []

    def extension(pi: Any) -> None:
        async def onFailed(event: Any, _ctx: Any) -> None:
            failedEvents.append(event)

        pi.on("session_compact_failed", onFailed)

    async with createHarness(extensionFactories=[extension]) as harness:
        seedCompactableSession(harness)

        def blowUp(*_: Any) -> Any:
            raise RuntimeError("summary generator blew up")

        harness.session.agent.streamFunction = blowUp

        assert await harness.session._runAutoCompaction("threshold", False) is False

        compactionEnd = harness.eventsOfType("compaction_end")[-1]
        assert read_field(compactionEnd, "reason") == "threshold"
        assert read_field(compactionEnd, "aborted") is False
        assert read_field(compactionEnd, "willRetry") is False
        assert read_field(compactionEnd, "errorMessage") == "Auto-compaction failed: summary generator blew up"
        assert len(failedEvents) == 1
        assert {
            key: read_field(failedEvents[0], key)
            for key in ("type", "reason", "aborted", "willRetry", "fromExtension", "errorMessage")
        } == {
            "type": "session_compact_failed",
            "reason": "threshold",
            "aborted": False,
            "willRetry": False,
            "fromExtension": False,
            "errorMessage": "Auto-compaction failed: summary generator blew up",
        }


async def test_compacts_and_resumes_after_a_length_stop_below_the_desired_output_limit():
    async with createHarness(
        models=[{"id": "faux-1", "contextWindow": 1000, "maxTokens": 100}],
        settings={"compaction": {"keepRecentTokens": 1, "reserveTokens": 0}},
        extensionFactories=[extensionSummary("overflow compacted")],
    ) as harness:
        harness.setResponses([
            fauxAssistantMessage("partial response", {"stopReason": "length"}),
            fauxAssistantMessage("completed response"),
        ])

        await harness.session.prompt("x" * 5000)

        assert harness.faux.state.callCount == 2
        compactionEnd = harness.eventsOfType("compaction_end")[-1]
        assert read_field(compactionEnd, "reason") == "overflow"
        assert read_field(compactionEnd, "aborted") is False
        assert read_field(compactionEnd, "willRetry") is True
        assert harness.session.getLastAssistantText() == "completed response"


def createLargeTool(name: str, text: str, *, terminate: bool = False) -> AgentTool:
    async def execute(*_args: Any, **_kwargs: Any) -> AgentToolResult:
        return AgentToolResult(content=[TextContent(text=text)], details={}, terminate=terminate or None)

    return AgentTool(
        name=name,
        label="Large result",
        description="Returns enough content to cross the compaction threshold",
        parameters={"type": "object", "properties": {}},
        execute=execute,
    )


# Regression coverage for #8133: model overrides must also apply between assistant turns.
# Regression coverage for #9740: an oversized trailing tool result must still produce a cut point.
@pytest.mark.parametrize("modelOverride", [False, True])
async def test_compacts_after_an_oversized_tool_result_in_the_same_run(modelOverride: bool):
    toolResult = f"large-tool-result:{'x' * 8000}"
    order: list[str] = []
    observedSettings: list[Any] = []

    def extension(pi: Any) -> None:
        def onBeforeCompact(event: Any, _ctx: Any) -> Any:
            order.append("compaction")
            preparation = read_field(event, "preparation")
            observedSettings.append(read_field(preparation, "settings"))
            return {
                "compaction": {
                    "summary": "compacted history",
                    "firstKeptEntryId": read_field(preparation, "firstKeptEntryId"),
                    "tokensBefore": read_field(preparation, "tokensBefore"),
                    "details": {},
                }
            }

        pi.on("session_before_compact", onBeforeCompact)

    async with createHarness(
        models=[{"id": "faux-1", "contextWindow": 2600, "maxTokens": 100}],
        settings={
            "compaction": {
                "enabled": True,
                "reserveTokens": 0,
                "keepRecentTokens": 20000,
                "modelOverrides": {"faux/faux-1": {"reserveTokens": 400, "keepRecentTokens": 1750}},
            }
            if modelOverride
            else {"enabled": True, "reserveTokens": 400, "keepRecentTokens": 1750}
        },
        tools=[createLargeTool("large_result", toolResult)],
        extensionFactories=[extension],
    ) as harness:
        resumed: dict[str, str] = {"request": ""}

        def resumedResponse(context: Any, *_: Any) -> Any:
            order.append("provider")
            resumed["request"] = stringify(context.messages)
            return fauxAssistantMessage("finished after compaction")

        harness.setResponses([
            fauxAssistantMessage(f"old-history:{'a' * 800}"),
            fauxAssistantMessage(f"recent-history:{'b' * 800}"),
            fauxAssistantMessage(fauxToolCall("large_result", {}), {"stopReason": "toolUse"}),
            resumedResponse,
        ])

        await harness.session.prompt("seed old history")
        await harness.session.prompt("seed recent history")
        agentStartsBefore = len(harness.eventsOfType("agent_start"))
        await harness.session.prompt("run the large tool")

        assert order[:2] == ["compaction", "provider"]
        settings = observedSettings[0]
        assert {key: read_field(settings, key) for key in ("enabled", "reserveTokens", "keepRecentTokens")} == {
            "enabled": True,
            "reserveTokens": 400,
            "keepRecentTokens": 1750,
        }
        assert len(harness.eventsOfType("agent_start")) == agentStartsBefore + 1
        assert harness.eventsOfType("compaction_start")[-1] == {"type": "compaction_start", "reason": "threshold"}
        assert "compacted history" in resumed["request"]
        assert "large-tool-result" in resumed["request"]
        assert harness.session.getLastAssistantText() == "finished after compaction"


async def test_includes_steering_queued_during_compaction_in_the_resumed_assistant_request():
    loop = asyncio.get_running_loop()
    compactionStarted: asyncio.Future[None] = loop.create_future()
    compactionReleased: asyncio.Future[None] = loop.create_future()

    def extension(pi: Any) -> None:
        async def onBeforeCompact(event: Any, _ctx: Any) -> Any:
            if not compactionStarted.done():
                compactionStarted.set_result(None)
            await compactionReleased
            preparation = read_field(event, "preparation")
            return {
                "compaction": {
                    "summary": "compacted history",
                    "firstKeptEntryId": read_field(preparation, "firstKeptEntryId"),
                    "tokensBefore": read_field(preparation, "tokensBefore"),
                    "details": {},
                }
            }

        pi.on("session_before_compact", onBeforeCompact)

    async with createHarness(
        models=[{"id": "faux-1", "contextWindow": 2600, "maxTokens": 100}],
        settings={"compaction": {"enabled": True, "reserveTokens": 400, "keepRecentTokens": 1750}},
        tools=[createLargeTool("large_result", f"large-tool-result:{'x' * 6800}")],
        extensionFactories=[extension],
    ) as harness:
        resumed: dict[str, str] = {"request": ""}

        def resumedResponse(context: Any, *_: Any) -> Any:
            resumed["request"] = stringify(context.messages)
            return fauxAssistantMessage("finished after compaction")

        harness.setResponses([
            fauxAssistantMessage(f"old-history:{'a' * 800}"),
            fauxAssistantMessage(f"recent-history:{'b' * 800}"),
            fauxAssistantMessage(fauxToolCall("large_result", {}), {"stopReason": "toolUse"}),
            resumedResponse,
            fauxAssistantMessage("finished after delayed steering"),
        ])

        await harness.session.prompt("seed old history")
        await harness.session.prompt("seed recent history")
        promptPromise = start(harness.session.prompt("run the large tool"))
        await compactionStarted
        await harness.session.steer("change direction")
        compactionReleased.set_result(None)
        await promptPromise

        assert "change direction" in resumed["request"]
        assert harness.faux.state.callCount == 4


async def test_does_not_compact_after_a_terminating_tool_result():
    async with createHarness(
        models=[{"id": "faux-1", "contextWindow": 2600, "maxTokens": 100}],
        settings={"compaction": {"enabled": True, "reserveTokens": 400, "keepRecentTokens": 1750}},
        tools=[createLargeTool("terminate_with_large_result", f"large-tool-result:{'x' * 6800}", terminate=True)],
        extensionFactories=[extensionSummary("unexpected compaction")],
    ) as harness:
        harness.setResponses([
            fauxAssistantMessage(f"old-history:{'a' * 800}"),
            fauxAssistantMessage(f"recent-history:{'b' * 800}"),
            fauxAssistantMessage(fauxToolCall("terminate_with_large_result", {}), {"stopReason": "toolUse"}),
        ])

        await harness.session.prompt("seed old history")
        await harness.session.prompt("seed recent history")
        await harness.session.prompt("run the terminating tool")

        assert harness.eventsOfType("compaction_start") == []
        assert compactionEntries(harness) == []
        assert harness.getPendingResponseCount() == 0


async def test_does_not_compact_when_a_length_stop_reaches_the_desired_output_limit():
    async with createHarness(models=[{"id": "faux-1", "contextWindow": 1_000_000, "maxTokens": 100}]) as harness:
        harness.setResponses([fauxAssistantMessage("x" * 400, {"stopReason": "length"})])

        await harness.session.prompt("hello")

        assert harness.faux.state.callCount == 1
        assert harness.eventsOfType("compaction_start") == []


async def test_stops_after_one_compact_and_retry_when_a_second_response_is_also_truncated():
    async with createHarness(
        models=[{"id": "faux-1", "contextWindow": 1_000_000, "maxTokens": 100}],
        settings={"compaction": {"keepRecentTokens": 1, "reserveTokens": 0}},
        extensionFactories=[extensionSummary("overflow compacted")],
    ) as harness:
        harness.setResponses([
            lambda *_: fauxAssistantMessage("x" * 64, {"stopReason": "length", "timestamp": nowMs() + 10_000}),
            lambda *_: fauxAssistantMessage("y" * 64, {"stopReason": "length", "timestamp": nowMs() + 10_000}),
        ])

        await harness.session.prompt("x" * 5000)

        assert harness.faux.state.callCount == 2
        assert len([e for e in harness.eventsOfType("compaction_start") if read_field(e, "reason") == "overflow"]) == 1
        assert (
            read_field(harness.eventsOfType("compaction_end")[-1], "errorMessage")
            == "Truncated response recovery failed after one compact-and-retry attempt."
        )


def _collectCompactionErrors(harness: Any) -> list[str]:
    compactionErrors: list[str] = []

    def onEvent(event: Any) -> None:
        if read_field(event, "type") == "compaction_end" and read_field(event, "errorMessage"):
            compactionErrors.append(read_field(event, "errorMessage"))

    harness.session.subscribe(onEvent)
    return compactionErrors


OVERFLOW_RETRY_FAILED = (
    "Context overflow recovery failed after one compact-and-retry attempt. Try reducing context or switching to a "
    "larger-context model."
)


async def test_keeps_overflow_wording_when_a_repeated_length_stop_fills_the_context_window(
    monkeypatch: pytest.MonkeyPatch,
):
    async with createHarness(models=[{"id": "faux-1", "contextWindow": 100, "maxTokens": 100}]) as harness:
        lengthOverflowMessage = createAssistant(harness, stopReason="length", totalTokens=100, timestamp=nowMs())
        calls = spyOnRunAutoCompaction(monkeypatch, harness.session)
        compactionErrors = _collectCompactionErrors(harness)

        await harness.session._checkCompaction(lengthOverflowMessage)
        await harness.session._checkCompaction(lengthOverflowMessage.model_copy(update={"timestamp": nowMs() + 1}))

        assert len(calls) == 1
        assert OVERFLOW_RETRY_FAILED in compactionErrors


async def test_cancels_in_progress_manual_compaction_when_abort_compaction_is_called():
    compactionStarted: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    async with createHarness(
        settings={"compaction": {"keepRecentTokens": 1}}, extensionFactories=[abortableCompaction(compactionStarted)]
    ) as harness:
        seedCompactableSession(harness)

        compactPromise = start(harness.session.compact())
        await compactionStarted
        harness.session.abortCompaction()

        with pytest.raises(Exception, match="Compaction cancelled"):
            await compactPromise


# Regression test for #8920.
async def test_aborts_an_in_progress_manual_compaction_and_waits_until_the_session_is_idle():
    compactionStarted: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    async with createHarness(
        settings={"compaction": {"keepRecentTokens": 1}}, extensionFactories=[abortableCompaction(compactionStarted)]
    ) as harness:
        seedCompactableSession(harness)
        harness.setResponses([fauxAssistantMessage("continued")])

        compactPromise = start(harness.session.compact())
        await compactionStarted
        await harness.session.abort()
        with pytest.raises(Exception, match="Compaction cancelled"):
            await compactPromise

        compactionEnd = harness.eventsOfType("compaction_end")[-1]
        assert read_field(compactionEnd, "reason") == "manual"
        assert read_field(compactionEnd, "aborted") is True
        assert harness.session.isCompacting is False
        assert harness.session.isIdle is True

        assert await harness.session.prompt("next prompt") is None
        assert harness.session.getLastAssistantText() == "continued"


async def test_resumes_after_threshold_compaction_when_only_agent_level_queued_messages_exist():
    async with createHarness(
        settings={"compaction": {"keepRecentTokens": 1}}, extensionFactories=[extensionSummary("auto compacted")]
    ) as harness:
        harness.setResponses([fauxAssistantMessage("one"), fauxAssistantMessage("two")])
        await harness.session.prompt("first")
        await harness.session.prompt("second")

        harness.session.agent.followUp({
            "role": "custom",
            "customType": "test",
            "content": [{"type": "text", "text": "queued custom"}],
            "display": False,
            "timestamp": nowMs(),
        })

        assert await harness.session._runAutoCompaction("threshold", False) is True


async def test_does_not_retry_overflow_recovery_more_than_once(monkeypatch: pytest.MonkeyPatch):
    async with createHarness() as harness:
        overflowMessage = createAssistant(
            harness, stopReason="error", errorMessage="prompt is too long", timestamp=nowMs()
        )
        calls = spyOnRunAutoCompaction(monkeypatch, harness.session)
        compactionErrors = _collectCompactionErrors(harness)

        await harness.session._checkCompaction(overflowMessage)
        await harness.session._checkCompaction(overflowMessage.model_copy(update={"timestamp": nowMs() + 1}))

        assert len(calls) == 1
        assert OVERFLOW_RETRY_FAILED in compactionErrors


async def test_compacts_successful_overflow_responses_without_retrying():
    async with createHarness(
        settings={"compaction": {"enabled": True, "keepRecentTokens": 1, "reserveTokens": 0}},
        models=[{"id": "faux-1", "contextWindow": 1, "maxTokens": 100}],
        extensionFactories=[extensionSummary("successful overflow compacted")],
    ) as harness:
        harness.setResponses([fauxAssistantMessage("completed answer")])

        assert await harness.session.prompt("hello") is None

        compactionEnd = harness.eventsOfType("compaction_end")[-1]
        assert read_field(compactionEnd, "reason") == "overflow"
        assert read_field(compactionEnd, "aborted") is False
        assert read_field(compactionEnd, "willRetry") is False
        assert harness.faux.state.callCount == 1


async def test_ignores_stale_pre_compaction_assistant_usage_on_pre_prompt_checks(monkeypatch: pytest.MonkeyPatch):
    async with createHarness() as harness:
        staleTimestamp = nowMs() - 10_000
        staleAssistant = createAssistant(harness, stopReason="stop", totalTokens=610_000, timestamp=staleTimestamp)

        harness.sessionManager.appendMessage({
            "role": "user",
            "content": [{"type": "text", "text": "before compaction"}],
            "timestamp": staleTimestamp - 1000,
        })
        harness.sessionManager.appendMessage(staleAssistant)
        firstKeptEntryId = read_field(harness.sessionManager.getEntries()[0], "id")
        harness.sessionManager.appendCompaction(
            "summary", firstKeptEntryId, staleAssistant.usage.totalTokens, None, False
        )
        harness.sessionManager.appendMessage({
            "role": "user",
            "content": [{"type": "text", "text": "after compaction"}],
            "timestamp": nowMs(),
        })

        calls = spyOnRunAutoCompaction(monkeypatch, harness.session)

        await harness.session._checkCompaction(staleAssistant, False)

        assert calls == []


async def test_triggers_threshold_compaction_for_error_messages_using_the_last_successful_usage(
    monkeypatch: pytest.MonkeyPatch,
):
    async with createHarness() as harness:
        successfulAssistant = createAssistant(harness, stopReason="stop", totalTokens=190_000, timestamp=nowMs())
        errorAssistant = createAssistant(
            harness, stopReason="error", errorMessage="529 overloaded", timestamp=nowMs() + 1000
        )
        harness.session.agent.state.messages = [
            {"role": "user", "content": [{"type": "text", "text": "hello"}], "timestamp": nowMs() - 1000},
            successfulAssistant,
            {"role": "user", "content": [{"type": "text", "text": "retry"}], "timestamp": nowMs() + 500},
            errorAssistant,
        ]

        calls = spyOnRunAutoCompaction(monkeypatch, harness.session)

        await harness.session._checkCompaction(errorAssistant)

        assert calls == [("threshold", False)]


async def test_does_not_trigger_threshold_compaction_for_error_messages_when_no_prior_usage_exists():
    async with createHarness() as harness:
        errorAssistant = createAssistant(harness, stopReason="error", errorMessage="529 overloaded", timestamp=nowMs())
        harness.session.agent.state.messages = [
            {"role": "user", "content": [{"type": "text", "text": "hello"}], "timestamp": nowMs() - 1000},
            errorAssistant,
        ]

        # MISAKA fork: see the module docstring.
        assert await harness.session._checkCompaction(errorAssistant) is False
        assert harness.eventsOfType("compaction_start") == []


async def test_does_not_trigger_threshold_compaction_when_only_kept_pre_compaction_usage_exists(
    monkeypatch: pytest.MonkeyPatch,
):
    async with createHarness() as harness:
        preCompactionTimestamp = nowMs() - 10_000
        keptAssistant = createAssistant(harness, stopReason="stop", totalTokens=190_000, timestamp=preCompactionTimestamp)

        harness.sessionManager.appendMessage({
            "role": "user",
            "content": [{"type": "text", "text": "before compaction"}],
            "timestamp": preCompactionTimestamp - 1000,
        })
        harness.sessionManager.appendMessage(keptAssistant)
        firstKeptEntryId = read_field(harness.sessionManager.getEntries()[0], "id")
        harness.sessionManager.appendCompaction("summary", firstKeptEntryId, keptAssistant.usage.totalTokens, None, False)

        errorAssistant = createAssistant(harness, stopReason="error", errorMessage="529 overloaded", timestamp=nowMs())
        harness.session.agent.state.messages = [
            {
                "role": "user",
                "content": [{"type": "text", "text": "kept user"}],
                "timestamp": preCompactionTimestamp - 1000,
            },
            keptAssistant,
            {"role": "user", "content": [{"type": "text", "text": "new prompt"}], "timestamp": nowMs() - 500},
            errorAssistant,
        ]

        calls = spyOnRunAutoCompaction(monkeypatch, harness.session)

        await harness.session._checkCompaction(errorAssistant)

        assert calls == []


async def test_does_not_trigger_threshold_compaction_below_the_threshold_or_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
):
    async with (
        createHarness(
            settings={"compaction": {"enabled": True, "reserveTokens": 1000}},
            models=[{"id": "faux-1", "contextWindow": 200_000}],
        ) as belowThresholdHarness,
        createHarness(settings={"compaction": {"enabled": False}}) as disabledHarness,
    ):
        disabledCalls = spyOnRunAutoCompaction(monkeypatch, disabledHarness.session)

        # MISAKA fork: see the module docstring; the below-threshold check runs the real `_runAutoCompaction`.
        assert (
            await belowThresholdHarness.session._checkCompaction(
                createAssistant(belowThresholdHarness, stopReason="stop", totalTokens=1_000, timestamp=nowMs())
            )
            is False
        )
        await disabledHarness.session._checkCompaction(
            createAssistant(disabledHarness, stopReason="stop", totalTokens=1_000_000, timestamp=nowMs())
        )

        assert belowThresholdHarness.eventsOfType("compaction_start") == []
        assert disabledCalls == []

"""pi's ``cache-warmer.test.ts`` (``misaka/core/cache_warmer.py``)."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from typing import Any

import pytest
from model_runtime_test_utils import createInMemoryModelRegistry
from oauth_fetch import useFakeTimers

from misaka.ai.providers.all import getBuiltinModel
from misaka.ai.types import AssistantMessage, Usage
from misaka.ai.utils.transcript import normalizeContext
from misaka.core import cache_warmer
from misaka.core.auth_storage import AuthStorage
from misaka.core.cache_warmer import (
    CacheWarmer,
    CacheWarmRequest,
    formatCacheWarmingStatus,
    formatCacheWarmingUsage,
    getCacheWarmingDelayMs,
    getPromptCacheTtlMs,
    isReplayable,
)
from misaka.core.event_bus import createEventBus
from misaka.core.extensions.loader import (
    createExtensionRuntime,
    loadExtensionFromFactory,
)
from misaka.core.extensions.runner import ExtensionRunner
from misaka.core.session_manager import SessionManager
from misaka.utils.js import AbortController

adaptiveModel = getBuiltinModel("anthropic", "claude-opus-4-6").model_copy(
    update={"promptCache": {"short": 300, "long": 3600}}
)
budgetModel = getBuiltinModel("anthropic", "claude-sonnet-4-5").model_copy(
    update={"promptCache": {"short": 300, "long": 3600}}
)
openaiModel = getBuiltinModel("openai", "gpt-5").model_copy(update={"promptCache": {"short": 300, "long": 86_400}})
unknownModel = adaptiveModel.model_copy(update={"promptCache": None})

warmUsage = Usage(
    input=0,
    output=1,
    cacheRead=100,
    cacheWrite=0,
    totalTokens=101,
    cost={"input": 0, "output": 0, "cacheRead": 0.01, "cacheWrite": 0, "total": 0.01},
)


def response(model: Any, stopReason: str = "length") -> AssistantMessage:
    return AssistantMessage(
        role="assistant",
        content=[],
        api=model.api,
        provider=model.provider,
        model=model.id,
        usage=warmUsage,
        stopReason=stopReason,
        timestamp=0,
    )


def branchWithPrompt(promptTokens: int) -> list[dict[str, Any]]:
    return [
        {
            "type": "message",
            "id": "a",
            "parentId": None,
            "timestamp": "1970-01-01T00:00:00.000Z",
            "message": response(adaptiveModel).model_copy(
                update={
                    "usage": warmUsage.model_copy(
                        update={"output": 10, "cacheRead": promptTokens, "totalTokens": promptTokens + 10}
                    )
                }
            ),
        }
    ]


class _AppendUsage:
    """``vi.fn(usageManager.appendUsage.bind(usageManager))``."""

    def __init__(self, manager: SessionManager) -> None:
        self.manager = manager
        self.calls: list[tuple[Any, ...]] = []
        self.results: list[Any] = []

    def __call__(self, *args: Any) -> Any:
        self.calls.append(args)
        entry = self.manager.appendUsage(*args)
        self.results.append(entry)
        return entry


def fakeRuntime(*, result=None, decide=None, mode: str | None = None, branch=None) -> SimpleNamespace:
    calls: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    warmedEntries: list[Any] = []
    usageManager = SessionManager.inMemory()
    appendUsage = _AppendUsage(usageManager)
    state = SimpleNamespace(mode=mode if mode is not None else "idle", branch=branch if branch is not None else branchWithPrompt(100_000))

    async def defaultResult(model):
        return response(model)

    def streamSimple(model, _context, streamOptions):
        calls.append({"model": model, "options": streamOptions})
        return SimpleNamespace(result=lambda: (result or defaultResult)(model))

    async def decideFn(event):
        events.append(event)
        if decide is None:
            return event["action"]
        value = decide(event)
        if asyncio.iscoroutine(value):
            value = await value
        return value if value is not None else event["action"]

    warmer = CacheWarmer(
        SimpleNamespace(streamSimple=streamSimple),
        SimpleNamespace(appendUsage=appendUsage, getBranch=lambda: state.branch),
        lambda: state.mode,
        decideFn,
    )
    warmer.onWarmed = warmedEntries.append
    return SimpleNamespace(
        warmer=warmer, calls=calls, events=events, warmedEntries=warmedEntries, appendUsage=appendUsage, state=state
    )


def request(model: Any = adaptiveModel, options: dict[str, Any] | None = None) -> CacheWarmRequest:
    return CacheWarmRequest(model=model, context=normalizeContext({"messages": []}), options=options or {})


def current() -> bool:
    return True


# --- cache warming --------------------------------------------------------------------------------------------------


def test_derives_eligibility_and_timing_from_retention_and_provider_behavior(monkeypatch):
    monkeypatch.delenv("PI_CACHE_RETENTION", raising=False)
    assert [
        getPromptCacheTtlMs(adaptiveModel, None),
        getPromptCacheTtlMs(adaptiveModel, {"cacheRetention": "long"}),
        getPromptCacheTtlMs(adaptiveModel, {"cacheRetention": "none"}),
        getPromptCacheTtlMs(adaptiveModel, {"env": {"PI_CACHE_RETENTION": "long"}}),
        getPromptCacheTtlMs(openaiModel, {"cacheRetention": "long"}),
        getPromptCacheTtlMs(unknownModel, None),
    ] == [300_000, 3_600_000, None, 3_600_000, 86_400_000, None]
    assert [getCacheWarmingDelayMs(300_000), getCacheWarmingDelayMs(60_000), getCacheWarmingDelayMs(10_000)] == [
        270_000,
        50_000,
        None,
    ]
    assert [
        isReplayable(budgetModel, {"reasoning": "medium"}),
        isReplayable(budgetModel, None),
        isReplayable(adaptiveModel, {"reasoning": "medium"}),
        isReplayable(openaiModel, {"reasoning": "medium"}),
    ] == [False, True, True, True]


async def test_replays_profitable_requests_and_preserves_options_across_repeated_refreshes(monkeypatch):
    timers = useFakeTimers(monkeypatch)
    runtime = fakeRuntime()
    signal = AbortController().signal

    async def transformHeaders(*_args):
        return {}

    runtime.warmer.start(
        request(adaptiveModel, {"reasoning": "high", "signal": signal, "sessionId": "s", "transformHeaders": transformHeaders}),
        current,
    )
    await timers.advanceAsync(270_000)

    first = runtime.calls[0]
    assert first["model"] is adaptiveModel
    assert {key: first["options"][key] for key in ("reasoning", "sessionId", "transformHeaders", "maxTokens", "maxRetries")} == {
        "reasoning": "high",
        "sessionId": "s",
        "transformHeaders": transformHeaders,
        "maxTokens": 1,
        "maxRetries": 0,
    }
    assert first["options"]["signal"] is not signal
    event = runtime.events[0]
    assert event["type"] == "cache_warming_decision"
    assert event["continuationProbability"] == 1
    assert event["action"] == "warm"
    assert event["missCost"] == pytest.approx(0.575)
    assert event["warmCost"] == pytest.approx(0.050025)
    assert runtime.appendUsage.calls == [("cache_warm", adaptiveModel.provider, adaptiveModel.id, warmUsage, None)]
    assert runtime.warmedEntries == [runtime.appendUsage.results[0]]

    await timers.advanceAsync(270_000)
    assert len(runtime.calls) == 2
    runtime.warmer.cancel()


async def test_does_not_issue_refreshes_after_their_safe_deadline(monkeypatch):
    timers = useFakeTimers(monkeypatch, 0)
    runtime = fakeRuntime()
    runtime.warmer.start(request(), current)

    # A five-minute cache is scheduled for 4m30s and retains 15 seconds of
    # the 30-second expiry margin. Simulate a timer delayed by sleep.
    timers.now = 285_001
    timers._timers.clear()
    if not runtime.warmer.run:
        raise AssertionError("expected an active cache-warming run")
    await runtime.warmer.refresh(runtime.warmer.run)

    assert len(runtime.calls) == 0
    status = runtime.warmer.status
    assert (status.state, status.reason) == ("inactive", "cache refresh deadline missed")


async def test_rechecks_the_deadline_after_an_extension_decision(monkeypatch):
    timers = useFakeTimers(monkeypatch, 0)

    async def decide(_event):
        await asyncio.sleep(0)
        timers.now = 285_001
        return "warm"

    runtime = fakeRuntime(decide=decide)
    runtime.warmer.start(request(), current)
    timers._timers.clear()
    if not runtime.warmer.run:
        raise AssertionError("expected an active cache-warming run")
    await runtime.warmer.refresh(runtime.warmer.run)

    assert len(runtime.calls) == 0
    status = runtime.warmer.status
    assert (status.state, status.reason) == ("inactive", "cache refresh deadline missed")


async def test_applies_economic_decisions_and_extension_overrides(monkeypatch):
    timers = useFakeTimers(monkeypatch)
    unprofitable = fakeRuntime(branch=branchWithPrompt(5_000))
    unprofitable.warmer.start(request(), current)
    await timers.advanceAsync(270_000)
    assert len(unprofitable.calls) == 0
    status = unprofitable.warmer.status
    assert status.state == "inactive"
    assert (status.decision.action, status.decision.economicsAvailable) == ("stop", True)
    assert status.extensionOverride is False

    forced = fakeRuntime(branch=branchWithPrompt(5_000), decide=lambda _event: "warm")
    forced.warmer.start(request(), current)
    await timers.advanceAsync(270_000)
    assert len(forced.calls) == 1
    assert forced.warmedEntries[0].get("note") == "extension override"
    forced.warmer.cancel()

    vetoed = fakeRuntime(decide=lambda _event: "stop")
    vetoed.warmer.start(request(), current)
    await timers.advanceAsync(270_000)
    assert len(vetoed.calls) == 0
    assert (vetoed.warmer.status.state, vetoed.warmer.status.extensionOverride) == ("inactive", True)

    unavailable = fakeRuntime(branch=branchWithPrompt(0))
    unavailable.warmer.start(request(), current)
    assert (unavailable.warmer.status.state, unavailable.warmer.status.reason) == (
        "inactive",
        "cache economics unavailable",
    )
    await timers.advanceAsync(270_000)
    assert len(unavailable.calls) == 0


async def test_stops_for_unsupported_requests_context_changes_and_mode_changes(monkeypatch):
    timers = useFakeTimers(monkeypatch)
    unsupported = fakeRuntime()
    unsupported.state.mode = "off"
    unsupported.warmer.start(request(), current)
    assert unsupported.warmer.status.reason == "cache warming disabled"
    unsupported.state.mode = "idle"
    unsupported.warmer.start(request(unknownModel), current)
    assert unsupported.warmer.status.reason == "cache lifetime unavailable"
    unsupported.warmer.start(request(budgetModel, {"reasoning": "high"}), current)
    assert unsupported.warmer.status.reason == "request cannot be replayed safely"

    stillCurrent = True
    unsupported.warmer.start(request(), lambda: stillCurrent)
    stillCurrent = False
    assert unsupported.warmer.status.reason == "conversation context changed"
    await timers.advanceAsync(270_000)
    assert len(unsupported.calls) == 0

    unsupported.warmer.start(request(), current)
    unsupported.state.mode = "off"
    await timers.advanceAsync(270_000)
    assert len(unsupported.calls) == 0

    streaming = fakeRuntime(mode="streaming", branch=branchWithPrompt(400_000))
    streaming.warmer.start(request(), current)
    streaming.warmer.onAgentSettled()
    assert streaming.warmer.status.reason == "agent run settled"


async def test_aborts_replaced_requests_and_does_not_record_failed_refreshes(monkeypatch):
    timers = useFakeTimers(monkeypatch)
    release: dict[str, Any] = {}

    def result(model):
        future = asyncio.get_running_loop().create_future()
        release["resolve"] = lambda: future.set_result(response(model))
        return future

    pending = fakeRuntime(result=result)
    pending.warmer.start(request(), current)
    await timers.advanceAsync(270_000)
    pending.warmer.start(request(), current)
    assert pending.calls[0]["options"]["signal"].aborted is True
    release["resolve"]()
    pending.warmer.cancel()
    await timers.advanceAsync(600_000)
    assert len(pending.calls) == 1

    async def failedResult(model):
        return response(model, "error")

    failed = fakeRuntime(result=failedResult)
    failed.warmer.start(request(), current)
    await timers.advanceAsync(270_000)
    assert failed.appendUsage.calls == []
    failed.warmer.cancel()


def test_formats_status_and_usage_entries():
    decision = cache_warmer.CacheWarmingDecision(
        phase="idle",
        warmCost=0.013,
        missCost=0.621,
        continuationProbability=0.6,
        expectedSavings=0.36,
        economicsAvailable=True,
        action="warm",
    )
    assert (
        formatCacheWarmingStatus(cache_warmer.CacheWarmingStatus(state="scheduled", nextWarmAt=222_000, decision=decision), 0)
        == "Decision in 3m 42s (60% continuation probability, expected savings $0.360 >= $0.050 -> warm)"
    )
    usage = warmUsage.model_copy(
        update={"cost": {"input": 0.00004, "output": 0.00005, "cacheRead": 0.02940725, "cacheWrite": 0, "total": 0.02949725}}
    )
    entry = SessionManager.inMemory().appendUsage(
        "cache_warm", adaptiveModel.provider, adaptiveModel.id, usage, "extension override"
    )
    assert formatCacheWarmingUsage(entry) == "Cache warmed (extension override): $0.029497"


# --- ExtensionRunner.emitCacheWarmingDecision -----------------------------------------------------------------------


async def test_uses_the_last_extension_override():
    runtime = createExtensionRuntime()
    eventBus = createEventBus()

    def warm(pi):
        pi.on("cache_warming_decision", lambda *_args: {"action": "warm"})

    def stop(pi):
        pi.on("cache_warming_decision", lambda *_args: {"action": "stop"})

    extensions = [await loadExtensionFromFactory(factory, os.getcwd(), eventBus, runtime) for factory in (warm, stop)]
    modelRegistry = await createInMemoryModelRegistry(AuthStorage.inMemory())
    runner = ExtensionRunner(extensions, runtime, os.getcwd(), SessionManager.inMemory(), modelRegistry)
    event = {
        "type": "cache_warming_decision",
        "warmCost": 0.05,
        "missCost": 0.5,
        "continuationProbability": 0.15,
        "action": "warm",
    }

    assert await runner.emitCacheWarmingDecision(event) == "stop"

"""Translated from pi's ``packages/coding-agent/test/suite/regressions/`` ``9178-tree-during-compaction.test.ts``,
``9340-9777-auto-compaction-cancellation.test.ts``, ``pre-prompt-compaction-no-continue.test.ts`` and
``tree-during-streaming.test.ts`` (v1.1.0).

MISAKA fork: ``9540-extension-loader-lazy.test.ts`` is not translated; it checks that pi defers loading jiti (its
TypeScript loader), which MISAKA's Python extension loader does not use.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from misaka.ai.providers.faux import fauxAssistantMessage
from misaka.ai.types import Usage
from misaka.utils.values import read_field
from tests.suite_harness import createHarness, getMessageText, getUserTexts
from tests.utilities import assistantMsg, userMsg

NAVIGATION_BUSY = "Wait for the current compaction or tree navigation to finish before navigating the session tree."


def createDeferred() -> tuple[asyncio.Future[None], Any]:
    future: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def resolve() -> None:
        if not future.done():
            future.set_result(None)

    return future, resolve


def createUsage(totalTokens: int) -> Usage:
    return Usage.model_validate({
        "input": totalTokens,
        "output": 0,
        "cacheRead": 0,
        "cacheWrite": 0,
        "totalTokens": totalTokens,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
    })


# --- issue #9178: tree navigation during manual compaction --------------------------------------


async def test_rejects_navigation_before_the_active_leaf_can_change():
    compactionStarted, startCompaction = createDeferred()
    compactionReleased, releaseCompaction = createDeferred()

    def extension(pi: Any) -> None:
        async def onBeforeCompact(event: Any, _ctx: Any) -> Any:
            startCompaction()
            await compactionReleased
            preparation = read_field(event, "preparation")
            return {
                "compaction": {
                    "summary": "summary",
                    "firstKeptEntryId": read_field(preparation, "firstKeptEntryId"),
                    "tokensBefore": read_field(preparation, "tokensBefore"),
                    "details": {},
                }
            }

        pi.on("session_before_compact", onBeforeCompact)

    async with createHarness(settings={"compaction": {"keepRecentTokens": 1}}, extensionFactories=[extension]) as harness:
        harness.sessionManager.appendMessage(userMsg("first user"))
        navigationTargetId = harness.sessionManager.appendMessage(assistantMsg("first assistant"))
        harness.sessionManager.appendMessage(userMsg("second user"))
        originalLeafId = harness.sessionManager.appendMessage(assistantMsg("second assistant"))
        harness.session.agent.state.messages = harness.sessionManager.buildSessionContext().messages

        compactionPromise = asyncio.ensure_future(harness.session.compact())
        await compactionStarted

        assert harness.session.isCompacting is True
        try:
            with pytest.raises(Exception, match=NAVIGATION_BUSY):
                await harness.session.navigateTree(navigationTargetId, {"summarize": False})
            assert harness.sessionManager.getLeafId() == originalLeafId
        finally:
            releaseCompaction()
        await compactionPromise

        lastEntry = harness.sessionManager.getEntries()[-1]
        assert read_field(lastEntry, "type") == "compaction"
        assert read_field(lastEntry, "parentId") == originalLeafId
        assert "second assistant" in [getMessageText(message) for message in harness.session.messages]


async def test_rejects_a_second_navigation_while_the_first_is_waiting():
    navigationStarted, startNavigation = createDeferred()
    navigationReleased, releaseNavigation = createDeferred()

    def extension(pi: Any) -> None:
        async def onBeforeTree(_event: Any, _ctx: Any) -> None:
            startNavigation()
            await navigationReleased

        pi.on("session_before_tree", onBeforeTree)

    async with createHarness(extensionFactories=[extension]) as harness:
        secondTargetId = harness.sessionManager.appendMessage(userMsg("first user"))
        firstTargetId = harness.sessionManager.appendMessage(assistantMsg("first assistant"))
        harness.sessionManager.appendMessage(userMsg("second user"))
        originalLeafId = harness.sessionManager.appendMessage(assistantMsg("second assistant"))
        harness.session.agent.state.messages = harness.sessionManager.buildSessionContext().messages

        firstNavigation = asyncio.ensure_future(harness.session.navigateTree(firstTargetId, {"summarize": False}))
        await navigationStarted

        secondNavigation = asyncio.ensure_future(harness.session.navigateTree(secondTargetId, {"summarize": False}))
        assert harness.sessionManager.getLeafId() == originalLeafId
        releaseNavigation()

        with pytest.raises(Exception, match=NAVIGATION_BUSY):
            await secondNavigation
        await firstNavigation
        assert harness.sessionManager.getLeafId() == firstTargetId


# --- automatic compaction cancellation regressions ----------------------------------------------


def seedCompactableSession(harness: Any) -> None:
    model = harness.getModel()
    harness.sessionManager.appendMessage({"role": "user", "content": [{"type": "text", "text": "x" * 500}], "timestamp": 1})
    assistant = fauxAssistantMessage("y" * 200, {"timestamp": 2}).model_copy(update={
        "api": model.api,
        "provider": model.provider,
        "model": model.id,
        "usage": createUsage(100),
    })
    harness.sessionManager.appendMessage(assistant)
    harness.session.agent.state.messages = harness.sessionManager.buildSessionContext().messages


def runAutoCompaction(harness: Any) -> Any:
    return harness.session._runAutoCompaction("threshold", False)


def _cancel(pi: Any) -> None:
    pi.on("session_before_compact", lambda _event, _ctx: {"cancel": True})


# Regression test for #9340.
async def test_does_not_start_post_run_auto_compaction_after_abort():
    async with createHarness(
        models=[{"id": "faux-1", "contextWindow": 200, "maxTokens": 50}],
        settings={
            "compaction": {"enabled": True, "reserveTokens": 50, "keepRecentTokens": 1},
            "retry": {"enabled": False},
        },
        extensionFactories=[_cancel],
    ) as harness:
        seedCompactableSession(harness)
        harness.setResponses([
            fauxAssistantMessage("", {"stopReason": "error", "errorMessage": "Synthetic network failure"})
        ])
        pending: list[Any] = []

        def onEvent(event: Any) -> None:
            if read_field(event, "type") == "message_end" and read_field(read_field(event, "message"), "role") == "assistant":
                harness.session.abortCompaction()
                # `void harness.session.abort()`: the call runs at once, up to its first suspension.
                pending.append(asyncio.Task(harness.session.abort(), loop=asyncio.get_running_loop(), eager_start=True))

        harness.session.subscribe(onEvent)

        await harness.session.prompt("z" * 1000)
        await asyncio.gather(*pending)

        assert harness.eventsOfType("compaction_start") == []


# Regression test for #9777.
async def test_cancels_summarization_authentication(monkeypatch: pytest.MonkeyPatch):
    async with createHarness(settings={"compaction": {"keepRecentTokens": 1}}) as harness:
        seedCompactableSession(harness)
        authStarted, markAuthStarted = createDeferred()
        state: dict[str, Any] = {}

        async def getAuth(_model: Any, options: Any = None) -> Any:
            signal = read_field(options, "signal")
            state["authSignal"] = signal
            markAuthStarted()
            if signal is None:
                raise RuntimeError("Missing auth abort signal")
            rejected: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            signal.addEventListener(
                "abort", lambda: rejected.done() or rejected.set_exception(RuntimeError("aborted")), once=True
            )
            return await rejected

        monkeypatch.setattr(harness.session.modelRuntime, "getAuth", getAuth)

        compaction = asyncio.ensure_future(runAutoCompaction(harness))
        await authStarted
        started = len(harness.eventsOfType("compaction_start"))
        wasCompacting = harness.session.isCompacting
        await asyncio.gather(compaction, harness.session.abort())

        assert {
            "started": started,
            "wasCompacting": wasCompacting,
            "authAborted": state["authSignal"].aborted,
        } == {"started": 1, "wasCompacting": True, "authAborted": True}
        assert read_field(harness.eventsOfType("compaction_end")[-1], "aborted") is True


# Regression test for #9777.
async def test_cancels_synchronously_from_compaction_start():
    async with createHarness(settings={"compaction": {"keepRecentTokens": 1}}) as harness:
        seedCompactableSession(harness)

        def onEvent(event: Any) -> None:
            if read_field(event, "type") == "compaction_start":
                harness.session.abortCompaction()

        harness.session.subscribe(onEvent)

        await runAutoCompaction(harness)

        assert harness.faux.state.callCount == 0
        assert read_field(harness.eventsOfType("compaction_end")[-1], "aborted") is True


class _AbortError(Exception):
    name = "AbortError"


# Regression test for #9777.
@pytest.mark.parametrize(
    ("_label", "createError"),
    [
        ("matching error text", lambda: RuntimeError("Compaction cancelled")),
        ("an unrelated AbortError", lambda: _AbortError("auth failed")),
    ],
)
async def test_reports_errors_as_a_failure(monkeypatch: pytest.MonkeyPatch, _label: str, createError: Any):
    async with createHarness(settings={"compaction": {"keepRecentTokens": 1}}) as harness:
        seedCompactableSession(harness)

        async def getAuth(*_args: Any, **_kwargs: Any) -> Any:
            raise createError()

        monkeypatch.setattr(harness.session.modelRuntime, "getAuth", getAuth)

        await runAutoCompaction(harness)

        event = harness.eventsOfType("compaction_end")[-1]
        assert read_field(event, "aborted") is False
        assert str(createError()) in read_field(event, "errorMessage")


async def test_reports_extension_cancellation_as_aborted():
    async with createHarness(settings={"compaction": {"keepRecentTokens": 1}}, extensionFactories=[_cancel]) as harness:
        seedCompactableSession(harness)

        await runAutoCompaction(harness)

        assert read_field(harness.eventsOfType("compaction_end")[-1], "aborted") is True


# --- pre-prompt compaction regression -----------------------------------------------------------


async def test_compacts_length_stop_overflow_before_a_new_prompt_without_continuing_from_an_assistant_message(
    monkeypatch: pytest.MonkeyPatch,
):
    def extension(pi: Any) -> None:
        async def onBeforeCompact(event: Any, _ctx: Any) -> Any:
            preparation = read_field(event, "preparation")
            return {
                "compaction": {
                    "summary": "pre-prompt summary",
                    "firstKeptEntryId": read_field(preparation, "firstKeptEntryId"),
                    "tokensBefore": read_field(preparation, "tokensBefore"),
                    "details": {},
                }
            }

        pi.on("session_before_compact", onBeforeCompact)

    async with createHarness(
        models=[{"id": "faux-1", "contextWindow": 100, "maxTokens": 100}],
        settings={"compaction": {"enabled": True, "keepRecentTokens": 1, "reserveTokens": 0}},
        extensionFactories=[extension],
    ) as harness:
        now = int(time.time() * 1000)
        model = harness.getModel()
        harness.sessionManager.appendMessage({
            "role": "user",
            "content": [{"type": "text", "text": "previous prompt"}],
            "timestamp": now - 1000,
        })
        lengthStopAssistant = fauxAssistantMessage(
            "length-stop assistant response", {"stopReason": "length", "timestamp": now - 500}
        ).model_copy(update={"api": model.api, "provider": model.provider, "model": model.id, "usage": createUsage(100)})
        harness.sessionManager.appendMessage(lengthStopAssistant)
        harness.session.agent.state.messages = harness.sessionManager.buildSessionContext().messages
        harness.setResponses([fauxAssistantMessage("answered next prompt")])
        continueCalls: list[Any] = []
        originalContinue = harness.session.agent.continue_

        async def continueSpy(*args: Any, **kwargs: Any) -> Any:
            continueCalls.append(args)
            return await originalContinue(*args, **kwargs)

        monkeypatch.setattr(harness.session.agent, "continue_", continueSpy)

        assert await harness.session.prompt("next prompt") is None

        assert continueCalls == []
        compactionEnd = harness.eventsOfType("compaction_end")[-1]
        assert read_field(compactionEnd, "reason") == "overflow"
        assert read_field(compactionEnd, "aborted") is False
        assert read_field(compactionEnd, "willRetry") is True
        assert "next prompt" in getUserTexts(harness)
        assert harness.faux.state.callCount == 1


# --- tree navigation during an active response --------------------------------------------------


async def test_rejects_navigation_without_changing_the_active_leaf():
    async with createHarness() as harness:
        targetId = harness.sessionManager.appendMessage(userMsg("first"))
        state: dict[str, Any] = {"leafUnchanged": False}

        # Navigate from inside the response factory, while the run is active.
        async def respond(*_: Any) -> Any:
            activeLeafId = harness.sessionManager.getLeafId()
            try:
                state["navigationResult"] = await harness.session.navigateTree(targetId, {"summarize": False})
            except Exception as error:  # noqa: BLE001 - the rejection is the result under test
                state["navigationResult"] = error
            state["leafUnchanged"] = activeLeafId != targetId and harness.sessionManager.getLeafId() == activeLeafId
            return fauxAssistantMessage("response")

        harness.setResponses([respond])
        await harness.session.prompt("second")

        navigationResult = state.get("navigationResult")
        assert isinstance(navigationResult, Exception)
        assert str(navigationResult) == "Wait for the current response to finish before navigating the session tree."
        assert state["leafUnchanged"] is True

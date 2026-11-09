"""Translated from pi's ``packages/coding-agent/test/agent-session-auto-compaction-queue.test.ts`` (v1.1.0).

MISAKA fork: ``_checkCompaction`` hands every threshold candidate to ``_runAutoCompaction``, which applies the threshold
itself after asking an installed context engine; where pi expects no ``_runAutoCompaction`` call because the threshold
was not reached, the case runs it and checks that no compaction starts.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.agent import Agent
from misaka.ai.auth.types import ApiKeyCredential
from misaka.ai.compat import getModel, streamSimple
from misaka.ai.providers.faux import fauxAssistantMessage
from misaka.ai.types import AssistantMessage, DoneEvent, Usage
from misaka.ai.utils.event_stream import createAssistantMessageEventStream
from misaka.core.agent_session import AgentSession, AgentSessionConfig
from misaka.core.auth_storage import AuthStorage
from misaka.core.session_manager import SessionManager
from misaka.core.settings_manager import SettingsManager
from misaka.utils.values import read_field
from tests.model_runtime_test_utils import createModelRegistry, getModelRuntime
from tests.suite_harness import createTestResourceLoader

OVERFLOW_RETRY_FAILED = (
    "Context overflow recovery failed after one compact-and-retry attempt. Try reducing context or switching to a "
    "larger-context model."
)


def nowMs() -> int:
    return int(time.time() * 1000)


def usage(input: int, output: int = 0) -> dict[str, Any]:
    return {
        "input": input,
        "output": output,
        "cacheRead": 0,
        "cacheWrite": 0,
        "totalTokens": input + output,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
    }


def assistantMessage(model: Any, text: str, usage_: dict[str, Any], timestamp: int, **extra: Any) -> AssistantMessage:
    return AssistantMessage.model_validate({
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "api": model.api,
        "provider": model.provider,
        "model": model.id,
        "usage": usage_,
        "stopReason": "stop",
        "timestamp": timestamp,
        **extra,
    })


@pytest.fixture
async def ctx(tmp_path: Path):
    model = getModel("anthropic", "claude-sonnet-4-5")
    agent = Agent(streamFn=streamSimple, initialState={"model": model, "systemPrompt": "Test", "tools": []})

    sessionManager = SessionManager.inMemory()
    settingsManager = SettingsManager.create(str(tmp_path), str(tmp_path))
    authStorage = AuthStorage.create(str(tmp_path / "auth.json"))

    async def apiKey(_current: Any) -> ApiKeyCredential:
        return ApiKeyCredential(type="api_key", key="test-key")

    await authStorage.modify("anthropic", apiKey)
    modelRegistry = await createModelRegistry(authStorage, str(tmp_path / "models.json"))

    session = AgentSession(AgentSessionConfig(
        agent=agent,
        sessionManager=sessionManager,
        settingsManager=settingsManager,
        cwd=str(tmp_path),
        modelRuntime=getModelRuntime(modelRegistry),
        resourceLoader=createTestResourceLoader(),
    ))
    try:
        yield SimpleNamespace(session=session, sessionManager=sessionManager, settingsManager=settingsManager)
    finally:
        session.dispose()


def spyOnRunAutoCompaction(monkeypatch: pytest.MonkeyPatch, session: Any) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []

    async def runAutoCompaction(*args: Any, **_kwargs: Any) -> None:
        calls.append(args)

    monkeypatch.setattr(session, "_runAutoCompaction", runAutoCompaction)
    return calls


async def test_should_resume_after_threshold_compaction_when_only_agent_level_queued_messages_exist(
    ctx: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
):
    session = ctx.session
    ctx.settingsManager.applyOverrides({"compaction": {"keepRecentTokens": 1}})
    model = session.model
    now = nowMs()
    ctx.sessionManager.appendMessage({
        "role": "user",
        "content": [{"type": "text", "text": "message to compact"}],
        "timestamp": now - 1000,
    })
    ctx.sessionManager.appendMessage(assistantMessage(model, "assistant response to compact", usage(100), now - 500))
    session.agent.state.messages = ctx.sessionManager.buildSessionContext().messages

    def streamFunction(summaryModel: Any, *_: Any) -> Any:
        stream = createAssistantMessageEventStream()
        message = fauxAssistantMessage("compacted").model_copy(update={
            "api": summaryModel.api,
            "provider": summaryModel.provider,
            "model": summaryModel.id,
            "usage": Usage.model_validate(usage(10)),
        })
        asyncio.get_running_loop().call_soon(lambda: stream.push(DoneEvent(reason="stop", message=message)))
        return stream

    session.agent.streamFunction = streamFunction

    session.agent.followUp({
        "role": "custom",
        "customType": "test",
        "content": [{"type": "text", "text": "Queued custom"}],
        "display": False,
        "timestamp": nowMs(),
    })

    assert session.pendingMessageCount == 0
    assert session.agent.hasQueuedMessages() is True

    continueCalls: list[Any] = []

    async def continueSpy(*args: Any) -> None:
        continueCalls.append(args)

    monkeypatch.setattr(session.agent, "continue_", continueSpy)

    assert await session._runAutoCompaction("threshold", False) is True

    assert continueCalls == []


async def test_should_not_compact_repeatedly_after_overflow_recovery_already_attempted(
    ctx: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
):
    session = ctx.session
    model = session.model
    overflowMessage = assistantMessage(
        model, "", usage(0), nowMs(), stopReason="error", errorMessage="prompt is too long"
    )

    calls = spyOnRunAutoCompaction(monkeypatch, session)

    events: list[dict[str, Any]] = []

    def onEvent(event: Any) -> None:
        if read_field(event, "type") == "compaction_end":
            events.append({
                "type": read_field(event, "type"),
                "reason": read_field(event, "reason"),
                "errorMessage": read_field(event, "errorMessage"),
            })

    session.subscribe(onEvent)

    await session._checkCompaction(overflowMessage)
    await session._checkCompaction(overflowMessage.model_copy(update={"timestamp": nowMs() + 1}))

    assert len(calls) == 1
    assert {"type": "compaction_end", "reason": "overflow", "errorMessage": OVERFLOW_RETRY_FAILED} in events


async def test_should_ignore_stale_pre_compaction_assistant_usage_on_pre_prompt_compaction_checks(
    ctx: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
):
    session = ctx.session
    model = session.model
    staleAssistantTimestamp = nowMs() - 10_000
    staleAssistant = assistantMessage(
        model, "large response before compaction", usage(600_000, 10_000), staleAssistantTimestamp
    )

    ctx.sessionManager.appendMessage({
        "role": "user",
        "content": [{"type": "text", "text": "before compaction"}],
        "timestamp": staleAssistantTimestamp - 1000,
    })
    ctx.sessionManager.appendMessage(staleAssistant)

    firstKeptEntryId = read_field(ctx.sessionManager.getEntries()[0], "id")
    ctx.sessionManager.appendCompaction("summary", firstKeptEntryId, staleAssistant.usage.totalTokens, None, False)

    ctx.sessionManager.appendMessage({
        "role": "user",
        "content": [{"type": "text", "text": "session recovery payload"}],
        "timestamp": nowMs(),
    })

    calls = spyOnRunAutoCompaction(monkeypatch, session)

    await session._checkCompaction(staleAssistant, False)

    assert calls == []


async def test_should_trigger_threshold_compaction_for_error_messages_using_last_successful_usage(
    ctx: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
):
    session = ctx.session
    model = session.model

    # A successful assistant message with token usage just over the compaction threshold.
    # Compute this from the selected model so generated catalog context-window changes do not break the test.
    compactionSettings = ctx.settingsManager.getCompactionSettings()
    thresholdTokens = (model.contextWindow or 200_000) - compactionSettings["reserveTokens"] + 1
    successfulAssistant = assistantMessage(
        model, "large successful response", usage(thresholdTokens - 10_000, 10_000), nowMs()
    )

    # An error message (e.g. 529 overloaded) with no useful usage data
    errorAssistant = assistantMessage(
        model, "", usage(0), nowMs() + 1000, stopReason="error", errorMessage="529 overloaded"
    )

    # Put both messages into agent state so estimateContextTokens can find the successful one
    session.agent.state.messages = [
        {"role": "user", "content": [{"type": "text", "text": "hello"}], "timestamp": nowMs() - 1000},
        successfulAssistant,
        {"role": "user", "content": [{"type": "text", "text": "another prompt"}], "timestamp": nowMs() + 500},
        errorAssistant,
    ]

    calls = spyOnRunAutoCompaction(monkeypatch, session)

    await session._checkCompaction(errorAssistant)

    assert calls == [("threshold", False)]


async def test_should_not_trigger_threshold_compaction_for_error_messages_when_no_prior_usage_exists(
    ctx: SimpleNamespace,
):
    session = ctx.session
    model = session.model

    # An error message with no prior successful assistant in context
    errorAssistant = assistantMessage(
        model, "", usage(0), nowMs(), stopReason="error", errorMessage="529 overloaded"
    )

    session.agent.state.messages = [
        {"role": "user", "content": [{"type": "text", "text": "hello"}], "timestamp": nowMs() - 1000},
        errorAssistant,
    ]
    compactionStarts: list[Any] = []
    session.subscribe(
        lambda event: compactionStarts.append(event) if read_field(event, "type") == "compaction_start" else None
    )

    # MISAKA fork: see the module docstring.
    assert await session._checkCompaction(errorAssistant) is False
    assert compactionStarts == []


async def test_should_not_trigger_threshold_compaction_for_error_messages_when_only_kept_pre_compaction_usage_exists(
    ctx: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
):
    session = ctx.session
    model = session.model
    preCompactionTimestamp = nowMs() - 10_000

    # A "kept" assistant message from before compaction with high usage
    keptAssistant = assistantMessage(
        model, "kept response from before compaction", usage(180_000, 10_000), preCompactionTimestamp
    )

    # Record the kept assistant in the session and create a compaction after it
    ctx.sessionManager.appendMessage({
        "role": "user",
        "content": [{"type": "text", "text": "before compaction"}],
        "timestamp": preCompactionTimestamp - 1000,
    })
    ctx.sessionManager.appendMessage(keptAssistant)
    firstKeptEntryId = read_field(ctx.sessionManager.getEntries()[0], "id")
    ctx.sessionManager.appendCompaction("summary", firstKeptEntryId, keptAssistant.usage.totalTokens, None, False)

    # Post-compaction error message
    errorAssistant = assistantMessage(
        model, "", usage(0), nowMs(), stopReason="error", errorMessage="529 overloaded"
    )

    # Agent state has the kept assistant (pre-compaction) and the error (post-compaction)
    session.agent.state.messages = [
        {
            "role": "user",
            "content": [{"type": "text", "text": "kept user msg"}],
            "timestamp": preCompactionTimestamp - 1000,
        },
        keptAssistant,
        {"role": "user", "content": [{"type": "text", "text": "new prompt"}], "timestamp": nowMs() - 500},
        errorAssistant,
    ]

    calls = spyOnRunAutoCompaction(monkeypatch, session)

    await session._checkCompaction(errorAssistant)

    # Should NOT compact because the only usage data is from a kept pre-compaction message
    assert calls == []

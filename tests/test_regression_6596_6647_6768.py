"""Translated from pi's ``packages/coding-agent/test/suite/regressions/`` ``6596-taskkill-enoent.test.ts``,
``6647-compaction-retries-transient-stream-drop.test.ts`` and ``6768-copilot-compaction-base-url.test.ts``
(v1.1.0)."""

from __future__ import annotations

import asyncio
import ntpath
import subprocess
import time
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.auth.types import (
    ApiKeyAuth,
    AuthResult,
    ModelAuth,
    OAuthAuth,
    OAuthCredential,
    ProviderAuth,
)
from misaka.ai.models import ModelsRefreshOptions
from misaka.ai.providers.faux import fauxAssistantMessage
from misaka.ai.types import AssistantMessage, DoneEvent, ErrorEvent, TextContent, Usage
from misaka.ai.utils.event_stream import createAssistantMessageEventStream
from misaka.utils import shell
from misaka.utils.shell import killProcessTree
from misaka.utils.values import read_field
from tests.fake_timers import flush
from tests.suite_harness import createHarness

# --- issue #6596 taskkill spawn failures --------------------------------------------------------


def test_uses_system32_taskkill_and_consumes_its_spawn_error(monkeypatch: pytest.MonkeyPatch):
    # MISAKA fork: Python's Popen reports a failed spawn by raising at once, where Node emits "error" later; the
    # second call checks that killProcessTree swallows it.
    calls: list[tuple[Any, dict[str, Any]]] = []
    failures: list[OSError] = []

    def spawnMock(args: Any, **options: Any) -> Any:
        calls.append((args, options))
        if failures:
            raise failures.pop()
        return SimpleNamespace()

    monkeypatch.setattr(shell.subprocess, "Popen", spawnMock)
    monkeypatch.setenv("SystemRoot", "C:\\CustomWindows")
    monkeypatch.setattr(shell.os, "name", "nt")

    killProcessTree(1234)
    failures.append(FileNotFoundError("spawn taskkill ENOENT"))
    killProcessTree(1234)

    assert calls[0] == (
        [ntpath.join("C:\\CustomWindows", "System32", "taskkill.exe"), "/F", "/T", "/PID", "1234"],
        {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
            "creationflags": 0x00000008 | 0x00000200 | 0x08000000,
        },
    )
    assert len(calls) == 2


# --- #6647 compaction retries transient summarization failures ----------------------------------
#
# Compaction ran a single non-retried summarization call, so a transient mid-stream socket death (`terminated`) failed
# the whole compaction. Verifies that summarization now reuses `settings.retry` (bounded retries with exponential
# backoff gated on isRetryableAssistantError), emits `summarization_retry_*` events, and that aborts / non-retryable
# errors are not retried.


def createUsage(totalTokens: int) -> Usage:
    return Usage.model_validate({
        "input": totalTokens,
        "output": 0,
        "cacheRead": 0,
        "cacheWrite": 0,
        "totalTokens": totalTokens,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
    })


def seedCompactableSession(harness: Any) -> None:
    harness.settingsManager.applyOverrides({"compaction": {"keepRecentTokens": 1}})
    now = int(time.time() * 1000)
    harness.sessionManager.appendMessage({
        "role": "user",
        "content": [{"type": "text", "text": "message to compact"}],
        "timestamp": now - 1000,
    })
    model = harness.getModel()
    assistant = fauxAssistantMessage("", {"stopReason": "stop", "timestamp": now - 500}).model_copy(update={
        "api": model.api,
        "provider": model.provider,
        "model": model.id,
        "usage": createUsage(100),
    })
    assistant.content = [TextContent(text="assistant response to compact")]
    harness.sessionManager.appendMessage(assistant)
    harness.session.agent.state.messages = harness.sessionManager.buildSessionContext().messages


def useScriptedStreamFn(harness: Any, script: list[AssistantMessage]) -> Any:
    """streamFn that responds with the given sequence of assistant messages across calls."""
    state = {"callCount": 0}

    def streamFunction(model: Any, *_: Any) -> Any:
        message = script[state["callCount"]] if state["callCount"] < len(script) else script[-1]
        state["callCount"] += 1
        stream = createAssistantMessageEventStream()

        def respond() -> None:
            response = message.model_copy(update={"api": model.api, "provider": model.provider, "model": model.id})
            if response.stopReason == "pending":
                error = response.model_copy(update={
                    "stopReason": "error",
                    "errorMessage": "Scripted response ended without a stop reason",
                })
                stream.push(ErrorEvent(reason="error", error=error))
            elif response.stopReason in ("error", "aborted"):
                stream.push(ErrorEvent(reason=response.stopReason, error=response))
            else:
                stream.push(DoneEvent(reason=response.stopReason, message=response))

        asyncio.get_running_loop().call_soon(respond)
        return stream

    harness.session.agent.streamFunction = streamFunction
    return lambda: state["callCount"]


def errorMessage(text: str) -> AssistantMessage:
    return fauxAssistantMessage("", {"stopReason": "error", "errorMessage": text}).model_copy(
        update={"usage": createUsage(10)}
    )


async def test_retries_a_transient_terminated_summarization_error_and_compacts_successfully():
    async with createHarness(withConfiguredAuth=False) as harness:
        seedCompactableSession(harness)
        harness.settingsManager.applyOverrides({"retry": {"enabled": True, "maxRetries": 3, "baseDelayMs": 0}})

        model = harness.getModel()
        success = fauxAssistantMessage("recovered summary").model_copy(update={"usage": createUsage(10)})
        getCallCount = useScriptedStreamFn(harness, [errorMessage("terminated"), errorMessage("terminated"), success])

        result = await harness.session.compact()

        assert "recovered summary" in read_field(result, "summary")
        assert getCallCount() == 3  # 1 prefix-summary attempt + 2 retries
        starts = harness.eventsOfType("summarization_retry_scheduled")
        ends = harness.eventsOfType("summarization_retry_finished")
        assert len(starts) == 2
        assert len(ends) == 1
        assert read_field(starts[0], "attempt") == 1
        assert read_field(starts[0], "maxAttempts") == 3
        assert read_field(starts[0], "errorMessage") == "terminated"
        assert read_field(starts[1], "attempt") == 2
        assert read_field(starts[1], "maxAttempts") == 3
        assert read_field(ends[0], "type") == "summarization_retry_finished"
        # model.* referenced to keep imports honest
        assert model.id


async def test_does_not_retry_a_non_retryable_error_insufficient_quota():
    async with createHarness(withConfiguredAuth=False) as harness:
        seedCompactableSession(harness)
        harness.settingsManager.applyOverrides({"retry": {"enabled": True, "maxRetries": 3, "baseDelayMs": 0}})

        getCallCount = useScriptedStreamFn(harness, [errorMessage("insufficient_quota")])

        with pytest.raises(Exception, match="insufficient_quota"):
            await harness.session.compact()
        assert getCallCount() == 1
        assert harness.eventsOfType("summarization_retry_scheduled") == []


async def test_does_not_retry_when_retry_is_disabled():
    async with createHarness(withConfiguredAuth=False) as harness:
        seedCompactableSession(harness)
        harness.settingsManager.applyOverrides({"retry": {"enabled": False, "maxRetries": 3, "baseDelayMs": 0}})

        getCallCount = useScriptedStreamFn(harness, [errorMessage("terminated")])

        with pytest.raises(Exception, match="terminated"):
            await harness.session.compact()
        assert getCallCount() == 1
        assert harness.eventsOfType("summarization_retry_scheduled") == []


async def test_stops_retrying_after_max_retries_and_reports_failure():
    async with createHarness(withConfiguredAuth=False) as harness:
        seedCompactableSession(harness)
        harness.settingsManager.applyOverrides({"retry": {"enabled": True, "maxRetries": 2, "baseDelayMs": 0}})

        error = errorMessage("terminated")
        getCallCount = useScriptedStreamFn(harness, [error, error, error])

        with pytest.raises(Exception, match="terminated"):
            await harness.session.compact()
        assert getCallCount() == 3  # 1 initial + 2 retries
        starts = harness.eventsOfType("summarization_retry_scheduled")
        ends = harness.eventsOfType("summarization_retry_finished")
        assert len(starts) == 2
        assert len(ends) == 1
        assert read_field(ends[0], "type") == "summarization_retry_finished"


async def test_aborts_an_in_flight_retry_backoff_via_abort_compaction():
    async with createHarness(withConfiguredAuth=False) as harness:
        seedCompactableSession(harness)
        harness.settingsManager.applyOverrides({"retry": {"enabled": True, "maxRetries": 5, "baseDelayMs": 30_000}})

        error = errorMessage("terminated")
        useScriptedStreamFn(harness, [error, error, error])

        compactPromise = asyncio.ensure_future(harness.session.compact())
        # Let the first error resolve and the retry backoff sleep start.
        await flush()
        harness.session.abortCompaction()

        # The aborted retry backoff is normalized to an aborted assistant message,
        # which compaction classifies as aborted.
        with pytest.raises(Exception):  # noqa: B017 - upstream only expects a rejection
            await compactPromise
        compactionEnd = harness.eventsOfType("compaction_end")[-1]
        assert read_field(compactionEnd, "aborted") is True


# --- issue #6768 Copilot compaction base URL ----------------------------------------------------

INDIVIDUAL_BASE_URL = "https://api.individual.githubcopilot.com"
ENTERPRISE_BASE_URL = "https://api.enterprise.githubcopilot.com"


async def test_uses_the_auth_resolved_base_url_through_the_sdk_style_stream_wrapper():
    async with createHarness() as harness:
        seedCompactableSession(harness)
        catalogModel = harness.getModel().model_copy(update={"baseUrl": INDIVIDUAL_BASE_URL})
        harness.session.agent.state.model = catalogModel

        requested: dict[str, Any] = {}

        def respond(requestModel: Any, *_: Any) -> Any:
            requested["baseUrl"] = requestModel.baseUrl
            stream = createAssistantMessageEventStream()
            stream.push(DoneEvent(
                reason="stop",
                message=fauxAssistantMessage("summary").model_copy(update={
                    "api": requestModel.api,
                    "provider": requestModel.provider,
                    "model": requestModel.id,
                }),
            ))
            return stream

        async def resolve(*, credential: Any = None, **_: Any) -> AuthResult | None:
            key = read_field(credential, "key")
            return AuthResult(auth=ModelAuth(apiKey=key), source="explicit token") if key else None

        async def login(*_: Any) -> Any:
            raise RuntimeError("unused")

        async def refresh(credential: Any, *_: Any) -> Any:
            return credential

        async def toAuth(credential: Any) -> ModelAuth:
            return ModelAuth(apiKey=credential.access, baseUrl=ENTERPRISE_BASE_URL)

        provider = SimpleNamespace(
            id=catalogModel.provider,
            name="Copilot regression provider",
            baseUrl=INDIVIDUAL_BASE_URL,
            auth=ProviderAuth(
                apiKey=ApiKeyAuth(name="Copilot token", resolve=resolve),
                oauth=OAuthAuth(name="Copilot OAuth", login=login, refresh=refresh, toAuth=toAuth),
            ),
            getModels=lambda: [catalogModel],
            stream=respond,
            streamSimple=respond,
        )

        async def oauthCredential(_current: Any) -> OAuthCredential:
            return OAuthCredential(
                access="enterprise-token",
                refresh="refresh-token",
                expires=int(time.time() * 1000) + 60 * 60_000,
            )

        await harness.authStorage.modify(catalogModel.provider, oauthCredential)
        modelRuntime = harness.session.modelRuntime
        modelRuntime.registerNativeProvider(provider)
        await modelRuntime.refresh(ModelsRefreshOptions(allowNetwork=False, providers=[catalogModel.provider]))
        harness.session.agent.streamFunction = lambda model, context, options=None: modelRuntime.streamSimple(
            model, context, options
        )

        await harness.session.compact()

        assert requested.get("baseUrl") == ENTERPRISE_BASE_URL


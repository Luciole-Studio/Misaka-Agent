"""pi durable's ``test/harness-generation.test.ts``."""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.providers.faux import fauxAssistantMessage
from misaka.ai.types import DeferredHandle
from misaka.durable import (
    AssistantEntry,
    GenerationTask,
    Harness,
    LiveDoc,
    MemoryStorage,
    ProviderDoc,
    UserEntry,
    createRegistry,
    defineDoc,
    defineExtension,
    wrapSection,
)
from misaka.durable.harness.agent import resolveSettings
from misaka.utils.js import AbortSignal
from tests.chat_support import (
    FakeStream,
    WithOverrides,
    allEntries,
    chatSetup,
    invalidFinalStream,
    openChat,
    textOf,
    unanswered,
    waitFor,
    withStream,
)
from tests.durable_support import AnyOf, ControlledStorage, context, toMatchObject
from tests.harness_support import addSection

ERROR_503 = fauxAssistantMessage([], {"stopReason": "error", "errorMessage": "503 Service Unavailable"})


def live(harness: Any, conversation: Any) -> Any:
    return harness.snapshot(LiveDoc, conversation.id, context)


async def runTask(harness: Any, conversation: Any) -> int:
    found: dict[str, Any] = {}

    async def check() -> bool:
        value = await live(harness, conversation)
        found["id"] = None if value is None else value.get("run", {}).get("taskId")
        return found["id"] is not None

    await waitFor(check)
    return found["id"]  # type: ignore[no-any-return]


def livePublications(harness: Any) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []

    def onCommit(publication: Any, _context: Any) -> None:
        for change in publication["changes"]:
            if change["type"] == "document" and change["record"]["kind"] == "pi.live" and change["value"] is not None:
                values.append(change["value"])

    harness.subscribeCommits(onCommit)
    return values


def generationOf(value: dict[str, Any]) -> dict[str, Any]:
    return value.get("generation") or {}


async def test_answers_an_input_and_settles_its_submission():
    setup = chatSetup()
    addSection(setup.registry, "preamble", lambda *_: "You are helpful.", {"tag": False})
    setup.faux.setResponses([fauxAssistantMessage("Hello there")])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    harness.resume()
    submission = await root.submit({"type": "input", "content": "hi"}, context)
    settled = await submission.wait(context)
    if settled["status"] != "done" or settled["type"] != "input":
        raise RuntimeError(f"Unexpected {settled['status']}")

    answer = await root.commit(lambda tx: tx.entry(AssistantEntry, settled["answer"]), context)
    assert textOf(answer["model"][0]) == "Hello there"
    entries = await allEntries(root)
    assert [entry["kind"] for entry in entries] == ["pi.user", "pi.system", "pi.assistant"]
    assert entries[0]["id"] == settled["entry"]
    assert entries[1]["model"] == [
        {"role": "system", "content": "", "sections": {"preamble": "You are helpful."}, "timestamp": AnyOf(int)}
    ]
    assert await live(harness, root) == {}
    task = (await harness.commit(lambda tx: tx.scanTasks({"conversationId": root.id}, 10), context))["items"][0]
    assert task["kind"] == "pi.generation"
    # Entries written by the generation are attributed to it; the admitted user entry is not task work.
    assert [entry.get("byTaskId") for entry in entries] == [None, task["id"], task["id"]]
    assert task["state"] == {
        "status": "terminal",
        "outcome": {"status": "completed", "result": {"entryId": settled["answer"]}},
    }
    await harness.close(context)


async def test_stores_partials_as_deltas_and_a_complete_base_once_nothing_is_in_flight():
    setup = chatSetup({"tokensPerSecond": 200, "tokenSize": {"min": 1, "max": 1}})
    setup.faux.setResponses([fauxAssistantMessage("w" * 200)])
    storage = ControlledStorage()
    opened = await openChat(storage, setup)
    harness, root = opened.harness, opened.root
    record = await storage.findDocument(
        {"kind": "pi.live", "scope": {"kind": "conversation", "conversationId": root.id}}, "current", context
    )
    harness.resume()
    await (await root.submit({"type": "input", "content": "hi"}, context)).wait(context)
    contents = [
        write["content"]["kind"]
        for writes in storage.commits
        for write in writes
        if write["type"] == "document.change" and write["id"] == record["id"]
    ]
    # Streaming writes deltas; the commit that settles the answer clears generation and writes a base.
    assert "delta" in contents
    assert contents[-1] == "base"
    await harness.close(context)


async def test_still_ends_a_run_whose_input_something_else_already_settled():
    setup = chatSetup()
    busy = unanswered()
    setup.faux.setResponses([busy.step])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    submission = await root.submit({"type": "input", "content": "hi"}, context)
    await busy.reached
    await root.commit(
        lambda tx: tx.settleSubmission(submission.id, {"status": "unanswered", "reason": "withdrawn"}), context
    )
    taskId = await runTask(harness, root)
    await harness.abortTask(taskId, context)
    assert (await harness.waitForTask(taskId, context))["state"]["outcome"] == {"status": "aborted"}
    # The earlier settlement stays; the run's own settlement leaves it unchanged.
    toMatchObject(await submission.status(context), {"status": "unanswered", "reason": "withdrawn"})
    assert await live(harness, root) == {}
    await harness.close(context)


async def test_fails_with_no_model_when_no_model_is_configured_or_the_model_is_unknown():
    setup = chatSetup()
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    plain = await harness.createConversation({"ownership": {"kind": "ownerless"}}, context)
    harness.resume()
    unset = await (await plain.submit({"type": "input", "content": "hi"}, context)).wait(context)
    toMatchObject(unset, {"status": "unanswered", "reason": "no_model"})

    await root.configure({"model": {"provider": "faux", "modelId": "missing"}}, context)
    unknown = await (await root.submit({"type": "input", "content": "hi"}, context)).wait(context)
    toMatchObject(unknown, {"status": "unanswered", "reason": "no_model", "entry": AnyOf(int)})
    assert [entry["kind"] for entry in await allEntries(root)] == ["pi.user"]
    tasks = await harness.commit(lambda tx: tx.scanTasks({"conversationId": root.id}, 10), context)
    assert tasks["items"][0]["state"] == {
        "status": "terminal",
        "outcome": {
            "status": "failed",
            "error": {"message": "Model faux/missing is not available", "detail": {"reason": "no_model"}},
        },
    }
    assert await live(harness, root) == {}
    assert await live(harness, plain) == {}
    await harness.close(context)


async def test_retries_a_retryable_error_after_a_durable_backoff_and_then_answers():
    setup = chatSetup()
    addSection(setup.registry, "preamble", lambda *_: "p", {"tag": False})
    setup.faux.setResponses([ERROR_503, fauxAssistantMessage("recovered")])
    setup.settings["retry"] = {"enabled": True, "maxRetries": 3, "baseDelayMs": 1}
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    values = livePublications(harness)
    harness.resume()
    settled = await (await root.submit({"type": "input", "content": "hi"}, context)).wait(context)
    assert settled["status"] == "done"
    entries = await allEntries(root)
    assert [entry["kind"] for entry in entries] == ["pi.user", "pi.system", "pi.assistant", "pi.assistant"]
    assert entries[2]["model"][0]["stopReason"] == "error"
    assert any(generationOf(value).get("retry", {}).get("error") == "503 Service Unavailable" for value in values)
    assert any(generationOf(value).get("attempt") == 2 for value in values)
    assert await live(harness, root) == {}
    await harness.close(context)


async def test_fails_with_model_error_once_retries_are_exhausted():
    setup = chatSetup()
    setup.faux.setResponses([ERROR_503, ERROR_503, fauxAssistantMessage("never")])
    setup.settings["retry"] = {"enabled": True, "maxRetries": 1, "baseDelayMs": 1}
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    harness.resume()
    settled = await (await root.submit({"type": "input", "content": "hi"}, context)).wait(context)
    toMatchObject(settled, {"status": "unanswered", "reason": "model_error", "detail": "503 Service Unavailable"})
    assert [entry["kind"] for entry in await allEntries(root)] == ["pi.user", "pi.assistant", "pi.assistant"]
    assert setup.faux.getPendingResponseCount() == 1
    await harness.close(context)


async def test_fails_a_retryable_error_without_retrying_when_the_retry_policy_is_disabled():
    setup = chatSetup()
    setup.faux.setResponses([ERROR_503, fauxAssistantMessage("never")])
    setup.settings["retry"] = {"enabled": False}
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    harness.resume()
    settled = await (await root.submit({"type": "input", "content": "hi"}, context)).wait(context)
    toMatchObject(settled, {"status": "unanswered", "reason": "model_error"})
    assert setup.faux.state.callCount == 1
    await harness.close(context)


async def test_reports_section_wrapper_failures_while_preparing():
    setup = chatSetup()
    addSection(setup.registry, "cwd", lambda *_: "/repo")

    def broken(inner: Any) -> Any:
        raise RuntimeError("wrapper failed")

    setup.registry.install(defineExtension({"name": "broken", "wraps": [wrapSection("cwd", broken)]}))
    setup.faux.setResponses([fauxAssistantMessage("ok")])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    harness.resume()
    assert (await (await root.submit({"type": "input", "content": "hi"}, context)).wait(context))["status"] == "done"
    assert any(str(error) == "wrapper failed" for error in setup.reports)
    # The failed section is absent, so nothing was rendered.
    assert [entry["kind"] for entry in await allEntries(root)] == ["pi.user", "pi.assistant"]
    await harness.close(context)


async def test_fails_a_non_retryable_error_without_retrying():
    setup = chatSetup()
    setup.faux.setResponses(
        [fauxAssistantMessage([], {"stopReason": "error", "errorMessage": "Invalid request"}), fauxAssistantMessage("never")]
    )
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    harness.resume()
    settled = await (await root.submit({"type": "input", "content": "hi"}, context)).wait(context)
    toMatchObject(settled, {"status": "unanswered", "reason": "model_error", "detail": "Invalid request"})
    tasks = await harness.commit(lambda tx: tx.scanTasks({"conversationId": root.id}, 10), context)
    toMatchObject(
        tasks["items"][0]["state"],
        {"outcome": {"status": "failed", "error": {"message": "Invalid request", "detail": {"reason": "model_error"}}}},
    )
    await harness.close(context)


async def test_polls_a_deferred_response_until_it_is_ready():
    setup = chatSetup({"deferred": {"pendingFetches": 1, "pollAfterMs": 1}})
    setup.faux.setResponses([fauxAssistantMessage("deferred answer")])
    setup.settings["stream"] = {"deferred": True}
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    values = livePublications(harness)
    harness.resume()
    settled = await (await root.submit({"type": "input", "content": "hi"}, context)).wait(context)
    if settled["status"] != "done" or settled["type"] != "input":
        raise RuntimeError(f"Unexpected {settled['status']}")
    assert setup.faux.state.deferredFetchCount == 2
    pollTimes = [
        generationOf(value)["deferred"]["pollAt"] for value in values if generationOf(value).get("deferred") is not None
    ]
    assert len(pollTimes) == 2
    assert pollTimes[1] > pollTimes[0]
    answer = await root.commit(lambda tx: tx.entry(AssistantEntry, settled["answer"]), context)
    assert textOf(answer["model"][0]) == "deferred answer"
    await harness.close(context)


async def test_converts_the_committed_partial_when_aborted_during_streaming():
    setup = chatSetup({"tokensPerSecond": 20, "tokenSize": {"min": 1, "max": 1}})
    setup.faux.setResponses([fauxAssistantMessage("x" * 400)])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    harness.resume()
    submission = await root.submit({"type": "input", "content": "hi"}, context)
    taskId = await runTask(harness, root)

    async def streaming() -> bool:
        return textOf(generationOf(await live(harness, root)).get("message")) is not None

    await waitFor(streaming)
    partial = generationOf(await live(harness, root))["message"]
    assert await harness.abortTask(taskId, context) == "marked"
    toMatchObject(await submission.wait(context), {"status": "unanswered", "reason": "aborted"})
    entries = await allEntries(root)
    assert [entry["kind"] for entry in entries] == ["pi.user", "pi.assistant"]
    converted = entries[1]["model"][0]
    assert converted["stopReason"] == "aborted"
    convertedText, partialText = textOf(converted), textOf(partial)
    assert convertedText is not None
    assert partialText is not None
    assert convertedText.startswith(partialText)
    assert await live(harness, root) == {}
    assert (await harness.waitForTask(taskId, context))["state"]["outcome"] == {"status": "aborted"}
    await harness.close(context)


async def test_cancels_a_deferred_response_when_aborted_during_polling():
    setup = chatSetup({"deferred": {"pendingFetches": 100, "pollAfterMs": 60_000}})
    setup.faux.setResponses([fauxAssistantMessage("never")])
    setup.settings["stream"] = {"deferred": True}
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    harness.resume()
    submission = await root.submit({"type": "input", "content": "hi"}, context)
    taskId = await runTask(harness, root)

    async def polling() -> bool:
        return generationOf(await live(harness, root)).get("deferred") is not None

    await waitFor(polling)
    await harness.abortTask(taskId, context)
    toMatchObject(await submission.wait(context), {"status": "unanswered", "reason": "aborted"})
    assert len(setup.faux.state.cancelledDeferred) == 1
    assert await live(harness, root) == {}
    await harness.close(context)


async def test_reports_a_failed_deferred_cancellation_and_still_ends_the_run_aborted():
    base = chatSetup({"deferred": {"pendingFetches": 100, "pollAfterMs": 60_000}})
    base.faux.setResponses([fauxAssistantMessage("never")])

    async def cancelDeferred(*_: Any) -> None:
        raise RuntimeError("cancel failed")

    setup = SimpleNamespace(**{**vars(base), "models": WithOverrides(base.models, cancelDeferred=cancelDeferred)})
    setup.settings["stream"] = {"deferred": True}
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    harness.resume()
    submission = await root.submit({"type": "input", "content": "hi"}, context)
    taskId = await runTask(harness, root)

    async def polling() -> bool:
        return generationOf(await live(harness, root)).get("deferred") is not None

    await waitFor(polling)
    await harness.abortTask(taskId, context)
    toMatchObject(await submission.wait(context), {"status": "unanswered", "reason": "aborted"})
    assert any(str(error) == "cancel failed" for error in setup.reports)
    assert await live(harness, root) == {}
    await harness.close(context)


async def test_forwards_stream_options_and_the_thinking_level():
    setup = chatSetup()
    seen: list[Any] = []

    def first(_context: Any, options: Any, *_: Any) -> Any:
        seen.append(options)
        return fauxAssistantMessage("a")

    def second(_context: Any, options: Any, *_: Any) -> Any:
        seen.append(options)
        return fauxAssistantMessage("b")

    setup.faux.setResponses([first, second])
    setup.settings["stream"] = {"timeoutMs": 1234, "headers": {"x-test": "1"}}
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    await root.configure({"thinkingLevel": "high"}, context)
    harness.resume()
    await (await root.submit({"type": "input", "content": "one"}, context)).wait(context)
    # Both are read at the next preparation: the thinking level from pi.agent, the stream options live from settings.
    await root.configure({"thinkingLevel": None}, context)
    setup.settings["stream"] = {"timeoutMs": 99}
    await (await root.submit({"type": "input", "content": "two"}, context)).wait(context)
    sessionId = (await harness.snapshot(ProviderDoc, root.id, context))["sessionId"]
    assert seen[0].timeoutMs == 1234
    assert seen[0].headers == {"x-test": "1"}
    assert seen[0].reasoning == "high"
    assert seen[0].sessionId == sessionId
    assert isinstance(seen[0].signal, AbortSignal)
    assert seen[1].reasoning is None
    assert seen[1].timeoutMs == 99
    assert seen[1].sessionId == sessionId
    assert seen[1].headers is None
    await harness.close(context)


# Regression coverage for #10424.
async def test_keeps_provider_session_ids_request_local_across_concurrent_conversations():
    setup = chatSetup()
    seen: dict[str, list[str]] = {}

    def capture(request: Any, options: Any, *_: Any) -> Any:
        message = next((candidate for candidate in reversed(request.messages) if candidate.role == "user"), None)
        content = None if message is None else message.content
        text = content if isinstance(content, str) else ""
        seen.setdefault(text, []).append((None if options is None else options.sessionId) or "")
        return fauxAssistantMessage(f"answer:{text}")

    setup.faux.setResponses([capture, capture, capture, capture])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    child = await harness.createConversation(
        {"ownership": {"kind": "ownerless"}, "agent": {"model": {"provider": "faux", "modelId": "faux-1"}}},
        context,
    )
    harness.resume()

    async def submit(conversation: Any, content: str) -> None:
        await (await conversation.submit({"type": "input", "content": content}, context)).wait(context)

    await asyncio.gather(*(submit(conversation, f"first-{index}") for index, conversation in enumerate([root, child])))
    await asyncio.gather(*(submit(conversation, f"second-{index}") for index, conversation in enumerate([root, child])))
    rootId = (await harness.snapshot(ProviderDoc, root.id, context))["sessionId"]
    childId = (await harness.snapshot(ProviderDoc, child.id, context))["sessionId"]
    assert rootId != childId
    assert seen.get("first-0") == [rootId]
    assert seen.get("second-0") == [rootId]
    assert seen.get("first-1") == [childId]
    assert seen.get("second-1") == [childId]
    await harness.close(context)


# Regression coverage for #10424.
async def test_creates_and_persists_provider_state_before_a_legacy_conversations_request():
    setup = chatSetup()
    sent: dict[str, Any] = {}

    def respond(_request: Any, options: Any, *_: Any) -> Any:
        sent["sessionId"] = None if options is None else options.sessionId
        return fauxAssistantMessage("ok")

    setup.faux.setResponses([respond])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    await root.commit(lambda tx: tx.retireDoc(ProviderDoc, root.id), context)
    assert await harness.snapshot(ProviderDoc, root.id, context) is None
    harness.resume()
    await (await root.submit({"type": "input", "content": "legacy"}, context)).wait(context)
    stored = await harness.snapshot(ProviderDoc, root.id, context)
    assert re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", stored["sessionId"])
    assert sent["sessionId"] == stored["sessionId"]
    await harness.close(context)


async def test_reads_settings_through_getters_at_every_decision():
    setup = chatSetup()
    timeoutMs = 111
    seen: list[int | None] = []

    def first(_context: Any, options: Any, *_: Any) -> Any:
        nonlocal timeoutMs
        seen.append(options.timeoutMs)
        # The user changes the setting while the first attempt runs.
        timeoutMs = 222
        return ERROR_503

    def second(_context: Any, options: Any, *_: Any) -> Any:
        seen.append(options.timeoutMs)
        return fauxAssistantMessage("ok")

    setup.faux.setResponses([first, second])

    class GetterSettings(dict[str, Any]):
        """Settings whose ``stream`` is a getter."""

        def get(self, key: str, default: Any = None) -> Any:
            if key == "stream":
                return {"timeoutMs": timeoutMs}
            return super().get(key, default)

    settings = GetterSettings(retry={"baseDelayMs": 1})
    harness = await Harness.open(
        MemoryStorage(), {"models": setup.models, "registry": setup.registry, "settings": settings}, context
    )
    root = await harness.root(context, {"agent": {"model": {"provider": "faux", "modelId": "faux-1"}}})
    harness.resume()
    assert (await (await root.submit({"type": "input", "content": "hi"}, context)).wait(context))["status"] == "done"
    # The retry prepares again, so it resolves the settings again and sends the new timeout.
    assert seen == [111, 222]
    await harness.close(context)


def test_resolves_settings_over_the_built_in_defaults():
    assert resolveSettings(None) == {
        "stream": {},
        "retry": {"enabled": True, "maxRetries": 3, "baseDelayMs": 2000, "maxAgentDelayMs": 60000},
        "compaction": {"enabled": True, "reserveTokens": 16384, "keepRecentTokens": 20000, "backgroundTokens": 32768},
        "progress": {"partialIntervalMs": 100, "outputIntervalMs": 100},
        "toolExecution": "parallel",
        "steeringMode": "one-at-a-time",
        "followUpMode": "one-at-a-time",
        "contextRetentionMs": 600_000,
        "documentRetentionMs": AnyOf(int, float),
    }
    toMatchObject(
        resolveSettings({"retry": {"enabled": False}, "compaction": {"backgroundTokens": 0}}),
        {
            "retry": {"enabled": False, "maxRetries": 3, "baseDelayMs": 2000},
            "compaction": {"enabled": True, "backgroundTokens": 0},
        },
    )
    assert resolveSettings({"progress": {"outputIntervalMs": 500}})["progress"] == {
        "partialIntervalMs": 100,
        "outputIntervalMs": 500,
    }


async def test_commits_partials_no_more_often_than_progress_partialintervalms():
    def shortStream(*_: Any) -> FakeStream:
        async def events() -> Any:
            yield SimpleNamespace(type="start", partial=fauxAssistantMessage("partial", {"stopReason": "pending"}))
            # Longer than the default 100 ms, shorter than the configured interval.
            await asyncio.sleep(0.3)

        return FakeStream(events, fauxAssistantMessage("final"))

    async def publishedPartial(settings: dict[str, Any]) -> bool:
        base = chatSetup()
        setup = SimpleNamespace(**{**vars(base), "settings": settings, "models": withStream(base.models, shortStream)})
        opened = await openChat(MemoryStorage(), setup)
        harness, root = opened.harness, opened.root
        values = livePublications(harness)
        harness.resume()
        submission = await root.submit({"type": "input", "content": "hi"}, context)
        toMatchObject(await submission.wait(context), {"status": "done"})
        await harness.close(context)
        return any(textOf((value.get("generation") or {}).get("message")) == "partial" for value in values)

    assert await publishedPartial({}) is True
    assert await publishedPartial({"progress": {"partialIntervalMs": 5000}}) is False


async def test_renders_sections_that_read_conversation_documents_through_input_read():
    Agent = defineDoc(
        {
            "kind": "test.agent",
            "version": 1,
            "scope": "conversation",
            "history": "latest",
            "fork": "current",
            "initial": lambda: {"cwd": "/", "kind": "main"},
        }
    )
    setup = chatSetup()

    async def cwd(input: Any, ctx: Any) -> Any:
        agent = await input["read"].snapshot(Agent, input["conversationId"], ctx)
        return None if agent is None else agent["cwd"]

    async def agents(input: Any, ctx: Any) -> Any:
        agent = await input["read"].snapshot(Agent, input["conversationId"], ctx)
        return None if agent is not None and agent["kind"] == "sub" else "Read AGENTS.md"

    addSection(setup.registry, "cwd", cwd)
    addSection(setup.registry, "agents", agents)
    setup.faux.setResponses([fauxAssistantMessage("a"), fauxAssistantMessage("b")])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root

    async def setRepo(tx: Any) -> None:
        (await tx.doc(Agent, root.id))["cwd"] = "/repo"

    await root.commit(setRepo, context)

    async def initSub(tx: Any, id: int) -> None:
        agent = await tx.doc(Agent, id)
        agent["kind"] = "sub"
        agent["cwd"] = "/sub"

    sub = await harness.createConversation(
        {
            "ownership": {"kind": "ownerless"},
            "agent": {"model": {"provider": "faux", "modelId": "faux-1"}},
            "init": initSub,
        },
        context,
    )
    harness.resume()
    await (await root.submit({"type": "input", "content": "one"}, context)).wait(context)
    await (await sub.submit({"type": "input", "content": "two"}, context)).wait(context)

    async def sections(conversation: Any) -> Any:
        entry = next((entry for entry in await allEntries(conversation) if entry["kind"] == "pi.system"), None)
        return None if entry is None else entry["model"][0]

    toMatchObject(
        await sections(root),
        {"sections": {"cwd": "<cwd>\n/repo\n</cwd>", "agents": "<agents>\nRead AGENTS.md\n</agents>"}},
    )
    assert await sections(sub) == {
        "role": "system",
        "content": "",
        "sections": {"cwd": "<cwd>\n/sub\n</cwd>"},
        "timestamp": AnyOf(int),
    }
    await harness.close(context)


async def test_commits_no_partial_for_a_response_that_turns_deferred_after_an_empty_start_event():
    base = chatSetup()
    handle = DeferredHandle(provider="faux", modelId="faux-1", api="faux", id="handle-1", pollAfterMs=60_000)

    def deferredStream(*_: Any) -> FakeStream:
        async def events() -> Any:
            yield SimpleNamespace(type="start", partial=fauxAssistantMessage([], {"stopReason": "pending"}))
            # Longer than the partial throttle: an empty partial would be committed here.
            await asyncio.sleep(0.3)

        return FakeStream(events, fauxAssistantMessage([], {"stopReason": "deferred", "deferred": handle}))

    setup = SimpleNamespace(**{**vars(base), "models": withStream(base.models, deferredStream)})
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    values = livePublications(harness)
    submission = await root.submit({"type": "input", "content": "hi"}, context)

    async def deferredReached() -> bool:
        return generationOf(await live(harness, root)).get("deferred") is not None

    await waitFor(deferredReached)
    assert not any(generationOf(value).get("message") is not None for value in values)
    await harness.abortTask((await live(harness, root))["run"]["taskId"], context)
    toMatchObject(await submission.wait(context), {"status": "unanswered", "reason": "aborted"})
    assert [entry["kind"] for entry in await allEntries(root)] == ["pi.user"]
    await harness.close(context)


async def test_faults_a_run_task_settling_its_inputs_and_converting_the_committed_partial():
    base = chatSetup()
    setup = SimpleNamespace(**{**vars(base), "models": withStream(base.models, invalidFinalStream)})
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    values = livePublications(harness)
    harness.resume()
    submission = await root.submit({"type": "input", "content": "hi"}, context)
    settled = await submission.wait(context)
    toMatchObject(settled, {"status": "unanswered", "reason": "faulted"})
    assert "non-JSON function" in settled["detail"]
    assert any(textOf(generationOf(value).get("message")) == "partial" for value in values)
    entries = await allEntries(root)
    assert [entry["kind"] for entry in entries] == ["pi.user", "pi.assistant"]
    toMatchObject(entries[1]["model"][0], {"role": "assistant", "stopReason": "aborted"})
    assert textOf(entries[1]["model"][0]) == "partial"
    assert await live(harness, root) == {}
    tasks = await harness.commit(lambda tx: tx.scanTasks({"conversationId": root.id}, 10), context)
    toMatchObject(tasks["items"][0]["state"], {"status": "terminal", "outcome": {"status": "faulted"}})
    await harness.close(context)


async def test_orphans_a_blocked_run_task_with_full_run_cleanup():
    setup = chatSetup()
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root

    # A run whose task was stored by a newer generation definition this process cannot run.
    async def seed(tx: Any) -> dict[str, int]:
        entry = await tx.appendEntry(UserEntry, root.id, {"model": [{"role": "user", "content": "hi", "timestamp": 1}]})
        submission = await tx.createSubmission(
            {"conversationId": root.id, "type": "input", "status": "placed", "entry": entry["id"]}
        )
        taskId = await tx.createTask(
            {"definition": {**GenerationTask["definition"], "version": 2}},
            {},
            {"ownership": {"kind": "conversation"}, "conversationId": root.id},
        )
        (await tx.doc(LiveDoc, root.id))["run"] = {"taskId": taskId, "inputs": [submission["id"]]}
        return {"taskId": taskId, "submissionId": submission["id"]}

    seeded = await harness.commitWith(seed, context)
    taskId, submissionId = seeded["taskId"], seeded["submissionId"]
    harness.resume()
    with pytest.raises(Exception, match="is busy"):
        await root.submit({"type": "input", "content": "busy", "whenBusy": "reject"}, context)
    assert await harness.abortTask(taskId, context) == "marked"
    assert (await harness.waitForTask(taskId, context))["state"]["outcome"] == {
        "status": "orphaned",
        "reason": "task_too_old",
    }
    toMatchObject(
        await (await harness.submission(submissionId, context)).status(context),
        {"status": "unanswered", "reason": "task_too_old"},
    )
    assert await live(harness, root) == {}
    await harness.close(context)


async def test_rejects_a_registry_without_the_built_in_tasks():
    empty = createRegistry().snapshot()
    snapshot = SimpleNamespace(
        installed=list,
        extension=lambda name: None,
        tools=list,
        sections=list,
        tasks=lambda: [task for task in empty.tasks() if task["definition"]["name"] != "pi.generation"],
        task=lambda name: None if name == "pi.generation" else empty.task(name),
    )
    registry = SimpleNamespace(snapshot=lambda: snapshot, subscribe=lambda listener: lambda: None)
    with pytest.raises(Exception, match="Registry lacks built-in tasks pi.generation"):
        await Harness.open(MemoryStorage(), {"models": chatSetup().models, "registry": registry}, context)

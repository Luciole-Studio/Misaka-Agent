"""pi durable's ``test/harness-generation-recovery.test.ts``."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.models import createModels
from misaka.ai.providers.faux import fauxAssistantMessage
from misaka.durable import AssistantEntry, LiveDoc
from misaka.durable.storage.sqlite.node import openNodeSqliteStorage
from tests.chat_support import (
    allEntries,
    chatSetup,
    openChat,
    textOf,
    unanswered,
    waitFor,
)
from tests.durable_support import context
from tests.harness_support import addSection
from tests.task_support import aborted, deferred


@pytest.fixture
def sqlitePath() -> Iterator[str]:
    directory = tempfile.mkdtemp(prefix="pi-durable-generation-")
    yield os.path.join(directory, "session.sqlite")
    shutil.rmtree(directory, ignore_errors=True)


async def open(path: str, setup: Any) -> Any:
    return await openChat(await openNodeSqliteStorage(path), setup)


async def runTaskId(harness: Any) -> int:
    live = await harness.snapshot(LiveDoc, 1, context)
    return live["run"]["taskId"]  # type: ignore[no-any-return]


async def checkpoint(harness: Any, id: int) -> Any:
    record = await harness.getTask(id, context)
    if record is None or record["state"]["status"] == "terminal":
        return None
    return record["state"]["checkpoint"]


def roles(request: Any) -> list[str]:
    return [message.role for message in request.messages]


async def test_reruns_preparation_interrupted_before_its_commit(sqlitePath):
    setup = chatSetup()
    reached = deferred()
    block = True

    async def preamble(_input: Any, ctx: Any) -> str:
        nonlocal block
        if block:
            block = False
            reached.resolve()
            await aborted(ctx.abortSignal)
        return "p"

    addSection(setup.registry, "preamble", preamble)
    setup.faux.setResponses([fauxAssistantMessage("answer")])
    opened = await open(sqlitePath, setup)
    opened.harness.resume()
    id = (await opened.root.submit({"type": "input", "content": "hi"}, context)).id
    await reached.promise
    taskId = await runTaskId(opened.harness)
    await opened.harness.close(context)

    opened = await open(sqlitePath, setup)
    assert await checkpoint(opened.harness, taskId) == {"phase": "prepare", "attempt": 1}
    opened.harness.resume()
    assert (await (await opened.harness.submission(id, context)).wait(context))["status"] == "done"
    assert [entry["kind"] for entry in await allEntries(opened.root)] == ["pi.user", "pi.system", "pi.assistant"]
    await opened.harness.close(context)


async def test_resends_a_request_interrupted_before_any_partial_without_repeating_preparation(sqlitePath):
    setup = chatSetup()
    addSection(setup.registry, "preamble", lambda *_: "p", {"tag": False})
    reached = deferred()
    sent: list[list[str]] = []
    timeouts: list[int | None] = []

    async def interrupted(_context: Any, options: Any, *_: Any) -> Any:
        reached.resolve()
        return await aborted(options.signal)

    def resent(request: Any, options: Any, *_: Any) -> Any:
        sent.append(roles(request))
        timeouts.append(options.timeoutMs)
        return fauxAssistantMessage("answer")

    setup.faux.setResponses([interrupted, resent])
    opened = await open(sqlitePath, setup)
    setup.settings["stream"] = {"timeoutMs": 1234}
    opened.harness.resume()
    id = (await opened.root.submit({"type": "input", "content": "hi"}, context)).id
    await reached.promise
    taskId = await runTaskId(opened.harness)
    await opened.harness.close(context)

    opened = await open(sqlitePath, setup)
    pinned = await checkpoint(opened.harness, taskId)
    assert pinned["phase"] == "request"
    assert pinned["attempt"] == 1
    assert pinned["thinkingLevel"] == "off"
    assert pinned["streamOptions"]["timeoutMs"] == 1234
    # The resend uses the pinned request, not options changed after preparation.
    setup.settings["stream"] = {"timeoutMs": 999}
    live = await opened.harness.snapshot(LiveDoc, opened.root.id, context)
    assert live["generation"]["attempt"] == 1
    opened.harness.resume()
    assert (await (await opened.harness.submission(id, context)).wait(context))["status"] == "done"
    assert sent == [["system", "user"]]
    assert timeouts == [1234]
    assert [entry["kind"] for entry in await allEntries(opened.root)] == ["pi.user", "pi.system", "pi.assistant"]
    await opened.harness.close(context)


async def test_converts_a_committed_partial_into_an_aborted_entry_and_resends_the_same_messages(sqlitePath):
    slow = chatSetup({"tokensPerSecond": 20, "tokenSize": {"min": 1, "max": 1}})
    slow.faux.setResponses([fauxAssistantMessage("z" * 400)])
    opened = await open(sqlitePath, slow)
    opened.harness.resume()
    watch = await opened.harness.watchDoc(LiveDoc, opened.root.id, context)
    watched: str | None = None

    async def onValue(value: Any, ops: Any, ctx: Any) -> None:
        nonlocal watched
        generation = None if value is None else value.get("generation")
        message = None if generation is None else generation.get("message")
        text = textOf(message)
        watched = text if text is not None else watched

    watch.start(onValue)
    id = (await opened.root.submit({"type": "input", "content": "hi"}, context)).id
    await waitFor(lambda: watched is not None)
    await opened.harness.close(context)

    setup = chatSetup()
    sent: list[list[str]] = []

    def resent(request: Any, *_: Any) -> Any:
        sent.append(roles(request))
        return fauxAssistantMessage("answer")

    setup.faux.setResponses([resent])
    opened = await open(sqlitePath, setup)
    stored = await opened.harness.snapshot(LiveDoc, opened.root.id, context)
    partial = textOf(stored["generation"]["message"])
    assert partial is not None
    # Everything observers saw before the crash is durable.
    assert watched is not None
    assert partial.startswith(watched)
    opened.harness.resume()
    assert (await (await opened.harness.submission(id, context)).wait(context))["status"] == "done"
    assert sent == [["user"]]
    entries = await allEntries(opened.root)
    assert [entry["kind"] for entry in entries] == ["pi.user", "pi.assistant", "pi.assistant"]
    converted = entries[1]["model"][0]
    assert converted["stopReason"] == "aborted"
    assert textOf(converted) == partial
    assert await opened.harness.snapshot(LiveDoc, opened.root.id, context) == {}
    await opened.harness.close(context)


async def test_resumes_a_retry_backoff_after_reopen(sqlitePath):
    setup = chatSetup()
    now = 1_000
    setup.now = lambda: now
    setup.faux.setResponses(
        [
            fauxAssistantMessage([], {"stopReason": "error", "errorMessage": "503 Service Unavailable"}),
            fauxAssistantMessage("recovered"),
        ]
    )
    opened = await open(sqlitePath, setup)
    opened.harness.resume()
    setup.settings["retry"] = {"enabled": True, "maxRetries": 2, "baseDelayMs": 60_000}
    id = (await opened.root.submit({"type": "input", "content": "hi"}, context)).id

    async def retrying() -> bool:
        live = await opened.harness.snapshot(LiveDoc, opened.root.id, context)
        return live.get("generation", {}).get("retry") is not None  # type: ignore[no-any-return]

    await waitFor(retrying)
    taskId = await runTaskId(opened.harness)
    await opened.harness.close(context)

    opened = await open(sqlitePath, setup)
    assert await checkpoint(opened.harness, taskId) == {"phase": "retry", "attempt": 1, "until": 61_000}
    assert await opened.harness.snapshot(LiveDoc, opened.root.id, context) == {
        "run": {"taskId": taskId, "inputs": [id]},
        "generation": {"attempt": 1, "retry": {"at": 61_000, "error": "503 Service Unavailable"}},
    }
    now = 61_000
    opened.harness.resume()
    assert (await (await opened.harness.submission(id, context)).wait(context))["status"] == "done"
    assert [entry["kind"] for entry in await allEntries(opened.root)] == ["pi.user", "pi.assistant", "pi.assistant"]
    await opened.harness.close(context)


async def test_resumes_polling_a_deferred_response_after_reopen(sqlitePath):
    setup = chatSetup({"deferred": {"pollAfterMs": 60_000}})
    now = 1_000
    setup.now = lambda: now
    setup.faux.setResponses([fauxAssistantMessage("deferred answer")])
    opened = await open(sqlitePath, setup)
    opened.harness.resume()
    setup.settings["stream"] = {"deferred": True}
    id = (await opened.root.submit({"type": "input", "content": "hi"}, context)).id

    async def polling() -> bool:
        live = await opened.harness.snapshot(LiveDoc, opened.root.id, context)
        return live.get("generation", {}).get("deferred") is not None  # type: ignore[no-any-return]

    await waitFor(polling)
    taskId = await runTaskId(opened.harness)
    await opened.harness.close(context)

    opened = await open(sqlitePath, setup)
    pinned = await checkpoint(opened.harness, taskId)
    assert pinned["phase"] == "poll"
    assert pinned["attempt"] == 1
    assert pinned["pollAt"] == 61_000
    now = 61_000
    opened.harness.resume()
    settled = await (await opened.harness.submission(id, context)).wait(context)
    if settled["status"] != "done" or settled["type"] != "input":
        raise RuntimeError(f"Unexpected {settled['status']}")
    answer = await opened.root.commit(lambda tx: tx.entry(AssistantEntry, settled["answer"]), context)
    assert textOf(answer["model"][0]) == "deferred answer"
    assert setup.faux.state.deferredFetchCount == 1
    await opened.harness.close(context)


async def test_fails_no_model_when_the_pinned_model_is_gone_after_reopen_in_request_and_in_poll(sqlitePath):
    setup = chatSetup({"deferred": {"pollAfterMs": 60_000}})
    busy = unanswered()
    setup.faux.setResponses([busy.step, fauxAssistantMessage("deferred")])
    opened = await open(sqlitePath, setup)
    opened.harness.resume()
    requesting = (await opened.root.submit({"type": "input", "content": "one"}, context)).id
    await busy.reached
    await opened.harness.close(context)

    # Reopened without the faux provider: the request's pinned model is unknown.
    empty = SimpleNamespace(**{**vars(setup), "models": createModels()})
    opened = await open(sqlitePath, empty)
    opened.harness.resume()
    settled = await (await opened.harness.submission(requesting, context)).wait(context)
    assert settled["status"] == "unanswered"
    assert settled["reason"] == "no_model"
    await opened.harness.close(context)

    opened = await open(sqlitePath, setup)
    setup.settings["stream"] = {"deferred": True}
    opened.harness.resume()
    polling = (await opened.root.submit({"type": "input", "content": "two"}, context)).id

    async def deferredReached() -> bool:
        live = await opened.harness.snapshot(LiveDoc, opened.root.id, context)
        return live.get("generation", {}).get("deferred") is not None  # type: ignore[no-any-return]

    await waitFor(deferredReached)
    await opened.harness.close(context)

    opened = await open(sqlitePath, empty)
    opened.harness.resume()
    settled = await (await opened.harness.submission(polling, context)).wait(context)
    assert settled["status"] == "unanswered"
    assert settled["reason"] == "no_model"
    await opened.harness.close(context)


async def test_runs_a_print_style_turn_and_reads_the_durable_answer_after_reopen(sqlitePath):
    setup = chatSetup()
    addSection(setup.registry, "preamble", lambda *_: "You are terse.", {"tag": False})
    setup.faux.setResponses([fauxAssistantMessage("42")])
    opened = await open(sqlitePath, setup)
    opened.harness.resume()
    submission = await opened.root.submit({"type": "input", "content": "answer?"}, context)
    settled = await submission.wait(context)
    if settled["status"] != "done" or settled["type"] != "input":
        raise RuntimeError(f"Unexpected {settled['status']}")
    answer = await opened.root.commit(lambda tx: tx.entry(AssistantEntry, settled["answer"]), context)
    assert textOf(answer["model"][0]) == "42"
    await opened.harness.close(context)

    opened = await open(sqlitePath, setup)
    assert await (await opened.harness.submission(submission.id, context)).status(context) == settled
    assert await opened.root.commit(lambda tx: tx.entry(AssistantEntry, settled["answer"]), context) == answer
    await opened.harness.close(context)

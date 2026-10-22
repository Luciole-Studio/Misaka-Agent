"""pi durable's ``test/harness-input-entries.test.ts``: inputs placed as application entries, and data-only writes
appended at once (#10407)."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.providers.faux import fauxAssistantMessage, fauxText, fauxToolCall
from misaka.durable import (
    ConversationBusy,
    InboxDoc,
    LiveDoc,
    MemoryStorage,
    defineTool,
    watchEvents,
)
from misaka.durable.storage.sqlite.node import openNodeSqliteStorage
from tests.chat_support import (
    allEntries,
    chatSetup,
    openChat,
    requestText,
    textOf,
    waitFor,
)
from tests.durable_support import context, toMatchObject
from tests.harness_support import addSection, addTool
from tests.task_support import aborted, deferred, race

EMPTY_OBJECT = {"type": "object", "properties": {}}


@pytest.fixture
def sqliteDirectory() -> Iterator[str]:
    directory = tempfile.mkdtemp(prefix="pi-durable-input-entries-")
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


def answer(text: str) -> Any:
    return fauxAssistantMessage([fauxText(text)])


def gated(message: Any) -> SimpleNamespace:
    """A faux response held until ``release`` or cancellation; ``reached`` resolves when the request is sent."""
    reached = deferred()
    gate = deferred()

    async def step(_request: Any, options: Any, *_: Any) -> Any:
        reached.resolve()
        await race(gate.promise, aborted(options.signal))
        return message

    return SimpleNamespace(step=step, reached=reached.promise, release=lambda: gate.resolve())


def holdTool(setup: Any, gate: Any) -> None:
    """Register a ``hold`` tool whose calls wait for ``gate``."""

    async def execute(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        await gate.promise
        return {"output": [{"type": "text", "text": "held"}]}

    addTool(
        setup.registry,
        defineTool(
            {"name": "hold", "description": "Waits for the test", "parameters": EMPTY_OBJECT, "execute": execute}
        ),
    )


HOLD = fauxAssistantMessage([fauxToolCall("hold", {}, {"id": "c1"})], {"stopReason": "toolUse"})


def requestLog(requests: list[list[str]], reply: Any) -> Any:
    """Role and text of each request message, as the provider receives them."""

    def step(request: Any, *_: Any) -> Any:
        requests.append([f"{message.role}:{requestText(message) or ''}" for message in request.messages])
        return reply

    return step


def transcript(entries: list[dict[str, Any]]) -> list[list[Any]]:
    """Kind, application data, and model text of each entry, skipping system entries."""
    result = []
    for entry in entries:
        if entry["kind"] == "pi.system":
            continue
        model = entry.get("model")
        message = model[0] if model else None
        text = None if message is None or message["role"] == "toolResult" else textOf(message)
        data = [] if "data" not in entry or entry["kind"].startswith("pi.") else [entry["data"]]
        result.append([entry["kind"], *data, *([] if text is None else [text])])
    return result


async def inboxItems(harness: Any, root: Any) -> list[dict[str, Any]]:
    return (await harness.snapshot(InboxDoc, root.id, context))["items"]  # type: ignore[no-any-return]


async def toolRunning(harness: Any, root: Any) -> None:
    async def running() -> bool:
        live = await harness.snapshot(LiveDoc, root.id, context)
        tools = None if live is None else live.get("tools")
        return tools is not None and len(tools) > 0 and tools[0]["status"] == "running"  # type: ignore[no-any-return]

    await waitFor(running)


def mention(text: str) -> dict[str, Any]:
    return {"kind": "chat.mention", "data": {"author": "ann", "text": text}}


# ─── Input entries ──────────────────────────────────────────────────────────


async def test_input_entries_places_an_idle_input_as_its_entry_the_given_kind_and_data_with_the_content_as_its_user_message():
    setup = chatSetup()
    setup.now = lambda: 42
    requests: list[list[str]] = []
    setup.faux.setResponses([requestLog(requests, answer("hi ann"))])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    submission = await root.submit({"type": "input", "content": "ann: hi", "entry": mention("hi")}, context)
    settled = await submission.wait(context)
    entries = await allEntries(root)
    assert transcript(entries) == [
        ["chat.mention", {"author": "ann", "text": "hi"}, "ann: hi"],
        ["pi.assistant", "hi ann"],
    ]
    placed = next(entry for entry in entries if entry["kind"] == "chat.mention")
    assert placed["model"] == [{"role": "user", "content": "ann: hi", "timestamp": 42}]
    toMatchObject(settled, {"status": "done", "entry": placed["id"], "answer": entries[-1]["id"]})
    assert requests == [["user:ann: hi"]]
    # The context of a fork at the input is the same as for a pi.user entry.
    view = await root.context(context, {"at": placed["id"]})
    assert [message for message in view["messages"] if message["role"] != "system"] == placed["model"]
    await harness.close(context)


async def test_input_entries_rejects_an_input_entry_whose_kind_is_not_a_non_empty_string_or_starts_with_pi_writing_nothing():
    setup = chatSetup()
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    before = len(await allEntries(root))
    for kind in [123, "", "pi.user", "pi.compaction"]:
        draft = {"type": "input", "content": "x", "entry": {"kind": kind}}
        with pytest.raises(Exception, match="An input entry's kind must be"):
            await root.submit(draft, context)
    assert len(await allEntries(root)) == before
    assert await harness.snapshot(LiveDoc, root.id, context) == {}
    # Writes keep their kinds: reset() writes pi.reset.
    await root.reset(None, context)
    assert (await allEntries(root))[-1]["kind"] == "pi.reset"
    await harness.close(context)


async def test_input_entries_takes_only_kind_and_data_from_an_untyped_callers_entry():
    setup = chatSetup()
    first = gated(answer("first"))
    setup.faux.setResponses([answer("idle"), first.step, answer("queued")])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    extra = {
        "kind": "chat.mention",
        "model": [{"role": "user", "content": "forged", "timestamp": 0}],
        "head": "self",
        "edits": [{"target": 1, "action": "omit"}],
    }

    def draft(content: str) -> dict[str, Any]:
        return {"type": "input", "content": content, "entry": extra}

    await (await root.submit(draft("idle"), context)).wait(context)
    await root.submit({"type": "input", "content": "a"}, context)
    await first.reached
    queued = await root.submit(draft("later"), context)
    assert (await inboxItems(harness, root))[-1] == {
        "id": queued.id,
        "mode": "followUp",
        "content": "later",
        "entry": {"kind": "chat.mention"},
    }
    first.release()
    await queued.wait(context)
    mentions = [entry for entry in await allEntries(root) if entry["kind"] == "chat.mention"]
    assert [
        [
            entry.get("head"),
            entry.get("edits"),
            textOf((entry.get("model") or [None])[0]),
            len(entry["model"]) if "model" in entry else None,
        ]
        for entry in mentions
    ] == [
        [None, None, "idle", 1],
        [None, None, "later", 1],
    ]
    await harness.close(context)


async def test_input_entries_queues_busy_inputs_with_their_entries_in_the_inbox_and_places_them_as_those_entries_at_boundaries():
    setup = chatSetup()
    gate = deferred()
    holdTool(setup, gate)
    setup.faux.setResponses([HOLD, answer("after tools"), answer("follow-up")])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    input = await root.submit({"type": "input", "content": "a"}, context)
    await toolRunning(harness, root)
    steer = await root.submit(
        {"type": "input", "content": "s", "entry": mention("s"), "whenBusy": "steer", "requestId": "r-s"}, context
    )
    followUp = await root.submit(
        {"type": "input", "content": "f", "entry": {"kind": "p2.input"}, "whenBusy": "followUp"}, context
    )
    assert await inboxItems(harness, root) == [
        {
            "id": steer.id,
            "mode": "steer",
            "content": "s",
            "entry": {"kind": "chat.mention", "data": {"author": "ann", "text": "s"}},
        },
        {"id": followUp.id, "mode": "followUp", "content": "f", "entry": {"kind": "p2.input"}},
    ]
    # The request ID deduplicates a queued input with an entry as any other.
    again = await root.submit(
        {"type": "input", "content": "other", "entry": mention("other"), "whenBusy": "steer", "requestId": "r-s"},
        context,
    )
    assert again.id == steer.id
    with pytest.raises(ConversationBusy):
        await root.submit({"type": "input", "content": "x", "entry": mention("x"), "whenBusy": "reject"}, context)
    assert len(await inboxItems(harness, root)) == 2

    gate.resolve()
    await followUp.wait(context)
    entries = await allEntries(root)
    assert transcript(entries) == [
        ["pi.user", "a"],
        ["pi.assistant"],
        ["pi.tool-result"],
        ["chat.mention", {"author": "ann", "text": "s"}, "s"],
        ["pi.assistant", "after tools"],
        ["p2.input", "f"],
        ["pi.assistant", "follow-up"],
    ]
    answers = [entry for entry in entries if entry["kind"] == "pi.assistant"]

    def placed(kind: str) -> int:
        return next(entry for entry in entries if entry["kind"] == kind)["id"]  # type: ignore[no-any-return]

    toMatchObject(await input.status(context), {"status": "done", "answer": answers[1]["id"]})
    toMatchObject(
        await steer.status(context), {"status": "done", "entry": placed("chat.mention"), "answer": answers[1]["id"]}
    )
    toMatchObject(
        await followUp.status(context), {"status": "done", "entry": placed("p2.input"), "answer": answers[2]["id"]}
    )
    await harness.close(context)


async def test_input_entries_withdraws_queued_inputs_with_entries_one_by_one_and_by_conversation_abort():
    setup = chatSetup()
    first = gated(answer("first"))
    setup.faux.setResponses([first.step])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    input = await root.submit({"type": "input", "content": "a"}, context)
    await first.reached
    withdrawn = await root.submit({"type": "input", "content": "w", "entry": mention("w")}, context)
    abortedInput = await root.submit({"type": "input", "content": "x", "entry": mention("x")}, context)
    assert await withdrawn.abort(context) == "aborted"
    assert [item["id"] for item in await inboxItems(harness, root)] == [abortedInput.id]
    await root.abort(context)
    for submission in (withdrawn, abortedInput):
        toMatchObject(await submission.wait(context), {"status": "unanswered", "reason": "aborted"})
    toMatchObject(await input.wait(context), {"status": "unanswered", "reason": "aborted"})
    assert await inboxItems(harness, root) == []
    assert not any(entry["kind"] == "chat.mention" for entry in await allEntries(root))
    await harness.close(context)


# ─── Data-only writes ───────────────────────────────────────────────────────


async def test_data_only_writes_appends_a_data_only_write_mid_run_in_time_order_leaving_the_models_messages_unchanged():
    setup = chatSetup()
    gate = deferred()
    holdTool(setup, gate)
    requests: list[list[str]] = []
    setup.faux.setResponses([requestLog(requests, HOLD), requestLog(requests, answer("done"))])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    events: list[dict[str, Any]] = []
    stream = await watchEvents(harness, root.id, context)

    async def onBatch(batch: Any, *_: Any) -> None:
        events.extend(batch)

    stream.start(onBatch)
    input = await root.submit({"type": "input", "content": "a"}, context)
    await toolRunning(harness, root)
    chat = {"kind": "chat.message", "data": {"author": "bob", "text": "lunch?"}}
    write = await root.submit({"type": "write", "entry": chat, "requestId": "bob:1"}, context)
    # Settled and appended in its own commit, between the call and its still-running tool's result.
    settled = await write.status(context)
    assert settled["status"] == "done"
    assert await inboxItems(harness, root) == []
    assert transcript(await allEntries(root)) == [
        ["pi.user", "a"],
        ["pi.assistant"],
        ["chat.message", {"author": "bob", "text": "lunch?"}],
    ]
    await waitFor(lambda: any(event["type"] == "entry_appended" for event in events))
    appended = next(event for event in events if event["type"] == "entry_appended")
    toMatchObject(appended, {"entry": {"kind": "chat.message", "id": settled["entry"]}})
    assert not any(event["type"] == "run_end" for event in events)
    # A repeated request ID returns the same write without a second entry.
    assert (await root.submit({"type": "write", "entry": chat, "requestId": "bob:1"}, context)).id == write.id

    gate.resolve()
    await input.wait(context)
    assert transcript(await allEntries(root)) == [
        ["pi.user", "a"],
        ["pi.assistant"],
        ["chat.message", {"author": "bob", "text": "lunch?"}],
        ["pi.tool-result"],
        ["pi.assistant", "done"],
    ]
    assert requests == [
        ["system:", "user:a"],
        ["system:", "user:a", "assistant:", "toolResult:held"],
    ]
    await stream.stop()
    await harness.close(context)


async def test_data_only_writes_queues_a_data_only_write_behind_a_queued_reset_so_it_lands_after_the_reset_and_stays_visible():
    setup = chatSetup()
    first = gated(answer("first"))
    setup.faux.setResponses([first.step])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    input = await root.submit({"type": "input", "content": "a"}, context)
    await first.reached
    await root.reset(None, context)
    chat = await root.submit({"type": "write", "entry": {"kind": "chat.message", "data": "hi"}}, context)
    assert (await chat.status(context))["status"] == "queued"
    first.release()
    await input.wait(context)
    settled = await chat.wait(context)
    entries = await allEntries(root)
    reset = next(entry for entry in entries if entry["kind"] == "pi.reset")
    assert settled["status"] == "done"
    assert settled["entry"] > reset["id"]
    assert [entry["kind"] for entry in (await root.context(context))["entries"]] == ["pi.reset", "chat.message"]
    # Once the reset is placed, display-only writes go in at once again.
    later = await root.submit({"type": "write", "entry": {"kind": "chat.message", "data": "again"}}, context)
    assert (await later.status(context))["status"] == "done"
    await harness.close(context)


async def test_data_only_writes_leaves_the_request_unchanged_by_a_data_only_write_that_lands_while_the_generation_prepares():
    setup = chatSetup()
    rendering = deferred()
    render = deferred()
    renders = 0

    async def slow(*_: Any) -> str:
        nonlocal renders
        current = renders
        renders += 1
        if current == 1:
            rendering.resolve()
            await render.promise
        return "SLOW"

    addSection(setup.registry, "slow", slow)
    requests: list[list[str]] = []
    setup.faux.setResponses([requestLog(requests, answer("one")), requestLog(requests, answer("two"))])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    await (await root.submit({"type": "input", "content": "a"}, context)).wait(context)
    second = await root.submit({"type": "input", "content": "b"}, context)
    await rendering.promise
    await root.submit({"type": "write", "entry": {"kind": "chat.message", "data": "hi"}}, context)
    render.resolve()
    await second.wait(context)
    assert requests[1] == ["system:", "user:a", "assistant:one", "user:b"]
    kinds = [entry["kind"] for entry in await allEntries(root)]
    # The unchanged prompt adds no second pi.system entry.
    assert kinds == ["pi.user", "pi.system", "pi.assistant", "pi.user", "chat.message", "pi.assistant"]
    await harness.close(context)


async def test_data_only_writes_appends_a_data_only_write_while_idle_after_a_failed_run_without_placing_the_queued_items():
    setup = chatSetup()
    failing = gated(fauxAssistantMessage([], {"stopReason": "error", "errorMessage": "invalid request"}))
    setup.faux.setResponses([failing.step, answer("for f")])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    input = await root.submit({"type": "input", "content": "a"}, context)
    await failing.reached
    f = await root.submit({"type": "input", "content": "f", "entry": mention("f")}, context)
    failing.release()
    toMatchObject(await input.wait(context), {"status": "unanswered", "reason": "model_error"})
    await harness.waitForIdle(context)
    note = await root.submit({"type": "write", "entry": {"kind": "note", "data": "n"}}, context)
    assert (await note.status(context))["status"] == "done"
    await harness.waitForIdle(context)
    assert (await f.status(context))["status"] == "queued"
    assert [item["id"] for item in await inboxItems(harness, root)] == [f.id]
    assert await harness.snapshot(LiveDoc, root.id, context) == {}
    assert setup.faux.state.callCount == 1
    # The next input places the waiting mention first.
    g = await root.submit({"type": "input", "content": "g"}, context)
    await g.wait(context)
    assert [row for row in transcript(await allEntries(root)) if row[0] != "pi.assistant"] == [
        ["pi.user", "a"],
        ["note", "n"],
        ["chat.mention", {"author": "ann", "text": "f"}, "f"],
        ["pi.user", "g"],
    ]
    await harness.close(context)


async def test_data_only_writes_keeps_a_queued_inputs_entry_across_reopen_and_places_it_as_that_entry(
    sqliteDirectory: str,
):
    path = os.path.join(sqliteDirectory, "session.sqlite")
    setup = chatSetup()
    first = gated(answer("first"))
    setup.faux.setResponses([first.step, answer("first again"), answer("for f")])
    opened = await openChat(await openNodeSqliteStorage(path), setup)
    await opened.root.submit({"type": "input", "content": "a"}, context)
    await first.reached
    f = (await opened.root.submit({"type": "input", "content": "f", "entry": mention("f")}, context)).id
    await opened.harness.close(context)

    opened = await openChat(await openNodeSqliteStorage(path), setup)
    item = next((item for item in await inboxItems(opened.harness, opened.root) if item["id"] == f), None)
    toMatchObject(item, {"entry": mention("f")})
    settled = await (await opened.harness.submission(f, context)).wait(context)
    placed = next(entry for entry in await allEntries(opened.root) if entry["kind"] == "chat.mention")
    toMatchObject(settled, {"status": "done", "entry": placed["id"]})
    toMatchObject(placed, {"data": mention("f")["data"], "model": [{"role": "user", "content": "f"}]})
    await opened.harness.close(context)


async def test_data_only_writes_appends_a_data_only_write_at_once_behind_a_non_empty_inbox_and_still_queues_writes_with_model_or_edits():
    setup = chatSetup()
    first = gated(answer("first"))
    setup.faux.setResponses([first.step, answer("second")])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    input = await root.submit({"type": "input", "content": "a"}, context)
    await first.reached
    followUp = await root.submit({"type": "input", "content": "f"}, context)
    model = await root.submit(
        {"type": "write", "entry": {"kind": "note", "model": [{"role": "user", "content": "m", "timestamp": 0}]}},
        context,
    )
    target = next(entry for entry in await allEntries(root) if entry["kind"] == "pi.user")
    edit = await root.submit(
        {"type": "write", "entry": {"kind": "redact", "edits": [{"target": target["id"], "action": "omit"}]}}, context
    )
    note = await root.submit({"type": "write", "entry": {"kind": "note", "data": "n"}}, context)
    assert (await note.status(context))["status"] == "done"
    assert [[item["id"], item["mode"]] for item in await inboxItems(harness, root)] == [
        [followUp.id, "followUp"],
        [model.id, "write"],
        [edit.id, "write"],
    ]
    first.release()
    await followUp.wait(context)
    assert transcript(await allEntries(root)) == [
        ["pi.user", "a"],
        ["note", "n"],
        ["pi.assistant", "first"],
        ["note", "m"],
        ["redact"],
        ["pi.user", "f"],
        ["pi.assistant", "second"],
    ]
    toMatchObject(await input.status(context), {"status": "done"})
    await harness.close(context)

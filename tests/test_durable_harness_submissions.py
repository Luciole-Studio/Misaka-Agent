"""pi durable's ``test/harness-submissions.test.ts``."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from typing import Any

import pytest

from misaka.ai.providers.faux import fauxAssistantMessage
from misaka.chord.context import withAbortSignal
from misaka.durable import (
    AssistantEntry,
    ConversationBusy,
    GenerationTask,
    LiveDoc,
    UserEntry,
    defineEntry,
)
from misaka.durable.storage.sqlite.node import openNodeSqliteStorage
from misaka.utils.js import AbortController
from tests.chat_support import allEntries, chatSetup, openChat, unanswered
from tests.durable_support import ControlledStorage, context
from tests.task_support import deferred


@pytest.fixture
def sqlitePath() -> Iterator[str]:
    directory = tempfile.mkdtemp(prefix="pi-durable-submissions-")
    yield os.path.join(directory, "session.sqlite")
    shutil.rmtree(directory, ignore_errors=True)


async def test_appends_an_idle_write_and_settles_it_done_without_a_turn():
    opened = await openChat(ControlledStorage(), chatSetup())
    harness, root = opened.harness, opened.root
    submission = await root.submit({"type": "write", "entry": {"kind": "note", "data": {"text": "x"}}}, context)
    settled = await submission.wait(context)
    assert isinstance(settled["entry"], int)
    assert settled == {
        "id": submission.id,
        "conversationId": root.id,
        "type": "write",
        "status": "done",
        "entry": settled["entry"],
    }
    entries = await allEntries(root)
    assert entries == [{"id": settled["entry"], "conversationId": root.id, "kind": "note", "data": {"text": "x"}}]
    assert await harness.snapshot(LiveDoc, root.id, context) == {}
    tasks = await harness.commit(lambda tx: tx.scanTasks({"conversationId": root.id}, 10), context)
    assert tasks["items"] == []
    await harness.close(context)


async def test_places_idle_input_and_rejects_busy_input_with_when_busy_reject_without_writing():
    storage = ControlledStorage()
    setup = chatSetup()
    setup.now = lambda: 42
    busy = unanswered()
    setup.faux.setResponses([busy.step])
    opened = await openChat(storage, setup)
    harness, root = opened.harness, opened.root
    submission = await root.submit({"type": "input", "content": "hi"}, context)
    await busy.reached
    record = await submission.status(context)
    assert record["status"] == "placed"
    entry = await root.commit(lambda tx: tx.entry(UserEntry, record["entry"]), context)
    assert entry["model"] == [{"role": "user", "content": "hi", "timestamp": 42}]
    live = await harness.snapshot(LiveDoc, root.id, context)
    assert live["run"]["inputs"] == [submission.id]
    assert (await harness.getTask(live["run"]["taskId"], context))["kind"] == "pi.generation"

    commits = len(storage.commits)
    with pytest.raises(ConversationBusy) as raised:
        await root.submit({"type": "input", "content": "again", "whenBusy": "reject"}, context)
    assert raised.value.conversationId == root.id
    assert len(storage.commits) == commits
    await harness.close(context)


async def test_deduplicates_request_ids_per_conversation_before_any_write():
    storage = ControlledStorage()
    setup = chatSetup()
    busy = unanswered()
    setup.faux.setResponses([busy.step])
    opened = await openChat(storage, setup)
    harness, root = opened.harness, opened.root
    first = await root.submit({"type": "input", "content": "hi", "requestId": "r1"}, context)
    await busy.reached
    commits = len(storage.commits)
    # Deduplication runs before the busy check.
    again = await root.submit({"type": "input", "content": "different", "requestId": "r1"}, context)
    assert again.id == first.id
    assert len(storage.commits) == commits
    with pytest.raises(Exception, match="Request r1 already identifies a submission of type input"):
        await root.submit({"type": "write", "entry": {"kind": "note"}, "requestId": "r1"}, context)
    status = await first.status(context)
    assert status["requestId"] == "r1"
    assert status["status"] == "placed"

    other = await harness.createConversation({"ownership": {"kind": "ownerless"}}, context)
    write = await other.submit({"type": "write", "entry": {"kind": "note"}, "requestId": "r1"}, context)
    assert write.id != first.id
    repeated = await other.submit({"type": "write", "entry": {"kind": "note"}, "requestId": "r1"}, context)
    assert repeated.id == write.id
    await harness.close(context)


async def test_reports_abort_results_and_looks_submissions_up_by_conversation():
    setup = chatSetup()
    release = deferred()

    async def answer(*_: Any) -> Any:
        await release.promise
        return fauxAssistantMessage("answer")

    setup.faux.setResponses([answer])
    opened = await openChat(ControlledStorage(), setup)
    harness, root = opened.harness, opened.root
    submission = await root.submit({"type": "input", "content": "hi"}, context)
    assert await submission.abort(context) == "already_placed"
    assert await harness.abortSubmission(submission.id, context, root.id) == "already_placed"
    other = await harness.createConversation({"ownership": {"kind": "ownerless"}}, context)
    assert await harness.abortSubmission(submission.id, context, other.id) == "not_found"
    assert await harness.abortSubmission(999_999, context) == "not_found"
    assert await harness.submission(999_999, context) is None

    release.resolve()
    await submission.wait(context)
    assert await submission.abort(context) == "settled"
    assert await harness.abortSubmission(submission.id, context) == "settled"
    await harness.close(context)


async def test_cancels_only_a_wait_and_rejects_pending_waits_on_close():
    setup = chatSetup()
    setup.faux.setResponses([unanswered().step])
    opened = await openChat(ControlledStorage(), setup)
    harness, root = opened.harness, opened.root
    submission = await root.submit({"type": "input", "content": "hi"}, context)
    controller = AbortController()
    cancelled = submission.wait(withAbortSignal(controller.signal, context))
    pending = submission.wait(context)
    controller.abort(RuntimeError("stop waiting"))
    with pytest.raises(Exception, match="stop waiting"):
        await cancelled
    assert (await submission.status(context))["status"] == "placed"
    await harness.close(context)
    with pytest.raises(Exception, match="Harness is closed"):
        await pending


async def test_rejects_a_wait_whose_submission_read_spans_the_start_of_close():
    entered = deferred()
    release = deferred()
    hold = False

    class HeldReads(ControlledStorage):
        async def submission(self, id: int, callContext: Any) -> Any:  # type: ignore[override]
            if hold:
                entered.resolve()
                await release.promise
            return await super().submission(id, callContext)

    setup = chatSetup()
    setup.faux.setResponses([unanswered().step])
    opened = await openChat(HeldReads(), setup)
    harness, root = opened.harness, opened.root
    submission = await root.submit({"type": "input", "content": "hi"}, context)
    hold = True
    waiting = submission.wait(context)
    await entered.promise
    closing = harness.close(context)
    release.resolve()
    with pytest.raises(Exception, match="Harness is closed"):
        await waiting
    await closing


async def test_reacquires_a_submission_after_reopen_and_settles_it_durably(sqlitePath):
    setup = chatSetup()
    # The first process never answers; the reopened one does.
    busy = unanswered()
    setup.faux.setResponses([busy.step, fauxAssistantMessage("after reopen")])
    opened = await openChat(await openNodeSqliteStorage(sqlitePath), setup)
    submitted = await opened.root.submit({"type": "input", "content": "hi", "requestId": "print"}, context)
    id = submitted.id
    await busy.reached
    await opened.harness.close(context)

    opened = await openChat(await openNodeSqliteStorage(sqlitePath), setup)
    submission = await opened.harness.submission(id, context)
    assert (await submission.status(context))["status"] == "placed"
    opened.harness.resume()
    settled = await submission.wait(context)
    assert settled["status"] == "done"
    assert settled["type"] == "input"
    await opened.harness.close(context)

    opened = await openChat(await openNodeSqliteStorage(sqlitePath), setup)
    assert await (await opened.harness.submission(id, context)).wait(context) == settled
    again = await opened.root.submit({"type": "input", "content": "hi", "requestId": "print"}, context)
    assert again.id == id
    await opened.harness.close(context)


async def test_enables_scheduling_when_a_caller_submits_or_waits():
    setup = chatSetup()
    setup.faux.setResponses([fauxAssistantMessage("answer")])
    opened = await openChat(ControlledStorage(), setup)
    harness, root = opened.harness, opened.root
    # No resume(): submitting asks for progress.
    assert (await (await root.submit({"type": "input", "content": "hi"}, context)).wait(context))["status"] == "done"
    await harness.close(context)

    passive = await openChat(ControlledStorage(), chatSetup())
    taskId = await passive.root.commit(
        lambda tx: tx.createTask(GenerationTask, {}, {"ownership": {"kind": "conversation"}}), context
    )
    # A committed task alone does not start scheduling; waiting for it does.
    assert (await passive.harness.getTask(taskId, context))["state"]["status"] == "pending"
    assert (await passive.harness.waitForTask(taskId, context))["state"]["status"] == "terminal"
    await passive.harness.close(context)


async def test_settles_submissions_by_their_current_record_in_the_transaction():
    opened = await openChat(ControlledStorage(), chatSetup())
    harness, root = opened.harness, opened.root
    entry = await root.commit(lambda tx: _appendNote(tx, root.id), context)

    def create(record: dict[str, Any]) -> Any:
        return harness.commitWith(lambda tx: _createSubmission(tx, record), context)

    queued = await create({"conversationId": root.id, "type": "input", "status": "queued"})
    write = await create({"conversationId": root.id, "type": "write", "status": "queued"})
    answer = {"status": "done", "answer": entry}
    with pytest.raises(Exception, match="is not a placed input"):
        await root.commit(lambda tx: tx.settleSubmission(queued, answer), context)
    with pytest.raises(Exception, match="is not a placed input"):
        await root.commit(lambda tx: tx.settleSubmission(write, answer), context)
    with pytest.raises(Exception, match="does not exist"):
        await root.commit(lambda tx: tx.settleSubmission(999_999, {"status": "unanswered", "reason": "x"}), context)

    # A submission created earlier in the same commit settles; a second settlement leaves the first.
    async def settleTwice(tx: Any) -> int:
        created = await tx.createSubmission({"conversationId": root.id, "type": "input", "status": "placed", "entry": entry})
        tx.settleSubmission(created["id"], answer)
        tx.settleSubmission(created["id"], {"status": "unanswered", "reason": "late"})
        return created["id"]  # type: ignore[no-any-return]

    placed = await harness.commitWith(settleTwice, context)
    status = await (await harness.submission(placed, context)).status(context)
    assert status["status"] == "done"
    assert status["entry"] == entry
    assert status["answer"] == entry
    await harness.close(context)


async def _appendNote(tx: Any, conversationId: int) -> int:
    return (await tx.appendEntry(conversationId, {"kind": "note"}))["id"]  # type: ignore[no-any-return]


async def _createSubmission(tx: Any, record: dict[str, Any]) -> int:
    return (await tx.createSubmission(record))["id"]  # type: ignore[no-any-return]


async def test_appends_and_reads_typed_entries_through_tokens():
    Counter = defineEntry("app.counter")
    Marker = defineEntry("app.marker")
    opened = await openChat(ControlledStorage(), chatSetup())
    harness, root = opened.harness, opened.root
    counter = await root.commit(lambda tx: tx.appendEntry(Counter, root.id, {"data": {"n": 1}}), context)
    assert counter["data"]["n"] == 1
    assert counter == {"id": counter["id"], "conversationId": root.id, "kind": "app.counter", "data": {"n": 1}}
    marker = await root.commit(lambda tx: tx.appendEntry(Marker, root.id, {}), context)
    assert marker["kind"] == "app.marker"
    assert await root.commit(lambda tx: tx.entry(Counter, counter["id"]), context) == counter
    assert await root.commit(lambda tx: tx.entry(Marker, counter["id"]), context) is None
    assert await root.commit(lambda tx: tx.entry(Counter, 999_999), context) is None
    assert Counter.is_(counter) is True
    assert AssistantEntry.is_(counter) is False
    assert [UserEntry.kind, AssistantEntry.kind] == ["pi.user", "pi.assistant"]
    await harness.close(context)


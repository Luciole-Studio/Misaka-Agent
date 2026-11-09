"""pi durable's ``test/harness-lifecycle.test.ts``."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.models import createModels
from misaka.ai.providers.faux import fauxAssistantMessage, fauxText, fauxToolCall
from misaka.chord.context import withAbortSignal
from misaka.durable import (
    GenerationTask,
    Harness,
    MemoryStorage,
    createRegistry,
    defineDoc,
    defineTask,
    defineTool,
)
from misaka.durable.storage.sqlite.node import openNodeSqliteStorage
from misaka.utils.js import AbortController
from tests.chat_support import chatSetup, openChat
from tests.durable_support import ControlledStorage, context, flush, toMatchObject
from tests.harness_support import addHooks, addTask, addTool
from tests.task_support import (
    completed,
    countingReader,
    deferred,
    eventually,
    openTasks,
    settled,
)

EMPTY_OBJECT = {"type": "object", "properties": {}}


@pytest.fixture
def sqliteDirectory() -> Iterator[str]:
    directory = tempfile.mkdtemp(prefix="pi-durable-lifecycle-")
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


def oneStep(name: str, run: Callable[[], Any] | None = None) -> dict[str, Any]:
    """A one-phase task running ``run`` and completing with null."""

    async def phase(_task: Any, runtime: Any, ctx: Any) -> None:
        if run is not None:
            await run()
        await runtime.commit(lambda tx, current: completed(None), ctx)

    async def abort(_task: Any, runtime: Any, ctx: Any) -> None:
        return None

    return defineTask(
        {
            "name": name,
            "version": 1,
            "initial": lambda input: {"phase": "run"},
            "phases": {"run": phase},
            "abort": abort,
        }
    )


def start(conversation: Any, task: dict[str, Any], background: bool = False) -> Any:
    return conversation.commit(
        lambda tx: tx.createTask(task, None, {"ownership": {"kind": "conversation"}, "background": background}),
        context,
    )


def withSignal(signal: Any) -> Any:
    return withAbortSignal(signal, context)


async def storageOpen(storage: Any) -> bool:
    """Whether a raw Storage read still succeeds, as code of a joined invocation relies on."""
    try:
        await storage.task(1, context)
    except Exception:  # noqa: BLE001 - a closed Storage rejects
        return False
    return True


async def seedRunningTask(storage: Any) -> None:
    """Commit a conversation with a ``running`` task, as a crash leaves it, so open has a reconciliation commit to
    fail."""
    conversationId = await storage.mintId()
    id = await storage.mintId()
    state = {"status": "running", "checkpoint": {"phase": "run"}}
    task = {"id": id, "conversationId": conversationId, "kind": "test.seeded", "version": 1, "input": None, "background": False, "state": state}
    await storage.commit(
        [
            {"type": "conversation", "value": {"id": conversationId}},
            {"type": "task", "value": {**task, "abortRequested": False}},
        ],
        context,
    )


async def test_open_closes_without_a_cancelled_caller_context_and_rethrows_the_original_error_when_open_fails():
    storage = ControlledStorage()
    await seedRunningTask(storage)
    reader = countingReader(createRegistry())
    held = storage.holdCommits()
    storage.failNextCommit(RuntimeError("disk full"))
    controller = AbortController()
    opening = Harness.open(storage, {"models": createModels(), "registry": reader}, withSignal(controller.signal))
    await held.entered
    controller.abort(RuntimeError("caller gave up"))
    held.release()
    with pytest.raises(Exception, match="disk full"):
        await opening
    assert reader.subscriptions() == 0
    assert await storageOpen(storage) is False


async def test_open_rethrows_the_open_error_and_reports_it_once_though_closing_then_fails_too():
    class FailingClose(ControlledStorage):
        async def close(self, closeContext: Any) -> None:
            await super().close(closeContext)
            raise RuntimeError("close failed")

    storage = FailingClose()
    await seedRunningTask(storage)
    storage.failNextCommit(RuntimeError("disk full"))
    reports: list[Any] = []
    opening = Harness.open(
        storage, {"models": createModels(), "registry": createRegistry(), "onReport": reports.append}, context
    )
    with pytest.raises(Exception, match="disk full"):
        await opening
    # The storage failure that failed open is the cause; the close that fails after it adds no report.
    assert [str(error) for error in reports] == ["disk full"]
    assert all(isinstance(error, RuntimeError) for error in reports)


async def test_close_joins_a_task_handler_that_ignores_its_signal_before_closing_storage():
    storage = MemoryStorage()
    reached = deferred()
    gate = deferred()
    readAfterRelease: bool | None = None

    async def stubbornRun() -> None:
        nonlocal readAfterRelease
        reached.resolve()
        await gate.promise
        readAfterRelease = await storageOpen(storage)

    Stubborn = oneStep("test.close-stubborn", stubbornRun)
    harness, _registry, _reports = await openTasks(storage, [Stubborn])
    await start(await harness.root(context), Stubborn)
    harness.resume()
    await reached.promise
    closing = harness.close(context)
    assert await settled(closing) is False
    assert await storageOpen(storage) is True
    gate.resolve()
    await closing
    assert readAfterRelease is True
    assert await storageOpen(storage) is False


@pytest.mark.parametrize("where", ["tool", "hook"])
async def test_close_joins_a_tool_execute_and_a_hook_that_ignore_their_signal_before_closing_storage(where):
    setup = chatSetup()
    storage = MemoryStorage()
    reached = deferred()
    gate = deferred()
    readAfterRelease: bool | None = None

    async def stubborn() -> None:
        nonlocal readAfterRelease
        reached.resolve()
        await gate.promise
        readAfterRelease = await storageOpen(storage)

    async def execute(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        if where == "tool":
            await stubborn()
        return {"output": []}

    addTool(
        setup.registry,
        defineTool({"name": "wait", "description": "wait", "parameters": EMPTY_OBJECT, "execute": execute}),
    )

    async def beforeRequest(*_: Any) -> None:
        if where == "hook":
            await stubborn()

    addHooks(setup.registry, GenerationTask, {"beforeRequest": beforeRequest})
    setup.faux.setResponses(
        [
            fauxAssistantMessage([fauxToolCall("wait", {}, {"id": "c1"})], {"stopReason": "toolUse"}),
            fauxAssistantMessage([fauxText("done")]),
        ]
    )
    opened = await openChat(storage, setup)
    harness, root = opened.harness, opened.root
    await root.submit({"type": "input", "content": "go"}, context)
    await reached.promise
    closing = harness.close(context)
    assert await settled(closing) is False
    assert await storageOpen(storage) is True
    gate.resolve()
    await closing
    assert readAfterRelease is True
    assert await storageOpen(storage) is False


async def test_close_lets_a_new_harness_open_the_same_storage_once_close_resolved_no_old_invocation_code_runs(
    sqliteDirectory,
):
    path = os.path.join(sqliteDirectory, "session.sqlite")
    log: list[str] = []
    gate = deferred()
    generation = 1

    async def stubbornRun() -> None:
        mine = generation
        log.append(f"start {mine}")
        if mine == 1:
            await gate.promise
        log.append(f"end {mine}")

    Stubborn = oneStep("test.generations", stubbornRun)
    first, _registry, _reports = await openTasks(await openNodeSqliteStorage(path), [Stubborn])
    id = await start(await first.root(context), Stubborn)
    first.resume()
    await eventually(lambda: len(log) == 1)

    async def closeAndLog() -> None:
        await first.close(context)
        log.append("closed")

    closing = asyncio.ensure_future(closeAndLog())
    assert await settled(closing) is False
    gate.resolve()
    await closing
    assert log == ["start 1", "end 1", "closed"]
    generation = 2
    second, _registry, _reports = await openTasks(await openNodeSqliteStorage(path), [Stubborn])
    second.resume()
    await second.waitForTask(id, context)
    assert log == ["start 1", "end 1", "closed", "start 2", "end 2"]
    await second.close(context)


async def test_close_keeps_shutting_down_after_a_cancelled_close_and_a_second_close_awaits_the_same_shutdown():
    storage = MemoryStorage()
    reached = deferred()
    gate = deferred()

    async def stubbornRun() -> None:
        reached.resolve()
        await gate.promise

    Stubborn = oneStep("test.close-cancelled", stubbornRun)
    harness, _registry, _reports = await openTasks(storage, [Stubborn])
    await start(await harness.root(context), Stubborn)
    harness.resume()
    await reached.promise
    controller = AbortController()
    cancelled = harness.close(withSignal(controller.signal))
    controller.abort(RuntimeError("stop waiting"))
    with pytest.raises(Exception, match="stop waiting"):
        await cancelled
    # Admission stays sealed and the invocation still holds Storage open.
    with pytest.raises(Exception, match="closed"):
        await harness.commit(lambda tx: None, context)
    assert await storageOpen(storage) is True
    second = harness.close(context)
    assert await settled(second) is False
    gate.resolve()
    await second
    assert await storageOpen(storage) is False


async def test_close_settles_durably_a_commit_whose_committer_was_cancelled_while_it_was_in_storage():
    storage = ControlledStorage()
    harness, _registry, _reports = await openTasks(storage, [])
    root = await harness.root(context)
    held = storage.holdCommits()
    controller = AbortController()

    async def appendNote(tx: Any) -> int:
        return (await tx.appendEntry(root.id, {"kind": "note"}))["id"]  # type: ignore[no-any-return]

    committing = root.commit(appendNote, withSignal(controller.signal))
    await held.entered
    controller.abort(RuntimeError("committer gave up"))
    held.release()
    id = await committing
    page = await root.entries({}, 10, None, context)
    assert [entry["id"] for entry in page["items"]] == [id]
    await harness.close(context)


async def test_close_publishes_no_frame_to_states_and_watches_from_a_commit_that_settles_during_close():
    Notes = defineDoc({"kind": "test.close-frames", "version": 1, "scope": "session", "initial": lambda: {"text": ""}})
    storage = ControlledStorage()
    harness, _registry, _reports = await openTasks(storage, [])
    root = await harness.root(context)

    async def before(tx: Any) -> None:
        (await tx.doc(Notes))["text"] = "before"

    await harness.commit(before, context)
    docState = await harness.documentState(Notes, context)
    docWatch = await harness.watchDoc(Notes, context)
    viewState = await root.viewState(context)
    viewWatch = await root.watch(context)
    graphState = await harness.taskGraph(context)
    graphWatch = await harness.watchTaskGraph(context)
    frames: list[str] = []

    def onUpdate(label: str) -> Callable[[Any, Any, Any], None]:
        def listener(_value: Any, _context: Any, delivery: Any) -> None:
            if delivery["kind"] == "update":
                frames.append(label)

        return listener

    def onFrame(label: str) -> Callable[..., Any]:
        async def listener(*_: Any) -> None:
            frames.append(label)

        return listener

    graphState.subscribe(onUpdate("graphState"))
    graphWatch.start(onFrame("graphWatch"))
    docState.subscribe(onUpdate("docState"))
    viewState.subscribe(onUpdate("viewState"))
    docWatch.start(onFrame("docWatch"))
    viewWatch.start(onFrame("viewWatch"))
    docValue = docState.value
    viewValue = viewState.value

    held = storage.holdCommits()

    async def duringClose(tx: Any) -> None:
        (await tx.doc(Notes))["text"] = "during close"
        await tx.appendEntry(root.id, {"kind": "note"})
        await tx.createTask(
            oneStep("test.close-graph"), None, {"ownership": {"kind": "conversation"}, "conversationId": root.id}
        )

    committing = harness.commit(duringClose, context)
    await held.entered
    closing = harness.close(context)
    held.release()
    await committing
    await closing
    await flush()
    assert frames == []
    assert docState.value is docValue
    assert viewState.value is viewValue
    assert graphState.value == {"tasks": {}}
    assert await graphWatch.closed == {"reason": "session_closed"}
    assert await docWatch.closed == {"reason": "session_closed"}
    assert await viewWatch.closed == {"reason": "session_closed"}
    # The commit itself settled.
    assert any(write["type"] == "entry" for write in storage.commits[-1])


async def test_close_leaves_no_subscription_behind_a_watch_acquisition_cancelled_on_the_line():
    Notes = defineDoc({"kind": "test.cancelled-watch", "version": 1, "scope": "session", "initial": lambda: {"text": ""}})
    storage = ControlledStorage()
    harness, _registry, _reports = await openTasks(storage, [])
    root = await harness.root(context)

    async def setText(tx: Any) -> None:
        (await tx.doc(Notes))["text"] = "x"

    await harness.commit(setText, context)
    # Count commit subscriptions of document observers.
    subscriptions = 0
    subscribe = harness.subscribeCommits

    def counted(listener: Any) -> Callable[[], None]:
        nonlocal subscriptions
        subscriptions += 1
        unsubscribe = subscribe(listener)
        active = True

        def release() -> None:
            nonlocal subscriptions, active
            if active:
                subscriptions -= 1
            active = False
            unsubscribe()

        return release

    harness.subscribeCommits = counted

    # Cancel a document watch while its acquisition loads the document on the line.
    await harness.unloadDocuments()
    find = storage.holdFindDocument()
    controller = AbortController()
    docWatch = harness.watchDoc(Notes, withSignal(controller.signal))
    await find.entered
    controller.abort(RuntimeError("cancelled"))
    find.release()
    with pytest.raises(Exception, match="cancelled"):
        await docWatch
    assert subscriptions == 0

    # Cancel a view watch while it builds its mount on the line.
    await harness.unloadDocuments()
    find = storage.holdFindDocument()
    controller = AbortController()
    viewWatch = root.watch(withSignal(controller.signal))
    await find.entered
    controller.abort(RuntimeError("cancelled"))
    find.release()
    with pytest.raises(Exception, match="cancelled"):
        await viewWatch
    # No observer kept the mount: the next observer builds a new one, and the one after that another.
    first = await root.viewState(context)
    firstValue = first.value
    first.dispose()
    second = await root.viewState(context)
    assert second.value is not firstValue
    second.dispose()
    await harness.close(context)


async def test_close_rejects_conversation_and_harness_operations_once_close_begins_inspect_queued_before_reports_closing():
    storage = ControlledStorage()
    harness, _registry, _reports = await openTasks(storage, [])
    root = await harness.root(context)

    async def note(tx: Any) -> dict[str, Any]:
        return await tx.appendEntry(root.id, {"kind": "note"})  # type: ignore[no-any-return]

    entry = await root.commit(note, context)
    submission = await root.submit({"type": "write", "entry": {"kind": "note"}}, context)

    held = storage.holdCommits()

    async def blocker(tx: Any) -> None:
        await tx.appendEntry(root.id, {"kind": "blocker"})

    blocking = harness.commit(blocker, context)
    await held.entered
    queuedInspect = harness.inspect(context)
    closing = harness.close(context)
    operations: dict[str, Callable[[], Any]] = {
        "submit": lambda: root.submit({"type": "input", "content": "x"}, context),
        "agent": lambda: root.agent(context),
        "configure": lambda: root.configure({"thinkingLevel": "low"}, context),
        "commit": lambda: root.commit(lambda tx: None, context),
        "context": lambda: root.context(context),
        "entries": lambda: root.entries({}, 10, None, context),
        "fork": lambda: root.fork(entry["id"], {"ownership": {"kind": "ownerless"}}, context),
        "compact": lambda: root.compact(None, context),
        "reset": lambda: root.reset(None, context),
        "abort": lambda: root.abort(context),
        "conversationIdle": lambda: root.waitForIdle(context),
        "viewState": lambda: root.viewState(context),
        "watch": lambda: root.watch(context),
        "taskGraph": lambda: harness.taskGraph(context),
        "watchTaskGraph": lambda: harness.watchTaskGraph(context),
        "status": lambda: submission.status(context),
        "wait": lambda: submission.wait(context),
        "abortSubmission": lambda: submission.abort(context),
        "root": lambda: harness.root(context),
        "conversation": lambda: harness.conversation(root.id, context),
        "createConversation": lambda: harness.createConversation({"ownership": {"kind": "ownerless"}}, context),
        "getTask": lambda: harness.getTask(1, context),
        "inspect": lambda: harness.inspect(context),
        "submission": lambda: harness.submission(submission.id, context),
        "abortTask": lambda: harness.abortTask(1, context),
        "waitForTask": lambda: harness.waitForTask(1, context),
        "harnessIdle": lambda: harness.waitForIdle(context),
        "usage": lambda: harness.usage(context),
    }
    outcomes: dict[str, str] = {}
    for name, operation in operations.items():
        try:
            await operation()
            outcomes[name] = "resolved"
        except Exception as error:  # noqa: BLE001 - the rejection is the observation
            outcomes[name] = "closed" if re.search("closed", str(error)) else str(error)
    assert outcomes == {name: "closed" for name in operations}
    with pytest.raises(Exception, match="closed"):
        harness.resume()
    held.release()
    await blocking
    assert (await queuedInspect)["scheduling"] == "closing"
    await closing


async def test_close_completes_reads_queued_at_the_seal_and_rejects_queued_waits_and_acquisitions_that_would_follow_commits():
    Notes = defineDoc({"kind": "test.queued-at-seal", "version": 1, "scope": "session", "initial": lambda: {"text": ""}})
    Absent = defineDoc({"kind": "test.queued-absent", "version": 1, "scope": "session", "initial": lambda: {"text": ""}})
    Pending = oneStep("test.queued-pending")
    storage = ControlledStorage()
    harness, _registry, _reports = await openTasks(storage, [])
    root = await harness.root(context)

    async def touch(tx: Any) -> None:
        await tx.doc(Notes)

    await harness.commit(touch, context)
    taskId = await start(root, Pending)
    write = await root.submit({"type": "write", "entry": {"kind": "note"}}, context)
    await write.wait(context)

    held = storage.holdCommits()

    async def blocker(tx: Any) -> None:
        await tx.appendEntry(root.id, {"kind": "blocker"})

    blocking = harness.commit(blocker, context)
    await held.entered

    async def settle(promise: Any) -> str:
        try:
            value = await promise
        except Exception as error:  # noqa: BLE001 - the rejection is the observation
            return "closed" if re.search("closed", str(error)) else str(error)
        return "undefined" if value is None else "resolved"

    queued = {
        "context": asyncio.ensure_future(settle(root.context(context))),
        "snapshot": asyncio.ensure_future(settle(harness.snapshot(Notes, context))),
        "settledWait": asyncio.ensure_future(settle(write.wait(context))),
        "absentWatch": asyncio.ensure_future(settle(harness.watchDoc(Absent, context))),
        "absentState": asyncio.ensure_future(settle(harness.documentState(Absent, context))),
        "waitForTask": asyncio.ensure_future(settle(harness.waitForTask(taskId, context))),
        "documentState": asyncio.ensure_future(settle(harness.documentState(Notes, context))),
        "watchDoc": asyncio.ensure_future(settle(harness.watchDoc(Notes, context))),
        "viewState": asyncio.ensure_future(settle(root.viewState(context))),
        "watch": asyncio.ensure_future(settle(root.watch(context))),
        "taskGraph": asyncio.ensure_future(settle(harness.taskGraph(context))),
    }
    closing = harness.close(context)
    held.release()
    await blocking
    outcomes = {name: await outcome for name, outcome in queued.items()}
    assert outcomes == {
        "context": "resolved",
        "snapshot": "resolved",
        "settledWait": "resolved",
        "absentWatch": "undefined",
        "absentState": "undefined",
        "waitForTask": "closed",
        "documentState": "closed",
        "watchDoc": "closed",
        "viewState": "closed",
        "watch": "closed",
        "taskGraph": "closed",
    }
    await closing


async def paused() -> SimpleNamespace:
    """Open a paused Harness with one background task pending and a queued write submission."""
    ran = False

    async def mark() -> None:
        nonlocal ran
        ran = True

    Marker = oneStep("test.marker", mark)
    harness, _registry, _reports = await openTasks(MemoryStorage(), [Marker])
    root = await harness.root(context)
    # Background, so idle waits and conversation abort leave it alone.
    id = await start(root, Marker, True)

    async def create(tx: Any) -> int:
        return (await tx.createSubmission({"conversationId": root.id, "type": "write", "status": "queued"}))["id"]  # type: ignore[no-any-return]

    submissionId = await harness.commit(create, context)
    return SimpleNamespace(harness=harness, root=root, id=id, submissionId=submissionId, ran=lambda: ran)


async def test_scheduling_never_schedules_from_a_read_only_viewer():
    opened = await paused()
    harness, root, id, submissionId = opened.harness, opened.root, opened.id, opened.submissionId
    Notes = defineDoc({"kind": "test.viewer-notes", "version": 1, "scope": "session", "initial": lambda: {"text": ""}})

    async def touch(tx: Any) -> None:
        await tx.doc(Notes)

    await harness.commit(touch, context)
    await harness.inspect(context)
    await harness.getTask(id, context)
    await harness.usage(context)
    await harness.conversation(root.id, context)
    await harness.snapshot(Notes, context)
    (await harness.documentState(Notes, context)).dispose()
    await (await harness.watchDoc(Notes, context)).stop()
    submission = await harness.submission(submissionId, context)
    await submission.status(context)
    await root.agent(context)
    await root.context(context)
    await root.entries({}, 10, None, context)
    (await root.viewState(context)).dispose()
    await (await root.watch(context)).stop()
    (await harness.taskGraph(context)).dispose()
    await (await harness.watchTaskGraph(context)).stop()
    await flush()
    await flush()
    assert opened.ran() is False
    assert (await harness.inspect(context))["scheduling"] == "paused"
    await harness.close(context)


async def test_scheduling_schedules_from_every_progress_call():
    async def submissionWait(opened: Any) -> Any:
        return await (await opened.harness.submission(opened.submissionId, context)).wait(context)

    calls: dict[str, Callable[[Any], Any]] = {
        "submit": lambda opened: opened.root.submit({"type": "write", "entry": {"kind": "note"}}, context),
        "compact": lambda opened: opened.root.compact(None, context),
        "abort": lambda opened: opened.root.abort(context),
        "conversationIdle": lambda opened: opened.root.waitForIdle(context),
        "submissionWait": submissionWait,
        "waitForTask": lambda opened: opened.harness.waitForTask(opened.id, context),
        "harnessIdle": lambda opened: opened.harness.waitForIdle(context),
    }

    async def ignoringFailure(promise: Any) -> None:
        try:
            await promise
        except Exception:  # noqa: BLE001, S110 - a queued write's wait settles only on placement; close rejects it
            pass

    for name, call in calls.items():
        opened = await paused()
        pending = asyncio.ensure_future(ignoringFailure(call(opened)))
        try:
            await eventually(lambda opened=opened: opened.ran())  # type: ignore[misc]
        except Exception as error:
            raise AssertionError(f"{name} did not enable scheduling") from error
        await opened.harness.close(context)
        await pending


async def test_registry_changes_run_what_the_registry_holds_at_resume_a_definition_installed_or_replaced_after_open():
    log: list[str] = []

    def define(label: str) -> dict[str, Any]:
        async def run() -> None:
            log.append(label)

        return oneStep("test.late-definition", run)

    storage = MemoryStorage()
    first, registry, _reports = await openTasks(storage, [])
    root = await first.root(context)
    missing = await start(root, define("unused"))
    toMatchObject((await first.inspect(context))["tasks"], [{"state": {"kind": "blocked", "reason": "missing_task"}}])
    installed = addTask(registry, define("v1"))
    toMatchObject((await first.inspect(context))["tasks"], [{"state": {"kind": "ready"}}])
    # The same extension name replaces the definition in place, still before resume.
    installed.dispose()
    addTask(registry, define("v2"))
    first.resume()
    await first.waitForTask(missing, context)
    assert log == ["v2"]
    await first.close(context)

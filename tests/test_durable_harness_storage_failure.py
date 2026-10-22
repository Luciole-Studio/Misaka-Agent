"""pi durable's ``test/harness-storage-failure.test.ts``.

Node's ``process.on("unhandledRejection")`` becomes the event loop's exception handler, which asyncio calls for a future
whose exception nobody retrieved once it is collected; the tests collect garbage before they look.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
from typing import Any

import pytest

from misaka.ai.models import createModels
from misaka.chord.context import withAbortSignal
from misaka.durable import (
    Harness,
    LiveDoc,
    MemoryStorage,
    SessionFailed,
    StorageRequestError,
    createRegistry,
    defineExtension,
    defineTask,
    watchEvents,
)
from misaka.durable.harness.harness import safeNow
from misaka.durable.harness.view import conversationViews
from misaka.durable.session.session import contained
from misaka.utils.js import AbortController
from tests.durable_support import AnyOf, ControlledStorage, context
from tests.task_support import aborted, deferred, openTasks

# Waits for its signal, records that it saw it, and ends `aborted` through its abort handler.
signals: list[str] = []


async def waitingWork(_task: Any, runtime: Any, *_: Any) -> None:
    with contextlib.suppress(BaseException):  # `.catch(() => {})`
        await aborted(runtime.signal)
    signals.append("aborted")


async def endAborted(_task: Any, runtime: Any, abortContext: Any) -> None:
    await runtime.commit(lambda tx, current: {"status": "terminal", "outcome": {"status": "aborted"}}, abortContext)


async def nothing(*_: Any) -> None:
    return None


Waiting = defineTask(
    {
        "name": "test.waiting",
        "version": 1,
        "initial": lambda input: {"phase": "work"},
        "phases": {"work": waitingWork},
        "abort": endAborted,
    }
)


class FailingCalls(ControlledStorage):
    """Fails each Storage method named in ``failing`` once, with ``error``. A scan of entries with a limit above 1 is the
    context read's scan of its range, off the Session line; ``heldRange`` holds it until released."""

    def __init__(self) -> None:
        super().__init__()
        self.error = RuntimeError("disk gone")
        self.failing: set[str] = set()
        self.heldRange: dict[str, Any] | None = None

    def _fail(self, method: str) -> None:
        if method in self.failing:
            self.failing.discard(method)
            raise self.error

    async def task(self, id: int, context: Any) -> Any:  # type: ignore[override]
        self._fail("task")
        return await super().task(id, context)

    async def scanEntries(self, query: Any, limit: int, cursor: Any, context: Any) -> Any:  # type: ignore[override]
        if limit > 1:
            held = self.heldRange
            if held is not None:
                held["entered"].resolve()
                await held["release"]
            self._fail("scanRange")
        return await super().scanEntries(query, limit, cursor, context)

    async def mintId(self) -> Any:  # type: ignore[override]
        self._fail("mintId")
        return await super().mintId()


async def started(storage: Any) -> Any:
    signals.clear()
    harness, _registry, reports = await openTasks(storage, [Waiting])
    root = await harness.root(context)
    return harness, root, reports


async def running(harness: Any, id: int) -> None:
    while True:
        task = await harness.getTask(id, context)
        if task is not None and task["state"]["status"] == "running":
            return
        await asyncio.sleep(0.001)


def appendNote(root: Any) -> Any:
    return lambda tx: tx.appendEntry(root.id, {"kind": "note"})


async def rejectsWith(awaitable: Any, error: BaseException) -> None:
    """``rejects.toBe(error)``."""
    with pytest.raises(BaseException) as raised:
        await awaitable
    assert raised.value is error


async def rejectsFailed(awaitable: Any, cause: Any) -> None:
    """``rejects.toMatchObject({ name: "SessionFailed", cause })``."""
    with pytest.raises(SessionFailed) as raised:
        await awaitable
    assert raised.value.cause is cause


class Unhandled:
    """Errors the event loop's exception handler received: asyncio's unhandled rejections."""

    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.previous = self.loop.get_exception_handler()
        self.errors: list[Any] = []
        self.loop.set_exception_handler(lambda _loop, ctx: self.errors.append(ctx.get("exception", ctx.get("message"))))

    def collected(self) -> list[Any]:
        gc.collect()
        return self.errors

    def stop(self) -> None:
        gc.collect()
        self.loop.set_exception_handler(self.previous)


def described(errors: list[Any]) -> list[str]:
    """``errors.map(String)`` of plain ``Error`` values."""
    return [f"Error: {error}" for error in errors]


class TestAFailedStorageCall:
    async def test_ends_everything_with_the_first_error_the_call_later_calls_waits_watches_streams_running_tasks(self):
        storage = ControlledStorage()
        harness, root, reports = await started(storage)
        id = await root.commit(lambda tx: tx.createTask(Waiting, None, {"ownership": {"kind": "conversation"}}), context)
        harness.resume()
        await running(harness, id)
        task = harness.waitForTask(id, context)
        idle = harness.waitForIdle(context)
        watch = await harness.watchDoc(LiveDoc, root.id, context)
        stream = await watchEvents(harness, root.id, context)
        stream.start(nothing)

        failure = RuntimeError("disk gone")
        storage.failNextCommit(failure)
        # The call that hit it gets the storage error itself.
        await rejectsWith(root.commit(appendNote(root), context), failure)

        await rejectsFailed(task, failure)
        await rejectsFailed(idle, failure)
        assert await watch.closed == {"reason": "session_failed", "error": failure}
        assert await stream.closed == {"reason": "session_failed", "error": failure}
        # Every later call, and a wait that starts now.
        await rejectsFailed(harness.getTask(id, context), failure)
        await rejectsFailed(harness.waitForIdle(context), failure)
        await rejectsFailed(root.submit({"type": "input", "content": "x"}, context), failure)
        assert await harness.closed == {"reason": "failed", "error": failure}
        assert signals == ["aborted"]
        assert reports == [failure]
        await harness.close(context)

    async def test_fails_it_from_a_read_off_the_session_line_as_a_context_read_makes(self):
        storage = FailingCalls()
        harness, root, reports = await started(storage)
        await root.commit(appendNote(root), context)
        storage.failing.add("scanRange")
        await rejectsWith(root.context(context), storage.error)
        assert await harness.closed == {"reason": "failed", "error": storage.error}
        assert reports == [storage.error]
        await harness.close(context)

    async def test_ends_storage_calls_underway_with_session_failed_and_closes_once_they_have_settled(self):
        storage = FailingCalls()
        harness, root, _reports = await started(storage)
        release = deferred()
        await root.commit(appendNote(root), context)
        storage.heldRange = {"entered": deferred(), "release": release.promise}
        reading = root.context(context)
        await storage.heldRange["entered"].promise
        # Another call fails the Session while the context read is underway.
        storage.failing.add("mintId")
        await rejectsWith(root.commit(appendNote(root), context), storage.error)
        closed = {"value": False}
        harness.closed.add_done_callback(lambda _done: closed.__setitem__("value", True))
        for _ in range(5):
            await asyncio.sleep(0)
        assert closed["value"] is False
        # The read succeeds at the backend, yet its caller gets the failure: nothing it read is used.
        release.resolve()
        await rejectsFailed(reading, storage.error)
        assert await harness.closed == {"reason": "failed", "error": storage.error}
        await harness.close(context)

    async def test_closes_the_backend_only_after_a_paged_read_underway_which_cannot_start_another_page(self):
        pages: list[str] = []
        firstPage = deferred()
        releaseFirst = deferred()

        class Paged(ControlledStorage):
            async def scanEntries(self, query: Any, limit: int, cursor: Any, scanContext: Any) -> Any:  # type: ignore[override]
                if limit > 1:
                    pages.append("first" if cursor is None else "next")
                    if cursor is None:
                        firstPage.resolve()
                        await releaseFirst.promise
                return await super().scanEntries(query, limit, cursor, scanContext)

            async def close(self, closeContext: Any) -> None:  # type: ignore[override]
                pages.append("close")
                return await super().close(closeContext)

        storage = Paged()
        harness, root, _reports = await started(storage)

        # More entries than one page of the context read.
        async def many(tx: Any) -> None:
            for _ in range(300):
                await tx.appendEntry(root.id, {"kind": "note"})

        await root.commit(many, context)
        reading = root.context(context)
        await firstPage.promise
        closing = harness.close(context)
        releaseFirst.resolve()
        with pytest.raises(Exception, match="is closed"):
            await reading
        await closing
        assert pages == ["first", "close"]
        assert await harness.closed == {"reason": "closed"}

    async def test_fails_it_when_storage_cannot_close_an_earlier_failure_stays_the_cause(self):
        closeError = RuntimeError("close failed")

        class FailingClose(ControlledStorage):
            async def close(self, closeContext: Any) -> None:  # type: ignore[override]
                await super().close(closeContext)
                raise closeError

        healthy, _root, healthyReports = await started(FailingClose())
        await rejectsWith(healthy.close(context), closeError)
        assert await healthy.closed == {"reason": "failed", "error": closeError}
        assert healthyReports == [closeError]

        storage = FailingClose()
        failed, failedRoot, failedReports = await started(storage)
        failure = RuntimeError("disk gone")
        storage.failNextCommit(failure)
        await rejectsWith(failedRoot.commit(appendNote(failedRoot), context), failure)
        assert await failed.closed == {"reason": "failed", "error": failure}
        assert failedReports == [failure]

    async def test_rejects_the_harness_s_own_calls_with_session_failed_after_a_failure(self):
        storage = ControlledStorage()
        harness, root, _reports = await started(storage)
        failure = RuntimeError("disk gone")
        storage.failNextCommit(failure)
        await rejectsWith(root.commit(appendNote(root), context), failure)
        with pytest.raises(SessionFailed) as raised:
            harness.resume()
        assert raised.value.cause is failure
        await rejectsFailed(harness.root(context), failure)
        await rejectsFailed(harness.conversation(root.id, context), failure)
        await rejectsFailed(harness.createConversation({"ownership": {"kind": "ownerless"}}, context), failure)
        await harness.close(context)

    async def test_ends_what_a_task_holds_with_session_failed_not_as_cancelled_by_its_signal(self):
        ends: list[Any] = []
        watching = deferred()

        async def work(_task: Any, runtime: Any, phaseContext: Any) -> None:
            doc = await runtime.watchDoc(LiveDoc, runtime.conversationId, phaseContext)
            handle = await runtime.conversation(runtime.conversationId, phaseContext)

            async def idleOutcome() -> str:
                try:
                    await handle.waitForIdle(phaseContext)
                except Exception as error:  # noqa: BLE001 - the name is the outcome
                    return type(error).__name__
                return "resolved"

            idle = asyncio.ensure_future(idleOutcome())
            watching.resolve()
            ends.append(await doc.closed)
            ends.append(await idle)

        Watcher = defineTask(
            {
                "name": "test.watcher",
                "version": 1,
                "initial": lambda input: {"phase": "work"},
                "phases": {"work": work},
                "abort": nothing,
            }
        )
        storage = ControlledStorage()
        harness, _registry, _reports = await openTasks(storage, [Watcher])
        root = await harness.root(context)
        await root.commit(lambda tx: tx.createTask(Watcher, None, {"ownership": {"kind": "conversation"}}), context)
        harness.resume()
        await watching.promise
        failure = RuntimeError("disk gone")
        storage.failNextCommit(failure)
        await rejectsWith(root.commit(appendNote(root), context), failure)
        await harness.closed
        assert ends == [{"reason": "session_failed", "error": failure}, "SessionFailed"]
        await harness.close(context)

    async def test_fails_it_from_a_failed_id_mint(self):
        storage = FailingCalls()
        harness, root, _reports = await started(storage)
        storage.failing.add("mintId")
        await rejectsWith(root.commit(appendNote(root), context), storage.error)
        assert await harness.closed == {"reason": "failed", "error": storage.error}
        await harness.close(context)

    async def test_does_not_commit_what_a_callback_did_after_it_caught_a_failed_read(self):
        storage = FailingCalls()
        harness, root, _reports = await started(storage)
        written = len(storage.admittedCommits)
        storage.failing.add("task")

        async def change(tx: Any) -> None:
            with contextlib.suppress(Exception):  # `.catch(() => {})`
                await tx.task(1)
            await tx.appendEntry(root.id, {"kind": "note"})

        await rejectsFailed(root.commit(change, context), storage.error)
        assert len(storage.admittedCommits) == written
        await harness.close(context)

    async def test_fails_it_for_a_storage_request_error_from_a_commit_whose_effect_is_unknown(self):
        storage = ControlledStorage()
        harness, root, _reports = await started(storage)
        rejection = StorageRequestError("bad batch")
        storage.failNextCommit(rejection)
        await rejectsWith(root.commit(appendNote(root), context), rejection)
        assert await harness.closed == {"reason": "failed", "error": rejection}
        await harness.close(context)

    async def test_fails_only_the_call_for_an_invalid_request(self):
        harness, root, reports = await started(MemoryStorage())
        entry = await root.commit(appendNote(root), context)

        async def reversedCursor() -> Any:
            page = await root.entries({"order": "ascending"}, 1, None, context)
            cursor = page.get("next") or {"after": 1, "order": "ascending"}
            return await root.entries({"order": "descending"}, 1, cursor, context)

        requests = [
            lambda: root.entries({}, 10, {"after": "x"}, context),
            reversedCursor,
            lambda: root.commit(lambda tx: tx.scanEntries({"conversationId": 999_999}, 10), context),
            # `pi.live` keeps no history.
            lambda: harness.snapshotAsOf(LiveDoc, root.id, entry["id"], context),
        ]
        for request in requests:
            with pytest.raises(StorageRequestError):
                await request()
        assert await root.commit(appendNote(root), context) is not None
        ended = {"value": False}
        harness.closed.add_done_callback(lambda _done: ended.__setitem__("value", True))
        await asyncio.sleep(0)
        assert ended["value"] is False
        assert reports == []
        await harness.close(context)

    async def test_fails_it_for_a_throw_in_its_own_commit_listeners_which_would_leave_memory_behind_storage(self):
        storage = ControlledStorage()
        harness, root, reports = await started(storage)
        bug = RuntimeError("listener bug")

        def listener(*_: Any) -> None:
            raise bug

        harness.observeCommits(listener)
        # The commit itself is durable and resolves; the Session fails behind it.
        assert await root.commit(appendNote(root), context) is not None
        assert await harness.closed == {"reason": "failed", "error": bug}
        assert reports == [bug]
        await harness.close(context)

    async def test_fails_it_for_a_storage_error_during_a_host_close_before_the_backend_closes(self):
        storage = ControlledStorage()
        harness, root, reports = await started(storage)
        held = storage.holdCommits()
        failure = RuntimeError("disk gone")
        storage.failNextCommit(failure)
        committing = root.commit(appendNote(root), context)
        await held.entered
        closing = harness.close(context)
        held.release()
        await rejectsWith(committing, failure)
        await closing
        assert await harness.closed == {"reason": "failed", "error": failure}
        assert reports == [failure]

    async def test_ends_an_abort_that_joins_a_run_ignoring_its_signal_and_a_reentrant_report_finds_the_session_failed(
        self,
    ):
        release = deferred()
        entered = deferred()

        async def work(*_: Any) -> None:
            entered.resolve()
            await release.promise

        Stubborn = defineTask(
            {
                "name": "test.stubborn",
                "version": 1,
                "initial": lambda input: {"phase": "work"},
                "phases": {"work": work},
                "abort": nothing,
            }
        )
        storage = ControlledStorage()
        registry = createRegistry()
        registry.install(defineExtension({"name": "tasks", "tasks": [Stubborn]}))
        holder: dict[str, Any] = {}

        def onReport(_error: Any) -> None:
            if "reentered" not in holder:
                holder["reentered"] = holder["harness"].waitForIdle(context)

        harness = await Harness.open(storage, {"models": createModels(), "registry": registry, "onReport": onReport}, context)
        holder["harness"] = harness
        root = await harness.root(context)
        id = await root.commit(lambda tx: tx.createTask(Stubborn, None, {"ownership": {"kind": "conversation"}}), context)
        harness.resume()
        await entered.promise
        held = storage.holdCommits()
        aborting = harness.abortTask(id, context)
        await held.entered
        held.release()
        # The abort mark commits and the abort joins the run, which ignores its signal; then the Session fails.
        failure = RuntimeError("disk gone")
        await asyncio.sleep(0.005)
        storage.failNextCommit(failure)
        await rejectsWith(root.commit(appendNote(root), context), failure)
        await rejectsFailed(aborting, failure)
        await rejectsFailed(holder["reentered"], failure)
        release.resolve()
        await harness.close(context)

    async def test_does_not_fail_it_for_a_read_its_caller_cancelled(self):
        controller = AbortController()
        cancelled = RuntimeError("caller gave up")

        class CancelledRead(MemoryStorage):
            async def scanEntries(self, query: Any, limit: int, cursor: Any, scanContext: Any) -> Any:  # type: ignore[override]
                if scanContext.abortSignal is controller.signal:
                    controller.abort(cancelled)
                    raise cancelled
                return await super().scanEntries(query, limit, cursor, scanContext)

        harness, root, _reports = await started(CancelledRead())
        cancelling = withAbortSignal(controller.signal, context)
        await rejectsWith(root.context(cancelling), cancelled)
        assert await root.commit(appendNote(root), context) is not None
        await harness.close(context)


class TestListenerAndReportErrors:
    async def test_reports_a_listener_or_on_report_whose_promise_rejects_without_an_unhandled_rejection(self):
        unhandled = Unhandled()
        try:
            harness, root, reports = await started(MemoryStorage())

            async def asyncThrow(*_: Any) -> None:
                raise RuntimeError("async listener")

            harness.subscribeCommits(asyncThrow)
            harness.subscribeClose(asyncThrow)
            await root.commit(appendNote(root), context)
            await harness.close(context)
            await asyncio.sleep(0.01)
            assert described(reports) == ["Error: async listener", "Error: async listener"]

            registry = createRegistry()
            rejecting = await Harness.open(
                MemoryStorage(), {"models": createModels(), "registry": registry, "onReport": asyncThrow}, context
            )

            def syncThrow(*_: Any) -> None:
                raise RuntimeError("sync listener")

            rejecting.subscribeCommits(syncThrow)
            again = await rejecting.root(context)
            await again.commit(appendNote(again), context)
            await rejecting.close(context)
            await asyncio.sleep(0.01)
            assert unhandled.collected() == []
        finally:
            unhandled.stop()

    async def test_reports_a_throwing_commit_or_close_listener_and_runs_the_others_the_commit_stands(self):
        storage = MemoryStorage()
        harness, root, reports = await started(storage)
        seen: list[str] = []

        def commitThrow(*_: Any) -> None:
            raise RuntimeError("commit listener")

        def closeThrow(*_: Any) -> None:
            raise RuntimeError("close listener")

        harness.subscribeCommits(commitThrow)
        harness.subscribeCommits(lambda *_: seen.append("commit"))
        harness.subscribeClose(closeThrow)
        harness.subscribeClose(lambda *_: seen.append("close"))
        assert await root.commit(appendNote(root), context) is not None
        await harness.close(context)
        assert seen == ["commit", "close"]
        assert described(reports) == ["Error: commit listener", "Error: close listener"]

    async def test_keeps_feeding_the_other_observers_of_a_view_when_one_throws(self):
        harness, root, reports = await started(MemoryStorage())
        views = conversationViews(harness)
        failure = RuntimeError("observer broke")

        class Observer:
            def publication(self, *_: Any) -> None:
                raise failure

            def closeSession(self, *_: Any) -> None:
                return None

        async def create(*_: Any) -> Observer:
            return Observer()

        await views.attach(root.id, create, context)
        watch = await root.watch(context)
        seen: list[Any] = []

        async def onValue(value: Any, *_: Any) -> None:
            seen.append(value)

        watch.start(onValue)
        await root.commit(appendNote(root), context)
        while len(seen) == 0:
            await asyncio.sleep(0.001)
        assert reports == [failure]
        await harness.close(context)
        assert await watch.closed == {"reason": "session_closed"}

    async def test_contains_a_fulfilled_thenable_that_calls_only_its_fulfillment_handler(self):
        unhandled = Unhandled()
        try:
            errors: list[Any] = []

            # MISAKA fork: upstream's thenable, like some libraries' promises, calls only the handler it settles with. A
            # Python awaitable has no handlers; the counterpart is an awaitable of no asyncio type that completes.
            class Thenable:
                def __await__(self) -> Any:
                    yield
                    return None

            contained(lambda: Thenable(), errors.append)
            await asyncio.sleep(0.01)
            assert errors == []
            assert unhandled.collected() == []
        finally:
            unhandled.stop()

    async def test_contains_a_rejected_promise_from_another_realm(self):
        unhandled = Unhandled()
        try:

            # MISAKA fork: Python has no realms; the counterpart of a `Promise` of another realm, which is no instance of
            # this realm's `Promise`, is an awaitable of no asyncio type that fails.
            class Foreign:
                def __await__(self) -> Any:
                    raise RuntimeError("foreign")
                    yield

            errors: list[Any] = []
            contained(lambda: Foreign(), errors.append)
            await asyncio.sleep(0.01)
            assert described(errors) == ["Error: foreign"]
            assert unhandled.collected() == []
        finally:
            unhandled.stop()

    async def test_reports_a_throwing_watch_listener_which_ends_that_watch(self):
        harness, root, reports = await started(MemoryStorage())
        watch = await harness.watchDoc(LiveDoc, root.id, context)
        failure = RuntimeError("listener broke")

        async def listener(*_: Any) -> None:
            raise failure

        watch.start(listener)

        async def change(tx: Any) -> None:
            (await tx.doc(LiveDoc, root.id))["compactions"] = []

        await root.commit(change, context)
        assert await watch.closed == {"reason": "listener_error", "error": failure}
        assert reports == [failure]
        await harness.close(context)

    async def test_faults_only_the_task_and_ends_only_the_watch_for_a_thrown_value_that_cannot_be_formatted(self):
        # MISAKA fork: JavaScript throws `Object.create(null)`, which `String()` cannot format. Python raises only
        # exceptions; the counterpart is one whose `str()` throws, formatted as `Object.prototype.toString` would name it.
        class Unformattable(Exception):
            def __str__(self) -> str:
                raise TypeError("Cannot convert object to primitive value")

            def __repr__(self) -> str:
                raise TypeError("Cannot convert object to primitive value")

        unformattable = Unformattable()

        async def work(*_: Any) -> None:
            raise unformattable

        Throws = defineTask(
            {
                "name": "test.throws-unformattable",
                "version": 1,
                "initial": lambda input: {"phase": "work"},
                "phases": {"work": work},
                "abort": nothing,
            }
        )
        harness, _registry, _reports = await openTasks(MemoryStorage(), [Throws])
        root = await harness.root(context)
        watch = await harness.watchDoc(LiveDoc, root.id, context)

        async def listener(*_: Any) -> None:
            raise unformattable

        watch.start(listener)
        id = await root.commit(lambda tx: tx.createTask(Throws, None, {"ownership": {"kind": "conversation"}}), context)
        harness.resume()
        assert (await harness.waitForTask(id, context))["state"]["outcome"] == {
            "status": "faulted",
            "error": {"message": "[object Unformattable]"},
        }

        async def change(tx: Any) -> None:
            (await tx.doc(LiveDoc, root.id))["compactions"] = []

        await root.commit(change, context)
        assert (await watch.closed)["reason"] == "listener_error"
        assert await root.commit(appendNote(root), context) is not None
        await harness.close(context)

    def test_reports_a_throwing_clock_once_also_when_the_report_handler_reads_the_clock_again(self):
        reports: list[Any] = []

        def broken() -> int:
            raise RuntimeError("clock broke")

        def report(error: Any) -> None:
            reports.append(error)
            # Reentry, as a handler that commits does: the clock is read again before this report returns.
            if len(reports) < 5:
                clock()

        clock = safeNow(broken, report)
        assert clock() == AnyOf(int, float)
        clock()
        assert described(reports) == ["Error: clock broke"]

    async def test_keeps_working_when_on_report_throws_and_falls_back_to_date_now_when_the_clock_throws(self):
        registry = createRegistry()
        registry.install(defineExtension({"name": "tasks", "tasks": [Waiting]}))

        def brokenClock() -> int:
            raise RuntimeError("clock broke")

        def brokenReport(_error: Any) -> None:
            raise RuntimeError("report broke")

        harness = await Harness.open(
            MemoryStorage(),
            {"models": createModels(), "registry": registry, "now": brokenClock, "onReport": brokenReport},
            context,
        )
        root = await harness.root(context)
        id = await root.commit(lambda tx: tx.createTask(Waiting, None, {"ownership": {"kind": "conversation"}}), context)
        harness.resume()
        await running(harness, id)
        assert (await harness.getTask(id, context))["startedAt"] == AnyOf(int, float)
        await harness.abortTask(id, context)
        assert (await harness.waitForTask(id, context))["state"]["outcome"]["status"] == "aborted"
        await harness.close(context)

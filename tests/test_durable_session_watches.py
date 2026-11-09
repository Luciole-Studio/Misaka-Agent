"""pi durable's ``test/session-watches.test.ts``."""

from __future__ import annotations

import re
from typing import Any

import pytest

from misaka.chord.context import (
    BACKGROUND_CONTEXT,
    createContextKey,
    withAbortSignal,
    withCancel,
    withContextValue,
)
from misaka.chord.delta import applyImmutable
from misaka.durable.documents import defineDoc
from misaka.utils.js import AbortController
from tests.durable_support import (
    Deferred,
    context,
    documentChanges,
    flush,
    openTestSession,
)

StateDoc = defineDoc(
    {
        "kind": "watch.state",
        "version": 1,
        "scope": "session",
        "initial": lambda: {"value": 0, "items": ["a", "b"], "retained": {"label": "stable"}},
    }
)


def throws(message: str):
    return pytest.raises(Exception, match=re.escape(message))


async def createState():
    harness = openTestSession()

    async def create(tx):
        await tx.doc(StateDoc)

    await harness.session.commit(create, context)
    return harness


async def setValue(session, value, commitContext=context):
    async def change(tx):
        (await tx.doc(StateDoc))["value"] = value

    await session.commit(change, commitContext)


class TestSessionDocumentWatches:
    async def test_never_creates_an_absent_document(self):
        ts = openTestSession()
        session, storage = ts.session, ts.storage
        commits = len(storage.commits)
        assert await session.watchDoc(StateDoc, context) is None
        assert len(storage.commits) == commits
        assert storage.mintCount == 0

    async def test_keeps_the_acquisition_revision_until_start_and_delivers_exact_committed_frames(self):
        ts = await createState()
        session, publications = ts.session, ts.publications
        watch = await session.watchDoc(StateDoc, context)
        initial = watch.value
        await setValue(session, 1)
        await flush()
        firstPublished = documentChanges(publications[-1])[0]
        await setValue(session, 2)
        await flush()
        secondPublished = documentChanges(publications[-1])[0]
        assert watch.value is initial

        state = {"inline": True}
        deliveries: list[dict[str, Any]] = []

        async def listener(value, ops, _context):
            assert state["inline"] is False
            assert watch.value is value
            deliveries.append({"value": value, "ops": ops})

        watch.start(listener)
        state["inline"] = False
        assert len(deliveries) == 0
        await flush()
        assert [d["value"]["value"] for d in deliveries] == [1, 2]
        assert deliveries[0]["value"] is firstPublished["value"]
        assert deliveries[0]["ops"] is firstPublished["ops"]
        assert deliveries[1]["value"] is secondPublished["value"]
        assert deliveries[1]["ops"] is secondPublished["ops"]
        assert initial["value"] == 0
        await watch.stop()

    async def test_serializes_callbacks_and_buffers_exact_frames_committed_while_one_is_in_flight(self):
        session = (await createState()).session
        watch = await session.watchDoc(StateDoc, context)
        entered = Deferred()
        release = Deferred()
        values: list[int] = []
        counters = {"active": 0, "maxActive": 0}

        async def listener(value, _ops, _context):
            counters["active"] += 1
            counters["maxActive"] = max(counters["maxActive"], counters["active"])
            values.append(-1 if value is None else value["value"])
            if len(values) == 1:
                entered.resolve()
                await release.promise
            counters["active"] -= 1

        watch.start(listener)
        await setValue(session, 1)
        await entered.promise
        for value in range(2, 21):
            await setValue(session, value)
        assert values == [1]
        release.resolve()
        await flush()
        assert counters["maxActive"] == 1
        assert values == list(range(1, 21))
        await watch.stop()

    async def test_allows_a_listener_to_initiate_a_later_session_commit(self):
        session = (await createState()).session
        watch = await session.watchDoc(StateDoc, context)
        completed = Deferred()
        values: list[int] = []

        async def listener(value, _ops, _context):
            current = -1 if value is None else value["value"]
            values.append(current)
            if current == 1:
                await setValue(session, 2)
            else:
                completed.resolve()

        watch.start(listener)
        await setValue(session, 1)
        await completed.promise
        assert values == [1, 2]
        await watch.stop()

    async def test_collapses_101_pending_commits_to_one_root_replacement(self):
        session = (await createState()).session
        watch = await session.watchDoc(StateDoc, context)
        for value in range(1, 102):
            await setValue(session, value)
        deliveries: list[dict[str, Any]] = []

        async def listener(value, ops, _context):
            deliveries.append({"value": -1 if value is None else value["value"], "ops": ops})

        watch.start(listener)
        await flush()
        assert deliveries == [{"value": 101, "ops": [["r", watch.value]]}]
        await watch.stop()

    async def test_never_folds_the_in_flight_frame_into_an_overflow_reset(self):
        session = (await createState()).session
        watch = await session.watchDoc(StateDoc, context)
        entered = Deferred()
        release = Deferred()
        deliveries: list[dict[str, Any]] = []

        async def listener(value, ops, _context):
            deliveries.append({"value": -1 if value is None else value["value"], "ops": ops})
            if len(deliveries) == 1:
                entered.resolve()
                await release.promise

        watch.start(listener)
        await setValue(session, 1)
        await entered.promise
        for value in range(2, 103):
            await setValue(session, value)
        release.resolve()
        await flush()
        assert deliveries[0]["value"] == 1
        assert deliveries[1] == {"value": 102, "ops": [["r", watch.value]]}
        assert len(deliveries) == 2
        await watch.stop()

    async def test_folds_retirement_into_an_overflow_reset_and_then_closes(self):
        session = (await createState()).session
        watch = await session.watchDoc(StateDoc, context)
        for value in range(1, 101):
            await setValue(session, value)
        await session.commit(lambda tx: tx.retireDoc(StateDoc), context)
        deliveries: list[dict[str, Any]] = []

        async def listener(value, ops, _context):
            deliveries.append({"value": value, "ops": ops})

        watch.start(listener)
        assert await watch.closed == {"reason": "retired"}
        assert deliveries == [{"value": None, "ops": [["r", None]]}]

    async def test_delivers_replayable_structural_no_op_commits_instead_of_suppressing_them(self):
        session = (await createState()).session
        watch = await session.watchDoc(StateDoc, context)
        initial = watch.value

        async def change(tx):
            state = await tx.doc(StateDoc)
            first = state["items"].shift()
            state["items"].unshift(first)

        await session.commit(change, context)
        newest = await session.snapshot(StateDoc, context)
        assert newest is not initial
        assert newest == initial
        batches: list[Any] = []

        async def listener(value, ops, _context):
            assert value is newest
            batches.append(ops)

        watch.start(listener)
        await flush()
        assert len(batches) == 1
        assert len(batches[0]) > 0
        await watch.stop()

    async def test_preserves_commit_context_values_without_inheriting_producer_cancellation(self):
        session = (await createState()).session
        watch = await session.watchDoc(StateDoc, context)
        key = createContextKey("watch-test")
        parentController = AbortController()
        commitContext = withContextValue(key, "newest-commit", withAbortSignal(parentController.signal, BACKGROUND_CONTEXT))
        entered = Deferred()
        release = Deferred()
        delivery: list[Any] = []

        async def listener(_value, _ops, delivered):
            delivery.append(delivered)
            entered.resolve()
            await release.promise

        watch.start(listener)
        await setValue(session, 1, commitContext)
        await entered.promise
        assert delivery[0].value(key) == "newest-commit"
        assert delivery[0].abortSignal is None
        parentController.abort("caller finished")
        stopped = watch.stop()
        assert await stopped == {"reason": "stopped"}
        assert delivery[0].abortSignal is None
        release.resolve()

    async def test_keeps_earlier_immutable_revisions_stable(self):
        session = (await createState()).session
        watch = await session.watchDoc(StateDoc, context)
        initial = watch.value
        delivered: list[Any] = []

        async def listener(value, _ops, _context):
            delivered.append(value)

        watch.start(listener)
        await setValue(session, 1)
        await flush()
        assert delivered[-1] is watch.value
        assert delivered[-1] is not initial
        assert delivered[-1]["retained"] is initial["retained"]
        assert initial["value"] == 0
        await watch.stop()

    async def test_delivers_retirement_and_does_not_follow_recreation(self):
        session = (await createState()).session
        oldWatch = await session.watchDoc(StateDoc, context)
        values: list[Any] = []

        async def listener(value, _ops, _context):
            values.append(value)

        oldWatch.start(listener)

        async def replace(tx):
            await tx.retireDoc(StateDoc)
            (await tx.doc(StateDoc))["value"] = 10

        await session.commit(replace, context)
        assert await oldWatch.closed == {"reason": "retired"}
        assert values == [None]
        assert oldWatch.value is None

        replacement = await session.watchDoc(StateDoc, context)
        assert replacement.value["value"] == 10
        await setValue(session, 11)
        await flush()
        assert oldWatch.value is None
        await replacement.stop()

    async def test_session_close_discards_retirement_buffered_before_start(self):
        session = (await createState()).session
        watch = await session.watchDoc(StateDoc, context)
        baseline = watch.value
        await session.commit(lambda tx: tx.retireDoc(StateDoc), context)
        await session.close(context)
        assert await watch.closed == {"reason": "session_closed"}
        assert watch.value is baseline

        async def noop(*_args):
            pass

        with throws("stopped"):
            watch.start(noop)

    async def test_session_close_discards_retirement_behind_an_in_flight_callback(self):
        session = (await createState()).session
        watch = await session.watchDoc(StateDoc, context)
        entered = Deferred()
        release = Deferred()
        values: list[Any] = []

        async def listener(value, _ops, _context):
            values.append(None if value is None else value["value"])
            entered.resolve()
            await release.promise

        watch.start(listener)
        await setValue(session, 1)
        await entered.promise
        await session.commit(lambda tx: tx.retireDoc(StateDoc), context)
        await session.close(context)
        assert await watch.closed == {"reason": "session_closed"}
        release.resolve()
        await flush()
        assert values == [1]

    async def test_supports_idempotent_stop_and_rejects_repeated_or_late_start(self):
        session = (await createState()).session

        async def noop(*_args):
            pass

        started = await session.watchDoc(StateDoc, context)
        started.start(noop)
        with throws("already started"):
            started.start(noop)
        first = started.stop()
        second = started.stop()
        assert second is first
        assert await first == {"reason": "stopped"}

        stopped = await session.watchDoc(StateDoc, context)
        await stopped.stop()
        with throws("stopped"):
            stopped.start(noop)

    async def test_cancels_acquisition_without_leaking_a_registered_watch(self):
        ts = await createState()
        session, storage = ts.session, ts.storage
        await session.unloadDocuments()
        gate = storage.holdFindDocument()
        child = withCancel(context)
        acquisition = session.watchDoc(StateDoc, child.context)
        await gate.entered
        child.cancel(RuntimeError("cancel acquisition"))
        gate.release()
        with throws("cancel acquisition"):
            await acquisition
        await session.close(context)

    async def test_cancels_future_delivery_without_aborting_an_in_flight_callback(self):
        session = (await createState()).session
        child = withCancel(context)
        watch = await session.watchDoc(StateDoc, child.context)
        entered = Deferred()
        release = Deferred()
        delivery: list[Any] = []

        async def listener(_value, _ops, delivered):
            delivery.append(delivered)
            entered.resolve()
            await release.promise

        watch.start(listener)
        await setValue(session, 1)
        await entered.promise
        child.cancel()
        assert await watch.closed == {"reason": "cancelled"}
        assert delivery[0].abortSignal is None
        release.resolve()

    async def test_session_close_stops_future_delivery_without_joining_an_in_flight_callback(self):
        session = (await createState()).session
        watch = await session.watchDoc(StateDoc, context)
        entered = Deferred()
        release = Deferred()
        delivery: list[Any] = []

        async def listener(_value, _ops, delivered):
            delivery.append(delivered)
            entered.resolve()
            await release.promise

        watch.start(listener)
        await setValue(session, 1)
        await entered.promise
        await session.close(context)
        assert delivery[0].abortSignal is None
        assert await watch.closed == {"reason": "session_closed"}
        release.resolve()

    async def test_settles_listener_failure_on_only_the_affected_watch(self):
        session = (await createState()).session
        failed = await session.watchDoc(StateDoc, context)
        healthy = await session.watchDoc(StateDoc, context)

        async def failing(*_args):
            raise RuntimeError("listener failed")

        calls = [0]

        async def counting(*_args):
            calls[0] += 1

        failed.start(failing)
        healthy.start(counting)
        await setValue(session, 1)
        end = await failed.closed
        assert end["reason"] == "listener_error"
        assert str(end["error"]) == "listener failed"
        await flush()
        assert calls[0] == 1
        await healthy.stop()

    async def test_hydrates_migration_without_writing_and_observes_the_later_exact_edit(self):
        Old = defineDoc({"kind": "watch.migration", "version": 1, "scope": "session", "initial": lambda: {"value": 3}})
        Current = defineDoc(
            {
                "kind": "watch.migration",
                "version": 2,
                "scope": "session",
                "initial": lambda: {"value": 0, "migrated": False},
                "migrate": lambda value, _from: {"value": value["value"], "migrated": True},
            }
        )
        ts = openTestSession()
        session, storage = ts.session, ts.storage

        async def touchOld(tx):
            await tx.doc(Old)

        async def touchCurrent(tx):
            await tx.doc(Current)

        await session.commit(touchOld, context)
        await session.unloadDocuments()
        commits = len(storage.commits)
        watch = await session.watchDoc(Current, context)
        assert watch.value == {"value": 3, "migrated": True}
        assert len(storage.commits) == commits
        values: list[int] = []

        async def listener(value, _ops, _context):
            values.append(-1 if value is None else value["value"])

        watch.start(listener)
        await session.commit(touchCurrent, context)
        await flush()
        assert values == []

        async def edit(tx):
            (await tx.doc(Current))["value"] = 4

        await session.commit(edit, context)
        await flush()
        assert values == [4]
        await watch.stop()

    async def test_can_replay_every_delivered_exact_operation_batch_from_the_acquisition_revision(self):
        session = (await createState()).session
        watch = await session.watchDoc(StateDoc, context)
        replica = {"value": watch.value}

        async def listener(value, ops, _context):
            replica["value"] = applyImmutable(replica["value"], ops)
            assert replica["value"] == value

        watch.start(listener)

        async def change(tx):
            state = await tx.doc(StateDoc)
            state["value"] = 3
            state["items"].splice(0, 1)

        await session.commit(change, context)
        await flush()
        assert replica["value"] == watch.value
        await watch.stop()

    async def test_continues_after_the_tracker_cache_unloads(self):
        ts = await createState()
        session, storage = ts.session, ts.storage
        watch = await session.watchDoc(StateDoc, context)
        baseline = watch.value
        reads = storage.documentReadCount
        await session.unloadDocuments()
        reloaded = await session.snapshot(StateDoc, context)
        assert reloaded == baseline
        assert reloaded is not baseline
        assert storage.documentReadCount > reads

        async def noop(*_args):
            pass

        watch.start(noop)
        await setValue(session, 7)
        await flush()
        assert watch.value["value"] == 7
        await watch.stop()

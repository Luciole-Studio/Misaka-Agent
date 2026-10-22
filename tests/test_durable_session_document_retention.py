"""pi durable's ``test/session-document-retention.test.ts``.

The Session's document cache is bounded by use: unattached documents leave it after the retention period and load from
Storage again; documents with a watch or a document state never leave it.
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

import misaka.durable.session.session as sessionModule
from misaka.ai.providers.faux import fauxAssistantMessage, fauxText, fauxToolCall
from misaka.durable import MemoryStorage, defineTool, watchEvents
from misaka.durable.documents import defineDoc, defineDocFamily
from misaka.durable.session.session import SessionImpl, setDefaultDocumentRetention
from misaka.utils.js import jsNumber, jsonStringify
from tests.chat_support import chatSetup, openChat
from tests.durable_support import (
    ControlledStorage,
    context,
    createConversation,
    toMatchObject,
)
from tests.harness_support import addTool
from tests.task_support import deferred, eventually

CounterDoc = defineDoc(
    {
        "kind": "test.retention.counter",
        "version": 1,
        "scope": "conversation",
        "history": "latest",
        "fork": "initial",
        "initial": lambda: {"count": 0},
    }
)
RewindableDoc = defineDoc(
    {
        "kind": "test.retention.rewindable",
        "version": 1,
        "scope": "conversation",
        "history": "rewindable",
        "fork": "asOf",
        "initial": lambda: {"count": 0},
    }
)
# A third-party conversation family: one member per event.
EventDoc = defineDocFamily(
    {
        "kind": "test.retention.event",
        "version": 1,
        "scope": "conversation",
        "history": "latest",
        "fork": "initial",
        "family": True,
        "initial": lambda seq: {"seq": seq},
    }
)
SessionCounterDoc = defineDoc(
    {"kind": "test.retention.session-counter", "version": 1, "scope": "session", "initial": lambda: {"count": 0}}
)


def _failingCheckpoint(*_args: Any) -> bool:
    raise RuntimeError("checkpoint failed")


# A session document whose checkpoint predicate throws, so the commit that changes it fails while it settles.
Failing = defineDoc(
    {
        "kind": "test.retention.failing",
        "version": 1,
        "scope": "session",
        "initial": lambda: {"count": 0},
        "checkpointWhen": _failingCheckpoint,
    }
)

# The default retention of this run, as the setup (``tests/durable_support.py``) set it.
_forced = os.environ.get("MISAKA_DURABLE_DOCUMENT_RETENTION_MS")
defaultForRun = 600_000 if _forced is None else jsNumber(_forced)

# A conversation document any Harness test can read.
TestDoc = defineDoc(
    {
        "kind": "test.retention.harness",
        "version": 1,
        "scope": "conversation",
        "history": "latest",
        "fork": "initial",
        "initial": lambda: {"count": 0},
    }
)


def dateNow() -> float:
    return int(time.time() * 1000)


def open_(retentionMs: Any) -> SimpleNamespace:
    """A Session with a controllable clock and retention."""
    clock = SimpleNamespace(now=1_000)
    storage = ControlledStorage()
    session = SessionImpl(storage, lambda: clock.now, lambda: retentionMs)
    return SimpleNamespace(storage=storage, session=session, clock=clock, sizes=lambda: session.documentCacheSizes)


async def settleLine(session: SessionImpl) -> None:
    """Let the line run what is queued on it: an eviction job, then return."""

    async def nothing() -> None:
        return None

    await session.readOnLine(nothing)


async def bump(session: SessionImpl, conversationId: int, by: int = 1) -> None:
    async def change(tx: Any) -> None:
        (await tx.doc(CounterDoc, conversationId))["count"] += by

    await session.commit(change, context)


async def noChange(_tx: Any) -> None:
    return None


async def touchSessionCounter(tx: Any) -> None:
    await tx.doc(SessionCounterDoc)


@pytest.fixture
def restoreDefaultRetention() -> Iterator[None]:
    """``onTestFinished(() => setDefaultDocumentRetention(defaultForRun))``."""
    yield
    setDefaultDocumentRetention(defaultForRun)


class TestDocumentCacheRetention:
    async def test_evicts_an_unattached_document_once_its_retention_passed_and_loads_it_again_with_the_same_value(self):
        opened = open_(60_000)
        session, storage, clock, sizes = opened.session, opened.storage, opened.clock, opened.sizes
        conversationId = await createConversation(session)
        await bump(session, conversationId, 3)
        assert sizes()["cached"] > 0
        # Not yet: inside the retention period.
        clock.now += 59_000
        await bump(session, conversationId, 0)
        await settleLine(session)
        assert sizes()["cached"] > 0
        # Past it, with no use since: the next commit evicts it.
        clock.now += 61_000
        await session.commit(noChange, context)
        await session.commit(touchSessionCounter, context)
        await settleLine(session)
        reads = storage.documentReadCount
        assert await session.snapshot(CounterDoc, conversationId, context) == {"count": 3}
        assert storage.documentReadCount > reads
        await session.close(context)

    async def test_evicts_with_retention_0_right_after_use_on_the_line(self):
        opened = open_(0)
        session, sizes = opened.session, opened.sizes
        conversationId = await createConversation(session)
        await bump(session, conversationId)

        async def evicted() -> bool:
            await settleLine(session)
            return sizes()["cached"] == 0

        await eventually(evicted)
        assert await session.snapshot(CounterDoc, conversationId, context) == {"count": 1}
        await session.close(context)

    async def test_never_evicts_a_document_a_watch_or_a_document_state_observes_and_evicts_it_once_the_last_detaches(self):
        opened = open_(1_000)
        session, storage, clock, sizes = opened.session, opened.storage, opened.clock, opened.sizes
        conversationId = await createConversation(session)
        await bump(session, conversationId)
        watch = await session.watchDoc(CounterDoc, conversationId, context)
        state = await session.documentState(CounterDoc, conversationId, context)
        seen: list[int] = []

        async def listener(value: Any, _ops: Any, _context: Any) -> None:
            seen.append(-1 if value is None else value["count"])

        watch.start(listener)
        clock.now += 1_000_000
        await bump(session, conversationId)
        await settleLine(session)
        # Still cached: a read long after its retention passed loads nothing from Storage.
        assert state.value["count"] == 2
        await eventually(lambda: 2 in seen)
        reads = storage.documentReadCount
        clock.now += 1_000_000
        await session.commit(touchSessionCounter, context)
        await settleLine(session)
        assert await session.snapshot(CounterDoc, conversationId, context) == {"count": 2}
        assert storage.documentReadCount == reads
        await watch.stop()
        clock.now += 1_000_000
        await bump(session, conversationId)
        await settleLine(session)
        assert state.value["count"] == 3
        state.dispose()
        clock.now += 1_000_000
        await session.commit(noChange, context)
        await session.commit(touchSessionCounter, context)
        await settleLine(session)
        # The session document was just used; the counter is gone.
        assert sizes()["cached"] == 1
        await session.close(context)

    async def test_never_takes_a_tracker_from_a_transaction_that_holds_it(self):
        opened = open_(0)
        session, clock = opened.session, opened.clock
        conversationId = await createConversation(session)
        await bump(session, conversationId)
        inside = deferred()
        release = deferred()

        async def hold(tx: Any) -> None:
            draft = await tx.doc(CounterDoc, conversationId)
            inside.resolve()
            await release.promise
            draft["count"] += 10

        commit = session.commit(hold, context)
        await inside.promise
        # Retention passes while the transaction holds the document; eviction waits on the line behind it.
        clock.now += 1_000_000
        release.resolve()
        await commit
        assert await session.snapshot(CounterDoc, conversationId, context) == {"count": 11}
        await bump(session, conversationId)
        assert await session.snapshot(CounterDoc, conversationId, context) == {"count": 12}
        await session.close(context)

    async def test_writes_after_a_reload_continue_the_stored_deltas_and_bases(self):
        opened = open_(0)
        session, storage = opened.session, opened.storage
        conversationId = await createConversation(session)
        writes: list[list[dict[str, Any]]] = []
        commit = storage.commit

        def recordingCommit(batch: Any, commitContext: Any) -> Any:
            writes.append(list(batch))
            return commit(batch, commitContext)

        storage.commit = recordingCommit  # type: ignore[method-assign]
        for _ in range(5):
            await bump(session, conversationId)
            await settleLine(session)
        assert await session.snapshot(CounterDoc, conversationId, context) == {"count": 5}
        # Every bump after the first is a change of the same incarnation, never a second creation.
        creations = [
            write
            for batch in writes
            for write in batch
            if write["type"] == "document.create" and write["record"]["kind"] == CounterDoc["definition"]["kind"]
        ]
        assert len(creations) == 1
        await session.close(context)
        # And a fresh Session over the same storage reads the same value.
        reopened = SessionImpl(storage.reopen(), dateNow, lambda: 0)
        assert await reopened.snapshot(CounterDoc, conversationId, context) == {"count": 5}
        await reopened.close(context)

    async def test_keeps_history_and_forks_of_rewindable_documents_after_eviction(self):
        session = open_(0).session
        conversationId = await createConversation(session)

        def setCount(count: int) -> Any:
            async def change(tx: Any) -> int:
                (await tx.doc(RewindableDoc, conversationId))["count"] = count
                return (await tx.appendEntry(conversationId, {"kind": "note", "data": None}))["id"]

            return change

        first = await session.commit(setCount(1), context)
        await settleLine(session)
        second = await session.commit(setCount(2), context)
        await settleLine(session)
        assert await session.snapshotAsOf(RewindableDoc, conversationId, first, context) == {"count": 1}
        assert await session.snapshotAsOf(RewindableDoc, conversationId, second, context) == {"count": 2}

        async def forkAtFirst(tx: Any) -> int:
            return (await tx.forkConversation(conversationId, first, {"ownership": {"kind": "ownerless"}}))["id"]

        fork = await session.commit(forkAtFirst, context)
        await settleLine(session)
        assert await session.snapshot(RewindableDoc, fork, context) == {"count": 1}
        assert await session.snapshot(RewindableDoc, conversationId, context) == {"count": 2}
        await session.close(context)

    async def test_bounds_a_third_party_family_with_a_member_per_event(self):
        opened = open_(0)
        session, sizes = opened.session, opened.sizes
        conversationId = await createConversation(session)
        for seq in range(200):

            async def member(tx: Any, seq: int = seq) -> None:
                await tx.doc(EventDoc, conversationId, str(seq), seq)

            await session.commit(member, context)

        async def evicted() -> bool:
            await settleLine(session)
            return sizes()["cached"] == 0

        await eventually(evicted)
        assert await session.snapshot(EventDoc, conversationId, "123", context) == {"seq": 123}
        await session.close(context)

    async def test_keeps_the_cache_of_a_long_lived_session_bounded_across_many_conversations(self):
        opened = open_(10_000)
        session, clock, sizes = opened.session, opened.clock, opened.sizes
        for _ in range(300):
            conversationId = await createConversation(session)
            await bump(session, conversationId)
            clock.now += 1_000
        await settleLine(session)
        # Only what was used in the last 10 s, about ten conversations' worth.
        assert sizes()["cached"] <= 12
        await session.close(context)

    async def test_never_changes_the_outcome_of_a_commit_when_the_clock_throws(self):
        clock = SimpleNamespace(now=1_000, fail=False)
        storage = ControlledStorage()

        def now() -> float:
            if clock.fail:
                raise RuntimeError("clock")
            return clock.now

        session = SessionImpl(storage, now, lambda: 0)
        conversationId = await createConversation(session)
        clock.fail = True
        await bump(session, conversationId, 42)
        clock.fail = False
        assert await session.snapshot(CounterDoc, conversationId, context) == {"count": 42}
        await session.close(context)

    async def test_evicts_after_a_commit_whose_settlement_fails(self):
        opened = open_(10_000)
        session, clock, sizes = opened.session, opened.clock, opened.sizes
        conversationId = await createConversation(session)
        await bump(session, conversationId)

        # Created first: the predicate runs only for changes to an existing document.
        async def create(tx: Any) -> None:
            await tx.doc(Failing)

        await session.commit(create, context)
        await settleLine(session)

        # Retention passes inside a commit whose checkpoint predicate throws while it settles.
        async def change(tx: Any) -> None:
            (await tx.doc(Failing))["count"] += 1
            clock.now += 1_000_000

        with pytest.raises(Exception, match="checkpoint failed"):
            await session.commit(change, context)
        assert sizes()["cached"] == 0
        await session.close(context)

    async def test_evicts_promptly_once_the_retention_is_shortened(self):
        retention = SimpleNamespace(ms=600_000)
        storage = ControlledStorage()
        clock = SimpleNamespace(now=1_000)
        session = SessionImpl(storage, lambda: clock.now, lambda: retention.ms)
        conversationId = await createConversation(session)
        await bump(session, conversationId)
        retention.ms = 0
        clock.now += 2_000
        # A read, no commit: the sweep pending for ten minutes from now moves up.
        await session.snapshot(CounterDoc, conversationId, context)

        async def evicted() -> bool:
            await settleLine(session)
            return session.documentCacheSizes["cached"] == 0

        await eventually(evicted)
        await session.close(context)

    async def test_does_not_keep_a_replacement_cached_for_an_observer_of_the_retired_incarnation(self):
        opened = open_(0)
        session, sizes = opened.session, opened.sizes
        conversationId = await createConversation(session)
        await bump(session, conversationId)
        state = await session.documentState(CounterDoc, conversationId, context)
        await session.commit(lambda tx: tx.retireDoc(CounterDoc, conversationId), context)
        assert state.value is None
        await bump(session, conversationId, 5)

        async def evicted() -> bool:
            await settleLine(session)
            return sizes()["cached"] == 0

        await eventually(evicted)
        assert await session.snapshot(CounterDoc, conversationId, context) == {"count": 5}
        state.dispose()
        await session.close(context)

    async def test_leaves_no_pin_behind_when_an_observer_s_load_loses_a_race_with_close(self):
        opened = open_(0)
        session, storage = opened.session, opened.storage
        conversationId = await createConversation(session)
        await bump(session, conversationId)

        async def evicted() -> bool:
            await settleLine(session)
            return session.documentCacheSizes["cached"] == 0

        await eventually(evicted)
        gate = storage.holdFindDocument()
        attaching = session.documentState(CounterDoc, conversationId, context)
        await gate.entered
        closing = session.close(context)
        gate.release()
        with pytest.raises(Exception):  # noqa: B017 - upstream's `rejects.toThrow()` names no error
            await attaching
        await closing
        toMatchObject(session.documentCacheSizes, {"cached": 0, "idle": 0, "attached": 0})

    async def test_evicts_on_its_timer_without_a_commit_and_tolerates_a_detach_called_twice(self):
        storage = ControlledStorage()
        session = SessionImpl(storage, dateNow, lambda: 20)
        conversationId = await createConversation(session)
        await bump(session, conversationId)
        state = await session.documentState(CounterDoc, conversationId, context)
        state.dispose()
        state.dispose()
        until = dateNow() + 2_000
        while session.documentCacheSizes["cached"] > 0 and dateNow() < until:
            await asyncio.sleep(0.01)
        assert session.documentCacheSizes["cached"] == 0
        await session.close(context)

    async def test_migrates_a_document_again_after_its_eviction(self):
        V1 = defineDoc(
            {"kind": "test.retention.migrating", "version": 1, "scope": "session", "initial": lambda: {"count": 1}}
        )
        migrations = [0]

        def migrate(value: Any, _fromVersion: Any) -> dict[str, Any]:
            migrations[0] += 1
            return {"count": value["count"], "migrated": True}

        V2 = defineDoc(
            {
                "kind": "test.retention.migrating",
                "version": 2,
                "scope": "session",
                "initial": lambda: {"count": 0, "migrated": True},
                "migrate": migrate,
            }
        )
        session = open_(0).session

        async def create(tx: Any) -> None:
            await tx.doc(V1)

        await session.commit(create, context)
        await settleLine(session)
        assert await session.snapshot(V2, context) == {"count": 1, "migrated": True}
        await settleLine(session)
        assert await session.snapshot(V2, context) == {"count": 1, "migrated": True}
        # Not written back, so each cold load migrates again.
        assert migrations[0] == 2
        await session.close(context)

    async def test_treats_a_retention_that_is_not_a_number_of_milliseconds_as_the_default_and_infinity_as_never(
        self, restoreDefaultRetention: None
    ):
        # The default as shipped, also in the suite run that forces another.
        setDefaultDocumentRetention(600_000)
        for retention in [math.nan, -5, "soon"]:
            opened = open_(retention)
            session, clock, sizes = opened.session, opened.clock, opened.sizes
            conversationId = await createConversation(session)
            await bump(session, conversationId)
            clock.now += 60_000
            await session.commit(noChange, context)
            await settleLine(session)
            # Not dropped at once, as a retention of 0 would: the ten-minute default holds it.
            assert sizes()["cached"] == 1
            await session.close(context)
        opened = open_(math.inf)
        session, clock, sizes = opened.session, opened.clock, opened.sizes
        conversationId = await createConversation(session)
        await bump(session, conversationId)
        clock.now += 1e15
        await session.commit(noChange, context)
        await settleLine(session)
        assert sizes()["cached"] == 1
        await session.close(context)

    async def test_keeps_no_timer_where_timers_cannot_be_unreferenced_and_evicts_on_commits(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        setTimer = sessionModule.setTimeout
        timers = [0]

        def countingSetTimeout(callback: Any, ms: Any = None, *args: Any) -> Any:
            timers[0] += 1
            return setTimer(callback, ms, *args)

        monkeypatch.setattr(sessionModule, "setTimeout", countingSetTimeout)
        opened = open_(1_000)
        session, clock, sizes = opened.session, opened.clock, opened.sizes
        # MISAKA fork: upstream replaces `setTimeout` with one returning a plain number, as in Cloudflare Workers, and the
        # Session notices the missing `unref` on its first timer. An asyncio timer never keeps the loop alive, so misaka
        # has no such runtime and never makes that check (see `SessionImpl._documentTimersUnavailable`); the test puts
        # the Session in the state that check leaves it in.
        session._documentTimersUnavailable = True
        conversationId = await createConversation(session)
        await bump(session, conversationId)
        await bump(session, conversationId)
        # Tried once, then never again.
        assert timers[0] <= 1
        clock.now += 10_000
        await session.commit(noChange, context)
        await settleLine(session)
        assert sizes()["cached"] == 0
        await session.close(context)

    async def test_follows_a_harness_settings_change_at_the_next_use(self, restoreDefaultRetention: None):
        setDefaultDocumentRetention(600_000)
        setup = chatSetup()
        settings = setup.settings
        opened = await openChat(MemoryStorage(), setup)
        harness, root = opened.harness, opened.root

        async def change(tx: Any) -> None:
            (await tx.doc(TestDoc, root.id))["count"] += 1

        await root.commit(change, context)

        def sizesOf() -> dict[str, int]:
            return harness.documentCacheSizes  # type: ignore[no-any-return]

        assert sizesOf()["cached"] > 0
        settings["documentRetentionMs"] = 0
        await harness.snapshot(TestDoc, root.id, context)

        async def evicted() -> bool:
            await settleLine(harness)
            return sizesOf()["cached"] == 0

        await eventually(evicted)
        await harness.close(context)

    async def test_gives_the_same_events_with_documents_evicted_right_after_use_as_with_documents_kept(self):
        async def run(documentRetentionMs: int) -> list[str]:
            setup = chatSetup()
            setup.settings["documentRetentionMs"] = documentRetentionMs
            setup.settings["progress"] = {"outputIntervalMs": 0}

            async def execute(_args: Any, api: Any, *_rest: Any) -> dict[str, Any]:
                for step in ["a\n", "b\n", "c\n"]:
                    api.output(step)
                    await asyncio.sleep(0.005)
                return {}

            addTool(
                setup.registry,
                defineTool(
                    {
                        "name": "steps",
                        "description": "Streams three steps",
                        "parameters": {"type": "object", "properties": {}},
                        "execute": execute,
                    }
                ),
            )
            setup.faux.setResponses(
                [
                    fauxAssistantMessage([fauxToolCall("steps", {}, {"id": "s1"})], {"stopReason": "toolUse"}),
                    fauxAssistantMessage([fauxText("done")]),
                ]
            )
            opened = await openChat(MemoryStorage(), setup)
            harness, root = opened.harness, opened.root
            stream = await watchEvents(harness, root.id, context)
            events: list[dict[str, Any]] = []

            async def onBatch(batch: Any, *_: Any) -> None:
                events.extend(batch)

            stream.start(onBatch)
            await (await root.submit({"type": "input", "content": "go"}, context)).wait(context)
            await harness.waitForIdle(context)
            await stream.stop()
            await harness.close(context)
            # Ids and times differ between runs; the sequence of event types and tool outputs must not.
            return [
                f"update:{jsonStringify(event['output'])}" if event["type"] == "tool_execution_update" else event["type"]
                for event in events
            ]

        assert await run(0) == await run(600_000)

    async def test_keeps_a_reloaded_newer_version_cached_while_an_observer_of_the_older_one_is_attached(self):
        V1 = defineDoc(
            {"kind": "test.retention.versioned", "version": 1, "scope": "session", "initial": lambda: {"count": 3}}
        )
        V2 = defineDoc(
            {
                "kind": "test.retention.versioned",
                "version": 2,
                "scope": "session",
                "initial": lambda: {"count": 0, "v2": True},
                "migrate": lambda value, _fromVersion: {"count": value["count"], "v2": True},
            }
        )
        opened = open_(0)
        session, sizes = opened.session, opened.sizes

        async def create(tx: Any) -> None:
            await tx.doc(V1)

        await session.commit(create, context)
        state = await session.documentState(V1, context)
        assert await session.snapshot(V2, context) == {"count": 3, "v2": True}
        await settleLine(session)
        assert sizes()["cached"] == 1
        state.dispose()

        async def evicted() -> bool:
            await settleLine(session)
            return sizes()["cached"] == 0

        await eventually(evicted)
        await session.close(context)

    async def test_queues_one_sweep_job_not_one_per_document_while_a_commit_holds_the_line(self):
        session = open_(0).session
        conversationIds: list[int] = []
        for _ in range(50):
            id = await createConversation(session)
            await bump(session, id)
            conversationIds.append(id)

        async def evicted() -> bool:
            await settleLine(session)
            return session.documentCacheSizes["cached"] == 0

        await eventually(evicted)
        before = session.documentCacheSizes["sweeps"]

        # One commit loads 50 evicted documents from storage, each touched as it loads; the sweep job stays pending
        # while the line is held.
        async def touchAll(tx: Any) -> None:
            for id in conversationIds:
                (await tx.doc(CounterDoc, id))["count"] += 1
                await asyncio.sleep(0)

        await session.commit(touchAll, context)
        await settleLine(session)
        assert session.documentCacheSizes["cached"] == 0
        assert session.documentCacheSizes["sweeps"] - before <= 2
        await session.close(context)

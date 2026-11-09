"""pi durable's ``test/session-states.test.ts``."""

from __future__ import annotations

import re
from typing import Any

import pytest

from misaka.durable.documents import defineDoc, defineDocFamily
from tests.durable_support import (
    context,
    createConversation,
    documentChanges,
    flush,
    openTestSession,
)

StateDoc = defineDoc(
    {
        "kind": "state.state",
        "version": 1,
        "scope": "session",
        "initial": lambda: {"value": 0, "retained": {"label": "stable"}},
    }
)

FamilyDoc = defineDocFamily(
    {
        "kind": "state.family",
        "version": 1,
        "family": True,
        "scope": "session",
        "initial": lambda seed: {"value": len(seed)},
    }
)


def throws(message: str):
    return pytest.raises(Exception, match=re.escape(message))


async def setValue(session, doc, value):
    async def change(tx):
        (await tx.doc(doc))["value"] = value

    await session.commit(change, context)


async def createState():
    harness = openTestSession()

    async def create(tx):
        await tx.doc(StateDoc)

    await harness.session.commit(create, context)
    await flush()
    return harness


class TestSessionDocumentStates:
    async def test_never_creates_an_absent_document(self):
        ts = openTestSession()
        session, storage = ts.session, ts.storage
        commits = len(storage.commits)
        assert await session.documentState(StateDoc, context) is None
        assert await session.documentState(FamilyDoc, "missing", context) is None
        assert len(storage.commits) == commits
        assert storage.mintCount == 0

    async def test_returns_an_immediately_hydrated_read_only_state_with_contiguous_chord_deliveries(self):
        session = (await createState()).session
        baseline = await session.snapshot(StateDoc, context)
        state = await session.documentState(StateDoc, context)
        deliveries: list[dict[str, Any]] = []
        state.subscribe(
            lambda value, _context, delivery: deliveries.append({"value": value, "sequence": delivery["sequence"]})
        )
        assert state.value is baseline

        await setValue(session, StateDoc, 1)
        await setValue(session, StateDoc, 2)
        await flush()

        assert [delivery["sequence"] for delivery in deliveries] == [0, 1, 2]
        assert [None if d["value"] is None else d["value"]["value"] for d in deliveries] == [0, 1, 2]
        assert state.value is await session.snapshot(StateDoc, context)
        state.dispose()

    async def test_creates_independent_disposable_states_for_one_incarnation(self):
        session = (await createState()).session
        first = await session.documentState(StateDoc, context)
        second = await session.documentState(StateDoc, context)
        assert second is not first
        await setValue(session, StateDoc, 1)
        await flush()
        assert first.value["value"] == 1
        assert second.value["value"] == 1

        first.dispose()
        await setValue(session, StateDoc, 2)
        await flush()
        assert first.value["value"] == 1
        assert second.value["value"] == 2
        second.dispose()

    async def test_shares_exact_committed_value_references_with_chord(self):
        ts = await createState()
        session, publications = ts.session, ts.publications
        state = await session.documentState(StateDoc, context)
        await setValue(session, StateDoc, 4)
        await flush()
        published = documentChanges(publications[-1])[0]
        assert state.value is published["value"]
        assert state.value["retained"] is (await session.snapshot(StateDoc, context))["retained"]
        state.dispose()

    async def test_captures_a_late_baseline_without_redelivering_an_already_covered_commit(self):
        session = (await createState()).session
        await setValue(session, StateDoc, 1)
        state = await session.documentState(StateDoc, context)
        deliveries: list[int] = []
        state.subscribe(lambda _value, _context, delivery: deliveries.append(delivery["sequence"]))
        await flush()
        assert state.value["value"] == 1
        assert deliveries == [0]
        state.dispose()

    async def test_publishes_null_retirement_and_never_follows_a_replacement_incarnation(self):
        session = (await createState()).session
        oldState = await session.documentState(StateDoc, context)

        async def replace(tx):
            await tx.retireDoc(StateDoc)
            (await tx.doc(StateDoc))["value"] = 10

        await session.commit(replace, context)
        await flush()
        assert oldState.value is None

        replacement = await session.documentState(StateDoc, context)
        assert replacement.value["value"] == 10
        await setValue(session, StateDoc, 11)
        await flush()
        assert oldState.value is None
        assert replacement.value["value"] == 11
        oldState.dispose()
        replacement.dispose()

    async def test_cold_loads_a_definition_free_fork_copy(self):
        Copied = defineDoc(
            {
                "kind": "state.copied",
                "version": 1,
                "scope": "conversation",
                "history": "latest",
                "fork": "current",
                "initial": lambda: {"value": 0},
            }
        )
        ts = openTestSession()
        session, storage = ts.session, ts.storage
        parentId = await createConversation(session)

        async def prepare(tx):
            created = await tx.appendEntry(parentId, {"kind": "point"})
            (await tx.doc(Copied, parentId))["value"] = 7
            return created

        entry = await session.commit(prepare, context)

        async def fork(tx):
            return (await tx.forkConversation(parentId, entry["id"], {"ownership": {"kind": "ownerless"}}))["id"]

        childId = await session.commit(fork, context)
        reads = storage.documentReadCount
        state = await session.documentState(Copied, childId, context)
        assert state.value == {"value": 7}
        assert storage.documentReadCount > reads
        state.dispose()

    async def test_hydrates_a_migrated_tracker_without_writing_and_skips_an_equal_version_base_update(self):
        Old = defineDoc({"kind": "state.migration", "version": 1, "scope": "session", "initial": lambda: {"value": 3}})
        Current = defineDoc(
            {
                "kind": "state.migration",
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
        state = await session.documentState(Current, context)
        assert state.value == {"value": 3, "migrated": True}
        assert len(storage.commits) == commits
        baseline = state.value

        await session.commit(touchCurrent, context)
        await flush()
        assert len(storage.commits) == commits + 1
        assert state.value is baseline
        await setValue(session, Current, 4)
        await flush()
        assert state.value == {"value": 4, "migrated": True}
        state.dispose()

    async def test_continues_from_exact_committed_values_after_the_tracker_cache_unloads(self):
        ts = await createState()
        session, storage = ts.session, ts.storage
        state = await session.documentState(StateDoc, context)
        baseline = state.value
        reads = storage.documentReadCount
        await session.unloadDocuments()
        reloaded = await session.snapshot(StateDoc, context)
        assert reloaded == baseline
        assert reloaded is not baseline
        assert storage.documentReadCount > reads
        await setValue(session, StateDoc, 6)
        await flush()
        assert state.value["value"] == 6
        state.dispose()

    async def test_exposes_trusted_shared_immutable_values_without_freezing(self):
        session = (await createState()).session
        snapshot = await session.snapshot(StateDoc, context)
        state = await session.documentState(StateDoc, context)
        assert state.value is snapshot
        state.dispose()

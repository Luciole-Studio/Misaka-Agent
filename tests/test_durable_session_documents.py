"""pi durable's ``test/session-documents.test.ts``."""

from __future__ import annotations

import asyncio
import copy
import datetime
import re
from typing import Any

import pytest

from misaka.durable.documents import defineDoc, defineDocFamily
from misaka.durable.errors import SessionFailed
from misaka.utils.js import UNDEFINED
from tests.chord_helpers import matches
from tests.durable_support import (
    context,
    createConversation,
    documentChanges,
    flush,
    openTestSession,
)

liveInitCount = [0]


def liveInitial() -> dict[str, Any]:
    liveInitCount[0] += 1
    return {"items": [], "nested": {"count": 0}, "other": {"label": "x"}}


LiveDoc = defineDoc(
    {"kind": "test.live", "version": 1, "scope": "conversation", "history": "latest", "fork": "initial", "initial": liveInitial}
)

RewindableLiveDoc = defineDoc(
    {
        "kind": "test.live",
        "version": 1,
        "scope": "conversation",
        "history": "rewindable",
        "fork": "asOf",
        "initial": lambda: {"items": [], "nested": {"count": 0}, "other": {"label": "x"}},
    }
)

LiveDocV2 = defineDoc(
    {
        "kind": "test.live",
        "version": 2,
        "scope": "conversation",
        "history": "latest",
        "fork": "initial",
        "initial": lambda: {"items": [], "nested": {"count": 0}, "other": {"label": "x"}},
    }
)

CounterDoc = defineDoc({"kind": "test.counter", "version": 1, "scope": "session", "initial": lambda: {"count": 0}})

seeds: list[str] = []


def memberInitial(seed: str) -> dict[str, Any]:
    seeds.append(seed)
    return {"seed": seed, "hits": 0}


MemberDoc = defineDocFamily(
    {"kind": "test.member", "version": 1, "family": True, "scope": "session", "initial": memberInitial}
)


def throws(message: str):
    return pytest.raises(Exception, match=re.escape(message))


async def setupLive():
    harness = openTestSession()
    conversationId = await createConversation(harness.session)

    async def create(tx):
        live = await tx.doc(LiveDoc, conversationId)
        live["items"].push("a", "b")

    await harness.session.commit(create, context)
    return harness, conversationId


class TestSessionDocumentTransactions:
    async def test_creates_an_initial_base_on_first_access_and_adopts_it_after_storage_success(self):
        ts = openTestSession()
        session, storage, publications = ts.session, ts.storage, ts.publications
        conversationId = await createConversation(session)

        async def create(tx):
            live = await tx.doc(LiveDoc, conversationId)
            live["message"] = "hello"

        await session.commit(create, context)
        writes = storage.commits[-1]
        assert len(writes) == 1
        assert matches(
            writes[0],
            {
                "type": "document.create",
                "record": {
                    "kind": "test.live",
                    "scope": {"kind": "conversation", "conversationId": conversationId},
                    "history": "latest",
                    "fork": "initial",
                },
                "content": {"kind": "base", "version": 1, "value": {"message": "hello", "items": [], "nested": {"count": 0}}},
            },
        )
        snapshot = await session.snapshot(LiveDoc, conversationId, context)
        assert snapshot == {"message": "hello", "items": [], "nested": {"count": 0}, "other": {"label": "x"}}
        await flush()
        publication = publications[-1]
        published = documentChanges(publication)[0]
        assert published["record"]["createdAt"] == publication["seq"]
        assert published["value"] is snapshot
        assert published["conversationId"] == conversationId
        create = next(write for write in storage.admittedCommits[-1] if write["type"] == "document.create")
        assert create["content"]["value"] is published["value"]

    async def test_never_creates_on_snapshot_and_returns_undefined_when_absent(self):
        ts = openTestSession()
        session, storage = ts.session, ts.storage
        conversationId = await createConversation(session)
        before = len(storage.commits)
        assert await session.snapshot(LiveDoc, conversationId, context) is None
        assert await session.snapshot(CounterDoc, context) is None
        assert await session.snapshot(MemberDoc, "k", context) is None
        assert len(storage.commits) == before
        assert storage.mintCount == 1

    async def test_returns_shared_immutable_snapshots_and_keeps_prior_revisions_stable(self):
        ts, conversationId = await setupLive()
        session = ts.session
        first = await session.snapshot(LiveDoc, conversationId, context)
        assert await session.snapshot(LiveDoc, conversationId, context) is first

        async def change(tx):
            live = await tx.doc(LiveDoc, conversationId)
            live["nested"]["count"] = 1

        await session.commit(change, context)
        second = await session.snapshot(LiveDoc, conversationId, context)
        assert second is not first
        assert first["nested"]["count"] == 0
        assert second["nested"]["count"] == 1
        # Unchanged subtrees are structurally shared between immutable revisions.
        assert second["items"] is first["items"]
        assert second["other"] is first["other"]

    async def test_adopts_by_pointer_swap_and_shares_operation_payloads_with_the_published_revision(self):
        ts, conversationId = await setupLive()
        session, storage, publications = ts.session, ts.storage, ts.publications

        async def change(tx):
            live = await tx.doc(LiveDoc, conversationId)
            live["other"] = {"label": "y"}
            live["items"].push("c")

        await session.commit(change, context)
        await flush()
        snapshot = await session.snapshot(LiveDoc, conversationId, context)
        published = documentChanges(publications[-1])[0]
        assert published["value"] is snapshot
        admitted = next(write for write in storage.admittedCommits[-1] if write["type"] == "document.change")
        assert admitted["content"]["kind"] == "delta"
        assert published["ops"] is admitted["content"]["ops"]
        setOp = next(op for op in published["ops"] if op[0] == "s")
        assert setOp == ["s", ["other"], {"label": "y"}]
        # Trusted immutability: the Session makes no second copy of operation payloads.
        assert setOp[2] is snapshot["other"]

    async def test_copies_assigned_values_per_placement(self):
        ts, conversationId = await setupLive()
        session = ts.session
        value = {"label": "shared"}

        async def change(tx):
            live = await tx.doc(LiveDoc, conversationId)
            live["other"] = value
            live["copy"] = value
            value["label"] = "mutated"

        await session.commit(change, context)
        snapshot = await session.snapshot(LiveDoc, conversationId, context)
        assert snapshot["other"] == {"label": "shared"}
        assert snapshot["copy"] == {"label": "shared"}
        assert snapshot["copy"] is not snapshot["other"]
        assert snapshot["other"] is not value

    async def test_suppresses_writes_and_publications_for_empty_batches(self):
        ts, conversationId = await setupLive()
        session, storage, publications = ts.session, ts.storage, ts.publications
        await flush()
        commits = len(storage.commits)
        published = len(publications)

        async def change(tx):
            live = await tx.doc(LiveDoc, conversationId)
            live["nested"]["count"] = 0
            live["items"].push("z")
            live["items"].pop()

        await session.commit(change, context)
        await flush()
        assert len(storage.commits) == commits
        assert len(publications) == published

    async def test_writes_and_publishes_replayable_nonempty_structural_no_ops(self):
        ts, conversationId = await setupLive()
        session, storage, publications = ts.session, ts.storage, ts.publications
        before = await session.snapshot(LiveDoc, conversationId, context)

        async def change(tx):
            live = await tx.doc(LiveDoc, conversationId)
            first = live["items"].shift()
            live["items"].unshift(first)

        await session.commit(change, context)
        await flush()
        writes = storage.commits[-1]
        assert matches(writes[0], {"type": "document.change", "content": {"kind": "delta"}})
        after = await session.snapshot(LiveDoc, conversationId, context)
        assert after == before
        assert after is not before
        assert len(documentChanges(publications[-1])[0]["ops"]) > 0

    async def test_revokes_escaped_drafts_when_the_callback_settles(self):
        ts, conversationId = await setupLive()
        session = ts.session
        escaped: dict[str, Any] = {}

        async def change(tx):
            escaped["live"] = await tx.doc(LiveDoc, conversationId)
            escaped["items"] = escaped["live"]["items"]
            escaped["live"]["message"] = "inside"

        await session.commit(change, context)
        with pytest.raises(Exception):  # noqa: B017
            escaped["live"]["message"]
        with pytest.raises(Exception):  # noqa: B017
            escaped["live"]["message"] = "outside"
        with pytest.raises(Exception):  # noqa: B017
            escaped["items"].length  # noqa: B018
        with pytest.raises(Exception):  # noqa: B017
            escaped["items"].push("outside")
        assert (await session.snapshot(LiveDoc, conversationId, context))["message"] == "inside"

        returned = await session.commit(lambda tx: tx.doc(LiveDoc, conversationId), context)
        with pytest.raises(Exception):  # noqa: B017
            returned["message"]

    async def test_aborts_every_change_when_the_callback_fails(self):
        ts, conversationId = await setupLive()
        session, storage = ts.session, ts.storage
        before = await session.snapshot(LiveDoc, conversationId, context)
        commits = len(storage.commits)

        async def fail(tx):
            live = await tx.doc(LiveDoc, conversationId)
            counter = await tx.doc(CounterDoc)
            live["message"] = "lost"
            counter["count"] = 5
            raise RuntimeError("callback failed")

        with throws("callback failed"):
            await session.commit(fail, context)
        assert len(storage.commits) == commits
        assert await session.snapshot(LiveDoc, conversationId, context) is before
        assert await session.snapshot(CounterDoc, context) is None

        async def keep(tx):
            (await tx.doc(LiveDoc, conversationId))["message"] = "kept"

        await session.commit(keep, context)
        assert (await session.snapshot(LiveDoc, conversationId, context))["message"] == "kept"

    async def test_memoizes_concurrent_duplicate_acquisition_and_initializes_once(self):
        ts = openTestSession()
        session, storage = ts.session, ts.storage
        conversationId = await createConversation(session)
        initCount = liveInitCount[0]
        mints = storage.mintCount

        async def work(tx):
            first, second = await asyncio.gather(tx.doc(LiveDoc, conversationId), tx.doc(LiveDoc, conversationId))
            assert first is second
            assert await tx.doc(LiveDoc, conversationId) is first
            first["message"] = "once"

        await session.commit(work, context)
        assert liveInitCount[0] == initCount + 1
        assert storage.mintCount == mints + 1
        assert len([write for write in storage.commits[-1] if write["type"] == "document.create"]) == 1

    async def test_uses_the_first_family_seed_and_ignores_seeds_for_existing_members(self):
        session = openTestSession().session
        seeds.clear()

        async def first(tx):
            firstMember = await tx.doc(MemberDoc, "k", "first")
            second = await tx.doc(MemberDoc, "k", "second")
            assert second is firstMember
            firstMember["hits"] = firstMember["hits"] + 1

        async def second(tx):
            member = await tx.doc(MemberDoc, "k", "third")
            member["hits"] = member["hits"] + 1
            other = await tx.doc(MemberDoc, "other", "fourth")
            other["hits"] = other["hits"] + 1

        await session.commit(first, context)
        await session.commit(second, context)
        assert seeds == ["first", "fourth"]
        assert await session.snapshot(MemberDoc, "k", context) == {"seed": "first", "hits": 2}
        assert await session.snapshot(MemberDoc, "other", context) == {"seed": "fourth", "hits": 1}

    async def test_rejects_a_callback_that_succeeds_with_a_pending_acquisition_and_drains_it(self):
        ts, conversationId = await setupLive()
        session, storage = ts.session, ts.storage
        await session.unloadDocuments()
        gate = storage.holdFindDocument()
        commits = len(storage.commits)
        pending: dict[str, Any] = {}

        def work(tx):
            pending["op"] = tx.doc(LiveDoc, conversationId)

        commit = session.commit(work, context)
        await gate.entered
        state = {"settled": False}

        def mark(_done):
            state["settled"] = True

        commit.add_done_callback(mark)
        await flush()
        # The line stays held until the late acquisition settles.
        assert state["settled"] is False
        gate.release()
        with throws("pending Tx operations"):
            await commit
        with throws("Transaction has settled"):
            await pending["op"]
        assert len(storage.commits) == commits

        async def after(tx):
            (await tx.doc(LiveDoc, conversationId))["message"] = "after"

        await session.commit(after, context)
        assert (await session.snapshot(LiveDoc, conversationId, context))["message"] == "after"

    async def test_does_not_initialize_or_mint_for_an_absent_acquisition_that_finishes_after_settlement(self):
        ts = openTestSession()
        session, storage = ts.session, ts.storage
        initialized = [0]

        def lateInitial() -> dict[str, Any]:
            initialized[0] += 1
            return {"count": 0}

        LateDoc = defineDoc({"kind": "test.late", "version": 1, "scope": "session", "initial": lateInitial})
        gate = storage.holdFindDocument()
        mints = storage.mintCount
        pending: dict[str, Any] = {}

        def work(tx):
            pending["op"] = tx.doc(LateDoc)

        commit = session.commit(work, context)
        await gate.entered
        gate.release()
        with throws("pending Tx operations"):
            await commit
        with throws("Transaction has settled"):
            await pending["op"]
        assert initialized[0] == 0
        assert storage.mintCount == mints

    async def test_rejects_with_the_callback_error_when_it_fails_with_a_pending_acquisition(self):
        ts, conversationId = await setupLive()
        session, storage = ts.session, ts.storage
        await session.unloadDocuments()
        gate = storage.holdFindDocument()
        pending: dict[str, Any] = {}

        def work(tx):
            pending["op"] = tx.doc(LiveDoc, conversationId)
            raise RuntimeError("callback failed")

        commit = session.commit(work, context)
        await gate.entered
        gate.release()
        with throws("callback failed"):
            await commit
        with throws("Transaction has settled"):
            await pending["op"]

    async def test_rejects_tx_use_after_the_callback_settles(self):
        ts, conversationId = await setupLive()
        session = ts.session
        captured: dict[str, Any] = {}

        def work(tx):
            captured["tx"] = tx

        await session.commitWith(work, context)
        with throws("Transaction has settled"):
            await captured["tx"].doc(LiveDoc, conversationId)
        with throws("Transaction has settled"):
            await captured["tx"].conversation(conversationId)
        with throws("Transaction has settled"):
            captured["tx"].setTask({})

    async def test_rejects_tokens_whose_semantics_or_version_disagree_with_the_stored_incarnation(self):
        ts, conversationId = await setupLive()
        session = ts.session
        with throws("does not match the supplied definition semantics"):
            await session.snapshot(RewindableLiveDoc, conversationId, context)

        async def rewindable(tx):
            await tx.doc(RewindableLiveDoc, conversationId)

        async def v2(tx):
            await tx.doc(LiveDocV2, conversationId)

        with throws("does not match the supplied definition semantics"):
            await session.commit(rewindable, context)
        with throws("requires migration from version 1"):
            await session.commit(v2, context)

    async def test_rejects_non_json_initializer_values_and_draft_placements_before_storage_admission(self):
        ts = openTestSession()
        session, storage = ts.session, ts.storage
        conversationId = await createConversation(session)
        DateDoc = defineDoc(
            {
                "kind": "test.date",
                "version": 1,
                "scope": "session",
                "initial": lambda: {"at": datetime.datetime.now(tz=datetime.UTC)},
            }
        )

        async def date(tx):
            await tx.doc(DateDoc)

        with throws("strict JSON"):
            await session.commit(date, context)
        commits = len(storage.commits)

        async def undefinedPlacement(tx):
            live = await tx.doc(LiveDoc, conversationId)
            live["items"].push(UNDEFINED)

        with throws("strict JSON"):
            await session.commit(undefinedPlacement, context)
        assert len(storage.commits) == commits
        assert await session.snapshot(LiveDoc, conversationId, context) is None

    async def test_rolls_back_prepared_documents_when_batch_assembly_fails(self):
        ts, conversationId = await setupLive()
        session, storage = ts.session, ts.storage

        async def one(tx):
            (await tx.doc(CounterDoc))["count"] = 1

        await session.commit(one, context)
        live = await session.snapshot(LiveDoc, conversationId, context)
        counter = await session.snapshot(CounterDoc, context)
        commits = len(storage.commits)

        async def failing(tx):
            (await tx.doc(LiveDoc, conversationId))["message"] = "lost"
            (await tx.doc(CounterDoc))["count"] = 2
            # Replacing a missing task fails during assembly, after every change was prepared.
            tx.setTask(
                {
                    "id": 999,
                    "conversationId": conversationId,
                    "kind": "missing",
                    "version": 1,
                    "input": None,
                    "background": False,
                    "abortRequested": False,
                    "state": {"status": "pending", "checkpoint": {"phase": "start"}},
                }
            )

        with throws("Task 999 does not exist"):
            await session.commitWith(failing, context)
        assert len(storage.commits) == commits
        assert await session.snapshot(LiveDoc, conversationId, context) is live
        assert await session.snapshot(CounterDoc, context) is counter

        async def next_(tx):
            (await tx.doc(LiveDoc, conversationId))["message"] = "next"
            (await tx.doc(CounterDoc))["count"] = 3

        await session.commit(next_, context)
        assert (await session.snapshot(CounterDoc, context))["count"] == 3

    async def test_fails_the_session_after_a_storage_failure_and_publishes_nothing(self):
        ts, conversationId = await setupLive()
        session, storage, publications = ts.session, ts.storage, ts.publications
        await flush()
        before = await session.snapshot(LiveDoc, conversationId, context)
        published = len(publications)
        storage.failNextCommit(RuntimeError("disk vanished"))

        async def change(tx):
            (await tx.doc(LiveDoc, conversationId))["message"] = "uncertain"

        with throws("disk vanished"):
            await session.commit(change, context)
        await flush()
        assert len(publications) == published
        assert "message" not in before
        with pytest.raises(SessionFailed):
            await session.snapshot(LiveDoc, conversationId, context)
        with pytest.raises(SessionFailed):
            await session.commit(lambda tx: None, context)
        await session.close(context)

    async def test_keeps_the_previous_revision_unchanged_through_storage_settlement(self):
        ts, conversationId = await setupLive()
        session, storage = ts.session, ts.storage
        before = await session.snapshot(LiveDoc, conversationId, context)
        snapshotCopy = copy.deepcopy(before)
        gate = storage.holdCommits()

        async def change(tx):
            live = await tx.doc(LiveDoc, conversationId)
            live["items"].push("c")
            live["nested"]["count"] = 9

        commit = session.commit(change, context)
        await gate.entered
        assert await session.snapshot(LiveDoc, conversationId, context) is before
        assert before == snapshotCopy
        gate.release()
        await commit
        after = await session.snapshot(LiveDoc, conversationId, context)
        assert after["items"] == ["a", "b", "c"]
        assert before == snapshotCopy

    async def test_retires_documents_and_creates_a_new_incarnation_at_the_same_address(self):
        ts, conversationId = await setupLive()
        session, storage, publications = ts.session, ts.storage, ts.publications
        await flush()
        oldId = documentChanges(publications[-1])[0]["record"]["id"]

        async def change(tx):
            live = await tx.doc(LiveDoc, conversationId)
            live["message"] = "final"
            await tx.retireDoc(LiveDoc, conversationId)
            replacement = await tx.doc(LiveDoc, conversationId)
            assert replacement is not live
            replacement["message"] = "new"

        await session.commit(change, context)
        writes = storage.commits[-1]
        assert len(writes) == 3
        assert any(write["type"] == "document.change" and write["id"] == oldId for write in writes)
        assert {"type": "document.retire", "id": oldId} in writes
        assert any(write["type"] == "document.create" and write["record"]["kind"] == "test.live" for write in writes)
        await flush()
        publication = publications[-1]
        retired, created = documentChanges(publication)
        assert matches(retired, {"record": {"id": oldId}, "value": None, "ops": []})
        assert retired["record"]["retiredAt"] == publication["seq"]
        assert created["record"]["id"] != oldId
        assert created["record"]["createdAt"] == publication["seq"]
        assert matches(created, {"value": {"message": "new", "items": []}, "ops": []})
        assert await session.snapshot(LiveDoc, conversationId, context) is created["value"]

        await session.commit(lambda tx: tx.retireDoc(LiveDoc, conversationId), context)
        assert await session.snapshot(LiveDoc, conversationId, context) is None
        await session.unloadDocuments()
        assert await session.snapshot(LiveDoc, conversationId, context) is None
        # Retiring an absent address is a no-op.
        commits = len(storage.commits)
        await session.commit(lambda tx: tx.retireDoc(LiveDoc, conversationId), context)
        assert len(storage.commits) == commits

    async def test_retires_without_acquisition_and_recreates_both_existing_and_absent_addresses(self):
        ts, conversationId = await setupLive()
        session, storage, publications = ts.session, ts.storage, ts.publications
        await flush()
        oldId = documentChanges(publications[-1])[0]["record"]["id"]
        await session.unloadDocuments()

        async def existing(tx):
            retired = tx.retireDoc(LiveDoc, conversationId)
            replacement = tx.doc(LiveDoc, conversationId)
            live, _ = await asyncio.gather(replacement, retired)
            live["message"] = "replacement"

        await session.commit(existing, context)
        existingWrites = storage.commits[-1]
        assert len(existingWrites) == 2
        assert {"type": "document.retire", "id": oldId} in existingWrites
        assert any(w["type"] == "document.create" and w["record"]["kind"] == "test.live" for w in existingWrites)

        async def absent(tx):
            retired = tx.retireDoc(MemberDoc, "absent")
            replacement = tx.doc(MemberDoc, "absent", "seed")
            member, _ = await asyncio.gather(replacement, retired)
            member["hits"] = 1

        await session.commit(absent, context)
        absentWrites = storage.commits[-1]
        assert len([write for write in absentWrites if write["type"] == "document.retire"]) == 0
        assert len([write for write in absentWrites if write["type"] == "document.create"]) == 1
        assert await session.snapshot(MemberDoc, "absent", context) == {"seed": "seed", "hits": 1}

    async def test_retires_the_existing_incarnation_when_retirement_races_a_pending_acquisition(self):
        ts, conversationId = await setupLive()
        session, storage, publications = ts.session, ts.storage, ts.publications
        await flush()
        oldId = documentChanges(publications[-1])[0]["record"]["id"]

        async def race(tx):
            acquired = tx.doc(LiveDoc, conversationId)
            retired = tx.retireDoc(LiveDoc, conversationId)
            (await acquired)["message"] = "final"
            await retired

        await session.commit(race, context)
        firstWrites = storage.commits[-1]
        assert len(firstWrites) == 2
        assert any(w["type"] == "document.change" and w["id"] == oldId and w["content"]["kind"] == "delta" for w in firstWrites)
        assert {"type": "document.retire", "id": oldId} in firstWrites
        assert await session.snapshot(LiveDoc, conversationId, context) is None

        async def second(tx):
            (await tx.doc(LiveDoc, conversationId))["message"] = "second"

        await session.commit(second, context)
        await flush()
        secondId = documentChanges(publications[-1])[0]["record"]["id"]

        async def recreate(tx):
            acquired = tx.doc(LiveDoc, conversationId)
            retired = tx.retireDoc(LiveDoc, conversationId)
            recreated = tx.doc(LiveDoc, conversationId)
            old, fresh, _ = await asyncio.gather(acquired, recreated, retired)
            assert fresh is not old
            fresh["message"] = "third"

        await session.commit(recreate, context)
        writes = storage.commits[-1]
        assert len(writes) == 2
        assert {"type": "document.retire", "id": secondId} in writes
        assert any(w["type"] == "document.create" and w["record"]["kind"] == "test.live" for w in writes)
        assert (await session.snapshot(LiveDoc, conversationId, context))["message"] == "third"

    async def test_reloads_an_unloaded_document_from_storage(self):
        ts, conversationId = await setupLive()
        session = ts.session
        loaded = await session.snapshot(LiveDoc, conversationId, context)
        await session.unloadDocuments()
        reloaded = await session.snapshot(LiveDoc, conversationId, context)
        assert reloaded is not loaded
        assert reloaded == loaded

        async def change(tx):
            (await tx.doc(LiveDoc, conversationId))["items"].push("c")

        await session.commit(change, context)
        await session.unloadDocuments()
        assert (await session.snapshot(LiveDoc, conversationId, context))["items"] == ["a", "b", "c"]

    async def test_delivers_complete_publications_synchronously_after_adoption(self):
        ts, conversationId = await setupLive()
        session, publications = ts.session, ts.publications
        published = len(publications)
        listenerContext: list[Any] = []
        unsubscribe = session.subscribeCommits(lambda _publication, deliveredContext: listenerContext.append(deliveredContext))

        async def change(tx):
            (await tx.doc(LiveDoc, conversationId))["message"] = "m"
            return "done"

        result = await session.commit(change, context)
        assert result == "done"
        assert listenerContext[0] is context
        assert len(publications) == published + 1
        unsubscribe()

    async def test_publishes_close_synchronously_and_supports_unsubscription(self):
        session = openTestSession().session
        calls: list[str] = []
        session.subscribeClose(lambda: calls.append("active"))
        unsubscribe = session.subscribeClose(lambda: calls.append("removed"))
        unsubscribe()
        await session.close(context)
        assert calls == ["active"]

    async def test_settles_admitted_commits_before_close_and_rejects_later_admission(self):
        ts, conversationId = await setupLive()
        session, storage = ts.session, ts.storage
        gate = storage.holdCommits()

        async def admittedChange(tx):
            (await tx.doc(LiveDoc, conversationId))["message"] = "admitted"

        async def queuedChange(tx):
            (await tx.doc(LiveDoc, conversationId))["message"] = "queued"

        commit = session.commit(admittedChange, context)
        await gate.entered
        queued = session.commit(queuedChange, context)
        admittedSnapshot = session.snapshot(MemberDoc, "absent", context)
        closed = session.close(context)
        with throws("closed"):
            await session.commit(lambda tx: None, context)
        with throws("closed"):
            await session.snapshot(LiveDoc, conversationId, context)
        gate.release()
        await commit
        await queued
        assert await admittedSnapshot is None
        await closed
        with pytest.raises(Exception):  # noqa: B017
            await storage.findDocument(
                {"kind": "test.live", "scope": {"kind": "conversation", "conversationId": conversationId}},
                "current",
                context,
            )

"""pi durable's ``test/session-checkpoints-migrations.test.ts``.

``checkpointWhen`` is a plain function here, so the upstream case that checks its ``this`` receiver (the definition) reads
the definition through its own closure instead.
"""

from __future__ import annotations

import asyncio
import datetime
import re
from typing import Any

import pytest

from misaka.durable.documents import defineDoc, defineDocFamily
from tests.chord_helpers import matches
from tests.durable_support import (
    context,
    createConversation,
    documentChanges,
    flush,
    openTestSession,
)


def throws(message: str):
    return pytest.raises(Exception, match=re.escape(message))


def documentWrites(writes):
    return [w for w in writes if w["type"] in ("document.create", "document.change", "document.retire")]


def sessionDoc(kind, version, initial, **extra):
    return defineDoc({"kind": kind, "version": version, "scope": "session", "initial": initial, **extra})


async def touch(session, token):
    async def run(tx):
        await tx.doc(token)

    await session.commit(run, context)


async def edit(session, token, key, value):
    async def run(tx):
        (await tx.doc(token))[key] = value

    await session.commit(run, context)


class TestSessionDocumentCheckpoints:
    async def test_calls_checkpoint_when_with_its_definition_as_the_receiver(self):
        holder: dict[str, Any] = {}

        def checkpointWhen(value, *_args):
            return value["count"] == holder["doc"]["definition"]["initial"]()["count"] + 2

        holder["doc"] = sessionDoc("checkpoint.receiver", 1, lambda: {"count": 0}, checkpointWhen=checkpointWhen)
        Doc = holder["doc"]
        ts = openTestSession()
        session, storage = ts.session, ts.storage
        for count in range(3):
            await edit(session, Doc, "count", count)
        kinds = [
            write["content"]["kind"]
            for writes in storage.commits
            for write in writes
            if write["type"] == "document.change"
        ]
        assert kinds == ["delta", "base"]

    async def test_selects_bases_only_for_nonempty_ordinary_batches_and_passes_the_exact_prepared_revision_and_ops(self):
        calls: list[dict[str, Any]] = []
        falseCalls: list[dict[str, Any]] = []

        def baseWhen(value, ops, _info):
            calls.append({"value": value, "ops": ops})
            return True

        def deltaWhen(value, ops, _info):
            falseCalls.append({"value": value, "ops": ops})
            return False

        BaseDoc = sessionDoc("checkpoint.base", 1, lambda: {"items": []}, checkpointWhen=baseWhen)
        DeltaDoc = sessionDoc("checkpoint.delta", 1, lambda: {"count": 0}, checkpointWhen=deltaWhen)
        DefaultDoc = sessionDoc("checkpoint.default", 1, lambda: {"count": 0})
        ts = openTestSession()
        session, storage, publications = ts.session, ts.storage, ts.publications

        async def create(tx):
            await tx.doc(BaseDoc)
            await tx.doc(DeltaDoc)
            await tx.doc(DefaultDoc)

        await session.commit(create, context)
        assert len(calls) == 0
        assert len(falseCalls) == 0
        assert all(
            write["type"] == "document.create" and write["content"]["kind"] == "base"
            for write in documentWrites(storage.commits[-1])
        )

        async def change(tx):
            (await tx.doc(BaseDoc))["items"].push("x")
            delta = await tx.doc(DeltaDoc)
            delta["count"] = delta["count"] + 1
            default = await tx.doc(DefaultDoc)
            default["count"] = default["count"] + 1

        await session.commit(change, context)
        await flush()
        writes = [write for write in storage.admittedCommits[-1] if write["type"] == "document.change"]
        assert [write["content"]["kind"] for write in writes] == ["base", "delta", "delta"]
        assert len(calls) == 1
        assert len(falseCalls) == 1
        snapshot = await session.snapshot(BaseDoc, context)
        deltaSnapshot = await session.snapshot(DeltaDoc, context)
        published, publishedDelta = documentChanges(publications[-1])[:2]
        assert calls[0]["value"] is snapshot
        assert calls[0]["ops"] is published["ops"]
        assert falseCalls[0]["value"] is deltaSnapshot
        assert falseCalls[0]["ops"] is publishedDelta["ops"]
        assert writes[1]["content"]["kind"] == "delta"
        assert writes[1]["content"]["ops"] is falseCalls[0]["ops"]
        assert writes[0]["type"] == "document.change"
        assert writes[0]["content"]["kind"] == "base"
        assert writes[0]["content"]["value"] is snapshot

    async def test_passes_the_stored_delta_count_since_the_newest_base_including_after_unload_and_version_bases(self):
        seen: list[int] = []

        def v1When(_value, _ops, info):
            seen.append(info["deltasSinceBase"])
            return info["deltasSinceBase"] >= 2

        def v2When(_value, _ops, info):
            seen.append(info["deltasSinceBase"])
            return False

        V1 = sessionDoc("checkpoint.deltas-since-base", 1, lambda: {"count": 0}, checkpointWhen=v1When)
        V2 = sessionDoc(
            "checkpoint.deltas-since-base",
            2,
            lambda: {"count": 0},
            migrate=lambda value, _from: {"count": value["count"]},
            checkpointWhen=v2When,
        )
        ts = openTestSession()
        session, storage = ts.session, ts.storage

        async def increment(token):
            async def run(tx):
                draft = await tx.doc(token)
                draft["count"] = draft["count"] + 1

            await session.commit(run, context)

        await touch(session, V1)
        await increment(V1)
        await increment(V1)
        await increment(V1)
        await session.unloadDocuments()
        await increment(V1)
        assert seen == [0, 1, 2, 0]
        kinds = [
            writes[0]["content"]["kind"] if writes[0]["type"] == "document.change" else writes[0]["type"]
            for writes in storage.commits[-4:]
        ]
        assert kinds == ["delta", "delta", "base", "delta"]

        # A required version base resets the count without calling the predicate.
        await increment(V2)
        await increment(V2)
        assert seen == [0, 1, 2, 0, 0]
        assert matches(storage.commits[-2][0], {"content": {"kind": "base", "version": 2}})

    async def test_skips_the_predicate_for_empty_batches_but_calls_it_for_nonempty_structural_no_ops(self):
        calls = [0]

        def checkpointWhen(*_args):
            calls[0] += 1
            return False

        Doc = sessionDoc("checkpoint.no-op", 1, lambda: {"items": ["a", "b"]}, checkpointWhen=checkpointWhen)
        ts = openTestSession()
        session, storage = ts.session, ts.storage
        await touch(session, Doc)
        commits = len(storage.commits)

        async def empty(tx):
            value = await tx.doc(Doc)
            value["items"].push("x")
            value["items"].pop()

        await session.commit(empty, context)
        assert calls[0] == 0
        assert len(storage.commits) == commits

        async def noOp(tx):
            value = await tx.doc(Doc)
            first = value["items"].shift()
            value["items"].unshift(first)

        await session.commit(noOp, context)
        assert calls[0] == 1
        assert matches(storage.commits[-1][0], {"type": "document.change", "content": {"kind": "delta"}})

    async def test_rolls_back_every_prepared_document_when_a_checkpoint_predicate_throws(self):
        flags = {"throwCheckpoint": True}
        firstCalls = [0]

        def firstWhen(*_args):
            firstCalls[0] += 1
            return False

        def secondWhen(*_args):
            if flags["throwCheckpoint"]:
                raise RuntimeError("checkpoint failed")
            return False

        First = sessionDoc("checkpoint.rollback.first", 1, lambda: {"count": 0}, checkpointWhen=firstWhen)
        Second = sessionDoc("checkpoint.rollback.second", 1, lambda: {"count": 0}, checkpointWhen=secondWhen)
        ts = openTestSession()
        session, storage, publications = ts.session, ts.storage, ts.publications

        async def create(tx):
            await tx.doc(First)
            await tx.doc(Second)

        await session.commit(create, context)
        await flush()
        first = await session.snapshot(First, context)
        second = await session.snapshot(Second, context)
        commits = len(storage.commits)
        published = len(publications)

        async def failing(tx):
            (await tx.doc(First))["count"] = 1
            (await tx.doc(Second))["count"] = 2

        with throws("checkpoint failed"):
            await session.commit(failing, context)
        await flush()
        assert firstCalls[0] == 1
        assert len(storage.commits) == commits
        assert len(publications) == published
        assert await session.snapshot(First, context) is first
        assert await session.snapshot(Second, context) is second

        flags["throwCheckpoint"] = False

        async def succeeding(tx):
            (await tx.doc(First))["count"] = 3
            (await tx.doc(Second))["count"] = 4

        await session.commit(succeeding, context)
        assert await session.snapshot(First, context) == {"count": 3}
        assert await session.snapshot(Second, context) == {"count": 4}

    async def test_persists_repeated_false_decisions_as_deltas_and_replays_the_complete_tail(self):
        calls = [0]

        def checkpointWhen(*_args):
            calls[0] += 1
            return False

        Doc = sessionDoc("checkpoint.tail", 1, lambda: {"values": []}, checkpointWhen=checkpointWhen)
        ts = openTestSession()
        session, storage = ts.session, ts.storage
        await touch(session, Doc)
        for value in range(1, 9):

            async def push(tx, value=value):
                (await tx.doc(Doc))["values"].push(value)

            await session.commit(push, context)
        changes = [write for writes in storage.commits for write in writes if write["type"] == "document.change"]
        assert len(changes) == 8
        assert all(write["content"]["kind"] == "delta" for write in changes)
        assert calls[0] == 8
        await session.unloadDocuments()
        assert await session.snapshot(Doc, context) == {"values": [1, 2, 3, 4, 5, 6, 7, 8]}

    async def test_keeps_a_prepared_root_replacement_as_a_delta_when_the_predicate_is_false(self):
        initial = {f"field{index}": 0 for index in range(4100)}
        Doc = sessionDoc(
            "checkpoint.root-replacement", 1, lambda: initial, checkpointWhen=lambda *_args: False
        )
        ts = openTestSession()
        session, storage = ts.session, ts.storage
        await touch(session, Doc)

        async def change(tx):
            value = await tx.doc(Doc)
            for index in range(4100):
                value[f"field{index}"] = 1

        await session.commit(change, context)
        write = storage.commits[-1][0]
        assert write["type"] == "document.change"
        assert write["content"]["kind"] == "delta"
        assert len(write["content"]["ops"]) == 1
        assert write["content"]["ops"][0][0] == "r"
        await session.unloadDocuments()
        assert (await session.snapshot(Doc, context))["field4099"] == 1

    async def test_uses_ordinary_checkpoint_selection_before_retirement(self):
        calls = [0]

        def checkpointWhen(*_args):
            calls[0] += 1
            return True

        Doc = sessionDoc("checkpoint.retire", 1, lambda: {"count": 0}, checkpointWhen=checkpointWhen)
        ts = openTestSession()
        session, storage = ts.session, ts.storage
        await touch(session, Doc)

        async def change(tx):
            (await tx.doc(Doc))["count"] = 1
            await tx.retireDoc(Doc)

        await session.commit(change, context)
        assert calls[0] == 1
        writes = documentWrites(storage.commits[-1])
        assert len(writes) == 2
        assert writes[0]["type"] == "document.change"
        assert writes[0]["content"]["kind"] == "base"
        assert writes[1]["type"] == "document.retire"


class TestSessionDocumentMigrations:
    async def test_migrates_read_only_once_per_cold_load_copies_the_callback_result_and_writes_nothing(self):
        Old = sessionDoc("migration.read-only", 1, lambda: {"count": 2})
        calls = [0]
        retained: dict[str, Any] = {}

        def migrate(value, fromVersion):
            assert fromVersion == 1
            calls[0] += 1
            retained["value"] = {"count": value["count"], "labels": ["migrated"]}
            return retained["value"]

        Current = sessionDoc("migration.read-only", 3, lambda: {"count": 0, "labels": []}, migrate=migrate)
        ts = openTestSession()
        session, storage = ts.session, ts.storage
        await touch(session, Old)
        await session.unloadDocuments()
        commits = len(storage.commits)

        first = await session.snapshot(Current, context)
        assert await session.snapshot(Current, context) is first
        assert calls[0] == 1
        assert len(storage.commits) == commits
        retained["value"]["count"] = 99
        retained["value"]["labels"].append("mutated")
        assert first == {"count": 2, "labels": ["migrated"]}

        await session.unloadDocuments()
        second = await session.snapshot(Current, context)
        assert second is not first
        assert second == first
        assert calls[0] == 2
        assert len(storage.commits) == commits

    async def test_writes_the_required_base_on_the_first_successful_transaction_then_writes_deltas(self):
        Old = sessionDoc("migration.transition", 1, lambda: {"count": 4})
        checkpoints = [0]

        def checkpointWhen(*_args):
            checkpoints[0] += 1
            return False

        Current = sessionDoc(
            "migration.transition",
            3,
            lambda: {"count": 0},
            migrate=lambda value, fromVersion: {"count": value["count"] + fromVersion - 1},
            checkpointWhen=checkpointWhen,
        )
        ts = openTestSession()
        session, storage, publications = ts.session, ts.storage, ts.publications
        await touch(session, Old)
        await session.unloadDocuments()
        # An observer of the older shape.
        watch = await session.watchDoc(Old, context)
        frames: list[Any] = []

        async def onOld(value, ops, _context):
            frames.append({"value": value, "ops": ops})

        watch.start(onOld)
        assert await session.snapshot(Current, context) == {"count": 4}
        snapshot = await session.snapshot(Current, context)
        # An observer of the new shape: the migration changes nothing it sees.
        current = await session.watchDoc(Current, context)
        currentFrames: list[Any] = []

        async def onCurrent(value, _ops, _context):
            currentFrames.append(value)

        current.start(onCurrent)

        await touch(session, Current)
        await flush()
        assert matches(
            storage.commits[-1][0],
            {"type": "document.change", "content": {"kind": "base", "version": 3, "value": {"count": 4}}},
        )
        assert checkpoints[0] == 0
        # The migration-only base is published, so the older-shape watch receives the new value as a root replacement.
        assert matches(documentChanges(publications[-1]), [{"version": 3, "value": {"count": 4}, "ops": []}])
        assert frames == [{"value": {"count": 4}, "ops": [["r", {"count": 4}]]}]
        assert currentFrames == []
        await watch.stop()
        await current.stop()
        assert await session.snapshot(Current, context) is snapshot

        await edit(session, Current, "count", 7)
        assert matches(storage.commits[-1][0], {"type": "document.change", "content": {"kind": "delta", "version": 3}})
        assert checkpoints[0] == 1
        await session.unloadDocuments()
        assert await session.snapshot(Current, context) == {"count": 7}

    async def test_rolls_migration_and_edits_back_with_the_callback_then_coalesces_later_edits_into_one_base(self):
        Old = sessionDoc("migration.rollback", 1, lambda: {"count": 1})
        migrations = [0]

        def migrate(value, _from):
            migrations[0] += 1
            return {"count": value["count"], "migrated": True}

        Current = sessionDoc("migration.rollback", 2, lambda: {"count": 0, "migrated": False}, migrate=migrate)
        ts = openTestSession()
        session, storage, publications = ts.session, ts.storage, ts.publications
        await touch(session, Old)
        await session.unloadDocuments()
        commits = len(storage.commits)

        async def failing(tx):
            (await tx.doc(Current))["count"] = 8
            raise RuntimeError("rollback")

        with throws("rollback"):
            await session.commit(failing, context)
        assert len(storage.commits) == commits
        assert await session.snapshot(Current, context) == {"count": 1, "migrated": True}
        assert migrations[0] == 1

        async def change(tx):
            value = await tx.doc(Current)
            value["count"] = 9
            value["migrated"] = False

        await session.commit(change, context)
        writes = documentWrites(storage.commits[-1])
        assert len(writes) == 1
        assert writes[0]["type"] == "document.change"
        assert writes[0]["content"] == {"kind": "base", "version": 2, "value": {"count": 9, "migrated": False}}
        await flush()
        published = documentChanges(publications[-1])[0]
        admitted = storage.admittedCommits[-1][0]
        assert admitted["type"] == "document.change"
        assert admitted["content"]["kind"] == "base"
        assert published["value"] is await session.snapshot(Current, context)
        assert published["value"] is admitted["content"]["value"]
        assert len(published["ops"]) > 0
        assert migrations[0] == 1

    async def test_strict_checks_migration_results_before_tracker_ownership_and_remains_usable_after_rejection(self):
        Old = sessionDoc("migration.invalid", 1, lambda: {"count": 1})
        Invalid = sessionDoc(
            "migration.invalid",
            2,
            dict,
            migrate=lambda _value, _from: {"invalid": datetime.datetime.now(tz=datetime.UTC)},
        )
        Other = sessionDoc("migration.invalid.other", 1, lambda: {"ok": True})
        ts = openTestSession()
        session, storage = ts.session, ts.storage
        await touch(session, Old)
        await session.unloadDocuments()
        commits = len(storage.commits)
        with throws("strict JSON"):
            await session.snapshot(Invalid, context)
        with throws("strict JSON"):
            await touch(session, Invalid)
        assert len(storage.commits) == commits
        await touch(session, Other)
        assert await session.snapshot(Other, context) == {"ok": True}

    async def test_rejects_newer_stored_versions_and_older_versions_without_migration_for_snapshots_and_transactions(self):
        V2 = sessionDoc("migration.compatibility", 2, lambda: {"count": 2})
        V1 = sessionDoc("migration.compatibility", 1, lambda: {"count": 1})
        V3WithoutMigration = sessionDoc("migration.compatibility", 3, lambda: {"count": 3})
        ts = openTestSession()
        session, storage = ts.session, ts.storage
        await touch(session, V2)
        await session.unloadDocuments()
        commits = len(storage.commits)

        with throws("newer version 2 than 1"):
            await session.snapshot(V1, context)
        with throws("newer version 2 than 1"):
            await touch(session, V1)
        with throws("requires migration from version 2"):
            await session.snapshot(V3WithoutMigration, context)
        with throws("requires migration from version 2"):
            await touch(session, V3WithoutMigration)
        assert len(storage.commits) == commits

    async def test_persists_a_required_migration_base_before_retirement_without_consulting_the_checkpoint_predicate(self):
        Old = sessionDoc("migration.retire", 1, lambda: {"count": 1})

        def mustNotRun(*_args):
            raise RuntimeError("must not run")

        Current = sessionDoc(
            "migration.retire",
            2,
            lambda: {"count": 0},
            migrate=lambda value, _from: {"count": value["count"]},
            checkpointWhen=mustNotRun,
        )
        ts = openTestSession()
        session, storage = ts.session, ts.storage
        await touch(session, Old)
        await session.unloadDocuments()

        async def change(tx):
            await tx.doc(Current)
            await tx.retireDoc(Current)

        await session.commit(change, context)
        writes = documentWrites(storage.commits[-1])
        assert len(writes) == 2
        assert writes[0]["type"] == "document.change"
        assert writes[0]["content"] == {"kind": "base", "version": 2, "value": {"count": 1}}
        assert writes[1]["type"] == "document.retire"

    async def test_leaves_unaccessed_older_documents_and_unavailable_definitions_untouched(self):
        FirstV1 = sessionDoc("migration.lazy.first", 1, lambda: {"count": 1})
        SecondV1 = sessionDoc("migration.lazy.second", 1, lambda: {"count": 2})
        secondMigrations = [0]
        FirstV2 = sessionDoc(
            "migration.lazy.first", 2, lambda: {"count": 0}, migrate=lambda value, _from: {"count": value["count"]}
        )

        def secondMigrate(value, _from):
            secondMigrations[0] += 1
            return {"count": value["count"]}

        sessionDoc("migration.lazy.second", 2, lambda: {"count": 0}, migrate=secondMigrate)
        ts = openTestSession()
        session, storage = ts.session, ts.storage

        async def create(tx):
            await tx.doc(FirstV1)
            await tx.doc(SecondV1)

        await session.commit(create, context)
        await session.unloadDocuments()
        commits = len(storage.commits)
        assert await session.snapshot(FirstV2, context) == {"count": 1}
        assert len(storage.commits) == commits
        assert secondMigrations[0] == 0
        secondRecord = await storage.findDocument(
            {"kind": "migration.lazy.second", "scope": {"kind": "session"}}, "current", context
        )
        assert (await storage.document(secondRecord["id"], "current", context))["version"] == 1


class TestSessionHistoricalDocumentSnapshots:
    async def test_migrates_current_and_historical_rewindable_values_independently_and_follows_fork_ancestry(self):
        def rewindable(version, initial, **extra):
            return defineDoc(
                {
                    "kind": "history.migration",
                    "version": version,
                    "scope": "conversation",
                    "history": "rewindable",
                    "fork": "asOf",
                    "initial": initial,
                    **extra,
                }
            )

        V1 = rewindable(1, lambda: {"count": 0})
        Family = defineDocFamily(
            {
                "kind": "history.family",
                "version": 1,
                "family": True,
                "scope": "conversation",
                "history": "rewindable",
                "fork": "asOf",
                "initial": lambda seed: {"seed": seed, "count": 0},
            }
        )
        migrations: list[int] = []

        def migrate(value, fromVersion):
            migrations.append(fromVersion)
            return {"count": value["count"], "version": 3}

        V3 = rewindable(3, lambda: {"count": 0, "version": 3}, migrate=migrate)
        ts = openTestSession()
        session, storage = ts.session, ts.storage
        conversationId = await createConversation(session)
        entries: dict[str, int] = {}

        async def first(tx):
            entries["first"] = (await tx.appendEntry(conversationId, {"kind": "first"}))["id"]
            (await tx.doc(V1, conversationId))["count"] = 1
            (await tx.doc(Family, conversationId, "member", "seed"))["count"] = 1

        async def second(tx):
            entries["second"] = (await tx.appendEntry(conversationId, {"kind": "second"}))["id"]
            (await tx.doc(V1, conversationId))["count"] = 2

        await session.commit(first, context)
        await session.commit(second, context)
        await session.unloadDocuments()
        commits = len(storage.commits)
        assert await session.snapshot(V3, conversationId, context) == {"count": 2, "version": 3}
        assert migrations == [1]
        assert len(storage.commits) == commits

        async def third(tx):
            entries["third"] = (await tx.appendEntry(conversationId, {"kind": "third"}))["id"]
            await tx.doc(V3, conversationId)

        await session.commit(third, context)
        assert any(
            write["type"] == "document.change" and write["content"]["kind"] == "base" and write["content"]["version"] == 3
            for write in storage.commits[-1]
        )

        assert await session.snapshotAsOf(V3, conversationId, entries["first"], context) == {"count": 1, "version": 3}
        assert await session.snapshotAsOf(V3, conversationId, entries["second"], context) == {"count": 2, "version": 3}
        assert await session.snapshotAsOf(V3, conversationId, entries["third"], context) == {"count": 2, "version": 3}
        assert migrations == [1, 1, 1]
        assert await session.snapshotAsOf(Family, conversationId, "member", entries["first"], context) == {
            "seed": "seed",
            "count": 1,
        }

        async def fork(tx):
            return (await tx.forkConversation(conversationId, entries["second"], {"ownership": {"kind": "ownerless"}}))["id"]

        childId = await session.commit(fork, context)
        assert await session.snapshotAsOf(V3, childId, entries["first"], context) == {"count": 1, "version": 3}
        assert await session.snapshotAsOf(V3, childId, entries["second"], context) == {"count": 2, "version": 3}
        assert await session.snapshotAsOf(Family, childId, "member", entries["first"], context) == {
            "seed": "seed",
            "count": 1,
        }
        with throws(f"Entry {entries['third']} is not visible"):
            await session.snapshotAsOf(V3, childId, entries["third"], context)

    async def test_selects_the_incarnation_alive_at_the_entry_commit_across_retirement_and_recreation(self):
        Doc = defineDoc(
            {
                "kind": "history.incarnation",
                "version": 1,
                "scope": "conversation",
                "history": "rewindable",
                "fork": "asOf",
                "initial": lambda: {"value": "initial"},
            }
        )
        session = openTestSession().session
        conversationId = await createConversation(session)

        async def before(tx):
            return (await tx.appendEntry(conversationId, {"kind": "before"}))["id"]

        beforeCreation = await session.commit(before, context)
        entries: dict[str, int] = {}

        async def create(tx):
            entries["createdAt"] = (await tx.appendEntry(conversationId, {"kind": "create"}))["id"]
            (await tx.doc(Doc, conversationId))["value"] = "old"

        async def retire(tx):
            entries["retiredAt"] = (await tx.appendEntry(conversationId, {"kind": "retire"}))["id"]
            await tx.retireDoc(Doc, conversationId)

        async def recreate(tx):
            entries["recreatedAt"] = (await tx.appendEntry(conversationId, {"kind": "recreate"}))["id"]
            (await tx.doc(Doc, conversationId))["value"] = "new"

        await session.commit(create, context)
        await session.commit(retire, context)
        await session.commit(recreate, context)

        assert await session.snapshotAsOf(Doc, conversationId, beforeCreation, context) is None
        assert await session.snapshotAsOf(Doc, conversationId, entries["createdAt"], context) == {"value": "old"}
        assert await session.snapshotAsOf(Doc, conversationId, entries["retiredAt"], context) is None
        assert await session.snapshotAsOf(Doc, conversationId, entries["recreatedAt"], context) == {"value": "new"}
        await session.close(context)
        with throws("closed"):
            await session.snapshotAsOf(Doc, conversationId, entries["recreatedAt"], context)


V1Doc = sessionDoc("cache.versioned", 1, lambda: {"name": "first"})
V2Doc = sessionDoc(
    "cache.versioned", 2, lambda: {"names": []}, migrate=lambda value, _from: {"names": [value["name"]]}
)


class TestSessionTrackerCacheAcrossDefinitionVersions:
    async def test_migrates_a_document_cached_by_an_older_token_without_unloading(self):
        ts = openTestSession()
        session, storage = ts.session, ts.storage
        await session.commit(lambda tx: tx.doc(V1Doc), context)
        assert await session.snapshot(V1Doc, context) == {"name": "first"}

        # Reloaded extension code accesses the still-cached document with a newer token.
        assert await session.snapshot(V2Doc, context) == {"names": ["first"]}

        async def push(tx):
            (await tx.doc(V2Doc))["names"].push("second")

        await session.commit(push, context)
        write = documentWrites(storage.commits[-1])[0]
        assert matches(
            write,
            {
                "type": "document.change",
                "content": {"version": 2, "kind": "base", "value": {"names": ["first", "second"]}},
            },
        )
        with throws("newer version 2"):
            await session.snapshot(V1Doc, context)

    async def test_sends_observers_of_an_older_shape_a_root_replacement_after_a_newer_token_writes(self):
        session = openTestSession().session
        await session.commit(lambda tx: tx.doc(V1Doc), context)
        state = await session.documentState(V1Doc, context)
        watch = await session.watchDoc(V1Doc, context)
        frames: list[Any] = []
        delivered: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        async def listener(_value, ops, _context):
            frames.append(ops)
            if not delivered.done():
                delivered.set_result(None)

        watch.start(listener)

        async def push(tx):
            (await tx.doc(V2Doc))["names"].push("second")

        await session.commit(push, context)
        await delivered
        assert state.value == {"names": ["first", "second"]}
        assert watch.value == {"names": ["first", "second"]}
        assert frames == [[["r", {"names": ["first", "second"]}]]]
        state.dispose()
        await watch.stop()

    async def test_serves_an_older_token_from_storage_after_a_newer_token_migrated_only_in_memory(self):
        session = openTestSession().session
        await session.commit(lambda tx: tx.doc(V1Doc), context)
        await session.unloadDocuments()
        assert await session.snapshot(V2Doc, context) == {"names": ["first"]}
        assert await session.snapshot(V1Doc, context) == {"name": "first"}
        await edit(session, V1Doc, "name", "renamed")
        assert await session.snapshot(V2Doc, context) == {"names": ["renamed"]}

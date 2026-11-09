"""pi durable's ``test/session-forks.test.ts``."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

from misaka.durable.documents import defineDoc, defineDocFamily
from misaka.durable.errors import SessionFailed
from misaka.durable.tasks import defineTask
from tests.chord_helpers import matches
from tests.durable_support import (
    context,
    createConversation,
    documentChanges,
    documentCopyChanges,
    flush,
    openTestSession,
)

OWNERLESS = {"ownership": {"kind": "ownerless"}}


def throws(message: str):
    return pytest.raises(Exception, match=re.escape(message))


def documentCreates(writes):
    return [write for write in writes if write["type"] == "document.create"]


def documentCopies(writes):
    return [write for write in writes if write["type"] == "document.copy"]


def conversationDoc(kind, history, fork, initial, **extra):
    return defineDoc(
        {"kind": kind, "version": 1, "scope": "conversation", "history": history, "fork": fork, "initial": initial, **extra}
    )


def forkOf(parentId, at):
    async def fork(tx):
        return await tx.forkConversation(parentId, at, OWNERLESS)

    return fork


class TestSessionConversationDocumentForks:
    async def test_copies_as_of_and_current_singleton_and_family_bases_while_leaving_initial_documents_absent(self):
        AsOf = conversationDoc("fork.policies.as-of", "rewindable", "asOf", lambda: {"value": "as-initial"})
        Current = conversationDoc("fork.policies.current", "latest", "current", lambda: {"value": "current-initial"})
        Initial = conversationDoc("fork.policies.initial", "latest", "initial", lambda: {"value": "fresh"})
        AsOfFamily = defineDocFamily(
            {
                "kind": "fork.policies.as-of-family",
                "version": 1,
                "family": True,
                "scope": "conversation",
                "history": "rewindable",
                "fork": "asOf",
                "initial": lambda seed: {"value": seed},
            }
        )
        CurrentFamily = defineDocFamily(
            {
                "kind": "fork.policies.current-family",
                "version": 1,
                "family": True,
                "scope": "conversation",
                "history": "latest",
                "fork": "current",
                "initial": lambda seed: {"value": seed},
            }
        )
        ts = openTestSession()
        session, storage, publications = ts.session, ts.storage, ts.publications
        parentId = await createConversation(session)
        state: dict[str, Any] = {}

        async def first(tx):
            state["forkAt"] = (await tx.appendEntry(parentId, {"kind": "fork-point"}))["id"]
            (await tx.doc(AsOf, parentId))["value"] = "as-at-fork"
            (await tx.doc(Current, parentId))["value"] = "current-at-fork"
            (await tx.doc(Initial, parentId))["value"] = "parent-only"
            (await tx.doc(AsOfFamily, parentId, "a", "unused"))["value"] = "family-as-a"
            (await tx.doc(AsOfFamily, parentId, "b", "unused"))["value"] = "family-as-b"
            (await tx.doc(CurrentFamily, parentId, "a", "unused"))["value"] = "family-current-a"

        async def second(tx):
            (await tx.doc(AsOf, parentId))["value"] = "as-after-fork"
            (await tx.doc(Current, parentId))["value"] = "current-when-copied"
            (await tx.doc(AsOfFamily, parentId, "a", "unused"))["value"] = "family-as-after"
            (await tx.doc(CurrentFamily, parentId, "a", "unused"))["value"] = "family-current-when-copied"

        await session.commit(first, context)
        await session.commit(second, context)
        await flush()
        documentReads = storage.documentReadCount

        child = await session.commit(forkOf(parentId, state["forkAt"]), context)
        await flush()
        assert storage.documentReadCount == documentReads

        assert await session.snapshot(AsOf, child["id"], context) == {"value": "as-at-fork"}
        assert await session.snapshot(Current, child["id"], context) == {"value": "current-when-copied"}
        assert await session.snapshot(Initial, child["id"], context) is None
        assert await session.snapshot(AsOfFamily, child["id"], "a", context) == {"value": "family-as-a"}
        assert await session.snapshot(AsOfFamily, child["id"], "b", context) == {"value": "family-as-b"}
        assert await session.snapshot(CurrentFamily, child["id"], "a", context) == {"value": "family-current-when-copied"}

        admitted = storage.admittedCommits[-1]
        copies = documentCopies(admitted)
        assert len(copies) == 5
        assert all(write["record"]["scope"]["kind"] == "conversation" for write in copies)
        assert all(write["record"]["scope"]["conversationId"] == child["id"] for write in copies)
        publication = publications[-1]
        copied = documentCopyChanges(publication)
        assert len(copied) == 5
        for change in copied:
            assert change["record"]["createdAt"] == publication["seq"]
            assert change["conversationId"] == child["id"]
            write = next(candidate for candidate in copies if candidate["record"]["id"] == change["record"]["id"])
            assert change["source"] == write["source"]

        parentRecord = await storage.findDocument(
            {"kind": AsOf["definition"]["kind"], "scope": {"kind": "conversation", "conversationId": parentId}},
            "current",
            context,
        )
        childRecord = await storage.findDocument(
            {"kind": AsOf["definition"]["kind"], "scope": {"kind": "conversation", "conversationId": child["id"]}},
            "current",
            context,
        )
        assert childRecord["id"] != parentRecord["id"]

        async def independent(tx):
            (await tx.doc(AsOf, child["id"]))["value"] = "child-independent"

        await session.commit(independent, context)
        assert await session.snapshot(AsOf, parentId, context) == {"value": "as-after-fork"}
        assert await session.snapshot(AsOf, child["id"], context) == {"value": "child-independent"}

        async def created(tx):
            (await tx.doc(Initial, child["id"]))["value"] = "child-created"

        await session.commit(created, context)
        assert await session.snapshot(Initial, child["id"], context) == {"value": "child-created"}

    async def test_uses_final_document_state_from_the_fork_entry_commit_while_excluding_later_same_commit_entries(self):
        Doc = conversationDoc("fork.same-commit", "rewindable", "asOf", lambda: {"value": "initial"})
        session = openTestSession().session
        parentId = await createConversation(session)
        state: dict[str, Any] = {}

        async def work(tx):
            state["forkAt"] = (await tx.appendEntry(parentId, {"kind": "included"}))["id"]
            (await tx.doc(Doc, parentId))["value"] = "final-state-of-commit"
            state["excluded"] = (await tx.appendEntry(parentId, {"kind": "excluded"}))["id"]

        await session.commit(work, context)
        child = await session.commit(forkOf(parentId, state["forkAt"]), context)
        assert await session.snapshot(Doc, child["id"], context) == {"value": "final-state-of-commit"}
        visible = await session.commit(lambda tx: tx.scanEntries({"conversationId": child["id"]}, 10), context)
        ids = [item["id"] for item in visible["items"]]
        assert state["forkAt"] in ids
        assert state["excluded"] not in ids

    async def test_selects_the_entry_owning_ancestor_for_as_of_copies_and_the_immediate_parent_for_current_copies(self):
        AsOf = conversationDoc("fork.ancestry.as-of", "rewindable", "asOf", lambda: {"value": "initial"})
        Current = conversationDoc("fork.ancestry.current", "latest", "current", lambda: {"value": "initial"})
        session = openTestSession().session
        rootId = await createConversation(session)
        state: dict[str, Any] = {}

        async def root(tx):
            state["inherited"] = (await tx.appendEntry(rootId, {"kind": "root"}))["id"]
            (await tx.doc(AsOf, rootId))["value"] = "root-at-entry"
            (await tx.doc(Current, rootId))["value"] = "root-current"

        await session.commit(root, context)
        parent = await session.commit(forkOf(rootId, state["inherited"]), context)

        async def parentWork(tx):
            state["parentEntry"] = (await tx.appendEntry(parent["id"], {"kind": "parent"}))["id"]
            (await tx.doc(AsOf, parent["id"]))["value"] = "parent-at-own-entry"
            (await tx.doc(Current, parent["id"]))["value"] = "parent-current"

        await session.commit(parentWork, context)

        inheritedFork = await session.commit(forkOf(parent["id"], state["inherited"]), context)
        assert await session.snapshot(AsOf, inheritedFork["id"], context) == {"value": "root-at-entry"}
        assert await session.snapshot(Current, inheritedFork["id"], context) == {"value": "parent-current"}

        ownEntryFork = await session.commit(forkOf(parent["id"], state["parentEntry"]), context)
        assert await session.snapshot(AsOf, ownEntryFork["id"], context) == {"value": "parent-at-own-entry"}
        assert await session.snapshot(Current, ownEntryFork["id"], context) == {"value": "parent-current"}

    async def test_copies_the_stored_value_and_version_without_consulting_migration_definitions_or_migrated_caches(self):
        V1 = conversationDoc("fork.stored-version", "rewindable", "asOf", lambda: {"count": 1})
        migrations = [0]

        def migrate(value, fromVersion):
            assert fromVersion == 1
            migrations[0] += 1
            return {"count": value["count"], "migrated": True}

        V3 = defineDoc(
            {
                "kind": "fork.stored-version",
                "version": 3,
                "scope": "conversation",
                "history": "rewindable",
                "fork": "asOf",
                "initial": lambda: {"count": 0, "migrated": False},
                "migrate": migrate,
            }
        )
        ts = openTestSession()
        session, storage = ts.session, ts.storage
        parentId = await createConversation(session)
        state: dict[str, Any] = {}

        async def work(tx):
            state["forkAt"] = (await tx.appendEntry(parentId, {"kind": "point"}))["id"]
            await tx.doc(V1, parentId)

        await session.commit(work, context)
        await session.unloadDocuments()
        assert await session.snapshot(V3, parentId, context) == {"count": 1, "migrated": True}
        assert migrations[0] == 1

        child = await session.commit(forkOf(parentId, state["forkAt"]), context)
        assert migrations[0] == 1
        childRecord = await storage.findDocument(
            {"kind": V1["definition"]["kind"], "scope": {"kind": "conversation", "conversationId": child["id"]}},
            "current",
            context,
        )
        childStored = await storage.document(childRecord["id"], "current", context)
        assert matches(childStored, {"version": 1, "value": {"count": 1}})
        assert await session.snapshot(V3, child["id"], context) == {"count": 1, "migrated": True}
        assert migrations[0] == 2

    async def test_coalesces_a_typed_migration_and_override_into_the_copied_creation_base(self):
        V1 = conversationDoc("fork.override", "rewindable", "asOf", lambda: {"count": 2})
        checkpoints = [0]

        def checkpointWhen(*_args):
            checkpoints[0] += 1
            return False

        V2 = defineDoc(
            {
                "kind": "fork.override",
                "version": 2,
                "scope": "conversation",
                "history": "rewindable",
                "fork": "asOf",
                "initial": lambda: {"count": 0, "migrated": False},
                "migrate": lambda value, _from: {"count": value["count"], "migrated": True},
                "checkpointWhen": checkpointWhen,
            }
        )
        ts = openTestSession()
        session, storage, publications = ts.session, ts.storage, ts.publications
        parentId = await createConversation(session)
        state: dict[str, Any] = {}

        async def work(tx):
            state["forkAt"] = (await tx.appendEntry(parentId, {"kind": "point"}))["id"]
            await tx.doc(V1, parentId)

        await session.commit(work, context)

        documentReads = storage.documentReadCount

        async def fork(tx):
            created = await tx.forkConversation(parentId, state["forkAt"], OWNERLESS)
            value = await tx.doc(V2, created["id"])
            value["count"] = 9
            return created

        child = await session.commit(fork, context)
        await flush()
        assert storage.documentReadCount == documentReads + 1
        creates = documentCreates(storage.admittedCommits[-1])
        assert len(creates) == 1
        assert matches(creates[0]["content"], {"kind": "base", "version": 2, "value": {"count": 9, "migrated": True}})
        assert checkpoints[0] == 0
        assert matches(documentChanges(publications[-1]), [{"version": 2}])
        assert await session.snapshot(V2, child["id"], context) == {"count": 9, "migrated": True}
        parentRecord = await storage.findDocument(
            {"kind": V1["definition"]["kind"], "scope": {"kind": "conversation", "conversationId": parentId}},
            "current",
            context,
        )
        assert (await storage.document(parentRecord["id"], "current", context))["version"] == 1

    async def test_copies_the_incarnation_alive_at_the_fork_point_across_retirement_and_recreation(self):
        Doc = conversationDoc("fork.incarnations", "rewindable", "asOf", lambda: {"value": "initial"})
        session = openTestSession().session
        parentId = await createConversation(session)
        state: dict[str, Any] = {}

        async def old(tx):
            state["oldAt"] = (await tx.appendEntry(parentId, {"kind": "old"}))["id"]
            (await tx.doc(Doc, parentId))["value"] = "old"

        async def retired(tx):
            state["retiredAt"] = (await tx.appendEntry(parentId, {"kind": "retired"}))["id"]
            await tx.retireDoc(Doc, parentId)

        async def new(tx):
            state["newAt"] = (await tx.appendEntry(parentId, {"kind": "new"}))["id"]
            (await tx.doc(Doc, parentId))["value"] = "new"

        await session.commit(old, context)
        await session.commit(retired, context)
        await session.commit(new, context)

        oldChild = await session.commit(forkOf(parentId, state["oldAt"]), context)
        emptyChild = await session.commit(forkOf(parentId, state["retiredAt"]), context)
        newChild = await session.commit(forkOf(parentId, state["newAt"]), context)
        assert await session.snapshot(Doc, oldChild["id"], context) == {"value": "old"}
        assert await session.snapshot(Doc, emptyChild["id"], context) is None
        assert await session.snapshot(Doc, newChild["id"], context) == {"value": "new"}

    async def test_rejects_invisible_fork_points_before_admission_and_remains_usable(self):
        ts = openTestSession()
        session, storage, publications = ts.session, ts.storage, ts.publications
        rootId = await createConversation(session)
        state: dict[str, Any] = {}

        async def work(tx):
            state["visible"] = (await tx.appendEntry(rootId, {"kind": "visible"}))["id"]
            state["hidden"] = (await tx.appendEntry(rootId, {"kind": "hidden"}))["id"]

        await session.commit(work, context)
        parent = await session.commit(forkOf(rootId, state["visible"]), context)
        await flush()
        commits = len(storage.commits)
        published = len(publications)
        with throws(f"Entry {state['hidden']} is not visible"):
            await session.commit(forkOf(parent["id"], state["hidden"]), context)
        await flush()
        assert len(storage.commits) == commits
        assert len(publications) == published
        independent = await session.commit(lambda tx: tx.createConversation(OWNERLESS), context)
        assert await storage.conversation(independent["id"], context) == independent

    async def test_rejects_duplicate_as_of_and_current_selections_for_one_child_address_before_admission(self):
        AsOf = conversationDoc("fork.duplicate-policy", "rewindable", "asOf", lambda: {"value": "as-of"})
        Current = conversationDoc("fork.duplicate-policy", "latest", "current", lambda: {"value": "current"})
        ts = openTestSession()
        session, storage = ts.session, ts.storage
        parentId = await createConversation(session)
        state: dict[str, Any] = {}

        async def work(tx):
            state["oldAt"] = (await tx.appendEntry(parentId, {"kind": "old"}))["id"]
            await tx.doc(AsOf, parentId)

        async def touchCurrent(tx):
            await tx.doc(Current, parentId)

        await session.commit(work, context)
        await session.commit(lambda tx: tx.retireDoc(AsOf, parentId), context)
        await session.commit(touchCurrent, context)
        commits = len(storage.commits)
        with throws("Fork selects multiple source documents"):
            await session.commit(forkOf(parentId, state["oldAt"]), context)
        assert len(storage.commits) == commits
        independent = await session.commit(lambda tx: tx.createConversation(OWNERLESS), context)
        assert await storage.conversation(independent["id"], context) == independent

    async def test_rejects_current_and_as_of_source_writes_in_the_fork_transaction(self):
        Current = conversationDoc("fork.same-transaction-current", "latest", "current", lambda: {"value": "committed"})
        AsOf = conversationDoc("fork.same-transaction-as-of", "rewindable", "asOf", lambda: {"value": "committed"})
        ts = openTestSession()
        session, storage = ts.session, ts.storage
        parentId = await createConversation(session)
        state: dict[str, Any] = {}

        async def work(tx):
            state["forkAt"] = (await tx.appendEntry(parentId, {"kind": "point"}))["id"]
            await tx.doc(Current, parentId)
            await tx.doc(AsOf, parentId)

        await session.commit(work, context)
        forkAt = state["forkAt"]
        commits = len(storage.commits)

        async def writeThenFork(tx):
            (await tx.doc(Current, parentId))["value"] = "before-fork"
            await tx.forkConversation(parentId, forkAt, OWNERLESS)

        async def forkThenWrite(tx):
            await tx.forkConversation(parentId, forkAt, OWNERLESS)
            (await tx.doc(Current, parentId))["value"] = "after-fork"

        async def asOfThenFork(tx):
            (await tx.doc(AsOf, parentId))["value"] = "as-of-write"
            await tx.forkConversation(parentId, forkAt, OWNERLESS)

        with throws("Cannot change fork source document"):
            await session.commit(writeThenFork, context)
        with throws("Cannot change fork source document"):
            await session.commit(forkThenWrite, context)
        with throws("Cannot change fork source document"):
            await session.commit(asOfThenFork, context)
        assert len(storage.commits) == commits
        assert await session.snapshot(Current, parentId, context) == {"value": "committed"}
        child = await session.commit(forkOf(parentId, forkAt), context)
        assert await session.snapshot(Current, child["id"], context) == {"value": "committed"}

    async def test_rolls_every_copied_base_back_when_later_pre_admission_assembly_fails(self):
        Copied = conversationDoc("fork.rollback.copied", "rewindable", "asOf", lambda: {"value": "copied"})
        flags = {"failCheckpoint": True}

        def checkpointWhen(*_args):
            if flags["failCheckpoint"]:
                raise RuntimeError("checkpoint failed")
            return False

        Failure = defineDoc(
            {
                "kind": "fork.rollback.failure",
                "version": 1,
                "scope": "session",
                "initial": lambda: {"count": 0},
                "checkpointWhen": checkpointWhen,
            }
        )
        ts = openTestSession()
        session, storage, publications = ts.session, ts.storage, ts.publications
        parentId = await createConversation(session)
        state: dict[str, Any] = {}

        async def work(tx):
            state["forkAt"] = (await tx.appendEntry(parentId, {"kind": "point"}))["id"]
            await tx.doc(Copied, parentId)
            await tx.doc(Failure)

        await session.commit(work, context)
        await flush()
        commits = len(storage.commits)
        published = len(publications)

        async def failing(tx):
            state["childId"] = (await tx.forkConversation(parentId, state["forkAt"], OWNERLESS))["id"]
            (await tx.doc(Failure))["count"] = 1

        with throws("checkpoint failed"):
            await session.commit(failing, context)
        await flush()
        assert len(storage.commits) == commits
        assert len(publications) == published
        assert await storage.conversation(state["childId"], context) is None
        flags["failCheckpoint"] = False

        async def after(tx):
            (await tx.doc(Failure))["count"] = 2

        await session.commit(after, context)
        assert await session.snapshot(Failure, context) == {"count": 2}
        assert await session.snapshot(Copied, parentId, context) == {"value": "copied"}

    async def test_fails_the_session_on_a_rejected_fork_commit_which_leaves_no_effect(self):
        Doc = conversationDoc("fork.storage-rejected", "rewindable", "asOf", lambda: {"value": "source"})
        ts = openTestSession()
        session, storage = ts.session, ts.storage
        parentId = await createConversation(session)
        state: dict[str, Any] = {}

        async def work(tx):
            state["forkAt"] = (await tx.appendEntry(parentId, {"kind": "point"}))["id"]
            await tx.doc(Doc, parentId)

        await session.commit(work, context)
        rejection = RuntimeError("copy rejected")
        storage.failNextCommit(rejection)

        async def rejected(tx):
            state["rejectedChildId"] = (await tx.forkConversation(parentId, state["forkAt"], OWNERLESS))["id"]

        with pytest.raises(RuntimeError) as raised:
            await session.commit(rejected, context)
        assert raised.value is rejection
        with pytest.raises(SessionFailed) as failed:
            await session.commit(lambda tx: tx.createConversation(OWNERLESS), context)
        assert failed.value.cause is rejection
        # The failed Session closed its Storage; reopened, it holds nothing of the batch.
        assert await session.closed == {"reason": "failed", "error": rejection}
        assert await storage.reopen().conversation(state["rejectedChildId"], context) is None

    async def test_retires_a_copied_document_and_can_recreate_the_address_in_the_fork_transaction(self):
        Doc = conversationDoc("fork.retire-recreate", "rewindable", "asOf", lambda: {"value": "fresh"})
        ts = openTestSession()
        session, storage = ts.session, ts.storage
        parentId = await createConversation(session)
        state: dict[str, Any] = {}

        async def work(tx):
            state["forkAt"] = (await tx.appendEntry(parentId, {"kind": "point"}))["id"]
            (await tx.doc(Doc, parentId))["value"] = "copied"

        await session.commit(work, context)

        async def fork(tx):
            created = await tx.forkConversation(parentId, state["forkAt"], OWNERLESS)
            await tx.retireDoc(Doc, created["id"])
            (await tx.doc(Doc, created["id"]))["value"] = "replacement"
            return created

        child = await session.commit(fork, context)
        writes = storage.commits[-1]
        assert len([write for write in writes if write["type"] == "document.copy"]) == 1
        assert len([write for write in writes if write["type"] == "document.create"]) == 1
        assert len([write for write in writes if write["type"] == "document.retire"]) == 1
        assert await session.snapshot(Doc, child["id"], context) == {"value": "replacement"}

    async def test_copies_only_conversation_documents_leaving_session_and_task_documents_in_their_original_scopes(self):
        Copied = conversationDoc("fork.scope.conversation", "latest", "current", lambda: {"value": "conversation"})
        SessionOnly = defineDoc(
            {"kind": "fork.scope.session", "version": 1, "scope": "session", "initial": lambda: {"value": "session"}}
        )
        TaskOnly = defineDoc(
            {"kind": "fork.scope.task", "version": 1, "scope": "task", "initial": lambda: {"value": "task"}}
        )

        async def noop(*_args):
            pass

        Work = defineTask(
            {
                "name": "fork.scope.work",
                "version": 1,
                "initial": lambda _input: {"phase": "start"},
                "phases": {"start": noop},
                "abort": noop,
            }
        )
        ts = openTestSession()
        session, storage, publications = ts.session, ts.storage, ts.publications
        parentId = await createConversation(session)
        state: dict[str, Any] = {}

        async def work(tx):
            state["forkAt"] = (await tx.appendEntry(parentId, {"kind": "point"}))["id"]
            state["taskId"] = await tx.createTask(
                Work, None, {"ownership": {"kind": "conversation"}, "conversationId": parentId}
            )
            await tx.doc(Copied, parentId)
            await tx.doc(SessionOnly)
            await tx.doc(TaskOnly, state["taskId"])

        await session.commit(work, context)
        child = await session.commit(forkOf(parentId, state["forkAt"]), context)
        await flush()
        copies = documentCopies(storage.commits[-1])
        assert [write["record"]["kind"] for write in copies] == [Copied["definition"]["kind"]]
        assert [change["record"]["kind"] for change in documentCopyChanges(publications[-1])] == [
            Copied["definition"]["kind"]
        ]
        assert await session.snapshot(Copied, child["id"], context) == {"value": "conversation"}
        assert await session.snapshot(SessionOnly, context) == {"value": "session"}
        assert await session.snapshot(TaskOnly, state["taskId"], context) == {"value": "task"}

    async def test_copies_every_family_member_across_storage_scan_pages(self):
        Family = defineDocFamily(
            {
                "kind": "fork.pagination",
                "version": 1,
                "family": True,
                "scope": "conversation",
                "history": "latest",
                "fork": "current",
                "initial": lambda seed: {"value": seed},
            }
        )
        ts = openTestSession()
        session, storage = ts.session, ts.storage
        parentId = await createConversation(session)

        async def point(tx):
            return (await tx.appendEntry(parentId, {"kind": "point"}))["id"]

        forkAt = await session.commit(point, context)

        async def members(tx):
            await asyncio.gather(*(tx.doc(Family, parentId, f"member-{index}", index) for index in range(260)))

        await session.commit(members, context)
        child = await session.commit(forkOf(parentId, forkAt), context)
        assert len(documentCopies(storage.commits[-1])) == 260
        assert await session.snapshot(Family, child["id"], "member-0", context) == {"value": 0}
        assert await session.snapshot(Family, child["id"], "member-259", context) == {"value": 259}

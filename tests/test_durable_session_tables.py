"""pi durable's ``test/session-tables.test.ts`` and the runtime parts of ``test/session-definitions.test.ts``."""

from __future__ import annotations

import re
from typing import Any

import pytest

from misaka.durable.documents import defineDoc, defineDocFamily
from misaka.durable.errors import ReadAfterWrite
from misaka.durable.tasks import defineTask
from misaka.utils.js import UNDEFINED
from tests.durable_support import (
    context,
    createConversation,
    documentChanges,
    flush,
    openTestSession,
)


async def _noop(*args: Any) -> None:
    pass


WorkTask = defineTask(
    {
        "name": "test.work",
        "version": 1,
        "initial": lambda _input: {"phase": "start"},
        "phases": {"start": _noop, "next": _noop},
        "abort": _noop,
    }
)

ProgressDoc = defineDoc(
    {"kind": "test.progress", "version": 1, "scope": "task", "initial": lambda: {"lines": []}}
)
StepDoc = defineDocFamily(
    {"kind": "test.step", "version": 1, "family": True, "scope": "task", "initial": lambda _seed: {"lines": []}}
)
NotesDoc = defineDoc(
    {
        "kind": "test.notes",
        "version": 1,
        "scope": "conversation",
        "history": "rewindable",
        "fork": "asOf",
        "initial": lambda: {"text": ""},
    }
)


def terminal(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": task["id"],
        "conversationId": task["conversationId"],
        "kind": task["kind"],
        "version": task["version"],
        "input": task["input"],
        "background": task["background"],
        "abortRequested": task["abortRequested"],
        "state": {"status": "terminal", "outcome": {"status": "completed", "result": {"ok": True}}},
    }


async def createTask(session, conversationId, withDocument=False):
    async def create(tx):
        taskId = await tx.createTask(
            WorkTask, {"path": "a"}, {"ownership": {"kind": "conversation"}, "conversationId": conversationId}
        )
        if withDocument:
            (await tx.doc(ProgressDoc, taskId))["lines"].push("started")
        return taskId

    return await session.commit(create, context)


def throws(message: str):
    return pytest.raises(Exception, match=re.escape(message))


class TestDocumentDefinitions:
    def test_validates_persisted_version_semantics(self):
        def initial() -> dict[str, Any]:
            return {"value": 0}

        with throws("positive integer"):
            defineDoc({"kind": "k", "version": 0, "scope": "session", "initial": initial})
        with throws("positive integer"):
            defineDoc({"kind": "k", "version": 1.5, "scope": "session", "initial": initial})
        assert defineDoc({"kind": "", "version": 1, "scope": "session", "initial": initial})["definition"]["kind"] == ""
        assert defineDoc({"kind": "t.session", "version": 1, "scope": "session", "initial": initial})["definition"]["kind"] == "t.session"

    async def test_snapshots_every_owner_and_key_overload_of_a_fresh_session(self):
        # The runtime half of upstream's "types every owner, key, and seed overload": a snapshot of a document whose owner
        # does not exist resolves to ``undefined``, and a seed is not part of a snapshot.
        from misaka.durable import ROOT_CONVERSATION_ID
        from misaka.durable.documents import defineDoc, defineDocFamily
        from misaka.durable.session.session import SessionImpl
        from misaka.durable.storage.memory import MemoryStorage

        def initial() -> dict[str, Any]:
            return {"value": 0}

        SessionDoc = defineDoc({"kind": "t.session", "version": 1, "scope": "session", "initial": initial})
        LatestDoc = defineDoc({"kind": "t.latest", "version": 1, "scope": "conversation", "initial": initial})
        TaskDoc = defineDoc({"kind": "t.task", "version": 1, "scope": "task", "initial": initial})
        SessionFamily = defineDocFamily(
            {"kind": "t.session-family", "version": 1, "family": True, "scope": "session", "initial": lambda seed: {"value": seed}}
        )
        ConversationFamily = defineDocFamily(
            {"kind": "t.conversation-family", "version": 1, "family": True, "scope": "conversation", "initial": lambda seed: {"value": seed}}
        )
        TaskFamily = defineDocFamily(
            {"kind": "t.task-family", "version": 1, "family": True, "scope": "task", "initial": lambda seed: {"value": seed}}
        )
        session = SessionImpl(MemoryStorage())
        try:
            assert await session.snapshot(SessionDoc, context) is None
            assert await session.snapshot(LatestDoc, ROOT_CONVERSATION_ID, context) is None
            assert await session.snapshot(TaskDoc, 1, context) is None
            assert await session.snapshot(SessionFamily, "k", context) is None
            assert await session.snapshot(ConversationFamily, ROOT_CONVERSATION_ID, "k", context) is None
            assert await session.snapshot(TaskFamily, 1, "k", context) is None
            # Upstream also makes a seeded snapshot call and swallows its outcome: a seed is a type error there.
        finally:
            await session.close(context)


class TestSessionTransactionTables:
    async def test_allows_table_reads_only_before_the_first_table_write(self):
        ts = openTestSession()
        session = ts.session
        conversationId = await createConversation(session)

        async def work(tx):
            assert await tx.conversation(conversationId) == {"id": conversationId}
            assert await tx.scanConversations({}, 1) == {"items": [{"id": conversationId}]}
            assert await tx.scanTasks({"conversationId": conversationId}, 10) == {"items": []}
            assert await tx.scanEntries({"conversationId": conversationId}, 10) == {"items": []}
            await tx.appendEntry(conversationId, {"kind": "note"})
            with pytest.raises(ReadAfterWrite):
                await tx.conversation(conversationId)
            with throws("Tx.task() cannot read tables after the first table write"):
                await tx.task(1)
            with pytest.raises(ReadAfterWrite):
                await tx.entry(1)
            with pytest.raises(ReadAfterWrite):
                await tx.scanConversations({}, 10)
            with pytest.raises(ReadAfterWrite):
                await tx.scanEntries({"conversationId": conversationId}, 10)
            # Document access remains available after table writes.
            (await tx.doc(NotesDoc, conversationId))["text"] = "after write"

        await session.commit(work, context)
        assert await session.snapshot(NotesDoc, conversationId, context) == {"text": "after write"}

    async def test_passes_caller_selected_limits_and_cursors_through_table_scans(self):
        session = openTestSession().session
        ids = [await createConversation(session), await createConversation(session), await createConversation(session)]

        async def work(tx):
            first = await tx.scanConversations({}, 2)
            assert [item["id"] for item in first["items"]] == ids[:2]
            assert first.get("next") is not None
            second = await tx.scanConversations({}, 2, first["next"])
            assert [item["id"] for item in second["items"]] == ids[2:]
            assert second.get("next") is None

        await session.commit(work, context)

    async def test_treats_synchronous_set_task_as_the_first_table_write(self):
        session = openTestSession().session
        conversationId = await createConversation(session)
        taskId = await createTask(session, conversationId)

        async def work(tx):
            task = await tx.task(taskId)
            tx.setTask(task)
            with pytest.raises(ReadAfterWrite):
                await tx.task(taskId)

        await session.commitWith(work, context)

    async def test_creates_conversations_entries_and_tasks_with_minted_ids(self):
        ts = openTestSession()
        session, storage, publications = ts.session, ts.storage, ts.publications

        async def work(tx):
            conversation = await tx.createConversation({"ownership": {"kind": "ownerless"}})
            first = await tx.appendEntry(conversation["id"], {"kind": "note", "data": "one"})
            headed = await tx.appendEntry(conversation["id"], {"kind": "summary", "head": "self"})
            task = await tx.createTask(
                WorkTask,
                {"path": "x"},
                {"ownership": {"kind": "conversation"}, "conversationId": conversation["id"], "background": True},
            )
            return {"conversation": conversation, "first": first, "headed": headed, "task": task}

        created = await session.commit(work, context)
        ids = [created["conversation"]["id"], created["first"]["id"], created["headed"]["id"], created["task"]]
        assert len(set(ids)) == 4
        assert created["headed"]["head"] == created["headed"]["id"]
        assert created["first"] == {
            "id": created["first"]["id"],
            "conversationId": created["conversation"]["id"],
            "kind": "note",
            "data": "one",
        }
        assert (await storage.entry(created["headed"]["id"], context))["entry"] == created["headed"]
        assert await storage.task(created["task"], context) == {
            "id": created["task"],
            "conversationId": created["conversation"]["id"],
            "kind": "test.work",
            "version": 1,
            "input": {"path": "x"},
            "background": True,
            "abortRequested": False,
            "state": {"status": "pending", "checkpoint": {"phase": "start"}},
        }
        await flush()
        changes = publications[-1]["changes"]
        assert len(changes) == 4
        admitted = storage.admittedCommits[-1]
        for change in changes:
            assert any(write is change for write in admitted)
        assert sorted(change["type"] for change in changes) == ["conversation", "entry", "entry", "task"]
        assert next(change for change in changes if change["type"] == "conversation")["value"] is created["conversation"]
        entries = [change["value"] for change in changes if change["type"] == "entry"]
        assert created["first"] in entries
        assert created["headed"] in entries
        with throws("requires options.conversationId"):
            await session.commit(
                lambda tx: tx.createTask(WorkTask, {"path": "x"}, {"ownership": {"kind": "conversation"}}), context
            )
        with throws("Conversation 12345 does not exist"):
            await session.commit(lambda tx: tx.appendEntry(12345, {"kind": "note"}), context)

    async def test_creates_a_conversation_with_an_explicitly_staged_task_owner(self):
        ts = openTestSession()
        session, storage = ts.session, ts.storage
        parentId = await createConversation(session)

        async def work(tx):
            supervisorId = await tx.createTask(
                WorkTask,
                {"path": "background"},
                {"ownership": {"kind": "conversation"}, "conversationId": parentId, "background": True},
            )
            child = await tx.createConversation({"ownership": {"kind": "task", "taskId": supervisorId}})
            return {"supervisorId": supervisorId, "child": child}

        created = await session.commit(work, context)

        assert created["child"]["owner"] == {"conversationId": parentId, "taskId": created["supervisorId"]}
        assert await storage.conversation(created["child"]["id"], context) == created["child"]

        async def move(tx):
            supervisor = await tx.task(created["supervisorId"])
            tx.setTask({**supervisor, "conversationId": created["child"]["id"]})

        with throws(f"Task {created['supervisorId']} cannot change conversations"):
            await session.commitWith(move, context)
        assert (await storage.task(created["supervisorId"], context))["conversationId"] == parentId

    async def test_rejects_missing_terminal_and_abort_marked_conversation_owners_atomically(self):
        ts = openTestSession()
        session, storage = ts.session, ts.storage
        parentId = await createConversation(session)
        staged: dict[str, Any] = {}

        async def moveStaged(tx):
            taskId = await tx.createTask(
                WorkTask, {"path": "move"}, {"ownership": {"kind": "conversation"}, "conversationId": parentId}
            )
            staged["movedStagedTaskId"] = taskId
            tx.setTask(
                {
                    "id": taskId,
                    "conversationId": 998,
                    "kind": WorkTask["definition"]["name"],
                    "version": WorkTask["definition"]["version"],
                    "input": {"path": "move"},
                    "background": False,
                    "abortRequested": False,
                    "state": {"status": "pending", "checkpoint": {"phase": "start"}},
                }
            )

        with throws("cannot change conversations"):
            await session.commitWith(moveStaged, context)
        assert await storage.task(staged["movedStagedTaskId"], context) is None

        with throws("Conversation owner task 999 does not exist"):
            await session.commit(
                lambda tx: tx.createConversation({"ownership": {"kind": "task", "taskId": 999}}), context
            )

        async def abortMarked(tx):
            supervisorId = await tx.createTask(
                WorkTask, {"path": "aborting"}, {"ownership": {"kind": "conversation"}, "conversationId": parentId}
            )
            staged["rejectedChildId"] = (
                await tx.createConversation({"ownership": {"kind": "task", "taskId": supervisorId}})
            )["id"]
            tx.setTask(
                {
                    "id": supervisorId,
                    "conversationId": parentId,
                    "kind": WorkTask["definition"]["name"],
                    "version": WorkTask["definition"]["version"],
                    "input": {"path": "aborting"},
                    "background": False,
                    "abortRequested": True,
                    "state": {"status": "pending", "checkpoint": {"phase": "start"}},
                }
            )

        with throws("is abort-marked"):
            await session.commitWith(abortMarked, context)
        assert staged["rejectedChildId"] is not None
        assert await storage.conversation(staged["rejectedChildId"], context) is None

        async def terminalOwner(tx):
            supervisorId = await tx.createTask(
                WorkTask, {"path": "terminal"}, {"ownership": {"kind": "conversation"}, "conversationId": parentId}
            )
            await tx.createConversation({"ownership": {"kind": "task", "taskId": supervisorId}})
            tx.setTask(
                {
                    "id": supervisorId,
                    "conversationId": parentId,
                    "kind": WorkTask["definition"]["name"],
                    "version": WorkTask["definition"]["version"],
                    "input": {"path": "terminal"},
                    "background": False,
                    "abortRequested": False,
                    "state": {"status": "terminal", "outcome": {"status": "completed", "result": {"ok": True}}},
                }
            )

        with throws("is terminal"):
            await session.commitWith(terminalOwner, context)

        terminalOwnerId = await createTask(session, parentId)

        async def settle(tx):
            tx.setTask(terminal(await tx.task(terminalOwnerId)))

        await session.commitWith(settle, context)
        with throws("is terminal"):
            await session.commit(
                lambda tx: tx.createConversation({"ownership": {"kind": "task", "taskId": terminalOwnerId}}), context
            )

    async def test_takes_ownership_of_table_json_and_rejects_non_strict_values(self):
        ts = openTestSession()
        session, storage = ts.session, ts.storage
        conversationId = await createConversation(session)
        payload = {"nested": {"value": 1}}

        async def own(tx):
            created = await tx.appendEntry(conversationId, {"kind": "data", "data": payload})
            payload["nested"]["value"] = 2
            return created

        entry = await session.commit(own, context)
        assert (await storage.entry(entry["id"], context))["entry"]["data"] == {"nested": {"value": 1}}
        omitted = await session.commit(
            lambda tx: tx.appendEntry(conversationId, {"kind": "omitted", "data": UNDEFINED}), context
        )
        assert "data" not in omitted
        assert "data" not in (await storage.entry(omitted["id"], context))["entry"]

        commits = len(storage.commits)

        async def invalid(tx):
            await tx.appendEntry(conversationId, {"kind": "invalid", "data": float("nan")})

        with throws("strict JSON"):
            await session.commit(invalid, context)
        assert len(storage.commits) == commits

    async def test_replaces_task_records_completely(self):
        ts = openTestSession()
        session, storage = ts.session, ts.storage
        conversationId = await createConversation(session)
        taskId = await createTask(session, conversationId)

        async def replace(tx):
            task = await tx.task(taskId)
            tx.setTask(
                {
                    "id": task["id"],
                    "conversationId": task["conversationId"],
                    "kind": task["kind"],
                    "version": task["version"],
                    "input": task["input"],
                    "background": task["background"],
                    "abortRequested": task["abortRequested"],
                    "state": {"status": "running", "checkpoint": {"phase": "next", "step": 2}},
                    "memos": {"choice": "b"},
                }
            )

        await session.commitWith(replace, context)
        stored = await storage.task(taskId, context)
        assert stored["state"] == {"status": "running", "checkpoint": {"phase": "next", "step": 2}}
        assert stored["memos"] == {"choice": "b"}

    async def test_creates_a_task_and_then_its_document_in_one_transaction_without_read_after_write(self):
        ts = openTestSession()
        session, publications = ts.session, ts.publications
        conversationId = await createConversation(session)

        async def work(tx):
            createdTaskId = await tx.createTask(
                WorkTask, {"path": "a"}, {"ownership": {"kind": "conversation"}, "conversationId": conversationId}
            )
            # Validation uses the candidate task record, not a caller table read.
            (await tx.doc(ProgressDoc, createdTaskId))["lines"].push("created")
            (await tx.doc(StepDoc, createdTaskId, "one", None))["lines"].push("step")
            return createdTaskId

        taskId = await session.commit(work, context)
        assert await session.snapshot(ProgressDoc, taskId, context) == {"lines": ["created"]}
        await flush()
        publication = publications[-1]
        documents = documentChanges(publication)
        assert any(change["type"] == "task" and change["value"]["id"] == taskId for change in publication["changes"])
        assert len(documents) == 2
        # Task documents derive their conversation from the task record.
        for document in documents:
            assert document["conversationId"] == conversationId

        async def more(tx):
            await tx.createConversation({"ownership": {"kind": "ownerless"}})
            (await tx.doc(ProgressDoc, taskId))["lines"].push("committed task")

        await session.commit(more, context)
        await flush()
        assert documentChanges(publications[-1])[0]["conversationId"] == conversationId

    async def test_rejects_task_documents_after_a_terminal_candidate(self):
        session = openTestSession().session
        conversationId = await createConversation(session)
        taskId = await createTask(session, conversationId, True)

        async def work(tx):
            task = await tx.task(taskId)
            progress = await tx.doc(ProgressDoc, taskId)
            tx.setTask(terminal(task))
            with throws(f"Task {taskId} is terminal"):
                await tx.doc(ProgressDoc, taskId)
            with throws(f"Task {taskId} is terminal"):
                await tx.doc(StepDoc, taskId, "late", None)
            with throws("terminal candidate"):
                tx.setTask(task)
            progress["lines"].push("final")

        await session.commitWith(work, context)

        async def late(tx):
            await tx.doc(ProgressDoc, taskId)

        with throws(f"Task {taskId} is terminal"):
            await session.commit(late, context)

    async def test_retires_task_documents_at_terminal_settlement_including_documents_created_in_the_same_transaction(
        self,
    ):
        ts = openTestSession()
        session, storage, publications = ts.session, ts.storage, ts.publications
        conversationId = await createConversation(session)
        taskId = await createTask(session, conversationId, True)

        async def committed(tx):
            (await tx.doc(StepDoc, taskId, "committed", None))["lines"].push("x")

        await session.commit(committed, context)
        await flush()
        published = len(publications)

        async def settle(tx):
            task = await tx.task(taskId)
            (await tx.doc(StepDoc, taskId, "new", None))["lines"].push("created then retired")
            tx.setTask(terminal(task))

        await session.commitWith(settle, context)
        writes = storage.commits[-1]
        assert len(writes) == 5
        assert len([write for write in writes if write["type"] == "task"]) == 1
        creations = [write for write in writes if write["type"] == "document.create"]
        retirements = [write for write in writes if write["type"] == "document.retire"]
        assert len(creations) == 1
        assert len(retirements) == 3
        assert creations[0]["record"]["id"] in [write["id"] for write in retirements]
        await flush()
        assert len(publications) == published + 1
        publication = publications[-1]
        documents = documentChanges(publication)
        assert len([change for change in publication["changes"] if change["type"] == "task"]) == 1
        assert [document["value"] for document in documents] == [None, None, None]
        for document in documents:
            assert document["ops"] == []
            assert document["conversationId"] == conversationId
        assert await session.snapshot(ProgressDoc, taskId, context) is None
        assert await session.snapshot(StepDoc, taskId, "committed", context) is None
        alive = await storage.scanDocuments({"scope": {"kind": "task", "taskId": taskId}, "at": "current"}, 10, None, context)
        assert alive["items"] == []

        async def again(tx):
            tx.setTask(terminal(await tx.task(taskId)))

        with throws(f"Task {taskId} is already terminal"):
            await session.commitWith(again, context)

    async def test_validates_document_owners(self):
        session = openTestSession().session

        async def noTask(tx):
            await tx.doc(ProgressDoc, 4242)

        async def noConversation(tx):
            await tx.doc(NotesDoc, 4242)

        with throws("Task 4242 does not exist"):
            await session.commit(noTask, context)
        with throws("Conversation 4242 does not exist"):
            await session.commit(noConversation, context)

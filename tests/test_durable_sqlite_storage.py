"""pi durable's ``test/sqlite-storage.test.ts``."""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable.storage.sqlite import SqliteStorage
from misaka.durable.storage.sqlite.node import (
    NodeSqliteStorageOptions,
    openNodeSqliteStorage,
)
from misaka.durable.testing import createAssertions, createStorageConformance
from misaka.durable.types import ROOT_CONVERSATION_ID
from misaka.utils.js import jsonParse

context = BACKGROUND_CONTEXT


class Workspace:
    def __init__(self) -> None:
        self.directories: set[str] = set()
        self.storages: set[SqliteStorage] = set()

    async def createSqliteStorage(self, options: NodeSqliteStorageOptions | None = None) -> tuple[SqliteStorage, str]:
        directory = tempfile.mkdtemp(prefix="pi-durable-sqlite-")
        self.directories.add(directory)
        path = os.path.join(directory, "storage.sqlite")
        storage = await openNodeSqliteStorage(path, options)
        self.storages.add(storage)
        return storage, path

    async def reopen(self, path: str) -> SqliteStorage:
        storage = await openNodeSqliteStorage(path)
        self.storages.add(storage)
        return storage


@pytest.fixture
async def workspace() -> AsyncIterator[Workspace]:
    value = Workspace()
    yield value
    for storage in value.storages:
        await storage.close(context)
    for directory in value.directories:
        shutil.rmtree(directory, ignore_errors=True)


class ReopeningStorage:
    """Wraps a SQLite storage and reopens it from its file after every commit."""

    def __init__(self, current: SqliteStorage, path: str, options: NodeSqliteStorageOptions | None = None) -> None:
        self.current = current
        self.path = path
        self.options = options
        self.closed = False

    async def commit(self, writes, commitContext):
        if self.closed:
            return await self.current.commit(writes, commitContext)
        try:
            return await self.current.commit(writes, commitContext)
        finally:
            await self.current.close(context)
            self.current = await openNodeSqliteStorage(self.path, self.options)

    async def close(self, closeContext):
        if self.closed:
            return
        self.closed = True
        await self.current.close(closeContext)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.current, name)


async def withPlainStorage(use):
    value = Workspace()
    try:
        storage, _ = await value.createSqliteStorage()
        await use(storage)
    finally:
        for storage in value.storages:
            await storage.close(context)
        for directory in value.directories:
            shutil.rmtree(directory, ignore_errors=True)


async def withReopeningStorage(use):
    directory = tempfile.mkdtemp(prefix="pi-durable-sqlite-conformance-")
    path = os.path.join(directory, "storage.sqlite")
    created = await openNodeSqliteStorage(path)
    await created.close(context)
    storage = ReopeningStorage(await openNodeSqliteStorage(path), path)
    try:
        await use(storage)
    finally:
        await storage.close(context)
        shutil.rmtree(directory, ignore_errors=True)


plainCases = createStorageConformance({"assertions": createAssertions(), "withStorage": withPlainStorage})
reopenCases = createStorageConformance({"assertions": createAssertions(), "withStorage": withReopeningStorage})


@pytest.mark.parametrize("case", plainCases, ids=[case["name"] for case in plainCases])
async def test_sqlite_storage_conformance(case):
    await case["run"]()


@pytest.mark.parametrize("case", reopenCases, ids=[case["name"] for case in reopenCases])
async def test_sqlite_storage_across_reopen_conformance(case):
    await case["run"]()


def entry(id: int, conversationId: int, data: Any = None) -> dict[str, Any]:
    return {"id": id, "conversationId": conversationId, "kind": "message", **({} if data is None else {"data": data})}


async def createRoot(storage: Any) -> int:
    return await storage.commit([{"type": "conversation", "value": {"id": ROOT_CONVERSATION_ID}}], context)


def pendingTask(id: int) -> dict[str, Any]:
    return {
        "id": id,
        "conversationId": ROOT_CONVERSATION_ID,
        "kind": "test.task",
        "version": 1,
        "input": None,
        "state": {"status": "pending", "checkpoint": {"phase": "ready"}},
        "background": False,
        "abortRequested": False,
    }


@contextmanager
def rawDatabase(path: str, readOnly: bool = False) -> Iterator[sqlite3.Connection]:
    if readOnly:
        connection = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, autocommit=True)
    else:
        connection = sqlite3.connect(path, autocommit=True)
    try:
        yield connection
    finally:
        connection.close()


def scalar(db: sqlite3.Connection, sql: str) -> int:
    return db.execute(sql).fetchone()[0]  # type: ignore[no-any-return]


def revisionCount(path: str, documentId: int) -> int:
    with rawDatabase(path, readOnly=True) as db:
        return db.execute(  # type: ignore[no-any-return]
            "SELECT count(*) AS count FROM document_revisions WHERE document_id = ?", (documentId,)
        ).fetchone()[0]


async def test_persists_records_sequence_allocation_and_global_id_allocation_across_reopen(workspace):
    storage, path = await workspace.createSqliteStorage()
    assert await createRoot(storage) == 1
    entryId = await storage.mintId()
    assert await storage.commit([{"type": "entry", "value": entry(entryId, ROOT_CONVERSATION_ID)}], context) == 2
    await storage.close(context)
    workspace.storages.discard(storage)

    reopened = await workspace.reopen(path)
    assert await reopened.entry(entryId, context) == {"entry": entry(entryId, ROOT_CONVERSATION_ID), "commitSeq": 2}
    assert await reopened.mintId() == entryId + 1
    assert await reopened.commit([{"type": "task", "value": pendingTask(await reopened.mintId())}], context) == 3


async def test_namespaces_tables_and_indexes_with_a_prefix_across_reopen(workspace):
    directory = tempfile.mkdtemp(prefix="pi-durable-sqlite-")
    workspace.directories.add(directory)
    path = os.path.join(directory, "storage.sqlite")
    with rawDatabase(path) as host:
        host.execute("CREATE TABLE tasks (text TEXT)")
    storage = await openNodeSqliteStorage(path, {"prefix": "pi_"})
    assert await createRoot(storage) == 1
    await storage.close(context)

    reopened = await openNodeSqliteStorage(path, {"prefix": "pi_"})
    workspace.storages.add(reopened)
    assert await reopened.conversation(ROOT_CONVERSATION_ID, context) == {"id": ROOT_CONVERSATION_ID}
    with rawDatabase(path) as database:
        names = [
            str(row[0])
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'index') AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
    assert [name for name in names if not name.startswith("pi_")] == ["tasks"]
    assert "pi_tasks" in names


async def test_rejects_an_invalid_prefix_before_creating_the_database(workspace):
    directory = tempfile.mkdtemp(prefix="pi-durable-sqlite-")
    workspace.directories.add(directory)
    databaseDirectory = os.path.join(directory, "nested")
    with pytest.raises(Exception, match="Invalid SQLite schema prefix"):
        await openNodeSqliteStorage(os.path.join(databaseDirectory, "storage.sqlite"), {"prefix": "bad-prefix"})
    assert not os.path.exists(databaseDirectory)


async def test_rejects_persisted_metadata_corruption_on_reopen(workspace):
    storage, path = await workspace.createSqliteStorage()
    await storage.close(context)
    workspace.storages.discard(storage)
    with rawDatabase(path) as database:
        database.execute("DELETE FROM durable_metadata")
    with pytest.raises(Exception, match="Durable SQLite metadata is missing"):
        await openNodeSqliteStorage(path)


async def test_rejects_a_document_whose_required_base_is_missing(workspace):
    storage, path = await workspace.createSqliteStorage()
    await createRoot(storage)
    id = await storage.mintId()
    await storage.commit(
        [
            {
                "type": "document.create",
                "record": {"id": id, "kind": "corrupt", "scope": {"kind": "session"}},
                "content": {"kind": "base", "version": 1, "value": {"retained": True}},
            }
        ],
        context,
    )
    with rawDatabase(path) as database:
        database.execute("DELETE FROM document_revisions WHERE document_id = ?", (id,))
    with pytest.raises(Exception, match=f"Document {id} is missing a required base"):
        await storage.document(id, "current", context)


async def test_replays_detached_root_replacements_and_follow_up_edits_while_rejecting_corrupt_operations(workspace):
    storage, path = await workspace.createSqliteStorage()
    await createRoot(storage)
    id = await storage.mintId()
    await storage.commit(
        [
            {
                "type": "document.create",
                "record": {"id": id, "kind": "replay", "scope": {"kind": "session"}},
                "content": {"kind": "base", "version": 1, "value": {"nested": {"value": 1}, "rows": []}},
            }
        ],
        context,
    )
    await storage.commit(
        [
            {
                "type": "document.change",
                "id": id,
                "content": {
                    "kind": "delta",
                    "version": 1,
                    "ops": [["r", {"nested": {"value": 2}, "rows": [{"id": 1}]}]],
                },
            }
        ],
        context,
    )
    await storage.commit(
        [
            {
                "type": "document.change",
                "id": id,
                "content": {
                    "kind": "delta",
                    "version": 1,
                    "ops": [["s", ["nested", "value"], 3], ["p", ["rows"], 1, 0, [{"id": 2}]], ["m", ["rows"], [1, 0]]],
                },
            }
        ],
        context,
    )
    await storage.commit(
        [
            {
                "type": "document.change",
                "id": id,
                "content": {"kind": "delta", "version": 1, "ops": [["s", ["nested", "value"], 4]]},
            }
        ],
        context,
    )

    expected = {"nested": {"value": 4}, "rows": [{"id": 2}, {"id": 1}]}
    first = await storage.document(id, "current", context)
    assert first["value"] == expected
    first["value"]["nested"]["value"] = 99
    first["value"]["rows"][0]["id"] = 99
    assert (await storage.document(id, "current", context))["value"] == expected

    with rawDatabase(path) as database:
        database.execute(
            """UPDATE document_revisions SET content = ? WHERE document_id = ? AND seq =
					(SELECT max(seq) FROM document_revisions WHERE document_id = ?)""",
            ('[["unknown"]]', id, id),
        )
    with pytest.raises(Exception, match="unknown op verb"):
        await storage.document(id, "current", context)


async def test_rolls_sql_rows_and_sequence_allocation_back_as_one_transaction(workspace):
    storage, path = await workspace.createSqliteStorage()
    await createRoot(storage)
    transientId = await storage.mintId()
    transientTaskId = await storage.mintId()
    circular: dict[str, Any] = {}
    circular["self"] = circular
    with pytest.raises(Exception, match="circular structure"):
        await storage.commit(
            [
                {"type": "entry", "value": entry(transientId, ROOT_CONVERSATION_ID)},
                {"type": "task", "value": {**pendingTask(transientTaskId), "input": circular}},
            ],
            context,
        )
    assert await storage.entry(transientId, context) is None
    assert await storage.task(transientTaskId, context) is None
    committedId = await storage.mintId()
    assert await storage.commit([{"type": "entry", "value": entry(committedId, ROOT_CONVERSATION_ID)}], context) == 2

    with rawDatabase(path, readOnly=True) as db:
        assert scalar(db, "SELECT count(*) AS value FROM entries") == 1
        assert scalar(db, "SELECT next_seq AS value FROM durable_metadata WHERE singleton = 1") == 3


async def test_reconstructs_recent_and_ancient_rewindable_points_after_reopen(workspace):
    storage, path = await workspace.createSqliteStorage()
    await createRoot(storage)
    id = await storage.mintId()
    record = {
        "id": id,
        "kind": "history",
        "scope": {"kind": "conversation", "conversationId": ROOT_CONVERSATION_ID},
        "history": "rewindable",
        "fork": "asOf",
    }
    createdAt = await storage.commit(
        [{"type": "document.create", "record": record, "content": {"kind": "base", "version": 1, "value": {"count": 0}}}],
        context,
    )
    ancientAt = createdAt
    recentAt = createdAt
    for count in range(1, 41):
        recentAt = await storage.commit(
            [
                {
                    "type": "document.change",
                    "id": id,
                    "content": {"kind": "base", "version": 1, "value": {"count": count}}
                    if count == 20
                    else {"kind": "delta", "version": 1, "ops": [["s", ["count"], count]]},
                }
            ],
            context,
        )
        if count == 5:
            ancientAt = recentAt
    await storage.close(context)
    workspace.storages.discard(storage)
    reopened = await workspace.reopen(path)
    assert (await reopened.document(id, ancientAt, context))["value"] == {"count": 5}
    assert (await reopened.document(id, recentAt, context))["value"] == {"count": 40}


async def test_uses_indexes_for_exact_addresses_exact_scopes_entry_history_and_document_revision_tails(workspace):
    storage, path = await workspace.createSqliteStorage()
    await storage.close(context)
    workspace.storages.discard(storage)
    queries = [
        (
            """SELECT record FROM documents
						WHERE kind = ? AND scope_kind = ? AND owner_id = ? AND family = ? AND key_value = ?
						AND retired_at IS NULL ORDER BY created_at DESC LIMIT 1""",
            ("kind", "session", 0, 0, ""),
        ),
        (
            """SELECT record FROM documents
						WHERE kind = ? AND scope_kind = ? AND owner_id = ? AND family = ? AND key_value = ?
						AND created_at <= ? AND (retired_at IS NULL OR retired_at > ?)
						ORDER BY created_at DESC LIMIT 1""",
            ("kind", "conversation", 1, 0, "", 10, 10),
        ),
        (
            """SELECT record FROM documents
						WHERE scope_kind = ? AND owner_id = ? AND kind = ? AND id > ? ORDER BY id LIMIT ?""",
            ("task", 1, "kind", 0, 10),
        ),
        (
            """SELECT record FROM entries
						WHERE conversation_id = ? AND id <= ? ORDER BY id DESC LIMIT ?""",
            (1, 10, 10),
        ),
        (
            """SELECT record FROM entries
						WHERE conversation_id = ? AND head IS NOT NULL AND id <= ? ORDER BY id DESC LIMIT 1""",
            (1, 10),
        ),
        ("SELECT record FROM tasks WHERE status = ? AND id > ? ORDER BY id LIMIT ?", ("pending", 0, 10)),
        (
            """SELECT seq, kind, version, content FROM document_revisions
						WHERE document_id = ? AND kind = 'base' AND seq <= ? ORDER BY seq DESC LIMIT 1""",
            (1, 10),
        ),
        (
            """SELECT seq, kind, version, content FROM document_revisions
						WHERE document_id = ? AND seq > ? AND seq <= ? ORDER BY seq""",
            (1, 5, 10),
        ),
    ]
    with rawDatabase(path, readOnly=True) as db:
        details = [
            "\n".join(row[3] for row in db.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall())
            for sql, params in queries
        ]
    assert "documents_by_address" in details[0]
    assert "documents_by_address" in details[1]
    assert "documents_by_scope_kind" in details[2]
    assert "entries_by_conversation" in details[3]
    assert "entry_heads_by_conversation" in details[4]
    assert "tasks_by_status" in details[5]
    assert "document_revisions_by_kind" in details[6]
    assert "sqlite_autoindex_document_revisions_1" in details[7]
    for detail in details[:3]:
        assert "SCAN documents" not in detail
    assert "SCAN document_revisions" not in details[7]


async def test_reclaims_current_only_revisions_only_after_a_base_or_retirement(workspace):
    storage, path = await workspace.createSqliteStorage()
    await createRoot(storage)
    id = await storage.mintId()
    await storage.commit(
        [
            {
                "type": "document.create",
                "record": {"id": id, "kind": "latest", "scope": {"kind": "session"}},
                "content": {"kind": "base", "version": 1, "value": {"count": 0}},
            }
        ],
        context,
    )
    for count in range(1, 11):
        await storage.commit(
            [
                {
                    "type": "document.change",
                    "id": id,
                    "content": {"kind": "delta", "version": 1, "ops": [["s", ["count"], count]]},
                }
            ],
            context,
        )
    assert revisionCount(path, id) == 11
    with rawDatabase(path, readOnly=True) as revisions:
        row = revisions.execute(
            "SELECT content FROM document_revisions WHERE document_id = ? AND kind = 'delta' ORDER BY seq DESC LIMIT 1",
            (id,),
        ).fetchone()
    assert jsonParse(row[0]) == [["s", ["count"], 10]]
    await storage.commit(
        [{"type": "document.change", "id": id, "content": {"kind": "base", "version": 1, "value": {"count": 11}}}],
        context,
    )
    assert revisionCount(path, id) == 1
    await storage.commit(
        [{"type": "document.change", "id": id, "content": {"kind": "delta", "version": 1, "ops": [["r", {"count": 12}]]}}],
        context,
    )
    assert revisionCount(path, id) == 2
    await storage.commit([{"type": "document.retire", "id": id}], context)
    assert revisionCount(path, id) == 0


async def test_auto_checkpoints_wal_frames_and_truncates_the_wal_on_close(workspace):
    storage, path = await workspace.createSqliteStorage({"walAutoCheckpointPages": 1})
    await createRoot(storage)
    for index in range(20):
        await storage.commit(
            [
                {
                    "type": "entry",
                    "value": entry(await storage.mintId(), ROOT_CONVERSATION_ID, {"text": "x" * (32 * 1024), "index": index}),
                }
            ],
            context,
        )
    walPath = f"{path}-wal"
    assert os.stat(walPath).st_size < 512 * 1024
    with rawDatabase(path, readOnly=True) as observer:
        assert scalar(observer, "SELECT count(*) AS value FROM entries") == 20
        await storage.close(context)
        workspace.storages.discard(storage)
        assert os.stat(walPath).st_size == 0


async def test_reuses_pages_released_by_current_only_checkpoints(workspace):
    storage, path = await workspace.createSqliteStorage()
    await createRoot(storage)
    id = await storage.mintId()
    large = "x" * (512 * 1024)
    await storage.commit(
        [
            {
                "type": "document.create",
                "record": {"id": id, "kind": "reuse", "scope": {"kind": "session"}},
                "content": {"kind": "base", "version": 1, "value": {"text": large}},
            }
        ],
        context,
    )
    await storage.commit(
        [{"type": "document.change", "id": id, "content": {"kind": "base", "version": 1, "value": {"text": "small"}}}],
        context,
    )
    with rawDatabase(path, readOnly=True) as before:
        pagesAfterDelete = scalar(before, "SELECT page_count AS value FROM pragma_page_count() ")
        freeAfterDelete = scalar(before, "SELECT freelist_count AS value FROM pragma_freelist_count() ")
    assert freeAfterDelete > 0
    await storage.commit(
        [{"type": "document.change", "id": id, "content": {"kind": "base", "version": 1, "value": {"text": large}}}],
        context,
    )
    with rawDatabase(path, readOnly=True) as after:
        pagesAfterReuse = scalar(after, "SELECT page_count AS value FROM pragma_page_count() ")
        freeAfterReuse = scalar(after, "SELECT freelist_count AS value FROM pragma_freelist_count() ")
    assert pagesAfterReuse <= pagesAfterDelete + 2
    assert freeAfterReuse < freeAfterDelete


async def test_keeps_representative_row_and_document_storage_bounded(workspace):
    storage, path = await workspace.createSqliteStorage()
    await createRoot(storage)
    for index in range(100):
        await storage.commit(
            [
                {
                    "type": "entry",
                    "value": entry(await storage.mintId(), ROOT_CONVERSATION_ID, {"index": index, "text": "x" * 1_024}),
                }
            ],
            context,
        )
    documentId = await storage.mintId()
    await storage.commit(
        [
            {
                "type": "document.create",
                "record": {
                    "id": documentId,
                    "kind": "size.history",
                    "scope": {"kind": "conversation", "conversationId": ROOT_CONVERSATION_ID},
                    "history": "rewindable",
                    "fork": "asOf",
                },
                "content": {"kind": "base", "version": 1, "value": {"count": 0}},
            }
        ],
        context,
    )
    for count in range(1, 101):
        await storage.commit(
            [
                {
                    "type": "document.change",
                    "id": documentId,
                    "content": {"kind": "delta", "version": 1, "ops": [["s", ["count"], count]]},
                }
            ],
            context,
        )
    await storage.close(context)
    workspace.storages.discard(storage)
    assert os.stat(path).st_size < 1024 * 1024


async def test_stores_the_json_text_the_upstream_encoding_writes(workspace):
    """Record text, indexed identities and the commit sequence match what pi's storage writes into its tables."""
    storage, path = await workspace.createSqliteStorage()
    await createRoot(storage)
    taskId = await storage.mintId()
    await storage.commit(
        [{"type": "task", "value": {**pendingTask(taskId), "kind": "lone \ud800 surrogate"}}],
        context,
    )
    await storage.close(context)
    workspace.storages.discard(storage)
    with rawDatabase(path, readOnly=True) as db:
        kind, record = db.execute("SELECT kind, record FROM tasks WHERE id = ?", (taskId,)).fetchone()
    assert kind == '"lone \\ud800 surrogate"'
    assert jsonParse(record)["kind"] == "lone \ud800 surrogate"
    reopened = await workspace.reopen(path)
    found = await reopened.scanTasks({"kind": "lone \ud800 surrogate"}, 10, None, context)
    assert [task["id"] for task in found["items"]] == [taskId]


async def test_rejects_a_copy_whose_source_changes_in_the_same_batch(workspace):
    storage, _ = await workspace.createSqliteStorage()
    await createRoot(storage)
    source = await storage.mintId()
    copied = await storage.mintId()
    scope = {"kind": "conversation", "conversationId": ROOT_CONVERSATION_ID}
    record = {"id": source, "kind": "fork", "scope": scope, "history": "rewindable", "fork": "asOf"}
    createdAt = await storage.commit(
        [{"type": "document.create", "record": record, "content": {"kind": "base", "version": 1, "value": {"count": 0}}}],
        context,
    )
    with pytest.raises(RuntimeError, match="source is changed in the copy batch"):
        await storage.commit(
            [
                {"type": "document.copy", "record": {**record, "id": copied}, "source": {"id": source, "at": createdAt}},
                {
                    "type": "document.change",
                    "id": source,
                    "content": {"kind": "base", "version": 1, "value": {"count": 1}},
                },
            ],
            context,
        )
    assert await storage.document(copied, "current", context) is None

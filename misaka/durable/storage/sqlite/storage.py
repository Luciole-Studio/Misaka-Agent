"""Portable SQLite implementation of the Pico storage contract.

Translated from pi's ``packages/durable/src/storage/sqlite/storage.ts`` (v1.3.0). Records are plain ``dict`` objects stored as
JSON text next to the columns that index them. Public async methods are ``@eager``: calling them starts the body, as
calling a JavaScript async function does, and returns the future of the result.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, TypedDict

from misaka.chord.context import Context
from misaka.chord.delta import apply
from misaka.utils.js import (
    UNDEFINED,
    eager,
    jsIsInteger,
    jsonParse,
    jsonStringify,
    startTask,
)

from ...errors import StorageRequestError
from ...ids import idFromNumber, seqFromNumber
from ...types import Cursor, DocumentPoint, Page, ScanOrder, Seq
from ..scan import ScanStart, nextCursor, scanStart
from .database import SqliteDatabase, SqliteExecutor, SqliteValue
from .migrations import applySqliteMigrationsForSchema
from .schema import SqliteSchema, createSqliteSchema

_MAX_SAFE_INTEGER = 2**53 - 1
_INFINITY = float("inf")


class SqliteStorageOptions(TypedDict, total=False):
    prefix: str
    """Prefix for every table and index in the durable schema. It must be a SQLite identifier and must not use SQLite's
    reserved ``sqlite_`` prefix. Existing data is not migrated when this changes."""


def parseJson(value: str) -> Any:
    return jsonParse(value)


def encodeJson(value: Any) -> str:
    return jsonStringify(value)


def encodeIndexedString(value: str) -> str:
    """Some SQLite bindings replace lone UTF-16 surrogates. JSON encoding keeps indexed identities lossless."""
    return jsonStringify(value)


def cursorId(cursor: Cursor | None) -> int | None:
    # ``cursor?.after === undefined``: a ``null`` is not an absent cursor.
    after = UNDEFINED if cursor is None else cursor.get("after", UNDEFINED)
    if after is UNDEFINED:
        return None
    if not jsIsInteger(after) or abs(after) > _MAX_SAFE_INTEGER:
        raise StorageRequestError("Invalid storage cursor")
    return idFromNumber(after)


def page(values: Sequence[dict[str, Any]], limit: int, order: ScanOrder = "ascending") -> Page:
    items = list(values[:limit])
    if len(values) <= limit:
        return {"items": items}
    return {"items": items, "next": nextCursor(items[-1]["id"], order)}


def scanSql(start: ScanStart) -> dict[str, Any]:
    """The ID condition, its parameter, and the ``ORDER BY`` direction of a table scan."""
    if start["order"] == "ascending":
        return {"clause": "id > ?", "param": -1 if start["after"] is None else start["after"], "direction": "ASC"}
    return {
        "clause": "id < ?",
        "param": _MAX_SAFE_INTEGER if start["after"] is None else start["after"],
        "direction": "DESC",
    }


def scopeColumns(scope: dict[str, Any]) -> dict[str, Any]:
    if scope["kind"] == "session":
        return {"scopeKind": "session", "ownerId": 0}
    if scope["kind"] == "conversation":
        return {"scopeKind": "conversation", "ownerId": scope["conversationId"]}
    return {"scopeKind": "task", "ownerId": scope["taskId"]}


def addressParts(address: dict[str, Any]) -> dict[str, Any]:
    scope = scopeColumns(address["scope"])
    key = address.get("key")
    return {
        "kind": encodeIndexedString(address["kind"]),
        **scope,
        "family": 0 if key is None else 1,
        "keyValue": encodeIndexedString("" if key is None else key),
    }


def addressKey(address: dict[str, Any]) -> str:
    parts = addressParts(address)
    return jsonStringify([parts["kind"], parts["scopeKind"], parts["ownerId"], parts["family"], parts["keyValue"]])


def isAliveAt(record: dict[str, Any], at: DocumentPoint) -> bool:
    if at == "current":
        return record.get("retiredAt") is None
    return record["createdAt"] <= at and (record.get("retiredAt") is None or at < record["retiredAt"])


def isCurrentOnly(record: dict[str, Any]) -> bool:
    return record["scope"]["kind"] != "conversation" or record["history"] == "latest"


def writeId(write: dict[str, Any]) -> int | None:
    if write["type"] in ("conversation", "entry", "task", "submission"):
        return write["value"]["id"]
    if write["type"] in ("document.create", "document.copy"):
        return write["record"]["id"]
    return None


class SqliteStorage:
    """Portable SQLite implementation of the Pico storage contract. Create it with ``SqliteStorage.open``."""

    def __init__(self, db: SqliteDatabase, schema: SqliteSchema, nextId: int) -> None:
        self._db = db
        self._schema = schema
        self._nextId = nextId
        self._closed = False
        self._closing: asyncio.Future[None] | None = None
        self._closeWaiter: asyncio.Future[None] | None = None
        self._admittedReads = 0
        self._readsDrained: asyncio.Future[None] | None = None

    @staticmethod
    @eager
    async def open(db: SqliteDatabase, options: SqliteStorageOptions | None = None) -> SqliteStorage:
        """Initialize storage over an owned SQLite database facade."""
        try:
            schema = createSqliteSchema(None if options is None else options.get("prefix"))
            await applySqliteMigrationsForSchema(db, schema)
            metadata = await db.get(f"SELECT next_id, next_seq FROM {schema['tables']['durableMetadata']} WHERE singleton = 1")
            if metadata is None:
                raise RuntimeError("Durable SQLite metadata is missing")
            return SqliteStorage(db, schema, int(metadata["next_id"]))
        except BaseException:
            with contextlib.suppress(Exception):  # Preserve the initialization failure.
                await db.close()
            raise

    @eager
    async def commit(self, writes: Sequence[dict[str, Any]], _context: Context) -> Seq:
        self._assertOpen()
        documentActions = self._prepareDocumentActions(writes)
        candidateNextId = self._candidateNextId(writes)

        async def transact(transaction: SqliteExecutor) -> Seq:
            metadata = await transaction.get(f"SELECT next_id, next_seq FROM {self._schema['tables']['durableMetadata']} WHERE singleton = 1")
            if metadata is None:
                raise RuntimeError("Durable SQLite metadata is missing")
            committedSeq = seqFromNumber(metadata["next_seq"])
            await self._checkGlobalIds(transaction, writes)
            await self._checkDocumentActions(transaction, documentActions)
            for write in writes:
                await self._applyTableWrite(transaction, write, committedSeq)
            await self._applyDocumentActions(transaction, documentActions, committedSeq)
            await transaction.run(
                f"UPDATE {self._schema['tables']['durableMetadata']} SET next_id = ?, next_seq = ? WHERE singleton = 1",
                str(max(int(metadata["next_id"]), candidateNextId)),
                committedSeq + 1,
            )
            return committedSeq

        seq = await self._db.transaction(transact)
        self._nextId = max(self._nextId, candidateNextId)
        return seq

    @eager
    async def mintId(self) -> int:
        self._assertOpen()
        if self._nextId > _MAX_SAFE_INTEGER:
            raise RuntimeError("ID space is exhausted")
        id = self._nextId
        self._nextId += 1
        return idFromNumber(id)

    @eager
    async def conversation(self, id: int, _context: Context) -> dict[str, Any] | None:
        self._assertOpen()
        row = await self._db.get(f"SELECT record FROM {self._schema['tables']['conversations']} WHERE id = ?", id)
        return None if row is None else parseJson(row["record"])

    @eager
    async def scanConversations(self, query: dict[str, Any], limit: int, cursor: Cursor | None, _context: Context) -> Page:
        self._assertOpen()
        start = scanStart(query.get("order", UNDEFINED), cursor, "ascending")
        scan = scanSql(start)
        clauses = [scan["clause"]]
        params: list[SqliteValue] = [scan["param"]]
        if query.get("ownerConversationId") is not None:
            clauses.append("owner_conversation_id = ?")
            params.append(query["ownerConversationId"])
        if query.get("ownerTaskId") is not None:
            clauses.append("owner_task_id = ?")
            params.append(query["ownerTaskId"])
        params.append(limit + 1)
        rows = await self._db.all(
            f"SELECT record FROM {self._schema['tables']['conversations']} WHERE {' AND '.join(clauses)} ORDER BY id {scan['direction']} LIMIT ?", *params
        )
        return page([parseJson(row["record"]) for row in rows], limit, start["order"])

    def entry(self, *args: Any) -> asyncio.Future[dict[str, Any] | None]:
        """``entry(id, context)`` or ``entry(conversationId, id, context)``; resolves ``{"entry": ..., "commitSeq":
        ...}``."""
        return self._admitRead(lambda: self._readEntry(*args))

    def findLatestHeadMarker(
        self, conversationId: int, atOrBeforeEntryId: int | None, _context: Context
    ) -> asyncio.Future[dict[str, Any] | None]:
        return self._admitRead(lambda: self._readLatestHeadMarker(conversationId, atOrBeforeEntryId))

    def scanEntries(self, query: dict[str, Any], limit: int, cursor: Cursor | None, _context: Context) -> asyncio.Future[Page]:
        return self._admitRead(lambda: self._readEntries(query, limit, cursor))

    async def _readEntry(self, *args: Any) -> dict[str, Any] | None:
        conversation: dict[str, Any] | None = None
        if len(args) == 2:
            id: int | None = idFromNumber(args[0])
        else:
            id = idFromNumber(args[1]) if isinstance(args[1], int) and not isinstance(args[1], bool) else None
        if id is None:
            raise StorageRequestError("Storage.entry() requires an entry ID")
        if len(args) != 2:
            conversationId = idFromNumber(args[0])
            conversation = await self._readConversation(conversationId)
            if conversation is None:
                raise StorageRequestError(f"Unknown conversation: {conversationId}")
        row = await self._db.get(f"SELECT record, commit_seq FROM {self._schema['tables']['entries']} WHERE id = ?", id)
        if row is None:
            return None
        entry = parseJson(row["record"])
        if conversation is not None:
            upperEntryId = _INFINITY
            while conversation["id"] != entry["conversationId"]:
                if conversation.get("parent") is None:
                    return None
                upperEntryId = min(upperEntryId, conversation["parent"]["at"])
                conversation = await self._readConversation(conversation["parent"]["conversationId"])
                assert conversation is not None
            if entry["id"] > upperEntryId:
                return None
        return {"entry": entry, "commitSeq": seqFromNumber(row["commit_seq"])}

    async def _readLatestHeadMarker(self, conversationId: int, atOrBeforeEntryId: int | None) -> dict[str, Any] | None:
        conversation = await self._readConversation(conversationId)
        if conversation is None:
            raise StorageRequestError(f"Unknown conversation: {conversationId}")
        upper = atOrBeforeEntryId
        while True:
            if upper is None:
                row = await self._db.get(
                    f"SELECT record FROM {self._schema['tables']['entries']} WHERE conversation_id = ? AND head IS NOT NULL ORDER BY id DESC LIMIT 1",
                    conversation["id"],
                )
            else:
                row = await self._db.get(
                    f"SELECT record FROM {self._schema['tables']['entries']} WHERE conversation_id = ? AND head IS NOT NULL AND id <= ? ORDER BY id DESC LIMIT 1",
                    conversation["id"],
                    upper,
                )
            if row is not None:
                return parseJson(row["record"])
            if conversation.get("parent") is None:
                return None
            upper = conversation["parent"]["at"] if upper is None else min(upper, conversation["parent"]["at"])
            conversation = await self._readConversation(conversation["parent"]["conversationId"])
            assert conversation is not None

    async def _readEntries(self, query: dict[str, Any], limit: int, cursor: Cursor | None) -> Page:
        start = scanStart(query.get("order", UNDEFINED), cursor, "descending")
        order, after = start["order"], start["after"]
        if order == "ascending":
            return await self._readEntriesAscending(query, limit, after)
        conversation = await self._readConversation(query["conversationId"])
        if conversation is None:
            raise StorageRequestError(f"Unknown conversation: {query['conversationId']}")
        upper: int | None = query.get("maxEntryId")
        if after is not None:
            upper = min(_MAX_SAFE_INTEGER if upper is None else upper, after - 1)
        values: list[dict[str, Any]] = []
        while True:
            clauses = ["conversation_id = ?"]
            params: list[SqliteValue] = [conversation["id"]]
            if query.get("minEntryId") is not None:
                clauses.append("id >= ?")
                params.append(query["minEntryId"])
            if upper is not None:
                clauses.append("id <= ?")
                params.append(upper)
            params.append(limit + 1 - len(values))
            rows = await self._db.all(f"SELECT record FROM {self._schema['tables']['entries']} WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?", *params)
            values.extend(parseJson(row["record"]) for row in rows)
            if len(values) > limit or conversation.get("parent") is None:
                break
            upper = conversation["parent"]["at"] if upper is None else min(upper, conversation["parent"]["at"])
            if query.get("minEntryId") is not None and upper < query["minEntryId"]:
                break
            conversation = await self._readConversation(conversation["parent"]["conversationId"])
            assert conversation is not None
        return page(values, limit, "descending")

    async def _readEntriesAscending(self, query: dict[str, Any], limit: int, after: int | None) -> Page:
        """Oldest first: the fork chain's segments from the root conversation forward, each up to its fork point."""
        segments: list[tuple[int, int | None]] = []
        conversation = await self._readConversation(query["conversationId"])
        if conversation is None:
            raise StorageRequestError(f"Unknown conversation: {query['conversationId']}")
        upper: int | None = query.get("maxEntryId")
        while True:
            segments.append((conversation["id"], upper))
            if conversation.get("parent") is None:
                break
            upper = conversation["parent"]["at"] if upper is None else min(upper, conversation["parent"]["at"])
            if query.get("minEntryId") is not None and upper < query["minEntryId"]:
                break
            conversation = await self._readConversation(conversation["parent"]["conversationId"])
            assert conversation is not None
        lower: int | None = query.get("minEntryId")
        if after is not None:
            lower = max(after + 1 if lower is None else lower, after + 1)
        values: list[dict[str, Any]] = []
        for segmentId, segmentUpper in reversed(segments):
            clauses = ["conversation_id = ?"]
            params: list[SqliteValue] = [segmentId]
            if lower is not None:
                clauses.append("id >= ?")
                params.append(lower)
            if segmentUpper is not None:
                clauses.append("id <= ?")
                params.append(segmentUpper)
            params.append(limit + 1 - len(values))
            rows = await self._db.all(f"SELECT record FROM {self._schema['tables']['entries']} WHERE {' AND '.join(clauses)} ORDER BY id ASC LIMIT ?", *params)
            values.extend(parseJson(row["record"]) for row in rows)
            if len(values) > limit:
                break
        return page(values, limit, "ascending")

    @eager
    async def task(self, id: int, _context: Context) -> dict[str, Any] | None:
        self._assertOpen()
        row = await self._db.get(f"SELECT record FROM {self._schema['tables']['tasks']} WHERE id = ?", id)
        return None if row is None else parseJson(row["record"])

    @eager
    async def scanTasks(self, query: dict[str, Any], limit: int, cursor: Cursor | None, _context: Context) -> Page:
        self._assertOpen()
        start = scanStart(query.get("order", UNDEFINED), cursor, "ascending")
        scan = scanSql(start)
        clauses = [scan["clause"]]
        params: list[SqliteValue] = [scan["param"]]
        if query.get("conversationId") is not None:
            clauses.append("conversation_id = ?")
            params.append(query["conversationId"])
        if query.get("kind") is not None:
            clauses.append("kind = ?")
            params.append(encodeIndexedString(query["kind"]))
        if query.get("status") is not None:
            clauses.append("status = ?")
            params.append(query["status"])
        if query.get("abortRequested") is not None:
            clauses.append("abort_requested = ?")
            params.append(1 if query["abortRequested"] else 0)
        if query.get("background") is not None:
            clauses.append("background = ?")
            params.append(1 if query["background"] else 0)
        params.append(limit + 1)
        rows = await self._db.all(
            f"SELECT record FROM {self._schema['tables']['tasks']} WHERE {' AND '.join(clauses)} ORDER BY id {scan['direction']} LIMIT ?", *params
        )
        return page([parseJson(row["record"]) for row in rows], limit, start["order"])

    @eager
    async def submission(self, id: int, _context: Context) -> dict[str, Any] | None:
        self._assertOpen()
        row = await self._db.get(f"SELECT record FROM {self._schema['tables']['submissions']} WHERE id = ?", id)
        return None if row is None else parseJson(row["record"])

    @eager
    async def scanSubmissions(self, query: dict[str, Any], limit: int, cursor: Cursor | None, _context: Context) -> Page:
        self._assertOpen()
        start = scanStart(query.get("order", UNDEFINED), cursor, "ascending")
        scan = scanSql(start)
        clauses = [scan["clause"]]
        params: list[SqliteValue] = [scan["param"]]
        if query.get("conversationId") is not None:
            clauses.append("conversation_id = ?")
            params.append(query["conversationId"])
        if query.get("status") is not None:
            clauses.append("status = ?")
            params.append(query["status"])
        params.append(limit + 1)
        rows = await self._db.all(
            f"SELECT record FROM {self._schema['tables']['submissions']} WHERE {' AND '.join(clauses)} ORDER BY id {scan['direction']} LIMIT ?", *params
        )
        return page([parseJson(row["record"]) for row in rows], limit, start["order"])

    @eager
    async def submissionByRequest(self, conversationId: int, requestId: str, _context: Context) -> dict[str, Any] | None:
        self._assertOpen()
        row = await self._db.get(
            f"SELECT record FROM {self._schema['tables']['submissions']} WHERE conversation_id = ? AND request_id = ?",
            conversationId,
            encodeIndexedString(requestId),
        )
        return None if row is None else parseJson(row["record"])

    @eager
    async def findDocument(self, address: dict[str, Any], at: DocumentPoint, _context: Context) -> dict[str, Any] | None:
        self._assertOpen()
        parts = addressParts(address)
        if at == "current":
            sql = f"""SELECT record FROM {self._schema['tables']['documents']}
					WHERE kind = ? AND scope_kind = ? AND owner_id = ? AND family = ? AND key_value = ?
					AND retired_at IS NULL ORDER BY created_at DESC LIMIT 1"""
        else:
            sql = f"""SELECT record FROM {self._schema['tables']['documents']}
					WHERE kind = ? AND scope_kind = ? AND owner_id = ? AND family = ? AND key_value = ?
					AND created_at <= ? AND (retired_at IS NULL OR retired_at > ?)
					ORDER BY created_at DESC LIMIT 1"""
        params: list[SqliteValue] = [parts["kind"], parts["scopeKind"], parts["ownerId"], parts["family"], parts["keyValue"]]
        if at != "current":
            params.extend([at, at])
        row = await self._db.get(sql, *params)
        return None if row is None else parseJson(row["record"])

    @eager
    async def document(self, id: int, at: DocumentPoint, _context: Context) -> dict[str, Any] | None:
        self._assertOpen()
        # The record and revision queries must observe one committed state; a commit between them can replace the base.
        return await self._db.transaction(lambda transaction: self._materializeDocument(transaction, id, at))

    @eager
    async def scanDocuments(self, query: dict[str, Any], limit: int, cursor: Cursor | None, _context: Context) -> Page:
        self._assertOpen()
        scope = scopeColumns(query["scope"])
        clauses = ["scope_kind = ?", "owner_id = ?", "id > ?"]
        after = cursorId(cursor)
        params: list[SqliteValue] = [scope["scopeKind"], scope["ownerId"], -1 if after is None else after]
        if query.get("kind") is not None:
            clauses.append("kind = ?")
            params.append(encodeIndexedString(query["kind"]))
        if query["at"] == "current":
            clauses.append("retired_at IS NULL")
        else:
            clauses.extend(["created_at <= ?", "(retired_at IS NULL OR retired_at > ?)"])
            params.extend([query["at"], query["at"]])
        params.append(limit + 1)
        rows = await self._db.all(f"SELECT record FROM {self._schema['tables']['documents']} WHERE {' AND '.join(clauses)} ORDER BY id LIMIT ?", *params)
        return page([parseJson(row["record"]) for row in rows], limit)

    def close(self, _context: Context) -> asyncio.Future[None]:
        if self._closing is None:
            self._closed = True
            self._closing = startTask(self._closeDatabase())
        # Shielded: a caller that gives up waiting must not cancel the close every other caller shares. A repeated close
        # returns the same future until a caller cancels it.
        if self._closeWaiter is None or self._closeWaiter.cancelled():
            self._closeWaiter = asyncio.shield(self._closing)
        return self._closeWaiter

    async def _closeDatabase(self) -> None:
        if self._admittedReads > 0:
            drained: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._readsDrained = drained
            await drained
        await self._db.close()

    @eager
    async def _admitRead(self, read: Callable[[], Awaitable[Any]]) -> Any:
        """Run a read that issues several queries. Close waits for admitted reads, so their later queries never reach a
        closed database. Single-query reads and transactions are already ordered before close by the database."""
        self._assertOpen()
        self._admittedReads += 1
        try:
            return await read()
        finally:
            self._admittedReads -= 1
            if self._admittedReads == 0 and self._readsDrained is not None and not self._readsDrained.done():
                self._readsDrained.set_result(None)

    async def _readConversation(self, id: int) -> dict[str, Any] | None:
        row = await self._db.get(f"SELECT record FROM {self._schema['tables']['conversations']} WHERE id = ?", id)
        return None if row is None else parseJson(row["record"])

    async def _materializeDocument(self, executor: SqliteExecutor, id: int, at: DocumentPoint) -> dict[str, Any] | None:
        row = await executor.get(f"SELECT record FROM {self._schema['tables']['documents']} WHERE id = ?", id)
        if row is None:
            return None
        record = parseJson(row["record"])
        if at != "current" and isCurrentOnly(record):
            raise StorageRequestError(f"Document {id} does not retain historical content")
        if not isAliveAt(record, at):
            return None
        upper = _MAX_SAFE_INTEGER if at == "current" else at
        base = await executor.get(
            f"""SELECT seq, kind, version, content FROM {self._schema['tables']['documentRevisions']}
				WHERE document_id = ? AND kind = 'base' AND seq <= ? ORDER BY seq DESC LIMIT 1""",
            id,
            upper,
        )
        if base is None:
            raise RuntimeError(f"Document {id} is missing a required base")
        value = parseJson(base["content"])
        tail = await executor.all(
            f"""SELECT seq, kind, version, content FROM {self._schema['tables']['documentRevisions']}
				WHERE document_id = ? AND seq > ? AND seq <= ? ORDER BY seq""",
            id,
            base["seq"],
            upper,
        )
        for revision in tail:
            if revision["kind"] != "delta" or revision["version"] != base["version"]:
                raise RuntimeError(f"Document {id} crosses a stored version boundary without a base")
            value = apply(value, parseJson(revision["content"]))
        return {"record": record, "version": base["version"], "value": value, "deltasSinceBase": len(tail)}

    def _candidateNextId(self, writes: Sequence[dict[str, Any]]) -> int:
        nextId = self._nextId
        for write in writes:
            id = writeId(write)
            if id is not None:
                nextId = max(nextId, id + 1)
        return nextId

    async def _checkGlobalIds(self, executor: SqliteExecutor, writes: Sequence[dict[str, Any]]) -> None:
        claimed: dict[int, str] = {}
        for write in writes:
            if write["type"] in ("document.change", "document.retire"):
                continue
            document = write["type"] in ("document.create", "document.copy")
            table = "document" if document else write["type"]
            id = write["record"]["id"] if document else write["value"]["id"]
            row = await executor.get(f"SELECT record_type FROM {self._schema['tables']['recordIds']} WHERE id = ?", id)
            existing = None if row is None else row["record_type"]
            earlier = claimed.get(id)
            if table in ("conversation", "entry", "document"):
                if existing is not None:
                    raise RuntimeError(f"ID {id} already belongs to {existing}")
                if earlier is not None:
                    raise RuntimeError(f"ID {id} is written more than once")
            else:
                if existing is not None and existing != table:
                    raise RuntimeError(f"ID {id} already belongs to {existing}")
                if earlier is not None and earlier != table:
                    raise RuntimeError(f"ID {id} is written as two record types")
            claimed[id] = table

    @staticmethod
    def _prepareDocumentActions(writes: Sequence[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        actions: dict[int, dict[str, Any]] = {}
        for write in writes:
            kind = write["type"]
            if kind not in ("document.create", "document.copy", "document.change", "document.retire"):
                continue
            id = write["record"]["id"] if kind in ("document.create", "document.copy") else write["id"]
            action = actions.get(id)
            if action is None:
                action = {"retire": False}
                actions[id] = action
            if kind == "document.create":
                if action.get("create") is not None or action.get("content") is not None or action.get("copy") is not None:
                    raise RuntimeError(f"Document {id} has more than one content command")
                action["create"] = write["record"]
                action["content"] = write["content"]
            elif kind == "document.copy":
                if action.get("create") is not None or action.get("content") is not None or action.get("copy") is not None:
                    raise RuntimeError(f"Document {id} has more than one content command")
                action["create"] = write["record"]
                action["copy"] = write["source"]
            elif kind == "document.change":
                if action.get("content") is not None or action.get("copy") is not None:
                    raise RuntimeError(f"Document {id} has more than one content command")
                action["content"] = write["content"]
            else:
                if action["retire"]:
                    raise RuntimeError(f"Document {id} is retired more than once")
                action["retire"] = True
        return actions

    async def _checkDocumentActions(self, executor: SqliteExecutor, actions: dict[int, dict[str, Any]]) -> None:
        liveCounts: dict[str, int] = {}
        for id, action in actions.items():
            copy = action.get("copy")
            if copy is not None and copy["id"] in actions:
                raise RuntimeError(f"Document copy {id} source is changed in the copy batch")
            row = await executor.get(f"SELECT record FROM {self._schema['tables']['documents']} WHERE id = ?", id)
            existing = None if row is None else parseJson(row["record"])
            create = action.get("create")
            if create is None and existing is None:
                raise RuntimeError(f"Unknown document: {id}")
            if create is not None and existing is not None:
                raise RuntimeError(f"Document {id} already exists")
            if existing is not None and existing.get("retiredAt") is not None:
                raise RuntimeError(f"Document {id} is retired")
            content = action.get("content")
            if content is not None and content["kind"] == "delta":
                previous = await executor.get(
                    f"SELECT version FROM {self._schema['tables']['documentRevisions']} WHERE document_id = ? ORDER BY seq DESC LIMIT 1", id
                )
                if previous is None:
                    raise RuntimeError(f"Document {id} delta has no base")
                if previous["version"] != content["version"]:
                    raise RuntimeError(f"Document {id} version transition requires a base")
            record = create if create is not None else existing
            assert record is not None
            key = addressKey(record)
            live = liveCounts.get(key)
            if live is None:
                live = 0 if await self._currentDocumentId(executor, record) is None else 1
            if action["retire"] and existing is not None:
                live -= 1
            if create is not None and not action["retire"]:
                live += 1
            liveCounts[key] = live
        for live in liveCounts.values():
            if live > 1:
                raise RuntimeError("Document address already has a current incarnation")

    async def _currentDocumentId(self, executor: SqliteExecutor, address: dict[str, Any]) -> int | None:
        parts = addressParts(address)
        row = await executor.get(
            f"""SELECT id FROM {self._schema['tables']['documents']}
				WHERE kind = ? AND scope_kind = ? AND owner_id = ? AND family = ? AND key_value = ? AND retired_at IS NULL
				LIMIT 1""",
            parts["kind"],
            parts["scopeKind"],
            parts["ownerId"],
            parts["family"],
            parts["keyValue"],
        )
        return None if row is None else idFromNumber(row["id"])

    async def _applyTableWrite(self, executor: SqliteExecutor, write: dict[str, Any], seq: Seq) -> None:
        kind = write["type"]
        if kind == "conversation":
            value = write["value"]
            await self._claimId(executor, value["id"], "conversation")
            owner = value.get("owner")
            await executor.run(
                f"INSERT INTO {self._schema['tables']['conversations']} (id, owner_conversation_id, owner_task_id, record) VALUES (?, ?, ?, ?)",
                value["id"],
                None if owner is None else owner["conversationId"],
                None if owner is None else owner["taskId"],
                encodeJson(value),
            )
        elif kind == "entry":
            value = write["value"]
            await self._claimId(executor, value["id"], "entry")
            await executor.run(
                f"INSERT INTO {self._schema['tables']['entries']} (id, conversation_id, head, commit_seq, record) VALUES (?, ?, ?, ?, ?)",
                value["id"],
                value["conversationId"],
                value.get("head"),
                seq,
                encodeJson(value),
            )
        elif kind == "task":
            value = write["value"]
            await self._claimId(executor, value["id"], "task")
            await executor.run(
                f"""INSERT INTO {self._schema['tables']['tasks']} (id, conversation_id, kind, status, abort_requested, background, record)
						VALUES (?, ?, ?, ?, ?, ?, ?)
						ON CONFLICT(id) DO UPDATE SET conversation_id = excluded.conversation_id, kind = excluded.kind,
						status = excluded.status, abort_requested = excluded.abort_requested,
						background = excluded.background, record = excluded.record""",
                value["id"],
                value["conversationId"],
                encodeIndexedString(value["kind"]),
                value["state"]["status"],
                1 if value["abortRequested"] else 0,
                1 if value["background"] else 0,
                encodeJson(value),
            )
        elif kind == "submission":
            value = write["value"]
            await self._claimId(executor, value["id"], "submission")
            await executor.run(
                f"""INSERT INTO {self._schema['tables']['submissions']} (id, conversation_id, request_id, status, record) VALUES (?, ?, ?, ?, ?)
						ON CONFLICT(id) DO UPDATE SET conversation_id = excluded.conversation_id,
						request_id = excluded.request_id, status = excluded.status, record = excluded.record""",
                value["id"],
                value["conversationId"],
                None if value.get("requestId") is None else encodeIndexedString(value["requestId"]),
                value["status"],
                encodeJson(value),
            )

    async def _claimId(self, executor: SqliteExecutor, id: int, table: str) -> None:
        await executor.run(f"INSERT OR IGNORE INTO {self._schema['tables']['recordIds']} (id, record_type) VALUES (?, ?)", id, table)

    async def _applyDocumentActions(self, executor: SqliteExecutor, actions: dict[int, dict[str, Any]], seq: Seq) -> None:
        for id, action in actions.items():
            content = action.get("content")
            copy = action.get("copy")
            if copy is not None:
                try:
                    stored = await self._materializeDocument(executor, copy["id"], copy["at"])
                    if stored is None:
                        raise RuntimeError(f"Fork source document {copy['id']} cannot be read")
                    create = action["create"]
                    if (
                        stored["record"]["scope"]["kind"] != "conversation"
                        or create["scope"]["kind"] != "conversation"
                        or stored["record"]["kind"] != create["kind"]
                        or stored["record"].get("key") != create.get("key")
                        or stored["record"]["history"] != create["history"]
                        or stored["record"]["fork"] != create["fork"]
                    ):
                        raise RuntimeError(f"Fork source document {copy['id']} does not match the copied record")
                    content = {"kind": "base", "version": stored["version"], "value": stored["value"]}
                except Exception as error:
                    raise RuntimeError(f"Document copy {id} was rejected") from error
            create = action.get("create")
            if create is not None:
                record = {**create, "createdAt": seq, **({"retiredAt": seq} if action["retire"] else {})}
                parts = addressParts(record)
                await self._claimId(executor, id, "document")
                await executor.run(
                    f"""INSERT INTO {self._schema['tables']['documents']}
						(id, kind, family, key_value, scope_kind, owner_id, created_at, retired_at, record)
						VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    id,
                    parts["kind"],
                    parts["family"],
                    parts["keyValue"],
                    parts["scopeKind"],
                    parts["ownerId"],
                    seq,
                    seq if action["retire"] else None,
                    encodeJson(record),
                )
            else:
                row = await executor.get(f"SELECT record FROM {self._schema['tables']['documents']} WHERE id = ?", id)
                assert row is not None
                record = parseJson(row["record"])

            if content is not None:
                if content["kind"] == "base" and isCurrentOnly(record):
                    await executor.run(f"DELETE FROM {self._schema['tables']['documentRevisions']} WHERE document_id = ?", id)
                encodedContent = encodeJson(content["value"]) if content["kind"] == "base" else encodeJson(content["ops"])
                await executor.run(
                    f"INSERT INTO {self._schema['tables']['documentRevisions']} (document_id, seq, kind, version, content) VALUES (?, ?, ?, ?, ?)",
                    id,
                    seq,
                    content["kind"],
                    content["version"],
                    encodedContent,
                )

            if action["retire"]:
                if create is None:
                    record = {**record, "retiredAt": seq}
                    await executor.run(
                        f"UPDATE {self._schema['tables']['documents']} SET retired_at = ?, record = ? WHERE id = ?", seq, encodeJson(record), id
                    )
                if isCurrentOnly(record):
                    await executor.run(f"DELETE FROM {self._schema['tables']['documentRevisions']} WHERE document_id = ?", id)

    def _assertOpen(self) -> None:
        if self._closed:
            raise RuntimeError("SqliteStorage is closed")


__all__ = ["SqliteStorage", "SqliteStorageOptions"]

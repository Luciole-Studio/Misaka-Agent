"""Detached in-memory reference implementation of ``Storage``.

Translated from pi's ``packages/durable/src/storage/memory.ts``. Reads and retained writes are cloned intentionally to
match the ownership boundary of serialization-backed stores. This is backend conformance, not validation. Python has no
frozen objects, so the "deeply frozen" detached writes of a prepared commit are plain detached copies.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right, insort
from collections.abc import Callable, Iterator, Sequence
from typing import Any

from misaka.chord.context import Context
from misaka.chord.delta import applyImmutableBatches
from misaka.utils.js import UNDEFINED, eager, jsIsInteger, jsonStringify

from ..errors import StorageRequestError
from ..types import Cursor, DocumentPoint, Page, ScanOrder, Seq
from .scan import ScanStart, nextCursor, scanStart

TASK_STATUSES = ("pending", "running", "waiting", "completing", "terminal")
SUBMISSION_STATUSES = ("queued", "placed", "done", "unanswered")
_MAX_SAFE_INTEGER = 2**53 - 1
_INFINITY = float("inf")


def documentDeltaBatches(id: int, version: int, revisions: Sequence[dict[str, Any]], start: int) -> Iterator[Any]:
    for index in range(start, len(revisions)):
        revision = revisions[index]
        if revision["kind"] != "delta" or revision["version"] != version:
            raise RuntimeError(f"Document {id} crosses a stored version boundary without a base")
        yield revision["ops"]


def clone(value: Any) -> Any:
    if isinstance(value, list):
        return [clone(item) for item in value]
    if isinstance(value, dict):
        return {key: clone(item) for key, item in value.items()}
    return value


def cursorId(cursor: Cursor | None) -> int | None:
    # ``cursor?.after === undefined``: a ``null`` is not an absent cursor.
    after = UNDEFINED if cursor is None else cursor.get("after", UNDEFINED)
    if after is UNDEFINED:
        return None
    if not jsIsInteger(after) or abs(after) > _MAX_SAFE_INTEGER:
        raise StorageRequestError("Invalid storage cursor")
    return after


def lowerBound(ids: Sequence[int], target: float) -> int:
    return bisect_left(ids, target)


def upperBound(ids: Sequence[int], target: float) -> int:
    return bisect_right(ids, target)


def insertSorted(ids: list[int], id: int) -> None:
    if len(ids) == 0 or ids[-1] < id:
        ids.append(id)
    else:
        insort(ids, id)


def removeSorted(ids: list[int], id: int) -> None:
    index = lowerBound(ids, id)
    if index < len(ids) and ids[index] == id:
        del ids[index]


def insertMapId(index: dict[Any, list[int]], key: Any, id: int) -> None:
    ids = index.get(key)
    if ids is None:
        ids = []
        index[key] = ids
    insertSorted(ids, id)


def scopeKey(scope: dict[str, Any]) -> str:
    if scope["kind"] == "session":
        return jsonStringify(["session"])
    if scope["kind"] == "conversation":
        return jsonStringify(["conversation", scope["conversationId"]])
    return jsonStringify(["task", scope["taskId"]])


def addressKey(address: dict[str, Any]) -> str:
    key = address.get("key")
    return jsonStringify(
        [address["kind"], scopeKey(address["scope"]), ["singleton"] if key is None else ["family", key]]
    )


def recordAddressKey(record: dict[str, Any]) -> str:
    return addressKey({"kind": record["kind"], "scope": record["scope"], "key": record.get("key")})


def isAliveAt(record: dict[str, Any], at: DocumentPoint) -> bool:
    if at == "current":
        return record.get("retiredAt") is None
    return record["createdAt"] <= at and (record.get("retiredAt") is None or at < record["retiredAt"])


def isCurrentOnly(record: dict[str, Any]) -> bool:
    return record["scope"]["kind"] != "conversation" or record["history"] == "latest"


def page(values: Sequence[dict[str, Any]], limit: int, order: ScanOrder = "ascending") -> Page:
    items = list(values[:limit])
    if len(values) <= limit:
        return {"items": clone(items)}
    return {"items": clone(items), "next": nextCursor(items[-1]["id"], order)}


def scanIndexes(ids: Sequence[int], start: ScanStart) -> Iterator[int]:
    """Indexes of sorted ``ids`` in scan order, after the cursor's ID when there is one."""
    if start["order"] == "ascending":
        first = 0 if start["after"] is None else upperBound(ids, start["after"])
        yield from range(first, len(ids))
    else:
        end = len(ids) if start["after"] is None else lowerBound(ids, start["after"])
        yield from range(end - 1, -1, -1)


class PreparedMemoryCommit:
    """A fully validated, detached state mutation whose application performs no fallible preparation. ``writes`` are
    detached writes for persistence."""

    def __init__(self, seq: Seq, writes: list[dict[str, Any]], apply: Callable[[], Seq]) -> None:
        self.seq = seq
        self.writes = writes
        self.apply = apply


class _State:
    def __init__(self) -> None:
        self.recordTypes: dict[int, str] = {}
        self.conversations: dict[int, dict[str, Any]] = {}
        self.conversationIds: list[int] = []
        self.conversationIdsByOwnerConversation: dict[int, list[int]] = {}
        self.conversationIdsByOwnerTask: dict[int, list[int]] = {}
        self.entries: dict[int, dict[str, Any]] = {}
        self.entryIds: dict[int, list[int]] = {}
        self.headEntryIds: dict[int, list[int]] = {}
        self.entryCommitSeqs: dict[int, int] = {}
        self.tasks: dict[int, dict[str, Any]] = {}
        self.taskIds: list[int] = []
        self.taskIdsByStatus: dict[str, list[int]] = {status: [] for status in TASK_STATUSES}
        self.submissions: dict[int, dict[str, Any]] = {}
        self.submissionIds: list[int] = []
        self.submissionIdsByStatus: dict[str, list[int]] = {status: [] for status in SUBMISSION_STATUSES}
        self.submissionIdsByRequest: dict[int, dict[str, int]] = {}
        self.documents: dict[int, dict[str, Any]] = {}  # {"record", "revisions"}
        self.documentAddresses: dict[str, dict[str, Any]] = {}  # {"ids", "currentId"?}
        self.documentIdsByScope: dict[str, list[int]] = {}


class MemoryStorage:
    def __init__(self) -> None:
        self._state = _State()
        self._nextId = 2
        self._nextSeq = 1
        self._closed = False

    @eager
    async def commit(self, writes: Sequence[dict[str, Any]], _context: Context) -> Seq:
        return self.prepareCommit(writes).apply()

    def prepareCommit(self, writes: Sequence[dict[str, Any]], seq: Seq | None = None) -> PreparedMemoryCommit:
        """Validate and detach one commit without changing observable state."""
        self._assertOpen()
        if seq is None:
            seq = self._nextSeq
        if not jsIsInteger(seq) or seq < self._nextSeq:
            raise RuntimeError(f"Commit sequence {seq} does not strictly increase")
        detachedWrites = self._resolveDocumentCopies([clone(write) for write in writes])
        self._checkGlobalIds(detachedWrites)
        documentActions = self._prepareDocumentActions(detachedWrites)
        self._checkDocumentActions(documentActions)
        applied = False

        def apply() -> Seq:
            nonlocal applied
            if not applied:
                applied = True
                self._applyPreparedCommit(detachedWrites, documentActions, seq)
            return seq

        # Python has no frozen objects: the exposed writes are a copy, so changing them cannot change retained state.
        return PreparedMemoryCommit(seq, clone(detachedWrites), apply)

    def _resolveDocumentCopies(self, writes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not any(write["type"] == "document.copy" for write in writes):
            return writes
        changedDocumentIds: set[int] = set()
        for write in writes:
            if write["type"] in ("document.create", "document.copy"):
                changedDocumentIds.add(write["record"]["id"])
            elif write["type"] in ("document.change", "document.retire"):
                changedDocumentIds.add(write["id"])
        resolved: list[dict[str, Any]] = []
        for write in writes:
            if write["type"] != "document.copy":
                resolved.append(write)
                continue
            try:
                if write["source"]["id"] in changedDocumentIds:
                    raise RuntimeError(f"Fork source document {write['source']['id']} is changed in the copy batch")
                stored = self._materializeDocument(write["source"]["id"], write["source"]["at"])
                if stored is None:
                    raise RuntimeError(f"Fork source document {write['source']['id']} cannot be read")
                record = write["record"]
                if (
                    stored["record"]["scope"]["kind"] != "conversation"
                    or record["scope"]["kind"] != "conversation"
                    or stored["record"]["kind"] != record["kind"]
                    or stored["record"].get("key") != record.get("key")
                    or stored["record"]["history"] != record["history"]
                    or stored["record"]["fork"] != record["fork"]
                ):
                    raise RuntimeError(f"Fork source document {write['source']['id']} does not match the copied record")
                resolved.append(
                    {
                        "type": "document.create",
                        "record": record,
                        "content": {"kind": "base", "version": stored["version"], "value": stored["value"]},
                    }
                )
            except Exception as error:
                raise RuntimeError(f"Document copy {write['record']['id']} was rejected") from error
        return resolved

    def _applyPreparedCommit(
        self, prepared: Sequence[dict[str, Any]], documentActions: dict[int, dict[str, Any]], seq: Seq
    ) -> Seq:
        state = self._state
        for write in prepared:
            kind = write["type"]
            if kind == "conversation":
                value = write["value"]
                state.recordTypes[value["id"]] = "conversation"
                state.conversations[value["id"]] = value
                insertSorted(state.conversationIds, value["id"])
                owner = value.get("owner")
                if owner is not None:
                    insertMapId(state.conversationIdsByOwnerConversation, owner["conversationId"], value["id"])
                    insertMapId(state.conversationIdsByOwnerTask, owner["taskId"], value["id"])
                self._nextId = max(self._nextId, value["id"] + 1)
            elif kind == "entry":
                value = write["value"]
                state.recordTypes[value["id"]] = "entry"
                state.entries[value["id"]] = value
                state.entryCommitSeqs[value["id"]] = seq
                ids = state.entryIds.get(value["conversationId"])
                if ids is None:
                    ids = []
                    state.entryIds[value["conversationId"]] = ids
                insertSorted(ids, value["id"])
                if value.get("head") is not None:
                    headIds = state.headEntryIds.get(value["conversationId"])
                    if headIds is None:
                        headIds = []
                        state.headEntryIds[value["conversationId"]] = headIds
                    insertSorted(headIds, value["id"])
                self._nextId = max(self._nextId, value["id"] + 1)
            elif kind == "task":
                value = write["value"]
                state.recordTypes[value["id"]] = "task"
                previous = state.tasks.get(value["id"])
                if previous is None:
                    insertSorted(state.taskIds, value["id"])
                    insertSorted(state.taskIdsByStatus[value["state"]["status"]], value["id"])
                elif previous["state"]["status"] != value["state"]["status"]:
                    removeSorted(state.taskIdsByStatus[previous["state"]["status"]], value["id"])
                    insertSorted(state.taskIdsByStatus[value["state"]["status"]], value["id"])
                state.tasks[value["id"]] = value
                self._nextId = max(self._nextId, value["id"] + 1)
            elif kind == "submission":
                value = write["value"]
                state.recordTypes[value["id"]] = "submission"
                previous = state.submissions.get(value["id"])
                if previous is None:
                    insertSorted(state.submissionIds, value["id"])
                    insertSorted(state.submissionIdsByStatus[value["status"]], value["id"])
                elif previous["status"] != value["status"]:
                    removeSorted(state.submissionIdsByStatus[previous["status"]], value["id"])
                    insertSorted(state.submissionIdsByStatus[value["status"]], value["id"])
                if previous is not None and previous.get("requestId") is not None:
                    previousRequests = state.submissionIdsByRequest.get(previous["conversationId"])
                    if previousRequests is not None and previousRequests.get(previous["requestId"]) == value["id"]:
                        del previousRequests[previous["requestId"]]
                        if len(previousRequests) == 0:
                            del state.submissionIdsByRequest[previous["conversationId"]]
                state.submissions[value["id"]] = value
                if value.get("requestId") is not None:
                    requests = state.submissionIdsByRequest.get(value["conversationId"])
                    if requests is None:
                        requests = {}
                        state.submissionIdsByRequest[value["conversationId"]] = requests
                    requests[value["requestId"]] = value["id"]
                self._nextId = max(self._nextId, value["id"] + 1)
            elif kind == "document.copy":
                raise RuntimeError("Prepared document copy was not resolved")

        self._applyDocumentActions(documentActions, seq)
        self._nextSeq = seq + 1
        return seq

    @eager
    async def mintId(self) -> int:
        self._assertOpen()
        if self._nextId > _MAX_SAFE_INTEGER:
            raise RuntimeError("ID space is exhausted")
        id = self._nextId
        self._nextId += 1
        return id

    @eager
    async def conversation(self, id: int, _context: Context) -> dict[str, Any] | None:
        self._assertOpen()
        value = self._state.conversations.get(id)
        return None if value is None else clone(value)

    @eager
    async def scanConversations(
        self, query: dict[str, Any], limit: int, cursor: Cursor | None, _context: Context
    ) -> Page:
        self._assertOpen()
        state = self._state
        if query.get("ownerTaskId") is not None:
            ids = state.conversationIdsByOwnerTask.get(query["ownerTaskId"], [])
        elif query.get("ownerConversationId") is not None:
            ids = state.conversationIdsByOwnerConversation.get(query["ownerConversationId"], [])
        else:
            ids = state.conversationIds
        start = scanStart(query.get("order", UNDEFINED), cursor, "ascending")
        values: list[dict[str, Any]] = []
        for index in scanIndexes(ids, start):
            if len(values) > limit:
                break
            value = state.conversations[ids[index]]
            if query.get("ownerConversationId") is not None and (
                value.get("owner") is None or value["owner"]["conversationId"] != query["ownerConversationId"]
            ):
                continue
            values.append(value)
        return page(values, limit, start["order"])

    @eager
    async def entry(self, *args: Any) -> dict[str, Any] | None:
        """``entry(id, context)`` or ``entry(conversationId, id, context)``."""
        self._assertOpen()
        state = self._state
        if len(args) == 2:
            id = args[0]
            found = state.entries.get(id)
            if found is None:
                return None
            return {"entry": clone(found), "commitSeq": state.entryCommitSeqs[id]}
        conversationId, id = args[0], args[1]
        if not isinstance(id, int) or isinstance(id, bool):
            raise StorageRequestError("Storage.entry() requires an entry ID")
        entry = next(self._visibleEntries(conversationId, id, id), None)
        if entry is None:
            return None
        return {"entry": clone(entry), "commitSeq": state.entryCommitSeqs[id]}

    @eager
    async def findLatestHeadMarker(
        self, conversationId: int, atOrBeforeEntryId: int | None, _context: Context
    ) -> dict[str, Any] | None:
        self._assertOpen()
        state = self._state
        if conversationId not in state.conversations:
            raise StorageRequestError(f"Unknown conversation: {conversationId}")
        currentId = conversationId
        upperEntryId: float = _INFINITY if atOrBeforeEntryId is None else atOrBeforeEntryId
        while True:
            ids = state.headEntryIds.get(currentId, [])
            index = upperBound(ids, upperEntryId) - 1
            if index >= 0:
                entry = state.entries[ids[index]]
                return clone({**entry, "head": entry["head"]})
            conversation = state.conversations[currentId]
            if conversation.get("parent") is None:
                return None
            upperEntryId = min(upperEntryId, conversation["parent"]["at"])
            currentId = conversation["parent"]["conversationId"]

    @eager
    async def scanEntries(self, query: dict[str, Any], limit: int, cursor: Cursor | None, _context: Context) -> Page:
        self._assertOpen()
        start = scanStart(query.get("order", UNDEFINED), cursor, "descending")
        order, after = start["order"], start["after"]
        # The cursor narrows the bound on the side the scan moves away from.
        minEntryId: float | None = query.get("minEntryId")
        maxEntryId: float | None = query.get("maxEntryId")
        if after is not None and order == "descending":
            maxEntryId = min(after if maxEntryId is None else maxEntryId, after - 1)
        if after is not None and order == "ascending":
            minEntryId = max(after if minEntryId is None else minEntryId, after + 1)
        visible: list[dict[str, Any]] = []
        entries = (
            self._visibleEntries(query["conversationId"], minEntryId, maxEntryId)
            if order == "descending"
            else self._visibleEntriesAscending(query["conversationId"], minEntryId, maxEntryId)
        )
        for entry in entries:
            visible.append(entry)
            if len(visible) > limit:
                break
        return page(visible, limit, order)

    @eager
    async def task(self, id: int, _context: Context) -> dict[str, Any] | None:
        self._assertOpen()
        value = self._state.tasks.get(id)
        return None if value is None else clone(value)

    @eager
    async def scanTasks(self, query: dict[str, Any], limit: int, cursor: Cursor | None, _context: Context) -> Page:
        self._assertOpen()
        state = self._state
        start = scanStart(query.get("order", UNDEFINED), cursor, "ascending")
        ids = state.taskIds if query.get("status") is None else state.taskIdsByStatus[query["status"]]
        values: list[dict[str, Any]] = []
        for index in scanIndexes(ids, start):
            if len(values) > limit:
                break
            value = state.tasks[ids[index]]
            if query.get("conversationId") is not None and value["conversationId"] != query["conversationId"]:
                continue
            if query.get("kind") is not None and value["kind"] != query["kind"]:
                continue
            if query.get("abortRequested") is not None and value["abortRequested"] != query["abortRequested"]:
                continue
            if query.get("background") is not None and value["background"] != query["background"]:
                continue
            values.append(value)
        return page(values, limit, start["order"])

    @eager
    async def submission(self, id: int, _context: Context) -> dict[str, Any] | None:
        self._assertOpen()
        value = self._state.submissions.get(id)
        return None if value is None else clone(value)

    @eager
    async def scanSubmissions(
        self, query: dict[str, Any], limit: int, cursor: Cursor | None, _context: Context
    ) -> Page:
        self._assertOpen()
        state = self._state
        start = scanStart(query.get("order", UNDEFINED), cursor, "ascending")
        ids = state.submissionIds if query.get("status") is None else state.submissionIdsByStatus[query["status"]]
        values: list[dict[str, Any]] = []
        for index in scanIndexes(ids, start):
            if len(values) > limit:
                break
            value = state.submissions[ids[index]]
            if query.get("conversationId") is not None and value["conversationId"] != query["conversationId"]:
                continue
            values.append(value)
        return page(values, limit, start["order"])

    @eager
    async def submissionByRequest(
        self, conversationId: int, requestId: str, _context: Context
    ) -> dict[str, Any] | None:
        self._assertOpen()
        requests = self._state.submissionIdsByRequest.get(conversationId)
        id = None if requests is None else requests.get(requestId)
        if id is None:
            return None
        return clone(self._state.submissions[id])

    @eager
    async def findDocument(self, address: dict[str, Any], at: DocumentPoint, _context: Context) -> dict[str, Any] | None:
        self._assertOpen()
        index = self._state.documentAddresses.get(addressKey(address))
        if at == "current":
            if index is None or index.get("currentId") is None:
                return None
            return clone(self._state.documents[index["currentId"]]["record"])
        for id in [] if index is None else index["ids"]:
            record = self._state.documents[id]["record"]
            if isAliveAt(record, at):
                return clone(record)
        return None

    @eager
    async def document(self, id: int, at: DocumentPoint, _context: Context) -> dict[str, Any] | None:
        self._assertOpen()
        stored = self._materializeDocument(id, at)
        if stored is None:
            return None
        return {
            "record": clone(stored["record"]),
            "version": stored["version"],
            "value": clone(stored["value"]),
            "deltasSinceBase": stored["deltasSinceBase"],
        }

    @eager
    async def scanDocuments(self, query: dict[str, Any], limit: int, cursor: Cursor | None, _context: Context) -> Page:
        self._assertOpen()
        state = self._state
        ids = state.documentIdsByScope.get(scopeKey(query["scope"]), [])
        after = cursorId(cursor)
        start = 0 if after is None else upperBound(ids, after)
        values: list[dict[str, Any]] = []
        index = start
        while index < len(ids) and len(values) <= limit:
            record = state.documents[ids[index]]["record"]
            index += 1
            if query.get("kind") is not None and record["kind"] != query["kind"]:
                continue
            if isAliveAt(record, query["at"]):
                values.append(record)
        return page(values, limit)

    @eager
    async def close(self, _context: Context) -> None:
        self._closed = True

    def _materializeDocument(self, id: int, at: DocumentPoint) -> dict[str, Any] | None:
        stored = self._state.documents.get(id)
        if stored is None:
            return None
        if at != "current" and isCurrentOnly(stored["record"]):
            raise StorageRequestError(f"Document {id} does not retain historical content")
        if not isAliveAt(stored["record"], at):
            return None
        revisions = stored["revisions"] if at == "current" else [r for r in stored["revisions"] if r["seq"] <= at]
        baseIndex = len(revisions) - 1
        while baseIndex >= 0 and revisions[baseIndex]["kind"] != "base":
            baseIndex -= 1
        base = revisions[baseIndex] if baseIndex >= 0 else None
        if base is None or base["kind"] != "base":
            raise RuntimeError(f"Document {id} is missing a required base")
        value = applyImmutableBatches(
            base["value"], documentDeltaBatches(id, base["version"], revisions, baseIndex + 1)
        )
        return {
            "record": stored["record"],
            "version": base["version"],
            "value": value,
            "deltasSinceBase": len(revisions) - baseIndex - 1,
        }

    def _visibleEntries(
        self, conversationId: int, minEntryId: float | None = None, maxEntryId: float | None = None
    ) -> Iterator[dict[str, Any]]:
        state = self._state
        if conversationId not in state.conversations:
            raise StorageRequestError(f"Unknown conversation: {conversationId}")
        minimum: float = -_INFINITY if minEntryId is None else minEntryId
        currentId = conversationId
        upperEntryId: float = _INFINITY if maxEntryId is None else maxEntryId
        while True:
            ids = state.entryIds.get(currentId, [])
            for index in range(upperBound(ids, upperEntryId) - 1, -1, -1):
                id = ids[index]
                if id < minimum:
                    break
                yield state.entries[id]
            conversation = state.conversations[currentId]
            if conversation.get("parent") is None:
                break
            upperEntryId = min(upperEntryId, conversation["parent"]["at"])
            if upperEntryId < minimum:
                break
            currentId = conversation["parent"]["conversationId"]

    def _visibleEntriesAscending(
        self, conversationId: int, minEntryId: float | None = None, maxEntryId: float | None = None
    ) -> Iterator[dict[str, Any]]:
        """Visible entries oldest first: the fork chain's segments from the root conversation forward."""
        state = self._state
        minimum: float = -_INFINITY if minEntryId is None else minEntryId
        if conversationId not in state.conversations:
            raise StorageRequestError(f"Unknown conversation: {conversationId}")
        segments: list[tuple[int, float]] = []
        currentId = conversationId
        upperEntryId: float = _INFINITY if maxEntryId is None else maxEntryId
        while True:
            segments.append((currentId, upperEntryId))
            conversation = state.conversations[currentId]
            if conversation.get("parent") is None:
                break
            upperEntryId = min(upperEntryId, conversation["parent"]["at"])
            if upperEntryId < minimum:
                break
            currentId = conversation["parent"]["conversationId"]
        for segmentId, upper in reversed(segments):
            ids = state.entryIds.get(segmentId, [])
            for index in range(lowerBound(ids, minimum), len(ids)):
                id = ids[index]
                if id > upper:
                    break
                yield state.entries[id]

    def _checkGlobalIds(self, writes: Sequence[dict[str, Any]]) -> None:
        claimed: dict[int, str] = {}
        for write in writes:
            if write["type"] in ("document.change", "document.retire"):
                continue
            document = write["type"] in ("document.create", "document.copy")
            table = "document" if document else write["type"]
            id = write["record"]["id"] if document else write["value"]["id"]
            existing = self._state.recordTypes.get(id)
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
            if write["type"] not in ("document.create", "document.change", "document.retire"):
                continue
            id = write["record"]["id"] if write["type"] == "document.create" else write["id"]
            action = actions.get(id)
            if action is None:
                action = {"retire": False}
                actions[id] = action
            if write["type"] == "document.create":
                if action.get("create") is not None or action.get("content") is not None:
                    raise RuntimeError(f"Document {id} has more than one content command")
                action["create"] = write["record"]
                action["content"] = write["content"]
            elif write["type"] == "document.change":
                if action.get("content") is not None:
                    raise RuntimeError(f"Document {id} has more than one content command")
                action["content"] = write["content"]
            else:
                if action["retire"]:
                    raise RuntimeError(f"Document {id} is retired more than once")
                action["retire"] = True
        return actions

    def _checkDocumentActions(self, actions: dict[int, dict[str, Any]]) -> None:
        liveCounts: dict[str, int] = {}
        state = self._state
        for id, action in actions.items():
            existing = state.documents.get(id)
            create = action.get("create")
            if create is None and existing is None:
                raise RuntimeError(f"Unknown document: {id}")
            if create is not None and existing is not None:
                raise RuntimeError(f"Document {id} already exists")
            if existing is not None and existing["record"].get("retiredAt") is not None:
                raise RuntimeError(f"Document {id} is retired")
            previous = existing["revisions"][-1] if existing is not None and existing["revisions"] else None
            content = action.get("content")
            if content is not None and content["kind"] == "delta":
                if previous is None:
                    raise RuntimeError(f"Document {id} delta has no base")
                if previous["version"] != content["version"]:
                    raise RuntimeError(f"Document {id} version transition requires a base")

            key = recordAddressKey(create) if create is not None else recordAddressKey(existing["record"])  # type: ignore[index]
            index = state.documentAddresses.get(key)
            currentId = None if index is None else index.get("currentId")
            live = liveCounts.get(key)
            if live is None:
                live = 0 if currentId is None else 1
            if action["retire"] and currentId == id:
                live -= 1
            if create is not None and not action["retire"]:
                live += 1
            liveCounts[key] = live

        for live in liveCounts.values():
            if live > 1:
                raise RuntimeError("Document address already has a current incarnation")

    def _applyDocumentActions(self, actions: dict[int, dict[str, Any]], seq: Seq) -> None:
        state = self._state
        for id, action in actions.items():
            stored = state.documents.get(id)
            create = action.get("create")
            if create is not None:
                record = {**create, "createdAt": seq, **({"retiredAt": seq} if action["retire"] else {})}
                stored = {"record": record, "revisions": [{**action["content"], "seq": seq}]}
                state.recordTypes[id] = "document"
                state.documents[id] = stored

                key = recordAddressKey(record)
                address = state.documentAddresses.get(key)
                if address is None:
                    address = {"ids": []}
                    state.documentAddresses[key] = address
                insertSorted(address["ids"], id)

                scopeIds = state.documentIdsByScope.get(scopeKey(record["scope"]))
                if scopeIds is None:
                    scopeIds = []
                    state.documentIdsByScope[scopeKey(record["scope"])] = scopeIds
                insertSorted(scopeIds, id)
                self._nextId = max(self._nextId, id + 1)
            elif action.get("content") is not None:
                assert stored is not None
                revision = {**action["content"], "seq": seq}
                if revision["kind"] == "base" and isCurrentOnly(stored["record"]):
                    stored["revisions"] = [revision]
                else:
                    stored["revisions"].append(revision)

            assert stored is not None
            if action["retire"] and create is None:
                stored["record"] = {**stored["record"], "retiredAt": seq}
            if action["retire"] and isCurrentOnly(stored["record"]):
                stored["revisions"] = []
            if create is not None or action["retire"]:
                address = state.documentAddresses[recordAddressKey(stored["record"])]
                if action["retire"] and address.get("currentId") == id:
                    del address["currentId"]
                if create is not None and not action["retire"]:
                    address["currentId"] = id

    def _assertOpen(self) -> None:
        if self._closed:
            raise RuntimeError("MemoryStorage is closed")


__all__ = ["MemoryStorage", "PreparedMemoryCommit", "clone"]

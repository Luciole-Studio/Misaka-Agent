"""The transaction of one Session commit callback.

Translated from pi's ``packages/durable/src/session/transaction.ts`` (v1.3.0). A method that returns a ``Promise`` upstream
returns a future here and starts its work when it is called (``startTask``), as a JavaScript async function does;
records are plain ``dict`` objects. ``Tx.entry(id)`` / ``Tx.entry(token, id)`` and ``Tx.appendEntry(...)`` take the
same positional forms as upstream, with an ``Entry`` object as the token.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from misaka.chord.context import Context
from misaka.chord.delta import Op, track
from misaka.chord.json import copyJson
from misaka.utils.js import (
    UNDEFINED,
    allSettled,
    rejectedPromise,
    resolvedPromise,
    startTask,
)

from ..documents import (
    AnyDocDefinition,
    addressId,
    checkRecordScope,
    checkRecordVersion,
    documentCreate,
    materializeDocumentValue,
    resolveAddress,
)
from ..errors import ReadAfterWrite
from ..types import ROOT_CONVERSATION_ID, Cursor, Storage
from .forks import prepareForkDocumentCopies


def applySubmissionChange(current: dict[str, Any], change: dict[str, Any]) -> dict[str, Any]:
    """Complete record after applying one change. Placement turns a queued input ``placed`` and a queued write ``done``;
    only a placed input can be answered. A settled record stays."""
    if current["status"] == "done" or current["status"] == "unanswered":
        return current
    if change["status"] == "placed":
        if current["status"] != "queued":
            raise RuntimeError(f"Submission {current['id']} is not queued")
        status = "placed" if current["type"] == "input" else "done"
        return {**current, "status": status, "entry": change["entry"]}
    if change["status"] == "done" and current["status"] != "placed":
        raise RuntimeError(f"Submission {current['id']} is not a placed input")
    # Queued and placed records carry no answer, reason, or detail; an unanswered input keeps its entry.
    return {**current, **change}

INTERNAL_SCAN_PAGE_SIZE = 256
EMPTY_OPERATIONS: list[Op] = []
TABLE_JSON_COPY_OPTIONS = {"omitUndefinedProperties": True}


class LoadedDocument:
    """One committed document incarnation owned by the Session tracker cache."""

    __slots__ = ("addressId", "deltasSinceBase", "record", "storedVersion", "tracker", "valueVersion")

    def __init__(
        self,
        addressId: str,
        record: dict[str, Any],
        storedVersion: int,
        valueVersion: int,
        deltasSinceBase: int,
        tracker: Any,
    ) -> None:
        self.addressId = addressId
        self.record = record
        self.storedVersion = storedVersion  # Persisted definition version; older while the value is migrated in memory only.
        self.valueVersion = valueVersion  # Definition version whose shape the tracked value has.
        self.deltasSinceBase = deltasSinceBase  # Stored deltas after the newest base.
        self.tracker = tracker


class TransactionHost:
    """Session services used by a transaction while it holds the mutation line."""

    def __init__(
        self,
        storage: Storage,
        now: Callable[[], float],
        cached: Callable[[str], LoadedDocument | None],
        load: Callable[[AnyDocDefinition, str, dict[str, Any], Context], Awaitable[LoadedDocument | None]],
        install: Callable[[LoadedDocument], None],
        evict: Callable[[str, int], None],
        conversationCreated: Callable[[Transaction, dict[str, Any]], Awaitable[None]],
    ) -> None:
        self.storage = storage
        self.now = now  # Wall clock for task lifecycle times.
        self.cached = cached  # Return the cached current incarnation without loading.
        self.load = load  # Return the cached incarnation, cold-loading and migrating it when necessary.
        self.install = install  # Install a newly committed incarnation.
        self.evict = evict  # Remove a retired incarnation if it is still the cached occupant of its address.
        self.conversationCreated = conversationCreated  # Stage writes that belong to every new or forked conversation.


class _TransactionTask:
    """Committed and candidate state for one task touched by this transaction."""

    __slots__ = ("committedRead", "publicationConversationId", "write")

    def __init__(self) -> None:
        self.committedRead: asyncio.Future[Any] | None = None
        self.write: dict[str, Any] | None = None  # {"kind": "create" | "replace", "record": ...}
        self.publicationConversationId: int | None = None


class _DocumentEntry:
    """One document incarnation acquired, created, or retired by this transaction."""

    __slots__ = (
        "address",
        "addressId",
        "change",
        "definition",
        "draftPromise",
        "prepared",
        "retireOnCommit",
        "target",
    )

    def __init__(
        self,
        addressId: str,
        address: dict[str, Any],
        definition: AnyDocDefinition | None = None,
        retireOnCommit: bool = False,
        target: dict[str, Any] | None = None,
    ) -> None:
        self.addressId = addressId
        self.address = address
        self.definition = definition  # Absent for definition-free fork copies and terminal-task retirement entries.
        self.draftPromise: asyncio.Future[Any] | None = None  # Memoized public acquisition.
        self.target = target  # {"kind": "loaded" | "created" | "fork-copy" | "retire-only", ...}
        self.change: Any = None
        self.prepared: Any = None
        self.retireOnCommit = retireOnCommit


class _DocumentPlan:
    """What one staged incarnation writes and publishes, decided once before Storage admission so adoption only
    applies it. ``record`` is a ``DocumentRecord`` for a committed incarnation and a ``DocumentCreate`` for a new one."""

    __slots__ = ("addressId", "change", "content", "conversationId", "record", "retire")

    def __init__(self, addressId: str, record: dict[str, Any], retire: bool) -> None:
        self.addressId = addressId
        self.record = record
        self.retire = retire
        self.content: dict[str, Any] | None = None  # Creation, copy, or change write; absent when only retiring.
        self.change: dict[str, Any] | None = None  # {"tracker", "prepared", "version", "loaded"?, "definition"?}
        self.conversationId: int | None = None


class Transaction:
    """Transaction for one Session commit callback.

    Every asynchronous operation is tracked so callback settlement can reject and drain unfinished work. Session calls
    one settlement method, then either discards prepared changes or adopts them once after Storage succeeds."""

    def __init__(self, host: TransactionHost, context: Context, scope: dict[str, Any] | None = None) -> None:
        self._host = host
        self._context = context
        self._scope: dict[str, Any] = {} if scope is None else scope
        self._pendingOperations: set[asyncio.Future[Any]] = set()
        self._sealed = False
        self._hasTableWrite = False
        # Atomic batch; conversation and entry writes stage eagerly, while task and document writes assemble later.
        self._writes: list[dict[str, Any]] = []
        self._createdConversationIds: set[int] = set()
        self._forkSourceConversationIds: set[int] = set()
        self._forkSourceDocumentIds: set[int] = set()
        # One entry per task touched by a public read, candidate write, or document-owner lookup.
        self._tasksById: dict[int, _TransactionTask] = {}
        # Submissions created by this transaction, by ID.
        self._submissions: dict[int, dict[str, Any]] = {}
        # Settlements and placements in staging order; resolved against the latest candidate record during assembly.
        self._submissionChanges: list[tuple[int, dict[str, Any]]] = []
        # Write and publication plans of every staged incarnation, built during assembly.
        self._plans: list[_DocumentPlan] = []
        # Every document acquisition or retirement marker in staging order.
        self._documents: list[_DocumentEntry] = []
        # Latest transaction-local incarnation or retirement marker at each logical address.
        self._latestDocumentByAddress: dict[str, _DocumentEntry] = {}

    # --- Table reads ---

    def conversation(self, id: int) -> asyncio.Future[Any]:
        return self._read("conversation", lambda: self._host.storage.conversation(id, self._context))

    def entry(self, first: Any, second: int | None = None) -> asyncio.Future[Any]:
        kind = None if isinstance(first, int) else first.kind
        id = first if isinstance(first, int) else second

        async def read() -> Any:
            stored = await self._host.storage.entry(id, self._context)
            entry = None if stored is None else stored["entry"]
            return entry if kind is None or (entry is not None and entry["kind"] == kind) else None

        return self._read("entry", read)

    def task(self, id: int) -> asyncio.Future[Any]:
        return self._read("task", lambda: self._committedTask(id))

    def scanConversations(self, query: dict[str, Any], limit: int, cursor: Cursor | None = None) -> asyncio.Future[Any]:
        return self._read(
            "scanConversations", lambda: self._host.storage.scanConversations(query, limit, cursor, self._context)
        )

    def scanEntries(self, query: dict[str, Any], limit: int, cursor: Cursor | None = None) -> asyncio.Future[Any]:
        return self._read("scanEntries", lambda: self._host.storage.scanEntries(query, limit, cursor, self._context))

    def latestHeadMarker(self, conversationId: int) -> asyncio.Future[Any]:
        return self._read(
            "latestHeadMarker",
            lambda: self._host.storage.findLatestHeadMarker(conversationId, None, self._context),
        )

    def scanTasks(self, query: dict[str, Any], limit: int, cursor: Cursor | None = None) -> asyncio.Future[Any]:
        return self._read("scanTasks", lambda: self._host.storage.scanTasks(query, limit, cursor, self._context))

    def submission(self, id: int) -> asyncio.Future[Any]:
        """Internal: committed submission record."""
        return self._read("submission", lambda: self._host.storage.submission(id, self._context))

    def submissionByRequest(self, conversationId: int, requestId: str) -> asyncio.Future[Any]:
        """Committed submission with a conversation-scoped request ID."""
        return self._read(
            "submissionByRequest",
            lambda: self._host.storage.submissionByRequest(conversationId, requestId, self._context),
        )

    # --- Table writes ---

    def createConversation(self, options: dict[str, Any]) -> asyncio.Future[Any]:
        return self._write(lambda: self._stageConversation(None, options["ownership"]))

    def createRootConversation(self) -> asyncio.Future[Any]:
        """Internal final-form bootstrap path for the reserved root identity."""
        return self._write(lambda: self._stageConversation(None, {"kind": "ownerless"}, ROOT_CONVERSATION_ID))

    def forkConversation(self, parentConversationId: int, at: int, options: dict[str, Any]) -> asyncio.Future[Any]:
        return self._write(
            lambda: self._stageConversation(
                {"conversationId": parentConversationId, "at": at}, options["ownership"]
            )
        )

    async def _stageConversation(
        self, parent: dict[str, Any] | None, ownership: dict[str, Any], reservedId: int | None = None
    ) -> dict[str, Any]:
        ownerTaskId = ownership["taskId"] if ownership["kind"] == "task" else None
        id = reservedId if reservedId is not None else await self._host.storage.mintId()
        self._assertOpen()
        owner: dict[str, Any] | None = None
        if ownerTaskId is not None:
            task = await self._currentTask(ownerTaskId)
            self._assertOpen()
            if task is None:
                raise RuntimeError(f"Conversation owner task {ownerTaskId} does not exist")
            owner = {"conversationId": task["conversationId"], "taskId": ownerTaskId}
        record: dict[str, Any] = {"id": id}
        if parent is not None:
            record["parent"] = parent
        if owner is not None:
            record["owner"] = owner
        copies = (
            []
            if parent is None
            else await prepareForkDocumentCopies(
                self._host.storage, parent["conversationId"], parent["at"], id, self._context
            )
        )
        self._assertOpen()
        for copy in copies:
            self._forkSourceDocumentIds.add(copy["source"]["id"])
            docEntry = _DocumentEntry(
                addressId(copy["record"]),
                copy["record"],
                target={"kind": "fork-copy", **copy},
            )
            self._documents.append(docEntry)
            self._latestDocumentByAddress[docEntry.addressId] = docEntry
        if parent is not None:
            self._forkSourceConversationIds.add(parent["conversationId"])
        self._createdConversationIds.add(id)
        self._writes.append({"type": "conversation", "value": record})
        await self._host.conversationCreated(self, record)
        self._assertOpen()
        return record

    def appendEntry(self, first: Any, second: Any, third: dict[str, Any] | None = None) -> asyncio.Future[Any]:
        if isinstance(first, int):
            return self._appendEntry(first, second)
        return self._appendEntry(second, {**(third or {}), "kind": first.kind})

    def _appendEntry(self, conversationId: int, value: dict[str, Any]) -> asyncio.Future[Any]:
        async def write() -> dict[str, Any]:
            await self._requireConversation(conversationId)
            self._assertOpen()
            id = await self._host.storage.mintId()
            self._assertOpen()
            head = value.get("head", UNDEFINED)
            rest = {key: item for key, item in value.items() if key != "head"}
            # Undefined `head` and `byTaskId` are omitted by the copy.
            record = copyJson(
                {
                    **rest,
                    "id": id,
                    "conversationId": conversationId,
                    "head": id if head == "self" else head,
                    "byTaskId": self._scope.get("taskId", UNDEFINED),
                },
                TABLE_JSON_COPY_OPTIONS,  # type: ignore[arg-type]
            )
            self._writes.append({"type": "entry", "value": record})
            return record  # type: ignore[return-value]

        return self._write(write)

    def createTask(self, task: dict[str, Any], input: Any, options: dict[str, Any]) -> asyncio.Future[Any]:
        async def write() -> int:
            ownership = options["ownership"]
            owner: dict[str, Any] | None = None
            if ownership["kind"] == "task":
                # Validated again against the owner's final candidate during assembly.
                owner = await self._currentTask(ownership["taskId"])
                self._assertOpen()
                if owner is None:
                    raise RuntimeError(f"Task owner {ownership['taskId']} does not exist")
                if options.get("background") is True:
                    raise TypeError("A child task cannot be background")
                if options.get("conversationId") is not None and options["conversationId"] != owner["conversationId"]:
                    raise RuntimeError(f"A child task lives in its owner's conversation {owner['conversationId']}")
            conversationId = (
                owner["conversationId"]
                if owner is not None
                else (
                    options["conversationId"]
                    if options.get("conversationId") is not None
                    else self._scope.get("conversationId")
                )
            )
            if conversationId is None:
                raise TypeError("Tx.createTask() requires options.conversationId")
            await self._requireConversation(conversationId)
            self._assertOpen()
            definition = task["definition"]
            checkpoint = definition["initial"](input)
            id = await self._host.storage.mintId()
            self._assertOpen()
            record = copyJson(
                {
                    "id": id,
                    "conversationId": conversationId,
                    "kind": definition["name"],
                    "version": definition["version"],
                    "input": input,
                    **({} if owner is None else {"owner": owner["id"]}),
                    "background": options.get("background") if options.get("background") is not None else False,
                    "abortRequested": False,
                    **({"abandonOnRestart": True} if options.get("abandonOnRestart") is True else {}),
                    "state": {"status": "pending", "checkpoint": checkpoint},
                },
                TABLE_JSON_COPY_OPTIONS,  # type: ignore[arg-type]
            )
            entry = _TransactionTask()
            entry.write = {"kind": "create", "record": record}
            self._tasksById[id] = entry
            return id

        return self._write(write)

    def createSubmission(self, create: dict[str, Any]) -> asyncio.Future[Any]:
        """Create a raw submission record with a fresh ID; no admission rules apply."""

        async def write() -> dict[str, Any]:
            await self._requireConversation(create["conversationId"])
            self._assertOpen()
            id = await self._host.storage.mintId()
            self._assertOpen()
            record = copyJson({**create, "id": id}, TABLE_JSON_COPY_OPTIONS)  # type: ignore[arg-type]
            self._submissions[id] = record  # type: ignore[assignment]
            return record  # type: ignore[return-value]

        return self._write(write)

    def settleSubmission(self, id: int, settlement: dict[str, Any]) -> None:
        """Settle a submission. Resolved during assembly against the transaction's latest candidate record, falling
        back to committed state, so it is not a caller table read and works after the first table write."""
        self._assertOpen()
        self._hasTableWrite = True
        self._submissionChanges.append((id, copyJson(settlement)))  # type: ignore[arg-type]

    def placeSubmission(self, id: int, entry: int) -> None:
        """Place a queued submission at ``entry``; resolved during assembly like ``settleSubmission()``."""
        self._assertOpen()
        self._hasTableWrite = True
        self._submissionChanges.append((id, {"status": "placed", "entry": entry}))

    def setTask(self, value: dict[str, Any]) -> None:
        """Internal: replace one task record completely. Tasks change their own state through their runtime."""
        self._assertOpen()
        self._hasTableWrite = True
        task = self._taskEntry(value["id"])
        candidate = None if task.write is None else task.write["record"]
        if candidate is not None and candidate["state"]["status"] == "terminal":
            raise RuntimeError(f"Task {value['id']} already has a terminal candidate")
        if candidate is not None and candidate["conversationId"] != value["conversationId"]:
            raise RuntimeError(f"Task {value['id']} cannot change conversations")
        task.write = {
            "kind": "create" if task.write is not None and task.write["kind"] == "create" else "replace",
            "record": copyJson(self._stampTimes(value, candidate), TABLE_JSON_COPY_OPTIONS),  # type: ignore[arg-type]
        }

    def stagedTasks(self) -> list[dict[str, Any]]:
        """Internal: candidate records of the tasks this transaction created or replaced so far."""
        return [task.write["record"] for task in self._tasksById.values() if task.write is not None]

    def stagedConversations(self) -> list[dict[str, Any]]:
        """Internal: conversations this transaction created or forked so far."""
        return [write["value"] for write in self._writes if write["type"] == "conversation"]

    # --- Documents ---

    def doc(self, token: dict[str, Any], *args: Any) -> asyncio.Future[Any]:
        try:
            self._assertOpen()
            definition = token["definition"]
            resolved = resolveAddress(definition, args)
            self._assertTaskDocumentsOpen(resolved)
            latest = self._latestDocumentByAddress.get(resolved.id)
            if latest is not None and not latest.retireOnCommit:
                if latest.draftPromise is not None:
                    return latest.draftPromise
                if latest.target is not None and latest.target["kind"] == "fork-copy":
                    latest.draftPromise = self._track(self._acquireForkCopy(latest, definition, latest.target))
                    return latest.draftPromise
            seed = copyJson(args[resolved.nextArgument]) if definition.get("family") is True else UNDEFINED
            docEntry = _DocumentEntry(resolved.id, resolved.address, definition)
            self._documents.append(docEntry)
            self._latestDocumentByAddress[docEntry.addressId] = docEntry
            # Capture retirement before awaiting so a pending old acquisition and its replacement stay distinct.
            docEntry.draftPromise = self._track(
                self._acquire(docEntry, seed, latest is not None and latest.retireOnCommit is True)
            )
            return docEntry.draftPromise
        except Exception as error:  # noqa: BLE001
            return rejectedPromise(error)

    def retireDoc(self, token: dict[str, Any], *args: Any) -> asyncio.Future[Any]:
        try:
            self._assertOpen()
            definition = token["definition"]
            resolved = resolveAddress(definition, args)
            latest = self._latestDocumentByAddress.get(resolved.id)
            if latest is not None and latest.retireOnCommit:
                return resolvedPromise(None)
            if latest is not None and latest.target is not None and latest.target["kind"] == "fork-copy":
                checkRecordScope(definition, latest.target["record"])
                latest.retireOnCommit = True
                return resolvedPromise(None)
            if latest is not None and latest.draftPromise is not None:
                # Retirement of an acquired draft persists its final content before retirement.
                latest.retireOnCommit = True
                return self._track(self._afterDraft(latest.draftPromise))
            docEntry = _DocumentEntry(resolved.id, resolved.address, definition, True)
            self._documents.append(docEntry)
            self._latestDocumentByAddress[docEntry.addressId] = docEntry
            return self._track(self._findRetirement(docEntry))
        except Exception as error:  # noqa: BLE001
            return rejectedPromise(error)

    @staticmethod
    async def _afterDraft(draft: asyncio.Future[Any]) -> None:
        await draft

    async def _acquire(self, entry: _DocumentEntry, seed: Any, skipLoad: bool) -> Any:
        definition = entry.definition
        assert definition is not None
        loaded = None if skipLoad else await self._host.load(definition, entry.addressId, entry.address, self._context)
        self._assertOpen()
        if loaded is not None:
            checkRecordScope(definition, loaded.record)
            checkRecordVersion(definition, loaded.record, loaded.storedVersion)
            entry.target = {"kind": "loaded", "document": loaded}
            entry.change = loaded.tracker.beginChange()
            return entry.change.state
        scope = entry.address["scope"]
        if scope["kind"] == "conversation":
            await self._requireConversation(scope["conversationId"])
        if scope["kind"] == "task":
            task = await self._currentTask(scope["taskId"])
            if task is None:
                raise RuntimeError(f"Task {scope['taskId']} does not exist")
            if task["state"]["status"] == "terminal":
                raise RuntimeError(f"Task {scope['taskId']} is terminal")
        self._assertOpen()
        value = copyJson(definition["initial"](seed) if definition.get("family") is True else definition["initial"]())
        id = await self._host.storage.mintId()
        self._assertOpen()
        tracker = track(value)
        entry.target = {
            "kind": "created",
            "record": documentCreate(definition, entry.address, id),  # type: ignore[arg-type]
            "version": definition["version"],
            "tracker": tracker,
        }
        entry.change = tracker.beginChange()
        return entry.change.state

    async def _acquireForkCopy(self, entry: _DocumentEntry, definition: AnyDocDefinition, target: dict[str, Any]) -> Any:
        stored = await self._host.storage.document(target["source"]["id"], target["source"]["at"], self._context)
        self._assertOpen()
        if stored is None:
            raise RuntimeError(f"Fork source document {target['source']['id']} cannot be read")
        record = stored["record"]
        if (
            record["scope"]["kind"] != "conversation"
            or record["kind"] != target["record"]["kind"]
            or record.get("key") != target["record"].get("key")
            or record.get("history") != target["record"].get("history")
            or record.get("fork") != target["record"].get("fork")
        ):
            raise RuntimeError(f"Fork source document {target['source']['id']} does not match the copied record")
        value = materializeDocumentValue(definition, target["record"], stored["version"], stored["value"])
        tracker = track(value)
        entry.definition = definition
        entry.target = {
            "kind": "created",
            "record": target["record"],
            "version": definition["version"],
            "tracker": tracker,
        }
        entry.change = tracker.beginChange()
        return entry.change.state

    async def _findRetirement(self, entry: _DocumentEntry) -> None:
        cached = self._host.cached(entry.addressId)
        record = (
            cached.record
            if cached is not None
            else await self._host.storage.findDocument(entry.address, "current", self._context)  # type: ignore[arg-type]
        )
        self._assertOpen()
        if record is None:
            return
        assert entry.definition is not None
        checkRecordScope(entry.definition, record)
        entry.target = {"kind": "retire-only", "record": record}

    # --- Settlement ---

    async def settleFailure(self) -> None:
        """Seal after callback failure: abort every change and observe every pending operation."""
        self._sealed = True
        self._abortChanges()
        await allSettled(list(self._pendingOperations))

    async def settleSuccess(self) -> list[dict[str, Any]]:
        """Seal after callback success, prepare every open change, and assemble the atomic batch. Any failure aborts
        every change before Storage admission."""
        self._sealed = True
        if len(self._pendingOperations) > 0:
            self._abortChanges()
            await allSettled(list(self._pendingOperations))
            raise RuntimeError("Session commit callback settled before its pending Tx operations")
        try:
            # Synchronously prepare every open change; this revokes every draft.
            for document in self._documents:
                if document.change is not None:
                    document.prepared = document.change.prepare()
            return await self._assemble()
        except Exception:
            self._abortChanges()
            raise

    def discard(self) -> None:
        """Abort every prepared change after Storage failure or when no write is required."""
        self._abortChanges()

    def adopt(self, seq: int) -> list[dict[str, Any]]:
        """Adopt every prepared change by pointer swap after Storage success and describe the publication."""
        publications: list[dict[str, Any]] = []
        for plan in self._plans:
            committed = "createdAt" in plan.record
            record = plan.record if committed else {**plan.record, "createdAt": seq}
            if plan.retire:
                record = {**record, "retiredAt": seq}
            change = plan.change
            if change is not None:
                tracker, prepared, loaded, version = (
                    change["tracker"],
                    change["prepared"],
                    change.get("loaded"),
                    change["version"],
                )
                # A new incarnation is adopted unless it retires in the same commit; a loaded one only when it changed.
                if (not plan.retire) if loaded is None else len(prepared.ops) > 0:
                    tracker.adopt(prepared)
                else:
                    prepared.abort()
                if loaded is not None:
                    loaded.storedVersion = max(loaded.storedVersion, version)
                    if plan.content is not None and plan.content["type"] == "document.change":
                        if plan.content["content"]["kind"] == "base":
                            loaded.deltasSinceBase = 0
                        else:
                            loaded.deltasSinceBase += 1
                elif not plan.retire:
                    self._host.install(LoadedDocument(plan.addressId, record, version, version, 0, tracker))
            conversationId = plan.conversationId
            if plan.retire:
                if committed:
                    self._host.evict(plan.addressId, record["id"])
                publications.append(
                    {
                        "type": "document",
                        "record": record,
                        "conversationId": conversationId,
                        "version": None,
                        "value": None,
                        "ops": EMPTY_OPERATIONS,
                    }
                )
            elif plan.content is not None and plan.content["type"] == "document.copy":
                publications.append(
                    {
                        "type": "document.copy",
                        "record": record,
                        "conversationId": conversationId,
                        "source": plan.content["source"],
                    }
                )
            elif change is not None and publishes(plan):
                ops = EMPTY_OPERATIONS if change.get("loaded") is None else change["prepared"].ops
                publications.append(
                    {
                        "type": "document",
                        "record": record,
                        "conversationId": conversationId,
                        "version": change["version"],
                        "value": change["prepared"].value,
                        "ops": ops,
                    }
                )
        return publications

    async def _assemble(self) -> list[dict[str, Any]]:
        storage = self._host.storage
        plans = self._plans
        for document in self._documents:
            plan = planDocument(document)
            if plan is not None:
                plans.append(plan)
        self._rejectForkSourceWrites(plans)
        await self._validateOwners()
        for id, task in list(self._tasksById.items()):
            if task.write is None or task.write["kind"] != "replace":
                continue
            committed = await self._committedTask(id)
            if committed is None:
                raise RuntimeError(f"Task {id} does not exist")
            if committed["state"]["status"] == "terminal":
                raise RuntimeError(f"Task {id} is already terminal")
            if committed["conversationId"] != task.write["record"]["conversationId"]:
                raise RuntimeError(f"Task {id} cannot change conversations")

        # Terminal settlement retires every task document, including ones created by this transaction.
        # A ``Set``: insertion order, which is the order of the retirements written.
        terminalTaskIds: dict[int, None] = {}
        for task in self._tasksById.values():
            if task.write is not None and task.write["record"]["state"]["status"] == "terminal":
                terminalTaskIds[task.write["record"]["id"]] = None
        if terminalTaskIds:
            retiring: set[int] = set()
            for plan in plans:
                scope = plan.record["scope"]
                if scope["kind"] != "task" or scope["taskId"] not in terminalTaskIds:
                    continue
                plan.retire = True
                retiring.add(plan.record["id"])
            for taskId in terminalTaskIds:
                owner = self._tasksById.get(taskId)
                if owner is not None and owner.write is not None and owner.write["kind"] == "create":
                    continue
                cursor: Cursor | None = None
                while True:
                    page = await storage.scanDocuments(
                        {"scope": {"kind": "task", "taskId": taskId}, "at": "current"},
                        INTERNAL_SCAN_PAGE_SIZE,
                        cursor,
                        self._context,
                    )
                    for record in page["items"]:
                        if record["id"] in retiring:
                            continue
                        plans.append(_DocumentPlan(addressId(record), record, True))
                        retiring.add(record["id"])
                    cursor = page.get("next")
                    if cursor is None:
                        break

        # Resolve publication ownership before Storage admission so adoption remains synchronous.
        for plan in plans:
            if not publishes(plan):
                continue
            scope = plan.record["scope"]
            if scope["kind"] == "conversation":
                plan.conversationId = scope["conversationId"]
            if scope["kind"] != "task":
                continue
            taskEntry = self._taskEntry(scope["taskId"])
            if taskEntry.publicationConversationId is None:
                current = await self._currentTask(scope["taskId"])
                if current is not None:
                    taskEntry.publicationConversationId = current["conversationId"]
            plan.conversationId = taskEntry.publicationConversationId

        for id, submissionChange in self._submissionChanges:
            current = self._submissions.get(id)
            if current is None:
                current = await storage.submission(id, self._context)
            if current is None:
                raise RuntimeError(f"Submission {id} does not exist")
            nextRecord = applySubmissionChange(current, submissionChange)
            if nextRecord is not current:
                self._submissions[id] = nextRecord

        writes = self._writes
        for value in self._submissions.values():
            writes.append({"type": "submission", "value": value})
        for task in self._tasksById.values():
            if task.write is not None:
                writes.append({"type": "task", "value": task.write["record"]})
        for plan in plans:
            change = plan.change
            # Checkpoint predicates run last, after every validation.
            if (
                plan.content is not None
                and plan.content["type"] == "document.change"
                and plan.content["content"]["kind"] == "delta"
                and change is not None
                and change.get("loaded") is not None
            ):
                prepared, version = change["prepared"], change["version"]
                info = {"deltasSinceBase": change["loaded"].deltasSinceBase}
                checkpointWhen = None if change.get("definition") is None else change["definition"].get("checkpointWhen")
                if checkpointWhen is not None and checkpointWhen(prepared.value, prepared.ops, info):
                    plan.content = {
                        **plan.content,
                        "content": {"version": version, "kind": "base", "value": prepared.value},
                    }
            if plan.content is not None:
                writes.append(plan.content)
            if plan.retire:
                writes.append({"type": "document.retire", "id": plan.record["id"]})
        return writes

    # --- Helpers ---

    async def _validateOwners(self) -> None:
        """New owned work needs a live owner, judged on the owner's final candidate: not ``completing``, terminal, or
        abort-marked. A task therefore cannot create owned work in the commit that finishes it (spec section 5.5)."""
        owners: list[tuple[str, int]] = []
        for write in self._writes:
            if write["type"] != "conversation" or write["value"].get("owner") is None:
                continue
            owners.append(("Conversation owner task", write["value"]["owner"]["taskId"]))
        for task in self._tasksById.values():
            record = task.write["record"] if task.write is not None and task.write["kind"] == "create" else None
            if record is not None and record.get("owner") is not None:
                owners.append(("Task owner", record["owner"]))
        for what, taskId in owners:
            current = await self._currentTask(taskId)
            if current is None:
                raise RuntimeError(f"{what} {taskId} does not exist")
            if current["state"]["status"] in ("terminal", "completing"):
                raise RuntimeError(f"{what} {taskId} is {current['state']['status']}")
            if current["abortRequested"]:
                raise RuntimeError(f"{what} {taskId} is abort-marked")

    def _rejectForkSourceWrites(self, plans: list[_DocumentPlan]) -> None:
        for plan in plans:
            if plan.content is None and not plan.retire:
                continue
            record = plan.record
            if record["id"] in self._forkSourceDocumentIds:
                raise RuntimeError(f"Cannot change fork source document {record['id']} in the fork transaction")
            scope = plan.record["scope"]
            if (
                scope["kind"] == "conversation"
                and scope["conversationId"] in self._forkSourceConversationIds
                and record["scope"]["kind"] == "conversation"
                and record.get("fork") == "current"
            ):
                raise RuntimeError(
                    f"Cannot fork conversation {scope['conversationId']} while changing its current-policy documents"
                )

    def _abortChanges(self) -> None:
        for document in self._documents:
            if document.change is not None:
                document.change.abort()

    def _assertOpen(self) -> None:
        if self._sealed:
            raise RuntimeError("Transaction has settled")

    def _assertTaskDocumentsOpen(self, resolved: Any) -> None:
        scope = resolved.address["scope"]
        if scope["kind"] == "task":
            task = self._tasksById.get(scope["taskId"])
            if task is not None and task.write is not None and task.write["record"]["state"]["status"] == "terminal":
                raise RuntimeError(f"Task {scope['taskId']} is terminal")

    def _track(self, operation: Awaitable[Any]) -> asyncio.Future[Any]:
        """Register an operation so callback settlement can reject and drain it.

        A JavaScript Storage call always takes a turn, and ``operation.then(settle, settle)`` is attached before the caller
        awaits the same promise. So an operation nobody awaits is still pending when the callback has settled, and one that
        is awaited has settled by the time its caller resumes. An operation here may finish without suspending, so the
        future handed out settles one turn after it, which gives the same two orders."""
        inner = startTask(operation)
        loop = asyncio.get_running_loop()
        tracked: asyncio.Future[Any] = loop.create_future()
        self._pendingOperations.add(tracked)

        def finish() -> None:
            self._pendingOperations.discard(tracked)
            if tracked.done():
                return
            if inner.cancelled():
                tracked.cancel()
            elif (error := inner.exception()) is not None:
                tracked.set_exception(error)
                tracked.exception()  # `operation.then(settle, settle)` handles a rejection; the caller still sees it
            else:
                tracked.set_result(inner.result())

        inner.add_done_callback(lambda _done: loop.call_soon(finish))
        return tracked

    def _read(self, method: str, read: Callable[[], Awaitable[Any]]) -> asyncio.Future[Any]:
        try:
            self._assertOpen()
            if self._hasTableWrite:
                raise ReadAfterWrite(method)
            return self._track(read())
        except Exception as error:  # noqa: BLE001
            return rejectedPromise(error)

    def _write(self, write: Callable[[], Awaitable[Any]]) -> asyncio.Future[Any]:
        try:
            self._assertOpen()
            self._hasTableWrite = True
            return self._track(write())
        except Exception as error:  # noqa: BLE001
            return rejectedPromise(error)

    async def _requireConversation(self, id: int) -> None:
        if id in self._createdConversationIds:
            return
        if await self._host.storage.conversation(id, self._context) is None:
            raise RuntimeError(f"Conversation {id} does not exist")

    def _stampTimes(self, value: dict[str, Any], candidate: dict[str, Any] | None) -> dict[str, Any]:
        """Lifecycle times: ``startedAt`` on the first change to ``running``, ``endedAt`` on the change to ``terminal``.
        Once set, they carry over from the replaced record; records written before they existed lack them."""
        status = value["state"]["status"]

        def now(stamps: bool) -> float | None:
            return self._host.now() if stamps else None

        startedAt = (candidate or {}).get("startedAt")
        if startedAt is None:
            startedAt = value.get("startedAt")
        if startedAt is None:
            startedAt = now(status == "running")
        endedAt = (candidate or {}).get("endedAt")
        if endedAt is None:
            endedAt = value.get("endedAt")
        if endedAt is None:
            endedAt = now(status == "terminal")
        if startedAt == value.get("startedAt") and endedAt == value.get("endedAt"):
            return value
        return {
            **value,
            **({} if startedAt is None else {"startedAt": startedAt}),
            **({} if endedAt is None else {"endedAt": endedAt}),
        }

    def _taskEntry(self, id: int) -> _TransactionTask:
        task = self._tasksById.get(id)
        if task is None:
            task = _TransactionTask()
            self._tasksById[id] = task
        return task

    async def _currentTask(self, id: int) -> dict[str, Any] | None:
        """Latest candidate task record, falling back to committed state; not a caller table read."""
        entry = self._tasksById.get(id)
        if entry is not None and entry.write is not None:
            return entry.write["record"]
        return await self._committedTask(id)

    def _committedTask(self, id: int) -> asyncio.Future[Any]:
        task = self._taskEntry(id)
        if task.committedRead is None:
            task.committedRead = startTask(self._host.storage.task(id, self._context))
        return task.committedRead


def planDocument(document: _DocumentEntry) -> _DocumentPlan | None:
    """Plan of one staged document: its record, content write, and prepared change. Retirement is decided later."""
    target = document.target
    if target is None:
        return None
    kind = target["kind"]
    if kind == "created":
        prepared = document.prepared
        record, version, tracker = target["record"], target["version"], target["tracker"]
        plan = _DocumentPlan(document.addressId, record, document.retireOnCommit)
        content = {"version": version, "kind": "base", "value": prepared.value}
        plan.content = {"type": "document.create", "record": record, "content": content}
        plan.change = {"tracker": tracker, "prepared": prepared, "version": version}
        return plan
    if kind == "fork-copy":
        plan = _DocumentPlan(document.addressId, target["record"], document.retireOnCommit)
        plan.content = {"type": "document.copy", "record": target["record"], "source": target["source"]}
        return plan
    if kind == "retire-only":
        return _DocumentPlan(document.addressId, target["record"], document.retireOnCommit)
    loaded = target["document"]
    definition = document.definition
    assert definition is not None
    prepared = document.prepared
    version = definition["version"]
    id = loaded.record["id"]
    # A version change stores a base even without operations; otherwise only a change stores a delta.
    changeContent: dict[str, Any] | None
    if loaded.storedVersion < version:
        changeContent = {
            "type": "document.change",
            "id": id,
            "content": {"version": version, "kind": "base", "value": prepared.value},
        }
    elif len(prepared.ops) > 0:
        changeContent = {
            "type": "document.change",
            "id": id,
            "content": {"version": version, "kind": "delta", "ops": prepared.ops},
        }
    else:
        changeContent = None
    plan = _DocumentPlan(document.addressId, loaded.record, document.retireOnCommit)
    plan.change = {
        "tracker": loaded.tracker,
        "prepared": prepared,
        "version": version,
        "loaded": loaded,
        "definition": definition,
    }
    if changeContent is not None:
        plan.content = changeContent
    return plan


def publishes(plan: _DocumentPlan) -> bool:
    """Whether adoption publishes the plan: every creation, copy, and retirement, and a loaded incarnation that writes
    content, which includes a migration-only base so observers of the older shape receive the new value."""
    return plan.retire or plan.change is None or plan.change.get("loaded") is None or plan.content is not None


__all__ = [
    "LoadedDocument",
    "Transaction",
    "TransactionHost",
    "applySubmissionChange",
    "planDocument",
    "publishes",
]

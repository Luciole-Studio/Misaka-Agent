"""Public types of the durable runtime: records, documents, storage, sessions.

Translated from pi's ``packages/durable/src/types.ts`` (v1.3.0). IDs are plain integers (the nominal brand is erased), records
and definitions are plain ``dict`` objects with the upstream key names, a key that upstream leaves ``undefined`` is
absent from the dict, ``None`` is ``null``, and ``UNDEFINED`` (``misaka.utils.js``) is ``undefined`` where a value
(not a key) can be undefined. A document or task *token* is ``{"definition": definition}``; a definition is a ``dict``
whose ``initial``/``migrate``/``checkpointWhen`` (documents) or ``initial``/``phases``/``abort`` (tasks) entries are
callables. Promises are awaitables.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Literal, NotRequired, Protocol, TypedDict

from misaka.chord.context import Context
from misaka.chord.delta import Op

type JsonObject = dict[str, Any]
"""JSON object used as the root of every durable document."""


# --- The rest of types.ts: shapes this port passes as dicts, named as upstream names them. ---

type Id = int
"""``Id<Kind, Type>``: a number with a nominal brand; the brand is erased here."""

type ConversationId = int
type EntryId = int
type TaskId = int
type SubmissionId = int
type DocumentId = int
type Seq = int
"""Strictly increasing sequence assigned to one atomic storage commit; gaps are permitted."""

ROOT_CONVERSATION_ID: ConversationId = 1
"""The root conversation always uses this reserved ID."""

type LatestConversationSemantics = dict[str, Any]
"""``{"scope": "conversation", "history": "latest", "fork": "copy" | "fresh"}``."""
type RewindableConversationSemantics = dict[str, Any]
"""``{"scope": "conversation", "history": "rewindable", "fork": "copy" | "fresh"}``."""
type DocumentSemantics = dict[str, Any]
"""``{"scope": "session"}``, latest or rewindable conversation semantics, or ``{"scope": "task"}``."""


class CheckpointInfo(TypedDict):
    """Stored replay state supplied to a document's checkpoint predicate."""

    deltasSinceBase: int  # Deltas already stored after the newest base, excluding the change being evaluated.
type CommonDocDefinition = dict[str, Any]
"""``kind`` (stable persisted kind), ``version``, ``initial()``, and optional ``migrate``/``checkpointWhen``."""
type DocDefinition = dict[str, Any]
"""A common definition with its semantics."""
type DocFamilyDefinition = dict[str, Any]
"""Keyed document family definition; ``initial(seed)`` runs only when a member is absent; ``family`` is true."""

type DocToken = dict[str, Any]
"""``{"definition": definition}``: a typed singleton or family document token passed explicitly to typed access."""
type DocFamilyToken = dict[str, Any]
"""``{"definition": familyDefinition}``."""
type SessionDocToken = DocToken
type ConversationDocToken = DocToken
type RewindableConversationDocToken = DocToken
type TaskDocToken = DocToken
type SessionDocFamilyToken = DocFamilyToken
type ConversationDocFamilyToken = DocFamilyToken
type RewindableConversationDocFamilyToken = DocFamilyToken
type TaskDocFamilyToken = DocFamilyToken
type RunningTask = dict[str, Any]
"""A task record whose state is running."""
type NextTaskState = TaskState
"""A task state a phase may move to: running, waiting, or terminal."""
type PhaseHandler = Callable[..., Any]
"""``(task: RunningTask, runtime: TaskRuntime, context: Context) -> NextTaskState | Awaitable[NextTaskState]``."""


class HookRunner(Protocol):
    def each(self, name: str, invoke: Callable[[Any], Any]) -> Awaitable[None]:
        """Call ``invoke`` with each matching handler named ``name``. An ordinary error from ``invoke`` is reported and
        the next handler runs; once the invocation is signalled, the error propagates."""
        ...
type TaskDefinition = dict[str, Any]
"""``name`` (registered kind persisted in ``TaskRecord.kind``), ``initial``, ``phases``, optional ``abort``."""
type Task = dict[str, Any]
"""``{"definition": taskDefinition}``."""
type TaskOwnership = dict[str, Any]
"""``{"kind": "conversation"}`` or ``{"kind": "task", "taskId"}``."""
type JoinPolicy = Literal["failFast", "allSettled"]
"""How a waiting task treats the tasks it waits on (spec section 5.5)."""
type TaskOptions = dict[str, Any]
"""``ownership`` (required: a task always names its owner), and optional ``conversationId``/``join``, ``background``
(conversation-owned tasks only: excluded from ordinary idle waits, conversation aborts, and cascades) and
``abandonOnRestart`` (its creator awaits it only in memory and does not resume after a restart, so it is pointless once
the Harness that created it is gone. When scheduling starts in a later Harness, the task, if still live, is abort-marked
with reason ``restart``, and the mark cascades to its ordinary owned work, before any of it runs again)."""
type ConversationOwnership = dict[str, Any]
"""``{"kind": "ownerless"}`` or ``{"kind": "task", "taskId"}``."""
type ConversationRecord = dict[str, Any]

type ContextEdit = dict[str, Any]
"""``{"target": EntryId, ...}``: an entry whose model messages are omitted or replaced."""
type EntryRecord = dict[str, Any]
type EntryDraft = dict[str, Any]
"""An entry record without ``id``/``conversationId``/``byTaskId``; ``head`` may be ``"self"``."""
type TypedEntry = EntryRecord
type TypedEntryDraft = EntryDraft


class Entry(Protocol):
    """A typed entry kind (``misaka.durable.entries.Entry``); ``is`` is ``is_`` here, a keyword in Python."""

    kind: str

    def is_(self, entry: EntryRecord | None) -> bool: ...
type SubmissionRecord = dict[str, Any]

type SubmissionSettlement = dict[str, Any]
"""``{"status": "done", "answer": EntryId}`` or ``{"status": "unanswered", "reason", "detail"?}``."""
type SubmissionCreate = dict[str, Any]
"""A submission record without its ``id``."""
type TaskOutcomeError = dict[str, Any]
"""``{"message": str, "data"?: JsonValue}``."""
type TaskOutcome = dict[str, Any]
"""``{"status": "completed", "result"}``, ``{"status": "failed", "error"}``, or ``{"status": "aborted"}``."""

type TaskState = dict[str, Any]
"""``{"status": "ready" | "running" | "waiting" | "terminal", ...}``: the scheduling state of a task, with its
phase state, waits, or outcome."""
type TaskRecord = dict[str, Any]
"""``startedAt``: wall-clock milliseconds of the first change to ``running``, stamped by the Session; kept through waits
and recovery, so the span to ``endedAt`` includes them; absent before the task first runs, and on records written by
earlier versions. ``endedAt``: wall-clock milliseconds of the change to ``terminal``, stamped by the Session; absent
while live. ``abortReason``: why the mark was set when not by an abort request: ``restart`` for a task abandoned after a
restart (``TaskOptions.abandonOnRestart``) or below one; such a task waits for its definition instead of becoming
``orphaned``, and a later abort request clears it. ``abandonOnRestart``: ``True`` from ``TaskOptions.abandonOnRestart``;
immutable."""
type DocumentRecord = dict[str, Any]
type DocumentCreate = dict[str, Any]
"""A document record without ``createdAt``/``retiredAt``."""
type Page = dict[str, Any]
"""One ordered scan result: ``{"items": [...], "next": cursor}``; ``next`` is absent on the last page."""
type Cursor = dict[str, Any]
"""Backend-owned JSON continuation state that callers only round-trip to the same scan. It carries the scan's order: a
scan given a cursor continues in that order, and rejects a different ``order`` in its query."""

type ScanOrder = Literal["ascending", "descending"]
"""ID order of a scan: ``ascending`` is oldest first, ``descending`` newest first."""
"""Backend-owned JSON continuation state that callers only round-trip to the same scan."""

type ConversationQuery = dict[str, Any]
"""Optional ``ownerConversationId``/``ownerTaskId``/``order``/``after``/``limit``."""
type EntryQuery = dict[str, Any]
"""``conversationId`` and optional bounds, ``kinds``, ``order``, ``after``, ``limit``."""
type TaskQuery = dict[str, Any]
"""Optional ``conversationId``/``kind``/``status``/``order``/``after``/``limit``."""
type SubmissionQuery = dict[str, Any]
"""Optional ``conversationId``/``status``/``order``/``after``/``limit``."""
type DocumentPoint = int | Literal["current"]
"""Current state or one historical commit sequence used for document membership and content reads."""


type DocumentAddress = dict[str, Any]
"""Exact logical identity of a singleton (no ``key``) or one keyed family member: ``{"kind", "scope", "key"?}``."""
type DocumentQuery = dict[str, Any]
"""``scope``, ``at`` (a document point), optional ``kind``/``order``/``after``/``limit``."""
type DocumentContent = dict[str, Any]
"""``{"version", "content"}`` of a stored document read."""
type DocumentCopySource = dict[str, Any]
"""Exact persisted source selected for a definition-free document copy: ``{"id", "at"}``."""
type StoredDocument = dict[str, Any]
"""Detached materialized value and stored definition version at a selected point: ``{"record", "version", "value",
"deltasSinceBase"}`` (the number of deltas replayed after the selected base)."""
type StorageWrite = dict[str, Any]
type DocumentCommitChange = dict[str, Any]
"""``{"type": "document", ...}``: one document change of a commit."""

type TableCommitChange = StorageWrite
"""A conversation, entry, task, or submission write of one commit."""
type CommitChange = TableCommitChange | DocumentCommitChange
"""Every immutable change from one successful Session commit. Change order is unspecified."""


class CommitPublication(TypedDict):
    """Every immutable change from one successful Session commit. Change order is unspecified."""

    seq: Seq
    changes: Sequence[dict[str, Any]]


class Tx(Protocol):
    """Transaction surface of one Session commit callback. Table reads and creation results are trusted immutable
    values and may be shared with internal commit state. Overloads are positional: ``entry(id)`` or
    ``entry(token, id)``; ``appendEntry(conversationId, draft)`` or ``appendEntry(token, conversationId, draft)``;
    ``doc(token, *owner_and_key_and_seed)`` and ``retireDoc(token, *owner_and_key)`` take the owner ID of the token's
    scope (none for a session document), then the family key and seed."""

    def conversation(self, id: ConversationId) -> Awaitable[ConversationRecord | None]: ...

    def entry(self, *args: Any) -> Awaitable[EntryRecord | None]: ...

    def task(self, id: TaskId) -> Awaitable[TaskRecord | None]: ...

    def scanConversations(self, query: dict[str, Any], limit: int, cursor: Cursor | None = None) -> Awaitable[Page]: ...

    def scanEntries(self, query: dict[str, Any], limit: int, cursor: Cursor | None = None) -> Awaitable[Page]: ...

    def latestHeadMarker(self, conversationId: ConversationId) -> Awaitable[EntryRecord | None]: ...

    def scanTasks(self, query: dict[str, Any], limit: int, cursor: Cursor | None = None) -> Awaitable[Page]: ...

    def submission(self, id: SubmissionId) -> Awaitable[SubmissionRecord | None]: ...

    def submissionByRequest(self, conversationId: ConversationId, requestId: str) -> Awaitable[SubmissionRecord | None]: ...

    def createConversation(self, options: dict[str, Any]) -> Awaitable[ConversationRecord]: ...

    def forkConversation(self, parentConversationId: ConversationId, at: EntryId, options: dict[str, Any]) -> Awaitable[ConversationRecord]: ...

    def appendEntry(self, *args: Any) -> Awaitable[EntryRecord]: ...

    def createTask(self, task: dict[str, Any], input: Any, options: dict[str, Any]) -> Awaitable[TaskId]: ...

    def createSubmission(self, create: dict[str, Any]) -> Awaitable[SubmissionRecord]: ...

    def settleSubmission(self, id: SubmissionId, settlement: dict[str, Any]) -> None: ...

    def placeSubmission(self, id: SubmissionId, entry: EntryId) -> None: ...

    def doc(self, token: DocToken, *args: Any) -> Awaitable[Any]: ...

    def retireDoc(self, token: DocToken, *args: Any) -> Awaitable[None]: ...

type DocumentState = Any
"""``AttachedReplicatedState<Readonly<T> | null>``: the replicated state of one watched document."""


class WatchEnd(TypedDict):
    """Terminal result of one document watch: ``reason`` is ``stopped``, ``cancelled``, ``session_closed``,
    ``retired``, ``listener_error`` (with the ``error``), or ``session_failed`` (the Session failed; ``error`` is what
    failed it, usually a storage error, see ``SessionFailed``)."""

    reason: str
    error: NotRequired[Any]


class SessionEnd(TypedDict):
    """Why a Session ended: ``{"reason": "closed"}`` after ``close()``, or ``{"reason": "failed", "error"}`` after a
    failed Storage call, whose error it carries."""

    reason: Literal["closed", "failed"]
    error: NotRequired[Any]


class WatchHandle(Protocol):
    """Serialized exact-frame observation of an immutable value with bounded pending delivery."""

    @property
    def value(self) -> Any:
        """Acquisition revision before start; latest delivered immutable revision afterward."""
        ...

    def start(self, listener: Callable[[Any, Sequence[Op], Context], Awaitable[None]]) -> None:
        """Install the sole asynchronous listener. Never invokes it inline."""
        ...

    def stop(self) -> Awaitable[WatchEnd]:
        """Idempotently stop future callbacks and return this watch's terminal result."""
        ...

    @property
    def closed(self) -> Awaitable[WatchEnd]:
        """Settles when the watch terminates; an already-running callback remains caller-owned."""
        ...
type DocumentWatch = WatchHandle
"""``WatchHandle<Readonly<T> | null>``."""


class DocumentReader(Protocol):
    """Committed document reads: ``Pick<Session, "snapshot" | "snapshotAsOf">``."""

    def snapshot(self, token: DocToken, *args: Any) -> Awaitable[Any]: ...

    def snapshotAsOf(self, token: DocToken, *args: Any) -> Awaitable[Any]: ...


class DocumentObserver(Protocol):
    """Non-creating document watch acquisition shared by Session and later invocation APIs: ``watchDoc(token,
    context)`` for session documents, ``watchDoc(token, conversationId | taskId, context)`` otherwise."""

    def watchDoc(self, token: DocToken, *args: Any) -> Awaitable[Any]: ...


class TaskRuntime(DocumentObserver, DocumentReader, Protocol):
    """Operations of one task invocation. Every operation rejects after the invocation ends; watches acquired through
    it stop at invocation end."""

    taskId: TaskId
    conversationId: ConversationId
    signal: Any
    registry: Any
    settings: Any
    models: Any
    hooks: HookRunner

    def agent(self, context: Context) -> Awaitable[Any]: ...

    def env(self, context: Context) -> Awaitable[Any]: ...

    def commit(self, change: Callable[[Tx, RunningTask], Any], context: Context) -> Awaitable[None]:
        """Commit on the Session line after rereading the task. Inside the callback read only through ``tx``:
        ``snapshot()``, ``documentState()``, ``watchDoc()``, ``getTask()``, and the other Session and runtime reads may
        queue on the line the callback holds, and then never resolve."""
        ...

    def memo(self, name: str, *args: Any) -> Awaitable[Any]: ...

    def getTask(self, id: TaskId, context: Context) -> Awaitable[TaskRecord | None]: ...

    def waitForTask(self, id: TaskId, context: Context) -> Awaitable[Any]: ...

    def abortOwned(self, id: TaskId, context: Context) -> Awaitable[None]:
        """Abort a task this task owns, as ``Harness.abortTask()`` does, and resolve once it is terminal. Rejects for a
        task another owns; resolves at once for a terminal one."""
        ...

    def ownedTasks(self, context: Context) -> Awaitable[list[TaskRecord]]:
        """Live tasks this task owns directly, in ID order: also those an earlier invocation created, before a restart.
        Tasks in conversations it owns are not included. Copies of the committed records."""
        ...

    def outcomes(self, ids: Sequence[TaskId], context: Context) -> Awaitable[list[TaskOutcome]]: ...

    def conversation(self, id: ConversationId, context: Context) -> Awaitable[Any]: ...

    def entry(self, *args: Any) -> Awaitable[EntryRecord | None]: ...

    def context(self, conversationId: ConversationId, context: Context, options: Any = None) -> Awaitable[Any]: ...

    def now(self) -> float: ...

    def report(self, error: Any) -> None: ...

    def sleep(self, until: float, context: Context) -> Awaitable[None]: ...


class Session(Protocol):
    """Owner of one mutation line, its records, and its tracked documents."""

    def commit(self, change: Callable[[Tx], Any], context: Context) -> Awaitable[Any]:
        """Run one atomic transaction on the Session mutation line. Inside the callback read only through ``tx``:
        ``snapshot()``, ``documentState()``, ``watchDoc()``, ``getTask()``, and the other Session and runtime reads may
        queue on the line the callback holds, and then never resolve."""
        ...

    def close(self, context: Context) -> Awaitable[None]:
        """Seal admission, settle admitted commits, then close storage."""
        ...

    @property
    def closed(self) -> Awaitable[SessionEnd]:
        """Settles once the Session has closed, Storage included: after ``close()``, or after the first failed Storage
        call, which closes the Session itself. A failed Session is reopened from Storage; nothing else is left to clean
        up."""
        ...

    def subscribeCommits(self, listener: Callable[[CommitPublication, Context], None]) -> Callable[[], None]:
        """Observe complete commits synchronously after adoption. The listener must not block or call Session APIs."""
        ...

    def subscribeClose(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Observe close synchronously when it begins, also when a failed Storage call closes the Session. The listener
        must not block or call Session APIs."""
        ...

    def snapshot(self, token: DocToken, *args: Any) -> Awaitable[Any]: ...

    def documentState(self, token: DocToken, *args: Any) -> Awaitable[Any]: ...

    def snapshotAsOf(self, token: DocToken, *args: Any) -> Awaitable[Any]: ...

    def watchDoc(self, token: DocToken, *args: Any) -> Awaitable[Any]: ...


class Storage(Protocol):
    """Atomic persistence boundary for Session records.

    Storage trusts the owning Session to supply semantically valid records, references, ancestry, and transitions.
    Implementations enforce atomicity, global ID ownership, immutable conversation/entry creation, document record
    consistency, and detached values; Session serializes commits.

    An error a method throws is final: it fails the Session, which nothing retries, so retry transient failures inside
    the method. Two errors of a read fail only that read: a ``StorageRequestError`` for an invalid request (an unknown
    conversation, a foreign or malformed cursor, history a document does not keep), which must have no durable effect,
    and a rejection because its caller's context was aborted. Any error from ``commit()`` or ``mintId()`` is fatal."""

    def commit(self, writes: Sequence[StorageWrite], context: Context) -> Awaitable[Seq]:
        """Atomically persist one batch and return its sequence. Once resolved, later reads through this storage
        observe it."""
        ...

    def mintId(self) -> Awaitable[int]:
        """Return a fresh candidate from the Session-global numeric ID namespace, greater than every ID minted or stored
        before, also across reopen: scans order by ID as creation order, and the Harness relies on it."""
        ...

    def conversation(self, id: ConversationId, context: Context) -> Awaitable[ConversationRecord | None]: ...

    def scanConversations(self, query: dict[str, Any], limit: int, cursor: Cursor | None, context: Context) -> Awaitable[Page]: ...

    def entry(self, *args: Any) -> Awaitable[dict[str, Any] | None]:
        """``entry(id, context)`` or ``entry(conversationId, id, context)``; resolves ``{"entry": ..., "commitSeq": ...}``."""
        ...

    def findLatestHeadMarker(self, conversationId: ConversationId, atOrBeforeEntryId: EntryId | None, context: Context) -> Awaitable[EntryRecord | None]: ...

    def scanEntries(self, query: dict[str, Any], limit: int, cursor: Cursor | None, context: Context) -> Awaitable[Page]: ...

    def task(self, id: TaskId, context: Context) -> Awaitable[TaskRecord | None]: ...

    def scanTasks(self, query: dict[str, Any], limit: int, cursor: Cursor | None, context: Context) -> Awaitable[Page]: ...

    def submission(self, id: SubmissionId, context: Context) -> Awaitable[SubmissionRecord | None]: ...

    def scanSubmissions(self, query: dict[str, Any], limit: int, cursor: Cursor | None, context: Context) -> Awaitable[Page]: ...

    def submissionByRequest(self, conversationId: ConversationId, requestId: str, context: Context) -> Awaitable[SubmissionRecord | None]: ...

    def findDocument(self, address: DocumentAddress, at: DocumentPoint, context: Context) -> Awaitable[DocumentRecord | None]: ...

    def document(self, id: DocumentId, at: DocumentPoint, context: Context) -> Awaitable[StoredDocument | None]: ...

    def scanDocuments(self, query: dict[str, Any], limit: int, cursor: Cursor | None, context: Context) -> Awaitable[Page]: ...

    def close(self, context: Context) -> Awaitable[None]: ...


__all__ = [
    "ROOT_CONVERSATION_ID",
    "CheckpointInfo",
    "CommitChange",
    "CommitPublication",
    "CommonDocDefinition",
    "ContextEdit",
    "ConversationDocFamilyToken",
    "ConversationDocToken",
    "ConversationId",
    "ConversationOwnership",
    "ConversationQuery",
    "ConversationRecord",
    "Cursor",
    "DocDefinition",
    "DocFamilyDefinition",
    "DocFamilyToken",
    "DocToken",
    "DocumentAddress",
    "DocumentCommitChange",
    "DocumentContent",
    "DocumentCopySource",
    "DocumentCreate",
    "DocumentId",
    "DocumentObserver",
    "DocumentPoint",
    "DocumentQuery",
    "DocumentReader",
    "DocumentRecord",
    "DocumentSemantics",
    "DocumentState",
    "DocumentWatch",
    "Entry",
    "EntryDraft",
    "EntryId",
    "EntryQuery",
    "EntryRecord",
    "HookRunner",
    "Id",
    "JoinPolicy",
    "JsonObject",
    "LatestConversationSemantics",
    "NextTaskState",
    "Page",
    "PhaseHandler",
    "RewindableConversationDocFamilyToken",
    "RewindableConversationDocToken",
    "RewindableConversationSemantics",
    "RunningTask",
    "Seq",
    "Session",
    "SessionDocFamilyToken",
    "SessionDocToken",
    "SessionEnd",
    "Storage",
    "StorageWrite",
    "StoredDocument",
    "SubmissionCreate",
    "SubmissionId",
    "SubmissionQuery",
    "SubmissionRecord",
    "SubmissionSettlement",
    "TableCommitChange",
    "Task",
    "TaskDefinition",
    "TaskDocFamilyToken",
    "TaskDocToken",
    "TaskId",
    "TaskOptions",
    "TaskOutcome",
    "TaskOutcomeError",
    "TaskOwnership",
    "TaskQuery",
    "TaskRecord",
    "TaskRuntime",
    "TaskState",
    "Tx",
    "TypedEntry",
    "TypedEntryDraft",
    "WatchEnd",
    "WatchHandle",
]

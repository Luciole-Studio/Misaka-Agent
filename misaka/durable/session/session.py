"""The Session kernel: one mutation line, the loaded document tracker cache, and committed publication.

Translated from pi's ``packages/durable/src/session/session.ts`` (v1.3.0). Public async methods are ``@eager``: calling them
starts the body, as calling a JavaScript async function does, and returns the future of the result. The overloaded
document accessors take their arguments positionally: the owner ID of the token's scope (none for a session
document), the family key, and finally the context (``snapshotAsOf`` takes the entry ``at`` before it).
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from typing import Any

from misaka.chord import replicatedState
from misaka.chord._promise import isRejection
from misaka.chord.context import (
    BACKGROUND_CONTEXT,
    Context,
    awaitWithContext,
    withoutAbortSignal,
)
from misaka.chord.delta import Op, track
from misaka.utils.js import (
    UNDEFINED,
    DOMException,
    Timeout,
    allSettled,
    awaitValue,
    clearTimeout,
    eager,
    jsIsSafeInteger,
    queueMicrotask,
    rejectedPromise,
    setTimeout,
    startTask,
)

from ..documents import (
    AnyDocDefinition,
    AnyDocToken,
    addressId,
    checkRecordScope,
    checkRecordVersion,
    materializeDocument,
    resolveAddress,
)
from ..errors import SessionFailed, StorageRequestError
from ..types import ConversationId, Storage
from .observation import RETIREMENT_OPERATIONS, CommittedStateSource, CommittedWatch
from .transaction import LoadedDocument, Transaction, TransactionHost


def _dateNow() -> float:
    return int(time.time() * 1000)


def createSession(storage: Storage, options: dict[str, Any] | None = None) -> SessionImpl:
    """Open a Session kernel over one storage backend. ``now`` is the wall clock for task times; default ``Date.now``.
    The first error a Storage method throws fails the Session (``SessionFailed``); see ``SessionImpl``."""
    retention = None if options is None else options.get("documentRetentionMs")
    return SessionImpl(
        storage, None if options is None else options.get("now"), None if retention is None else (lambda: retention)
    )


# Default document retention: ten minutes, as for kept contexts.
_defaultRetention: float = 600_000


def setDefaultDocumentRetention(ms: float) -> None:
    """Internal, for tests: the retention of every Session that keeps the default, as a suite run with eviction at
    once."""
    global _defaultRetention
    _defaultRetention = ms


def documentRetention(ms: Any) -> float:
    """A retention as given, when it is a number of milliseconds, ``0`` or more (``Infinity`` keeps documents);
    otherwise the default."""
    return ms if isinstance(ms, int | float) and not isinstance(ms, bool) and ms >= 0 else _defaultRetention


MAX_TIMER_DELAY = 2_147_483_647
"""Longest delay ``setTimeout`` supports; a longer retention waits in several steps."""


class _DocumentSweep:
    """The pending eviction: a timer for the oldest idle document, or a line job when the retention is 0."""

    __slots__ = ("at", "timer")

    def __init__(self, at: float) -> None:
        self.at = at
        self.timer: Timeout | None = None


class SessionImpl:
    """Session kernel: one mutation line, the loaded document tracker cache, and committed publication.

    The cache is bounded by use: a document with no watch or document state attached leaves it once it was not read or
    written for the retention period, and the next access loads it from Storage. Eviction runs as a job on the mutation
    line, so no transaction ever holds a tracker that leaves.

    Only committed state is observable. Every commit callback, preparation, Storage settlement, adoption, and
    publication enqueue runs while the line is held; listeners run later.

    Storage failures are final. Every Storage call goes through one guard: the first error a Storage method throws,
    other than for a cancelled caller, fails the Session, as does a commit it cannot adopt. That call gets the error;
    every later call and every Storage call still underway gets ``SessionFailed``; close listeners run at once with the
    error, and the Session then closes itself, Storage included, and ``closed`` settles. Nothing retries: transient
    errors are the Storage's to retry. Reopening recovers from what was committed."""

    def __init__(
        self,
        storage: Storage,
        now: Callable[[], float] | None = None,
        documentRetentionMs: Callable[[], Any] | None = None,
    ) -> None:
        if now is None:
            now = _dateNow
        if documentRetentionMs is None:
            documentRetentionMs = lambda: None
        self._documents: dict[str, LoadedDocument] = {}
        # Cached documents with no observer attached, by address, oldest use first, with the time of that use.
        self._idleDocuments: dict[str, float] = {}
        # Observers attached per document incarnation (record ID); a cached incarnation with any is never evicted.
        self._attachedDocuments: dict[int, int] = {}
        # The pending eviction: a timer for the oldest idle document, or a line job when the retention is 0.
        self._documentSweep: _DocumentSweep | None = None
        self._sweepJobs = 0
        # Set once a timer could not be unreferenced: then none is kept, and the end of each commit evicts what expired.
        # MISAKA fork: an asyncio timer never keeps the process alive, as an unreferenced Node timer does, so every
        # runtime misaka runs in can keep one and this stays false.
        self._documentTimersUnavailable = False
        # Sets, as upstream's: a listener subscribed twice is one listener, and one unsubscribe removes it. A dict keeps
        # the insertion order a JavaScript Set iterates in.
        self._commitListeners: dict[Callable[[dict[str, Any], Context], None], None] = {}
        # The Session's own listeners, such as its scheduler's: they keep memory in step with storage, so a throw fails
        # it.
        self._internalListeners: dict[Callable[[dict[str, Any], Context], None], None] = {}
        self._closeListeners: dict[Callable[[], None], None] = {}
        self._tail: asyncio.Future[None] | None = None
        self._closing: asyncio.Future[None] | None = None
        self._failure: dict[str, Any] | None = None
        # `Promise.withResolvers()`, made on first use: a Session may be built before its event loop runs. `_failed`
        # rejects with `SessionFailed` the moment the Session fails; `_closed` settles with the `SessionEnd`.
        self._failedFuture: asyncio.Future[Any] | None = None
        # What `fail()` rejected `_failed` with, for a future made after it: a backend close failure records a failure
        # without `fail()`, and upstream's `#failed` (made in the constructor) never rejects then.
        self._failedWith: SessionFailed | None = None
        self._closedFuture: asyncio.Future[dict[str, Any]] | None = None
        self._storage: Storage = GuardedStorage(storage, self)
        nowFunction = now
        retentionFunction = documentRetentionMs

        # Cache bookkeeping must never change the outcome of the operation it follows: a clock or a retention setting
        # that throws counts as now, and as the default.
        def clock() -> float:
            try:
                return nowFunction()
            except Exception:  # noqa: BLE001
                return _dateNow()

        def retentionMs() -> float:
            try:
                return documentRetention(retentionFunction())
            except Exception:  # noqa: BLE001
                return documentRetention(None)

        self._clock = clock
        self._documentRetentionMs = retentionMs

        def cached(id: str) -> LoadedDocument | None:
            current = self._documents.get(id)
            if current is not None:
                self._touchDocument(id)
            return current

        def install(document: LoadedDocument) -> None:
            self._documents[document.addressId] = document
            self._touchDocument(document.addressId)

        def evict(id: str, recordId: int) -> None:
            current = self._documents.get(id)
            if current is not None and current.record["id"] == recordId:
                self._dropDocument(id)

        self._host = TransactionHost(
            self._storage,
            now,
            cached,
            lambda definition, addressId, address, context: self._loadDocument(definition, addressId, address, context),
            install,
            evict,
            lambda tx, record: self.conversationCreated(tx, record),
        )

    def _failedPromise(self) -> asyncio.Future[Any]:
        if self._failedFuture is None:
            self._failedFuture = asyncio.get_running_loop().create_future()
            # Waits race it; none has to.
            self._failedFuture.add_done_callback(_observe)
            if self._failedWith is not None:
                self._failedFuture.set_exception(self._failedWith)
        return self._failedFuture

    def _closedPromise(self) -> asyncio.Future[dict[str, Any]]:
        if self._closedFuture is None:
            self._closedFuture = asyncio.get_running_loop().create_future()
        return self._closedFuture

    @property
    def closed(self) -> asyncio.Future[dict[str, Any]]:
        return self._closedPromise()

    @property
    def failure(self) -> dict[str, Any] | None:
        """Internal: the error that failed the Session, if one did (``{"error": ...}``)."""
        return self._failure

    @property
    def failed(self) -> asyncio.Future[Any]:
        """Internal: rejects with ``SessionFailed`` the moment the Session fails, before work underway has ended; never
        resolves."""
        return self._failedPromise()

    @property
    def storage(self) -> Storage:
        """The Storage behind the failure guard; every component reads and writes through it."""
        return self._storage

    def commit(self, change: Callable[[Transaction], Any], context: Context) -> asyncio.Future[Any]:
        return self.commitWith(change, context)

    def fail(self, error: Any) -> None:
        """Internal: fail the Session with ``error``, the first wins, and report it once. Admission ends, and close
        listeners run now; they read the error from ``failure``. The Storage guard calls this; the Harness also calls it
        for a throw in its scheduler's own commits."""
        if self._failure is not None:
            return
        self._failure = {"error": error}
        self._failedWith = SessionFailed(error)
        if self._failedFuture is not None and not self._failedFuture.done():
            self._failedFuture.set_exception(self._failedWith)
        # Seal first, so a report handler that calls back finds the Session failed. Closing runs the close listeners
        # synchronously; a failing backend close is the caller's to see, and here nobody waits.
        _observe(self.close(BACKGROUND_CONTEXT))
        self.report(error)

    def commitWith(
        self, change: Callable[[Transaction], Any], context: Context, scope: dict[str, Any] | None = None
    ) -> asyncio.Future[Any]:
        """Internal commit exposing the concrete transaction and its internal operations, such as the reserved-ID root
        bootstrap and task replacement. ``scope`` sets the default ``tx.createTask()`` conversation and the task
        attributed to appended entries."""
        try:
            self.assertUsable()
        except Exception as error:  # noqa: BLE001
            return rejectedPromise(error)
        return self._enqueue(lambda: self._runCommit(change, context, scope))

    def readOnLine(self, job: Callable[[], Awaitable[Any]]) -> asyncio.Future[Any]:
        """Internal: run a read-only job on the mutation line so multi-read derivations observe one committed state."""
        try:
            self.assertUsable()
        except Exception as error:  # noqa: BLE001
            return rejectedPromise(error)

        async def run() -> Any:
            self._assertHealthy()
            return await job()

        return self._enqueue(run)

    async def conversationDocumentOnLine(
        self, token: AnyDocToken, conversationId: ConversationId, context: Context
    ) -> dict[str, Any] | None:
        """Internal: a conversation document's current incarnation and value, for a job already running on the line (see
        ``readOnLine()``). Absent documents are ``None``."""
        definition = token["definition"]
        resolved = resolveAddress(definition, [conversationId, context])
        loaded = await self._loadDocument(definition, resolved.id, resolved.address, context)
        if loaded is None:
            return None
        checkRecordScope(definition, loaded.record)
        checkRecordVersion(definition, loaded.record, loaded.storedVersion)
        return {"record": loaded.record, "version": loaded.valueVersion, "value": loaded.tracker.value}

    @eager
    async def snapshot(self, token: AnyDocToken, *args: Any) -> Any:
        self.assertUsable()
        definition = token["definition"]
        resolved = resolveAddress(definition, args)
        context = args[resolved.nextArgument]
        cached = self._documents.get(resolved.id)
        if cached is not None and cached.valueVersion == definition["version"]:
            self._touchDocument(resolved.id)
        if cached is not None and cached.valueVersion == definition["version"]:
            loaded: LoadedDocument | None = cached
        else:

            async def load() -> LoadedDocument | None:
                self._assertHealthy()
                return await self._loadDocument(definition, resolved.id, resolved.address, context)

            loaded = await self._enqueue(load)
        if loaded is None:
            return None
        checkRecordScope(definition, loaded.record)
        checkRecordVersion(definition, loaded.record, loaded.storedVersion)
        return loaded.tracker.value

    def documentState(self, token: AnyDocToken, *args: Any) -> asyncio.Future[Any]:
        try:
            self.assertUsable()
            definition = token["definition"]
            resolved = resolveAddress(definition, args)
            context = args[resolved.nextArgument]

            async def attach() -> Any:
                self._assertHealthy()
                loaded = await self._loadDocument(definition, resolved.id, resolved.address, context)
                if loaded is None:
                    return None
                source, detach = self._attachDocument(
                    definition, loaded, lambda value, release: CommittedStateSource(value, release)
                )
                try:
                    return replicatedState(source, {"onError": lambda error: self.report(error)})
                except Exception:
                    detach()
                    raise

            return self._enqueue(attach)
        except Exception as error:  # noqa: BLE001
            return rejectedPromise(error)

    @eager
    async def watchDoc(self, token: AnyDocToken, *args: Any) -> Any:
        self.assertUsable()
        definition = token["definition"]
        resolved = resolveAddress(definition, args)
        context = args[resolved.nextArgument]
        signal = context.abortSignal
        cancelled = bool(signal.aborted) if signal is not None else False

        def markCancelled() -> None:
            nonlocal cancelled
            cancelled = True

        if signal is not None:
            signal.addEventListener("abort", markCancelled, once=True)
        try:

            async def attach() -> Any:
                self._assertHealthy()
                if cancelled:
                    raise cancellationError(signal)
                loaded = await self._loadDocument(definition, resolved.id, resolved.address, context)
                if cancelled:
                    raise cancellationError(signal)
                if loaded is None:
                    return None
                return self._attachDocument(
                    definition,
                    loaded,
                    lambda value, release: CommittedWatch(value, release, lambda error: self.report(error)),
                )[0]

            watch = await self._enqueue(attach)
            if watch is None:
                return None
            if cancelled:
                watch.cancel()
                raise cancellationError(signal)
            if signal is not None:
                watch.observeCancellation(signal)
            return watch
        finally:
            if signal is not None:
                signal.removeEventListener("abort", markCancelled)

    @eager
    async def snapshotAsOf(self, token: AnyDocToken, *args: Any) -> Any:
        self.assertUsable()
        definition = token["definition"]
        resolved = resolveAddress(definition, args)
        if resolved.address["scope"]["kind"] != "conversation":
            raise TypeError("Session.snapshotAsOf() requires a conversation document")
        conversationId = resolved.address["scope"]["conversationId"]
        at = args[resolved.nextArgument]
        if not jsIsSafeInteger(at):
            raise TypeError("Session.snapshotAsOf() requires an entry ID")
        at = int(at)
        context = args[resolved.nextArgument + 1]

        async def read() -> Any:
            self._assertHealthy()
            storedEntry = await self._storage.entry(conversationId, at, context)
            if storedEntry is None:
                raise RuntimeError(f"Entry {at} is not visible from conversation {conversationId}")
            address = {
                **resolved.address,
                "scope": {"kind": "conversation", "conversationId": storedEntry["entry"]["conversationId"]},
            }
            record = await self._storage.findDocument(address, storedEntry["commitSeq"], context)  # type: ignore[arg-type]
            if record is None:
                return None
            stored = await self._storage.document(record["id"], storedEntry["commitSeq"], context)
            if stored is None:
                raise RuntimeError(f"Historical document {record['id']} ({record['kind']}) cannot be read")
            return materializeDocument(definition, stored)

        return await self._enqueue(read)

    def close(self, context: Context) -> asyncio.Future[None]:
        if self._closing is None:
            cleanup = withoutAbortSignal(context)
            # Seal admission before anything else runs, then stop observers; admitted work settles before Storage closes.
            self._closing = startTask(self._closeSequence(cleanup))
            self._notifyClose()
        # Shielded: a caller that gives up waiting must not cancel the close the others share.
        return startTask(awaitWithContext(asyncio.shield(self._closing), context))

    async def _closeSequence(self, cleanup: Context) -> None:
        try:
            await asyncio.sleep(0)
            await self.beforeClose()

            async def shutdown() -> None:
                self._commitListeners.clear()
                self._internalListeners.clear()
                self._documents.clear()
                self._idleDocuments.clear()
                self._attachedDocuments.clear()
                try:
                    await self._storage.close(cleanup)
                except Exception as error:
                    # A Storage that cannot close is a failed one; an earlier failure stays the cause.
                    if self._failure is None:
                        self._failure = {"error": error}
                        self.report(error)
                    raise

            await self._enqueue(shutdown)
        finally:
            failure = self._failure
            closed = self._closedPromise()
            if not closed.done():
                closed.set_result({"reason": "closed"} if failure is None else {"reason": "failed", "error": failure["error"]})

    def _notifyClose(self) -> None:
        """Run the close listeners once, each on its own: one that fails is reported, and the rest still run."""
        if self._documentSweep is not None and self._documentSweep.timer is not None:
            clearTimeout(self._documentSweep.timer)
        self._documentSweep = None
        listeners = list(self._closeListeners)
        self._closeListeners.clear()
        for listener in listeners:
            contained(listener, lambda error: self.report(error))

    def report(self, _error: Any) -> None:
        """Internal: the Session's failure, and errors of listeners, which never fail what ran them. A plain Session
        drops them."""

    def conversationCreated(self, _tx: Transaction, _record: dict[str, Any]) -> Awaitable[None]:
        """Runs inside every transaction that creates or forks a conversation, after the conversation record is staged.
        A plain Session stages nothing; a Harness stages its built-in documents."""

        async def nothing() -> None:
            pass

        return nothing()

    def beforeClose(self) -> Awaitable[None]:
        """Runs after close seals admission and before the line closes Storage; must not reject."""

        async def nothing() -> None:
            pass

        return nothing()

    def subscribeCommits(self, listener: Callable[[dict[str, Any], Context], None]) -> Callable[[], bool]:
        """Register a synchronous post-adoption listener. It must not block or call Session operations; a throw is
        reported."""
        self.assertUsable()
        self._commitListeners[listener] = None

        def unsubscribe() -> bool:
            return self._commitListeners.pop(listener, UNDEFINED) is not UNDEFINED

        return unsubscribe

    def observeCommits(self, listener: Callable[[dict[str, Any], Context], None]) -> Callable[[], bool]:
        """Internal: ``subscribeCommits()`` for the Session's own components, whose state follows each commit. A throw
        leaves that state behind storage, so it fails the Session instead of being reported. They run before host
        listeners."""
        self.assertUsable()
        self._internalListeners[listener] = None

        def unsubscribe() -> bool:
            return self._internalListeners.pop(listener, UNDEFINED) is not UNDEFINED

        return unsubscribe

    def subscribeClose(self, listener: Callable[[], None]) -> Callable[[], bool]:
        """Register a listener called synchronously when close begins, also when a failure closes the Session
        (``failure`` is then set). It must not block or call Session operations; a throw is reported."""
        self.assertUsable()
        self._closeListeners[listener] = None

        def unsubscribe() -> bool:
            return self._closeListeners.pop(listener, UNDEFINED) is not UNDEFINED

        return unsubscribe

    def unloadDocuments(self) -> asyncio.Future[None]:
        """Drop every loaded tracker on the mutation line; later access cold-loads from Storage."""

        async def unload() -> None:
            self._documents.clear()
            self._idleDocuments.clear()

        return self._enqueue(unload)

    @property
    def documentCacheSizes(self) -> dict[str, int]:
        """Internal, for tests: how many documents the cache holds, how many are idle, how many incarnations are pinned,
        and how many sweep jobs were queued on the line so far."""
        return {
            "cached": len(self._documents),
            "idle": len(self._idleDocuments),
            "attached": len(self._attachedDocuments),
            "sweeps": self._sweepJobs,
        }

    def _touchDocument(self, addressId: str) -> None:
        """Note a use of a cached document: an unattached one becomes the newest idle one, and an eviction is
        scheduled."""
        cached = self._documents.get(addressId)
        if cached is None or cached.record["id"] in self._attachedDocuments:
            return
        now = self._clock()
        since = self._idleDocuments.get(addressId)
        # A hot document moves to the back at most once per granule, a tenth of the retention up to 1 s: it may then
        # leave up to a granule early, which costs a reload, while a commit-heavy workload skips the reordering.
        if since is not None and now - since < min(1_000, self._documentRetentionMs() / 10):
            return
        self._idleDocuments.pop(addressId, None)
        self._idleDocuments[addressId] = now
        # A pending sweep for the oldest document comes first unless the retention was shortened since; then it moves up.
        if self._documentSweep is None or self._documentSweep.at > now + self._documentRetentionMs():
            self._scheduleDocumentSweep()

    def _dropDocument(self, addressId: str) -> None:
        self._documents.pop(addressId, None)
        self._idleDocuments.pop(addressId, None)

    def _scheduleDocumentSweep(self) -> None:
        """Schedule the eviction of the oldest idle document: on the line at once when the retention is 0, otherwise
        when it expires, by a timer that never keeps the process alive. Where timers cannot be unreferenced, as in
        Cloudflare Workers, none is kept: the end of each commit evicts what expired."""
        if self._closing is not None:
            return
        oldest = next(iter(self._idleDocuments.values()), UNDEFINED)
        if oldest is UNDEFINED:
            return
        at = oldest + self._documentRetentionMs()
        if self._documentSweep is not None and self._documentSweep.at <= at:
            return
        if self._documentSweep is not None and self._documentSweep.timer is not None:
            clearTimeout(self._documentSweep.timer)
        # The pending sweep stays set until its job runs, so touches while the line is busy queue no more.
        sweep = _DocumentSweep(at)

        def run() -> None:
            self._sweepJobs += 1

            async def job() -> None:
                if self._documentSweep is sweep:
                    self._documentSweep = None
                if self._closing is None:
                    self._sweepDocuments()

            _observe(self._enqueue(job))

        delay = at - self._clock()
        self._documentSweep = sweep
        if delay <= 0:
            queueMicrotask(run)
            return
        if self._documentTimersUnavailable:
            self._documentSweep = None
            return
        # An asyncio timer is never referenced: it does not keep the loop running (see `_documentTimersUnavailable`).
        sweep.timer = setTimeout(run, min(delay, MAX_TIMER_DELAY))

    def _sweepDocuments(self) -> None:
        """On the line: evict the idle documents whose retention passed, oldest first, then schedule the next
        eviction."""
        expired = self._clock() - self._documentRetentionMs()
        for idleAddressId, since in list(self._idleDocuments.items()):
            if since > expired:
                break
            self._dropDocument(idleAddressId)
        self._scheduleDocumentSweep()

    def _pinDocument(self, addressId: str, recordId: int) -> Callable[[], None]:
        """Pin a cached incarnation for one observer; the returned release unpins it once, idle again after the last."""
        self._attachedDocuments[recordId] = self._attachedDocuments.get(recordId, 0) + 1
        self._idleDocuments.pop(addressId, None)
        pinned = True

        def unpin() -> None:
            nonlocal pinned
            if not pinned:
                return
            pinned = False
            count = self._attachedDocuments.get(recordId, 1) - 1
            if count > 0:
                self._attachedDocuments[recordId] = count
                return
            self._attachedDocuments.pop(recordId, None)
            current = self._documents.get(addressId)
            if current is not None and current.record["id"] == recordId:
                self._touchDocument(addressId)

        return unpin

    async def _runCommit(
        self, change: Callable[[Transaction], Any], context: Context, scope: dict[str, Any] | None
    ) -> Any:
        self._assertHealthy()
        if context.abortSignal is not None:
            context.abortSignal.throwIfAborted()
        tx = Transaction(self._host, context, scope)
        try:
            result = await awaitValue(change(tx))
            # A callback that caught a failed read must not commit, nor succeed as if it had.
            self._assertHealthy()
        except BaseException:
            await tx.settleFailure()
            self._sweepDocuments()
            raise
        try:
            writes = await tx.settleSuccess()
        except BaseException:
            # Settlement aborted every change already.
            self._sweepDocuments()
            raise
        if len(writes) == 0:
            tx.discard()
            self._sweepDocuments()
            return result
        try:
            # Once admitted, caller cancellation does not interrupt Storage settlement.
            seq = await self._storage.commit(writes, withoutAbortSignal(context))
        except BaseException:
            # The guard has failed the Session.
            tx.discard()
            raise
        try:
            documents = tx.adopt(seq)
        except BaseException as error:
            # Storage already committed; a failed adoption leaves memory behind durable state.
            self.fail(error)
            raise
        for document in documents:
            self._touchDocument(addressId(document["record"]))
        self._publish(seq, writes, documents, context)
        self._sweepDocuments()
        return result

    def _publish(self, seq: int, writes: list[dict[str, Any]], documents: list[dict[str, Any]], context: Context) -> None:
        if not self._commitListeners and not self._internalListeners:
            return
        changes: list[dict[str, Any]] = []
        for write in writes:
            if write["type"] in ("conversation", "entry", "task", "submission"):
                changes.append(write)
        changes.extend(documents)
        publication = {"seq": seq, "changes": changes}
        # The commit is durable: a failing listener neither fails the commit nor skips the others.
        for internal in list(self._internalListeners):
            contained(lambda listener=internal: listener(publication, context), lambda error: self.fail(error))
        for listener in list(self._commitListeners):
            contained(lambda listener=listener: listener(publication, context), lambda error: self.report(error))

    def _attachDocument(
        self, definition: AnyDocDefinition, loaded: LoadedDocument, create: Callable[[Any, Callable[[], None]], Any]
    ) -> tuple[Any, Callable[[], None]]:
        """Attach an observer to one committed incarnation: check the definition, then forward this incarnation's
        committed changes and close. ``detach`` removes both subscriptions."""
        checkRecordScope(definition, loaded.record)
        checkRecordVersion(definition, loaded.record, loaded.storedVersion)
        unsubscribe: dict[str, Callable[[], None]] = {"commit": lambda: None, "close": lambda: None}
        # Attached, the incarnation stays cached; once its last observer detaches, or it retires, it is idle again.
        unpin = self._pinDocument(loaded.addressId, loaded.record["id"])

        def detach() -> None:
            unsubscribe["commit"]()
            unsubscribe["close"]()
            unpin()

        try:
            observer = create(loaded.tracker.value, detach)
        except BaseException:
            unpin()
            raise
        observed = {"version": loaded.valueVersion}

        def onCommit(publication: dict[str, Any], context: Context) -> None:
            for change in publication["changes"]:
                if change["type"] != "document" or change["record"]["id"] != loaded.record["id"]:
                    continue
                # A document state's frames carry no caller cancellation; a watch observes its own cancellation.
                frameContext = withoutAbortSignal(context) if isinstance(observer, CommittedStateSource) else context
                ops = observedOperations(observed, change)
                # A migration-only base changes nothing for an observer of the new version.
                if len(ops) == 0:
                    continue
                # A retired incarnation never changes again: its observers keep their last value but pin nothing.
                if change["value"] is None:
                    unpin()
                observer.advance(change["value"], ops, frameContext)

        unsubscribe["commit"] = self.observeCommits(onCommit)
        unsubscribe["close"] = self.subscribeClose(lambda: observer.closeSession(self._failure))
        return observer, detach

    async def _loadDocument(
        self, definition: AnyDocDefinition, addressId: str, address: dict[str, Any], context: Context
    ) -> LoadedDocument | None:
        cached = self._documents.get(addressId)
        # A tracker serves only tokens of the version its value was materialized for; others reload from Storage.
        if cached is not None and cached.valueVersion == definition["version"]:
            self._touchDocument(addressId)
            return cached
        if cached is not None:
            self._dropDocument(addressId)
        record = await self._storage.findDocument(address, "current", context)  # type: ignore[arg-type]
        if record is None:
            return None
        stored = await self._storage.document(record["id"], "current", context)
        if stored is None:
            raise RuntimeError(f"Current document {record['id']} ({record['kind']}) cannot be read")
        value = materializeDocument(definition, stored)
        loaded = LoadedDocument(
            addressId, stored["record"], stored["version"], definition["version"], stored["deltasSinceBase"], track(value)
        )
        self._documents[addressId] = loaded
        self._touchDocument(addressId)
        return loaded

    def _enqueue(self, job: Callable[[], Awaitable[Any]]) -> asyncio.Future[Any]:
        run = startTask(self._runAfter(self._tail, job))
        self._tail = startTask(_settle(run))
        # Shielded: a promise cannot be cancelled by whoever waits on it, so a caller that stops waiting leaves the job
        # on the line, and every job behind it, running.
        return asyncio.shield(run)

    @staticmethod
    async def _runAfter(tail: asyncio.Future[None] | None, job: Callable[[], Awaitable[Any]]) -> Any:
        # `.then(job)` never runs the job inline.
        await asyncio.sleep(0)
        if tail is not None:
            await asyncio.shield(tail)
        return await job()

    def assertUsable(self) -> None:
        """Internal: throw ``SessionFailed`` when failed, else an error when closing."""
        self._assertHealthy()
        if self._closing is not None:
            raise RuntimeError("Session is closed")

    def _assertHealthy(self) -> None:
        if self._failure is not None:
            raise SessionFailed(self._failure["error"])


def contained(callback: Callable[[], Any], onError: Callable[[Any], None]) -> None:
    """Run a host callback that must not fail its caller: a throw, or a promise it returns that rejects, goes to
    ``onError``, which is not awaited."""
    try:
        result = callback()
        # Any awaitable; a coroutine is started so its failure is seen.
        if inspect.isawaitable(result):
            future = startTask(result)

            def settled(done: asyncio.Future[Any]) -> None:
                if done.cancelled():
                    return
                error = done.exception()
                if error is not None:
                    onError(error)

            future.add_done_callback(settled)
    except Exception as error:  # noqa: BLE001 - a host callback must not fail its caller
        onError(error)


def observedOperations(observed: dict[str, int], change: dict[str, Any]) -> list[Op]:
    """Operations an observer applies for one committed change. An observer hydrated under another definition version
    holds a differently shaped value, so it receives the new value as a root replacement instead of operations for that
    shape."""
    if change["value"] is None:
        return list(RETIREMENT_OPERATIONS)
    if change["version"] == observed["version"]:
        return change["ops"]
    observed["version"] = change["version"]
    return [["r", change["value"]]]


def cancellationError(signal: Any) -> Exception:
    reason = signal.reason
    return reason if isinstance(reason, Exception) else DOMException("The operation was aborted", "AbortError")


async def _settle(future: asyncio.Future[Any]) -> None:
    """A future that finishes when ``future`` settles, whatever it settles with: ``run.then(() => undefined, () => undefined)``."""
    try:
        await asyncio.shield(future)
    except Exception:  # noqa: BLE001, S110
        pass
    except asyncio.CancelledError:
        # A job that ended cancelled is settled for the line; this task's own cancellation is not.
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise


def _consume(future: asyncio.Future[Any]) -> None:
    if not future.cancelled():
        future.exception()


def _observe(future: asyncio.Future[Any]) -> None:
    """``promise.catch(() => {})``: a failure nobody awaits is not reported."""
    future.add_done_callback(_consume)


class GuardedStorage:
    """The Session's Storage behind its failure guard; every component of the Session reads and writes through it.
    Before a call, a failed Session gets ``SessionFailed`` without reaching the backend. A call that throws fails the
    Session and rethrows the backend's error, unless it is a ``StorageRequestError`` or a read whose caller's context was
    aborted: neither says the Storage is broken. ``close()`` always reaches the backend, so a failed Session still closes
    it. ``latch`` is what the guard needs of its Session: the ``failure`` so far, and how to ``fail()`` it."""

    def __init__(self, storage: Storage, latch: Any) -> None:
        self._backend = storage
        self._latch = latch
        # Backend calls underway, also those off the Session line, which close waits for before it closes the backend.
        self._underway: dict[asyncio.Future[Any], None] = {}
        self._closing = False

    def commit(self, writes: Any, context: Context) -> asyncio.Future[Any]:
        """Never exempt, whatever it throws: once admitted, whether a failed batch committed is unknown."""
        return self._call(None, lambda: self._backend.commit(writes, context))

    def mintId(self) -> asyncio.Future[Any]:
        return self._call(None, lambda: self._backend.mintId())

    def conversation(self, id: int, context: Context) -> asyncio.Future[Any]:
        return self._call(context, lambda: self._backend.conversation(id, context))

    def scanConversations(self, query: Any, limit: int, cursor: Any, context: Context) -> asyncio.Future[Any]:
        return self._call(context, lambda: self._backend.scanConversations(query, limit, cursor, context))

    def entry(self, *args: Any) -> asyncio.Future[Any]:
        """``entry(id, context)`` or ``entry(conversationId, id, context)``."""
        context = args[-1]
        return self._call(context, lambda: self._backend.entry(*args))

    def findLatestHeadMarker(self, conversationId: int, atOrBeforeEntryId: int | None, context: Context) -> asyncio.Future[Any]:
        return self._call(context, lambda: self._backend.findLatestHeadMarker(conversationId, atOrBeforeEntryId, context))

    def scanEntries(self, query: Any, limit: int, cursor: Any, context: Context) -> asyncio.Future[Any]:
        return self._call(context, lambda: self._backend.scanEntries(query, limit, cursor, context))

    def task(self, id: int, context: Context) -> asyncio.Future[Any]:
        return self._call(context, lambda: self._backend.task(id, context))

    def scanTasks(self, query: Any, limit: int, cursor: Any, context: Context) -> asyncio.Future[Any]:
        return self._call(context, lambda: self._backend.scanTasks(query, limit, cursor, context))

    def submission(self, id: int, context: Context) -> asyncio.Future[Any]:
        return self._call(context, lambda: self._backend.submission(id, context))

    def scanSubmissions(self, query: Any, limit: int, cursor: Any, context: Context) -> asyncio.Future[Any]:
        return self._call(context, lambda: self._backend.scanSubmissions(query, limit, cursor, context))

    def submissionByRequest(self, conversationId: int, requestId: str, context: Context) -> asyncio.Future[Any]:
        return self._call(context, lambda: self._backend.submissionByRequest(conversationId, requestId, context))

    def findDocument(self, address: Any, at: Any, context: Context) -> asyncio.Future[Any]:
        return self._call(context, lambda: self._backend.findDocument(address, at, context))

    def document(self, id: int, at: Any, context: Context) -> asyncio.Future[Any]:
        return self._call(context, lambda: self._backend.document(id, at, context))

    def scanDocuments(self, query: Any, limit: int, cursor: Any, context: Context) -> asyncio.Future[Any]:
        return self._call(context, lambda: self._backend.scanDocuments(query, limit, cursor, context))

    @eager
    async def close(self, context: Context) -> None:
        """Close the backend once every call underway has settled, so none outlives it or fails after ``closed``
        settles. New calls are refused from here on: a paged read must not start its next page while the backend
        closes."""
        self._closing = True
        await allSettled(list(self._underway))
        return await self._backend.close(context)

    @eager
    async def _call(self, read: Context | None, run: Callable[[], Awaitable[Any]]) -> Any:
        """Run one backend call under the guard. ``read`` is a read's context; ``commit()`` and ``mintId()`` pass none,
        so nothing exempts their errors: once a batch is admitted, whether it committed is unknown. A call still
        underway when another fails the Session ends with ``SessionFailed`` too, whatever it returns: nothing it read or
        wrote is used."""
        self._assertHealthy()
        if self._closing:
            raise RuntimeError("Session is closed")

        # An async wrapper, so a backend that throws synchronously is guarded too.
        async def wrapper() -> Any:
            return await run()

        call = startTask(wrapper())
        self._underway[call] = None
        try:
            result = await call
        except GeneratorExit:
            # The caller's coroutine is being closed, not the call answered.
            raise
        except BaseException as error:
            if not isRejection(error):
                # MISAKA fork: asyncio can cancel the running task (a caller's timeout, loop teardown), which
                # JavaScript cannot. A read cancelled that way says nothing about the Storage and fails that call only.
                # A commit or mint is never exempt: it runs on the shielded commit line, so only teardown cancels it
                # there, and whether its batch landed is unknown. A ``CancelledError`` the backend raised while the
                # task is not cancelled is a rejection like any other (`isRejection`), handled below.
                if read is None:
                    self._latch.fail(error)
                raise
            self._assertHealthy()
            # An invalid read, or one its caller cancelled, says nothing about the Storage; it fails that call only.
            exempt = read is not None and (
                isinstance(error, StorageRequestError) or bool(read.abortSignal is not None and read.abortSignal.aborted)
            )
            if not exempt:
                self._latch.fail(error)
            raise
        finally:
            self._underway.pop(call, None)
        self._assertHealthy()
        return result

    def _assertHealthy(self) -> None:
        failure = self._latch.failure
        if failure is not None:
            raise SessionFailed(failure["error"])


__all__ = [
    "MAX_TIMER_DELAY",
    "GuardedStorage",
    "SessionImpl",
    "contained",
    "createSession",
    "documentRetention",
    "setDefaultDocumentRetention",
]

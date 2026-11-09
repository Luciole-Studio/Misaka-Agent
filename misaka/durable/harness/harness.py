"""The durable agent Harness over one Session: conversation handles, a registry, scheduling and submissions.

Translated from pi's ``packages/durable/src/harness/harness.ts`` (v1.3.0). ``Harness.open(storage, options, context)``
opens one;
the options are a ``dict`` (``models``, ``registry``, ``settings``, ``env``, ``conversationCreated``, ``now``,
``onReport``). A conversation handle is a ``ConversationImpl`` and the handle a task or tool gets is a
``types.SimpleNamespace`` (``id``, ``submit``, ``abort``, ``waitForIdle``). Public async methods are ``@eager``.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from misaka.chord.context import Context, withAbortSignal, withoutAbortSignal
from misaka.utils.js import awaitValue, eager

from ..entries import ResetEntry
from ..session.session import SessionImpl, contained
from ..types import (
    ROOT_CONVERSATION_ID,
    ConversationId,
    EntryId,
    Storage,
    SubmissionId,
    TaskId,
)
from .agent import AgentDoc, configure, createAgent, resolveAgent, resolveSettings
from .compaction import createCompaction
from .context import readContext
from .inbox import InboxDoc, withdrawQueuedInputs
from .live import LiveDoc, settleSchedulerOutcome
from .provider import ProviderDoc
from .registry import BUILTIN_TASKS
from .scheduler import TaskScheduler
from .submissions import Submissions
from .task_graph import TaskGraphView
from .usage import UsageDoc, addUsageState
from .util import scanAll
from .view import ConversationViews

SCAN_PAGE_SIZE = 256

SCHEDULER_INDEX_SIZES = "pi-durable.schedulerIndexSizes"
"""Key of the Harness's internal index-size getter, for tests: ``getattr(harness, SCHEDULER_INDEX_SIZES)``. (Upstream's
``Symbol.for("pi-durable.schedulerIndexSizes")``; a Python attribute named by the same string.)"""


class _ConversationHost:
    """Harness-private services used by Conversation handles."""

    def __init__(self, harness: HarnessImpl, storage: Storage, create: Callable[..., Any]) -> None:
        self.harness = harness
        self.storage = storage
        self.tasks: TaskScheduler = harness._tasks
        self.submissions: Submissions = harness._submissions
        self.views = ConversationViews(harness, storage)
        self.now: Callable[[], int] = harness._now
        self.create = create


class ConversationImpl:
    """Stateless handle for one conversation, bound to the Harness that returned it. Compare handles by ``id``."""

    def __init__(self, id: ConversationId, host: _ConversationHost) -> None:
        self.id = id
        self._host = host

    def agent(self, context: Context) -> Any:
        """Resolved with the current registry snapshot and settings."""
        return self._host.harness.resolveAgent(self.id, None, context)

    def configure(self, change: dict[str, Any], context: Context) -> Any:
        """``configure()`` in its own commit."""
        return self._host.harness.commitWith(lambda tx: configure(tx, self.id, change), context)

    def submit(self, submission: dict[str, Any], context: Context) -> Any:
        """Durably admit user input or a passive entry write. A busy conversation, or one with queued items, queues it in
        ``pi.inbox``; ``whenBusy: "reject"`` rejects with ``ConversationBusy`` instead and writes nothing."""
        return self._host.submissions.submit(self.id, submission, context)

    def compact(self, instructions: str | None, context: Context) -> Any:
        """Admit a manual compaction task and return its ID. It summarizes while the conversation keeps working and places
        its summary through a write submission: at once when idle, otherwise at the next boundary (spec section 8.7)."""
        self._host.tasks.resume()
        compaction = {"reason": "manual", **({} if instructions is None else {"instructions": instructions})}
        return self._host.harness.commitWith(lambda tx: createCompaction(tx, self.id, compaction), context)

    @eager
    async def reset(self, handoff: str | None, context: Context) -> None:
        """Admit a write of a ``pi.reset`` entry that starts a new context, carrying ``handoff`` as a user message when
        given. Resolves after admission; while busy, it is placed at the next boundary."""
        model = (
            {}
            if handoff is None
            else {"model": [{"role": "user", "content": handoff, "timestamp": self._host.now()}]}
        )
        entry = {"kind": ResetEntry.kind, "head": "self", **model}
        await self._host.submissions.submit(self.id, {"type": "write", "entry": entry}, context)

    def commit(self, change: Callable[..., Any], context: Context) -> Any:
        """Session commit whose ``tx.createTask()`` defaults to this conversation."""
        return self._host.harness.commitWith(change, context, {"conversationId": self.id})

    @eager
    async def context(self, context: Context, options: dict[str, Any] | None = None) -> dict[str, Any]:
        """Committed raw active transcript and model context. With ``at``, the context as of that visible entry: the same
        view ``fork(at)`` would start with, without creating a conversation."""
        return await readContext(
            self._host.harness, self._host.storage, self.id, context, None if options is None else options.get("at")
        )

    def entries(self, query: dict[str, Any], limit: int, cursor: Any, context: Context) -> Any:
        """Fork-aware history of this conversation, newest first unless ``query.order`` is ``ascending``."""
        bounded = {
            "conversationId": self.id,
            **({} if query.get("minEntryId") is None else {"minEntryId": query["minEntryId"]}),
            **({} if query.get("maxEntryId") is None else {"maxEntryId": query["maxEntryId"]}),
            **({} if query.get("order") is None else {"order": query["order"]}),
        }
        return self._host.harness.readOnLine(lambda: self._host.storage.scanEntries(bounded, limit, cursor, context))

    def fork(self, at: EntryId, options: dict[str, Any], context: Context) -> Any:
        return self._host.create(
            {"kind": "fork", "parentId": self.id, "at": at, "ownership": options["ownership"]}, options, context
        )

    def abort(self, context: Context, options: dict[str, Any] | None = None) -> Any:
        """Withdraw queued inputs (queued writes stay), mark every live non-background task of the ordinary ownership
        scope, signal them, and resolve once the scope is idle. Background subtrees survive unless ``background`` is
        set."""
        self._host.tasks.resume()
        background = options is not None and options.get("background") is True
        return self._host.tasks.abortConversation(self.id, background, context)

    def waitForIdle(self, context: Context) -> Any:
        """Resolve when the ordinary ownership scope has no live non-background task: this conversation and the
        conversations owned, transitively, by its non-background tasks."""
        self._host.tasks.resume()
        return self._host.tasks.waitForIdle(self.id, context)

    def viewState(self, context: Context) -> Any:
        """The structural view (spec section 9.3) as a disposable read-only Chord state."""
        return self._host.views.state(self.id, context)

    def watch(self, context: Context) -> Any:
        """The structural view as a serialized exact-frame watch with bounded pending frames."""
        return self._host.views.watch(self.id, context)


class HarnessImpl(SessionImpl):
    """Session kernel extended with conversation handles and a registry."""

    def __init__(self, rawStorage: Storage, options: dict[str, Any], context: Context) -> None:
        report = safeReport(options.get("onReport"))
        now = safeNow(options.get("now"), report)
        # Read at each use, as all settings are, but only this field: it is read on every commit.
        super().__init__(rawStorage, now, lambda: (options.get("settings") or {}).get("documentRetentionMs"))
        # Every component reads and writes through the Session's failure guard: `rawStorage` serves only `super()`.
        storage = self.storage
        self._options = options
        self._report: Callable[[Any], None] = report
        self._now: Callable[[], int] = now

        def settings() -> dict[str, Any]:
            return resolveSettings(options.get("settings"))

        async def conversation(id: ConversationId, binding: dict[str, Any], callContext: Context) -> Any:
            record = await self.readOnLine(lambda: storage.conversation(id, callContext))
            return None if record is None else boundConversation(id, binding, self._submissions, self._tasks)

        self._tasks = TaskScheduler(
            {
                "session": self,
                "storage": storage,
                "registry": options["registry"],
                "models": options["models"],
                "agent": lambda id, snapshot, callContext: self.resolveAgent(id, snapshot, callContext),
                "settings": settings,
                "env": lambda id, callContext: self.buildEnv(id, callContext),
                "now": self._now,
                "report": self._report,
                "settleOutcome": settleSchedulerOutcome,
                "withdrawInputs": withdrawQueuedInputs,
                "conversation": conversation,
                "context": withoutAbortSignal(context),
            }
        )
        self._submissions = Submissions(self, storage, self._now, settings, lambda: self._tasks.resume())
        self._taskGraph = TaskGraphView(self, storage)
        # MISAKA fork: upstream's `#host`; `_host` is the Session kernel's here, where private fields are not per
        # class.
        self._conversationHost = _ConversationHost(
            self, storage, lambda target, createOptions, createContext: self._create(target, createOptions, createContext)
        )

    @eager
    async def resolveAgent(self, id: ConversationId, snapshot: Any, context: Context) -> dict[str, Any]:
        """Resolve a conversation's committed ``pi.agent`` against ``snapshot``, or the current one, and the current
        settings."""
        registry = snapshot if snapshot is not None else self._options["registry"].snapshot()
        state = await self.snapshot(AgentDoc, id, context)
        return resolveAgent(state, registry, resolveSettings(self._options.get("settings")), self._report)

    @eager
    async def buildEnv(self, id: ConversationId, context: Context) -> Any:
        """Build a conversation's environment from its current ``cwd``; ``None`` without an ``env`` option."""
        build = self._options.get("env")
        if build is None:
            return None
        state = await self.snapshot(AgentDoc, id, context)
        cwd = None if state is None else state.get("cwd")
        return await awaitValue(
            build({"conversationId": id, **({} if cwd is None else {"cwd": cwd}), "read": self}, context)
        )

    def openTasks(self, context: Context) -> Any:
        """Reconcile surviving ``running`` tasks to ``pending``; part of open."""
        return self._tasks.open(context)

    def resume(self) -> None:
        """Enable task scheduling. Idempotent; throws after close. Calls that ask for progress enable it too."""
        self.assertUsable()
        self._tasks.resume()

    def getTask(self, id: TaskId, context: Context) -> Any:
        return self.readOnLine(lambda: self.storage.task(id, context))

    @property
    def schedulerIndexSizes(self) -> dict[str, int]:
        """Internal, for tests: sizes of the scheduler's ownership indexes (``SCHEDULER_INDEX_SIZES``)."""
        return self._tasks.indexSizes

    def inspect(self, context: Context) -> Any:
        """Live tasks and unsettled submissions. Writes nothing and runs no task code."""

        async def inspect() -> dict[str, Any]:
            inspected = await self._tasks.inspect(self._options["registry"].snapshot())

            async def scan(status: str) -> list[dict[str, Any]]:
                return await scanAll(
                    lambda cursor: self.storage.scanSubmissions({"status": status}, SCAN_PAGE_SIZE, cursor, context)
                )

            submissions = sorted([*(await scan("queued")), *(await scan("placed"))], key=lambda record: record["id"])
            return {"scheduling": inspected["scheduling"], "tasks": inspected["tasks"], "submissions": submissions}

        return self.readOnLine(inspect)

    def submission(self, id: SubmissionId, context: Context) -> Any:
        """Reacquire a submission, for example after reopen."""
        return self._submissions.get(id, context)

    def abortSubmission(self, id: SubmissionId, context: Context, conversationId: ConversationId | None = None) -> Any:
        """``not_found`` for an unknown submission or one of another conversation than ``conversationId``."""
        return self._submissions.abort(id, context, conversationId)

    def abortTask(self, id: TaskId, context: Context) -> Any:
        """Commit the abort mark, signal and join an active run invocation, and schedule the abort invocation. A task whose
        definition cannot take it settles as ``orphaned`` instead."""
        return self._tasks.abort(id, context)

    def waitForTask(self, id: TaskId, context: Context) -> Any:
        """Resolve with the terminal receipt; cancelling ``context`` cancels only this wait."""
        self._tasks.resume()
        return self._tasks.waitForTask(id, context)

    def waitForIdle(self, context: Context) -> Any:
        """Resolve when the ordinary ownership scope of every ownerless conversation has no live non-background task."""
        self._tasks.resume()
        return self._tasks.waitForIdle(None, context)

    @eager
    async def usage(self, context: Context) -> dict[str, Any]:
        """Sum every conversation's committed ``pi.usage``. Each document is read at its own point; totals only grow."""
        conversations = await self.readOnLine(
            lambda: scanAll(lambda cursor: self.storage.scanConversations({}, SCAN_PAGE_SIZE, cursor, context))
        )
        total: dict[str, Any] = UsageDoc["definition"]["initial"]()
        for conversation in conversations:
            state = await self.snapshot(UsageDoc, conversation["id"], context)
            if state is not None:
                addUsageState(total, state)  # type: ignore[arg-type]
        return total

    def taskGraph(self, context: Context) -> Any:
        """Every live task with its owner edge, status, and owned conversations (spec section 9.5), as a disposable Chord
        state."""
        return self._taskGraph.state(context)

    def watchTaskGraph(self, context: Context) -> Any:
        """The task graph as a serialized exact-frame watch with bounded pending frames."""
        return self._taskGraph.watch(context)

    def root(self, context: Context, options: dict[str, Any] | None = None) -> Any:
        """Return the reserved root conversation, creating it with ``agent`` and ``init`` in one commit when absent."""
        return self._create({"kind": "root"}, options or {}, context)

    @eager
    async def conversation(self, id: ConversationId, context: Context) -> ConversationImpl | None:
        self.assertUsable()
        record = await self.readOnLine(lambda: self.storage.conversation(id, context))
        return None if record is None else ConversationImpl(record["id"], self._conversationHost)

    def createConversation(self, options: dict[str, Any], context: Context) -> Any:
        return self._create({"kind": "independent", "ownership": options["ownership"]}, options, context)

    def report(self, error: Any) -> None:
        self._report(error)

    def beforeClose(self) -> Any:
        """Signal and join task invocations after the close listeners and before Storage closes; writes no task
        outcome."""
        return self._tasks.join()

    @eager
    async def _create(self, target: dict[str, Any], options: dict[str, Any], context: Context) -> ConversationImpl:
        self.assertUsable()

        async def create(tx: Any) -> int:
            if target["kind"] == "root" and (await tx.conversation(ROOT_CONVERSATION_ID)) is not None:
                return ROOT_CONVERSATION_ID
            if target["kind"] == "root":
                record = await tx.createRootConversation()
            elif target["kind"] == "fork":
                record = await tx.forkConversation(target["parentId"], target["at"], {"ownership": target["ownership"]})
            else:
                record = await tx.createConversation({"ownership": target["ownership"]})
            if options.get("agent") is not None:
                await configure(tx, record["id"], options["agent"])
            if options.get("init") is not None:
                await awaitValue(options["init"](tx, record["id"]))
            return record["id"]  # type: ignore[no-any-return]

        id = await self.commitWith(create, context)
        return ConversationImpl(id, self._conversationHost)

    async def conversationCreated(self, tx: Any, record: dict[str, Any]) -> None:  # type: ignore[override]
        """The built-in creation hook, in every commit that creates or forks a conversation: empty ``pi.live``,
        ``pi.inbox``, and ``pi.usage``, a fresh ``pi.provider``, the conversation's ``pi.agent`` (see ``createAgent()``),
        then ``HarnessOptions.conversationCreated``."""
        await tx.doc(LiveDoc, record["id"])
        await tx.doc(InboxDoc, record["id"])
        await tx.doc(UsageDoc, record["id"])
        await tx.doc(ProviderDoc, record["id"])
        await createAgent(tx, record)
        created = self._options.get("conversationCreated")
        if created is not None:
            await awaitValue(created(tx, record))


# `harness[SCHEDULER_INDEX_SIZES]`: the getter under the key's name.
setattr(HarnessImpl, SCHEDULER_INDEX_SIZES, HarnessImpl.schedulerIndexSizes)


def safeReport(onReport: Callable[[Any], Any] | None) -> Callable[[Any], None]:
    """``onReport`` that cannot fail: it is the last place an error goes, so its own throw or rejection is dropped."""

    def report(error: Any) -> None:
        contained(lambda: None if onReport is None else onReport(error), lambda _error: None)

    return report


def _dateNow() -> int:
    return int(time.time() * 1000)


def safeNow(now: Callable[[], int] | None, report: Callable[[Any], None]) -> Callable[[], int]:
    """The host clock, falling back to ``Date.now`` when it throws: a broken clock must not stop commits. The first
    throw is reported; the clock is read in every task change, so later ones are not."""
    if now is None:
        return _dateNow
    reported = False

    def clock() -> int:
        nonlocal reported
        try:
            return now()
        except Exception as error:  # noqa: BLE001 - a broken clock must not stop commits
            # Set first: a report handler that reads the clock again must not report again.
            if not reported:
                reported = True
                report(error)
            return _dateNow()

    return clock


def boundConversation(id: ConversationId, binding: dict[str, Any], submissions: Submissions, tasks: TaskScheduler) -> Any:
    """Invocation-bound handle for tasks and tools. Every operation, and every operation of a submission it returns, first
    checks the invocation and runs under its signal, so it rejects once the invocation ends; admitted work stays
    durable."""

    def bind(context: Context) -> Context:
        return withAbortSignal(binding["signal"], context)

    def bound(operation: Callable[[Context], Any]) -> Callable[[Context], Any]:
        @eager
        async def run(context: Context) -> Any:
            binding["check"]()
            return await operation(bind(context))

        return run

    @eager
    async def submit(draft: dict[str, Any], context: Context) -> Any:
        binding["check"]()
        submission = await submissions.submit(id, draft, bind(context))
        return SimpleNamespace(
            id=submission.id,
            status=bound(lambda callContext: submission.status(callContext)),
            wait=bound(lambda callContext: submission.wait(callContext)),
            abort=bound(lambda callContext: submission.abort(callContext)),
        )

    @eager
    async def abort(context: Context, options: dict[str, Any] | None = None) -> Any:
        binding["check"]()
        background = options is not None and options.get("background") is True
        return await tasks.abortConversation(id, background, bind(context))

    return SimpleNamespace(
        id=id,
        submit=submit,
        abort=abort,
        waitForIdle=bound(lambda callContext: tasks.waitForIdle(id, callContext)),
    )


class Harness:
    """Durable agent harness over one Session."""

    @staticmethod
    @eager
    async def open(storage: Storage, options: dict[str, Any], context: Context) -> HarnessImpl:
        """Open a Harness over storage. The registry may keep changing while the Harness runs."""
        signal = context.abortSignal
        if signal is not None:
            signal.throwIfAborted()
        snapshot = options["registry"].snapshot()
        missing = [task for task in BUILTIN_TASKS if snapshot.task(task["definition"]["name"]) is None]
        if len(missing) > 0:
            names = ", ".join(task["definition"]["name"] for task in missing)
            raise RuntimeError(f"Registry lacks built-in tasks {names}; create it with createRegistry()")
        harness = HarnessImpl(storage, options, context)
        try:
            await harness.openTasks(context)
        except BaseException:
            # The caller's context may be what failed open: close without it, and rethrow the open error. A failing
            # Storage close fails the Session, which reports it.
            with contextlib.suppress(Exception):
                await harness.close(withoutAbortSignal(context))
            raise
        return harness


__all__ = ["SCHEDULER_INDEX_SIZES", "ConversationImpl", "Harness", "HarnessImpl", "safeNow", "safeReport"]

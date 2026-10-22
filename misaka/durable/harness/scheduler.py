"""The durable task scheduler of one Harness.

Translated from pi's ``packages/durable/src/harness/scheduler.ts`` (v1.3.0). Records, states, outcomes, definitions and
resolutions are plain ``dict`` objects with the upstream keys. A task handler is called ``handler(task, runtime,
context)`` with the stored record as ``task`` and a runtime object (attributes ``taskId``, ``conversationId``,
``signal``, ``models``, ``registry``, ``settings`` and ``hooks``; methods ``agent``, ``env``, ``commit``, ``memo``,
``sleep``, ``watchDoc``, ``snapshot``, ``snapshotAsOf``, ``getTask``, ``waitForTask``, ``ownedTasks``, ``abortOwned``,
``outcomes``, ``conversation``, ``entry``, ``context``, ``now`` and ``report``). A node of the downward ownership index is
a task ID (``int``) or a conversation as the string ``c<id>``, as upstream. A ``memo(name, context)`` read answers ``UNDEFINED`` for an absent
memo, since a memo may hold ``None``. Public async methods are ``@eager``; the JavaScript ``void`` calls
(``queueMicrotask(() => void this.#reconcile())``) are ``spawn`` tasks the scheduler keeps alive.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Iterator
from typing import Any

from misaka.chord._promise import isRejection
from misaka.chord.context import Context, abortError, awaitWithContext, withAbortSignal
from misaka.chord.json import copyJson
from misaka.utils.js import (
    UNDEFINED,
    AbortController,
    AbortSignal,
    awaitValue,
    eager,
    queueMicrotask,
    rejectedPromise,
    resolvedPromise,
    spawn,
    startTask,
)

from ..errors import errorMessage
from ..types import ConversationId, Storage, TaskId
from .agent import agentHooks
from .context import readContextFrom
from .util import LineWait, Waiters, closedError, scanAll, thrownBy

SCAN_PAGE_SIZE = 256
MAX_TIMER_DELAY = 2_147_483_647
"""Longest delay ``setTimeout`` supports; longer sleeps wait in several steps."""
LIVE_STATUSES = ("pending", "running", "waiting", "completing")

type Node = TaskId | str
"""A node of the downward index: a task, or a conversation as ``c<id>``."""


def conversationNode(id: ConversationId) -> Node:
    return f"c{id}"


def conversationOf(node: str) -> ConversationId:
    return int(node[1:])


def parentNode(node: dict[str, Any]) -> Node:
    """The node a task hangs from: its owner task, or its conversation."""
    owner = node.get("owner")
    return owner if owner is not None else conversationNode(node["conversationId"])


class _Invocation:
    """One in-memory execution of a task in run or abort mode."""

    def __init__(self, record: dict[str, Any], mode: str, controller: AbortController, context: Context) -> None:
        self.taskId = record["id"]
        self.conversationId = record["conversationId"]
        self.mode = mode
        self.controller = controller
        self.context = context  # Context passed to handlers; cancelled by ``controller``.
        self.watches: dict[Any, None] = {}  # Watches acquired through the runtime; stopped at invocation end.
        self.ended = False
        self.done: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def finish(self) -> None:
        if not self.done.done():
            self.done.set_result(None)


class _Phase:
    """What a runtime reads for the phase handler it serves: the phase's snapshot and task, and its lazily resolved
    agent."""

    def __init__(self, snapshot: Callable[[], Any], task: Callable[[], dict[str, Any]]) -> None:
        self.snapshot = snapshot
        self.task = task
        self.agent: asyncio.Future[Any] | None = None


class _Reservation:
    def __init__(self, invocation: _Invocation, task: dict[str, Any], snapshot: Any) -> None:
        self.invocation = invocation
        self.task = task
        self.snapshot = snapshot


class _PhaseState:
    """The task definition and registry snapshot a run invocation currently uses, and the replacement definition it
    already reported as unable to take over."""

    def __init__(self, task: dict[str, Any], snapshot: Any) -> None:
        self.task = task
        self.snapshot = snapshot
        self.reported: dict[str, Any] | None = None  # ``{"task": definition or None}`` once reported.


class _Hooks:
    """Dispatches one hook of a task to every matching registered handler, in registry order of the phase snapshot."""

    def __init__(self, scheduler: TaskScheduler, invocation: _Invocation, phase: _Phase, agent: Callable[[], Any]) -> None:
        self._scheduler = scheduler
        self._invocation = invocation
        self._phase = phase
        self._agent = agent

    @eager
    async def each(self, name: str, invoke: Callable[[Callable[..., Any]], Any]) -> None:
        """Call ``invoke`` with each matching handler named ``name``. An ordinary throw from ``invoke`` is reported and
        the next handler runs; once the invocation is signalled, the error propagates."""
        for handlers in agentHooks(await self._agent(), self._phase.task()["definition"]["name"]):
            handler = handlers.get(name) if isinstance(handlers, dict) else getattr(handlers, name, None)
            if not callable(handler):
                continue
            try:
                await awaitValue(invoke(handler))
            except BaseException as error:
                # A CancelledError from something the handler awaited is a throw like any other (`isRejection`).
                if self._invocation.controller.signal.aborted or not isRejection(error):
                    raise
                self._scheduler._report(thrownBy(error))


class TaskRuntimeImpl:
    """Operations of one task invocation. Every operation rejects after the invocation ends; watches acquired through it
    stop at invocation end."""

    def __init__(self, scheduler: TaskScheduler, invocation: _Invocation, phase: _Phase) -> None:
        self._scheduler = scheduler
        self._invocation = invocation
        self._phase = phase
        self.taskId = invocation.taskId
        self.conversationId = invocation.conversationId
        self.signal = invocation.controller.signal
        self.models = scheduler._models
        self.hooks = _Hooks(scheduler, invocation, phase, self._sharedAgent)

    @property
    def settings(self) -> Any:
        return self._scheduler._settings()

    @property
    def registry(self) -> Any:
        return self._phase.snapshot()

    def _sharedAgent(self) -> asyncio.Future[Any]:
        """Resolved at most once per phase handler, at first use, with the invocation's context; fixed for the
        phase."""
        phase = self._phase
        invocation = self._invocation
        if invocation.ended:
            return rejectedPromise(endedError(invocation))
        if phase.agent is None:
            phase.agent = startTask(
                self._scheduler._agent(invocation.conversationId, phase.snapshot(), invocation.context)
            )
            # A caller that stops waiting must not leave the shared resolution's failure unobserved.
            phase.agent.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        # Shielded: a caller that stops waiting must not cancel the resolution the others share.
        return asyncio.shield(phase.agent)

    @eager
    async def agent(self, context: Context) -> Any:
        if self._invocation.ended:
            raise endedError(self._invocation)
        return await awaitWithContext(self._sharedAgent(), context)

    def env(self, context: Context) -> asyncio.Future[Any]:
        return self._scheduler._read(
            self._invocation, lambda: self._scheduler._env(self._invocation.conversationId, context)
        )

    def commit(self, change: Callable[..., Any], context: Context) -> asyncio.Future[None]:
        """Commit on the Session line after rereading the task. Rejects when the task is terminal, the invocation
        ended, the Harness is closing, or, in a run invocation, the task carries an abort mark. A returned state
        replaces the task's state in the same commit; returning nothing leaves it unchanged."""

        async def apply(tx: Any, current: dict[str, Any]) -> None:
            following = await awaitValue(change(tx, current))
            if following is not None:
                await self._scheduler._commitState(tx, self._invocation, current, following)

        return self._scheduler._gated(self._invocation, apply, context)

    def memo(self, name: str, *rest: Any) -> asyncio.Future[Any]:
        """``memo(name, context)`` reads a durable memo of this task; ``memo(name, candidate, context)`` stores
        ``candidate`` unless a memo already exists and returns the durable winner."""
        scheduler = self._scheduler
        invocation = self._invocation
        if len(rest) == 1:

            async def read() -> Any:
                return memoOf(scheduler._live.get(invocation.taskId), name)

            return scheduler._read(invocation, read)
        candidate = rest[0]

        def change(tx: Any, current: dict[str, Any]) -> Any:
            winner = memoOf(current, name)
            if winner is not UNDEFINED:
                return winner
            tx.setTask({**current, "memos": {**(current.get("memos") or {}), name: candidate}})
            return candidate

        return scheduler._gated(invocation, change, rest[1])

    def sleep(self, until: float, context: Context) -> asyncio.Future[None]:
        """Resolve once the Harness clock reaches ``until``; rejects when the invocation or ``context`` is cancelled."""
        return startTask(self._scheduler._sleep(self._invocation, until, context))

    def watchDoc(self, *args: Any) -> asyncio.Future[Any]:
        return startTask(self._scheduler._watchDoc(self._invocation, args))

    def snapshot(self, *args: Any) -> asyncio.Future[Any]:
        return self._scheduler._read(self._invocation, lambda: self._scheduler._session.snapshot(*args))

    def snapshotAsOf(self, *args: Any) -> asyncio.Future[Any]:
        return self._scheduler._read(self._invocation, lambda: self._scheduler._session.snapshotAsOf(*args))

    def getTask(self, id: TaskId, context: Context) -> asyncio.Future[Any]:
        scheduler = self._scheduler
        return scheduler._read(
            self._invocation, lambda: scheduler._session.readOnLine(lambda: scheduler._storage.task(id, context))
        )

    def waitForTask(self, id: TaskId, context: Context) -> asyncio.Future[Any]:
        scheduler = self._scheduler
        return scheduler._read(
            self._invocation,
            lambda: scheduler.waitForTask(id, withAbortSignal(self._invocation.controller.signal, context)),
        )

    def ownedTasks(self, context: Context) -> asyncio.Future[Any]:
        """Live tasks this task owns directly, in ID order: also those an earlier invocation created, before a restart.
        Tasks in conversations it owns are not included. Copies of the committed records."""
        scheduler = self._scheduler
        invocation = self._invocation

        async def read() -> list[dict[str, Any]]:
            # The step that ends the invocation may have run on the line before this.
            if invocation.ended:
                raise endedError(invocation)
            if context.abortSignal is not None:
                context.abortSignal.throwIfAborted()
            # Every live task is listed under its parent node, its owner task when it has one.
            owned: list[dict[str, Any]] = []
            for node in list(scheduler._below.get(invocation.taskId) or ()):
                record = None if isinstance(node, str) else scheduler._live.get(node)
                if record is not None and record.get("owner") == invocation.taskId:
                    owned.append(record)
            # Copies, as from storage: the scheduler reads its own.
            return [copyJson(record) for record in sorted(owned, key=lambda record: record["id"])]

        return scheduler._read(invocation, lambda: scheduler._session.readOnLine(read))

    def abortOwned(self, id: TaskId, context: Context) -> asyncio.Future[None]:
        """Abort a task this task owns, as ``Harness.abortTask()`` does, and resolve once it is terminal. Rejects for a
        task another owns; resolves at once for a terminal one."""
        scheduler = self._scheduler
        invocation = self._invocation

        async def run() -> None:
            bound = withAbortSignal(invocation.controller.signal, context)
            record = await scheduler._session.readOnLine(lambda: scheduler._storage.task(id, bound))
            if record is None or record.get("owner") != invocation.taskId:
                raise RuntimeError(f"Task {id} is not owned by task {invocation.taskId}")
            if record["state"]["status"] == "terminal":
                return
            await scheduler.abort(id, bound, True)
            await scheduler.waitForTask(id, bound)

        return scheduler._read(invocation, run)

    def outcomes(self, ids: list[TaskId], context: Context) -> asyncio.Future[Any]:
        scheduler = self._scheduler

        async def read() -> list[dict[str, Any]]:
            outcomes: list[dict[str, Any]] = []
            for id in ids:
                record = await scheduler._storage.task(id, context)
                state = None if record is None else record["state"]
                if state is None or state["status"] != "terminal":
                    raise RuntimeError(f"Task {id} is not terminal")
                outcomes.append(state["outcome"])
            return outcomes

        return scheduler._read(self._invocation, lambda: scheduler._session.readOnLine(read))

    def conversation(self, id: ConversationId, context: Context) -> asyncio.Future[Any]:
        invocation = self._invocation
        scheduler = self._scheduler

        def check() -> None:
            if invocation.ended:
                raise endedError(invocation)

        return scheduler._read(
            invocation, lambda: scheduler._conversation(id, {"signal": invocation.controller.signal, "check": check}, context)
        )

    def entry(self, *args: Any) -> asyncio.Future[Any]:
        """``entry(id, context)``, or ``entry(token, id, context)``: ``None`` when the entry is absent, not visible, or
        has another kind."""
        scheduler = self._scheduler
        invocation = self._invocation
        if len(args) == 2:
            token, id, context = None, args[0], args[1]
        else:
            token, id, context = args[0], args[1], args[2]

        async def read() -> Any:
            found = await scheduler._session.readOnLine(
                lambda: scheduler._storage.entry(invocation.conversationId, id, context)
            )
            entry = None if found is None else found["entry"]
            return entry if token is None or (entry is not None and entry["kind"] == token.kind) else None

        return scheduler._read(invocation, read)

    def context(
        self, conversationId: ConversationId, context: Context, options: dict[str, Any] | None = None
    ) -> asyncio.Future[Any]:
        """Committed raw active transcript and model context, optionally cut off at the visible entry ``at``."""
        scheduler = self._scheduler
        invocation = self._invocation
        at = None if options is None else options.get("at")

        async def read() -> Any:
            kept = scheduler._contexts.get(conversationId)
            result = await readContextFrom(
                scheduler._session,
                scheduler._storage,
                conversationId,
                context,
                at,
                None if kept is None else kept["range"],
            )
            range_ = result["range"]
            # Keep it unless the invocation ended or a concurrent read already kept a newer range.
            kept = scheduler._contexts.get(conversationId)
            if (
                range_ is not None
                and not scheduler._closing
                and not invocation.ended
                and (kept is None or kept["range"]["bounds"]["tail"] <= range_["bounds"]["tail"])
            ):
                # A read of another, idle conversation starts or continues its retention period.
                if not scheduler._idle(conversationId):
                    scheduler._contexts[conversationId] = {"range": range_, "idleSince": None}
                elif scheduler._contextRetentionMs() > 0:
                    scheduler._contexts[conversationId] = {
                        "range": range_,
                        "idleSince": scheduler._now() if kept is None or kept["idleSince"] is None else kept["idleSince"],
                    }
                    scheduler._scheduleExpiry()
            return result["view"]

        return scheduler._read(invocation, read)

    def now(self) -> int:
        if self._invocation.ended:
            raise endedError(self._invocation)
        return self._scheduler._now()

    def report(self, error: BaseException) -> None:
        if self._invocation.ended:
            raise endedError(self._invocation)
        self._scheduler._report(error)


class TaskScheduler:
    """Durable task scheduler of one Harness.

    ``_live`` mirrors every committed non-terminal task record: pending, running, waiting, and completing. The
    synchronous commit listener updates it on the Session line, so code running on the line reads exactly the committed
    state from it.

    Tasks and conversations form one ownership tree (spec section 5.5): a task's parent is its owner task, or its
    conversation; a conversation's parent is its owner task, if any. Walks up that tree decide what reaches one task: a
    cancelling owner, a scope. ``_below`` lists the tree downward around live work, so what lies below one node is a
    walk down from it: a task's live ordinary owned work, which holds its outcome as ``completing`` and delays its abort
    handler, a scope's tasks, a cascade's reach. No pass visits every live task; the sets below hold each pass's
    candidates.

    The ownership indexes are bounded by live work: an owner edge and an ended task's ownership fields are kept only
    while something is listed below them in ``_below``, so only the chains above live tasks are known, whole. Work that
    appears later below a dropped node finds it unknown and loads its chain from storage again.

    Invariant: every task transition is decided and written by one callback serialized on the Session line. That covers
    reservation, marks, runtime commits, finalization, and the synchronous step before each phase, which applies the
    precedence rules and writes a fault or handover. Handlers and joins run off the line. An invocation ends inside the
    step that decides its end, so a runtime commit it queued either lands before that decision or is rejected.

    Options (``dict``): ``session``, ``storage``, ``registry``, ``models``, ``agent(conversationId, snapshot, context)``
    (resolve a conversation's agent against a snapshot; the runtime calls it at most once per phase), ``settings()`` (read
    at each access), ``env(conversationId, context)``, ``now()``, ``report(error)``, ``settleOutcome(tx, record,
    outcome)`` (Harness cleanup staged in the commit that makes an outcome the scheduler wrote itself terminal),
    ``withdrawInputs(tx, conversationId)``, ``conversation(id, binding, context)`` (invocation-bound handle of an
    existing conversation; ``binding`` is ``{"signal", "check"}``) and ``context`` (for scheduler commits and
    invocations; carries no caller cancellation).
    """

    def __init__(self, options: dict[str, Any]) -> None:
        self._session = options["session"]
        self._storage: Storage = options["storage"]
        self._registry = options["registry"]
        self._models = options["models"]
        self._agent = options["agent"]
        self._settings = options["settings"]
        self._env = options["env"]
        self._now = options["now"]
        self._report = options["report"]
        self._settleOutcome = options["settleOutcome"]
        self._withdrawInputs = options["withdrawInputs"]
        self._conversation = options["conversation"]
        self._context: Context = options["context"]
        self._live: dict[TaskId, dict[str, Any]] = {}
        # Context range last read through a task runtime, per conversation: a later read, by any of its tasks, scans only
        # newer entries. ``idleSince`` is the Harness time it was first seen idle. Derived and never persisted; dropped at
        # the first idle check after ``settings.contextRetentionMs`` of idleness, and at close.
        self._contexts: dict[ConversationId, dict[str, Any]] = {}
        # Timer for the earliest expiry of an idle context (see ``_scheduleExpiry``): ``{"at", "timer"}``.
        self._expiry: dict[str, Any] | None = None
        self._invocations: dict[TaskId, _Invocation] = {}
        self._taskWaiters = Waiters()
        self._idleWaiters = Waiters()  # Idle waiters by conversation; ``None`` waits for the whole Harness.
        # Definition whose migration failed per task; retried only once the registry resolves another definition.
        self._failedMigrations: dict[TaskId, dict[str, Any]] = {}
        self._edges: dict[ConversationId, TaskId | None] = {}  # Owner task of each known conversation, None when ownerless.
        self._settled: dict[TaskId, dict[str, Any]] = {}  # Ownership fields of the known terminal tasks, walks pass them.
        # Ended tasks and conversations found unneeded, dropped by `_sweep()` at the end of the commit's publication
        # unless needed again by then: a chain load installs nodes before the work below them is listed. Insertion order.
        self._dropQueue: dict[Node, None] = {}
        # The tree downward around live work: each node's child nodes that are live tasks or have nodes listed below
        # them. A conversation's parent is its owner task; ownerless ones with work are in `_roots`. Ordered sets.
        self._below: dict[Node, dict[Node, None]] = {}
        self._roots: dict[ConversationId, None] = {}
        # Known tasks whose chain may not be loaded; every other known task's chain is, and stays, loaded.
        self._unloaded: dict[TaskId, None] = {}
        # Live tasks with cancellation intent, the sources of abort cascades (spec section 5.4).
        self._intent: dict[TaskId, None] = {}
        # Live tasks that are not `completing`, the candidates of a reservation pass; ones found waiting leave it.
        self._runnable: dict[TaskId, None] = {}
        # `completing` tasks the next finalize pass checks: those that started holding, and holders of ended work.
        self._finalizeChecks: dict[TaskId, None] = {}
        # Live waiting tasks by each task in their `on`.
        self._waiters: dict[TaskId, dict[TaskId, None]] = {}
        # ``failFast`` waiters the next reconcile checks for a failed task in ``on``: at open, when they start waiting,
        # and when one of their tasks fails.
        self._failFastChecks: dict[TaskId, None] = {}
        # Live tasks with `abandonOnRestart` found at open, from an earlier Harness. The first reservation pass
        # abort-marks them with reason `restart`, so nothing of them or below them runs a phase again.
        self._abandoned: dict[TaskId, None] = {}
        self._reconcileScheduled = False
        self._sweepScheduled = False
        self._cascadePending = False
        self._unsubscribeRegistry: Callable[[], None] = lambda: None
        self._enabled = False
        self._closing = False
        self._dirty = False
        self._draining = False
        self._tasks: set[asyncio.Task[Any]] = set()

    @eager
    async def open(self, context: Context) -> None:
        """Load live tasks and change surviving ``running`` tasks back to ``pending``. Dispatches nothing."""
        self._session.observeCommits(lambda publication, commitContext: self._observe(publication))
        self._session.subscribeClose(lambda: self._seal())
        self._unsubscribeRegistry = self._registry.subscribe(lambda: self._kick())

        async def load(tx: Any) -> None:
            # Every table read before the first write.
            scans = []
            for status in LIVE_STATUSES:

                def scanStatus(cursor: Any, status: str = status) -> Any:
                    return tx.scanTasks({"status": status}, SCAN_PAGE_SIZE, cursor)

                scans.append(await scanAll(scanStatus))
            for records in scans:
                for record in records:
                    self._track(None, record)
                    if record.get("abandonOnRestart") is True:
                        self._abandoned[record["id"]] = None
                    if record["state"]["status"] == "running":
                        tx.setTask(withState(record, {"status": "pending", "checkpoint": record["state"]["checkpoint"]}))
                    if record["state"]["status"] == "waiting" and record["state"]["policy"] == "failFast":
                        self._failFastChecks[record["id"]] = None

        await self._session.commitWith(load, context)
        # Derive abort marks a crash left unapplied below cancelled owners, and finalize held outcomes.
        self._cascadePending = True
        self._scheduleReconcile()

    def resume(self) -> None:
        """Enable scheduling. Idempotent; the kick does nothing once closing."""
        self._enabled = True
        self._kick()

    @eager
    async def join(self) -> None:
        """Signal every invocation and wait for them; writes nothing. Close calls this after every close listener has run,
        so waits, watches, and streams a task holds end with the Session's reason, not as cancelled by its signal."""
        for invocation in list(self._invocations.values()):
            invocation.controller.abort()
        # Shielded: ``Promise.allSettled`` cannot cancel what it joins, so a join that is cancelled leaves them running.
        await asyncio.gather(
            *[asyncio.shield(invocation.done) for invocation in list(self._invocations.values())], return_exceptions=True
        )

    @eager
    async def abort(self, id: TaskId, context: Context, keepRestart: bool = False) -> str:
        """Commit the abort mark, or settle a task that no registered definition can take as ``orphaned`` when nothing it
        owns is live, then join the run invocation seen on the line; the commit listener signalled it. The abort
        invocation starts once the task's ordinary owned work is gone. A ``completing`` task is only marked. A request
        replaces a ``restart`` mark, so a task abandoned after a restart that waits for its definition is orphaned; with
        ``keepRestart``, as for an owner's own cleanup, such a task keeps its mark and waits on."""

        async def mark(tx: Any) -> dict[str, Any]:
            current = await tx.task(id)
            if current is None:
                raise RuntimeError(f"Task {id} does not exist")
            if current["state"]["status"] == "terminal":
                return {"result": "terminal"}
            invocation = self._invocations.get(id)
            # A restart-marked task has no run invocation; it only waits for its abort.
            if keepRestart and current.get("abortReason") == "restart":
                return {"result": "marked"}
            if invocation is None and current["state"]["status"] != "completing":
                await self._loadScopes(False)
                if not self._hasOwnedLive(id):
                    resolution = self._resolve(current, self._registry.snapshot())
                    if resolution["kind"] == "blocked":
                        await self._terminate(tx, current, {"status": "orphaned", "reason": resolution["reason"]})
                        return {"result": "marked"}
            if not current["abortRequested"] or current.get("abortReason") is not None:
                tx.setTask(withAbortMark(current))
            return {"result": "marked", "run": invocation if invocation is not None and invocation.mode == "run" else None}

        marked = await self._session.commitWith(mark, context)
        # The commit listener signalled the run; join it. A run that ignores its signal can outlive a failed Session,
        # which ends the wait instead.
        if marked.get("run") is not None:
            await awaitWithContext(_firstSettled(marked["run"].done, self._session.failed), context)
        return marked["result"]  # type: ignore[no-any-return]

    @eager
    async def waitForTask(self, id: TaskId, context: Context) -> dict[str, Any]:
        line = LineWait()

        # Check and register on the line so no terminal publication falls between them.
        async def check() -> dict[str, Any]:
            if self._closing:
                raise closedError(self._session)
            if id in self._live:
                return {"promise": line.register(self._taskWaiters.add(id, context))}
            record = await self._storage.task(id, context)
            if record is None:
                raise RuntimeError(f"Task {id} does not exist")
            return {"promise": resolvedPromise(record)}

        try:
            found = await self._session.readOnLine(check)
        except BaseException:
            line.abandon()
            raise
        return await found["promise"]  # type: ignore[no-any-return]

    def waitForIdle(self, conversationId: ConversationId | None, context: Context) -> asyncio.Future[Any]:
        """Resolve when ordinary traversal from the conversation, or from every ownerless conversation, reaches no live
        non-background task."""
        if self._closing:
            return rejectedPromise(closedError(self._session))
        if self._idle(conversationId):
            return resolvedPromise(None)
        self._scheduleReconcile()
        return self._idleWaiters.add(conversationId, context)

    @eager
    async def abortConversation(self, conversationId: ConversationId, background: bool, context: Context) -> None:
        """``Conversation.abort()``: in one commit, withdraw the queued inputs and mark every live non-background task
        that ordinary traversal from the conversation reaches; resolves once the scope is idle. With ``background``,
        traversal crosses background boundaries, and the wait also covers every task it reached."""

        async def mark(tx: Any) -> list[TaskId]:
            queued = await self._loadScopes(True)
            scope = {"conversation": conversationId}
            reached: list[TaskId] = []
            for step in self._walkDown(conversationNode(conversationId), crossBackground=background):
                node, live = step["node"], step["live"]
                if live is None or (node["background"] and not background):
                    continue
                reached.append(live["id"])
                if not live["abortRequested"] or live.get("abortReason") is not None:
                    tx.setTask(withAbortMark(live))
            for id in queued:
                if self._inScope({"conversation": id}, scope, background) is True:
                    await self._withdrawInputs(tx, id)
            return reached

        reached = await self._session.commitWith(mark, context)
        if background:
            for id in reached:
                await self.waitForTask(id, context)
        await self.waitForIdle(conversationId, context)

    # ─── Scheduling ────────────────────────────────────────────────────────

    def _observe(self, publication: dict[str, Any]) -> None:
        updated: list[dict[str, Any]] = []
        failed: list[TaskId] = []
        changed = False
        # Edges first, so the tasks a commit creates in a new conversation hang from it.
        for change in publication["changes"]:
            if change["type"] == "conversation" and change["value"]["id"] not in self._edges:
                owner = change["value"].get("owner")
                self._setEdge(change["value"]["id"], None if owner is None else owner["taskId"])
        for change in publication["changes"]:
            if change["type"] != "task":
                continue
            changed = True
            record = change["value"]
            previous = self._live.get(record["id"])
            if failedOutcome(record) and (previous is None or not failedOutcome(previous)):
                failed.append(record["id"])
            self._track(previous, record)
            if record["state"]["status"] == "terminal":
                self._failedMigrations.pop(record["id"], None)
                self._failFastChecks.pop(record["id"], None)
                self._taskWaiters.resolve(record["id"], record)
                # Its owner may finalize now.
                self._scheduleReconcile()
                continue
            if record["abortRequested"] and not (previous is not None and previous["abortRequested"] is True):
                self._cascadePending = True
                # Signal a run invocation of the newly marked task; its next step ends it.
                invocation = self._invocations.get(record["id"])
                if invocation is not None and invocation.mode == "run":
                    invocation.controller.abort()
            # A request replacing a `restart` mark upgrades the marks of the work below.
            if previous is not None and previous.get("abortReason") is not None and record.get("abortReason") is None:
                self._cascadePending = True
            status = record["state"]["status"]
            if status == "completing" and not (previous is not None and previous["state"]["status"] == "completing"):
                if cancellationIntent(record):
                    self._cascadePending = True
                self._scheduleReconcile()
            if (
                status == "waiting"
                and record["state"]["policy"] == "failFast"
                and not (previous is not None and previous["state"]["status"] == "waiting")
            ):
                self._failFastChecks[record["id"]] = None
                self._scheduleReconcile()
            # Its owners and place never change: what is above it is checked once, when it becomes live.
            if previous is None:
                updated.append(record)
        for id in failed:
            for waiter in list(self._waiters.get(id) or ()):
                found = self._live.get(waiter)
                state = None if found is None else found["state"]
                if state is not None and state["status"] == "waiting" and state["policy"] == "failFast":
                    self._failFastChecks[waiter] = None
                    self._scheduleReconcile()
        for change in publication["changes"]:
            # A queued input below a cancelled owner is withdrawn, even after its cascade.
            if change["type"] != "submission" or change["value"]["status"] != "queued" or change["value"]["type"] != "input":
                continue
            # Only an owner with cancellation intent withdraws it; a later intent's mark runs the cascade then.
            if len(self._intent) == 0:
                continue
            conversation = change["value"]["conversationId"]
            if not self._known(conversationNode(conversation)) or self._belowCancelled({"conversation": conversation}):
                self._cascadePending = True
        for record in updated:
            # Work created below a cancelled owner, even after its cascade, is aborted too.
            if record["id"] in self._unloaded:
                self._scheduleReconcile()
            elif not record["background"] and not record["abortRequested"] and self._belowCancelled(parentOf(record)):
                self._cascadePending = True
        # A cascade that a reservation or step found pending runs with the next commit of any kind.
        if self._cascadePending:
            self._scheduleReconcile()
        self._sweep()
        if not changed:
            return
        self._settleIdle()
        self._kick()

    def _contextRetentionMs(self) -> int:
        """``settings.contextRetentionMs``, or 0 when the host's settings throw. Kept contexts are only a cache, so
        dropping them is safe, while a throw here would escape commit listeners, reconciliation, and the expiry timer."""
        try:
            return self._settings()["contextRetentionMs"]  # type: ignore[no-any-return]
        except Exception as error:  # noqa: BLE001 - reported; the cache is dropped instead
            self._report(error)
            return 0

    def _settleIdle(self) -> None:
        """Resolve idle waiters, and drop each kept context whose conversation has been idle for the retention period."""
        for conversationId in self._idleWaiters.keys():  # noqa: SIM118 - a Waiters, not a dict
            if self._idle(conversationId):
                self._idleWaiters.resolve(conversationId, None)
        now = self._now()
        retention = self._contextRetentionMs()
        for conversationId, kept in list(self._contexts.items()):
            if kept["idleSince"] is not None and now - kept["idleSince"] >= retention:
                del self._contexts[conversationId]
            elif not self._idle(conversationId):
                kept["idleSince"] = None
            elif retention > 0:
                if kept["idleSince"] is None:
                    kept["idleSince"] = now
            else:
                del self._contexts[conversationId]
        self._scheduleExpiry()

    def _scheduleExpiry(self) -> None:
        """Run ``_settleIdle()`` when the earliest idle context expires. The timer never keeps the process alive.
        MISAKA fork: an asyncio timer handle never keeps the event loop running, as an unreferenced Node timer does, so
        upstream's branch for runtimes whose timers cannot be unreferenced (Cloudflare Workers) has no counterpart."""
        retention = self._contextRetentionMs()
        at: int | None = None
        for kept in self._contexts.values():
            if kept["idleSince"] is not None and (at is None or kept["idleSince"] + retention < at):
                at = kept["idleSince"] + retention
        if self._expiry is not None and self._expiry["at"] == at:
            return
        if self._expiry is not None:
            self._expiry["timer"].cancel()
        self._expiry = None
        if at is None or self._closing:
            return

        def fire() -> None:
            self._expiry = None
            self._settleIdle()

        timer = asyncio.get_running_loop().call_later(min(max(0, at - self._now()), MAX_TIMER_DELAY) / 1000, fire)
        self._expiry = {"at": at, "timer": timer}

    # ─── Ownership ───────────────────────────────────────────────────────────

    def _scheduleReconcile(self) -> None:
        if self._reconcileScheduled or self._closing:
            return
        self._reconcileScheduled = True
        queueMicrotask(self._startReconcile)

    def _startReconcile(self) -> None:
        spawn(self._reconcile(), self._tasks)

    def _startDrain(self) -> None:
        spawn(self._drain(), self._tasks)

    async def _reconcile(self) -> None:
        """One commit that applies what committed records imply: abort marks below live owners with cancellation intent
        (spec section 5.4), ``failFast`` marks (spec section 5.5), withdrawn queued inputs below cancelled owners, and
        the final terminal record of every ``completing`` task whose ordinary owned work is gone. The durable records
        are the intent, so this also repairs whatever a crash left unapplied. Resolves idle waiters that the loaded
        edges decide."""
        self._reconcileScheduled = False
        cascade = self._cascadePending
        self._cascadePending = False
        checks = list(self._failFastChecks)
        self._failFastChecks.clear()
        try:

            async def apply(tx: Any) -> None:
                if self._closing:
                    return
                queued = await self._loadScopes(cascade)
                # A cascade from an abandoned owner passes its `restart` reason on; any other intent marks, or upgrades a
                # `restart` mark, as a request. Collected first, so a request wins over a `restart` mark of the same
                # pass.
                marks: dict[TaskId, dict[str, Any]] = {}

                def mark(record: dict[str, Any], reason: str | None) -> None:
                    staged = marks.get(record["id"])
                    if staged is not None and staged["reason"] is None:
                        return
                    if record["abortRequested"] and (record.get("abortReason") is None or reason is not None):
                        return
                    marks[record["id"]] = {"record": record, "reason": reason}

                # Every live foreground task below a live owner with cancellation intent, which is its nearest one: a
                # walk down from each owner with intent does not enter another, whose own walk covers what is below it.
                # Derived again on every pass, as loading edges can reveal a cancelled owner.
                for id in list(self._intent):
                    owner = self._live[id]
                    # Only an owner abandoned after a restart, and nothing else, passes its reason on.
                    restart = owner.get("abortReason") == "restart" and not failedOutcome(owner)
                    for step in self._walkDown(id, enter=lambda below: below not in self._intent):
                        if step["live"] is not None and not step["node"]["background"]:
                            mark(step["live"], "restart" if restart else None)
                for id in checks:
                    waiter = self._live.get(id)
                    if waiter is None or waiter["state"]["status"] != "waiting" or not await self._anyFailed(waiter["state"]["on"]):
                        continue
                    # Every other live task: the failed one keeps its own outcome.
                    for member in waiter["state"]["on"]:
                        other = self._live.get(member)
                        if other is not None and not failedOutcome(other):
                            mark(other, None)
                for staged in marks.values():
                    tx.setTask(withAbortMark(staged["record"], staged["reason"]))
                for id in queued:
                    if self._belowCancelled({"conversation": id}):
                        await self._withdrawInputs(tx, id)
                await self._finalize(tx)

            await self._session.commitWith(apply, self._context)
        except Exception as error:  # noqa: BLE001
            # No extension code runs in this commit: a throw is a storage failure, a host callback, or a bug. None is
            # fixed by running the pass again, so it fails the Session, which reports it.
            self._failSession(error)
        self._settleIdle()

    async def _anyFailed(self, ids: list[TaskId]) -> bool:
        """Whether any of ``ids`` holds or ended with an outcome other than ``completed``."""
        for id in ids:
            record = self._live.get(id)
            if record is None:
                record = await self._storage.task(id, self._context)
            if record is not None and failedOutcome(record):
                return True
        return False

    async def _finalize(self, tx: Any) -> None:
        """Write the terminal record of every ``completing`` task without live ordinary owned work. A worklist: the
        tasks that started holding, and the holders of work that ended. Finalizing one can free the nearest live task
        above it, which goes on the list. Tasks finalized here have no live work below them, so walks down skip them. In
        rounds: the holders the tasks of one round free are checked once, in the next, so a holder of many tasks that end
        together is walked once, not once per task. A held scheduler outcome gets its Harness cleanup here."""
        round_ = list(self._finalizeChecks)
        self._finalizeChecks.clear()
        done: set[TaskId] = set()
        overlay = overlayOf(tx)
        while len(round_) > 0:
            following: dict[TaskId, None] = {}
            for id in round_:
                record = overlay["tasks"].get(id)
                if record is None:
                    record = self._live.get(id)
                if record is None or record["state"]["status"] != "completing" or id in done:
                    continue
                if self._hasOwnedLive(id, overlay, done):
                    continue
                outcome = record["state"]["outcome"]
                terminal = withState(record, {"status": "terminal", "outcome": outcome})
                tx.setTask(terminal)
                overlay["tasks"][id] = terminal
                done.add(id)
                # REMINDER: only the scheduler writes ``faulted`` and ``orphaned`` (spec section 5.4); their cleanup waits
                # for this commit.
                if outcome["status"] == "faulted" or outcome["status"] == "orphaned":
                    await self._settleOutcome(tx, record, outcome)
                above = self._nearestLiveAbove(record, overlay)
                if above is not None and above["state"]["status"] == "completing":
                    following[above["id"]] = None
            round_ = list(following)

    async def _loadScopes(self, queued: bool) -> list[ConversationId]:
        """Load the owner chains of the tasks in ``_unloaded`` and, with ``queued``, of every conversation with queued
        inputs, on the Session line; returns the latter. Reads committed Storage directly, so it may run inside a
        commit callback."""
        for id in list(self._unloaded):
            # An earlier load in this loop may have passed it.
            if id in self._unloaded:
                await self._loadChain({"task": id})
        if not queued:
            return []
        submissions = await scanAll(
            lambda cursor: self._storage.scanSubmissions({"status": "queued"}, SCAN_PAGE_SIZE, cursor, self._context)
        )
        inputs = [submission for submission in submissions if submission["type"] == "input"]
        conversations = list(dict.fromkeys(submission["conversationId"] for submission in inputs))
        for id in conversations:
            await self._loadChain({"conversation": id})
        return conversations

    async def _loadChain(self, start: dict[str, Any], overlay: dict[str, Any] | None = None) -> None:
        """Load the owner edges and task nodes from ``start`` up to its ownerless root. A known task outside
        ``_unloaded`` ends the walk, as its chain is loaded; the ``_unloaded`` tasks passed have a loaded chain
        afterwards. Loading a node can reveal a cancelled owner, so it makes the next reconcile derive cascade marks."""
        passed: list[TaskId] = []
        at: dict[str, Any] | None = start
        while at is not None:
            if "task" in at:
                id: TaskId = at["task"]
                node = self._live.get(id)
                if node is None:
                    node = self._settled.get(id)
                # Created in this commit: the load from its own record covers what is above it.
                if node is None and overlay is not None and id in overlay["tasks"]:
                    break
                if node is not None and id not in self._unloaded:
                    break
                if node is None:
                    record = await self._storage.task(id, self._context)
                    if record is None:
                        return
                    node = nodeOf(record)
                    if record["state"]["status"] == "terminal":
                        self._settle(id, node)
                    # A loaded node can reveal a cancelling owner: the next reconcile derives marks again.
                    self._scheduleReconcile()
                passed.append(id)
                at = parentOf(node)
            else:
                edge = self._edge(at["conversation"], overlay)
                if edge is UNDEFINED:
                    record = await self._storage.conversation(at["conversation"], self._context)
                    owner = None if record is None else record.get("owner")
                    edge = None if owner is None else owner["taskId"]
                    self._setEdge(at["conversation"], edge)
                    # A loaded node can reveal a cancelling owner: the next reconcile derives marks again.
                    self._scheduleReconcile()
                at = None if edge is None else {"task": edge}
        for id in passed:
            self._unloaded.pop(id, None)

    def _setEdge(self, conversationId: ConversationId, owner: TaskId | None) -> None:
        self._edges[conversationId] = owner
        # The conversation now hangs from its owner, or is a root; with nothing below it, it waits for the sweep.
        self._relist(conversationNode(conversationId))
        self._dropIfUnneeded(conversationNode(conversationId))

    def _settle(self, id: TaskId, node: dict[str, Any]) -> None:
        """Know a terminal task a chain load read, so walks pass through it; its own chain is not loaded yet."""
        self._settled[id] = node
        self._unloaded[id] = None
        self._relist(id)
        self._dropIfUnneeded(id)

    def _dropIfUnneeded(self, node: Node) -> None:
        """Queue an ended task or a conversation for the sweep when nothing is listed below it."""
        if not isinstance(node, str) and node in self._live:
            return
        if node in self._below:
            return
        self._dropQueue[node] = None
        self._scheduleSweep()

    def _scheduleSweep(self) -> None:
        """Sweep in a job of its own on the Session line, after the operation underway: a rejected commit, one with
        nothing to write, and a read publish nothing, so the sweep at the end of ``_observe`` would not run for what they
        loaded."""
        if self._sweepScheduled or self._closing:
            return
        self._sweepScheduled = True

        async def job() -> None:
            self._sweepScheduled = False
            self._sweep()

        # Rejected only once the Session is closing or failed, when nothing is kept anyway.
        self._session.readOnLine(job).add_done_callback(_consume)

    def _sweep(self) -> None:
        """Drop the queued nodes that are still unneeded: not live, nothing listed below. Runs once a publication is
        fully tracked, and as a job of its own on the line, never inside an operation: work a commit creates below a
        chain its own callback loaded keeps that chain. A flat loop: unlisting already queued every node on the way up
        that lost its last listed node."""
        for node in list(self._dropQueue):
            if node in self._below:
                continue
            if isinstance(node, str):
                self._edges.pop(conversationOf(node), None)
                self._roots.pop(conversationOf(node), None)
            elif node not in self._live:
                self._settled.pop(node, None)
                self._unloaded.pop(node, None)
        self._dropQueue.clear()

    def _track(self, previous: dict[str, Any] | None, record: dict[str, Any]) -> None:
        """Bring the scheduler's sets in line with a task's committed record. A task that ended may free the nearest live
        task above it: one holding its outcome finalizes, an abort-marked one runs its abort handler; its waiters may
        run."""
        id = record["id"]
        if previous is not None and previous["state"]["status"] == "waiting":
            for on in previous["state"]["on"]:
                deleteFrom(self._waiters, on, id)
        if record["state"]["status"] == "terminal":
            node = nodeOf(record)
            self._live.pop(id, None)
            self._intent.pop(id, None)
            self._runnable.pop(id, None)
            self._finalizeChecks.pop(id, None)
            self._settled[id] = node
            # Before anything is forgotten: the walk up to the task it held passes the ended tasks between them. Its chain
            # is loaded, as every commit that ends a task loads the chains of `_unloaded` first.
            self._release(node)
            for waiter in list(self._waiters.get(id) or ()):
                self._runnable[waiter] = None
            self._relist(id)
            self._dropIfUnneeded(id)
            return
        self._live[id] = record
        if previous is None:
            if not self._known(parentNode(record)):
                self._unloaded[id] = None
            self._relist(id)
        if cancellationIntent(record):
            self._intent[id] = None
        else:
            self._intent.pop(id, None)
        if record["state"]["status"] == "completing":
            self._runnable.pop(id, None)
            self._finalizeChecks[id] = None
        else:
            self._runnable[id] = None
        if record["state"]["status"] == "waiting":
            for on in record["state"]["on"]:
                addTo(self._waiters, on, id)

    def _release(self, node: dict[str, Any], overlay: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """The nearest live task above ended ordinary work may be free now: one holding its outcome, or an aborting
        one."""
        above = self._nearestLiveAbove(node, overlay)
        if above is not None and above["state"]["status"] == "completing":
            self._finalizeChecks[above["id"]] = None
        elif above is not None and above["abortRequested"] is True:
            self._runnable[above["id"]] = None
        return above

    def _relist(self, start: Node) -> None:
        """List ``start`` under its parent node in ``_below`` while it is a live task or has nodes listed below it, and
        unlist it otherwise; then its parent, as far up as that changes. So ``_below`` holds exactly the tree around live
        work; a parent that loses its last listed node is queued for the sweep. A loop, as ownership chains can be deeper
        than the call stack."""
        node: Node = start
        while True:
            needed = (not isinstance(node, str) and node in self._live) or node in self._below
            parent: Any
            if isinstance(node, str):
                parent = self._edges.get(conversationOf(node), UNDEFINED)
            else:
                known = self._live.get(node)
                if known is None:
                    known = self._settled.get(node)
                if known is None:
                    return
                parent = parentNode(known)
            if parent is UNDEFINED:
                return
            if parent is None:
                root = conversationOf(node)  # type: ignore[arg-type]
                if needed:
                    self._roots[root] = None
                else:
                    self._roots.pop(root, None)
                return
            if (node in (self._below.get(parent) or ())) == needed:
                return
            if needed:
                first = parent not in self._below
                addTo(self._below, parent, node)
                if not first:
                    return
            else:
                deleteFrom(self._below, parent, node)
                if parent in self._below:
                    return
                self._dropIfUnneeded(parent)
            node = parent

    def _nearestLiveAbove(self, node: dict[str, Any], overlay: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """The nearest live task above ended ordinary work ``node``, in the overlay's view: the one that work held, if
        any. Work at or below a background task holds nothing above it."""
        current = node
        while not current["background"]:
            owner = current.get("owner")
            if owner is None:
                owner = self._edge(current["conversationId"], overlay)
            if owner is UNDEFINED or owner is None:
                return None
            record = None if overlay is None else overlay["tasks"].get(owner)
            if record is None:
                record = self._live.get(owner)
            if record is not None and record["state"]["status"] != "terminal":
                return record
            above = record if record is not None else self._settled.get(owner)
            if above is None:
                return None
            current = above
        return None

    def _known(self, node: Node) -> bool:
        """Whether the chain above ``node`` is loaded, without walking: known tasks outside ``_unloaded`` have a loaded
        chain."""
        if not isinstance(node, str):
            return (node in self._live or node in self._settled) and node not in self._unloaded
        owner = self._edges.get(conversationOf(node), UNDEFINED)
        return owner is None or (owner is not UNDEFINED and self._known(owner))

    def _edge(self, id: ConversationId, overlay: dict[str, Any] | None = None) -> Any:
        """Owner task of a conversation, ``None`` when ownerless, ``UNDEFINED`` while not loaded."""
        if overlay is not None and id in overlay["edges"]:
            return overlay["edges"][id]
        return self._edges.get(id, UNDEFINED)

    def _node(self, id: TaskId, overlay: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if overlay is not None and overlay["tasks"].get(id) is not None:
            return overlay["tasks"][id]  # type: ignore[no-any-return]
        found = self._live.get(id)
        return found if found is not None else self._settled.get(id)

    def _above(self, start: dict[str, Any], overlay: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        """Walk up from ``start``: owner tasks and conversations, ending at an ownerless root or an edge not loaded
        yet."""
        at: dict[str, Any] | None = start
        while at is not None:
            if "task" in at:
                node = self._node(at["task"], overlay)
                if node is None:
                    yield {"unknown": True}
                    return
                yield {"task": at["task"], "node": node}
                at = parentOf(node)
            else:
                yield {"conversation": at["conversation"]}
                edge = self._edge(at["conversation"], overlay)
                if edge is UNDEFINED:
                    yield {"unknown": True}
                    return
                at = None if edge is None else {"task": edge}

    def _walkDown(
        self,
        start: Node,
        *,
        overlay: dict[str, Any] | None = None,
        crossBackground: bool = False,
        enter: Callable[[TaskId], bool] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """The tasks below ``start`` that a walk down reaches: those ``_below`` lists and, under an overlay, the commit's
        new tasks, with its candidates in place of committed records. Background tasks are reached but not entered,
        unless ``crossBackground``, and neither are the tasks ``enter`` rejects. A loop, as chains can be deeper than the
        call stack. Each step is ``{"id", "node", "live"}``; ``live`` is ``None`` for a terminal task."""
        links = None if overlay is None else self._links(overlay)
        pending: list[Node] = [start]
        while len(pending) > 0:
            at = pending.pop()
            # Copied: a caller may stage changes while it walks.
            for children in (self._below.get(at), None if links is None else links.get(at)):
                for child in list(children or ()):
                    if isinstance(child, str):
                        pending.append(child)
                        continue
                    record = None if overlay is None else overlay["tasks"].get(child)
                    if record is None:
                        record = self._live.get(child)
                    node = record if record is not None else self._settled.get(child)
                    if node is None:
                        continue
                    live = record if record is not None and record["state"]["status"] != "terminal" else None
                    yield {"id": child, "node": node, "live": live}
                    if (crossBackground or not node["background"]) and (enter is None or enter(child)):
                        pending.append(child)

    def _links(self, overlay: dict[str, Any]) -> dict[Node, dict[Node, None]]:
        """The links walks down need for the commit's new tasks: each one under its parent node, then each node above it
        that ``_below`` does not list yet, until one it lists, a live task (always listed), or the chain's end."""
        if overlay.get("links") is not None:
            return overlay["links"]  # type: ignore[no-any-return]
        links: dict[Node, dict[Node, None]] = {}
        for record in overlay["tasks"].values():
            if record["id"] in self._live or record["id"] in self._settled:
                continue
            child: Node = record["id"]
            parent: Any = parentNode(record)
            while parent is not UNDEFINED and parent is not None and child not in (links.get(parent) or ()):
                addTo(links, parent, child)
                if not isinstance(parent, str) and (parent in self._live or parent in overlay["tasks"]):
                    break
                above: Any
                if isinstance(parent, str):
                    above = self._edge(conversationOf(parent), overlay)
                else:
                    fields = self._settled.get(parent)
                    above = UNDEFINED if fields is None else parentNode(fields)
                if above is not UNDEFINED and above is not None and parent in (self._below.get(above) or ()):
                    break
                child = parent
                parent = above
        overlay["links"] = links
        return links

    def _ownedLive(
        self,
        owner: TaskId,
        overlay: dict[str, Any] | None = None,
        first: bool = False,
        done: set[TaskId] | None = None,
    ) -> list[TaskId]:
        """The live ordinary owned work of ``owner`` (spec section 5.5): the live non-background tasks below it that a
        walk down reaches without entering a background task; with ``first``, only the first. Candidates in
        ``overlay`` replace committed records; the walk does not enter tasks in ``done``, which have none. Owner chains
        must be loaded."""
        found: list[TaskId] = []
        walk = self._walkDown(owner, overlay=overlay, enter=None if done is None else (lambda id: id not in done))
        for step in walk:
            if step["live"] is None or step["node"]["background"]:
                continue
            found.append(step["id"])
            if first:
                break
        return found

    def _hasOwnedLive(self, owner: TaskId, overlay: dict[str, Any] | None = None, done: set[TaskId] | None = None) -> bool:
        return len(self._ownedLive(owner, overlay, True, done)) > 0

    def _inScope(self, start: dict[str, Any], scope: dict[str, Any], crossBackground: bool = False) -> bool | None:
        """Whether ordinary traversal from ``scope`` reaches ``start``: walking up reaches the scope's conversation, or
        an ownerless one for ``roots``, without crossing a background owner, unless ``crossBackground``. ``None`` while
        an edge is not loaded."""
        for step in self._above(start):
            if "unknown" in step:
                return None
            if "conversation" in step:
                if "conversation" in scope and step["conversation"] == scope["conversation"]:
                    return True
            elif step["node"]["background"] and not crossBackground:
                return False
        return "roots" in scope

    def _belowCancelled(self, start: dict[str, Any]) -> bool:
        """Whether a live owner's cancellation intent reaches ``start``: walking up finds an owner with intent before a
        background owner without it. Terminal owners never cascade (spec section 5.4)."""
        return self._cancellingOwner(start) is not None

    def _cancellingOwner(self, start: dict[str, Any]) -> dict[str, Any] | None:
        """The nearest live owner above ``start`` whose cancellation intent reaches it, if any; see
        ``_belowCancelled``."""
        # Nothing cancels, the common case: no walk.
        if len(self._intent) == 0:
            return None
        for step in self._above(start):
            if "unknown" in step:
                return None
            if "task" not in step:
                continue
            live = self._live.get(step["task"])
            if live is not None and cancellationIntent(live):
                return live
            if step["node"]["background"]:
                return None
        return None

    def _failSession(self, error: BaseException) -> None:
        """Fail the Session, which reports it, for a throw in one of the scheduler's own commits; ignored once
        closing."""
        if not self._closing:
            self._session.fail(error)

    def _seal(self) -> None:
        """Close listener: runs synchronously once admission is sealed, before ``join()``, or when the Session fails."""
        self._closing = True
        try:
            self._unsubscribeRegistry()
        except Exception as error:  # noqa: BLE001 - reported; sealing goes on
            self._report(error)
        error = closedError(self._session)
        self._taskWaiters.rejectAll(error)
        self._idleWaiters.rejectAll(error)
        self._contexts.clear()
        if self._expiry is not None:
            self._expiry["timer"].cancel()
        self._expiry = None

    def _kick(self) -> None:
        self._dirty = True
        if self._draining or not self._enabled or self._closing:
            return
        self._draining = True
        # Never commit synchronously from a commit or registry listener.
        queueMicrotask(self._startDrain)

    async def _drain(self) -> None:
        try:
            while self._dirty and self._enabled and not self._closing:
                self._dirty = False
                for reservation in await self._reserve():
                    self._start(reservation)
        except Exception as error:  # noqa: BLE001
            # As in `_reconcile()`: the reservation commit runs no extension code.
            self._failSession(error)
        finally:
            self._draining = False
            # A wakeup that arrived after the loop's last check still needs its pass.
            if self._dirty:
                self._kick()

    async def _reserve(self) -> list[_Reservation]:
        """Reserve every eligible task in one commit; orphan abort-marked tasks no definition can take, unless abandoned
        after a restart. The first pass after open only abort-marks the abandoned tasks, so later passes see the
        marks."""
        reservations: list[_Reservation] = []
        abandoned: list[TaskId] = []
        try:

            async def reserve(tx: Any) -> None:
                nonlocal abandoned
                if not self._enabled or self._closing:
                    return
                for id in self._abandoned:
                    found = self._live.get(id)
                    if found is not None and not found["abortRequested"]:
                        tx.setTask(withAbortMark(found, "restart"))
                if len(self._abandoned) > 0:
                    abandoned = list(self._abandoned)
                    return
                await self._loadScopes(False)
                # Taken once per pass, and only when some task is a candidate.
                snapshot: Any = None
                for id in list(self._runnable):
                    record = self._live.get(id)
                    if record is None or record["state"]["status"] == "completing" or id in self._invocations:
                        self._runnable.pop(id, None)
                        continue
                    # It returns once what it waits for ends, its record changes, or the work below it ends.
                    if len(self._waitingOn(record, True)) > 0:
                        self._runnable.pop(id, None)
                        continue
                    mode = "abort" if record["abortRequested"] else "run"
                    # Work below an owner with cancellation intent waits for its cascade mark instead of running a phase;
                    # schedule the cascade, which may not have run yet.
                    if mode == "run" and not record["background"] and self._belowCancelled(parentOf(record)):
                        self._cascadePending = True
                        self._scheduleReconcile()
                        continue
                    if snapshot is None:
                        snapshot = self._registry.snapshot()
                    resolution = self._resolve(record, snapshot)
                    if resolution["kind"] == "blocked":
                        # An abandoned task waits for its definition, so its abort handler can clean up.
                        if mode == "abort" and record.get("abortReason") is None:
                            await self._terminate(tx, record, {"status": "orphaned", "reason": resolution["reason"]})
                        continue
                    if resolution["record"] is not record or record["state"]["status"] != "running":
                        tx.setTask(
                            withState(
                                resolution["record"],
                                {"status": "running", "checkpoint": resolution["record"]["state"]["checkpoint"]},
                            )
                        )
                    # Registered on the line, so marks and later reservations see it and close joins it; `_end` makes it
                    # a candidate again.
                    self._runnable.pop(id, None)
                    invocation = self._createInvocation(record, mode)
                    reservations.append(_Reservation(invocation, resolution["task"], snapshot))

            await self._session.commitWith(reserve, self._context)
        except BaseException:
            for reservation in reservations:
                if self._invocations.get(reservation.invocation.taskId) is reservation.invocation:
                    del self._invocations[reservation.invocation.taskId]
                self._runnable[reservation.invocation.taskId] = None
                reservation.invocation.finish()
            raise
        # Marked, or already marked or gone: done with them. Reserve again, now seeing the marks.
        for id in abandoned:
            self._abandoned.pop(id, None)
        if len(abandoned) > 0:
            self._dirty = True
        return reservations

    def _waitingOn(self, record: dict[str, Any], first: bool = False) -> list[TaskId]:
        """Live tasks a task waits for before its next invocation: its live ordinary owned work when abort-marked, since
        abort runs bottom-up, otherwise the live part of the ``on`` of a wait."""
        if record["abortRequested"]:
            return self._ownedLive(record["id"], first=first)
        if record["state"]["status"] != "waiting":
            return []
        if not first:
            return [id for id in record["state"]["on"] if id in self._live]
        live = next((id for id in record["state"]["on"] if id in self._live), None)
        return [] if live is None else [live]

    def _resolve(self, record: dict[str, Any], snapshot: Any) -> dict[str, Any]:
        """Resolve the record's definition by kind, migrating an older stored version."""
        fit = self._fit(record, snapshot.task(record["kind"]))
        if "reason" in fit:
            return {"kind": "blocked", "reason": fit["reason"]}
        if not fit["migrates"]:
            return {"kind": "ready", "task": fit["task"], "record": record}
        definition = erased(fit["task"])
        try:
            if definition.get("migrate") is None:
                raise missingMigration(record, definition)
            migrated = definition["migrate"](record["input"], record["state"]["checkpoint"], record["version"])
            state = {**record["state"], "checkpoint": copyJson(migrated["checkpoint"])}
            migratedRecord = {**record, "version": definition["version"], "input": copyJson(migrated["input"]), "state": state}
            return {"kind": "ready", "task": fit["task"], "record": migratedRecord}
        except Exception as error:  # noqa: BLE001 - a failing migration blocks the task and is reported
            self._failedMigrations[record["id"]] = {"task": fit["task"], "error": error}
            self._report(error)
            return {"kind": "blocked", "reason": "migration_failed"}

    def _fit(self, record: dict[str, Any], task: dict[str, Any] | None) -> dict[str, Any]:
        if task is None:
            return {"reason": "missing_task"}
        version = task["definition"]["version"]
        if version == record["version"]:
            return {"task": task, "migrates": False}
        if version < record["version"]:
            return {"reason": "task_too_old"}
        failed = self._failedMigrations.get(record["id"])
        if failed is not None and failed["task"] is task:
            return {"reason": "migration_failed", "error": failed["error"]}
        return {"task": task, "migrates": True}

    @property
    def indexSizes(self) -> dict[str, int]:
        """Sizes of the ownership indexes, for tests that bound the scheduler's memory."""
        return {
            "live": len(self._live),
            "settled": len(self._settled),
            "edges": len(self._edges),
            "below": sum(len(children) for children in self._below.values()),
            "roots": len(self._roots),
            "unloaded": len(self._unloaded),
            "dropQueue": len(self._dropQueue),
        }

    @eager
    async def inspect(self, snapshot: Any) -> dict[str, Any]:
        """Scheduling state and every live task with its derived state, read on the Session line. Runs no task code: a
        pending migration shows as ``ready`` with ``migrates``, and only a migration the scheduler already tried, or one
        that cannot exist, shows as failed."""
        await self._loadScopes(False)
        tasks = [{"record": record, "state": self._inspectTask(record, snapshot)} for record in self._live.values()]
        scheduling = "closing" if self._closing else "running" if self._enabled else "paused"
        return {"scheduling": scheduling, "tasks": tasks}

    def _inspectTask(self, record: dict[str, Any], snapshot: Any) -> dict[str, Any]:
        if record["id"] in self._invocations:
            return {"kind": "running"}
        if record["state"]["status"] == "completing":
            return {"kind": "completing"}
        on = self._waitingOn(record)
        if len(on) > 0:
            return {"kind": "waiting", "on": on}
        fit = self._fit(record, snapshot.task(record["kind"]))
        if "reason" in fit:
            return {"kind": "blocked", **fit}
        if fit["migrates"] and fit["task"]["definition"].get("migrate") is None:
            return {
                "kind": "blocked",
                "reason": "migration_failed",
                "error": missingMigration(record, erased(fit["task"])),
            }
        return {"kind": "ready", "migrates": fit["migrates"]}

    def _createInvocation(self, record: dict[str, Any], mode: str) -> _Invocation:
        controller = AbortController()
        invocation = _Invocation(record, mode, controller, withAbortSignal(controller.signal, self._context))
        self._invocations[record["id"]] = invocation
        return invocation

    def _start(self, reservation: _Reservation) -> None:
        invocation = reservation.invocation

        async def execute() -> None:
            discarded = False
            try:
                if invocation.mode == "run":
                    await self._run(reservation)
                else:
                    await self._runAbort(reservation)
            except Exception as error:  # noqa: BLE001 - an escaped failure is reported
                self._report(error)
            except GeneratorExit:
                # The coroutine is dropped while a handler is still suspended, as when the loop closes on a crashed
                # Harness. The continuation of a promise that never settles never runs, so nothing here may either.
                discarded = True
                raise
            finally:
                if not discarded:
                    self._end(invocation)
                    invocation.finish()
                    self._kick()

        spawn(execute(), self._tasks)

    async def _run(self, reservation: _Reservation) -> None:
        """Run phase handlers, each preceded by a step that decides on the line whether the invocation continues."""
        invocation = reservation.invocation
        state = _PhaseState(reservation.task, reservation.snapshot)
        phase = _Phase(lambda: state.snapshot, lambda: state.task)
        runtime = TaskRuntimeImpl(self, invocation, phase)
        previous: dict[str, Any] | None = None
        while True:
            def decideWith(tx: Any, running: dict[str, Any], previous: dict[str, Any] | None = previous) -> Any:
                return self._decide(tx, running, previous, state)

            current = await self._step(invocation, decideWith)
            # Close may seal between the decision and dispatch.
            if current is None or self._closing:
                return
            checkpoint = current["state"]["checkpoint"]
            # Each phase handler resolves its agent afresh, at first use.
            phase.agent = None
            try:
                await awaitValue(erased(state.task)["phases"][checkpoint["phase"]](current, runtime, invocation.context))
                previous = {"checkpoint": checkpoint}
            except BaseException as error:  # noqa: BLE001 - an uncaught throw is a fault the next step writes
                previous = {"checkpoint": checkpoint, "failure": {"error": thrownBy(error)}}

    def _decide(
        self, tx: Any, current: dict[str, Any], previous: dict[str, Any] | None, state: _PhaseState
    ) -> bool | dict[str, Any]:
        """Precedence rules for a run invocation, on the line. Rules 1 (terminal, ``completing``, or ``waiting``) and 2
        (closing) are applied by ``_step``. Returns whether the invocation continues with the next phase."""
        # 3. abort mark: end; a fresh abort invocation starts once the task's ordinary owned work is gone.
        if current["abortRequested"]:
            return False
        # An owner's cancellation intent ends it too, before its cascade marks it; reservation then holds it back.
        if not current["background"] and self._belowCancelled(parentOf(current)):
            self._cascadePending = True
            self._scheduleReconcile()
            return False
        if previous is None:
            return True
        # 4. uncaught error.
        if previous.get("failure") is not None:
            return {"fault": previous["failure"]["error"]}
        # 6. no durable progress.
        if jsonEqual(current["state"]["checkpoint"], previous["checkpoint"]):
            message = f"Task {current['kind']} phase {previous['checkpoint']['phase']} returned without durable progress"
            return {"fault": RuntimeError(message)}
        # 5. progress: refresh the snapshot; hand over to a replacement definition that can take the task.
        state.snapshot = self._registry.snapshot()
        following = state.snapshot.task(current["kind"])
        if following is not state.task:
            if following is not None and canReserve(following, current):
                tx.setTask(withState(current, {"status": "pending", "checkpoint": current["state"]["checkpoint"]}))
                return False
            if state.reported is None or state.reported["task"] is not following:
                state.reported = {"task": following}
                cause = "missing_task" if following is None else "incompatible_task"
                error = RuntimeError(f"Task {current['id']} keeps running under its old {current['kind']} definition")
                error.cause = cause  # type: ignore[attr-defined]
                self._report(error)
        return True

    async def _runAbort(self, reservation: _Reservation) -> None:
        """Run the abort handler once; rules 1, 2, and 4 apply, and returning without an outcome faults."""
        invocation = reservation.invocation
        current = self._live.get(invocation.taskId)
        if current is None or self._closing:
            return
        failure: dict[str, Any] | None = None
        try:
            phase = _Phase(lambda: reservation.snapshot, lambda: reservation.task)
            runtime = TaskRuntimeImpl(self, invocation, phase)
            await awaitValue(erased(reservation.task)["abort"](current, runtime, invocation.context))
        except BaseException as error:  # noqa: BLE001 - a failing abort handler faults the task
            failure = {"error": thrownBy(error)}
        message = f"Abort handler of task {invocation.taskId} returned without a terminal outcome"
        fault = failure["error"] if failure is not None else RuntimeError(message)
        await self._step(invocation, lambda tx, running: {"fault": fault})

    async def _step(self, invocation: _Invocation, decide: Callable[[Any, dict[str, Any]], Any]) -> dict[str, Any] | None:
        """One synchronous decision on the Session line. A task that is no longer running (rule 1: terminal,
        ``completing``, or ``waiting``) or a closing Harness (rule 2) ends the invocation without a write; otherwise
        ``decide`` may stage a write and returns whether the invocation continues. Ending happens inside the callback,
        before a fault's Harness cleanup. A rejected step, such as admission after close, also ends the invocation."""
        try:

            async def decideOnLine(tx: Any) -> dict[str, Any] | None:
                found = self._live.get(invocation.taskId)
                current = found if found is not None and found["state"]["status"] == "running" else None
                decision = decide(tx, current) if current is not None and not self._closing else False
                if decision is True:
                    return current
                self._end(invocation)
                if decision is not False:
                    message = errorMessage(decision["fault"])
                    assert current is not None
                    await self._terminate(tx, current, {"status": "faulted", "error": {"message": message}})
                return None

            return await self._session.commitWith(decideOnLine, self._context)  # type: ignore[no-any-return]
        except Exception as error:  # noqa: BLE001
            # The step writes the scheduler's own decision, a fault or a terminal record; a throw there would otherwise
            # leave the task running, to be reserved and run again.
            self._end(invocation)
            self._failSession(error)
            return None

    async def _terminate(self, tx: Any, record: dict[str, Any], outcome: dict[str, Any]) -> None:
        """Write an outcome the scheduler decided. While the task's ordinary owned work is live it holds as
        ``completing`` and its Harness cleanup waits for the final commit (spec section 5.5, rule 4); otherwise it is
        terminal with its cleanup."""
        await self._loadScopes(False)
        if self._hasOwnedLive(record["id"], overlayOf(tx)):
            tx.setTask(withState(record, {"status": "completing", "outcome": outcome}))
            return
        tx.setTask(withState(record, {"status": "terminal", "outcome": outcome}))
        await self._settleOutcome(tx, record, outcome)

    async def _commitState(self, tx: Any, invocation: _Invocation, current: dict[str, Any], following: dict[str, Any]) -> None:
        """Replace a running task's state with what it committed. A terminal state holds as ``completing`` while
        ordinary owned work is live, judged on the commit's candidates, so work the same commit creates below the task
        counts. A wait is validated first."""
        if following["status"] == "waiting":
            await self._validateWait(tx, invocation, current, following["on"], following["policy"])
        if following["status"] == "terminal":
            overlay = overlayOf(tx)
            await self._loadScopes(False)
            for record in list(overlay["tasks"].values()):
                await self._loadChain(parentOf(record), overlay)
            if self._hasOwnedLive(current["id"], overlay):
                tx.setTask(withState(current, {"status": "completing", "outcome": following["outcome"]}))
                return
        tx.setTask(withState(current, following))

    async def _validateWait(
        self, tx: Any, invocation: _Invocation, current: dict[str, Any], on: list[TaskId], policy: str
    ) -> None:
        """A wait names existing tasks, and ``failFast`` only tasks the waiter owns. It may not close a cycle in the
        wait-for graph, whose edges run from each task to what it waits on and to its live ordinary owned work (spec
        section 5.5); waiting on itself or on an owner holding it are the shortest such cycles. An abort handler cannot
        wait.

        Invariant: the wait-for graph is acyclic. Only a wait adds an edge between existing tasks, as a new task has only
        incoming edges, so checking each wait maintains it."""
        if invocation.mode == "abort":
            raise RuntimeError(f"Abort handler of task {current['id']} cannot wait")
        overlay = overlayOf(tx)
        targets: list[TaskId] = []
        for id in on:
            member = overlay["tasks"].get(id)
            if member is None:
                member = self._live.get(id)
            if member is None:
                member = await self._storage.task(id, self._context)
            if member is None:
                raise RuntimeError(f"Task {id} does not exist")
            if policy == "failFast" and member.get("owner") != current["id"]:
                raise RuntimeError(f"Task {current['id']} can wait failFast only on tasks it owns; {id} is not one")
            # A live child already holds the waiter (task-owned tasks are never background): waiting on it adds no edge.
            # A terminal task has no edges.
            if member["state"]["status"] != "terminal" and member.get("owner") != current["id"]:
                targets.append(id)
        if len(targets) == 0:
            return
        cycle = await self._waitCycle(current["id"], targets, overlay)
        if cycle is not None:
            path = " -> ".join(str(id) for id in [current["id"], *cycle])
            raise RuntimeError(f"Task {current['id']} cannot wait on {cycle[0]}: {path} is a wait cycle")

    async def _waitCycle(self, task: TaskId, on: list[TaskId], overlay: dict[str, Any]) -> list[TaskId] | None:
        """The path from a member of ``on`` back to ``task`` in the wait-for graph, if the wait would close a cycle
        (spec section 5.1). An edge X -> Y means X cannot settle before Y: Y is in the ``on`` of waiting X that is not
        abort-marked, or Y is live ordinary owned work of X, which holds X as ``completing``. Each task is visited once
        and each subtree walked once, so a check is bounded by the live work it reaches."""
        # Owned work is found by walks down, which need the chains of every live task loaded.
        await self._loadScopes(False)
        origin: dict[TaskId, TaskId | None] = {}
        walked: set[TaskId] = set()
        pending: list[TaskId] = []

        def reach(id: TaskId, via: TaskId | None) -> None:
            if id in origin:
                return
            origin[id] = via
            pending.append(id)

        def path(end: TaskId) -> list[TaskId]:
            steps: list[TaskId] = []
            at: TaskId | None = end
            while at is not None:
                steps.insert(0, at)
                at = origin.get(at)
            return steps

        for id in on:
            reach(id, None)
        while len(pending) > 0:
            id = pending.pop()
            if id == task:
                return path(id)
            record = overlay["tasks"].get(id)
            if record is None:
                record = self._live.get(id)
            if record is None or record["state"]["status"] == "terminal":
                continue
            # An abort-marked task waits only for its owned work, as abort runs bottom-up (`_waitingOn`).
            if record["state"]["status"] == "waiting" and not record["abortRequested"]:
                for member in record["state"]["on"]:
                    reach(member, id)
            if id in walked:
                continue
            walked.add(id)
            # Below a walked task the walk does not go again: its own walk covered what lies below.
            found: list[TaskId] = []
            for step in self._walkDown(id, overlay=overlay, enter=lambda below: below not in walked):
                if step["live"] is None or step["node"]["background"]:
                    continue
                found.append(step["id"])
                reach(step["id"], id)
            walked.update(found)
        return None

    def _end(self, invocation: _Invocation) -> None:
        """End an invocation: its runtime operations reject from now on, its signal aborts, its watches stop, and its
        task is free."""
        if invocation.ended:
            return
        invocation.ended = True
        if self._invocations.get(invocation.taskId) is invocation:
            del self._invocations[invocation.taskId]
            if invocation.taskId in self._live:
                self._runnable[invocation.taskId] = None
        for watch in list(invocation.watches):
            spawn(_stopWatch(watch), self._tasks)
        # Pending waits bound to the invocation, such as a tool's waitForTask(), reject with it.
        invocation.controller.abort(endedError(invocation))

    def _idle(self, conversationId: ConversationId | None) -> bool:
        """No live non-background task in the scope; a task whose owner edges are not loaded yet counts as inside."""
        scope = {"roots": True} if conversationId is None else {"conversation": conversationId}
        # Tasks whose chain is not loaded are not listed below their scope yet; one counts as inside unless its walk up
        # decides otherwise.
        for id in list(self._unloaded):
            record = self._live.get(id)
            if record is not None and not record["background"] and self._inScope(parentOf(record), scope) is not False:
                return False
        starts = list(self._roots) if conversationId is None else [conversationId]
        for start in starts:
            for step in self._walkDown(conversationNode(start)):
                if step["live"] is not None and not step["node"]["background"]:
                    return False
        return True

    # ─── Invocation runtime ──────────────────────────────────────────────────

    @eager
    async def _read(self, invocation: _Invocation, read: Callable[[], Any]) -> Any:
        """Run a committed-state read unless the invocation has ended."""
        if invocation.ended:
            raise endedError(invocation)
        return await read()

    def _gated(self, invocation: _Invocation, change: Callable[[Any, dict[str, Any]], Any], context: Context) -> asyncio.Future[Any]:
        """Commit after rereading the task on the line and gating the invocation."""
        if invocation.ended:
            return rejectedPromise(endedError(invocation))

        async def gated(tx: Any) -> Any:
            if invocation.ended:
                raise endedError(invocation)
            if self._closing:
                raise closedError(self._session)
            found = self._live.get(invocation.taskId)
            if found is None:
                raise RuntimeError(f"Task {invocation.taskId} is terminal")
            if found["state"]["status"] != "running":
                raise RuntimeError(f"Task {invocation.taskId} is {found['state']['status']}")
            if invocation.mode == "run" and found["abortRequested"]:
                raise RuntimeError(f"Task {invocation.taskId} has a durable abort mark")
            return await awaitValue(change(tx, found))

        return self._session.commitWith(  # type: ignore[no-any-return]
            gated, context, {"conversationId": invocation.conversationId, "taskId": invocation.taskId}
        )

    async def _sleep(self, invocation: _Invocation, until: float, context: Context) -> None:
        """Wait until the Harness clock reaches ``until``, rechecking it after every timer."""
        if invocation.ended:
            raise endedError(invocation)
        signals = [invocation.controller.signal]
        if context.abortSignal is not None:
            signals.append(context.abortSignal)
        signal = AbortSignal.any(signals)
        while True:
            signal.throwIfAborted()
            remaining = until - self._now()
            if remaining <= 0:
                return
            await delay(min(remaining, MAX_TIMER_DELAY), signal)

    async def _watchDoc(self, invocation: _Invocation, args: tuple[Any, ...]) -> Any:
        if invocation.ended:
            raise endedError(invocation)
        watch = await self._session.watchDoc(*args)
        if watch is None:
            return None
        if invocation.ended:
            spawn(_stopWatch(watch), self._tasks)
            raise endedError(invocation)
        invocation.watches[watch] = None
        watch.closed.add_done_callback(lambda done: invocation.watches.pop(watch, None))
        return watch


async def _stopWatch(watch: Any) -> None:
    await watch.stop()


def withAbortMark(record: dict[str, Any], reason: str | None = None) -> dict[str, Any]:
    """The record with an abort mark: ``restart`` for an abandonment; without a reason, a request, which replaces
    ``restart``."""
    rest = {key: value for key, value in record.items() if key != "abortReason"}
    return {**rest, "abortRequested": True, **({} if reason is None else {"abortReason": reason})}


def cancellationIntent(record: dict[str, Any]) -> bool:
    """A live owner's durable cancellation intent: its abort mark, or a held outcome other than ``completed``."""
    return record["state"]["status"] != "terminal" and (record["abortRequested"] or failedOutcome(record))


def failedOutcome(record: dict[str, Any]) -> bool:
    """Whether the record holds or ends with an outcome other than ``completed``."""
    state = record["state"]
    return state["status"] in ("completing", "terminal") and state["outcome"]["status"] != "completed"


def parentOf(node: dict[str, Any]) -> dict[str, Any]:
    """Where a walk up the ownership tree continues: an owner task, or a conversation."""
    return {"task": node["owner"]} if node.get("owner") is not None else {"conversation": node["conversationId"]}


def nodeOf(record: dict[str, Any]) -> dict[str, Any]:
    """The immutable ownership fields of a task."""
    return {
        "conversationId": record["conversationId"],
        **({} if record.get("owner") is None else {"owner": record["owner"]}),
        "background": record["background"],
    }


def overlayOf(tx: Any) -> dict[str, Any]:
    """Candidate records a commit staged; they override committed records in ownership walks."""
    tasks = {record["id"]: record for record in tx.stagedTasks()}
    edges = {
        record["id"]: (None if record.get("owner") is None else record["owner"]["taskId"])
        for record in tx.stagedConversations()
    }
    return {"tasks": tasks, "edges": edges}


def addTo(map: dict[Any, dict[Any, None]], key: Any, value: Any) -> None:
    values = map.get(key)
    if values is None:
        map[key] = {value: None}
    else:
        values[value] = None


def deleteFrom(map: dict[Any, dict[Any, None]], key: Any, value: Any) -> None:
    values = map.get(key)
    if values is None:
        return
    values.pop(value, None)
    if len(values) == 0:
        del map[key]


def _consume(future: asyncio.Future[Any]) -> None:
    if not future.cancelled():
        future.exception()


async def _firstSettled(*futures: asyncio.Future[Any]) -> Any:
    """``Promise.race(futures)`` of futures that keep running: settles as the first does. Shielded, so a caller that
    stops waiting cancels neither."""
    shielded = [asyncio.shield(future) for future in futures]
    try:
        done, _ = await asyncio.wait(shielded, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for future in shielded:
            if not future.done():
                future.cancel()
            future.add_done_callback(_consume)
    first = next(future for future in shielded if future in done)
    return first.result()


def memoOf(record: dict[str, Any] | None, name: str) -> Any:
    """Own memo entry only; memo names such as ``toString`` must not resolve to inherited properties. ``UNDEFINED`` when
    there is none."""
    memos = None if record is None else record.get("memos")
    return memos[name] if memos is not None and name in memos else UNDEFINED


def missingMigration(record: dict[str, Any], definition: dict[str, Any]) -> Exception:
    return RuntimeError(f"Task {record['kind']} version {definition['version']} has no migration from {record['version']}")


def erased(task: dict[str, Any]) -> dict[str, Any]:
    return task["definition"]  # type: ignore[no-any-return]


def endedError(invocation: _Invocation) -> Exception:
    return RuntimeError(f"Task {invocation.taskId} invocation has ended")


def withState(record: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Replace a live record's state; memos disappear once an outcome is decided."""
    if state["status"] != "terminal" and state["status"] != "completing":
        return {**record, "state": state}
    rest = {key: value for key, value in record.items() if key != "memos"}
    return {**rest, "state": state}


def canReserve(task: dict[str, Any], record: dict[str, Any]) -> bool:
    """Whether a definition can take the task at reservation: same version, or newer with a migration."""
    definition = task["definition"]
    return definition["version"] == record["version"] or (
        definition["version"] > record["version"] and definition.get("migrate") is not None
    )


async def delay(ms: float, signal: AbortSignal) -> None:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[None] = loop.create_future()

    def onAbort() -> None:
        handle.cancel()
        if not future.done():
            future.set_exception(abortError(signal))

    def fire() -> None:
        signal.removeEventListener("abort", onAbort)
        if not future.done():
            future.set_result(None)

    handle = loop.call_later(ms / 1000, fire)
    signal.addEventListener("abort", onAbort, once=True)
    try:
        await future
    finally:
        handle.cancel()
        with contextlib.suppress(Exception):
            signal.removeEventListener("abort", onAbort)


def jsonEqual(left: Any, right: Any) -> bool:
    """Structural equality of two JSON values; object key order is ignored."""
    if left is right:
        return True
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    if not isinstance(left, list | dict) or not isinstance(right, list | dict):
        return left == right and not isinstance(left, list | dict) and not isinstance(right, list | dict)
    if isinstance(left, list) or isinstance(right, list):
        if not isinstance(left, list) or not isinstance(right, list) or len(left) != len(right):
            return False
        return all(jsonEqual(value, right[index]) for index, value in enumerate(left))
    if len(left) != len(right):
        return False
    return all(key in right and jsonEqual(value, right[key]) for key, value in left.items())


__all__ = ["TaskRuntimeImpl", "TaskScheduler", "jsonEqual", "withAbortMark", "withState"]

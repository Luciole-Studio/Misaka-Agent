"""The task graph: every live task of the Session as a Chord state.

Translated from pi's ``packages/durable/src/harness/task-graph.ts`` (v1.3.0). A graph is ``{"tasks": {decimal id: node}}``; a node
is ``{"id", "kind", "conversationId", "owner"?, "background", "abortRequested", "state", "conversations"}`` and a node
state is ``{"status": "pending" | "running", "phase"}``, ``{"status": "waiting", "phase", "on", "policy"}`` or
``{"status": "completing", "outcome"}`` (spec section 9.5).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from misaka.chord import replicatedState
from misaka.chord.context import Context, abortError, withoutAbortSignal
from misaka.chord.delta import applyImmutable
from misaka.utils.js import eager, jsonStringify

from ..session.observation import CommittedStateSource, CommittedWatch
from ..session.session import contained
from ..types import CommitPublication, ConversationId, Storage
from .util import closedError, scanAll

LIVE_STATUSES = ("pending", "running", "waiting", "completing")
SCAN_PAGE_SIZE = 256


class _Mount:
    def __init__(self, value: Any) -> None:
        self.value = value
        self.observers: dict[Any, None] = {}  # Insertion-ordered set of observers.


class TaskGraphView:
    """The Harness's task graph mount: built on the Session line by its first observer and dropped with its last. It
    advances from the Session's commit publications, which are durable."""

    def __init__(self, session: Any, storage: Storage) -> None:
        self._session = session
        self._storage = storage
        self._mount: _Mount | None = None
        self._closed = False

        def onCommit(publication: CommitPublication, context: Context) -> None:
            if self._mount is not None:
                advance(self._mount, publication, context, lambda error: session.report(error))

        def onClose() -> None:
            self._closed = True
            for observer in list(self._mount.observers if self._mount is not None else ()):
                contained(
                    lambda observer=observer: observer.closeSession(self._session.failure),
                    lambda error: session.report(error),
                )
            self._mount = None

        session.observeCommits(onCommit)
        session.subscribeClose(onClose)

    @eager
    async def state(self, context: Context) -> Any:
        """A disposable read-only Chord state of the graph."""
        attached = await self._attach(lambda value, release: CommittedStateSource(value, release), context)
        try:
            return replicatedState(attached["observer"], {"onError": lambda error: self._session.report(error)})
        except BaseException:
            attached["detach"]()
            raise

    @eager
    async def watch(self, context: Context) -> Any:
        """A serialized exact-frame watch of the graph; cancelling ``context`` stops it."""
        attached = await self._attach(
            lambda value, release: CommittedWatch(value, release, lambda error: self._session.report(error)), context
        )
        observer = attached["observer"]
        signal = context.abortSignal
        if signal is not None and signal.aborted:
            observer.cancel()
            raise abortError(signal)
        if signal is not None:
            observer.observeCancellation(signal)
        return observer

    def _attach(self, create: Callable[[Any, Callable[[], None]], Any], context: Context) -> Any:
        """Register an observer created from the current revision, atomically on the Session line."""

        async def job() -> dict[str, Any]:
            mount = self._mount if self._mount is not None else _Mount(await self._build(context))

            def detach() -> None:
                mount.observers.pop(observer, None)
                if len(mount.observers) == 0 and self._mount is mount:
                    self._mount = None

            observer = create(mount.value, detach)
            # Close or cancellation may begin while the mount builds; register nothing then.
            if self._closed:
                raise closedError(self._session)
            signal = context.abortSignal
            if signal is not None:
                signal.throwIfAborted()
            self._mount = mount
            mount.observers[observer] = None
            return {"observer": observer, "detach": detach}

        return self._session.readOnLine(job)

    async def _build(self, context: Context) -> dict[str, Any]:
        tasks: dict[str, Any] = {}
        records: list[dict[str, Any]] = []
        for status in LIVE_STATUSES:

            def scanStatus(cursor: Any, status: str = status) -> Any:
                return self._storage.scanTasks({"status": status}, SCAN_PAGE_SIZE, cursor, context)

            records.extend(await scanAll(scanStatus))
        records.sort(key=lambda record: record["id"])
        for record in records:

            def scanOwned(cursor: Any, owner: int = record["id"]) -> Any:
                return self._storage.scanConversations({"ownerTaskId": owner}, SCAN_PAGE_SIZE, cursor, context)

            owned = await scanAll(scanOwned)
            conversations = sorted(conversation["id"] for conversation in owned)
            tasks[str(record["id"])] = nodeOf(record, conversations)
        return {"tasks": tasks}


def advance(mount: _Mount, publication: CommitPublication, context: Context, report: Callable[[Any], None]) -> None:
    """Derive the mount's operations from one publication, apply them, and hand the revision to every observer; one that
    throws is reported through ``report``, and the others still get it."""
    ops: list[Any] = []
    # Nodes this publication set or deleted, over the mount's value.
    changed: dict[str, Any] = {}

    def node(key: str) -> Any:
        return changed[key] if key in changed else mount.value["tasks"].get(key)

    for change in publication["changes"]:
        if change["type"] != "task":
            continue
        record = change["value"]
        key = str(record["id"])
        previous = node(key)
        if record["state"]["status"] == "terminal":
            if previous is None:
                continue
            ops.append(["d", ["tasks", key]])
            changed[key] = None
            continue
        following = nodeOf(record, [] if previous is None else previous["conversations"])
        if previous is not None and jsonStringify(following) == jsonStringify(previous):
            continue
        ops.append(["s", ["tasks", key], following])
        changed[key] = following
    # After the tasks, so a conversation created with its owner task in one commit finds the owner's node. Change order
    # within a publication is unspecified, so each owner's list is sorted again.
    created: dict[str, list[ConversationId]] = {}
    for change in publication["changes"]:
        if change["type"] != "conversation" or change["value"].get("owner") is None:
            continue
        key = str(change["value"]["owner"]["taskId"])
        if node(key) is not None:
            created[key] = [*created.get(key, []), change["value"]["id"]]
    for key, ids in created.items():
        conversations = sorted([*node(key)["conversations"], *ids])
        ops.append(["s", ["tasks", key, "conversations"], conversations])
    if len(ops) == 0:
        return
    mount.value = applyImmutable(mount.value, ops)
    frameContext = withoutAbortSignal(context)
    for observer in list(mount.observers):
        contained(lambda observer=observer: observer.advance(mount.value, ops, frameContext), report)


def nodeOf(record: dict[str, Any], conversations: list[ConversationId]) -> dict[str, Any]:
    return {
        "id": record["id"],
        "kind": record["kind"],
        "conversationId": record["conversationId"],
        **({} if record.get("owner") is None else {"owner": record["owner"]}),
        "background": record["background"],
        "abortRequested": record["abortRequested"],
        "state": stateOf(record),
        "conversations": conversations,
    }


def stateOf(record: dict[str, Any]) -> dict[str, Any]:
    state = record["state"]
    if state["status"] in ("pending", "running"):
        return {"status": state["status"], "phase": phaseOf(state["checkpoint"])}
    if state["status"] == "waiting":
        return {
            "status": "waiting",
            "phase": phaseOf(state["checkpoint"]),
            "on": list(state["on"]),
            "policy": state["policy"],
        }
    # Terminal records never reach here: they leave the graph.
    return {"status": "completing", "outcome": state["outcome"]["status"]}


def phaseOf(checkpoint: Any) -> str:
    return checkpoint["phase"]  # type: ignore[no-any-return]


__all__ = ["TaskGraphView"]

"""The structural view of a conversation: its active transcript and built-in documents as a Chord state.

Translated from pi's ``packages/durable/src/harness/view.ts`` (v1.3.0, spec section 9.3). A view is ``{"conversation":
record, "entries": [...], "docs": {kind: value}}``; an observer is any object with ``closeSession(failure)`` (the
Session's failure, ``None`` after a plain close) and optionally
``advance(value, ops, context)`` and ``publication(before, after, ops, publication, context)``. The views of a Harness
hang off the Session object (upstream keeps them in a ``WeakMap``).
"""

from __future__ import annotations

import weakref
from collections.abc import Callable
from typing import Any

from misaka.chord import replicatedState
from misaka.chord.context import Context, abortError, withoutAbortSignal
from misaka.chord.delta import applyImmutable
from misaka.utils.js import awaitValue, eager

from ..session.observation import CommittedStateSource, CommittedWatch
from ..session.session import contained
from ..types import CommitPublication, ConversationId, Storage
from .agent import AgentDoc
from .context import activeEntries, captureContextBounds
from .inbox import InboxDoc
from .live import LiveDoc
from .provider import ProviderDoc
from .usage import UsageDoc
from .util import closedError

MOUNTED = (
    AgentDoc,
    LiveDoc,
    InboxDoc,
    ProviderDoc,
    UsageDoc,
)
MOUNTED_KINDS = frozenset(token["definition"]["kind"] for token in MOUNTED)


class _Mount:
    """One conversation's mount: its current revision, the document incarnations it shows (the mounted incarnation and
    definition version per kind; another incarnation or version is set whole), and its observers."""

    def __init__(self, value: dict[str, Any], docs: dict[str, dict[str, Any]]) -> None:
        self.value = value
        self.docs = docs
        self.observers: dict[Any, None] = {}  # Insertion-ordered set of observers.


VIEWS: weakref.WeakKeyDictionary[Any, ConversationViews] = weakref.WeakKeyDictionary()


def conversationViews(harness: Any) -> ConversationViews:
    """The view mounts of a Harness, for adapters built on them."""
    views = VIEWS.get(harness)
    if views is None:
        raise RuntimeError("Not a Harness")
    return views


class ConversationViews:
    """The Harness's conversation view mounts: at most one per conversation, built on the Session line by its first
    observer and dropped with its last. Each mount advances from the Session's commit publications, which are
    durable."""

    def __init__(self, session: Any, storage: Storage) -> None:
        self._session = session
        self._storage = storage
        self._mounts: dict[ConversationId, _Mount] = {}
        self._closed = False
        VIEWS[session] = self

        def onCommit(publication: CommitPublication, context: Context) -> None:
            for id, mount in list(self._mounts.items()):
                advance(id, mount, publication, context, lambda error: self.report(error))

        def onClose() -> None:
            self._closed = True
            for mount in list(self._mounts.values()):
                for observer in list(mount.observers):
                    contained(
                        lambda observer=observer: observer.closeSession(self._session.failure),
                        lambda error: self.report(error),
                    )
            self._mounts.clear()

        session.observeCommits(onCommit)
        session.subscribeClose(onClose)

    def report(self, error: Any) -> None:
        """Report an observer's listener error through the Harness."""
        self._session.report(error)

    @eager
    async def state(self, id: ConversationId, context: Context) -> Any:
        """A disposable read-only Chord state of the view."""
        attached = await self.attach(id, lambda value, release, storage: CommittedStateSource(value, release), context)
        try:
            return replicatedState(attached["observer"], {"onError": lambda error: self._session.report(error)})
        except BaseException:
            attached["detach"]()
            raise

    @eager
    async def watch(self, id: ConversationId, context: Context) -> Any:
        """A serialized exact-frame watch of the view; cancelling ``context`` stops it."""
        attached = await self.attach(
            id,
            lambda value, release, storage: CommittedWatch(value, release, lambda error: self._session.report(error)),
            context,
        )
        observer = attached["observer"]
        signal = context.abortSignal
        if signal is not None and signal.aborted:
            observer.cancel()
            raise abortError(signal)
        if signal is not None:
            observer.observeCancellation(signal)
        return observer

    def attach(self, id: ConversationId, create: Callable[..., Any], context: Context) -> Any:
        """Register an observer created from the current revision, atomically on the Session line: it sees every later
        publication and nothing earlier. ``create(value, release, storage)`` may read committed Storage, still on the
        line. ``release`` drops it, and the mount with its last observer."""

        async def job() -> dict[str, Any]:
            mount = self._mounts.get(id)
            if mount is None:
                mount = await self._build(id, context)

            def detach() -> None:
                mount.observers.pop(observer, None)
                if len(mount.observers) == 0 and self._mounts.get(id) is mount:
                    del self._mounts[id]

            observer = await awaitValue(create(mount.value, detach, self._storage))
            # Close or cancellation may begin while the mount hydrates; register nothing then.
            if self._closed:
                raise closedError(self._session)
            signal = context.abortSignal
            if signal is not None:
                signal.throwIfAborted()
            self._mounts[id] = mount
            mount.observers[observer] = None
            return {"observer": observer, "detach": detach}

        return self._session.readOnLine(job)

    async def _build(self, id: ConversationId, context: Context) -> _Mount:
        conversation = await self._storage.conversation(id, context)
        if conversation is None:
            raise RuntimeError(f"Conversation {id} does not exist")
        bounds = await captureContextBounds(self._storage, id, context)
        entries = await activeEntries(self._storage, id, bounds, context)
        docs: dict[str, Any] = {}
        incarnations: dict[str, dict[str, Any]] = {}
        for token in MOUNTED:
            loaded = await self._session.conversationDocumentOnLine(token, id, context)
            if loaded is None:
                continue
            kind = token["definition"]["kind"]
            docs[kind] = loaded["value"]
            incarnations[kind] = {"id": loaded["record"]["id"], "version": loaded["version"]}
        return _Mount({"conversation": conversation, "entries": entries, "docs": docs}, incarnations)


def advance(
    id: ConversationId,
    mount: _Mount,
    publication: CommitPublication,
    context: Context,
    report: Callable[[Any], None],
) -> None:
    """Derive the mount's operations from one publication, apply them, and hand the revision to every observer; one that
    throws is reported through ``report``, and the others still get it."""
    docOps: list[Any] = []
    entryOps: list[Any] = []
    entries = mount.value["entries"]
    # Entry writes are published in ID order.
    for change in publication["changes"]:
        if change["type"] == "entry" and change["value"]["conversationId"] == id:
            entry = change["value"]
            if entry.get("head") is None:
                entryOps.append(["p", ["entries"], len(entries), 0, [entry]])
                entries = [*entries, entry]
                continue
            # A head marker keeps the non-head entries from its head, which are always a suffix, and goes in front.
            target = entry["head"]
            kept = next(
                (index for index, candidate in enumerate(entries) if candidate.get("head") is None and candidate["id"] >= target),
                -1,
            )
            if kept < 0:
                kept = len(entries)
            entryOps.append(["p", ["entries"], 0, kept, [entry]])
            entries = [entry, *entries[kept:]]
            continue
        if change["type"] != "document" or change.get("conversationId") != id:
            continue
        kind = change["record"]["kind"]
        if kind not in MOUNTED_KINDS or change["record"].get("key") is not None:
            continue
        path = ["docs", kind]
        mounted = mount.docs.get(kind)
        if change["value"] is None:
            if mounted is None or mounted["id"] != change["record"]["id"]:
                continue
            del mount.docs[kind]
            docOps.append(["d", path])
        elif mounted is not None and mounted["id"] == change["record"]["id"] and mounted["version"] == change["version"]:
            for op in change["ops"]:
                docOps.append(prefixed(op, path))
        else:
            mount.docs[kind] = {"id": change["record"]["id"], "version": change["version"]}
            docOps.append(["s", path, change["value"]])
    before = mount.value
    ops = [*docOps, *entryOps]
    frameContext = withoutAbortSignal(context)
    if len(ops) > 0:
        value = mount.value if len(docOps) == 0 else applyImmutable(mount.value, docOps)
        mount.value = value if entries is value["entries"] else {**value, "entries": entries}
        for observer in list(mount.observers):
            advanceObserver = getattr(observer, "advance", None)
            if advanceObserver is not None:
                contained(lambda advanceObserver=advanceObserver: advanceObserver(mount.value, ops, frameContext), report)
    for observer in list(mount.observers):
        publicationObserver = getattr(observer, "publication", None)
        if publicationObserver is not None:
            contained(
                lambda publicationObserver=publicationObserver: publicationObserver(
                    before, mount.value, ops, publication, frameContext
                ),
                report,
            )


def prefixed(op: list[Any], prefix: list[Any]) -> list[Any]:
    """``op`` moved under ``prefix``; a root replacement becomes a set of the prefix."""
    verb = op[0]
    if verb == "r":
        return ["s", prefix, op[1]]
    if verb == "p":
        return ["p", [*prefix, *op[1]], op[2], op[3], op[4]]
    if verb == "m":
        return ["m", [*prefix, *op[1]], op[2]]
    if verb == "s":
        return ["s", [*prefix, *op[1]], op[2]]
    if verb == "d":
        return ["d", [*prefix, *op[1]]]
    if verb == "a":
        return ["a", [*prefix, *op[1]], op[2]]
    if verb == "t":
        return ["t", [*prefix, *op[1]], op[2]]
    raise TypeError(f"unknown op verb: {verb}")


__all__ = ["ConversationViews", "conversationViews"]

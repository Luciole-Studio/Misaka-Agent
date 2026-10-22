"""Cross-host reload, coordinated by the server (the host without a parent).

Translated from pi's ``packages/chord/src/cluster.ts``. A promise is an ``asyncio.Future``
(``misaka.chord._promise``).

  server: prepare itself and send `prepare` to every synced child host; collect votes
  all ok: the server installs its new code version, sends `commit` to every child, and only then starts what may make
          new-code-version calls (tasks, listener and registry callbacks). Links are FIFO, so a child commits before
          it can receive any frame of a root started in the new code version.
  any failure or missed vote deadline: `abort` everywhere; each host rolls back to where it was.
Clients (browsers) are not participants: they see shape/reset pushes, or `upgrade_required`.
A child that connects behind the server's code version is fenced and reconciled with the same protocol.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from misaka.chord._promise import Deferred, background, deferred, start
from misaka.chord.connection import Pending
from misaka.chord.errors import ChordError
from misaka.chord.reload import (
    Reload,
    ReloadOptions,
    ReloadReport,
    beginReload,
    endReload,
    reloadError,
)
from misaka.chord.wire import Frame, toWireError
from misaka.utils.js import allSettled, clearTimeout, eager, nullishCoalesce, setTimeout

if TYPE_CHECKING:
    import asyncio

    from misaka.chord.connection import Connection
    from misaka.chord.host import HostImpl, PluginRef

_reloadSeq = 0


def _nextReload() -> int:
    global _reloadSeq
    _reloadSeq += 1
    return _reloadSeq


def modulesFor(remote: Mapping[str, Sequence[str]] | None, hostName: str | None) -> list[str]:
    best: tuple[str, Sequence[str]] | None = None
    for entry in (remote or {}).items():
        if hostName is not None and hostName.startswith(entry[0]) and (best is None or len(entry[0]) > len(best[0])):
            best = entry
    return list(best[1] if best is not None else [])


def awaitVote(connection: Connection, id: int, deadline: float) -> asyncio.Future[Any]:
    """A vote, or its rejection after ``deadline`` milliseconds."""
    vote: Deferred[Any] = deferred()
    connection.votes[id] = Pending(vote.resolve, vote.reject)

    def expire() -> None:
        connection.votes.pop(id, None)
        vote.reject(ChordError("aborted", f"votes missing after {_ms(deadline)} ms", {"reason": "vote_deadline"}))

    timer = setTimeout(expire, deadline)
    vote.promise.add_done_callback(lambda _: clearTimeout(timer))
    return vote.promise


def _ms(ms: float) -> str:
    return str(int(ms)) if isinstance(ms, float) and ms.is_integer() else str(ms)


@eager
async def runClusterReload(host: HostImpl, plugins: Sequence[PluginRef], options: ReloadOptions) -> ReloadReport:
    # Participants are the children in step now. Fenced children (behind, being reconciled) reconcile to the new code
    # version afterwards; a child whose hello arrives while the reload runs is synced or reconciled once it ends
    # (`HostImpl.peerHello`).
    synced = [
        connection
        for connection in list(host.connections)
        if connection.role == "downstream" and connection.peer.kind == "host" and connection.open and connection.synced
    ]
    codeVersion = host.peekNextCodeVersion()
    reload = await beginReload(host, plugins, options, codeVersion)
    try:
        # Retire first: its `retire` frames reach every child before `prepare` (FIFO), so a child never retires a code
        # version this host still runs roots of.
        await reload.waitForRetirement()
        participants = [connection for connection in synced if connection.open]
        id = _nextReload()
        deadline = options.get("voteDeadline")
        if deadline is None:
            deadline = nullishCoalesce(options.get("gateDeadline"), 5000) + nullishCoalesce(options.get("retireDeadline"), 5000) + 5000

        async def voteOf(connection: Connection) -> tuple[str, ReloadReport | None]:
            name = nullishCoalesce(connection.name, "child")
            vote = awaitVote(connection, id, deadline)
            reloadOptions: dict[str, Any] = {"remove": modulesFor(options.get("remoteRemove"), name)}
            if options.get("gateDeadline") is not None:
                reloadOptions["gateDeadline"] = options["gateDeadline"]
            if options.get("retireDeadline") is not None:
                reloadOptions["retireDeadline"] = options["retireDeadline"]
            connection.send(
                {
                    "t": "prepare",
                    "reload": id,
                    "codeVersion": codeVersion,
                    "plugins": modulesFor(options.get("remote"), name),
                    "options": reloadOptions,
                }
            )
            try:
                report = await vote
            except Exception as error:  # noqa: BLE001 - every refusal names its child
                cause = reloadError(error)
                raise ChordError(cause.code, f"{name}: {cause.message}", {"cause": cause, "reason": cause.reason})
            return name, report

        local = [False]

        async def prepareLocal() -> None:
            await reload.prepare()
            local[0] = True

        votes = [start(voteOf(connection)) for connection in participants]
        prepared = start(prepareLocal())
        try:
            results = await allSettled([prepared, *votes])
        except GeneratorExit:
            raise
        except BaseException:
            # MISAKA fork: the reload's own task was cancelled, which happens only when the loop shuts down (a caller
            # that stops waiting cannot, see `HostImpl.reload`): the children abort, and this host's prepare, which runs
            # on in its own task, is rolled back (it ends at its next step, as a coordinator's abort ends a child's).
            for connection in participants:
                connection.votes.pop(id, None)
                connection.send({"t": "abort", "reload": id})
            await reload.rollback()
            await allSettled([prepared])
            raise
        failed = next((result for result in results if result.status == "rejected"), None)
        if failed is not None:
            for connection in participants:
                connection.votes.pop(id, None)
                connection.send({"t": "abort", "reload": id})
            if local[0]:
                await reload.rollback()
            raise failed.reason  # type: ignore[misc]

        def beforeStart() -> None:
            for connection in participants:
                connection.send({"t": "commit", "reload": id})
                connection.peerCodeVersion = codeVersion

        report = reload.commit(beforeStart)
        host.rememberRemoteModules(options.get("remote"), options.get("remoteRemove"))
        children: dict[str, ReloadReport] = {}
        for result in results[1:]:
            if result.status != "fulfilled":
                continue
            name, child = result.value
            if child is not None:
                children[name] = child
        return {**report, "children": children}
    finally:
        endReload(host)


@eager
async def reconcileChild(host: HostImpl, connection: Connection) -> None:
    """Brings a child that connected behind this host's code version up to it: a reload of the child's modules (the
    ones cluster reloads gave its name) to this host's current code version, prepared, voted and committed like a
    cluster reload. Until then the child is fenced: nothing is routed to it. The child's old code is never relabeled as
    a newer code version."""
    while True:
        if not connection.open:
            return
        # Not during a reload of this host: the child is not one of its participants, so whatever it would be synced to
        # now may be superseded by that reload's commit. Recheck once it ended.
        if host.reloading:
            await host.whenReloadEnded()
            continue
        codeVersion = host.current.id
        if connection.peerCodeVersion == codeVersion:
            connection.markSynced(codeVersion)
            host.announceRetired(connection)
            return
        # Before `prepare`: the child's prepare first retires its previous code version, which waits for this
        # permission.
        host.announceRetired(connection)
        id = _nextReload()
        vote = awaitVote(connection, id, 30_000)
        connection.send(
            {
                "t": "prepare",
                "reload": id,
                "codeVersion": codeVersion,
                "plugins": modulesFor(host.remoteModules, connection.name),
                "options": {"remove": modulesFor(host.remoteRemoved, connection.name)},
            }
        )
        await vote
        if host.current.id != codeVersion:
            # This host moved on meanwhile: abort and try again with the newer code version.
            connection.send({"t": "abort", "reload": id})
            continue
        connection.send({"t": "commit", "reload": id})
        connection.peerCodeVersion = codeVersion


@dataclass
class ChildOperation:
    """One prepare the child received, registered before its first await so an abort or disconnect is never lost."""

    id: int
    reload: Reload | None
    state: Literal["loading", "preparing", "prepared"]
    cancelled: bool


class ChildReloads:
    """The child side: prepare and vote, then wait for the server's commit or abort. An abort or a lost parent link can
    arrive at any point: while loading modules, while preparing (pending opens close themselves when they complete),
    or after the vote. The operation is unwound once its pending work settles, and the reload slot is freed."""

    def __init__(self, host: HostImpl) -> None:
        self._host = host
        self._operation: ChildOperation | None = None

    def handle(self, connection: Connection, frame: Frame) -> None:
        if frame["t"] == "prepare":
            if self._operation is not None:
                error = ChordError("unavailable", "a reload is already running", {"reason": "reload_running"})
                connection.send({"t": "vote", "reload": frame["reload"], "error": toWireError(error)})
                return
            operation = ChildOperation(frame["reload"], None, "loading", False)
            self._operation = operation
            background(self._run(operation, connection, frame), self._host.report)
            return
        operation = self._operation
        if operation is None or operation.id != frame["reload"]:
            return
        if frame["t"] == "abort":
            self._cancel(operation)
            return
        if operation.state != "prepared":
            return
        self._operation = None
        try:
            if operation.reload is not None:
                operation.reload.commit()
        finally:
            endReload(self._host)

    def parentLost(self) -> None:
        """The parent link died: nobody will commit, so roll back (now, or once pending work settles)."""
        if self._operation is not None:
            self._cancel(self._operation)

    def _cancel(self, operation: ChildOperation) -> None:
        operation.cancelled = True
        if operation.state != "prepared":
            # Loading or preparing: make prepare fail now; _run unwinds when it settles.
            if operation.reload is not None:
                background(operation.reload.rollback(), self._host.report)
            return
        self._operation = None
        reload = operation.reload

        async def rollback() -> None:
            try:
                if reload is not None:
                    await reload.rollback()
            except Exception as error:  # noqa: BLE001 - reported; the slot is freed anyway
                self._host.report(error)
            finally:
                endReload(self._host)

        background(rollback())

    async def _run(self, operation: ChildOperation, connection: Connection, frame: Frame) -> None:
        def cancelled() -> ChordError:
            return ChordError("aborted", "reload aborted by the server", {"reason": "aborted"})

        try:
            operation.reload = await beginReload(self._host, frame["plugins"], frame["options"], frame["codeVersion"])
            if operation.cancelled:
                raise cancelled()
            operation.state = "preparing"
            await operation.reload.prepare()
            if operation.cancelled:
                raise cancelled()
            operation.state = "prepared"
            connection.send({"t": "vote", "reload": frame["reload"], "report": operation.reload.summary()})
        except Exception as error:  # noqa: BLE001 - every failure is the vote
            if operation.reload is not None:
                try:
                    await operation.reload.rollback()
                except Exception as cleanup:  # noqa: BLE001 - reported; the vote goes out anyway
                    self._host.report(cleanup)
                endReload(self._host)
            if self._operation is operation:
                self._operation = None
            if not operation.cancelled:
                connection.send({"t": "vote", "reload": frame["reload"], "error": toWireError(error)})


__all__ = ["ChildOperation", "ChildReloads", "modulesFor", "reconcileChild", "runClusterReload"]

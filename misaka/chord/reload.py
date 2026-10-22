"""Hot reload on one host: load, prepare, gate, then commit or roll back.

Translated from pi's ``packages/chord/src/reload.ts``. A promise is an ``asyncio.Future``
(``misaka.chord._promise``); ``structuredClone`` is ``copy.deepcopy``; upstream's ``async`` methods are ``eager``
coroutines.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Final, TypedDict

from misaka.chord._promise import (
    Deferred,
    awaited,
    deferred,
    isRejection,
    race,
    resolved,
    start,
)
from misaka.chord.contract import AnyContract, bind, coverage
from misaka.chord.errors import ChordError, asChordError, invalid, isChordError
from misaka.chord.object import (
    Factory,
    Gate,
    Installed,
    ObjectImpl,
    RefImpl,
    ResourceImpl,
    Run,
    ServeOptions,
    Service,
    StagedListeners,
    hostLifetime,
    makeService,
    memberSchemas,
)
from misaka.chord.publish import holdPublications
from misaka.chord.registry import RegistryTable
from misaka.chord.schema import assertValid, deepEqual
from misaka.utils.js import (
    UNDEFINED,
    AbortController,
    allSettled,
    clearTimeout,
    eager,
    nullishCoalesce,
    setTimeout,
)

if TYPE_CHECKING:
    import asyncio

    from misaka.chord.cell import Cell
    from misaka.chord.host import Facet, HostImpl, PluginRef
    from misaka.chord.types import Readable


class ReloadOptions(TypedDict, total=False):
    # Facet names to remove.
    remove: Sequence[str]
    # Milliseconds the gate waits for grandfathered roots (default 5000). Missed: the reload aborts.
    gateDeadline: float
    # Milliseconds to wait for the previous code version to retire first (default 5000). Missed: it is retired anyway.
    retireDeadline: float
    # Server: facet modules for child hosts by host-name prefix (longest wins). Every connected host participates.
    remote: Mapping[str, Sequence[str]]
    # Server: facet names to remove on child hosts, by the same host-name prefix rules.
    remoteRemove: Mapping[str, Sequence[str]]
    # Server: milliseconds to wait for every child's vote (default: gate + retire deadlines + 5000).
    voteDeadline: float


class ReloadReport(TypedDict, total=False):
    codeVersion: int
    previous: int
    changed: Sequence[str]
    removed: Sequence[str]
    # Addresses of objects whose new factory was installed.
    prepared: Sequence[str]
    # Addresses of objects that went through the gate.
    gated: Sequence[str]
    # Cluster reloads: what each participating child host prepared, by host name (its vote; the child commits it after
    # the server). Absent for local reloads.
    children: Mapping[str, ReloadReport]


@dataclass
class Candidate:
    object: ObjectImpl
    service: Service
    run: Run
    # Why the object needs the gate (empty: compatible).
    reasons: list[str] = field(default_factory=list)
    # New exclusive resources and their new dependents, opened at the gate.
    atGate: list[ResourceImpl] = field(default_factory=list)
    # Old exclusive resources closed at the gate.
    replaced: list[ResourceImpl] = field(default_factory=list)
    migrated: dict[Cell[Any], Any] = field(default_factory=dict)
    # Values of the run's derivations over the candidate (migrated) values, validated.
    derived: dict[Any, Any] = field(default_factory=dict)
    # Listener subscriptions made at the end of prepare, activated by the commit.
    staged: StagedListeners | None = None


def dryRun(candidate: Candidate) -> None:
    """Computes the new derivation functions over the candidate values and validates every prospective value (migrated
    cells and derivations) against every schema that will apply after the commit: the cell's new schema and the new
    implementation's remote state-member schemas. A failure fails prepare; the commit then installs without
    re-checking."""
    run, migrated, service, object = candidate.run, candidate.migrated, candidate.service, candidate.object
    exposed = memberSchemas(service, run.implementation)
    for cell, value in migrated.items():
        for schema in exposed.get(cell, []):
            assertValid("state", f"{object.address}: migrated state {cell.key}", schema, value)
    values: dict[Any, Any] = dict(migrated)
    derives = sorted(run.decls.derives.items(), key=lambda entry: entry[1].seq)
    for name, derived in derives:
        value = derived.compute(lambda source: values[source] if source in values else candidateValue(source))
        for schema in exposed.get(derived, []):
            assertValid("state", f"{object.address}: derivation {name}", schema, value)
        values[derived] = value
    candidate.derived = values


def candidateValue(source: Readable[Any]) -> Any:
    """A source's value for a dry run. A reference to a provider this reload upgrades too reads nothing before the
    commit (its new contract does not bind the old code): the dry run computes without it, the commit with the real
    value."""
    try:
        return source.value
    except Exception as error:
        if isChordError(error, "not_ready") or isChordError(error, "not_in_variant"):
            return UNDEFINED
        raise


_ABORTED: Final = re.compile(r"^reload aborted")
_PREFIX: Final = re.compile(r"^[^:]*: ")


def reloadError(error: Any) -> ChordError:
    """A reload error keeps the underlying code (``contract_mismatch``, ``invalid``, ...) and says it aborted the
    reload."""
    cause = asChordError(error, "reload")
    if _ABORTED.search(_PREFIX.sub("", cause.message, count=1)):
        return cause
    return ChordError(
        cause.code,
        f"reload aborted: {cause.message}",
        {"cause": cause, "reason": cause.reason, "details": cause.details},
    )


@eager
async def runReload(host: HostImpl, plugins: Sequence[PluginRef], options: ReloadOptions) -> ReloadReport:
    """Serialized: a second reload while one runs is rejected."""
    reload = await beginReload(host, plugins, options)
    try:
        await reload.prepare()
        return reload.commit()
    finally:
        endReload(host)


@eager
async def beginReload(
    host: HostImpl, plugins: Sequence[PluginRef], options: ReloadOptions, codeVersion: int | None = None
) -> Reload:
    """Takes the host's reload slot and loads the plugin modules. Pair with ``endReload``."""
    if host.reloading:
        raise ChordError("unavailable", "a reload is already running", {"reason": "reload_running"})
    host.reloading = True
    try:
        return Reload(host, await host.resolve(plugins), options, codeVersion)
    except GeneratorExit:
        host.reloadEnded()
        raise
    except BaseException as error:  # every failure frees the reload slot
        host.reloadEnded()
        if not isRejection(error):
            raise
        raise reloadError(error)


def endReload(host: HostImpl) -> None:
    host.reloadEnded()


# Most milliseconds a forced retirement waits for aborted roots to settle before it closes what they used.
ABORTED_ROOTS_GRACE: Final = 1000


def until(host: HostImpl, ready: Callable[[], bool], ms: float) -> asyncio.Future[bool]:
    """Resolves true once ``ready()`` holds (checked whenever retirement-relevant state changes), false after
    ``ms``."""
    if ready():
        return resolved(True)
    result: Deferred[bool] = deferred()
    stop: list[Callable[[], Any]] = []

    def expire() -> None:
        for unwatch in stop:
            unwatch()
        result.resolve(False)

    timer = setTimeout(expire, ms)

    def watcher() -> None:
        if not ready():
            return
        clearTimeout(timer)
        for unwatch in stop:
            unwatch()
        result.resolve(True)

    stop.append(host.watchRetirement(watcher))
    return result.promise


class Reload:
    """One reload on one host: ``prepare`` (load, prepare, gate), then ``commit`` or ``rollback``."""

    def __init__(self, host: HostImpl, facets: Sequence[Facet], options: ReloadOptions, codeVersion: int | None = None) -> None:
        self.host = host
        self.facets = list(facets)
        self.options = options
        # Code version id to commit (issued by the coordinating server); a local reload allocates one.
        self.codeVersion = codeVersion
        self.candidates: dict[ObjectImpl, Candidate] = {}
        self.abort = AbortController()
        self.closed: list[tuple[ObjectImpl, ResourceImpl]] = []
        self.services: dict[str, Service] = {}
        self.changed: dict[str, None] = {}
        self.removed: dict[str, None] = {}
        self.tables: dict[str, RegistryTable[Any]] = {}
        self.gated: list[Candidate] = []
        self.rolledBack = False
        # Objects of types this reload adds, with their staged listeners: their runtime starts at commit.
        self.deferred: dict[ObjectImpl, StagedListeners] = {}
        # This reload's run of each facet it loads (`FacetApi.signal`): installed at commit, aborted at rollback.
        self.facetRuns: dict[str, AbortController] = {}
        # Work that may start new-code-version roots, run after installation (and after the coordinator's commit
        # frames).
        self._starts: list[Callable[[], Any]] = []

    @eager
    async def prepare(self) -> None:
        """Everything up to the commit. On failure the host is rolled back and the underlying error is thrown."""
        try:
            await self.waitForRetirement()
            self.load()
            self.checkProviders()
            await self.prepareAll()
            # From here new objects of changed types are rejected (upgrade_conflict, retryable) until commit or abort,
            # so the candidate set is final once the activations already in flight have been prepared.
            self.host.gating = self.changed
            await self.withDeadline(self.prepareAll(), "activations in flight did not finish")
            self.checkRefs()
            self.tables = self.host.buildTables(self.entries())
            await self.gate()
            self._alive()
            self._stage()
        except GeneratorExit:
            raise
        except BaseException as error:  # every failure rolls back
            await self.rollback()
            # MISAKA fork: JavaScript cannot cancel a reload, and a caller that stops waiting cannot cancel one here
            # either (`HostImpl.reload` hands out a shielded view). Its task is cancelled only when the loop shuts down:
            # the reload then rolls back as a failed one does, and the cancellation goes on.
            if not isRejection(error):
                raise
            raise reloadError(error)

    def summary(self) -> ReloadReport:
        """What a successful prepare will commit (a child sends it with its vote)."""
        live = [candidate for candidate in self.candidates.values() if candidate.object.status == "live"]
        return {
            "codeVersion": self.host.pending.id if self.host.pending is not None else self.host.current.id,
            "previous": self.host.current.id,
            "changed": list(self.changed),
            "removed": list(self.removed),
            "prepared": [candidate.object.address for candidate in live],
            "gated": [candidate.object.address for candidate in self.gated],
        }

    def _alive(self) -> None:
        """After every await of prepare: a rollback meanwhile (abort from the coordinator) ends it."""
        if self.rolledBack:
            raise ChordError("aborted", "reload aborted while preparing", {"reason": "aborted"})

    def _stage(self) -> None:
        """Subscribes every new listener now, buffering its events, so a failing source fails prepare instead of
        leaving a half-installed code version. Objects running only the new code (types this reload adds) are staged
        too."""
        for candidate in self.candidates.values():
            if candidate.object.status == "live":
                candidate.staged = candidate.object.stageListeners(candidate.run.decls)
        for object in list(self.host.objects.values()):
            if object.status == "live" and object.deferredRuntime:
                self.deferred[object] = object.stageListeners(object.decls)

    @eager
    async def waitForRetirement(self) -> None:
        """Retires the previous code version before loading the next one (a host keeps two). Waits until its roots
        ended and, on a child host, its parent retired it too (``retire`` frame), at most ``retireDeadline``. Past the
        deadline it is retired anyway: its remaining roots are aborted first and get a short grace to stop, so they
        see cancellation before the resources only that code version used are closed; their next call is
        ``superseded``."""
        host = self.host
        previous = host.previous
        if previous is None:
            return
        deadline = nullishCoalesce(self.options.get("retireDeadline"), 5000)

        def gone() -> bool:
            return host.previous is not previous

        if not (await until(host, lambda: gone() or host.retirable(), deadline)) and not gone():
            previous.abort.abort(ChordError("superseded", f"code version {previous.id} was retired"))
            await until(host, lambda: gone() or len(previous.roots) == 0, min(deadline, ABORTED_ROOTS_GRACE))
        if not gone():
            host.retire()

    def load(self) -> None:
        """Builds the candidate service table: unchanged services plus everything the given facets serve."""
        replaced = {facet.name for facet in self.facets} | set(self.options.get("remove") or [])
        current = self.host.current.services
        self.services = {name: service for name, service in current.items() if service.facet not in replaced}
        for facet in self.facets:
            run = AbortController()
            self.facetRuns[facet.name] = run

            def serve(type: AnyContract, factory: Factory, options: ServeOptions | None = None, facet: Facet = facet) -> None:
                self.host.serveInto(self.services, makeService(type, factory, options or {}, facet.name))

            facet.setup(self.host.facetApi(serve, run.signal))
        for name, service in self.services.items():
            if current.get(name) is not service:
                self.changed[name] = None
        for name in current:
            if name not in self.services:
                self.removed[name] = None
        # Imported here: host.py imports this module.
        from misaka.chord.host import CodeVersionState

        self.host.pending = CodeVersionState(
            id=self.host.nextCodeVersionId(self.codeVersion),
            services=self.services,
            tables={},
            roots={},
            abort=AbortController(),
        )

    @eager
    async def withDeadline[T](self, promise: asyncio.Future[T], what: str) -> T:
        deadline = nullishCoalesce(self.options.get("gateDeadline"), 5000)
        timeout: Deferred[Any] = deferred()
        timer = setTimeout(
            lambda: timeout.reject(
                ChordError("aborted", f"{what} within {_ms(deadline)} ms", {"reason": "deadline"})
            ),
            deadline,
        )
        try:
            return await race([promise, timeout.promise])
        finally:
            clearTimeout(timer)

    def checkProviders(self) -> None:
        """Same contract version must cover the old description (forgotten bump); facets' ``uses`` must bind."""
        for name in self.changed:
            next = self.services[name].contract
            oldService = self.host.current.services.get(name)
            old = None if oldService is None else oldService.contract
            if old is None or old.version != next.version:
                continue
            reason = coverage(next.description, old.description)
            if reason is not None:
                raise ChordError(
                    "contract_mismatch",
                    f"{name}@{next.version} changed incompatibly without a version bump ({reason})",
                )
        facets = dict(self.host.facets)
        for facet in self.facets:
            facets[facet.name] = facet
        for name in self.options.get("remove") or []:
            facets.pop(name, None)
        for facet in facets.values():
            for token in facet.uses or []:
                service = self.services.get(token.name)
                if service is not None and token.name in self.changed:
                    bind(service.contract, service.options.get("accepts"), token)

    @eager
    async def prepareAll(self) -> None:
        """Prepares every live object of a changed type, including objects that activate meanwhile, to a fixpoint.
        Objects already running the new code (types this reload adds) are skipped."""
        while True:
            todo = [
                object
                for object in list(self.host.objects.values())
                if object.typeName in self.changed
                and object not in self.candidates
                and object.service is not self.services.get(object.typeName)
                and (object.status == "live" or object.status == "activating")
            ]
            if len(todo) == 0:
                return
            await allSettled([object.ready for object in todo])
            self._alive()
            for object in todo:
                if object.status == "live" and object.service is not self.services.get(object.typeName):
                    await self.prepareObject(object)
                    self._alive()

    @eager
    async def prepareObject(self, object: ObjectImpl) -> None:
        """Runs the new factory in recording mode and opens what can be opened without the gate."""
        service = self.services[object.typeName]
        run = object.run(service, object.decls)
        candidate = Candidate(object, service, run)
        self.candidates[object] = candidate
        # Each cell carries its own version: a raised one gates and migrates, a schema change without one is refused.
        for name, cell in run.decls.cells.items():
            if object.decls.cells.get(name) is not cell:
                continue
            fromVersion = nullishCoalesce(object.decls.cellVersions.get(name), 1)
            to = nullishCoalesce(run.decls.cellVersions.get(name), 1)
            if to < fromVersion:
                raise invalid("state", f"{object.address}: version of state {name} lowered from {fromVersion} to {to}")
            if to > fromVersion:
                candidate.reasons.append(f"state {name} version {fromVersion} -> {to}")
            elif not deepEqual(cell.schema, run.decls.cellSchemas.get(name)):
                raise invalid("state", f"{object.address}: schema of state {name} changed without a version bump")
        deferred_: dict[ResourceImpl, None] = {}
        for resource in run.created.resources:
            if resource.exclusive or any(dep in deferred_ for dep in resource.deps if isinstance(dep, ResourceImpl)):
                deferred_[resource] = None
        for resource in deferred_:
            candidate.atGate.append(resource)
            old = object.decls.resources.get(resource.key)
            if resource.exclusive and old is not None and old.exclusive and old.state == "open":
                candidate.replaced.append(old)
        if len(deferred_) > 0:
            candidate.reasons.append("exclusive resource replaced")
        await object.open(
            [resource for resource in run.created.resources if resource not in deferred_],
            run.created.refs,
            self.abort.signal,
        )
        await object.waitHydrated(run)
        # Cells this reload adds read their first value once the new resources are open (gated objects: at the gate);
        # the derivations over them are checked after.
        if len(candidate.reasons) == 0:
            await object.initCells(run, self.abort.signal)
            dryRun(candidate)

    def checkRefs(self) -> None:
        """Every reference that survives into the new code version must bind to the new provider. Browser references
        don't block a reload: after the commit they get ``upgrade_required`` instead."""
        dropped: set[Any] = set()
        for candidate in self.candidates.values():
            kept = list(candidate.run.decls.refs.values())
            for ref in candidate.object.decls.refs.values():
                if all(ref is not other for other in kept):
                    dropped.add(ref)
        for object in list(self.host.objects.values()):
            service = self.services.get(object.typeName)
            if service is None or object.typeName not in self.changed:
                continue
            for ref in list(object.refs):
                # The host's bootstrap reference follows this provider at commit; it is not an old-code consumer.
                if ref is self.host.hostObjects.get(object.typeName):
                    continue
                if ref not in dropped and ref.owner != "client":
                    bind(service.contract, service.options.get("accepts"), ref.contract)

    def entries(self) -> list[tuple[ObjectImpl, Any]]:
        result = []
        for object, decls in self.host.liveDecls(True):
            if object.typeName in self.removed:
                continue
            candidate = self.candidates.get(object)
            result.append((object, decls if candidate is None else candidate.run.decls))
        return result

    @eager
    async def gate(self) -> None:
        """Admission cut, wait for grandfathered roots (deadline), replace exclusive resources, migrate into private
        copies. Old tasks of gated objects are stopped at the cut; listener events are queued until commit or
        abort."""
        self.gated = [
            candidate
            for candidate in self.candidates.values()
            if len(candidate.reasons) > 0 and candidate.object.status == "live"
        ]
        drained = []
        for candidate in self.gated:
            object = candidate.object
            grandfathered = dict.fromkeys(object.leases)
            done: Deferred[None] = deferred()
            object.gate = Gate(grandfathered, [], done.resolve)
            object.stopTasks()
            if len(grandfathered) == 0:
                done.resolve()
            drained.append(done.promise)
        deadline = nullishCoalesce(self.options.get("gateDeadline"), 5000)
        late: Deferred[bool] = deferred()
        timer = setTimeout(lambda: late.resolve(True), deadline)

        async def allDrained() -> bool:
            for promise in drained:
                await promise
            return False

        waiting = start(allDrained())
        try:
            timedOut = await race([waiting, late.promise])
        finally:
            # A missed deadline leaves gates that never drain (the rollback opens them without draining): the private
            # wait would stay pending until it is collected, which asyncio reports ("Task was destroyed but it is
            # pending"); upstream's unreachable promise is collected silently. Nothing else awaits it: it is cancelled.
            if not waiting.done():
                waiting.cancel()
        clearTimeout(timer)
        if timedOut:
            blocking = [
                rootId for candidate in self.gated for rootId in (candidate.object.gate.grandfathered if candidate.object.gate else [])
            ]
            raise ChordError(
                "aborted",
                f"gate deadline ({_ms(deadline)} ms) missed; blocking roots: {', '.join(blocking)}",
                {"reason": "gate_deadline", "details": {"blocking": blocking}},
            )
        self._alive()
        for candidate in self.gated:
            object = candidate.object
            for old in candidate.replaced:
                # Owned by the rollback before closing: a failing close must still be restored.
                self.closed.append((object, old))
                await old.close(object.openOrder)
            await object.open(candidate.atGate, [], self.abort.signal)
            self._alive()
            await object.initCells(candidate.run, self.abort.signal)
            self._alive()
        for candidate in self.gated:
            self.migrate(candidate)

    def migrate(self, candidate: Candidate) -> None:
        object, run = candidate.object, candidate.run
        for name, cell in run.decls.cells.items():
            if object.decls.cells.get(name) is not cell:
                continue
            schema = run.decls.cellSchemas[name]
            fromVersion = nullishCoalesce(object.decls.cellVersions.get(name), 1)
            fn = run.decls.cellMigrations.get(name)
            if nullishCoalesce(run.decls.cellVersions.get(name), 1) > fromVersion and fn is not None:
                value = fn(copy.deepcopy(cell.value), fromVersion)
            elif not deepEqual(cell.schema, schema):
                value = cell.value
            else:
                continue
            assertValid("state", f"{object.address}: migrated state {name}", schema, value)
            candidate.migrated[cell] = value
        dryRun(candidate)

    @eager
    async def rollback(self) -> None:
        if self.rolledBack:
            return
        self.rolledBack = True
        self.abort.abort(ChordError("aborted", "reload aborted"))
        for run in list(self.facetRuns.values()):
            run.abort(ChordError("aborted", "reload aborted"))
        report = self.host.report
        for candidate in self.candidates.values():
            if candidate.staged is not None:
                candidate.staged.discard()
        for staged in self.deferred.values():
            staged.discard()
        for candidate in list(self.candidates.values()):
            candidate.object.pending.pop(candidate.run.decls, None)
            try:
                await candidate.object.unwind(candidate.run)
            except Exception as error:  # noqa: BLE001 - reported; the rollback goes on
                report(error)
        # Replaced exclusive resources are restored; one that cannot be is marked unavailable, never silently closed.
        for object, resource in self.closed:
            if resource.state != "closed":
                continue
            try:
                await resource.open(AbortController().signal, object.openOrder)
            except Exception as error:  # noqa: BLE001 - reported as the resource's unavailability
                resource.markUnavailable()
                report(
                    ChordError(
                        "unavailable",
                        f"{object.address}: resource {resource.key} could not be restored",
                        {"cause": error},
                    )
                )
        for candidate in self.gated:
            object = candidate.object
            if object.gate is None:
                continue
            object.openGate()
            object.startTasks()
        # Objects of types this reload would have added lose their code.
        reason = ChordError("gone", "reload aborted; type not served")
        for object in list(self.host.objects.values()):
            if object.typeName not in self.host.current.services:
                try:
                    await awaited(object.dispose(True, reason))
                except Exception as error:  # noqa: BLE001 - reported; the rollback goes on
                    report(error)
        self.host.pending = None
        self.host.gating = None

    def commit(self, beforeStart: Callable[[], None] | None = None) -> ReloadReport:
        """Publishes the new code version in one synchronous step. Everything fallible ran in prepare (factories,
        opens, migration and derivation checks against the prospective schemas, listener subscriptions), so this only
        installs. Observers (state, shapes, registries, listener roots) are notified after the whole step. If an install
        still throws, the code version is half-published: the host fails terminally and admits no more work."""
        try:

            def step() -> ReloadReport:
                report = self._publish()
                # Installed, not yet started: the coordinator queues its commit frames here, so nothing this code
                # version starts (tasks, host object activations, then the held listener/registry/state notifications)
                # can reach a child first.
                if beforeStart is not None:
                    beforeStart()
                starts, self._starts = self._starts, []
                for begin in starts:
                    begin()
                return report

            return holdPublications(step)
        except Exception as error:  # noqa: BLE001 - a failed install fails the host
            self.host.fail(error)
            raise reloadError(error)

    def _publish(self) -> ReloadReport:
        host = self.host
        previous = host.current
        next = host.pending
        assert next is not None
        host.pending = None
        host.codeVersions[next.id] = next
        host.previous = previous
        host.current = next
        prepared: list[str] = []
        for object in list(host.objects.values()):
            if object.typeName in self.removed:
                continue
            candidate = self.candidates.get(object)
            if candidate is None:
                installed = object.implementations.get(previous.id)
                if installed is not None:
                    object.implementations[next.id] = replace(installed, codeVersion=next.id)
                continue
            if object.status != "live":
                continue
            self.install(candidate, next.id)
            prepared.append(object.address)
        for candidate in self.candidates.values():
            if candidate.object.status == "live":
                continue
            if candidate.staged is not None:
                candidate.staged.discard()
            candidate.object.pending.pop(candidate.run.decls, None)
            candidate.object.unwind(candidate.run).add_done_callback(lambda done: _report(done, host.report))
        # Objects of types this reload adds start their runtime now.
        for object, staged in self.deferred.items():
            if object.status != "live":
                staged.discard()
                continue
            object.deferredRuntime = False
            staged.activate()
            self._starts.append(object.startTasks)
        host.installTables(next, self.tables, True)
        reason = ChordError("gone", "type no longer served")
        for object in list(host.objects.values()):
            if object.typeName in self.removed:
                object.dispose(True, reason).add_done_callback(lambda done: _report(done, host.report))
        for name in self.removed:
            host.hostObjects.pop(name, None)
        for facet in self.facets:
            host.facets[facet.name] = facet
        for name in self.options.get("remove") or []:
            host.facets.pop(name, None)
        # The replaced and removed runs' signals abort after the install, like other observers.
        for name, run in self.facetRuns.items():
            self._starts.append(lambda name=name, run=run: host.facetRunChanged(name, run))
        for name in self.options.get("remove") or []:
            self._starts.append(lambda name=name: host.facetRunChanged(name, None))
        host.gating = None
        for name, service in self.services.items():
            pinnedObject = host.hostObjects.get(name)
            if (
                hostLifetime(service)
                and name in self.changed
                and (pinnedObject is None or pinnedObject.object is None or pinnedObject.object.status != "live")
            ):
                self._starts.append(lambda service=service: host.startHostObject(service))
            # A type whose lifetime became "referenced" lives on only while someone else references its object.
            pinned = host.hostObjects.get(name)
            if not hostLifetime(service) and pinned is not None:
                del host.hostObjects[name]
                self._starts.append(pinned.release)
        return {
            "codeVersion": next.id,
            "previous": previous.id,
            "changed": list(self.changed),
            "removed": list(self.removed),
            "prepared": prepared,
            "gated": [candidate.object.address for candidate in self.gated],
        }

    def install(self, candidate: Candidate, codeVersion: int) -> None:
        object, run, service = candidate.object, candidate.run, candidate.service
        gated = len(candidate.reasons) > 0
        object.pending.pop(run.decls, None)
        object.stopTasks()
        if gated:
            # Old derivations would compute over migrated values; the old implementation can no longer run anyway.
            kept = list(run.decls.derives.values())
            for derived in list(object.decls.derives.values()):
                if all(derived is not other for other in kept):
                    derived.detach()
        object.decls = run.decls
        object.service = service
        # Kept resources close with the new code's `close`; replaced ones keep their own (closed at retirement).
        for resource, declaration in run.decls.adopted.items():
            resource.adopt(declaration)
        object.implementations[codeVersion] = Installed(codeVersion, service, run.implementation, run.decls)
        if gated:
            # Old code can never run over migrated state: drop the previous implementation now, not at retirement.
            object.minCodeVersion = codeVersion
            for old in list(object.implementations):
                if old != codeVersion:
                    self.host.retireImplementation(object, old)
        # Schemas first, then the values validated against them in prepare.
        object.refreshSchemas()
        for name, cell in run.decls.cells.items():
            schema = run.decls.cellSchemas[name]
            if cell in candidate.migrated:
                cell.reset(candidate.migrated[cell], schema)
            else:
                cell.schema = schema
        created = list(run.created.derives)
        for derived in created:
            derived.attach(candidate.derived)
        for derived in list(run.decls.derives.values()):
            if all(derived is not other for other in created):
                derived.refresh()
        if candidate.staged is not None:
            candidate.staged.activate()
        self._starts.append(object.startTasks)
        object.openGate()
        pinned = self.host.hostObjects.get(object.typeName)
        if pinned is not None and pinned.object is object and pinned.contract.fingerprint != service.contract.fingerprint:
            replacement = RefImpl(object, service.contract, "host")
            # Acquire first: releasing the only reference must not dispose/reopen the surviving host object.
            object.addRef(replacement)
            self.host.hostObjects[object.typeName] = replacement
            pinned.release()
        for ref in list(object.refs):
            # Browser references whose contract no longer binds are told to reload their code (they never block a
            # reload).
            if ref.owner == "client":
                try:
                    bind(service.contract, service.options.get("accepts"), ref.contract)
                except Exception as error:  # noqa: BLE001 - the browser is told to reload its code
                    ref.gone(
                        ChordError(
                            "upgrade_required",
                            f"{object.typeName} changed; reload the web facets",
                            {"cause": error},
                        )
                    )
                    object.release(ref)
                    continue
            ref.shapeChanged()


def _ms(ms: float) -> str:
    return str(int(ms)) if isinstance(ms, float) and ms.is_integer() else str(ms)


def _report(done: Any, report: Callable[[Any], None]) -> None:
    if done.cancelled():
        return
    error = done.exception()
    if error is not None:
        report(error)


__all__ = [
    "Candidate",
    "Reload",
    "ReloadOptions",
    "ReloadReport",
    "beginReload",
    "dryRun",
    "endReload",
    "reloadError",
    "runReload",
]


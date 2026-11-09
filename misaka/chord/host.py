"""Hosts: they serve types, hold references, run roots, accept and connect links, route, and reload facets.

Translated from pi's ``packages/chord/src/host.ts``. Options are mappings with upstream's keys
(``createHost({"self": ..., "name": ...})``); a facet is an object with ``name``, ``setup`` and ``uses``; a promise is
an ``asyncio.Future`` (``misaka.chord._promise``) and upstream's ``async`` methods are ``eager`` coroutines.
``console.error`` (the default ``onError``) writes to ``sys.stderr``; ``Math.random().toString(36)`` is
``secrets``-chosen base-36 text.
"""

from __future__ import annotations

import asyncio
import inspect
import secrets
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypedDict

from misaka.chord._promise import (
    asException,
    background,
    deferred,
    hold,
    promiseAll,
    quiet,
    rejected,
    resolved,
    start,
)
from misaka.chord.cluster import ChildReloads, reconcileChild, runClusterReload
from misaka.chord.connection import (
    AcceptOptions,
    Connection,
    LinkOptions,
    Peer,
    RelayRef,
)
from misaka.chord.contract import (
    AnyContract,
    bind,
    contractFromDescription,
    placedOn,
    placementText,
    resolveHost,
)
from misaka.chord.errors import ChordError, invalid, runAll
from misaka.chord.link import Link
from misaka.chord.object import (
    CtxImpl,
    Decls,
    Factory,
    ObjectIdentity,
    ObjectImpl,
    RefCore,
    RefImpl,
    RefOwner,
    ServeOptions,
    Service,
    awaitReady,
    hostLifetime,
    makeService,
    strictEqual,
)
from misaka.chord.publish import enqueue
from misaka.chord.registry import Registry, RegistryTable, RegistryView
from misaka.chord.reload import ReloadOptions, ReloadReport, runReload
from misaka.chord.remote_ref import RemoteBinding, RemoteRef
from misaka.chord.schema import canonicalIdentity, canonicalJson, deepEqual, normalize
from misaka.utils.js import (
    UNDEFINED,
    AbortController,
    AbortSignal,
    allSettled,
    eager,
    nullishCoalesce,
    setTimeout,
)

if TYPE_CHECKING:
    from misaka.chord.types import Ctx, GetOptions
    from misaka.chord.wire import Frame


class Serve(Protocol):
    """Registers a factory for a type: ``serve(type, factory, options?)``."""

    def __call__(self, type: AnyContract, factory: Factory, options: ServeOptions | None = None) -> None: ...


class RootOptions(TypedDict, total=False):
    principal: Any
    signal: AbortSignal | None
    # Start the root in a retained code version (links carry code versions). Defaults to the current code version.
    codeVersion: int


@dataclass(frozen=True)
class FacetApi:
    """What a facet's ``setup`` gets. ``serve`` registers types; ``root`` and ``registry`` give the facet's code that is
    not a Chord method, listener or task (Durable tools and hooks, callbacks of native libraries) access to its own
    host, so plugins never need a process-global host. Both may be kept and used after ``setup`` returns."""

    serve: Serve
    # The host's name (`server`, `session:S1`, ...).
    name: str
    # Starts a root on the facet's host (in its current code version at call time).
    root: Callable[..., asyncio.Future[Any]]
    # The facet's host's registry view (`get(key, ctx)` reads the table of `ctx.codeVersion`).
    registry: Callable[[Registry[Any]], RegistryView[Any]]
    # Aborted when this run of the facet is no longer installed: a reload that replaced or removed it committed, the
    # reload that loaded it aborted, or the host closed. While a reload prepares, the installed and the candidate run
    # are both live.
    signal: AbortSignal


@dataclass(frozen=True)
class FacetDefinition:
    """What a facet module exports (its ``default``): the code that serves its types on one host kind."""

    setup: Callable[[FacetApi], None]
    # Contract tokens this facet consumes; checked against providers at reload.
    uses: Sequence[AnyContract] | None = None


@dataclass(frozen=True)
class Facet:
    """A named facet. Modules are named by their URL; reloading a facet replaces everything it served."""

    name: str
    setup: Callable[[FacetApi], None]
    uses: Sequence[AnyContract] | None = None


def defineFacet(setup: Callable[[FacetApi], None], options: Mapping[str, Any] | None = None) -> FacetDefinition:
    """``defineFacet(setup, {"uses": [...]})``."""
    return FacetDefinition(setup, (options or {}).get("uses"))


# Imports facet modules (fresh copies on every call) and names them by module URL.
type FacetLoader = Callable[[Sequence[str | PurePath]], Any]
# A facet object, or a module (a path) whose `default` is a FacetDefinition.
type PluginRef = Facet | str | PurePath


def isFacet(ref: Any) -> bool:
    return not isinstance(ref, str | PurePath)


def facetOf(ref: Any) -> Facet:
    """A facet given as an object, or as a mapping with its keys (a JavaScript object literal, ``{name, setup}``)."""
    if isinstance(ref, Mapping):
        return Facet(ref.get("name"), ref.get("setup"), ref.get("uses"))  # type: ignore[arg-type]
    return ref


class ConnectOptions(LinkOptions, total=False):
    """``connect`` options: the link's, and the principal its parent's own requests act as."""

    # What a request from the parent acts as when the parent does not forward its caller's principal (its own code).
    principal: Any


class UpstreamOptions(ConnectOptions, total=False):
    link: Link


class HostOptions(TypedDict, total=False):
    name: str
    # The principal of the host's own code: roots it starts, code inside its objects, and its `lifetime: "host"`
    # objects. Compared by `principalKey`, so an equal principal from elsewhere reaches the same objects.
    self: Any
    # Maps a principal to its object key (should include connection provenance and grant revision).
    principalKey: Callable[[Any], str]
    # Validates every cell write and exposed state value against its schemas.
    dev: bool
    # Errors from background work (disposal, listeners, tasks). Defaults to printing to stderr.
    onError: Callable[[Any], None]
    # Milliseconds a reload waits for an object's aborted tasks to finish before it starts their replacements anyway
    # (reported). Default 5000.
    taskStopDeadline: float
    # Milliseconds an activation (or a reload's prepare) waits for its references to be ready and hydrated, and an
    # awaited `get` for its reference, before failing with `unavailable/ready_timeout`. Default 60000.
    readyTimeout: float
    # Consumer-side linger of remote references (`{ types: { typeName: ms }, cap }`, cap default 4): when the last
    # reference to a remote object is released, its binding stays held, and hydrated, for `types[typeName]`
    # milliseconds, so a reference taken again meanwhile costs nothing on the link. At most `cap` bindings linger;
    # beyond, the least recently released go first. Types not listed do not linger.
    linger: Mapping[str, Any]
    # Needed for `load`/`reload` with modules; `nodeLoader` from loader.py.
    loader: FacetLoader
    # `client` (browser): never a reload participant, never carries roots or code versions. Default `host`.
    kind: Literal["host", "client"]
    # The link to this host's parent (server). Can also be set later with `connect`.
    upstream: UpstreamOptions


class Route(Protocol):
    """Where requests for hosts named ``prefix...`` go (on the server)."""

    def authorize(self, principal: Any, hostName: str) -> Any:
        """Checked before resolving (or spawning) and again after: grants may change meanwhile."""
        ...

    def resolve(self, hostName: str) -> Any:
        """A link to the host, spawned if needed."""
        ...

    def principal(self, hostName: str) -> Any:
        """The principal the resolved host acts as when it calls back."""
        ...


# Fields a principal must have, equal (`deepEqual`), for an `invalidatePrincipal` pattern.
type PrincipalPattern = Mapping[str, Any]
# `invalidatePrincipal` matches: a predicate (this host only), or a pattern (fields that must be equal; propagates).
type PrincipalMatch = Callable[[Any], bool] | PrincipalPattern


class LoadOptions(TypedDict, total=False):
    # Await the activation of the `lifetime: "host"` objects the facets serve (default true). False starts them but
    # leaves readiness to `host.ready()` (a child host whose objects need its parent, which admits it only later).
    awaitHostObjects: bool


# What `createHost` returns (`HostImpl`'s public surface).
type Host = HostImpl


@dataclass
class CodeVersionState:
    id: int
    services: dict[str, Service]
    tables: Mapping[str, RegistryTable[Any]]
    # Running roots with the number of their calls running here.
    roots: dict[str, int] = field(default_factory=dict)
    # Aborts every root of this code version (forced retirement).
    abort: AbortController = field(default_factory=AbortController)


EMPTY_TABLE: Mapping[str, Any] = {}


def createHost(options: HostOptions) -> HostImpl:
    """A host whose principals are the app's (``self`` is the host's own)."""
    return HostImpl(options)


def _route(route: Any, name: str) -> Callable[..., Any]:
    """A member of a route given as a mapping or an object (None when it has none, as `route.principal` is
    undefined upstream)."""
    return route.get(name) if isinstance(route, Mapping) else getattr(route, name, None)


def _consoleError(error: Any) -> None:
    print("chord:", error, file=sys.stderr)


_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"


class HostImpl:
    def __init__(self, options: HostOptions) -> None:
        self.name: str = nullishCoalesce(options.get("name"), "host")
        self.dev: bool = nullishCoalesce(options.get("dev"), False)
        if "self" not in options:
            raise TypeError("createHost needs `self`: the principal of the host's own code")
        self.principal = options["self"]
        self._principalKey: Callable[[Any], str] = nullishCoalesce(options.get("principalKey"), canonicalJson)
        self._selfKey = self._principalKey(options["self"])
        self._onError: Callable[[Any], None] = nullishCoalesce(options.get("onError"), _consoleError)
        self._loader = options.get("loader")
        self.taskStopDeadline: float = nullishCoalesce(options.get("taskStopDeadline"), 5000)
        self.readyTimeout: float = nullishCoalesce(options.get("readyTimeout"), 60_000)
        linger = nullishCoalesce(options.get("linger"), {})
        self._lingerTypes: Mapping[str, float] = nullishCoalesce(linger.get("types"), {})
        self._lingerCap: int = nullishCoalesce(linger.get("cap"), 4)
        self.inFactory = 0
        self.failed: Any = None
        self.objects: dict[str, ObjectImpl] = {}
        self.codeVersions: dict[int, CodeVersionState] = {}
        self.facets: dict[str, Facet] = {}
        # The host's own references to its `lifetime: "host"` objects, by type name.
        self.hostObjects: dict[str, RefCore] = {}
        self.connections: dict[Connection, None] = {}
        self.upstream: Connection | None = None
        # Shared bindings of this host's remote references, by key (re-held after an upstream reconnect).
        self.bindings: dict[str, RemoteBinding] = {}
        # Bindings without references that linger, least recently released first.
        self._lingering: dict[str, RemoteBinding] = {}
        self._originSeq = 0
        self._routes: dict[str, Any] = {}
        self._childReloads = ChildReloads(self)
        # Child facet modules by host-name prefix, from every committed cluster reload (for reconciling children).
        self.remoteModules: dict[str, list[str]] = {}
        # Removal tombstones also reconcile children that connected during a transaction.
        self.remoteRemoved: dict[str, list[str]] = {}
        # Highest code version the parent has retired.
        self._retireAllowed = 0
        # Foreign roots (started on another host), by the links they came over.
        self._foreign: dict[str, dict[Connection, None]] = {}
        # Roots started on this host that have not ended.
        self._local: set[str] = set()
        self._incarnation = "".join(secrets.choice(_BASE36) for _ in range(6))
        # Links each root's calls went out on.
        self._rootLinks: dict[str, dict[Connection, None]] = {}
        self._codeVersionSeq = 1
        self._rootSeq = 0
        self.current = CodeVersionState(self._codeVersionSeq, {}, {})
        self.previous: CodeVersionState | None = None
        # The code version a running reload will commit; new types activated during prepare install their
        # implementation for it.
        self.pending: CodeVersionState | None = None
        # Types behind a reload gate: new objects of these types are rejected `upgrade_conflict` until commit/abort.
        self.gating: Mapping[str, Any] | None = None
        self.reloading = False
        # Called when a root ends, the parent allows a retirement, or the previous code version retires.
        self._retirementWatchers: dict[Callable[[], None], None] = {}
        # Child hosts whose hello arrived while a reload ran: synced or reconciled once it ends.
        self._deferredHellos: dict[Connection, int] = {}
        # Resolved when the running reload ends.
        self._reloadWaiters: list[Callable[[], None]] = []
        # Aborted when the installed run of a facet (by name) is replaced, removed, or the host closes.
        self._facetRuns: dict[str, AbortController] = {}
        self._dying: dict[str, ObjectImpl] = {}
        self._touches: dict[str, dict[ObjectImpl, None]] = {}
        self._order: dict[str, dict[str, int]] = {}
        self._subscribers: dict[str, dict[Callable[[RegistryTable[Any], int], None], None]] = {}
        self.codeVersions[self.current.id] = self.current
        self.kind: Literal["host", "client"] = nullishCoalesce(options.get("kind"), "host")
        upstream = options.get("upstream")
        if upstream is not None:
            self.connect(upstream["link"], upstream)

    def isSelf(self, principal: Any) -> bool:
        """Whether ``principal`` is the host's own: the same key as ``self``."""
        return principal is self.principal or self._principalKey(principal) == self._selfKey

    @property
    def codeVersion(self) -> int:
        return self.current.id

    @property
    def previousCodeVersion(self) -> int | None:
        return None if self.previous is None else self.previous.id

    def nextCodeVersionId(self, given: int | None = None) -> int:
        """Allocates the next code version id, or adopts one issued by the coordinating server."""
        if given is not None and given <= self.current.id:
            raise RuntimeError(f"code version {given} is not newer than {self.current.id}")
        self._codeVersionSeq = self._codeVersionSeq + 1 if given is None else given
        return self._codeVersionSeq

    def report(self, error: Any) -> None:
        self._onError(error)

    # ─── serving ───

    def serve(self, type: AnyContract, factory: Factory, options: ServeOptions | None = None) -> None:
        options = options or {}
        if self.reloading:
            raise RuntimeError("host.serve during a reload; serve from a facet passed to reload()")
        self.serveInto(self.current.services, makeService(type, factory, options, ""))
        if options.get("lifetime") == "host":
            self.startHostObject(self.current.services[type.name])

    def install(self, facet: Facet, options: LoadOptions | None = None) -> asyncio.Future[None]:
        """Installs synchronously, then awaits this facet's host objects unless ``awaitHostObjects`` is false."""
        options = options or {}
        facet = facetOf(facet)
        if self.reloading:
            raise RuntimeError("host.install during a reload")
        if facet.name in self.facets:
            raise RuntimeError(f"facet {facet.name} is already installed; use reload()")
        added: list[Service] = []
        run = AbortController()

        def serve(type: AnyContract, factory: Factory, serveOptions: ServeOptions | None = None) -> None:
            service = makeService(type, factory, serveOptions or {}, facet.name)
            self.serveInto(self.current.services, service)
            added.append(service)

        try:
            facet.setup(self.facetApi(serve, run.signal))
        except BaseException as error:
            run.abort(error)
            raise
        self.facets[facet.name] = facet
        self.facetRunChanged(facet.name, run)
        refs = [self.startHostObject(service) for service in added if hostLifetime(service)]
        return resolved() if options.get("awaitHostObjects") is False else self._hostObjectsReady(refs)

    @eager
    async def load(self, plugins: Sequence[PluginRef], options: LoadOptions | None = None) -> None:
        """Installs all facets before awaiting their host objects; referenced objects are created when first
        referenced."""
        facets = await self.resolve(plugins)
        # No await between installs: a host object's factory may consume a type served by a later facet.
        await promiseAll([self.install(facet, options) for facet in facets])

    def ready(self) -> asyncio.Future[None]:
        """Awaits the host objects present at call time, including those started by ``serve`` or deferred loads."""
        return self._hostObjectsReady(list(self.hostObjects.values()))

    def _hostObjectsReady(self, refs: Iterable[RefCore]) -> asyncio.Future[None]:
        async def ready() -> None:
            await promiseAll([ref.ready for ref in refs])

        # Ignoring install's return still reports through onError without an unhandled rejection.
        return quiet(start(ready()))

    @eager
    async def resolve(self, plugins: Sequence[PluginRef]) -> list[Facet]:
        """Turns plugin references into facets, importing modules through the loader."""
        modules = [ref for ref in plugins if not isFacet(ref)]
        if len(modules) > 0 and self._loader is None:
            raise RuntimeError("loading modules needs createHost({ loader }), e.g. nodeLoader from misaka.chord.node")
        loaded: list[Facet] = []
        if len(modules) > 0 and self._loader is not None:
            result = self._loader(modules)
            loaded = list(await result if inspect.isawaitable(result) else result)
        return [facetOf(ref) if isFacet(ref) else loaded.pop(0) for ref in plugins]

    def facetApi(self, serve: Serve, signal: AbortSignal) -> FacetApi:
        return FacetApi(
            serve=serve,
            name=self.name,
            root=lambda fn, options=None: self.root(fn, options),
            registry=lambda registry: self.registry(registry),
            signal=signal,
        )

    def facetRunChanged(self, name: str, run: AbortController | None) -> None:
        """The installed run of facet ``name`` is now ``run`` (or none): the run it replaces is aborted."""
        replaced = self._facetRuns.get(name)
        if run is None:
            self._facetRuns.pop(name, None)
        else:
            self._facetRuns[name] = run
        if replaced is not run and replaced is not None:
            replaced.abort(ChordError("gone", f"facet {name} was replaced or removed"))

    def serveInto(self, services: dict[str, Service], service: Service) -> None:
        if service.contract.name in services:
            raise RuntimeError(f"{service.contract.name} is already served")
        placement = service.contract.description.get("host")
        if placement is not None and not placedOn(placement, self.name):
            raise TypeError(
                f"{service.contract.name} is declared for {placementText(placement)}, but {self.name} serves it"
            )
        services[service.contract.name] = service

    def startHostObject(self, service: Service) -> RefCore:
        ref = self.acquire(service.contract, {}, None, "host")
        self.hostObjects[service.contract.name] = ref
        ref.ready.add_done_callback(lambda done: _reportFailure(done, self.report))
        return ref

    # ─── HostCore ───

    def currentCodeVersion(self) -> int:
        return self.current.id

    def hasCodeVersion(self, codeVersion: int) -> bool:
        return codeVersion in self.codeVersions

    def retainedCodeVersions(self) -> Iterator[tuple[int, Mapping[str, Service]]]:
        for id, state in list(self.codeVersions.items()):
            yield id, state.services
        if self.pending is not None:
            yield self.pending.id, self.pending.services

    def pendingCodeVersion(self) -> int | None:
        return None if self.pending is None else self.pending.id

    def service(self, typeName: str) -> Service | None:
        """The current code for a type; a type a running reload adds is served from the pending code version."""
        service = self.current.services.get(typeName)
        if service is None and self.pending is not None:
            service = self.pending.services.get(typeName)
        return service

    def addressOf(self, type: AnyContract, input: Any, principal: Any, candidate: bool = False) -> str:
        elsewhere = self._elsewhere(type, input)
        if elsewhere is not None:
            return self._remoteKey(type, elsewhere[1], principal)
        service = self._bindingService(type.name, candidate)
        if service is None:
            raise unknownType(type.name, self.name)
        return self._locate(service, input, principal)[0]

    def _elsewhere(self, type: AnyContract, input: Any) -> tuple[str, Any] | None:
        """Where an object of a remote type lives when that is another host: its name and the input normalized with
        the caller's contract (the placement is read from it). None: here (and every local type)."""
        placement = type.description.get("host")
        if type.scope != "remote" or placement is None:
            return None
        normalized = normalize(type.input, input, f"{type.name} input")
        at = resolveHost(type.name, placement, normalized)
        return None if at == self.name else (at, normalized)

    def _locate(self, service: Service, input: Any, principal: Any) -> tuple[str, Any, Any]:
        """The address of ``input`` for ``principal``, normalized. A ``lifetime: "host"`` type has one object: the empty
        input, for the host principal, whoever asks; another input is invalid."""
        name, schema = service.contract.name, service.contract.input
        normalized = normalize(schema, input, f"{name} input")
        identity = canonicalIdentity(schema, normalized)
        if hostLifetime(service):
            if identity != canonicalIdentity(schema, normalize(schema, {}, f"{name} input")):
                raise invalid("input", f'{name} has lifetime "host": its one object has the empty input')
            principal = self.principal
        return f"{name}\u0000{identity}\u0000{self._principalPart(principal)}", normalized, principal

    def _principalPart(self, principal: Any) -> str:
        return "host" if self.isSelf(principal) else f"p:{self._principalKey(principal)}"

    def _remoteKey(self, type: AnyContract, normalized: Any, principal: Any) -> str:
        """The key of a remote binding: type, input identity (fields equal to their defaults omitted, as for local
        objects), principal key and the consumer's contract fingerprint. No host name: the type and the input decide
        where it lives."""
        identity = canonicalIdentity(type.input, normalized)
        return f"@{type.name}\u0000{identity}\u0000{self._principalPart(principal)}\u0000{type.fingerprint}"

    def acquire(
        self,
        type: AnyContract,
        input: Any,
        options: GetOptions | None,
        owner: RefOwner,
        from_: ObjectImpl | None = None,
        candidate: bool = False,
    ) -> RefCore:
        elsewhere = self._elsewhere(type, input)
        if elsewhere is not None:
            return self._acquireRemote(type, elsewhere[0], elsewhere[1], options, owner)
        service = self._bindingService(type.name, candidate)
        if service is None:
            raise unknownType(type.name, self.name)
        bind(service.contract, service.options.get("accepts"), type)
        principal = (options or {}).get("principal")
        address, normalized, principal = self._locate(service, input, self.principal if principal is None else principal)
        # The provider's own placement decides, never where a request says it is going (a client picks that).
        placement = service.contract.description.get("host")
        if placement is not None and resolveHost(type.name, placement, normalized) != self.name:
            raise invalid(
                "placement",
                f"{type.name} with this input lives on {resolveHost(type.name, placement, normalized)}, not {self.name}",
            )
        object = self.objects.get(address)
        if from_ is not None and object is not None and (object is from_ or reaches(object, from_)):
            raise invalid("cycle", f"{from_.address} -> {address} would create a ref cycle")
        if object is None:
            if self.gating is not None and type.name in self.gating:
                raise ChordError("upgrade_conflict", f"{type.name} is being upgraded; retry", {"reason": "gate"})
            dying = self._dying.get(address)
            if dying is not None and dying.status == "disposed":
                del self._dying[address]
            object = ObjectImpl(
                self,
                ObjectIdentity(address, type.name, normalized, principal, hostLifetime(service)),
            )
            self.objects[address] = object
            # A new object for an address still being disposed waits for that cleanup (exclusive resources).
            object.start(dying.dispose(False) if dying is not None and dying.status == "disposing" else None)
        ref = RefImpl(object, type, owner)
        object.addRef(ref)
        return ref

    def forget(self, object: ObjectImpl) -> None:
        if self.objects.get(object.address) is object:
            del self.objects[object.address]
        if object.status == "disposing":
            self._dying[object.address] = object

    def disposed(self, object: ObjectImpl) -> None:
        if self._dying.get(object.address) is object:
            del self._dying[object.address]

    def touch(self, rootId: str, object: ObjectImpl) -> None:
        self._touches.setdefault(rootId, {})[object] = None

    # ─── roots ───

    def root(self, fn: Callable[[Ctx], Any], options: RootOptions | Mapping[str, Any] | None = None) -> asyncio.Future[Any]:
        options = options or {}
        # The incarnation keeps root ids unique across restarts of a host with the same name.
        self._rootSeq += 1
        rootId = f"{self.name}#{self._incarnation}:{self._rootSeq}"
        self._local.add(rootId)
        codeVersion = options.get("codeVersion")
        return self.joinRoot(rootId, self.current.id if codeVersion is None else codeVersion, fn, options)

    @eager
    async def joinRoot(
        self,
        rootId: str,
        codeVersionId: int,
        fn: Callable[[Ctx], Any],
        options: Mapping[str, Any] | None = None,
    ) -> Any:
        """Runs ``fn`` inside root ``rootId`` of ``codeVersion``. A root counts as running here (for retirement) while
        any of its calls runs here. A root that started here ends when its last call here settles, even if it went out
        and came back over a link meanwhile. A foreign root (started elsewhere, arriving ``from`` a link) keeps its
        leases on objects until every link it came over announced its end (``root_end``) or closed, so its re-entry
        stays grandfathered."""
        options = options or {}
        codeVersion = self.codeVersions.get(codeVersionId)
        if codeVersion is None:
            self._local.discard(rootId)
            raise ChordError("superseded", f"code version {codeVersionId} is not retained by {self.name}")
        source = options.get("from")
        if source is not None and rootId not in self._local:
            self._foreign.setdefault(rootId, {})[source] = None
        codeVersion.roots[rootId] = codeVersion.roots.get(rootId, 0) + 1
        signal = options.get("signal")
        principal = options.get("principal")
        ctx = CtxImpl(
            self,
            # Forced retirement aborts what still runs on a code version before closing what only it used.
            signals=[codeVersion.abort.signal] if signal is None else [signal, codeVersion.abort.signal],
            rootId=rootId,
            codeVersion=codeVersion.id,
            principal=self.principal if principal is None else principal,
        )
        try:
            result = fn(ctx)
            # MISAKA fork: a promise resolved with a promise adopts it; an awaited coroutine may return a future.
            while inspect.isawaitable(result):
                result = await result
            return result
        finally:
            ctx.close()
            count = codeVersion.roots.get(rootId, 1) - 1
            if count > 0:
                codeVersion.roots[rootId] = count
            else:
                codeVersion.roots.pop(rootId, None)
                if rootId not in self._foreign:
                    self.endRoot(rootId)
                self.maybeRetire()
                self._retirementChanged()

    def rootSent(self, rootId: str, connection: Connection) -> None:
        """Remembers that calls of ``rootId`` went out on ``connection``, which must hear when the root ends."""
        self._rootLinks.setdefault(rootId, {})[connection] = None

    def endRoot(self, rootId: str) -> None:
        """A root ended (here, or announced by its origin): end its leases and tell the links it went out on."""
        self._local.discard(rootId)
        self._foreign.pop(rootId, None)
        for object in list(self._touches.get(rootId, {})):
            object.rootEnded(rootId)
        self._touches.pop(rootId, None)
        for connection in list(self._rootLinks.get(rootId, {})):
            connection.send({"t": "root_end", "rootId": rootId})
        self._rootLinks.pop(rootId, None)

    def foreignRootEnded(self, rootId: str, from_: Connection) -> None:
        """``from_`` announced the end of a foreign root (or closed). Honored only if the root came over ``from_``: a
        peer cannot end leases of roots it never carried. The root ends when no link that brought it remains."""
        links = self._foreign.get(rootId)
        if links is None or links.pop(from_, UNDEFINED) is UNDEFINED or len(links) > 0:
            return
        running = any(rootId in codeVersion.roots for codeVersion in self.codeVersions.values())
        # Still running here (a call in flight): the lease ends when it settles.
        if running:
            self._foreign.pop(rootId, None)
        else:
            self.endRoot(rootId)

    def maybeRetire(self) -> None:
        """Retires the previous code version once no local roots of it remain. A child also waits for its parent to
        retire it: roots span hosts, and an old root on the server may still call in."""
        if self.retirable():
            self.retire()

    def retirable(self) -> bool:
        """The previous code version has no roots left here and, on a child host, the parent has retired it."""
        previous = self.previous
        if previous is None or len(previous.roots) > 0:
            return False
        return self.kind == "client" or self.upstream is None or previous.id <= self._retireAllowed

    def allowRetire(self, codeVersion: int) -> None:
        self._retireAllowed = max(self._retireAllowed, codeVersion)
        self.maybeRetire()
        self._retirementChanged()

    def watchRetirement(self, watcher: Callable[[], None]) -> Callable[[], Any]:
        """Calls ``watcher`` whenever a root ends, the parent allows a retirement, or a code version retires."""
        self._retirementWatchers[watcher] = None
        return lambda: self._retirementWatchers.pop(watcher, UNDEFINED) is not UNDEFINED

    def _retirementChanged(self) -> None:
        for watcher in list(self._retirementWatchers):
            watcher()

    def announceRetired(self, connection: Connection) -> None:
        """Tells a child that is now in step which code versions this host retired before: every id below the oldest
        one it retains (ids only grow). Without it, a child reconciled after those retirements would never hear a
        ``retire`` frame for its own previous code version and would wait for the retire deadline at every reload."""
        retired = (self.previous or self.current).id - 1
        if retired > 0:
            connection.send({"t": "retire", "codeVersion": retired})

    def retire(self) -> None:
        """Drops the previous code version: its implementations and the declarations only it used."""
        previous = self.previous
        if previous is None:
            return
        self.previous = None
        self.codeVersions.pop(previous.id, None)
        for object in list(self.objects.values()):
            self.retireImplementation(object, previous.id)
        for connection in list(self.connections):
            if connection.role == "downstream" and connection.peer.kind == "host":
                connection.send({"t": "retire", "codeVersion": previous.id})
        self._retirementChanged()

    def retireImplementation(self, object: ObjectImpl, codeVersion: int) -> None:
        """Drops an object's implementation for ``codeVersion`` and what only that implementation declared, in disposal
        order: resources close (reverse open order) before its references are released."""
        installed = object.implementations.pop(codeVersion, None)
        if installed is None or installed.decls is object.decls:
            return
        kept = object.allDecls()

        def keep(pick: Callable[[Decls], Iterable[Any]]) -> list[Any]:
            return [item for decls in kept for item in pick(decls)]

        refs = keep(lambda decls: decls.refs.values())
        resources = keep(lambda decls: decls.resources.values())
        derives = keep(lambda decls: decls.derives.values())
        for derived in list(installed.decls.derives.values()):
            if all(derived is not other for other in derives):
                derived.detach()
        closing = sorted(
            [resource for resource in installed.decls.resources.values() if all(resource is not other for other in resources)],
            key=lambda resource: -_index(object.openOrder, resource),
        )
        releasing = [ref for ref in installed.decls.refs.values() if all(ref is not other for other in refs)]
        object.refreshSchemas()
        background(
            runAll(
                [
                    *[(lambda resource=resource: resource.close(object.openOrder)) for resource in closing],
                    *[(lambda ref=ref: ref.release()) for ref in releasing],
                ],
                f"{object.address}: retiring code version {codeVersion} failed",
            ),
            self.report,
        )

    # ─── registries ───

    def registry(self, registry: Registry[Any]) -> RegistryView[Any]:
        host = self

        def table(ctx: Ctx | None = None) -> RegistryTable[Any]:
            codeVersion = host.current if ctx is None else host.codeVersions.get(ctx.codeVersion)
            if codeVersion is None:
                raise ChordError("superseded", f"code version {None if ctx is None else ctx.codeVersion} is retired")
            return codeVersion.tables.get(registry.name, EMPTY_TABLE)

        class View:
            def table(self, ctx: Ctx | None = None) -> RegistryTable[Any]:
                return table(ctx)

            def get(self, key: str, ctx: Ctx | None = None) -> Any:
                return table(ctx).get(key)

            def subscribe(self, listener: Callable[[RegistryTable[Any], int], None]) -> Callable[[], Any]:
                listeners = host._subscribers.setdefault(registry.name, {})
                listeners[listener] = None
                listener(table(), host.current.id)
                return lambda: listeners.pop(listener, UNDEFINED) is not UNDEFINED

        return View()

    def buildTables(self, entries: Iterable[tuple[ObjectImpl, Decls]]) -> dict[str, RegistryTable[Any]]:
        """Builds registry tables from (object, declarations) pairs. Throws registry_conflict."""
        byRegistry: dict[str, dict[str, tuple[Any, ObjectImpl]]] = {}
        for object, decls in entries:
            for registration in decls.registrations.values():
                registry, key, value = registration.registry, registration.key, registration.value
                registered = byRegistry.setdefault(registry.name, {})
                existing = registered.get(key)
                if existing is not None and existing[1] is not object and registry.conflict == "error":
                    raise invalid(
                        "registry_conflict",
                        f"{registry.name}: {key} registered by {existing[1].address} and {object.address}",
                    )
                registered[key] = (value, object)
                order = self._order.setdefault(registry.name, {})
                if key not in order:
                    order[key] = len(order)
        tables: dict[str, RegistryTable[Any]] = {}
        for name, registered in byRegistry.items():
            order = self._order[name]
            ordered = sorted(registered.items(), key=lambda entry: order[entry[0]])
            tables[name] = {key: entry[0] for key, entry in ordered}
        return tables

    def _bindingService(self, typeName: str, candidate: bool) -> Service | None:
        """The service a reference binds against: the current one, or for a prepare rerun (``candidate``) the running
        reload's pending one, so consumer and provider of a breaking change can reload in one batch."""
        service = self.pending.services.get(typeName) if candidate and self.pending is not None else None
        return service if service is not None else self.service(typeName)

    def liveDecls(self, candidates: bool = False) -> list[tuple[ObjectImpl, Decls]]:
        """Live objects with their current declarations; objects waiting for a reload's commit only with
        ``candidates``."""
        return [
            (object, object.decls)
            for object in list(self.objects.values())
            if object.status == "live" and (candidates or not object.deferredRuntime)
        ]

    def checkRegistrations(self, object: ObjectImpl, decls: Decls) -> None:
        if len(decls.registrations) == 0:
            return
        self.buildTables([*[entry for entry in self.liveDecls() if entry[0] is not object], (object, decls)])

    def registriesChanged(self, including: ObjectImpl | None = None) -> None:
        entries = [entry for entry in self.liveDecls() if entry[0] is not including]
        if including is not None:
            entries.append((including, including.decls))
        self.installTables(self.current, self.buildTables(entries))

    def fail(self, error: Any) -> None:
        """A commit failed midway: from now on this host admits no work (calls fail ``unavailable/host_failed``)."""
        if self.failed is None:
            self.failed = error
        self.report(error)

    def installTables(
        self, codeVersion: CodeVersionState, tables: Mapping[str, RegistryTable[Any]], all: bool = False
    ) -> None:
        """Installs tables for ``codeVersion``; subscribers hear about changed tables of the current code version (all
        tables when ``all``)."""
        before = codeVersion.tables
        codeVersion.tables = tables
        if codeVersion is not self.current:
            return
        for name, listeners in list(self._subscribers.items()):
            next = tables.get(name, EMPTY_TABLE)
            if not all and sameTable(before.get(name, EMPTY_TABLE), next):
                continue
            current = list(listeners)

            def deliver(current: list[Any] = current, next: RegistryTable[Any] = next) -> None:
                for listener in current:
                    try:
                        listener(next, codeVersion.id)
                    except Exception as error:  # noqa: BLE001 - a subscriber's failure is reported
                        self.report(error)

            enqueue(deliver)

    # ─── public API ───

    @eager
    async def get(self, type: AnyContract, input: Any, options: Mapping[str, Any] | None = None) -> Any:
        """A reference held until ``release()``. Resolves once it is ready: live, with every state member of its variant
        (unless ``state: False``). Rejects with the activation error, ``aborted`` (``signal``), or
        ``unavailable/ready_timeout`` after ``readyTimeout``; the reference is released then."""
        ref = self.acquire(type, input, options, "external")  # type: ignore[arg-type]
        signal = (options or {}).get("signal")
        await awaitReady(ref, [] if signal is None else [signal], self.readyTimeout)
        return ref.proxy

    def hold(self, type: AnyContract, input: Any, options: GetOptions | None = None) -> Any:
        """The same reference, synchronously, possibly still ``loading``: for UI runtimes that render its status (hooks,
        a TUI watch). Its state members throw ``not_ready`` until it is live."""
        return self.acquire(type, input, options, "external").proxy

    @eager
    async def invalidatePrincipal(self, match: PrincipalMatch) -> None:
        """Force-disposes every object whose principal matches (never the host's own principal), re-stamps or closes
        matching links, and propagates pattern matches to child hosts."""
        matches = match if callable(match) else (lambda principal: matchesPattern(match, principal))
        reason = ChordError("gone", "principal invalidated")
        # Links acting as a matching principal are re-stamped (if the acceptor can) or closed.
        for connection in list(self.connections):
            if connection.role != "downstream" or not matches(connection.principal):
                continue
            # Relays under the old principal close at once, including those still resolving their route.
            connection.revoke(None if connection.restamp is None else connection.restamp(connection.principal))
        # Patterns propagate to child hosts, which dispose their own matching objects (e.g. relayed guests).
        if not callable(match):
            for connection in list(self.connections):
                if connection.role == "downstream" and connection.peer.kind == "host":
                    connection.send({"t": "invalidate", "pattern": dict(match)})
        matching = [
            object
            for object in list(self.objects.values())
            if not self.isSelf(object.principal) and matches(object.principal)
        ]
        await disposeHoldersFirst(matching, reason, "invalidatePrincipal: disposal failed")

    # ─── links ───

    def accept(self, link: Link, options: AcceptOptions) -> None:
        """Accepts a child link (a worker or a browser); requests on it act as ``options["principal"]``."""
        kind = nullishCoalesce(options.get("kind"), "client")
        if kind == "host" and options.get("name") is None:
            raise TypeError("accepting a host link needs its name")
        config: dict[str, Any] = {
            **options,
            "role": "downstream",
            "principal": options.get("principal"),
            "peer": Peer(kind, options.get("name") if kind == "host" else None),
        }
        # Holds over a client link carry its id as their origin (also when relayed), so hosts can tell clients apart.
        if kind == "client":
            self._originSeq += 1
            config["originId"] = f"{self._incarnation}:{self._originSeq}"
        self._addConnection(link, config)

    def connect(self, link: Link, options: Mapping[str, Any]) -> None:
        """Sets (or replaces, after a reconnect) the link to this host's parent; remote references re-hold over it. The
        parent's own requests act as ``options["principal"]``; requests it relays act as the principal it forwards."""
        if "principal" not in options:
            raise TypeError("connect needs `principal`: what the parent's own requests act as")
        if self.upstream is not None and self.upstream.open:
            self.upstream.link.close("replaced")
        connection = self._addConnection(
            link,
            {**options, "role": "upstream", "principal": options["principal"], "peer": Peer("host", None)},
        )
        self.upstream = connection
        # Bindings that lost their link re-hold over the new one (`again`: they keep their values meanwhile).
        for binding in list(self.bindings.values()):
            if binding.hold is None and not binding.ended:
                connection.hold(binding, binding.applied)

    def _addConnection(self, link: Link, config: Mapping[str, Any]) -> Connection:
        connection = Connection(self, link, config)
        # A link that was already closed closed the connection in its constructor: keeping it would route to it
        # forever.
        if connection.open:
            self.connections[connection] = None
        return connection

    def route(self, prefix: str, route: Any) -> None:
        if not callable(_route(route, "principal")):
            raise TypeError(f"route {prefix} needs `principal`: what the routed hosts act as when they call back")
        self._routes[prefix] = route

    def peerHello(self, connection: Connection, codeVersion: int) -> None:
        """A child host announced its code version. In step: usable. Behind: fenced (nothing is routed to it) and
        reconciled by a reload of its modules to this host's code version, never by renumbering what it runs. Ahead:
        impossible, refused."""
        if codeVersion > self.current.id:
            self.report(
                ChordError("invalid", f"{connection.name} runs code version {codeVersion}, ahead of {self.current.id}")
            )
            connection.link.close("code version ahead of parent")
            return
        if self.reloading:
            # Not a participant of the running reload (participants are fixed when it starts): in step now, it would
            # miss the commit. Decided when the reload ends. A child already behind is fenced now, so the calls its
            # roots make meanwhile are admitted in this host's code version (never relabeled) instead of being
            # `superseded`.
            connection.peerCodeVersion = codeVersion
            if codeVersion < self.current.id:
                connection.fencedCodeVersions.add(codeVersion)
            self._deferredHellos[connection] = codeVersion
            return
        if codeVersion == self.current.id:
            connection.markSynced(codeVersion)
            self.announceRetired(connection)
            return
        connection.peerCodeVersion = codeVersion
        connection.fencedCodeVersions.add(codeVersion)

        def failed(error: BaseException) -> None:
            self.report(error)
            connection.link.close("reconciliation failed")

        background(reconcileChild(self, connection), failed)

    def reloadEnded(self) -> None:
        """The host's reload slot was freed: children that said hello meanwhile are synced or reconciled now."""
        self.reloading = False
        deferred = list(self._deferredHellos.items())
        self._deferredHellos.clear()
        for connection, codeVersion in deferred:
            if connection.open:
                self.peerHello(connection, codeVersion)
        waiters, self._reloadWaiters = self._reloadWaiters, []
        for resolve in waiters:
            resolve()
        self.maybeRetire()

    def whenReloadEnded(self) -> asyncio.Future[None]:
        """Resolves once no reload runs on this host."""
        if not self.reloading:
            return resolved()
        done = deferred()
        self._reloadWaiters.append(done.resolve)
        return done.promise

    def connectionClosed(self, connection: Connection) -> None:
        self.connections.pop(connection, None)
        self._deferredHellos.pop(connection, None)
        # Foreign roots that came over this link will never announce their end over it.
        for rootId, links in list(self._foreign.items()):
            if connection in links:
                self.foreignRootEnded(rootId, connection)
        for links in self._rootLinks.values():
            links.pop(connection, None)
        if self.upstream is connection:
            self.upstream = None
            self._childReloads.parentLost()

    def bindingIdle(self, binding: RemoteBinding) -> None:
        """The last reference to ``binding`` was released: it lingers if its type does (``HostOptions.linger``), else
        closes."""
        ms = self._lingerTypes.get(binding.contract.name, 0) if binding.contract.name in self._lingerTypes else 0
        # A binding that never had a shape has nothing worth keeping.
        if ms <= 0 or binding.ended or not binding.applied or self._lingerCap <= 0:
            binding.close()
            return
        binding.lingerTimer = setTimeout(binding.close, ms)
        self._lingering[binding.key] = binding
        for oldest in list(self._lingering.values()):
            if len(self._lingering) <= self._lingerCap:
                break
            oldest.close()

    def forgetBinding(self, binding: RemoteBinding) -> None:
        if self.bindings.get(binding.key) is binding:
            del self.bindings[binding.key]
        if self._lingering.get(binding.key) is binding:
            del self._lingering[binding.key]

    def _childNamed(self, name: str) -> Connection | None:
        """A downstream host connection by its configured name (never by what a peer announced)."""
        for connection in self.connections:
            if (
                connection.open
                and connection.role == "downstream"
                and connection.peer.kind == "host"
                and connection.peer.name == name
            ):
                return connection
        return None

    def _acquireRemote(
        self, type: AnyContract, at: str, input: Any, options: GetOptions | None, owner: RefOwner
    ) -> RefCore:
        """A reference to an object on host ``at``: a view onto the binding for its key, created (and held) if there is
        none. ``input`` is normalized with ``type`` (the hold carries it so)."""
        given = (options or {}).get("principal")
        principal = None if given is None or self.isSelf(given) else given
        key = self._remoteKey(type, input, self.principal if principal is None else principal)
        binding = self.bindings.get(key)
        fresh = binding is None or binding.ended
        if binding is None or binding.ended:
            binding = RemoteBinding(self, key, type, at, input, principal)
            self.bindings[key] = binding
        ref = RemoteRef(binding, owner, (options or {}).get("state") is not False)
        self._lingering.pop(key, None)
        binding.addView(ref)
        if fresh:
            self._hold(binding)
        return ref

    def _hold(self, binding: RemoteBinding) -> None:
        """Holds ``binding`` over the link to its host: the parent's, a child's, or one a route resolves."""

        def bindWhenSynced(connection: asyncio.Future[Connection]) -> None:
            def settled(done: asyncio.Future[Connection]) -> None:
                if done.cancelled():
                    return
                error = done.exception()
                if error is not None:
                    binding.gone(error)
                    return
                target = done.result()
                if not binding.ended and binding.hold is None:
                    target.hold(binding, False)

            connection.add_done_callback(settled)

        direct = self._childNamed(binding.at)
        if self.upstream is not None:
            self.upstream.hold(binding, False)
        elif direct is not None:

            async def synced() -> Connection:
                await direct.whenSynced()
                return direct

            bindWhenSynced(quiet(start(synced())))
        else:
            route = self._routeFor(binding.at)
            if route is None:
                binding.gone(ChordError("unavailable", f"no route to {binding.at}"))
            else:
                bindWhenSynced(
                    quiet(self._resolve(route, binding.at, self.principal if binding.principal is None else binding.principal))
                )

    def _routeFor(self, hostName: str) -> Any:
        best: tuple[str, Any] | None = None
        for entry in self._routes.items():
            if hostName.startswith(entry[0]) and (best is None or len(entry[0]) > len(best[0])):
                best = entry
        return None if best is None else best[1]

    @eager
    async def _resolve(
        self, route: Any, hostName: str, principal: Any, still: Callable[[], bool] = lambda: True
    ) -> Connection:
        """Authorize, resolve (maybe spawn), authorize again; returns the synced connection to the host."""

        def refuse() -> ChordError:
            return ChordError("forbidden", f"{hostName} refused for this principal")

        def revoked() -> ChordError:
            return ChordError("gone", "principal invalidated while resolving")

        async def authorize() -> bool:
            allowed = _route(route, "authorize")(principal, hostName)
            return bool(await allowed if inspect.isawaitable(allowed) else allowed)

        if not await authorize():
            raise refuse()
        if not still():
            raise revoked()
        resolving = _route(route, "resolve")(hostName)
        link = await resolving if inspect.isawaitable(resolving) else resolving
        if not still():
            raise revoked()
        if not await authorize():
            raise refuse()
        if not still():
            raise revoked()
        connection = next((candidate for candidate in self.connections if candidate.link is link), None)
        if connection is None:
            connection = self._addConnection(
                link,
                {"role": "downstream", "principal": _route(route, "principal")(hostName), "peer": Peer("host", hostName)},
            )
        await connection.whenSynced()
        if not still():
            raise revoked()
        return connection

    @eager
    async def relay(self, relay: RelayRef) -> None:
        """Relays a reference from ``relay.origin`` to the host it names. Relaying needs a route: it is where
        authorization lives. The origin's authorization generation is re-checked after every await and right before
        forwarding, so a revocation during resolution wins."""
        at = relay.frame["at"]
        # The hold names its target; its contract token says where the type lives. They must agree.
        placement = contractFromDescription(relay.frame["token"]).description.get("host")
        if placement is None or resolveHost(relay.frame["token"]["name"], placement, relay.frame.get("input", UNDEFINED)) != at:
            raise invalid("placement", f"{relay.frame['token']['name']} with this input does not live on {at}")
        route = self._routeFor(at)
        if route is None:
            if self._childNamed(at) is None:
                raise ChordError("unavailable", f"no route to {at}")
            raise ChordError("forbidden", f"no route authorizes {at}")
        down = await self._resolve(route, at, relay.principal, lambda: relay.current)
        if not relay.current:
            return
        relay.open(down)

    def reload(self, plugins: Sequence[PluginRef], options: ReloadOptions | None = None) -> asyncio.Future[ReloadReport]:
        """Reloads the given facets (modules are re-imported); facets not listed keep running unchanged."""
        options = options or {}
        # Client-local facet code versions never cross the parent link; only child hosts require cluster coordination.
        if self.kind != "client" and self.upstream is not None:
            return rejected(
                ChordError("unavailable", "reloads are coordinated by the server", {"reason": "not_coordinator"})
            )
        children = any(
            connection.role == "downstream" and connection.peer.kind == "host" for connection in self.connections
        )
        running = (
            runClusterReload(self, plugins, options)
            if children or options.get("remote") is not None or options.get("remoteRemove") is not None
            else runReload(self, plugins, options)
        )
        # A promise cannot be cancelled: a caller that stops waiting (`asyncio.wait_for`) leaves the reload running to
        # its commit or failure, as upstream's does; only its own view of it is cancelled.
        view = asyncio.shield(hold(running))
        # The caller that gave up raced the reload (`Promise.race` attaches a handler upstream): its later failure is
        # handled, not reported as never retrieved. One nobody ever awaited still is (the view is not quieted).
        view.add_done_callback(lambda done: quiet(running) if done.cancelled() else None)
        return view

    def onReloadFrame(self, connection: Connection, frame: Frame) -> None:
        self._childReloads.handle(connection, frame)

    def rememberRemoteModules(
        self, remote: Mapping[str, Sequence[str]] | None, remove: Mapping[str, Sequence[str]] | None
    ) -> None:
        for prefix in dict.fromkeys([*(remote or {}), *(remove or {})]):
            modules = list((remote or {}).get(prefix) or [])
            removed = list((remove or {}).get(prefix) or [])
            self.remoteModules[prefix] = [
                module for module in dict.fromkeys([*self.remoteModules.get(prefix, []), *modules]) if module not in removed
            ]
            self.remoteRemoved[prefix] = [
                module for module in dict.fromkeys([*self.remoteRemoved.get(prefix, []), *removed]) if module not in modules
            ]

    def peekNextCodeVersion(self) -> int:
        """The code version id the next reload would commit."""
        return self._codeVersionSeq + 1

    @eager
    async def close(self) -> None:
        """Disposes every object, holders before what they reference (a resource's ``close`` may still use the objects
        its object references, local or remote), then closes the links."""
        for binding in list(self._lingering.values()):
            binding.close()
        self.hostObjects.clear()
        for name in list(self._facetRuns):
            self.facetRunChanged(name, None)
        reason = ChordError("gone", f"host {self.name} closed")
        try:
            await disposeHoldersFirst(list(self.objects.values()), reason, "host close: disposal failed")
        finally:
            for connection in list(self.connections):
                connection.link.close("host closed")


async def disposeHoldersFirst(objects: Sequence[ObjectImpl], reason: Any, message: str) -> None:
    """Force-disposes ``objects`` in waves: an object goes once no object of the set that references it is left, so
    holders are disposed (and their resources closed) while what they reference is still live. In-host references are
    acyclic. Errors of every disposal are collected."""
    remaining: dict[ObjectImpl, None] = dict.fromkeys(objects)

    def held(object: ObjectImpl) -> list[ObjectImpl]:
        result: dict[ObjectImpl, None] = {}
        for decls in object.allDecls():
            for ref in decls.refs.values():
                if ref.object is not None:
                    result[ref.object] = None
        return list(result)

    refs = {object: held(object) for object in objects}
    errors: list[Exception] = []
    while len(remaining) > 0:
        heldByRemaining: set[ObjectImpl] = set()
        for object in remaining:
            for target in refs.get(object, []):
                heldByRemaining.add(target)
        wave = [object for object in remaining if object not in heldByRemaining]
        # A cycle cannot happen in-host; dispose the rest together rather than loop.
        if len(wave) == 0:
            wave = list(remaining)
        for object in wave:
            remaining.pop(object, None)
        results = await allSettled([object.dispose(True, reason) for object in wave])
        for result in results:
            if result.status == "rejected":
                errors.append(asException(result.reason))  # type: ignore[arg-type]
    if len(errors) == 1:
        raise errors[0]
    if len(errors) > 1:
        raise ExceptionGroup(message, errors)


def sameTable(a: RegistryTable[Any], b: RegistryTable[Any]) -> bool:
    if len(a) != len(b):
        return False
    keys = list(a.keys())
    return all(
        strictEqual(keys[index], key) and strictEqual(a.get(key, UNDEFINED), value)
        for index, (key, value) in enumerate(b.items())
    )


def matchesPattern(pattern: Mapping[str, Any], principal: Any) -> bool:
    """Whether every field of ``pattern`` is equal (``deepEqual``) in ``principal``, which may be any object: an array's
    fields are its indices (``"0"``, ``"1"``, ...), and a missing field is ``undefined``."""
    if principal is None or principal is UNDEFINED or isinstance(principal, str | int | float | bool):
        return False
    return all(deepEqual(_field(principal, key), value) for key, value in pattern.items())


def _field(value: Any, key: str) -> Any:
    """``value[key]`` of a JSON object or array (``undefined`` when it has no such field)."""
    if isinstance(value, Mapping):
        return value.get(key, UNDEFINED)
    if isinstance(value, list):
        if key == "length":
            return len(value)
        # An array index is the canonical decimal text of an integer (`"1"`, not `"01"`).
        if key.isdecimal() and key.isascii() and str(int(key)) == key and int(key) < len(value):
            return value[int(key)]
    return UNDEFINED


def unknownType(type: str, host: str) -> ChordError:
    return ChordError("unavailable", f"{type} is not served by {host}", {"reason": "unknown_type"})


def reaches(from_: ObjectImpl, to: ObjectImpl) -> bool:
    """True if ``from_`` references ``to``, directly or transitively, through object-owned references on this host."""
    seen: set[ObjectImpl] = set()
    stack = [from_]
    while len(stack) > 0:
        object = stack.pop()
        if object is to:
            return True
        if object in seen:
            continue
        seen.add(object)
        # Remote references have no object here: cycles across hosts are avoided by convention.
        for decls in object.allDecls():
            for ref in decls.refs.values():
                if ref.object is not None:
                    stack.append(ref.object)
    return False


def _index(items: list[Any], item: Any) -> int:
    for index, candidate in enumerate(items):
        if candidate is item:
            return index
    return -1


def _reportFailure(done: asyncio.Future[Any], report: Callable[[Any], None]) -> None:
    if done.cancelled():
        return
    error = done.exception()
    if error is not None:
        report(error)


__all__ = [
    "CodeVersionState",
    "ConnectOptions",
    "Facet",
    "FacetApi",
    "FacetDefinition",
    "FacetLoader",
    "Host",
    "HostImpl",
    "HostOptions",
    "LoadOptions",
    "PluginRef",
    "PrincipalMatch",
    "PrincipalPattern",
    "RootOptions",
    "Route",
    "Serve",
    "createHost",
    "defineFacet",
]

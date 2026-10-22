"""Objects, references, call contexts and the factory's ``self``: the core of a host.

Translated from pi's ``packages/chord/src/object.ts``. MISAKA forks, all forced by the language:

- A promise is an ``asyncio.Future`` (``misaka.chord._promise``); upstream's ``async`` methods are ``eager``
  coroutines, so they start when called, as JavaScript async functions do.
- A reference's ``Proxy`` is ``RefProxy``: attribute access binds members late to the current implementation,
  ``name in ref`` is the proxy's ``has`` trap, and the reference behind a proxy is ``refOf(proxy)`` (upstream's
  ``refsByProxy`` WeakMap).
- An implementation is the factory's own ``dict``. Upstream caches a reference's adapter views in a ``WeakMap`` keyed
  by the implementation; a ``WeakMap`` is an ephemeron table, and a Python weak mapping is not: its value (a view,
  which closes over its implementation) would keep its key alive as long as the reference lives. The cache is
  therefore kept on the implementation's ``Installed`` records (``Installed.views``, shared by the records of one
  implementation), weakly keyed by the reference: it goes when the implementation is retired, an entry with its
  reference.
- JavaScript has no cancellation: a ``CancelledError`` that is not the running task's own cancellation (one raised by
  awaiting a future someone else cancelled) is a rejection like any other (``isRejection``). The task's own
  cancellation, ``KeyboardInterrupt`` and ``SystemExit`` run the same cleanup, then propagate (``GeneratorExit``, a
  coroutine being closed, skips the cleanup: nothing may be awaited then). Awaiting a promise never cancels it in
  JavaScript, so a shared one (an activation, a disposal, a reference's ``ready``) is awaited without passing a
  waiter's cancellation on (``asyncio.wait``, ``promiseAll``), and ``ref.ready`` is an ``exposed`` view: a caller that
  gives up (``wait_for``) cancels its own view, never the activation every holder shares.
- JavaScript ``Set``\\ s keep insertion order: they are ``dict``\\ s with ``None`` values here.
- Two listener ``select`` functions are the same when they are one object or their code objects are equal (upstream
  compares their source text).
"""

from __future__ import annotations

import asyncio
import inspect
import math
import weakref
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal, Never, Protocol, TypedDict

from misaka.chord._promise import (
    background,
    deferred,
    exposed,
    isRejection,
    promiseAll,
    quiet,
    race,
    rejected,
    rejection,
    resolved,
    start,
)
from misaka.chord._typebox import TSchema, Value
from misaka.chord.cell import (
    Cell,
    Derived,
    Hydration,
    SourceOptions,
    hydrationOf,
    isSource,
    notReady,
    unhydrated,
)
from misaka.chord.contract import (
    LOCAL_VARIANT,
    Accepts,
    Adapter,
    AnyContract,
    MemberDescription,
    bind,
    takesNoArgs,
)
from misaka.chord.declare import ListenerDecl, ResourceDeclaration, Subscribable
from misaka.chord.errors import ChordError, asChordError, invalid, runAll
from misaka.chord.publish import enqueue
from misaka.chord.registry import Registration, Registry
from misaka.chord.schema import assertValid, normalize
from misaka.utils.js import (
    UNDEFINED,
    AbortController,
    AbortSignal,
    allSettled,
    clearTimeout,
    eager,
    jsIsInteger,
    jsString,
    nullishCoalesce,
    setTimeout,
)

if TYPE_CHECKING:
    from misaka.chord.remote_ref import RemoteHost
    from misaka.chord.types import (
        Ctx,
        GetOptions,
        Readable,
        RefStatus,
        Shape,
        StateEvent,
    )


def noop(*_args: Any) -> None:
    return None


async def _settled(promise: asyncio.Future[Any]) -> None:
    """``await promise.catch(noop)``: waits until it settles, whatever the outcome. A waiter that gives up does not
    cancel it: it is shared (a predecessor's disposal, an activation others wait for)."""
    if not promise.done():
        await asyncio.wait([promise])


def strictEqual(a: Any, b: Any) -> bool:
    """JavaScript's ``a === b``: strings and numbers by value (a boolean is never a number; NaN equals nothing; -0 is
    0), ``null`` and ``undefined`` by themselves, everything else (objects, arrays, functions) by identity."""
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return a == b
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    return a is b


# ─── Services ────────────────────────────────────────────────────────────────


class ReferencedServeOptions(TypedDict, total=False):
    lifetime: Literal["referenced"]
    # Milliseconds an object stays alive after its last reference is released.
    linger: float
    # Adapters for consumers built against older contract versions.
    accepts: Accepts


class HostServeOptions(TypedDict, total=False):
    lifetime: Literal["host"]
    # An object of lifetime `"host"` lives as long as the host: it does not linger (the key must be absent).
    linger: Never
    # Adapters for consumers built against older contract versions.
    accepts: Accepts


# `serve` options. `lifetime` says how long objects of the type live. `"referenced"` (default): created at the first
# `get` of an address, disposed after the last reference is released (plus `linger`). `"host"`: one object, created
# when its facet loads (loading waits until it is live) and kept until the host closes or a reload removes its facet.
# Only local types whose input accepts `{}` may have it (`serve` checks it); every `get` reaches that one object,
# created for the host principal. Only such objects may register into registries.
type ServeOptions = ReferencedServeOptions | HostServeOptions
# The options `serve` accepts for `C` (TypeScript narrows them by the contract; `serve` checks at run time).
type ServeOptionsFor = ServeOptions
type Lifetime = Literal["referenced", "host"]
# A type's factory: `(input, principal, self) -> implementation`. `principal` is the caller the object is created for.
type Factory = Callable[[Any, Any, "Self"], Any]


@dataclass(eq=False)
class Service:
    contract: AnyContract
    factory: Factory
    options: ServeOptions
    facet: str


def makeService(contract: AnyContract, factory: Factory, options: ServeOptions, facet: str) -> Service:
    checkLifetime(contract, options)
    return Service(contract, factory, options, facet)


def hostLifetime(service: Service) -> bool:
    """Whether objects of ``service`` have ``lifetime: "host"``."""
    return service.options.get("lifetime") == "host"


def checkLifetime(contract: AnyContract, options: ServeOptions) -> None:
    """Refuses lifetimes the type cannot have, when it is served."""
    lifetime = nullishCoalesce(options.get("lifetime"), "referenced")
    if lifetime != "referenced" and lifetime != "host":
        raise TypeError(f"{contract.name}: unknown lifetime {jsString(lifetime)}")
    if lifetime != "host":
        return
    if contract.scope != "local":
        raise TypeError(f'{contract.name}: lifetime "host" needs a local type (remote types are referenced)')
    # `options.linger !== undefined`: an explicit `None` (JSON null) is a linger too.
    if options.get("linger", UNDEFINED) is not UNDEFINED:
        raise TypeError(f'{contract.name}: an object of lifetime "host" does not linger (it lives as long as the host)')
    try:
        normalize(contract.input, {}, f"{contract.name} input")
    except Exception:  # noqa: BLE001 - any refusal of `{}` is the same answer
        raise TypeError(f'{contract.name}: lifetime "host" needs an input that accepts {{}} (the object\'s one address)') from None


# ─── Declarations ────────────────────────────────────────────────────────────


class StateOptions(TypedDict, total=False):
    """Options of ``self.state``."""

    # The meaning of the cell's value (default 1). A reload that raises it runs `migrate`.
    version: int
    # Maps the value of an older version (`from`) to this version's, at the reload that raised the version. The cell
    # may be several versions behind. Without it, the value is kept and must fit the new schema.
    migrate: Callable[[Any, int], Any]


class StateInit(TypedDict):
    """A cell's first value from its source: ``init`` runs after the object's resources are open and its references
    are live, before the object goes live, so no call, listener or subscriber sees a value that is not from the
    source. It runs in a root of its own (acting as the host principal) and may be async; the inits of one object run
    in parallel. Its value is validated against the schema; a throw or an invalid value fails the activation."""

    init: Callable[[Ctx], Any]


class Self(Protocol):
    """What a factory declares through (see ``SelfImpl`` for each declaration)."""

    def state(self, name: str, schema: TSchema, initial: Any, options: StateOptions | None = None) -> Cell[Any]: ...

    def get(self, type: AnyContract, input: Any, options: GetOptions | None = None) -> Any: ...

    def resource(self, name: str, declaration: ResourceDeclaration[Any]) -> Any: ...

    def on(self, name: str, source: Any, *rest: Any) -> None: ...

    def task(self, fn: Callable[[Ctx], Awaitable[None]]) -> None: ...

    def derive(self, sources: Sequence[Any], fn: Callable[..., Any]) -> Readable[Any]: ...

    def register(self, registry: Registry[Any], entries: Mapping[str, Any]) -> None: ...

    def expose(self, resource: Any) -> Any: ...


class ExposedImpl:
    """Runtime form of ``Exposed<T>``."""

    __slots__ = ("resource",)

    def __init__(self, resource: ResourceImpl) -> None:
        self.resource = resource


class ResourceImpl:
    def __init__(self, key: str, spec: ResourceDeclaration[Any], deps: Sequence[RefCore | ResourceImpl]) -> None:
        self.key = key
        # The declaration of the factory run whose code is current: a kept resource adopts the new one at commit.
        self.spec = spec
        self.deps = list(deps)
        # `unavailable`: a close/reopen failed during a reload rollback; the value is gone for good.
        self.state: Literal["closed", "opening", "open", "unavailable"] = "closed"
        self._value: Any = UNDEFINED

    @property
    def value(self) -> Any:
        if self.state == "unavailable":
            raise ChordError("unavailable", f"resource {self.key} could not be restored")
        if self.state != "open":
            raise notReady(f"resource {self.key}")
        return self._value

    @property
    def exclusive(self) -> bool:
        return self.spec.get("exclusive") is True

    @eager
    async def open(self, signal: AbortSignal, order: list[ResourceImpl]) -> None:
        """Opens the resource. An open that completes after ``signal`` aborted (rollback, abandonment) closes the value
        it produced at once: nobody owns it any more."""
        if self.state != "closed":
            return
        self.state = "opening"
        try:
            value = self.spec["open"]({"signal": signal})
            if inspect.isawaitable(value):
                value = await value
        except BaseException:
            self.state = "closed"
            raise
        if signal.aborted:
            self.state = "closed"
            close = self.spec.get("close")
            if close is not None:
                closed = close(value)
                if inspect.isawaitable(closed):
                    await closed
            raise ChordError("aborted", f"resource {self.key} opened after abort")
        self._value = value
        self.state = "open"
        order.append(self)

    @eager
    async def close(self, order: list[ResourceImpl]) -> None:
        if self.state != "open":
            return
        self.state = "closed"
        if self in order:
            order.remove(self)
        value = self._value
        self._value = UNDEFINED
        close = self.spec.get("close")
        if close is not None:
            closed = close(value)
            if inspect.isawaitable(closed):
                await closed

    def markUnavailable(self) -> None:
        self.state = "unavailable"
        self._value = UNDEFINED

    def adopt(self, spec: ResourceDeclaration[Any]) -> None:
        """A reload kept this resource (same version, deps and exclusivity): its later ``close`` is the new
        declaration's, and the old declaration (with the old code it closes over) is no longer referenced."""
        self.spec = spec


class ResourceFacade:
    """What ``self.resource`` returns: the resource, except that ``.value`` throws ``not_ready`` while any factory
    runs, even if the resource is open (a reload's prepare rerun must not touch the native resource the old code
    uses)."""

    __slots__ = ("_host", "_resource")

    def __init__(self, resource: ResourceImpl, host: HostCore) -> None:
        self._resource = resource
        self._host = host

    @property
    def key(self) -> str:
        return self._resource.key

    @property
    def value(self) -> Any:
        if self._host.inFactory > 0:
            raise notReady(f"resource {self.key} (inside a factory)")
        return self._resource.value


def resourceOf(value: Any) -> ResourceImpl | None:
    if isinstance(value, ResourceImpl):
        return value
    if isinstance(value, ResourceFacade):
        return value._resource
    return None


type TaskFn = Callable[[Ctx], Awaitable[None]]


class Decls:
    """Everything one factory run declared, by kind and key."""

    def __init__(self) -> None:
        self.cells: dict[str, Cell[Any]] = {}
        self.cellSchemas: dict[str, TSchema] = {}
        self.cellVersions: dict[str, int] = {}
        self.cellMigrations: dict[str, Callable[[Any, int], Any]] = {}
        self.refs: dict[str, RefCore] = {}
        self.resources: dict[str, ResourceImpl] = {}
        # Resources this run kept from the previous one, with this run's declarations (adopted at commit).
        self.adopted: dict[ResourceImpl, ResourceDeclaration[Any]] = {}
        self.listeners: dict[str, ListenerDecl] = {}
        # By position (`#0`, `#1`, ...): tasks have no names.
        self.tasks: dict[str, TaskFn] = {}
        # By position: derivations have no names and are new at every run.
        self.derives: dict[str, Derived[Any]] = {}
        self.registrations: dict[str, Registration] = {}


type InitFn = Callable[[Ctx], Any]


@dataclass(frozen=True)
class RunCreated:
    refs: list[RefCore] = field(default_factory=list)
    resources: list[ResourceImpl] = field(default_factory=list)
    derives: list[Derived[Any]] = field(default_factory=list)
    # Cells this run created with `init`, which fills them before they are used.
    inits: list[tuple[Cell[Any], InitFn]] = field(default_factory=list)


@dataclass(frozen=True)
class Run:
    """One factory run: its implementation, declarations and the objects it created (not reused from the previous
    run)."""

    implementation: Any
    decls: Decls
    created: RunCreated


def isStateInit(value: Any) -> bool:
    return isinstance(value, Mapping) and callable(value.get("init"))


@dataclass(frozen=True)
class Installed:
    codeVersion: int
    service: Service
    implementation: dict[str, Any]
    decls: Decls
    # MISAKA fork: upstream's per-reference `views` WeakMap, kept with the implementation (see the module docstring):
    # its adapter view for each reference, shared by every record of this implementation (`dataclasses.replace` and
    # `install` pass it on).
    views: weakref.WeakKeyDictionary[RefImpl, dict[str, Any]] = field(
        default_factory=weakref.WeakKeyDictionary, compare=False, repr=False
    )


# ─── Host interface the runtime pieces use ───────────────────────────────────


class HostCore(Protocol):
    name: str
    dev: bool
    principal: Any
    inFactory: int
    # Set when a commit failed midway: the host admits no more work (terminal).
    failed: Any
    # Milliseconds a reload waits for aborted tasks to finish before starting their replacements.
    taskStopDeadline: float
    # Milliseconds an activation or a reload's prepare waits for its references to be ready and hydrated before it
    # fails (`unavailable/ready_timeout`; a reference cycle across hosts never resolves).
    readyTimeout: float

    def report(self, error: Any) -> None: ...

    def currentCodeVersion(self) -> int: ...

    def hasCodeVersion(self, codeVersion: int) -> bool: ...

    def retainedCodeVersions(self) -> Iterable[tuple[int, Mapping[str, Service]]]: ...

    def pendingCodeVersion(self) -> int | None: ...

    def service(self, typeName: str) -> Service | None: ...

    def acquire(
        self,
        type: AnyContract,
        input: Any,
        options: GetOptions | None,
        owner: RefOwner,
        from_: ObjectImpl | None = None,
        candidate: bool = False,
    ) -> RefCore: ...

    def addressOf(self, type: AnyContract, input: Any, principal: Any, candidate: bool = False) -> str: ...

    def root(self, fn: Callable[[Ctx], Any], options: Mapping[str, Any] | None = None) -> asyncio.Future[Any]: ...

    def touch(self, rootId: str, object: ObjectImpl) -> None: ...

    def forget(self, object: ObjectImpl) -> None: ...

    def disposed(self, object: ObjectImpl) -> None:
        """The object finished its disposal."""
        ...

    def checkRegistrations(self, object: ObjectImpl, decls: Decls) -> None: ...

    def registriesChanged(self, including: ObjectImpl | None = None) -> None:
        """Rebuilds the current registry tables from live objects (plus ``including``, which is finishing
        activation)."""
        ...


# ─── Contexts ────────────────────────────────────────────────────────────────


class CtxImpl:
    def __init__(
        self,
        host: HostCore,
        signals: Sequence[AbortSignal],
        rootId: str,
        codeVersion: int,
        principal: Any,
        holder: bool = False,
    ) -> None:
        """``signals``: the ctx is aborted with the first of them; it stops following them when it closes."""
        self.host = host
        linked = linkedSignal(signals)
        self.signal = linked.signal
        self._release = linked.release
        self.rootId = rootId
        self.codeVersion = codeVersion
        self.principal = principal
        # Code holders (tasks): every call they make starts a fresh root.
        self.holder = holder
        self._refs: list[RefCore] = []
        # Aborted when the ctx closes: awaited `get`s give up.
        self._closing = AbortController()
        self._closed = False

    @property
    def closed(self) -> bool:
        """True after the call or root that received this ctx settled: it may no longer start work."""
        return self._closed

    def check(self, host: HostCore | RemoteHost) -> None:
        """Throws unless this ctx may start work on ``host`` (the called reference's host) now: not expired, aborted or
        foreign."""
        if self.host is not host:
            raise invalid("foreign_ctx", "ctx belongs to another host")
        if self._closed:
            raise ChordError("aborted", "ctx expired: its call already settled", {"reason": "expired"})
        if self.signal.aborted:
            reason = self.signal.reason
            # A forced retirement aborted the root: its code version is gone, so the next call is `superseded`
            # (retryable).
            if isinstance(reason, ChordError) and reason.code == "superseded":
                raise ChordError("superseded", reason.message, {"cause": reason})
            raise ChordError("aborted", "ctx aborted", {"cause": reason})

    @eager
    async def get(self, type: AnyContract, input: Any, options: GetOptions | None = None) -> Any:
        self.check(self.host)
        ref = self.host.acquire(type, input, {"principal": self.principal, **(options or {})}, "call")
        self._refs.append(ref)
        await awaitReady(ref, [self.signal, self._closing.signal], self.host.readyTimeout)
        return ref.proxy

    def child(self, object: ObjectImpl) -> CtxImpl:
        """A context for a call into ``object``: same root and code version, its own call-owned references, aborted on
        forced disposal. Code inside an object acts as the host principal."""
        return CtxImpl(
            self.host,
            signals=[self.signal, object.callAbort.signal],
            rootId=self.rootId,
            codeVersion=self.codeVersion,
            principal=self.host.principal,
        )

    def close(self) -> None:
        self._closed = True
        self._closing.abort(ChordError("aborted", "ctx expired: its call already settled", {"reason": "expired"}))
        self._release()
        refs, self._refs = self._refs, []
        for ref in refs:
            ref.release()


@dataclass(frozen=True, slots=True)
class LinkedSignal:
    signal: AbortSignal
    release: Callable[[], None]


def linkedSignal(sources: Sequence[AbortSignal]) -> LinkedSignal:
    """A signal aborted with the first of ``sources`` to abort, until ``release()``. Not ``AbortSignal.any``: a
    dependant of a long-lived source (an object's or a code version's) would stay reachable while it has abort
    listeners, so every call or task context would keep its listeners, and the code they close over, alive as long as
    the source."""
    controller = AbortController()
    detach: list[Callable[[], None]] = []

    def release() -> None:
        removers = list(detach)
        detach.clear()
        for remove in removers:
            remove()

    for source in sources:
        if source.aborted:
            release()
            controller.abort(source.reason)
            break

        def forward(source: AbortSignal = source) -> None:
            release()
            controller.abort(source.reason)

        source.addEventListener("abort", forward, once=True)
        detach.append(lambda source=source, forward=forward: source.removeEventListener("abort", forward))
    return LinkedSignal(controller.signal, release)


@eager
async def awaitReady(ref: RefCore, signals: Sequence[AbortSignal], ms: float) -> None:
    """Waits until ``ref`` is ready, at most ``ms`` and until a signal aborts; on failure the reference is released
    (only it: a binding other references share stays) and the error is rethrown: the activation error, ``aborted``,
    or ``unavailable/ready_timeout``."""
    timeout = AbortController()
    timer = (
        setTimeout(
            lambda: timeout.abort(
                ChordError("unavailable", f"{ref.address} was not ready after {_ms(ms)} ms", {"reason": "ready_timeout"})
            ),
            ms,
        )
        if math.isfinite(ms)
        else None
    )
    linked = linkedSignal([*signals, timeout.signal])
    try:
        await raceSignal(ref.ready, linked.signal)
    except GeneratorExit:
        raise
    except BaseException as error:
        ref.release()
        reason = linked.signal.reason
        if isRejection(error) and isinstance(reason, ChordError) and reason.reason == "ready_timeout":
            raise reason
        raise
    finally:
        clearTimeout(timer)
        linked.release()


def _ms(ms: float) -> str:
    """A number of milliseconds as JavaScript prints it."""
    return str(int(ms)) if isinstance(ms, float) and ms.is_integer() else str(ms)


async def raceSignal[T](promise: Awaitable[T], signal: AbortSignal) -> T:
    if signal.aborted:
        raise ChordError("aborted", "call aborted", {"cause": signal.reason})
    aborted = deferred()

    def onAbort() -> None:
        aborted.reject(ChordError("aborted", "call aborted", {"cause": signal.reason}))

    signal.addEventListener("abort", onAbort, once=True)
    try:
        return await race([promise, aborted.promise])
    finally:
        signal.removeEventListener("abort", onAbort)


# ─── References ─────────────────────────────────────────────────────────────────

# Who a reference belongs to. `connection`: a peer host over a link; `client`: a browser (not a reload participant).
type RefOwner = Literal["object", "call", "host", "external", "connection", "client"]


class RefCore(Protocol):
    """A claim on an object: a local ``RefImpl``, or a ``RemoteRef`` on another host (no object here)."""

    @property
    def address(self) -> str: ...

    @property
    def contract(self) -> AnyContract: ...

    @property
    def status(self) -> RefStatus: ...

    @property
    def ready(self) -> asyncio.Future[None]: ...

    @property
    def proxy(self) -> RefProxy: ...

    @property
    def object(self) -> ObjectImpl | None: ...

    def release(self) -> None: ...


REF_BASE: Final[frozenset[str]] = frozenset(
    {
        "contract",
        "status",
        "ready",
        "variant",
        "shape",
        "error",
        "onShape",
        "onStatus",
        "release",
    }
)

type StatusListener = Callable[[RefStatus, Any], None]


def notifyStatus(
    listeners: Mapping[StatusListener, None] | Iterable[StatusListener],
    status: RefStatus,
    error: Any,
    report: Callable[[Any], None],
) -> None:
    """Queues a status notification (after the change that caused it is complete, like every publication)."""
    current = list(listeners)
    if len(current) == 0:
        return

    def deliver() -> None:
        for listener in current:
            try:
                listener(status, error)
            except Exception as thrown:  # noqa: BLE001 - a listener's failure is reported
                report(thrown)

    enqueue(deliver)


class RefProxy:
    """A reference as callers hold it (upstream's ``Proxy``): ``REF_BASE`` names are the reference's own, every other
    name is a member, bound late to the current implementation."""

    __slots__ = ("__weakref__", "_chordRef")

    def __init__(self, target: Any) -> None:
        object.__setattr__(self, "_chordRef", target)

    def __getattr__(self, name: str) -> Any:
        target = object.__getattribute__(self, "_chordRef")
        if name in REF_BASE:
            return getattr(target, name)
        # Symbols and `then` are undefined upstream (a reference is not a thenable); dunders are Python's symbols.
        if name == "then" or (name.startswith("__") and name.endswith("__")):
            raise AttributeError(name)
        return target.member(name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise TypeError(f"cannot set {name} on a reference")

    def __contains__(self, name: Any) -> bool:
        return object.__getattribute__(self, "_chordRef").has(str(name))

    def __repr__(self) -> str:
        target = object.__getattribute__(self, "_chordRef")
        return f"<ref {target.address} {target.status}>"


def refOf(value: Any) -> RefCore | None:
    """Proxy → reference, for ``deps: [ref]`` and links."""
    if isinstance(value, RefProxy):
        return object.__getattribute__(value, "_chordRef")
    return None


class MemberReadable:
    """A reference's state member: a stable readable that follows whatever source the current implementation exposes
    under that name. Reading throws ``not_ready`` until the reference is live, ``not_in_variant`` while the variant
    lacks the member. A listener added before the member is readable gets its snapshot then. Rebinding (reload,
    variant) delivers a reset. Upstream is subscribed only while someone listens."""

    def __init__(self, ref: RefImpl, name: str) -> None:
        self._ref = ref
        self._name = name
        self._values: dict[Callable[[Any], None], None] = {}
        self._events: dict[Callable[[StateEvent[Any]], None], None] = {}
        # Event listeners that have not had their snapshot yet (added while the member was not readable).
        self._unseen: dict[Callable[[StateEvent[Any]], None], None] = {}
        self._source: Readable[Any] | None = None
        self._upstream: Callable[[], Any] = noop
        # The value listeners last saw; a first upstream snapshot equal to it is not re-announced.
        self._seen: tuple[Any] | None = None

    @property
    def value(self) -> Any:
        ref = self._ref
        if ref.status == "released" or ref.status == "gone":
            raise rejection(ref.terminalError())
        if ref.status != "live":
            raise notReady(f"{ref.address}.{self._name}")
        view = ref.currentView()
        if view is None:
            raise notReady(f"{ref.address}.{self._name}")
        source = self._current()
        if source is None:
            raise ChordError(
                "not_in_variant",
                f"{ref.address}: {self._name} is not a state member of variant {variantOf(view) or '(local)'}",
            )
        return source.value

    @property
    def __chord_hydration__(self) -> Hydration | None:
        """Not hydrated while the reference is not live, or while its source (a forwarded remote member) is not."""
        ref = self._ref
        if ref.status == "live":
            return hydrationOf(self._current())

        async def whenHydrated() -> None:
            await ref.ready
            hydration = hydrationOf(self._current())
            if hydration is not None:
                await hydration.whenHydrated()

        return Hydration(False, whenHydrated)

    def subscribe(self, listener: Callable[[Any], None]) -> Callable[[], None]:
        self._values[listener] = None
        source = self._readable()
        if source is not None:
            value = source.value
            self._seen = (value,)
            self._safe(lambda: listener(value))
        self.bind()
        return lambda: self._remove(self._values, listener)

    def events(self, listener: Callable[[StateEvent[Any]], None]) -> Callable[[], None]:
        self._events[listener] = None
        source = self._readable()
        if source is not None:
            value = source.value
            self._seen = (value,)
            self._safe(lambda: listener({"type": "snap", "value": value}))
        else:
            self._unseen[listener] = None
        self.bind()

        def unsubscribe() -> None:
            self._unseen.pop(listener, None)
            self._remove(self._events, listener)

        return unsubscribe

    def bind(self) -> None:
        """(Re)subscribes upstream to the current source; a changed source is announced as a reset."""
        source = self._current() if len(self._values) + len(self._events) > 0 else None
        if source is self._source:
            return
        self._upstream()
        self._upstream = noop
        self._source = source
        if source is None:
            return
        first = [True]

        def onEvent(event: StateEvent[Any]) -> None:
            isFirst = first[0]
            first[0] = False
            if isFirst and len(self._unseen) == 0 and self._seen is not None and strictEqual(event["value"], self._seen[0]):
                return
            self._seen = (event["value"],)
            translated: StateEvent[Any] = {"type": "reset", "value": event["value"]} if isFirst else event
            events = list(self._events)
            values = list(self._values)
            unseen = dict(self._unseen)
            self._unseen.clear()

            # Queued: a rebind during a commit must not run observers before the commit completes.
            def deliver() -> None:
                for listener in events:
                    if listener not in self._events:
                        continue
                    delivered: StateEvent[Any] = (
                        {"type": "snap", "value": event["value"]} if listener in unseen else translated
                    )
                    self._safe(lambda listener=listener, delivered=delivered: listener(delivered))
                for listener in values:
                    if listener in self._values:
                        self._safe(lambda listener=listener: listener(event["value"]))

            enqueue(deliver)

        self._upstream = source.events(onEvent)

    def stop(self) -> None:
        self._values.clear()
        self._events.clear()
        self._unseen.clear()
        self.bind()

    def _current(self) -> Readable[Any] | None:
        view = self._ref.currentView()
        if view is None or not self._ref.inVariant(view, self._name):
            return None
        value = view.get(self._name, UNDEFINED)
        # Cells and derivations, or any readable an `accepts` adapter maps them to.
        return value if isSource(value) or isReadableLike(value) else None

    def _readable(self) -> Readable[Any] | None:
        """The current source if its value can be read now (live, in the variant, hydrated)."""
        source = self._current()
        if source is None:
            return None
        hydration = hydrationOf(source)
        return None if hydration is not None and hydration.hydrated is False else source

    def _remove(self, listeners: dict[Any, None], listener: Any) -> None:
        listeners.pop(listener, None)
        self.bind()

    def _safe(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as error:  # noqa: BLE001 - a listener's failure is reported
            self._ref.object.host.report(error)


class RefImpl:
    """One reference on an object. Exposed through a ``RefProxy`` so members bind late to the current
    implementation."""

    def __init__(self, object: ObjectImpl, contract: AnyContract, owner: RefOwner) -> None:
        self.object = object
        self.contract = contract
        self.owner = owner
        self.address = object.address
        self.status: RefStatus = "live" if object.status == "live" else "loading"
        self.error: Any = None
        self.shapeListeners: dict[Callable[[Shape], None], None] = {}
        # Internal: notified when the reference becomes gone (links forward it).
        self.goneListeners: dict[Callable[[Any], None], None] = {}
        self.readables: dict[str, MemberReadable] = {}
        # Weak: a retired code version's service (and the factory code it holds) must not be kept alive by its
        # consumers.
        self.bindings: weakref.WeakKeyDictionary[Service, Adapter | None] = weakref.WeakKeyDictionary()
        # MISAKA fork: upstream's `views` WeakMap (implementation -> view) is `Installed.views` (see the module
        # docstring).
        self.callers: dict[str, Callable[..., Any]] = {}
        self._statusListeners: dict[StatusListener, None] = {}

        async def ready(activation: asyncio.Future[None]) -> None:
            # `object.ready.then(...)`: runs after the code that made this reference, also on a settled activation. The
            # activation is shared by every reference to the object: a waiter that gives up must not cancel it.
            await asyncio.wait([activation])
            activation.result()
            if self.status == "gone":
                raise rejection(self.error)
            if self.status == "released":
                raise ChordError("released", f"reference {self.address} released")

        self._ready: asyncio.Future[None] = quiet(start(ready(object.ready)))
        self.proxy = RefProxy(self)

    @property
    def ready(self) -> asyncio.Future[None]:
        """Settles once the reference is live (or rejects with why it is not). Every reader shares this promise: a
        waiter that gives up (``wait_for``) cancels its own view of it only (``exposed``), as a JavaScript promise
        cannot be cancelled."""
        return exposed(self._ready)

    @property
    def variant(self) -> str | None:
        view = self.currentView()
        return None if view is None else variantOf(view)

    @property
    def shape(self) -> Shape | None:
        installed = self.object.currentInstalled() if self.status == "live" else None
        view = None if installed is None else self._safeView(installed)
        if installed is None or view is None:
            return None
        return {
            "variant": variantOf(view),
            "members": [key for key in view if key != "variant"],
            "contract": installed.service.contract.fingerprint,
        }

    def onShape(self, listener: Callable[[Shape], None]) -> Callable[[], Any]:
        self.shapeListeners[listener] = None
        return lambda: self.shapeListeners.pop(listener, UNDEFINED) is not UNDEFINED

    def onStatus(self, listener: StatusListener) -> Callable[[], Any]:
        self._statusListeners[listener] = None
        return lambda: self._statusListeners.pop(listener, UNDEFINED) is not UNDEFINED

    def _statusChanged(self) -> None:
        notifyStatus(self._statusListeners, self.status, self.error, lambda error: self.object.host.report(error))

    def release(self) -> None:
        if self.status == "released" or self.status == "gone":
            return
        self.status = "released"
        self._stopReadables()
        self._statusChanged()
        self.object.release(self)

    def has(self, name: str) -> bool:
        """The proxy's ``has`` trap: a member whose value is ``None`` (a JSON null opaque) is there."""
        return name in REF_BASE or self._member(name) is not UNDEFINED

    # ─── internal ───

    def live(self) -> None:
        if self.status != "loading":
            return
        self.status = "live"
        self._statusChanged()
        self.shapeChanged()

    def gone(self, error: Any) -> None:
        if self.status == "released" or self.status == "gone":
            return
        self.status = "gone"
        self.error = error
        self._stopReadables()
        self._statusChanged()
        for listener in list(self.goneListeners):
            listener(error)

    def shapeChanged(self) -> None:
        for readable in list(self.readables.values()):
            readable.bind()
        shape = self.shape
        if shape is None:
            return
        listeners = list(self.shapeListeners)

        def deliver() -> None:
            for listener in listeners:
                try:
                    listener(shape)
                except Exception as error:  # noqa: BLE001 - a listener's failure is reported
                    self.object.host.report(error)

        enqueue(deliver)

    def terminalError(self) -> Any:
        return self.error if self.status == "gone" else ChordError("released", f"reference {self.address} released")

    def viewOf(self, installed: Installed) -> dict[str, Any]:
        """The implementation as this consumer sees it (through an accepts adapter when bound to another version)."""
        if installed.service in self.bindings:
            adapter = self.bindings[installed.service]
        else:
            adapter = bind(installed.service.contract, installed.service.options.get("accepts"), self.contract)
            self.bindings[installed.service] = adapter
        if adapter is None:
            return installed.implementation
        view = installed.views.get(self)
        if view is None:
            view = adapter(installed.implementation)
            installed.views[self] = view
        return view

    def currentView(self) -> dict[str, Any] | None:
        if self.status != "live":
            return None
        installed = self.object.currentInstalled()
        return None if installed is None else self._safeView(installed)

    def _safeView(self, installed: Installed) -> dict[str, Any] | None:
        """The view, or ``None`` when this consumer no longer binds (reported; calls reject with the bind error)."""
        try:
            return self.viewOf(installed)
        except Exception as error:  # noqa: BLE001 - reported; calls reject with the bind error
            self.object.host.report(error)
            return None

    def describe(self, variant: str | None, member: str) -> MemberDescription | None:
        variants = self.contract.description["variants"]
        if variant is not None:
            members = variants.get(variant)
            return None if members is None else members.get(member)
        for members in variants.values():
            if members.get(member) is not None:
                return members[member]
        return None

    def inVariant(self, view: Mapping[str, Any], name: str) -> bool:
        """Remote consumers see only the members their contract lists for the current variant."""
        return self.contract.scope != "remote" or self.describe(variantOf(view), name) is not None

    def member(self, name: str) -> Any:
        """MISAKA fork: a member that is not there reads ``None`` (a Python attribute read has no ``undefined``);
        ``name in ref`` (``has``) tells it from a member whose value is ``None``."""
        value = self._member(name)
        return None if value is UNDEFINED else value

    def _member(self, name: str) -> Any:
        """Upstream's ``member``: ``UNDEFINED`` when the reference has no such member."""
        declared = self.describe(None, name)
        view = self.currentView()
        value = UNDEFINED if view is None else view.get(name, UNDEFINED)
        if (declared is not None and declared["kind"] == "method") or (
            declared is None and callable(value) and not isSource(value) and not isReadableLike(value)
        ):
            return self.caller(name)
        if (declared is not None and declared["kind"] == "state") or (
            declared is None and (isSource(value) or isReadableLike(value))
        ):
            readable = self.readables.get(name)
            if readable is None:
                readable = MemberReadable(self, name)
                self.readables[name] = readable
            return readable
        if view is None or not self.inVariant(view, name):
            return UNDEFINED
        if isinstance(value, ExposedImpl):
            return value.resource.value
        return value

    def caller(self, name: str) -> Callable[..., Any]:
        caller = self.callers.get(name)
        if caller is None:
            noArgs = takesNoArgs(self.contract, name)

            def caller(*given: Any) -> asyncio.Future[Any]:
                # A method without arguments is called as `ref.name(ctx)`.
                if noArgs:
                    args, ctx = UNDEFINED, given[0] if len(given) > 0 else None
                else:
                    args = given[0] if len(given) > 0 else UNDEFINED
                    ctx = given[1] if len(given) > 1 else None
                host = self.object.host
                if host.inFactory > 0:
                    raise ChordError("not_ready", f"{self.address}.{name} called inside a factory")
                if ctx is not None and not isinstance(ctx, CtxImpl):
                    return rejected(invalid("foreign_ctx", "ctx must come from a Chord host"))
                if ctx is None or ctx.holder:
                    # A code holder (task) starts a fresh root per call, cancelled with the holder.
                    try:
                        if ctx is not None:
                            ctx.check(host)
                    except Exception as error:  # noqa: BLE001 - a rejected call, as upstream
                        return rejected(error)
                    return host.root(
                        lambda root: self.object.call(self, name, args, root),
                        {"signal": None if ctx is None else ctx.signal},
                    )
                return self.object.call(self, name, args, ctx)

            self.callers[name] = caller
        return caller

    def _stopReadables(self) -> None:
        for readable in list(self.readables.values()):
            readable.stop()


def variantOf(implementation: Mapping[str, Any]) -> str | None:
    variant = implementation.get("variant")
    return variant if isinstance(variant, str) else None


# ─── Objects ─────────────────────────────────────────────────────────────────

type ObjectStatus = Literal["activating", "live", "failed", "disposing", "disposed"]


@dataclass
class Gate:
    grandfathered: dict[str, None]
    queue: list[tuple[str, Any]]
    drained: Callable[[], None]


@dataclass
class Subscription:
    # What the listener was declared on: the resource (not its facade), or the source itself.
    origin: Any
    select: Callable[[Any], Subscribable[Any]] | None
    unsubscribe: Callable[[], Any]


def sameSelect(a: Any, b: Any) -> bool:
    """Two ``select`` functions are the same if they are identical or have the same code: an inline lambda is a new
    object on every factory run, but its code is what matters. (A select whose implementation depends on captured
    values that change between runs is not detected; such a listener keeps its old subscription until its source
    changes.)"""
    if a is b:
        return True
    if a is None or b is None:
        return False
    # MISAKA fork: equal code objects stand in for upstream's equal source text (`String(fn)`).
    codeA = getattr(a, "__code__", None)
    codeB = getattr(b, "__code__", None)
    return codeA is not None and codeA == codeB


class StagedListeners(Protocol):
    """Listener subscriptions made ahead of a commit: ``activate`` switches over, ``discard`` drops them."""

    def activate(self) -> None: ...

    def discard(self) -> None: ...


@dataclass(frozen=True)
class _Staged:
    activate: Callable[[], None]
    discard: Callable[[], None]


@dataclass(frozen=True)
class RunningTask:
    controller: AbortController
    done: asyncio.Future[None]


@dataclass(frozen=True)
class ObjectIdentity:
    address: str
    typeName: str
    input: Any
    principal: Any
    # The one object of a `lifetime: "host"` type.
    hostLifetime: bool


@dataclass
class _Fresh:
    origin: Any
    select: Any
    unsubscribe: Callable[[], Any]
    buffer: list[Any] | None
    subscribe: Callable[[], None] | None = None


class ObjectImpl:
    def __init__(self, host: HostCore, identity: ObjectIdentity) -> None:
        self.host = host
        self.address = identity.address
        self.typeName = identity.typeName
        self.input = identity.input
        self.principal = identity.principal
        # The one object of a `lifetime: "host"` type: only such objects may register.
        self.hostLifetime = identity.hostLifetime
        self.status: ObjectStatus = "activating"
        # MISAKA fork: `Promise.resolve()` is a future of the running loop (``_promise``): objects are made inside one,
        # as `start`, which replaces it at once, needs one too.
        self.ready: asyncio.Future[None] = resolved()
        self.service: Service | None = None
        # Calls carrying an older code version are rejected upgrade_conflict (state bump or exclusive resource
        # replaced).
        self.minCodeVersion = 0
        self.decls = Decls()
        # Declarations of a factory run that has not been installed yet (activation or reload candidate).
        self.pending: dict[Decls, None] = {}
        self.implementations: dict[int, Installed] = {}
        self.refs: dict[RefImpl, None] = {}
        self.leases: dict[str, int] = {}
        self.openOrder: list[ResourceImpl] = []
        self.callAbort = AbortController()
        self.activationAbort = AbortController()
        self.gate: Gate | None = None
        # Served only by a running reload's pending code version: listeners, tasks and registrations start at its
        # commit.
        self.deferredRuntime = False
        self._subscriptions: dict[str, Subscription] = {}
        self._invocations: dict[asyncio.Future[Any], None] = {}
        self._tasks: dict[str, RunningTask] = {}
        # Aborted task runs that have not finished yet.
        self._stopped: dict[asyncio.Future[None], None] = {}
        # Bumped by every start and stop of the tasks: a delayed start that is no longer the latest is dropped.
        self._taskRuns = 0
        self._linger: Any = None
        self._disposal: asyncio.Future[None] | None = None

    def start(self, after: asyncio.Future[None] | None) -> None:
        async def activate() -> None:
            try:
                await self._activate(after)
            except GeneratorExit:
                raise
            except BaseException as cause:  # every activation failure ends the references (`.then(_, onRejected)`)
                if self.status == "activating":
                    self.status = "failed"
                self.host.forget(self)
                # A ChordError (forbidden, a dependency's error, ...) keeps its code; anything else is `unavailable`.
                error = asChordError(cause, f"activating {self.address}")
                for ref in list(self.refs):
                    ref.gone(error)
                if not isRejection(cause):
                    raise
                raise error
            for ref in list(self.refs):
                ref.live()
            self._maybeIdle()

        self.ready = quiet(start(activate()))

    async def _activate(self, after: asyncio.Future[None] | None) -> None:
        """Activation as one transaction: factory, opens, then a synchronous finalization (registry check and
        publication, implementations, runtime). Every failure, including one during finalization or a release while
        listeners start, unwinds everything this activation did."""
        if after is not None and not after.done():
            # `after?.catch(noop)`: only its settling matters.
            await _settled(after)
        else:
            # `await` of `undefined` or of a settled promise still yields: the factory never runs inside the code that
            # created the object (a factory's own `self.get` of another type, for one).
            await asyncio.sleep(0)
        signal = self.activationAbort.signal

        def abandoned() -> ChordError:
            return ChordError("aborted", f"{self.address} abandoned")

        if signal.aborted:
            raise abandoned()
        service = self.host.service(self.typeName)
        if service is None:
            raise ChordError("unavailable", f"{self.typeName} is not served", {"reason": "unknown_type"})
        self.service = service
        run = self.run(service, None)
        try:
            await self.open(run.created.resources, run.created.refs, signal)
            if signal.aborted:
                raise abandoned()
            await self.waitHydrated(run)
            if signal.aborted:
                raise abandoned()
            await self.initCells(run, signal)
            if signal.aborted:
                raise abandoned()
            self.host.checkRegistrations(self, run.decls)
            self.decls = run.decls
            views: weakref.WeakKeyDictionary[RefImpl, dict[str, Any]] = weakref.WeakKeyDictionary()
            for retained, services in self.host.retainedCodeVersions():
                if services.get(self.typeName) is service:
                    self.implementations[retained] = Installed(retained, service, run.implementation, run.decls, views)
            pending = self.host.pendingCodeVersion()
            if self.host.currentCodeVersion() not in self.implementations and (
                pending is None or pending not in self.implementations
            ):
                raise ChordError("superseded", f"{self.address}: code changed during activation")
            for derived in run.created.derives:
                derived.attach()
            self.refreshSchemas()
            # Served only by a running reload's pending code version: new code must not run (listeners, tasks,
            # registrations) before that reload commits.
            self.deferredRuntime = self.host.currentCodeVersion() not in self.implementations
            if not self.deferredRuntime:
                if len(run.decls.registrations) > 0:
                    self.host.registriesChanged(self)
                self.startRuntime()
            # A listener starting up may have released the last reference synchronously: no resurrection.
            if signal.aborted:
                raise abandoned()
            self.status = "live"
        except GeneratorExit:
            raise
        except BaseException as error:
            # Invocations started during finalization (listener roots) end before their resources close.
            self.callAbort.abort(error)
            self.stopListeners()
            self.stopTasks()
            registered = len(self.decls.registrations) > 0 and not self.deferredRuntime
            self.implementations.clear()
            self.decls = Decls()
            if registered:
                self.host.registriesChanged()
            await allSettled([*self._invocations, *self._stopped])
            try:
                await self.unwind(run)
            except GeneratorExit:
                raise
            except BaseException as cleanup:  # reported; the activation error is what callers see
                if not isRejection(cleanup):
                    raise
                self.host.report(cleanup)
            raise
        finally:
            self.pending.pop(run.decls, None)

    def run(self, service: Service, previous: Decls | None) -> Run:
        """Runs the factory synchronously. With ``previous``, runs in recording mode for a reload prepare. The run's
        declarations stay in ``pending`` (visible to the cycle check) until the caller installs or discards them."""
        self_ = SelfImpl(self, previous)

        def discard(run: Run) -> None:
            self.pending.pop(run.decls, None)
            background(self.unwind(run), self.host.report)

        self.pending[self_.run(None).decls] = None
        self.host.inFactory += 1
        try:
            implementation = service.factory(self.input, self.principal, self_)
        except BaseException:
            self.host.inFactory -= 1
            discard(self_.run(None))
            raise
        self.host.inFactory -= 1
        run = self_.run(implementation)
        try:
            if inspect.isawaitable(implementation):
                if inspect.iscoroutine(implementation):
                    implementation.close()
                raise invalid("implementation", f"{self.typeName}: factories must be synchronous")
            validateImplementation(service, run.implementation)
        except BaseException:
            discard(run)
            raise
        return run

    @eager
    async def open(self, resources: Sequence[ResourceImpl], refs: Sequence[RefCore], signal: AbortSignal) -> None:
        """Opens resources (dependencies first) and waits for references; settles all before failing."""
        opening: dict[ResourceImpl, asyncio.Future[None]] = {}

        def openOne(resource: ResourceImpl) -> asyncio.Future[None]:
            promise = opening.get(resource)
            if promise is None:

                async def opened() -> None:
                    await promiseAll(
                        [
                            (dep.ready if not isinstance(dep, ResourceImpl) else openOne(dep) if dep in resources else None)
                            for dep in resource.deps
                        ]
                    )
                    await resource.open(signal, self.openOrder)

                promise = start(opened())
                opening[resource] = promise
            return promise

        ready = self.withinReadyTimeout(
            promiseAll([ref.ready for ref in refs]),
            lambda: [ref.address for ref in refs if ref.status == "loading"],
        )
        results = await allSettled([*[openOne(resource) for resource in resources], ready])
        for result in results:
            if result.status == "rejected":
                raise rejection(result.reason)

    @eager
    async def waitHydrated(self, run: Run) -> None:
        """Waits until the state sources a run exposes or derives from have their values: a local object goes live only
        with its state, also where it forwards or derives from a remote reference's member."""
        exposed = list((run.implementation or {}).items())
        pending = unhydrated([*run.created.derives, *[value for _, value in exposed]])
        if len(pending) == 0:
            return

        def waiting() -> list[str]:
            names = [f"{self.address}.{name}" for name, value in exposed if _unhydrated(value)]
            names += [f"derive {derived.key}" for derived in run.created.derives if hydrationOf(derived) is not None]
            return names

        await self.withinReadyTimeout(promiseAll([hydration.whenHydrated() for hydration in pending]), waiting)

    @eager
    async def withinReadyTimeout[T](self, work: Awaitable[T], waiting: Callable[[], Sequence[str]]) -> T:
        """``work`` within the host's ``readyTimeout``; a timeout names what is still awaited."""
        ms = self.host.readyTimeout
        if not math.isfinite(ms):
            return await work
        timeout = deferred()

        def expire() -> None:
            names = waiting()
            listed = "" if len(names) == 0 else f", waiting on {', '.join(names)}"
            timeout.reject(
                ChordError(
                    "unavailable",
                    f"{self.address} was not ready after {_ms(ms)} ms{listed} (a reference cycle across hosts?)",
                    {"reason": "ready_timeout"},
                )
            )

        timer = setTimeout(expire, ms)
        try:
            return await race([work, timeout.promise])
        finally:
            clearTimeout(timer)

    @eager
    async def initCells(self, run: Run, signal: AbortSignal) -> None:
        """Fills the cells ``run`` created with ``init``, each in a root of its own, in parallel; settles all before
        failing. Runs after the run's resources are open and its references live, before anything reads the cells."""
        if len(run.created.inits) == 0:
            return

        async def initOne(cell: Cell[Any], init: InitFn) -> None:
            value = await self.host.root(lambda ctx: init(ctx), {"signal": signal})
            if not signal.aborted:
                cell.initialize(value)

        results = await allSettled([start(initOne(cell, init)) for cell, init in run.created.inits])
        for result in results:
            if result.status == "rejected":
                raise rejection(result.reason)

    @eager
    async def unwind(self, run: Run) -> None:
        """Undoes the objects a run created: closes its resources (reverse order) and releases its references."""
        for derived in run.created.derives:
            derived.detach()
        resources = sorted(run.created.resources, key=lambda resource: -_index(self.openOrder, resource))
        await runAll(
            [
                *[(lambda resource=resource: resource.close(self.openOrder)) for resource in resources],
                *[(lambda ref=ref: ref.release()) for ref in run.created.refs],
            ],
            f"{self.address}: cleanup failed",
        )

    def currentInstalled(self) -> Installed | None:
        installed = self.implementations.get(self.host.currentCodeVersion())
        if installed is not None:
            return installed
        values = list(self.implementations.values())
        return values[-1] if values else None

    def refreshSchemas(self) -> None:
        """Every state source exposed as a remote state member gets that member's schema (checked per write in dev
        mode)."""
        schemas: dict[Any, list[TSchema]] = {}
        for installed in self.implementations.values():
            for source, entries in memberSchemas(installed.service, installed.implementation).items():
                schemas[source] = [*schemas.get(source, []), *entries]
        for decls in self.allDecls():
            for source in [*decls.cells.values(), *decls.derives.values()]:
                source.schemas = schemas.get(source, [])

    def allDecls(self) -> dict[Decls, None]:
        result: dict[Decls, None] = {self.decls: None}
        for decls in self.pending:
            result[decls] = None
        for installed in self.implementations.values():
            result[installed.decls] = None
        return result

    # ─── runtime: listeners and tasks of the current declarations ───

    def _origin(self, decl: ListenerDecl) -> Any:
        """The resource a listener is declared on, or its source itself."""
        resource = resourceOf(decl.source)
        return decl.source if resource is None else resource

    def _listenSource(self, key: str, decl: ListenerDecl) -> Subscribable[Any]:
        """Resolves a listener's subscribable: ``select(resource value)``, ``select(source)`` or the source itself."""
        resource = resourceOf(decl.source)
        value = decl.source if resource is None else resource.value
        source = value if decl.select is None else decl.select(value)
        if not callable(getattr(source, "subscribe", None)):
            raise invalid("implementation", f"listener {key}: source has no subscribe()")
        return source

    def stageListeners(self, decls: Decls) -> StagedListeners:
        """Subscribes the listeners of ``decls`` without delivering: events are buffered until ``activate``. A listener
        declared on the same resource (or source) with the same ``select`` keeps its subscription without calling
        ``select`` again (only its function is swapped, by name), so sources that accept a single subscriber survive
        reloads. Throws, with nothing left subscribed, if any source fails."""
        fresh: dict[str, _Fresh] = {}
        kept: set[str] = set()
        try:
            for key, decl in decls.listeners.items():
                origin = self._origin(decl)
                previous = self._subscriptions.get(key)
                if previous is not None and previous.origin is origin and sameSelect(previous.select, decl.select):
                    kept.add(key)
                    continue
                source = self._listenSource(key, decl)
                entry = _Fresh(origin, decl.select, noop, [])
                fresh[key] = entry
                # State (a readable) is subscribed when the listener activates: it delivers the value current then, not
                # one of the candidate before the commit (a new derivation computes its value at the commit).
                if isSource(source) or isReadableLike(source):
                    entry.buffer = None

                    def subscribe(entry: _Fresh = entry, key: str = key, source: Any = source) -> None:
                        entry.unsubscribe = source.subscribe(lambda event: self._deliver(key, event))

                    entry.subscribe = subscribe
                    continue

                def onEvent(event: Any, entry: _Fresh = entry, key: str = key) -> None:
                    if entry.buffer is not None:
                        entry.buffer.append(event)
                    else:
                        self._deliver(key, event)

                entry.unsubscribe = source.subscribe(onEvent)
        except BaseException:
            for entry in fresh.values():
                entry.unsubscribe()
            raise

        def activate() -> None:
            for key, subscription in list(self._subscriptions.items()):
                if key in kept:
                    continue
                subscription.unsubscribe()
                del self._subscriptions[key]
            for key, entry in fresh.items():
                self._subscriptions[key] = Subscription(entry.origin, entry.select, lambda entry=entry: entry.unsubscribe())
                if entry.subscribe is not None:
                    entry.subscribe()
                buffered = entry.buffer or []
                entry.buffer = None
                for event in buffered:
                    self._deliver(key, event)

        def discard() -> None:
            for entry in fresh.values():
                entry.unsubscribe()

        return _Staged(activate, discard)

    def startRuntime(self) -> None:
        self.stageListeners(self.decls).activate()
        self.startTasks()

    def startTasks(self) -> None:
        """Starts the tasks of the current declarations. Runs aborted by ``stopTasks`` (a reload, a gate) may still be
        finishing: their replacements start once they all finished, or after ``taskStopDeadline`` (reported), so the
        old and the new run of a task never work side by side. A later start or stop, or the object leaving ``live``,
        cancels a delayed start."""
        self._taskRuns += 1
        run = self._taskRuns

        def startAll() -> None:
            for key in list(self.decls.tasks):
                self._startTask(key, self.host.currentCodeVersion())

        if len(self._stopped) == 0:
            startAll()
            return
        deadline = self.host.taskStopDeadline
        late = deferred()
        timer = setTimeout(lambda: late.resolve(True), deadline)
        stopped = list(self._stopped)

        async def stoppedAll() -> bool:
            await allSettled(stopped)
            return False

        async def delayed() -> None:
            missed = await race([start(stoppedAll()), late.promise])
            clearTimeout(timer)
            if run != self._taskRuns or self.status != "live":
                return
            if missed:
                self.host.report(
                    ChordError(
                        "aborted",
                        f"{self.address}: aborted tasks still running after {_ms(deadline)} ms; starting their "
                        "replacements anyway",
                    )
                )
            startAll()

        background(delayed())

    def stopListeners(self) -> None:
        for subscription in list(self._subscriptions.values()):
            subscription.unsubscribe()
        self._subscriptions.clear()

    def _startTask(self, key: str, codeVersion: int) -> None:
        fn = self.decls.tasks.get(key)
        if fn is None:
            return
        controller = AbortController()
        ctx = CtxImpl(
            self.host,
            signals=[controller.signal, self.callAbort.signal],
            rootId=f"task:{self.address}:{key}",
            codeVersion=codeVersion,
            principal=self.host.principal,
            holder=True,
        )

        async def run() -> None:
            try:
                result = fn(ctx)
                try:
                    if inspect.isawaitable(result):
                        await result
                finally:
                    # `await fn(ctx)` yields even when `fn` returned a value or a settled promise: the task's end never
                    # runs before it is registered below (a synchronous throw does, as upstream's does).
                    await asyncio.sleep(0)
            except GeneratorExit:
                raise
            except BaseException as error:  # a task's failure is reported unless it was stopped
                if not isRejection(error):
                    raise
                if not ctx.signal.aborted:
                    self.host.report(error)
            finally:
                ctx.close()
                task = self._tasks.get(key)
                if task is not None and task.controller is controller:
                    del self._tasks[key]

        done = start(run())
        self._tasks[key] = RunningTask(controller, done)

    def stopTasks(self) -> None:
        """Aborts running tasks (and cancels a delayed start). Their completion is awaited by the next ``startTasks``
        and at disposal; each leaves ``_stopped`` when it finishes."""
        self._taskRuns += 1
        for task in list(self._tasks.values()):
            task.controller.abort(ChordError("aborted", "task stopped"))
            done = task.done
            self._stopped[done] = None
            done.add_done_callback(lambda done: self._stopped.pop(done, None))
        self._tasks.clear()

    def _deliver(self, key: str, event: Any) -> None:
        """Delivers one listener event, in publication order. The invocation is a root that references a lease on this
        object (a reload gate waits for it like for any other root that touched the object), counts as an invocation
        (disposal waits for it) and is cancelled by forced disposal."""

        def deliver() -> None:
            if self.gate is not None:
                self.gate.queue.append((key, event))
                return
            decl = self.decls.listeners.get(key)
            if decl is None or (self.status != "live" and self.status != "activating"):
                return

            def inRoot(root: Ctx) -> Any:
                self.lease(root)
                child = root.child(self)  # type: ignore[attr-defined]

                async def handle() -> None:
                    try:
                        result = decl.fn(event, child)
                        if inspect.isawaitable(result):
                            await result
                    finally:
                        child.close()

                return self._invoke(handle)

            invocation = self.host.root(inRoot)
            invocation.add_done_callback(lambda done: _reportFailure(done, self.host.report))

        enqueue(deliver)

    def openGate(self) -> None:
        """Opens the object's gate and replays queued listener events through the current listeners."""
        gate = self.gate
        self.gate = None
        for key, event in [] if gate is None else gate.queue:
            self._deliver(key, event)

    # ─── calls ───

    def admit(self, ctx: Ctx) -> None:
        """Admission for a call or write carrying ``ctx``."""
        if self.host.failed is not None:
            raise ChordError("unavailable", f"{self.host.name} failed and admits no work", {"reason": "host_failed"})
        if not self.host.hasCodeVersion(ctx.codeVersion):
            raise ChordError("superseded", f"code version {ctx.codeVersion} is retired")
        if ctx.codeVersion < self.minCodeVersion:
            raise ChordError("upgrade_conflict", f"{self.address} was upgraded in code version {self.minCodeVersion}")
        if self.gate is not None and ctx.rootId not in self.gate.grandfathered:
            raise ChordError("upgrade_conflict", f"{self.address} is being upgraded")

    def lease(self, ctx: Ctx) -> None:
        """Records that the root of ``ctx`` touched this object (a reload gate grandfathers it)."""
        if ctx.rootId in self.leases:
            return
        self.leases[ctx.rootId] = ctx.codeVersion
        self.host.touch(ctx.rootId, self)

    def guard(self, ctx: Ctx) -> None:
        if self.host.inFactory > 0:
            raise invalid("read_only", "cells cannot be written while a factory runs")
        if isinstance(ctx, CtxImpl):
            ctx.check(self.host)
        if ctx.signal.aborted:
            raise ChordError("aborted", "write with an aborted ctx")
        if self.status == "disposed":
            raise ChordError("gone", f"{self.address} is disposed")
        if ctx.rootId.startswith("task:"):
            return
        self.admit(ctx)

    @eager
    async def call(self, ref: RefImpl, name: str, args: Any, ctx: CtxImpl) -> Any:
        ctx.check(self.host)
        if ref.status == "released" or ref.status == "gone":
            raise rejection(ref.terminalError())
        if ref.status == "loading":
            await raceSignal(ref.ready, ctx.signal)
        if ref.status != "live" or self.status != "live":
            if ref.status == "live":
                raise ChordError("gone", f"{self.address} is {self.status}")
            raise rejection(ref.terminalError())
        self.admit(ctx)
        installed = self.implementations.get(ctx.codeVersion)
        if installed is None:
            raise ChordError("superseded", f"{self.address} has no implementation for code version {ctx.codeVersion}")
        view = ref.viewOf(installed)
        variant = variantOf(view)
        wanted = ref.describe(variant if ref.contract.scope == "remote" else LOCAL_VARIANT, name)
        fn = view.get(name, UNDEFINED)
        if not callable(fn) or (ref.contract.scope == "remote" and (wanted is None or wanted["kind"] != "method")):
            raise ChordError("not_in_variant", f"{self.address}: {name} is not a method of variant {variant or '(local)'}")
        direct = view is installed.implementation
        if direct:
            members = installed.service.contract.description["variants"].get(variant if variant is not None else LOCAL_VARIANT)
            schema = None if members is None else members.get(name)
        else:
            schema = wanted
        method = schema if schema is not None and schema["kind"] == "method" else None
        validated = method if ref.contract.scope == "remote" else None
        # No arguments: the handler takes only ctx. No result (void): the caller gets undefined, whatever it returned.
        takesArgs = method is None or "args" in method
        returns = method is None or "result" in method
        input: Any = UNDEFINED
        if takesArgs:
            # The additive rule assumes a consumer sends only what its own schema knows, and a provider sees only what
            # its schema knows: strip with both before validating.
            own = wanted.get("args") if ref.contract.scope == "remote" and wanted is not None and wanted["kind"] == "method" else None
            sent = args if own is None else Value.Clean(own, Value.Clone(args))
            checked = None if validated is None else validated.get("args")
            input = sent if checked is None else Value.Clean(checked, Value.Clone(sent))
            if checked is not None:
                assertValid("args", f"{self.address}.{name} args", checked, input)
        self.lease(ctx)
        child = ctx.child(self)
        try:
            result = await self._invoke(lambda: fn(input, child) if takesArgs else fn(child))
        finally:
            child.close()
        if not returns:
            return UNDEFINED
        if validated is not None and validated.get("result") is not None:
            assertValid("result", f"{self.address}.{name} result", validated["result"], result)
        return result

    def _invoke[T](self, fn: Callable[[], Any]) -> asyncio.Future[Any]:
        """Runs an invocation (method or listener) that keeps the object alive until it settles."""

        async def invoke() -> Any:
            result = fn()
            # MISAKA fork: a promise resolved with a promise adopts it; an awaited coroutine may return a future.
            while inspect.isawaitable(result):
                result = await result
            return result

        invocation = start(invoke())
        self._invocations[invocation] = None

        def done(_: asyncio.Future[Any]) -> None:
            self._invocations.pop(invocation, None)
            if len(self._invocations) == 0:
                self._maybeIdle()

        invocation.add_done_callback(done)
        return invocation

    def rootEnded(self, rootId: str) -> None:
        self.leases.pop(rootId, None)
        gate = self.gate
        if gate is not None and gate.grandfathered.pop(rootId, UNDEFINED) is not UNDEFINED and len(gate.grandfathered) == 0:
            gate.drained()

    # ─── lifetime ───

    def addRef(self, ref: RefImpl) -> None:
        self.refs[ref] = None
        if self._linger is not None:
            clearTimeout(self._linger)
        self._linger = None

    def release(self, ref: RefImpl) -> None:
        if self.refs.pop(ref, UNDEFINED) is not UNDEFINED:
            self._maybeIdle()

    def _maybeIdle(self) -> None:
        """No references and no invocations: dispose (after ``linger`` for a live object)."""
        if len(self.refs) > 0 or len(self._invocations) > 0 or self._linger is not None:
            return
        if self.status == "activating":
            self._background(self.dispose(False))
            return
        if self.status != "live":
            return
        linger = (self.service.options.get("linger") if self.service is not None else None) or 0
        if linger <= 0:
            self._background(self.dispose(False))
            return

        def expire() -> None:
            self._linger = None
            if len(self.refs) == 0 and len(self._invocations) == 0 and self.status == "live":
                self._background(self.dispose(False))

        self._linger = setTimeout(expire, linger)

    def _background(self, disposal: asyncio.Future[None]) -> None:
        disposal.add_done_callback(lambda done: _reportFailure(done, self.host.report))

    def dispose(self, forced: bool, reason: Any = UNDEFINED) -> asyncio.Future[None]:
        """Disposes the object. Forced: references become gone at once (new calls are rejected) and running invocations
        are cancelled. Either way, resources close only after every invocation settled."""
        if self._disposal is None:
            self._disposal = quiet(start(self._dispose(forced, reason)))
        return self._disposal

    async def _dispose(self, forced: bool, reason: Any) -> None:
        wasActivating = self.status == "activating"
        self.status = "disposing"
        self.host.forget(self)
        if self._linger is not None:
            clearTimeout(self._linger)
        self._linger = None
        error = ChordError("gone", f"{self.address} was disposed") if reason is UNDEFINED or reason is None else reason
        if forced:
            for ref in list(self.refs):
                ref.gone(error)
            self.callAbort.abort(error)
        self.activationAbort.abort(error)
        if wasActivating:
            # `ready.catch(noop)`: the activation's failure is its own.
            await _settled(self.ready)
        for ref in list(self.refs):
            ref.gone(error)
        decls = self.allDecls()
        self.stopTasks()
        self.stopListeners()
        await allSettled(list(self._invocations))
        registered = any(len(entry.registrations) > 0 for entry in decls) and not self.deferredRuntime
        self.implementations.clear()
        self.decls = Decls()
        if registered:
            self.host.registriesChanged()
        refs: dict[Any, None] = {}
        for entry in decls:
            for derived in entry.derives.values():
                derived.detach()
            for ref in entry.refs.values():
                refs[ref] = None
        try:
            stopped = list(self._stopped)
            await runAll(
                [
                    lambda: allSettled(stopped),
                    *[(lambda resource=resource: resource.close(self.openOrder)) for resource in reversed(list(self.openOrder))],
                    *[(lambda ref=ref: ref.release()) for ref in refs],
                ],
                f"{self.address}: disposal failed",
            )
        finally:
            self.status = "disposed"
            self.host.disposed(self)


def _index(items: list[Any], item: Any) -> int:
    """``Array.prototype.indexOf``."""
    for index, candidate in enumerate(items):
        if candidate is item:
            return index
    return -1


def _unhydrated(value: Any) -> bool:
    hydration = hydrationOf(value)
    return hydration is not None and hydration.hydrated is False


def _reportFailure(done: asyncio.Future[Any], report: Callable[[Any], None]) -> None:
    """``promise.catch(report)``."""
    if done.cancelled():
        return
    error = done.exception()
    if error is not None:
        report(error)


def isReadableLike(value: Any) -> bool:
    """An object with ``events`` and ``subscribe`` methods, a reference among them when its contract has both (its
    proxy answers them with callers, as upstream's does). MISAKA fork: a ``dict``'s keys are not its attributes, so a
    mapping (a JavaScript object literal's counterpart) is never readable-like."""
    if value is None or isinstance(value, str | int | float | bool | dict | list):
        return False
    return callable(getattr(value, "events", None)) and callable(getattr(value, "subscribe", None))


def memberSchemas(service: Service, implementation: Mapping[str, Any]) -> dict[Any, list[TSchema]]:
    """The remote state-member schemas an implementation exposes, by source."""
    schemas: dict[Any, list[TSchema]] = {}
    contract = service.contract
    if contract.scope != "remote":
        return schemas
    members = contract.description["variants"].get(variantOf(implementation) or "") or {}
    for name, description in members.items():
        source = implementation.get(name, UNDEFINED)
        if description["kind"] != "state" or not isSource(source):
            continue
        schemas[source] = [*schemas.get(source, []), description["schema"]]
    return schemas


def validateImplementation(service: Service, implementation: Any) -> None:
    contract = service.contract

    def fail(message: str) -> None:
        raise invalid("implementation", f"{contract.name}: {message}")

    if not isinstance(implementation, dict):
        fail("factory must return an object")
    if contract.scope == "remote":
        variant = implementation.get("variant")
        if not isinstance(variant, str) or contract.description["variants"].get(variant) is None:
            fail(f"unknown variant {_jsText(variant)}")
        members = contract.description["variants"][variant]
        for key in implementation:
            if key != "variant" and members.get(key) is None:
                fail(f"{key} is not in variant {variant}")
    else:
        members = contract.description["variants"].get(LOCAL_VARIANT) or {}
    for name, description in members.items():
        value = implementation.get(name, UNDEFINED)
        if description["kind"] == "method" and (not callable(value) or isSource(value)):
            fail(f"{name} must be a method")
        if description["kind"] == "state" and not isSource(value) and not isReadableLike(value):
            fail(f"{name} must be a cell, a derivation or a held object's state member")
        if description["kind"] == "opaque" and value is UNDEFINED:
            fail(f"{name} is missing")


def _jsText(value: Any) -> str:
    """``String(value)``."""
    if value is UNDEFINED:
        return "undefined"
    if value is None:
        return "null"
    return str(value)


# ─── The factory's `self` ────────────────────────────────────────────────────


class SelfImpl:
    def __init__(self, object: ObjectImpl, previous: Decls | None) -> None:
        self._object = object
        self._previous = previous
        self._decls = Decls()
        self._created = RunCreated()

    def run(self, implementation: Any) -> Run:
        return Run(implementation, self._decls, self._created)

    def _unique(self, map: Mapping[str, Any], kind: str, name: Any) -> None:
        """Refuses a second declaration of ``name`` of one kind in this run."""
        if not isinstance(name, str) or name == "":
            raise TypeError(f"{kind} needs a name")
        if name in map:
            raise invalid("duplicate", f"{self._object.typeName}: duplicate {kind} name {name}")

    def state(self, name: str, schema: TSchema, initial: Any, options: StateOptions | None = None) -> Cell[Any]:
        """A state cell named ``name``: its schema (required: there is no TypeScript at run time) and its initial
        value (validated), or ``{"init": fn}`` to read the first value from its source before the object goes live.
        The name identifies the cell across reloads: a reload keeps the cell and its value (``initial`` is ignored,
        ``init`` does not run), migrates it when its version rises, and refuses a schema change without a version
        bump. A cell a reload adds runs its ``init`` while the reload prepares."""
        options = options or {}
        self._unique(self._decls.cells, "state", name)
        version = nullishCoalesce(options.get("version"), 1)
        if not jsIsInteger(version) or version < 1:
            raise TypeError(f"state {name}: version must be a positive integer")
        host = self._object.host
        # A JSON value never holds a function: an object with a function `init` is the initializer, not a value.
        init = initial["init"] if isStateInit(initial) else None
        cell = None if self._previous is None else self._previous.cells.get(name)
        if cell is None:
            cell = Cell(
                name,
                schema,
                {"value": initial} if init is None else None,
                self._object.guard,
                SourceOptions(dev=host.dev, report=lambda error: host.report(error)),
            )
            if init is not None:
                self._created.inits.append((cell, init))
        self._decls.cells[name] = cell
        self._decls.cellSchemas[name] = schema
        self._decls.cellVersions[name] = version
        if options.get("migrate") is not None:
            self._decls.cellMigrations[name] = options["migrate"]
        return cell

    def get(self, type: AnyContract, input: Any, options: GetOptions | None = None) -> Any:
        """An object-owned reference, identified by its address."""
        host = self._object.host
        # A prepare rerun (recording mode) binds new references against the reload's pending contracts: a breaking
        # provider change and its updated consumer reload together.
        candidate = self._previous is not None
        principal = (options or {}).get("principal")
        address = host.addressOf(type, input, host.principal if principal is None else principal, candidate)
        declared = self._decls.refs.get(address)
        if declared is not None:
            if declared.contract.fingerprint != type.fingerprint:
                raise invalid("duplicate", f"{self._object.typeName}: {type.name} held twice with different contracts")
            return declared.proxy
        existing = None if self._previous is None else self._previous.refs.get(address)
        if (
            existing is not None
            and existing.contract.fingerprint == type.fingerprint
            and existing.status != "gone"
            and existing.status != "released"
        ):
            self._decls.refs[address] = existing
            return existing.proxy
        ref = host.acquire(type, input, options, "object", self._object, candidate)
        self._decls.refs[address] = ref
        self._created.refs.append(ref)
        return ref.proxy

    def resource(self, name: str, declaration: ResourceDeclaration[Any]) -> ResourceFacade:
        """A resource named ``name``: opened before the object goes live, closed after it is disposed. A reload keeps
        it unless its ``version``, ``deps`` or ``exclusive`` changed."""
        self._unique(self._decls.resources, "resource", name)
        version = declaration.get("version")
        if not jsIsInteger(version):
            raise TypeError(f"resource {name}: version must be an integer")
        deps: list[RefCore | ResourceImpl] = []
        for dep in declaration.get("deps") or []:
            resolved_ = refOf(dep) or resourceOf(dep)
            if resolved_ is None:
                raise TypeError(f"resource {name}: deps must be references or resources of this object")
            deps.append(resolved_)
        existing = None if self._previous is None else self._previous.resources.get(name)
        kept = (
            existing is not None
            and existing.spec["version"] == version
            and existing.exclusive == (declaration.get("exclusive") is True)
            and len(existing.deps) == len(deps)
            and all(dep is deps[index] for index, dep in enumerate(existing.deps))
        )
        if kept:
            assert existing is not None
            resource = existing
            self._decls.adopted[existing] = declaration
        else:
            resource = ResourceImpl(name, declaration, deps)
            self._created.resources.append(resource)
        self._decls.resources[name] = resource
        return ResourceFacade(resource, self._object.host)

    def on(self, name: str, source: Any, *rest: Any) -> None:
        """Reacts to ``source`` once the object is live: a readable (cell, derivation, reference state member) delivers
        its current value, then every new value; an event source (or a resource whose value is one) delivers its
        events. With ``select``, subscribes to ``select(value)`` instead (the opened value for a resource). Each event
        runs ``handler`` in a fresh root. The name keeps the subscription across reloads while its source and
        ``select`` are unchanged; a listener on state whose source the reload replaced (a derivation is new at every
        reload) subscribes again and gets the current value again, so a handler of state must reconcile, not count."""
        self._unique(self._decls.listeners, "listener", name)
        select, fn = (None, rest[0]) if len(rest) == 1 else (rest[0], rest[1]) if len(rest) >= 2 else (None, None)
        if not callable(fn):
            raise TypeError(f"listener {name}: the handler must be a function")
        if select is not None and not callable(select):
            raise TypeError(f"listener {name}: select must be a function")
        self._decls.listeners[name] = ListenerDecl(source, select, fn)

    def task(self, fn: Callable[[Ctx], Awaitable[None]]) -> None:
        """Long-running work of the live object (a code holder: its calls start fresh roots), restarted at every
        reload."""
        if not callable(fn):
            raise TypeError("a task must be a function")
        self._decls.tasks[f"#{len(self._decls.tasks)}"] = fn

    def derive(self, sources: Sequence[Any], fn: Callable[..., Any]) -> Derived[Any]:
        """A synchronous projection of readables, recomputed when they change; a new one at every reload."""
        if not callable(fn):
            raise TypeError("a derivation needs a function")
        host = self._object.host
        key = f"#{len(self._decls.derives)}"
        derived = Derived(key, sources, fn, SourceOptions(dev=host.dev, report=lambda error: host.report(error)))
        self._created.derives.append(derived)
        self._decls.derives[key] = derived
        return derived

    def expose(self, resource: Any) -> ExposedImpl:
        """A member backed by one of this object's resources, for local contracts: references read the resource's
        value once the object is live (``None`` before). Typically an ``opaque`` member."""
        resolved_ = resourceOf(resource)
        if resolved_ is None or all(resolved_ is not declared for declared in self._decls.resources.values()):
            raise TypeError("expose takes a resource declared by this object")
        return ExposedImpl(resolved_)

    def register(self, registry: Registry[Any], entries: Mapping[str, Any]) -> None:
        """Objects of lifetime ``"host"`` only. Keys are the registry's own keys."""
        if not self._object.hostLifetime:
            raise invalid("not_host_lifetime", f'{self._object.typeName}: only objects of lifetime "host" may register')
        for key, value in entries.items():
            id = f"{registry.name}\u0000{key}"
            self._unique(self._decls.registrations, "register", id)
            self._decls.registrations[id] = Registration(registry, key, value)


__all__ = [
    "REF_BASE",
    "CtxImpl",
    "Decls",
    "ExposedImpl",
    "Factory",
    "HostCore",
    "HostServeOptions",
    "Installed",
    "Lifetime",
    "MemberReadable",
    "ObjectIdentity",
    "ObjectImpl",
    "ObjectStatus",
    "RefCore",
    "RefImpl",
    "RefOwner",
    "RefProxy",
    "ReferencedServeOptions",
    "ResourceFacade",
    "ResourceImpl",
    "Run",
    "Self",
    "SelfImpl",
    "ServeOptions",
    "ServeOptionsFor",
    "Service",
    "StagedListeners",
    "StateInit",
    "StateOptions",
    "StatusListener",
    "awaitReady",
    "hostLifetime",
    "isReadableLike",
    "linkedSignal",
    "makeService",
    "memberSchemas",
    "notifyStatus",
    "raceSignal",
    "refOf",
    "validateImplementation",
    "variantOf",
]

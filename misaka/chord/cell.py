"""State cells and derivations: object-owned state sources, their fan-out, and hydration.

Translated from pi's ``packages/chord/src/cell.ts``. Upstream's ``HYDRATION`` symbol is the attribute name
``HYDRATION`` (a readable's hydration, read with ``hydrationOf``); listener sets are insertion-ordered dicts, as
JavaScript's ``Set`` keeps insertion order.
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

from misaka.chord._promise import isRejection, promiseAll
from misaka.chord._typebox import TSchema
from misaka.chord.delta import Draft, Op, diffRevisions, track
from misaka.chord.errors import ChordError
from misaka.chord.publish import enqueue, holdPublications
from misaka.chord.schema import assertValid
from misaka.utils.js import UNDEFINED

if TYPE_CHECKING:
    from misaka.chord.types import Ctx, Readable, StateEvent


@dataclass(frozen=True, slots=True)
class SourceOptions:
    dev: bool
    report: Callable[[Any], None]


# Checks that `ctx` may write an object's cells now; throws otherwise.
type WriteGuard = Callable[[Ctx], None]

_sequence = 0


def _nextSequence() -> int:
    global _sequence
    _sequence += 1
    return _sequence


class Fanout[T]:
    """Fans out values and delta events; listener errors are reported, not thrown to the writer."""

    def __init__(self, report: Callable[[Any], None]) -> None:
        self._values: dict[Callable[[T], None], None] = {}
        self._events: dict[Callable[[StateEvent[T]], None], None] = {}
        self._report = report

    def subscribe(self, listener: Callable[[T], None], current: T) -> Callable[[], bool]:
        self._values[listener] = None
        self._call(listener, current)
        return lambda: self._values.pop(listener, UNDEFINED) is not UNDEFINED

    def events(self, listener: Callable[[StateEvent[T]], None], current: T) -> Callable[[], bool]:
        self._events[listener] = None
        self._call(listener, {"type": "snap", "value": current})
        return lambda: self._events.pop(listener, UNDEFINED) is not UNDEFINED

    def emit(self, event: StateEvent[T]) -> None:
        """Queues delivery to the listeners subscribed now. A listener that subscribes later already got this revision
        in its snapshot; one that unsubscribes before delivery is skipped."""
        events = list(self._events)
        values = list(self._values)

        def deliver() -> None:
            for listener in events:
                if listener in self._events:
                    self._call(listener, event)
            for listener in values:
                if listener in self._values:
                    self._call(listener, event["value"])

        enqueue(deliver)

    def _call(self, listener: Callable[[Any], None], value: Any) -> None:
        try:
            listener(value)
        except BaseException as error:  # a listener's failure is reported, never thrown to the writer
            if not isRejection(error):
                raise
            self._report(error)


# Whether a state source has its value (entry 8): a remote reference's member before its first snapshot has none, and
# neither has anything derived from or forwarding it. Sources without this attribute always have their value.
HYDRATION: Final = "__chord_hydration__"


@dataclass(frozen=True, slots=True)
class Hydration:
    hydrated: bool
    # Resolves once hydrated; rejects if the source ends first.
    whenHydrated: Callable[[], Awaitable[None]]


def hydrationOf(value: Any) -> Hydration | None:
    """The hydration of a readable, or ``None`` for a source that always has its value."""
    if value is None or isinstance(value, str | int | float | bool | dict | list) or value is UNDEFINED:
        return None
    return getattr(value, HYDRATION, None)


def unhydrated(sources: Iterable[Any]) -> list[Hydration]:
    """The hydrations of ``sources`` that are not hydrated yet."""
    result: list[Hydration] = []
    for source in sources:
        hydration = hydrationOf(source)
        if hydration is not None and hydration.hydrated is False:
            result.append(hydration)
    return result


class Source[T](ABC):
    """An object-owned state source (cell or derivation). Writes recompute dependents atomically."""

    def __init__(self, key: str, options: SourceOptions) -> None:
        self.key = key
        self.seq = _nextSequence()
        self.dependents: dict[Derived[Any], None] = {}
        # Schemas of remote state members currently exposing this source (checked per write in dev mode).
        self.schemas: Sequence[TSchema] = []
        self.options = options
        self.fanout: Fanout[T] = Fanout(options.report)

    @property
    @abstractmethod
    def value(self) -> T: ...

    def subscribe(self, listener: Callable[[T], None]) -> Callable[[], bool]:
        return self.fanout.subscribe(listener, self.value)

    def events(self, listener: Callable[[StateEvent[T]], None]) -> Callable[[], bool]:
        return self.fanout.events(listener, self.value)

    def validate(self, value: Any) -> None:
        """Per-write validation, dev mode only."""
        if not self.options.dev:
            return
        for schema in self.ownSchemas():
            assertValid("state", f"state {self.key}", schema, value)
        for schema in self.schemas:
            assertValid("state", f"state {self.key}", schema, value)

    def ownSchemas(self) -> Sequence[TSchema]:
        return []


def unbox(ops: Sequence[Op], value: Any) -> list[Op]:
    """The tracker tracks containers, so a cell tracks ``{"v": value}`` and strips the box from published ops."""
    result: list[Op] = []
    for op in ops:
        if op[0] == "r" or len(op[1]) == 0:
            return [["r", value]]
        path = list(op[1][1:])
        if op[0] == "p":
            result.append(["p", path, op[2], op[3], op[4]])
        elif op[0] == "m":
            result.append(["m", path, op[2]])
        else:
            # s/d/a/t on the value itself replace it.
            if len(path) == 0:
                return [["r", value]]
            if op[0] == "d":
                result.append(["d", path])
            else:
                result.append([op[0], path, op[2]])
    return result


class Cell[T](Source[T]):
    """A state cell holding any JSON value, validated against its schema at creation and (dev mode) on writes."""

    def __init__(
        self,
        key: str,
        schema: TSchema,
        initial: Mapping[str, Any] | None,
        guard: WriteGuard,
        options: SourceOptions,
    ) -> None:
        """``initial`` (``{"value": ...}``) is always validated against ``schema``; ownership moves to the cell.
        Without ``initial``, the cell has no value until ``initialize`` (a cell declared with ``init``, filled before
        its object goes live)."""
        super().__init__(key, options)
        if initial is not None:
            assertValid("state", f"initial value of state {key}", schema, initial["value"])
        # Replaced at a reload commit that changes the schema (state-version bump).
        self.schema = schema
        self._tracker = track({"v": initial["value"] if initial is not None else None})
        self._guard = guard
        # False until `initialize` for a cell declared with `init`: reads and writes throw `not_ready`.
        self._initialized = initial is not None

    def initialize(self, value: T) -> None:
        """Sets the first value of a cell declared with ``init`` (validated; ownership moves to the cell). Publishes
        nothing."""
        if self._initialized:
            raise RuntimeError(f"state {self.key} is already initialized")
        assertValid("state", f"initial value of state {self.key}", self.schema, value)
        self._tracker = track({"v": value})
        self._initialized = True

    def ownSchemas(self) -> Sequence[TSchema]:
        return [self.schema]

    @property
    def value(self) -> T:
        if not self._initialized:
            raise notReady(f"state {self.key} (its init has not run)")
        return self._tracker.value["v"]

    def change(self, ctx: Ctx, fn: Callable[[Draft[T]], Any]) -> None:
        """Mutates a draft (only mutated paths are published), or returns a new value (``lambda n: n + 1``). A
        returned value is always validated against the schema. The draft is unusable after ``fn`` returns.

        MISAKA fork: a Python function that mutates the draft returns ``None``, so ``None`` means "no new value" (as
        ``undefined`` does upstream): a function returning ``None`` writes nothing, also where upstream, given JSON
        ``null``, would write it or refuse it (``invalid``). Write ``null`` with ``replace``."""
        self._guard(ctx)
        if not self._initialized:
            raise notReady(f"state {self.key} (its init has not run)")
        change = self._tracker.beginChange()
        try:
            draft = change.state
            current = draft["v"]
            result = fn(current)
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise TypeError("cell.change callbacks must be synchronous")
            if result is not None and result is not UNDEFINED:
                if result is not current:
                    assertValid("state", f"state {self.key}", self.schema, result)
                draft["v"] = result
            prepared = change.prepare()
        except BaseException:
            change.abort()
            raise
        self._publish(prepared, "ops")

    def replace(self, ctx: Ctx, value: T, options: Mapping[str, Any] | None = None) -> None:
        """Replaces the whole value. Ownership of ``value`` moves to the cell: never mutate it afterwards.

        Subscribers get a structural diff (``diffRevisions``) when ``diff`` is true, or by default when the new value
        shares containers with the old one (a new immutable revision of the same state, e.g. mirrored from another
        store); otherwise a single ``r`` op. The resulting value is the same either way; only the ops differ."""
        self._guard(ctx)
        if not self._initialized:
            raise notReady(f"state {self.key} (its init has not run)")
        before = self.value
        diff = (options or {}).get("diff")
        if diff is None:
            diff = sharesStructure(before, value)
        ops = diffRevisions(before, value) if diff else None
        self._publish(self._tracker.prepareReplace({"v": value}), "ops", True, ops)

    def reset(self, value: T, schema: TSchema) -> None:
        """Installs a migrated value and its schema (reload commit). The caller validated the value and the
        derivations over it against every schema that will apply, so nothing is checked here. Subscribers receive a
        reset."""
        self.schema = schema
        self._publish(self._tracker.prepareReplace({"v": value}), "reset", False)

    def _publish(
        self,
        prepared: Any,
        type: Literal["ops", "reset"],
        validate: bool = True,
        ops: Sequence[Op] | None = None,
    ) -> None:
        """Adopts this revision and every derived one, then queues all notifications (cell first, then
        derivations)."""
        if len(prepared.ops) == 0 and type == "ops":
            self._tracker.adopt(prepared)
            return
        value = prepared.value["v"]
        try:
            if validate:
                self.validate(value)
            candidates = computeDependents(self, value, validate)
        except BaseException:
            prepared.abort()
            raise

        def install() -> None:
            self._tracker.adopt(prepared)
            published = list(ops) if ops is not None and len(ops) > 0 else unbox(prepared.ops, value)
            self.fanout.emit({"type": type, "value": value} if type == "reset" else {"type": type, "ops": published, "value": value})
            for derived, next in candidates.items():
                derived.install(next)

        holdPublications(install)


def sharesStructure(a: Any, b: Any) -> bool:
    """True if ``b`` reuses at least one container of ``a`` at the same key: a new revision of the same state."""
    if not isinstance(a, dict | list) or not isinstance(b, dict | list) or a is b:
        return False
    if isinstance(a, list) != isinstance(b, list):
        return False
    entries = b.items() if isinstance(b, dict) else enumerate(b)
    for key, child in entries:
        if isinstance(child, dict | list):
            if isinstance(a, dict):
                if a.get(key) is child:
                    return True
            elif isinstance(key, int) and key < len(a) and a[key] is child:
                return True
    return False


def computeDependents(changed: Source[Any], value: Any, validate: bool = True) -> dict[Derived[Any], Any]:
    """Recomputes every transitive dependent of ``changed`` against candidate values and validates them."""
    pending: dict[Derived[Any], None] = {}

    def visit(source: Source[Any]) -> None:
        for derived in source.dependents:
            if derived in pending:
                continue
            pending[derived] = None
            visit(derived)

    visit(changed)
    values: dict[Any, Any] = {changed: value}
    result: dict[Derived[Any], Any] = {}
    for derived in sorted(pending, key=lambda item: item.seq):
        next = derived.compute(lambda source: values[source] if source in values else source.value)  # noqa: SIM401 - read lazily
        if validate:
            derived.validate(next)
        values[derived] = next
        result[derived] = next
    return result


# Readables: cells, derivations, reference state members.
type Sources = Sequence[Readable[Any]]
type SourceValues = Sequence[Any]


def isReadable(value: Any) -> bool:
    if value is None or isinstance(value, str | int | float | bool | dict | list):
        return False
    try:
        subscribe = getattr(value, "subscribe", None)
        hasValue = hasattr(type(value), "value") or "value" in getattr(value, "__dict__", {})
    except Exception:  # noqa: BLE001 - an object whose attributes fail to read is not a readable
        return False
    return callable(subscribe) and hasValue


class Derived[T](Source[T]):
    """A synchronous derivation. Sources are object cells/derivations (recomputed atomically with the write) or
    other readables such as reference state members (recomputed when they publish). Subscribers receive diffs."""

    def __init__(self, key: str, sources: Sources, fn: Callable[..., Any], options: SourceOptions) -> None:
        super().__init__(key, options)
        checked = []
        for source in sources:
            if not isReadable(source):
                raise TypeError(f"derive {key}: sources must be readables")
            checked.append(source)
        self.sources: Sequence[Readable[Any]] = checked
        self._fn = fn
        self._value: Any = UNDEFINED
        self._external: list[Callable[[], Any]] = []

    @property
    def value(self) -> T:
        return self._value

    @property
    def __chord_hydration__(self) -> Hydration | None:
        """Hydrated once every source is (a derivation over a remote member waits for its snapshot)."""
        pending = unhydrated(self.sources)
        if len(pending) == 0:
            return None

        async def whenHydrated() -> None:
            await promiseAll([hydration.whenHydrated() for hydration in pending])

        return Hydration(False, whenHydrated)

    def attach(self, precomputed: Mapping[Any, Any] | None = None) -> None:
        """Computes the first value (or takes it from ``precomputed``, a reload's validated dry run) and starts
        following the sources (at install, never inside a factory)."""
        if precomputed is not None and self in precomputed:
            self._value = precomputed[self]
        else:
            self._value = self.compute(lambda source: source.value)
        for source in self.sources:
            if isinstance(source, Source):
                source.dependents[self] = None
            else:
                self._external.append(source.subscribe(lambda _value: self.refresh()))

    def detach(self) -> None:
        for source in self.sources:
            if isinstance(source, Source):
                source.dependents.pop(self, None)
        external, self._external = self._external, []
        for unsubscribe in external:
            unsubscribe()

    def compute(self, read: Callable[[Readable[Any]], Any]) -> T:
        result = self._fn(*[read(source) for source in self.sources])
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            raise TypeError(f"derive {self.key}: fn must be synchronous")
        return result

    def refresh(self) -> None:
        """Recomputes and republishes, including dependents. Errors are reported (the source write already
        happened)."""
        try:
            next = self.compute(lambda source: source.value)
            dependents = computeDependents(self, next)

            def install() -> None:
                self.install(next)
                for derived, value in dependents.items():
                    derived.install(value)

            holdPublications(install)
        except Exception as error:  # noqa: BLE001 - reported: the source write already happened
            self.options.report(error)

    def install(self, next: T) -> None:
        before = self._value
        self._value = next
        if before is UNDEFINED or next is UNDEFINED:
            ops: list[Op] = [["r", None if next is UNDEFINED else next]]
        else:
            ops = diffRevisions(before, next)
        if len(ops) > 0:
            self.fanout.emit({"type": "ops", "ops": ops, "value": next})


def isSource(value: Any) -> bool:
    return isinstance(value, Source)


def notReady(what: str) -> ChordError:
    """Thrown by placeholders used before activation finished."""
    return ChordError("not_ready", f"{what} is not ready")


__all__ = [
    "HYDRATION",
    "Cell",
    "Derived",
    "Fanout",
    "Hydration",
    "Source",
    "SourceOptions",
    "SourceValues",
    "Sources",
    "WriteGuard",
    "computeDependents",
    "hydrationOf",
    "isReadable",
    "isSource",
    "notReady",
    "sharesStructure",
    "unbox",
    "unhydrated",
]

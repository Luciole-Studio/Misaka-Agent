"""Replicated state: an immutable JSON value with ordered, serialized change notifications. Either authoritative and
mutable (``replicatedState(initial)``) or a publication-only view of an external revision source
(``replicatedState(source)``).

Translated from pi's ``packages/chord/src/state.ts`` (``services/state.ts`` before 1.2.0).

MISAKA forks, all forced by the language: a listener may be a plain function or a coroutine function (a
returned awaitable is awaited on the running event loop, where upstream awaits a ``PromiseLike``; the next delivery
to that subscriber waits for the task's done callback, a turn of the event loop where upstream waits a microtask, so
subscribers that keep pace with each other upstream may drift apart here, and more of a slow subscriber's pending
deliveries can be dropped at unsubscribe; each subscriber's own sequence is the same);
``AggregateError`` is an ``ExceptionGroup``; ``queueMicrotask(() => { throw error })`` reports on the running
loop through ``loop.call_soon``, so asyncio's exception handler sees it, and through ``sys.excepthook``
without one; ``undefined`` is ``UNDEFINED``. The state types are ``Protocol`` classes and the plain shapes
``TypedDict`` values, with upstream's key names.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Literal, Protocol, TypedDict

from misaka.chord._promise import isRejection
from misaka.chord.context import BACKGROUND_CONTEXT, Context
from misaka.chord.delta import Draft, Op, Prepared, track
from misaka.utils.js import jsIsInteger, startTask

_MAX_SAFE_INTEGER = 2**53 - 1


class ReplicatedStateDelivery(TypedDict):
    kind: Literal["hydrate", "update"]
    sequence: int


class ReplicatedState[T](Protocol):
    @property
    def value(self) -> T:
        """Contract-immutable value, or ``UNDEFINED`` until hydration. It is not frozen and may share
        containers with an in-process provider; consumers must not mutate it. Later updates do not mutate
        previously returned values."""
        ...

    def subscribe(self, listener: StateListener[T]) -> Callable[[], None]:
        """Each subscription serializes callbacks, awaiting hydration before updates. Values are immutable
        and may share unchanged revision data. At most 100 deliveries wait behind the running callback;
        overflow keeps only the newest pending value/context/delivery, so update sequences may skip.
        Failures are reported in isolation and delivery continues. Unsubscribe discards pending work without
        aborting or joining a callback."""
        ...


class MutableReplicatedState[T](ReplicatedState[T], Protocol):
    def change(self, context: Context, mutate: Callable[[Draft[T]], None]) -> None:
        """Atomically publish one synchronous overlay mutation. Draft handles are unusable after the callback
        returns. Values placed through the draft are cloned by value; assigning ``UNDEFINED`` to an object
        key deletes it."""
        ...

    def replace(self, context: Context, value: T) -> None:
        """Atomically take immutable ownership of an alias-free strict-JSON replacement. The caller must not
        mutate the transferred root after this call."""
        ...


class ReplicatedStateSourceFrame[T](TypedDict):
    """One immutable authoritative revision committed after an attachment snapshot."""

    cursor: int  # Monotonic source cursor. The first frame after a snapshot must be `snapshot.cursor + 1`.
    value: T  # The exact immutable value produced by this commit.
    ops: Sequence[Op]  # The exact immutable operation batch that produced `value` from the preceding source revision.
    context: Context


class ReplicatedStateSourceSnapshot[T](TypedDict):
    value: T
    cursor: int


class ReplicatedStateSourceAttachment[T](Protocol):
    @property
    def snapshot(self) -> ReplicatedStateSourceSnapshot[T]:
        """Fixed immutable snapshot captured at the atomic attachment boundary."""
        ...

    def activate(self, listener: Callable[[ReplicatedStateSourceFrame[T]], None]) -> None:
        """Install the sole listener and synchronously drain every buffered frame in source commit order.
        This method is single-use. After it begins, every new committed frame must also be delivered in order
        until disposal, including commits made reentrantly while a prior frame is being delivered."""
        ...

    def dispose(self) -> None:
        """Stop delivery and release source resources. Disposal must be idempotent."""
        ...


class ReplicatedStateSource[T](Protocol):
    """An authoritative immutable revision source.

    ``attach()`` must synchronously and atomically capture one snapshot and register the returned attachment
    to buffer every later committed frame. The snapshot must include every commit before that boundary;
    buffered frames must include every commit after it, with no overlap or gap. Snapshot values, frame values
    and operation batches are immutable and remain valid after delivery. Chord only publishes these
    references; it never applies or re-diffs them."""

    def attach(self) -> ReplicatedStateSourceAttachment[T]: ...


class ReplicatedStateSourceOptions(TypedDict, total=False):
    # Receives source-contract and publication-listener failures without throwing them into the source.
    onError: Callable[[Exception], None]


class AttachedReplicatedState[T](ReplicatedState[T], Protocol):
    """A synchronously hydrated publication-only state backed by one source attachment."""

    def dispose(self) -> None:
        """Idempotently release the source attachment. The last published value remains readable."""
        ...


# The void signature also accepts synchronous callbacks that return an ignored value.
type StateListener[T] = Callable[[T, Context, ReplicatedStateDelivery], Awaitable[None] | None]


class StateDelivery[T]:
    __slots__ = ("context", "delivery", "value")

    def __init__(self, value: T, context: Context, delivery: ReplicatedStateDelivery) -> None:
        self.value = value
        self.context = context
        self.delivery = delivery


class StateSubscriber[T]:
    """One public subscription, independent of producer and other subscriber progress."""

    def __init__(self, listener: StateListener[T], reportError: Callable[[Exception], None]) -> None:
        self._listener = listener
        self._reportError = reportError
        self._pending: list[StateDelivery[T]] = []
        self._running = False
        self._started = False
        self._closed = False

    def push(self, frame: StateDelivery[T]) -> None:
        if self._closed:
            return
        if len(self._pending) == 100:
            # A cold replica can queue updates reentrantly before this subscriber's first hydration starts.
            hydration = None if self._started else self._pending[0]
            self._pending.clear()
            if hydration is not None:
                self._pending.append(hydration)
        self._pending.append(frame)

    def drain(self) -> None:
        if self._running or self._closed:
            return
        self._running = True
        waiting = False
        try:
            while self._pending:
                frame = self._pending.pop(0)
                self._started = True
                result: Any = None
                try:
                    result = self._listener(frame.value, frame.context, frame.delivery)
                    if inspect.isawaitable(result):
                        task = startTask(result)
                        task.add_done_callback(self._settled)
                        waiting = True
                        return
                except BaseException as error:  # a listener's failure is reported and delivery continues
                    if inspect.iscoroutine(result):
                        result.close()
                    if not isRejection(error):
                        raise
                    self._report(error)
        finally:
            # Also when a listener's KeyboardInterrupt (or the task's cancellation) leaves the loop: later deliveries
            # still run.
            if not waiting:
                self._running = False

    def _settled(self, task: asyncio.Future[Any]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            self._report(error)
        self._resume()

    def clear(self) -> None:
        self._pending.clear()

    def close(self) -> None:
        self._closed = True
        self.clear()

    def _resume(self) -> None:
        self._running = False
        self.drain()

    def _report(self, error: BaseException) -> None:
        try:
            self._reportError(toError(error))
        except BaseException as reportError:  # a reporter that fails goes to the loop's handler
            if not isRejection(reportError):
                raise
            reportErrorAsync(toError(reportError))

class Publication[T]:
    __slots__ = ("context", "sequence", "value")

    def __init__(self, value: T, sequence: int, context: Context) -> None:
        self.value = value
        self.sequence = sequence
        self.context = context


class ReplicatedStatePublisher[T]:
    """Maintains local publication order independently of how revisions are produced."""

    def __init__(self, initial: T, reportError: Callable[[Exception], None] | None = None) -> None:
        self._listeners: dict[StateSubscriber[T], int] = {}
        self._reportError = reportError if reportError is not None else reportErrorAsync
        self._publications: list[Publication[T]] = []
        self._value = initial
        self._sequence = 0
        self._delivering = False

    @property
    def value(self) -> T:
        return self._value

    def snapshot(self) -> dict[str, Any]:
        return {"value": self._value, "sequence": self._sequence}

    def subscribe(self, listener: StateListener[T]) -> Callable[[], None]:
        value, sequence = self._value, self._sequence
        subscriber = StateSubscriber(listener, self._reportError)
        self._listeners[subscriber] = sequence
        # Hydration has no caller; it is delivered with the background context.
        subscriber.push(StateDelivery(value, BACKGROUND_CONTEXT, {"kind": "hydrate", "sequence": sequence}))
        subscriber.drain()

        def unsubscribe() -> None:
            subscriber.close()
            self._listeners.pop(subscriber, None)

        return unsubscribe

    def publish(self, value: T, context: Context) -> None:
        """Publish an already-prepared immutable revision. Subscriber failures are reported, never thrown."""
        self._value = value
        self._sequence += 1
        self._publications.append(Publication(value, self._sequence, context))
        if self._delivering:
            return

        self._delivering = True
        try:
            while self._publications:
                publication = self._publications.pop(0)
                delivery: ReplicatedStateDelivery = {"kind": "update", "sequence": publication.sequence}
                for subscriber, hydratedSequence in list(self._listeners.items()):
                    if publication.sequence <= hydratedSequence:
                        continue
                    subscriber.push(StateDelivery(publication.value, publication.context, delivery))
                    subscriber.drain()
        finally:
            self._delivering = False


class MutableReplicatedStateImpl[T](MutableReplicatedState[T]):
    def __init__(self, initial: T) -> None:
        self._tracker = track(initial)
        self._publisher: ReplicatedStatePublisher[T] = ReplicatedStatePublisher(self._tracker.value)
        self._changing = False

    @property
    def value(self) -> T:
        return self._tracker.value

    def change(self, context: Context, mutate: Callable[[Any], Any]) -> None:
        if self._changing:
            raise RuntimeError("Replicated state cannot be changed reentrantly from a change callback")
        self._changing = True
        prepared: Prepared[T]
        try:
            change = self._tracker.beginChange()
            try:
                outcome = mutate(change.state)
                if inspect.isawaitable(outcome):
                    discardAwaitable(outcome)
                    raise TypeError("Replicated state change callbacks must be synchronous")
                prepared = change.prepare()
            except BaseException:
                change.abort()
                raise
        finally:
            self._changing = False

        self._tracker.adopt(prepared)
        if len(prepared.ops) > 0:
            self._publisher.publish(prepared.value, context)

    def replace(self, context: Context, value: T) -> None:
        if self._changing:
            raise RuntimeError("Replicated state cannot be replaced from a change callback")
        prepared = self._tracker.prepareReplace(value)
        self._tracker.adopt(prepared)
        if len(prepared.ops) > 0:
            self._publisher.publish(prepared.value, context)

    def subscribe(self, listener: StateListener[T]) -> Callable[[], None]:
        return self._publisher.subscribe(listener)


class AttachedReplicatedStateImpl[T](AttachedReplicatedState[T]):
    def __init__(self, attachment: ReplicatedStateSourceAttachment[T], options: ReplicatedStateSourceOptions) -> None:
        snapshot = attachment.snapshot
        assertCursor(snapshot["cursor"], "snapshot")
        self._attachment = attachment
        self._cursor = snapshot["cursor"]
        self._reportError = options.get("onError") or reportErrorAsync
        self._disposed = False
        self._publisher: ReplicatedStatePublisher[T] = ReplicatedStatePublisher(snapshot["value"], self._report)

    @property
    def value(self) -> T:
        return self._publisher.value

    def subscribe(self, listener: StateListener[T]) -> Callable[[], None]:
        return self._publisher.subscribe(listener)

    def activate(self) -> None:
        self._attachment.activate(self._receive)

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._attachment.dispose()

    def _receive(self, frame: ReplicatedStateSourceFrame[T]) -> None:
        if self._disposed:
            return
        try:
            assertCursor(frame["cursor"], "frame")
            expected = self._cursor + 1
            if frame["cursor"] != expected:
                raise RuntimeError(
                    f"Replicated state source cursor has a gap: expected {expected}, received {frame['cursor']}"
                )
            self._cursor = frame["cursor"]
            self._publisher.publish(frame["value"], frame["context"])
        except Exception as error:  # noqa: BLE001 - a source that breaks its contract fails the state
            self._fail(toError(error))

    def _fail(self, error: Exception) -> None:
        if self._disposed:
            return
        self._disposed = True
        try:
            self._attachment.dispose()
        except Exception as disposeError:  # noqa: BLE001
            self._report(ExceptionGroup("Replicated state source contract failed", [error, disposeError]))
            return
        self._report(error)

    def _report(self, error: BaseException) -> None:
        try:
            self._reportError(toError(error))
        except Exception as reportError:  # noqa: BLE001
            reportErrorAsync(toError(reportError))


def attachReplicatedStateSource[T](
    source: ReplicatedStateSource[T], options: ReplicatedStateSourceOptions | None = None
) -> AttachedReplicatedState[T]:
    """Attach a publication-only replicated state to one authoritative immutable source stream."""
    attachment = source.attach()
    try:
        state = AttachedReplicatedStateImpl(attachment, options or {})
        state.activate()
        return state
    except Exception as error:
        try:
            attachment.dispose()
        except Exception as disposeError:  # noqa: BLE001
            raise ExceptionGroup("Failed to attach replicated state source", [error, disposeError]) from None
        raise


def assertCursor(cursor: Any, kind: str) -> None:
    if not jsIsInteger(cursor) or abs(cursor) > _MAX_SAFE_INTEGER:
        raise TypeError(f"Replicated state source {kind} cursor must be a safe integer")


def discardAwaitable(value: Any) -> None:
    """``void Promise.resolve(value).catch(() => undefined)``: nothing will wait for the outcome."""
    if inspect.iscoroutine(value):
        value.close()
        return
    if isinstance(value, asyncio.Future):
        value.add_done_callback(lambda done: done.cancelled() or done.exception())


def reportErrorAsync(error: Exception) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        sys.excepthook(type(error), error, error.__traceback__)
        return
    loop.call_soon(_raise, error)


def _raise(error: Exception) -> None:
    raise error


def toError(error: BaseException) -> Exception:
    return error if isinstance(error, Exception) else Exception(str(error))


def replicatedState(initialOrSource: Any, options: ReplicatedStateSourceOptions | None = None) -> Any:
    """``replicatedState(source, options?)`` attaches a publication-only state (``AttachedReplicatedState``) to a source
    (an object with ``attach()``); ``replicatedState(initial)`` creates authoritative state (``MutableReplicatedState``)
    by taking immutable ownership of an alias-free strict-JSON root. The caller must not mutate ``initial`` after this
    call."""
    if isReplicatedStateSource(initialOrSource):
        return attachReplicatedStateSource(initialOrSource, options)
    return MutableReplicatedStateImpl(initialOrSource)


def isReplicatedStateSource(value: Any) -> bool:
    return callable(getattr(value, "attach", None))


__all__ = [
    "AttachedReplicatedState",
    "MutableReplicatedState",
    "ReplicatedState",
    "ReplicatedStateDelivery",
    "ReplicatedStateSource",
    "ReplicatedStateSourceAttachment",
    "ReplicatedStateSourceFrame",
    "ReplicatedStateSourceOptions",
    "ReplicatedStateSourceSnapshot",
    "replicatedState",
]

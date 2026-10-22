"""MISAKA fork: the JavaScript promise idioms Chord's host runtime is written in, on asyncio.

A promise is an ``asyncio.Future`` on the running loop. ``deferred()`` is ``Promise.withResolvers()`` with
``promise.catch(() => {})`` applied (upstream's ``deferred`` in ``remote.ts``); ``quiet`` is ``.catch(noop)``: the
future's failure counts as observed, as an unhandled rejection is silenced upstream; ``background`` is
``void promise.catch(report)``-style fire and forget, holding the task until it finishes (asyncio holds tasks weakly);
``race`` is ``Promise.race``; ``rejection`` turns any thrown JavaScript value into an exception (JavaScript throws
anything, Python only exceptions).

JavaScript cannot cancel a promise, and awaiting one never affects it. asyncio's ``await`` cancels what it awaits when
the waiter is cancelled (``wait_for``, a timeout, a TaskGroup), so a shared promise is awaited with ``awaited`` (or
``promiseAll``, which does not cancel its inputs as ``asyncio.gather`` does) and handed to outside code through
``exposed`` (a view a waiter that gives up cancels, ``asyncio.shield``). ``isRejection`` tells a rejection from the
running task's own cancellation.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from misaka.utils.js import jsString, startTask

_background: set[asyncio.Future[Any]] = set()


def _observe(future: asyncio.Future[Any]) -> None:
    if not future.cancelled():
        future.exception()


def quiet[T](future: asyncio.Future[T]) -> asyncio.Future[T]:
    """``promise.catch(() => {})`` without a new promise: a failure nobody awaits is not reported."""
    future.add_done_callback(_observe)
    return future


_quiet = quiet


def rejection(error: Any) -> BaseException:
    """The exception to raise for a thrown JavaScript value."""
    return error if isinstance(error, BaseException) else Exception(jsString(error))


def isRejection(error: BaseException) -> bool:
    """Whether ``error`` stands for a JavaScript rejection, which every ``catch`` and ``.then(_, onRejected)`` sees:
    any ``Exception``, and a ``CancelledError`` that is not the running task's own cancellation (one raised by awaiting
    a future someone else cancelled; JavaScript has no cancellation). The running task's own cancellation,
    ``KeyboardInterrupt`` and ``SystemExit`` are not: code that cleans up after a rejection cleans up after them too,
    then lets them propagate."""
    if isinstance(error, Exception):
        return True
    if not isinstance(error, asyncio.CancelledError):
        return False
    try:
        task = asyncio.current_task()
    except RuntimeError:
        return True
    return task is None or task.cancelling() == 0


class CancelledRejection(Exception):
    """A rejection-like ``CancelledError`` (``isRejection``) as an ``Exception``: what an aggregate collects (an
    ``ExceptionGroup`` holds only exceptions) and what a task fails with instead of counting as cancelled. The
    ``CancelledError`` is its ``__cause__``."""

    def __init__(self, cancelled: BaseException) -> None:
        super().__init__(str(cancelled) or "cancelled")
        self.__cause__ = cancelled


def asException(error: BaseException) -> Exception:
    """A rejection as an ``Exception``: an ``Exception`` as it is, a rejection-like ``CancelledError`` wrapped."""
    return error if isinstance(error, Exception) else CancelledRejection(error)


async def awaited[T](promise: asyncio.Future[T]) -> T:
    """``await promise`` as JavaScript awaits it: when the waiter is cancelled (``wait_for``, a timeout, a TaskGroup),
    only its wait ends; the promise, which other code may share, is not cancelled with it."""
    if not promise.done():
        await asyncio.wait([promise])
    return promise.result()


def exposed[T](promise: asyncio.Future[T]) -> asyncio.Future[T]:
    """A shared promise as code outside Chord gets it: a view (``asyncio.shield``) that a waiter giving up cancels,
    never the promise itself. A settled promise is itself (awaiting it cannot cancel it)."""
    if promise.done():
        return promise
    return quiet(asyncio.shield(promise))


async def promiseAll(awaitables: Iterable[Any]) -> list[Any]:
    """``Promise.all(values)``: every awaitable starts at once, the first failure fails the whole (the first in order
    among failures seen together), the results keep their order. A waiter's cancellation ends only its wait: the
    inputs, which may be shared, are not cancelled (``asyncio.gather`` cancels them), and those still running keep
    running, as JavaScript's do; their later failures count as handled, as ``Promise.all`` attached a handler."""
    started = [background(item) if inspect.isawaitable(item) else item for item in awaitables]
    pending = [quiet(item) for item in started if isinstance(item, asyncio.Future)]
    if not pending:
        await asyncio.sleep(0)
        return started
    # One callback per input counts the inputs settled (linear; `asyncio.wait` in a loop re-registers on every input
    # at each turn). The first failure to settle fails the whole; already settled inputs report in their order.
    outcome: asyncio.Future[asyncio.Future[Any] | None] = asyncio.get_running_loop().create_future()
    left = len(pending)

    def settled(future: asyncio.Future[Any]) -> None:
        nonlocal left
        if outcome.done():
            return
        if future.cancelled() or future.exception() is not None:
            outcome.set_result(future)
            return
        left -= 1
        if left == 0:
            outcome.set_result(None)

    for future in pending:
        future.add_done_callback(settled)
    try:
        failed = await outcome
    finally:
        for future in pending:
            future.remove_done_callback(settled)
    if failed is not None:
        if failed.cancelled():
            raise asyncio.CancelledError
        raise failed.exception()  # type: ignore[misc]
    return [item.result() if isinstance(item, asyncio.Future) else item for item in started]


class Deferred[T]:
    """``Promise.withResolvers()``: ``promise``, ``resolve(value)`` and ``reject(error)``. Settling twice is a no-op, as
    a promise settles once, and so is settling a promise its awaiter cancelled (``resolve`` never throws). ``quiet``
    (the default) is upstream's ``.catch(noop)`` on it; ``Deferred(quiet=False)`` is the bare
    ``Promise.withResolvers()``, whose rejection nobody awaits is reported by asyncio as never retrieved."""

    __slots__ = ("promise",)

    def __init__(self, quiet: bool = True) -> None:
        future: asyncio.Future[T] = asyncio.get_running_loop().create_future()
        self.promise: asyncio.Future[T] = _quiet(future) if quiet else future

    def resolve(self, value: Any = None) -> None:
        if not self.promise.done():
            self.promise.set_result(value)

    def reject(self, error: Any) -> None:
        if not self.promise.done():
            self.promise.set_exception(rejection(error))


def deferred() -> Deferred[Any]:
    return Deferred()


def resolved(value: Any = None) -> asyncio.Future[Any]:
    """``Promise.resolve(value)`` for a plain value."""
    future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    future.set_result(value)
    return future


def rejected(error: Any) -> asyncio.Future[Any]:
    """``Promise.reject(error)``, observed (the caller hands it to someone who awaits it)."""
    future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    future.set_exception(rejection(error))
    return quiet(future)


def start[T](awaitable: Awaitable[T]) -> asyncio.Future[T]:
    """A promise for ``awaitable``: a coroutine starts at once, as a JavaScript async function does."""
    return startTask(awaitable)


def hold[T](future: asyncio.Future[T]) -> asyncio.Future[T]:
    """Keeps a running task referenced until it ends (asyncio holds tasks weakly), without observing its outcome."""
    if not future.done():
        _background.add(future)
        future.add_done_callback(_background.discard)
    return future


def background(awaitable: Awaitable[Any], onError: Callable[[BaseException], None] | None = None) -> asyncio.Future[Any]:
    """``void promise`` (or ``promise.catch(onError)``): runs to completion without anyone awaiting it."""
    future = startTask(awaitable)
    if not future.done():
        _background.add(future)
        future.add_done_callback(_background.discard)

    def settled(done: asyncio.Future[Any]) -> None:
        if done.cancelled():
            return
        error = done.exception()
        if error is not None and onError is not None:
            onError(error)

    future.add_done_callback(settled)
    return future


async def race(awaitables: Iterable[Awaitable[Any]]) -> Any:
    """``Promise.race(values)``: settles as the first of them settles (the first in order when several already
    have); the others keep running."""
    # Every input counts as handled, as `Promise.race` attaches a handler to each.
    futures = [quiet(startTask(item)) for item in awaitables]
    for future in futures:
        if future.done():
            return future.result()
    waiter: asyncio.Future[asyncio.Future[Any]] = asyncio.get_running_loop().create_future()

    def settled(done: asyncio.Future[Any]) -> None:
        if not waiter.done():
            waiter.set_result(done)

    for future in futures:
        future.add_done_callback(settled)
    try:
        first = await waiter
    finally:
        for future in futures:
            future.remove_done_callback(settled)
    return first.result()


__all__ = [
    "CancelledRejection",
    "Deferred",
    "asException",
    "awaited",
    "background",
    "deferred",
    "exposed",
    "hold",
    "isRejection",
    "promiseAll",
    "quiet",
    "race",
    "rejected",
    "rejection",
    "resolved",
    "start",
]

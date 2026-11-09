"""Invocation-scoped values and cancellation.

Translated from pi's ``packages/chord/src/context/index.ts``. ``abortSignal`` answers ``None``, not
``undefined``, when a context carries no signal; ``value(key)`` answers ``UNDEFINED`` for an absent key.
A key's ``token`` is a :class:`_Token`, the stand-in for upstream's ``Symbol``; the type-only ``valueType``
marker has no runtime counterpart.
"""

from __future__ import annotations

import asyncio
import inspect
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from misaka.utils.js import UNDEFINED, AbortController, AbortSignal, DOMException


class _Token:
    """The unique identity of a context key; its ``description`` names it in a context's text form."""

    __slots__ = ("description",)

    def __init__(self, description: str | None) -> None:
        self.description = description

    def __repr__(self) -> str:
        return f"Symbol({self.description or ''})"


@dataclass(frozen=True, slots=True)
class ContextKey[T]:
    """Typed identity for one value carried by a :class:`Context`."""

    token: _Token


class Context(ABC):
    """Immutable invocation-scoped values passed explicitly through operations."""

    @property
    @abstractmethod
    def abortSignal(self) -> AbortSignal | None:
        """The signal that cancels the invocation, or ``None``."""

    @abstractmethod
    def value[T](self, key: ContextKey[T]) -> T:
        """The value for ``key``, or ``UNDEFINED`` when the context holds none."""

    @abstractmethod
    def __str__(self) -> str: ...

ABORT_SIGNAL_CONTEXT_KEY: ContextKey[AbortSignal | None] = ContextKey(_Token("chord.abortSignal"))


class BaseContext(Context):
    @property
    def abortSignal(self) -> AbortSignal | None:
        signal = self.value(ABORT_SIGNAL_CONTEXT_KEY)
        return None if signal is UNDEFINED else signal


class EmptyContext(BaseContext):
    def __init__(self, name: str) -> None:
        self._name = name

    def value[T](self, _key: ContextKey[T]) -> T:
        return UNDEFINED  # type: ignore[return-value]

    def __str__(self) -> str:
        return self._name


class ContextValue[T](BaseContext):
    def __init__(self, parent: Context, key: ContextKey[T], value: T) -> None:
        self._parent = parent
        self._key = key
        self._value = value

    def value[Value](self, key: ContextKey[Value]) -> Value:
        if key.token is self._key.token:
            return self._value  # type: ignore[return-value]
        return self._parent.value(key)

    def __str__(self) -> str:
        description = self._key.token.description
        return f"{self._parent}.WithValue({'anonymous' if description is None else description})"


BACKGROUND_CONTEXT: Context = EmptyContext("[Context BACKGROUND_CONTEXT]")
TODO_CONTEXT: Context = EmptyContext("[Context TODO_CONTEXT]")


def createContextKey[T](description: str) -> ContextKey[T]:
    return ContextKey(_Token(description))


def withContextValue[T](key: ContextKey[T], value: T, parent: Context) -> Context:
    """Derive a context containing one additional or replaced value."""
    return ContextValue(parent, key, value)


def withAbortSignal(signal: AbortSignal, context: Context) -> Context:
    """Derive a context cancelled by either the parent signal or the supplied signal. The parent context
    remains unchanged."""
    parentSignal = context.abortSignal
    combined = signal if parentSignal is None else AbortSignal.any([parentSignal, signal])
    return withContextValue(ABORT_SIGNAL_CONTEXT_KEY, combined, context)


def withoutAbortSignal(context: Context) -> Context:
    """Derive a context retaining all values except caller cancellation. Intended for mandatory cleanup only."""
    return withContextValue(ABORT_SIGNAL_CONTEXT_KEY, UNDEFINED, context)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class CancelableContext:
    context: Context
    cancel: Callable[..., None]


def withCancel(context: Context) -> CancelableContext:
    """Derive an independently cancellable child context."""
    controller = AbortController()
    return CancelableContext(
        context=withAbortSignal(controller.signal, context),
        cancel=lambda reason=UNDEFINED: controller.abort(reason),
    )


async def awaitWithContext[T](promise: Awaitable[T], context: Context) -> T:
    """Observe an awaitable until it settles or the invocation is cancelled. Cancellation raises only for
    this waiter; it does not cancel the underlying awaitable."""
    signal = context.abortSignal
    if signal is None:
        return await promise
    if signal.aborted:
        if inspect.iscoroutine(promise):
            promise.close()
        raise abortError(signal)
    loop = asyncio.get_running_loop()
    work = asyncio.ensure_future(promise)
    waiter: asyncio.Future[T] = loop.create_future()

    def onAbort() -> None:
        if not waiter.done():
            waiter.set_exception(abortError(signal))

    def settled(done: asyncio.Future[T]) -> None:
        signal.removeEventListener("abort", onAbort)
        if waiter.done():
            # Nobody is waiting for the outcome any more; observe a failure so it is not left "never retrieved".
            if not done.cancelled():
                done.exception()
            return
        if done.cancelled():
            waiter.cancel()
        elif (error := done.exception()) is not None:
            waiter.set_exception(error)
        else:
            waiter.set_result(done.result())

    signal.addEventListener("abort", onAbort, once=True)
    work.add_done_callback(settled)
    return await waiter


def abortError(signal: Any) -> BaseException:
    reason = signal.reason
    return reason if isinstance(reason, BaseException) else DOMException("The operation was aborted", "AbortError")


__all__ = [
    "ABORT_SIGNAL_CONTEXT_KEY",
    "BACKGROUND_CONTEXT",
    "TODO_CONTEXT",
    "CancelableContext",
    "Context",
    "ContextKey",
    "awaitWithContext",
    "createContextKey",
    "withAbortSignal",
    "withCancel",
    "withContextValue",
    "withoutAbortSignal",
]

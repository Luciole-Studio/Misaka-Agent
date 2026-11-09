"""Translated from pi's ``packages/ai/src/utils/event-stream.ts`` (v1.1.0).

MISAKA fork: a waiting consumer is an ``asyncio.Future``; a consumer cancelled while it waits leaves a cancelled future
behind, which ``push`` skips. The final-result future is made on first use, because a future needs an event loop. The
live error cause, the producer task and ``spawn_stream_task`` at the end are MISAKA additions.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from misaka.ai.types import AssistantMessage, AssistantMessageEvent
from misaka.utils.js import UNDEFINED, jsRound


class _FifoQueue[T]:
    def __init__(self) -> None:
        self._incoming: list[T] = []
        self._outgoing: list[T] = []

    @property
    def length(self) -> int:
        return len(self._incoming) + len(self._outgoing)

    def enqueue(self, value: T) -> None:
        self._incoming.append(value)

    def dequeue(self) -> T | None:
        if len(self._outgoing) == 0:
            while len(self._incoming) > 0:
                self._outgoing.append(self._incoming.pop())
        return self._outgoing.pop() if self._outgoing else None


_detached_loop: asyncio.AbstractEventLoop | None = None


def _detached_future_loop() -> asyncio.AbstractEventLoop:
    """MISAKA fork: the one loop backing futures built outside any running loop. A few synchronous call paths construct
    already-complete streams; their futures are resolved and read in place, so they share one detached loop instead of
    leaking a new one each time."""
    global _detached_loop
    if _detached_loop is None or _detached_loop.is_closed():
        _detached_loop = asyncio.new_event_loop()
    return _detached_loop


class EventStream[T, R]:
    """Generic event stream class for async iteration"""

    def __init__(self, isComplete: Callable[[T], bool], extractResult: Callable[[T], R]) -> None:
        self._queue: _FifoQueue[T] = _FifoQueue()
        self._waiting: _FifoQueue[asyncio.Future[tuple[Any, bool]]] = _FifoQueue()
        self._done = False
        self._finalResultPromise: asyncio.Future[R] | None = None
        self._isComplete = isComplete
        self._extractResult = extractResult

    def _resolveFinalResult(self, result: R) -> None:
        promise = self._finalResult()
        if not promise.done():
            promise.set_result(result)

    def push(self, event: T) -> None:
        if self._done:
            return

        if self._isComplete(event):
            self._done = True
            self._resolveFinalResult(self._extractResult(event))

        # Deliver to waiting consumer or queue it
        waiter = self._dequeueWaiter()
        if waiter is not None:
            waiter.set_result((event, False))
        else:
            self._queue.enqueue(event)

    def end(self, result: Any = UNDEFINED) -> None:
        self._done = True
        if result is not UNDEFINED:
            self._resolveFinalResult(result)
        # Notify all waiting consumers that we're done
        while self._waiting.length > 0:
            waiter = self._waiting.dequeue()
            if waiter is not None and not waiter.done():
                waiter.set_result((None, True))

    def __aiter__(self) -> AsyncIterator[T]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[T]:
        while True:
            if self._queue.length > 0:
                yield self._queue.dequeue()  # type: ignore[misc]
            elif self._done:
                return
            else:
                waiter: asyncio.Future[tuple[Any, bool]] = asyncio.get_running_loop().create_future()
                self._waiting.enqueue(waiter)
                value, done = await waiter
                if done:
                    return
                yield value

    def result(self) -> asyncio.Future[R]:
        return self._finalResult()

    def _finalResult(self) -> asyncio.Future[R]:
        """MISAKA fork: ``finalResultPromise``, made on first use."""
        if self._finalResultPromise is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = _detached_future_loop()
            self._finalResultPromise = loop.create_future()
        return self._finalResultPromise

    def _dequeueWaiter(self) -> asyncio.Future[tuple[Any, bool]] | None:
        """MISAKA fork: the next waiter still waiting; a consumer cancelled while it waited left a cancelled one."""
        while self._waiting.length > 0:
            waiter = self._waiting.dequeue()
            if waiter is not None and not waiter.done():
                return waiter
        return None


class AssistantMessageEventStream(EventStream[AssistantMessageEvent, AssistantMessage]):
    """Event stream of one assistant response. It also times the response: the final message (`done` or `error` event,
    or the result passed to `end()`) gets `durationMs`, measured with a monotonic clock from the stream's creation,
    unless the message already has one or its `timestamp` predates the stream. A stream that forwards a response which
    started elsewhere, such as a deferred result fetched later, therefore leaves it untimed."""

    def __init__(self) -> None:
        self._startedAt = time.time_ns() // 1_000_000
        self._startedAtMonotonic = time.perf_counter()
        # MISAKA fork: live control data only, never serialized into the message or session: the exception behind an
        # `error` event, and the producer task `settled()` joins.
        self.error_cause: BaseException | None = None
        self._producer: asyncio.Task[Any] | None = None

        def extractResult(event: AssistantMessageEvent) -> AssistantMessage:
            if event.type == "done":
                return event.message
            if event.type == "error":
                return event.error
            raise RuntimeError("Unexpected event type for final result")

        super().__init__(lambda event: event.type in ("done", "error"), extractResult)

    def push(self, event: AssistantMessageEvent, *, cause: BaseException | None = None) -> None:
        if event.type == "done":
            self._time(event.message)
        elif event.type == "error":
            self._time(event.error)
            # MISAKA fork: keep the live exception behind the error.
            if not self._done:
                self.error_cause = cause
        super().push(event)

    def end(self, result: Any = UNDEFINED) -> None:
        if result is not UNDEFINED and result is not None:
            self._time(result)
        super().end(result)

    def _time(self, message: AssistantMessage) -> None:
        if self._done or message.durationMs is not None or message.timestamp < self._startedAt:
            return
        message.durationMs = max(0, jsRound((time.perf_counter() - self._startedAtMonotonic) * 1000))

    async def settled(self) -> None:
        """MISAKA fork: join this stream's producer, including its transport finalizers.

        A done/error event resolves result() before the provider's finally block. Short-lived auxiliary event loops
        must drain that owner before closing.
        """
        if self._producer is not None and self._producer is not asyncio.current_task():
            from misaka.utils.async_lifecycle import settle

            _, cancelled = await settle(self._producer)
            if cancelled is not None:
                raise cancelled


def createAssistantMessageEventStream() -> AssistantMessageEventStream:
    """Factory function for AssistantMessageEventStream (for use in extensions)"""
    return AssistantMessageEventStream()


# MISAKA fork: provider tasks need a strong reference while they run.
_live_stream_tasks: set[asyncio.Task[Any]] = set()


def spawn_stream_task(coro: Any, *, stream: AssistantMessageEventStream | None = None) -> asyncio.Task[Any]:
    """MISAKA fork: run a provider's streaming body, holding the task until it finishes.

    The event loop keeps only a weak reference to a task, so a bare ``asyncio.create_task(run())`` can be collected
    mid-response. The reference lives here until the task completes.
    """
    task = asyncio.create_task(coro)
    if stream is not None:
        stream._producer = task
    _live_stream_tasks.add(task)
    task.add_done_callback(_live_stream_tasks.discard)
    return task


__all__ = ["AssistantMessageEventStream", "EventStream", "createAssistantMessageEventStream"]

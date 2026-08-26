"""Async event stream primitives used by provider streaming adapters."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import TypeVar, cast

from misaka.ai.types import AssistantMessage, AssistantMessageEvent

TEvent = TypeVar("TEvent")
TResult = TypeVar("TResult")

_END_OF_STREAM = object()
_UNSET = object()


class EventStream[TEvent, TResult]:
    def __init__(
        self,
        is_complete: Callable[[TEvent], bool],
        extract_result: Callable[[TEvent], TResult],
    ) -> None:
        self._queue: asyncio.Queue[object] = asyncio.Queue()
        self._done = False
        self._is_complete = is_complete
        self._extract_result = extract_result
        self._result_future: asyncio.Future[TResult] | None = None
        self._waiting_consumers = 0

    def push(self, event: TEvent) -> None:
        if self._done:
            return

        if self._is_complete(event):
            self._done = True
            result_future = self._ensure_result_future()
            if not result_future.done():
                result_future.set_result(self._extract_result(event))

        self._queue.put_nowait(event)

    def end(self, result: TResult | object = _UNSET) -> None:
        self._done = True
        result_future = self._ensure_result_future()
        if result is not _UNSET and not result_future.done():
            result_future.set_result(cast(TResult, result))
        for _ in range(max(1, self._waiting_consumers)):
            self._queue.put_nowait(_END_OF_STREAM)

    def result(self) -> asyncio.Future[TResult]:
        return self._ensure_result_future()

    def __aiter__(self) -> AsyncIterator[TEvent]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[TEvent]:
        while True:
            if self._queue.empty() and self._done:
                return

            self._waiting_consumers += 1
            try:
                item = await self._queue.get()
            finally:
                self._waiting_consumers -= 1

            if item is _END_OF_STREAM:
                return
            yield cast(TEvent, item)

    def _ensure_result_future(self) -> asyncio.Future[TResult]:
        if self._result_future is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # Streams are normally created inside an active event loop, but a few
                # synchronous call paths construct already-complete streams for tests
                # and lightweight wrappers. In that case, use an isolated loop-backed
                # future rather than the deprecated implicit current-loop lookup.
                loop = asyncio.new_event_loop()
            self._result_future = loop.create_future()
        return self._result_future


class AssistantMessageEventStream(EventStream[AssistantMessageEvent, AssistantMessage]):
    def __init__(self) -> None:
        super().__init__(
            lambda event: event.type in {"done", "error"},
            lambda event: event.message if event.type == "done" else event.error,
        )


def create_assistant_message_event_stream() -> AssistantMessageEventStream:
    return AssistantMessageEventStream()


__all__ = [
    "AssistantMessageEventStream",
    "EventStream",
    "create_assistant_message_event_stream",
]

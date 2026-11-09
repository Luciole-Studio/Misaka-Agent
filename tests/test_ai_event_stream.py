"""Translated from pi's ``packages/ai/test/event-stream.test.ts`` (v1.1.0).

Regression tests for https://github.com/earendil-works/pi/issues/9055. MISAKA fork: an async generator starts on its
first ``__anext__`` await, not on the call, so a test that needs consumers already waiting runs their ``next()`` as
tasks and lets them reach the wait first.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from misaka.ai.types import AssistantMessage, DoneEvent, ErrorEvent, Usage, UsageCost
from misaka.ai.utils.event_stream import AssistantMessageEventStream, EventStream


def _nowMs() -> int:
    return time.time_ns() // 1_000_000


async def test_drains_buffered_events_in_order_and_ignores_events_pushed_after_completion():
    stream: EventStream[int, int] = EventStream(lambda event: event == 3, lambda event: event)
    stream.push(1)
    stream.push(2)
    stream.push(3)
    stream.push(4)

    assert await stream.result() == 3

    events = [event async for event in stream]
    assert events == [1, 2, 3]


async def test_preserves_order_when_events_arrive_after_buffered_draining_starts():
    stream: EventStream[int, int] = EventStream(lambda event: False, lambda event: event)
    stream.push(1)
    stream.push(2)

    iterator = stream.__aiter__()
    assert await iterator.__anext__() == 1

    stream.push(3)
    assert await iterator.__anext__() == 2
    assert await iterator.__anext__() == 3

    stream.end(3)
    with pytest.raises(StopAsyncIteration):
        await iterator.__anext__()


async def test_delivers_events_to_waiting_consumers_in_registration_order():
    stream: EventStream[int, int] = EventStream(lambda event: False, lambda event: event)
    firstIterator = stream.__aiter__()
    secondIterator = stream.__aiter__()
    firstEvent = asyncio.ensure_future(firstIterator.__anext__())
    await asyncio.sleep(0)
    secondEvent = asyncio.ensure_future(secondIterator.__anext__())
    await asyncio.sleep(0)

    stream.push(1)
    stream.push(2)

    assert await firstEvent == 1
    assert await secondEvent == 2


async def test_drains_buffered_events_after_end_and_resolves_the_explicit_result():
    stream: EventStream[int, str] = EventStream(lambda event: False, lambda event: str(event))
    stream.push(1)
    stream.push(2)
    stream.end("complete")

    assert await stream.result() == "complete"

    events = [event async for event in stream]
    assert events == [1, 2]


async def test_wakes_all_waiting_consumers_when_ended_without_a_result():
    stream: EventStream[int, int] = EventStream(lambda event: False, lambda event: event)
    firstIterator = stream.__aiter__()
    secondIterator = stream.__aiter__()
    firstEvent = asyncio.ensure_future(firstIterator.__anext__())
    secondEvent = asyncio.ensure_future(secondIterator.__anext__())
    await asyncio.sleep(0)

    stream.end()

    with pytest.raises(StopAsyncIteration):
        await firstEvent
    with pytest.raises(StopAsyncIteration):
        await secondEvent


def message(timestamp: int, durationMs: int | None = None) -> AssistantMessage:
    return AssistantMessage(
        role="assistant",
        content=[],
        api="openai-responses",
        provider="openai",
        model="m",
        usage=Usage(
            input=0,
            output=0,
            cacheRead=0,
            cacheWrite=0,
            totalTokens=0,
            cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
        ),
        stopReason="stop",
        timestamp=timestamp,
        **({} if durationMs is None else {"durationMs": durationMs}),
    )


async def test_sets_duration_ms_on_the_final_done_or_error_message_of_a_response_it_saw_start():
    done = AssistantMessageEventStream()
    answer = message(_nowMs())
    await asyncio.sleep(0.02)
    done.push(DoneEvent(reason="stop", message=answer))
    assert answer.durationMs is not None and answer.durationMs >= 15
    assert (await done.result()).durationMs == answer.durationMs

    failed = AssistantMessageEventStream()
    error = message(_nowMs()).model_copy(update={"stopReason": "error"})
    failed.push(ErrorEvent(reason="error", error=error))
    assert error.durationMs is not None and error.durationMs >= 0

    ended = AssistantMessageEventStream()
    result = message(_nowMs())
    ended.end(result)
    assert result.durationMs is not None and result.durationMs >= 0


async def test_keeps_an_existing_duration_so_a_forwarding_stream_keeps_the_inner_measurement():
    outer = AssistantMessageEventStream()
    await asyncio.sleep(0.02)
    inner = AssistantMessageEventStream()
    answer = message(_nowMs())
    inner.push(DoneEvent(reason="stop", message=answer))
    measured = answer.durationMs
    outer.push(DoneEvent(reason="stop", message=answer))
    assert answer.durationMs == measured
    assert measured is not None and measured < 20

    preset = message(_nowMs(), 1234)
    AssistantMessageEventStream().push(DoneEvent(reason="stop", message=preset))
    assert preset.durationMs == 1234


def test_leaves_a_message_untimed_when_it_started_before_the_stream_such_as_a_fetched_deferred_result():
    fetched = message(_nowMs() - 60_000)
    AssistantMessageEventStream().push(DoneEvent(reason="stop", message=fetched))
    assert fetched.durationMs is None


def test_does_not_time_a_message_pushed_after_the_stream_completed():
    stream = AssistantMessageEventStream()
    stream.push(DoneEvent(reason="stop", message=message(_nowMs())))
    late = message(_nowMs())
    stream.push(DoneEvent(reason="stop", message=late))
    assert late.durationMs is None

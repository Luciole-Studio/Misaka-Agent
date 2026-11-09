"""pi's ``packages/ai/src/api/lazy.ts`` (v1.1.0) has no unit test of its own; these pin its contract: setup
failures end the stream with an error stamped at the request start (AI-CU-9), the inner stream is forwarded with its
result, and the deferred methods exist only when advertised."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from misaka.ai.api.lazy import lazyApi, lazyStream
from misaka.ai.providers.faux import fauxAssistantMessage
from misaka.ai.types import DoneEvent, Model, ModelCost, StartEvent
from misaka.ai.utils.event_stream import AssistantMessageEventStream

MODEL = Model(
    id="m",
    name="M",
    api="faux",
    provider="faux",
    baseUrl="",
    reasoning=False,
    input=["text"],
    cost=ModelCost(input=0, output=0, cacheRead=0, cacheWrite=0),
    contextWindow=1000,
    maxTokens=100,
)


async def test_a_setup_failure_ends_the_stream_with_an_error_stamped_at_the_request_start():
    before = time.time_ns() // 1_000_000

    async def setup():
        await asyncio.sleep(0.02)
        raise RuntimeError("module failed to load")

    stream = lazyStream(MODEL, setup)
    events = [event async for event in stream]
    result = await stream.result()

    assert [event.type for event in events] == ["error"]
    assert result.stopReason == "error"
    assert result.errorMessage == "module failed to load"
    assert before <= result.timestamp < before + 20


async def test_the_inner_stream_is_forwarded_and_its_result_ends_the_outer_one():
    message = fauxAssistantMessage("hello")

    async def setup():
        inner = AssistantMessageEventStream()
        inner.push(StartEvent(partial=message))
        inner.push(DoneEvent(reason="stop", message=message))
        inner.end()
        return inner

    stream = lazyStream(MODEL, setup)
    events = [event.type async for event in stream]

    assert events == ["start", "done"]
    assert await stream.result() is message


async def test_deferred_methods_exist_only_when_advertised():
    async def load():
        return SimpleNamespace(stream=None, streamSimple=None)

    plain = lazyApi(load)
    deferred = lazyApi(load, {"fetchDeferred": True, "cancelDeferred": True})

    assert not hasattr(plain, "fetchDeferred") and not hasattr(plain, "cancelDeferred")
    assert callable(deferred.fetchDeferred) and callable(deferred.cancelDeferred)
    result = await deferred.fetchDeferred(MODEL, {"id": "h"}).result()
    assert result.errorMessage == "API does not support deferred responses"

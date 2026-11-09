"""Translated from pi's ``packages/ai/src/api/lazy.ts`` (v1.1.0).

MISAKA fork: the promise chain behind the returned stream is a task held by ``spawn_stream_task``; an error event
carries its live exception (``cause=``); and the outer producer joins the inner stream's producer
(``AssistantMessageEventStream.settled``), so a short-lived event loop can drain both before closing. The
``ProviderStreams`` object is a ``SimpleNamespace``, the plain object upstream builds. ``lazyApi``'s two streams meter
each request against the token cap (``api/_meter.py``).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterable, Awaitable, Callable
from types import SimpleNamespace
from typing import Any, TypedDict

from misaka.ai.api._meter import markMetered, metered
from misaka.ai.types import (
    AssistantMessage,
    AssistantMessageEvent,
    ErrorEvent,
    Model,
    ProviderStreams,
)
from misaka.ai.utils.diagnostics import formatThrownValue
from misaka.ai.utils.event_stream import AssistantMessageEventStream, spawn_stream_task
from misaka.utils.js import UNDEFINED, jsString


def _createSetupErrorMessage(model: Model, error: Any, timestamp: int) -> AssistantMessage:
    return AssistantMessage(
        role="assistant",
        content=[],
        api=model.api,
        provider=model.provider,
        model=model.id,
        usage={
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 0,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
        },
        stopReason="error",
        errorMessage=formatThrownValue(error) if isinstance(error, Exception) else jsString(error),
        timestamp=timestamp,
    )


def _hasResult(source: AsyncIterable[AssistantMessageEvent]) -> bool:
    return callable(getattr(source, "result", None))


async def _forwardStream(
    target: AssistantMessageEventStream,
    source: AsyncIterable[AssistantMessageEvent],
) -> None:
    async for event in source:
        # MISAKA fork: the inner stream's live exception rides along with its error event.
        target.push(event, cause=getattr(source, "error_cause", None))
    target.end(await source.result() if _hasResult(source) else UNDEFINED)  # type: ignore[attr-defined]


def lazyStream(
    model: Model,
    setup: Callable[[], Awaitable[AsyncIterable[AssistantMessageEvent]]],
) -> AssistantMessageEventStream:
    """Returns a stream synchronously while running async setup (auth resolution,
    lazy module loading) behind it. Setup failures terminate the stream with an
    error event."""
    startedAt = time.time_ns() // 1_000_000
    outer = AssistantMessageEventStream()

    async def run() -> None:
        inner: Any = None
        try:
            inner = await setup()
            await _forwardStream(outer, inner)
        except Exception as error:  # noqa: BLE001 - `.catch(...)` turns any failure into the stream's error
            message = _createSetupErrorMessage(model, error, startedAt)
            outer.push(ErrorEvent(reason="error", error=message), cause=error)
            outer.end(message)
        finally:
            # MISAKA fork: join the inner producer, transport finalizers included.
            settled = getattr(inner, "settled", None)
            if settled is not None:
                await settled()

    spawn_stream_task(run(), stream=outer)
    return outer


class LazyApiCapabilities(TypedDict, total=False):
    """Wraps a dynamically imported API implementation module as `ProviderStreams`.
    The module loads on first stream call; the host's import cache deduplicates
    loads. Load failures terminate the returned stream with an error event."""

    fetchDeferred: bool
    cancelDeferred: bool


def lazyApi(
    load: Callable[[], Awaitable[ProviderStreams]],
    capabilities: LazyApiCapabilities | None = None,
) -> ProviderStreams:
    async def stream(implementation: str, model: Model, context: Any, options: Any) -> Any:
        return getattr(await load(), implementation)(model, context, options)

    # MISAKA fork: each request is metered against the token cap here (`api/_meter.py`).
    api: Any = SimpleNamespace(
        stream=markMetered(
            lambda model, context, options=None: metered(
                model, context, options, lambda sent: lazyStream(model, lambda: stream("stream", model, context, sent))
            )
        ),
        streamSimple=markMetered(
            lambda model, context, options=None: metered(
                model,
                context,
                options,
                lambda sent: lazyStream(model, lambda: stream("streamSimple", model, context, sent)),
            )
        ),
    )

    if capabilities and capabilities.get("fetchDeferred"):

        def fetchDeferred(model: Model, handle: Any, options: Any = None) -> AssistantMessageEventStream:
            async def setup() -> Any:
                implementation = await load()
                if not getattr(implementation, "fetchDeferred", None):
                    raise RuntimeError("API does not support deferred responses")
                return implementation.fetchDeferred(model, handle, options)  # type: ignore[attr-defined]

            return lazyStream(model, setup)

        api.fetchDeferred = fetchDeferred
    if capabilities and capabilities.get("cancelDeferred"):

        async def cancelDeferred(model: Model, handle: Any, options: Any = None) -> None:
            implementation = await load()
            if not getattr(implementation, "cancelDeferred", None):
                raise RuntimeError("API cannot cancel deferred responses")
            await implementation.cancelDeferred(model, handle, options)  # type: ignore[attr-defined]

        api.cancelDeferred = cancelDeferred

    return api  # type: ignore[no-any-return]


__all__ = ["LazyApiCapabilities", "lazyApi", "lazyStream"]

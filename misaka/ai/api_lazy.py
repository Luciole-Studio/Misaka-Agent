"""Lazy stream construction, translated from pi's ``packages/ai/src/api/lazy.ts``.

A stream has to be returned *synchronously* -- the caller wires it into the UI before
anything is known -- while the work that produces it (auth resolution, importing a
provider module) is async. ``lazy_stream`` is that seam: it hands back an empty stream
immediately and fills it from behind. Setup that fails does not raise into a caller that
has already moved on; it terminates the stream with an error event, which is where the
UI is already looking.

``ai/providers/register_builtins.py`` builds every built-in provider's streams through
``lazy_api`` below. It used to carry a second copy of this idea, one function per stream
kind; upstream has one ``lazy.ts`` that every provider goes through, and so does this now.
See ``_forward_stream`` for why upstream's ``result()`` forwarding does not survive the
translation.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterable, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from misaka.ai.types import AssistantMessage, DeferredHandle, ErrorEvent, Model
from misaka.ai.utils.event_stream import AssistantMessageEventStream, spawn_stream_task


def _error_text(error: Any) -> str:
    message = getattr(error, "message", None)
    return message if isinstance(message, str) else str(error)


def _create_setup_error_message(model: Model, error: Any) -> AssistantMessage:
    """The shape ``register_builtins`` already produces, so both paths fail alike."""
    return AssistantMessage(
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
        errorMessage=_error_text(error),
        timestamp=time.time_ns() // 1_000_000,
    )


async def _forward_stream(target: AssistantMessageEventStream, source: AsyncIterable[Any]) -> None:
    """Forward every event, then close.

    Upstream ends the outer stream with ``await source.result()``, guarded by a check for
    whether the source has a ``result`` method at all (``api/lazy.ts:25``). That guard
    does not translate: every misaka stream is an ``EventStream``, so ``result()`` is
    always there. Forwarding is enough on its own -- pushing a ``done``/``error`` event
    resolves the outer result (``utils/event_stream.py``) -- while awaiting the inner
    ``result()`` would hang on a source that finishes through a bare ``end()``, which
    leaves that future unresolved.
    """
    async for event in source:
        target.push(event)
    target.end()


def _fail(outer: AssistantMessageEventStream, model: Model, error: Any) -> None:
    message = _create_setup_error_message(model, error)
    outer.push(ErrorEvent(reason="error", error=message))
    outer.end(message)


def lazy_stream(
    model: Model,
    setup: Callable[[], Awaitable[AsyncIterable[Any]]],
) -> AssistantMessageEventStream:
    """Return a stream now; run ``setup`` behind it and forward what it produces."""
    outer = AssistantMessageEventStream()

    async def run() -> None:
        try:
            inner = await setup()
        except Exception as error:  # noqa: BLE001 - any setup failure becomes a stream error
            _fail(outer, model, error)
            return
        try:
            await _forward_stream(outer, inner)
        except Exception as error:  # noqa: BLE001 - the inner stream broke after setup
            # The caller is watching the stream, not awaiting a raise, so a break here
            # ends the same way a setup failure does.
            _fail(outer, model, error)

    spawn_stream_task(run())
    return outer


@dataclass(frozen=True, slots=True)
class LazyApiCapabilities:
    """Which optional methods the wrapped module is known to provide.

    Upstream only attaches ``fetchDeferred``/``cancelDeferred`` to the lazy wrapper when
    the module actually has them, because their presence is how a caller *tests* for
    deferred-response support (``types.ts``: "Lazy wrappers and provider implementations
    export deferred-response methods"). Attaching them unconditionally would make every
    provider claim a capability most do not have.
    """

    fetchDeferred: bool = False
    cancelDeferred: bool = False


class LazyApi:
    """A provider module wrapped so it loads on first use.

    ``load`` is called at most as often as the caller streams; caching it is the caller's
    business, which is how ``providers/register_builtins.py`` memoises the import task.
    """

    __slots__ = ("_load", "cancelDeferred", "fetchDeferred")

    def __init__(
        self,
        load: Callable[[], Awaitable[Any]],
        capabilities: LazyApiCapabilities | None = None,
    ) -> None:
        self._load = load
        capabilities = capabilities or LazyApiCapabilities()
        # Bound only when advertised, so `hasattr` stays the capability test upstream
        # makes it: an API that cannot defer must not answer to these names.
        self.fetchDeferred = self._fetch_deferred if capabilities.fetchDeferred else None
        self.cancelDeferred = self._cancel_deferred if capabilities.cancelDeferred else None
        if self.fetchDeferred is None:
            del self.fetchDeferred
        if self.cancelDeferred is None:
            del self.cancelDeferred

    def stream(self, model: Model, context: Any, options: Any = None) -> AssistantMessageEventStream:
        return lazy_stream(model, lambda: self._call("stream", model, context, options))

    def streamSimple(self, model: Model, context: Any, options: Any = None) -> AssistantMessageEventStream:
        return lazy_stream(model, lambda: self._call("streamSimple", model, context, options))

    async def _call(self, name: str, *args: Any) -> AsyncIterable[Any]:
        module = await self._load()
        return getattr(module, name)(*args)

    def _fetch_deferred(
        self, model: Model, handle: DeferredHandle, options: Any = None
    ) -> AssistantMessageEventStream:
        async def setup() -> AsyncIterable[Any]:
            module = await self._load()
            fetch = getattr(module, "fetchDeferred", None)
            if fetch is None:
                raise RuntimeError("API does not support deferred responses")
            return fetch(model, handle, options)

        return lazy_stream(model, setup)

    async def _cancel_deferred(
        self, model: Model, handle: DeferredHandle, options: Any = None
    ) -> None:
        module = await self._load()
        cancel = getattr(module, "cancelDeferred", None)
        if cancel is None:
            raise RuntimeError("API cannot cancel deferred responses")
        await cancel(model, handle, options)


def lazy_api(
    load: Callable[[], Awaitable[Any]],
    capabilities: LazyApiCapabilities | None = None,
) -> LazyApi:
    """Wrap a lazily-imported provider module as a streams object."""
    return LazyApi(load, capabilities)


__all__ = ["LazyApi", "LazyApiCapabilities", "lazy_api", "lazy_stream"]

"""MISAKA fork (no upstream file): where a model request is metered against the token cap.

Every built-in API is built by ``lazyApi`` (``api/lazy.py``), and built-in providers call those streams directly, as
pi's do; extension and test APIs reach requests through the api registry (``api_registry.py``). Both paths ask this
module: ``lazyApi`` meters the streams it builds and marks them, and the registry meters only streams that are not
marked, so a request is metered once whichever way it came. The meter is ``meter(model, context, options, send)``,
``send(options)`` the API's own call; ``misaka.core.platform.metering`` installs it. Without one installed the streams
are pi's.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from misaka.ai.runtime_observation import (
    has_observation_sink,
    observation_context,
    observe,
    request_identity,
)

_request_meter = None


def set_request_meter(meter) -> None:
    global _request_meter
    _request_meter = meter


def metered(model: Any, context: Any, options: Any, send: Callable[[Any], Any]) -> Any:
    meter = _request_meter
    if not has_observation_sink() or request_identity() is not None:
        return send(options) if meter is None else meter(model, context, options, send)
    # Auxiliary calls (for example a Research planner call outside AgentSession)
    # also receive an identity. The normal agent loop already supplied one.
    request_id = uuid4().hex
    started = time.perf_counter()
    with observation_context(request_id=request_id, auxiliary=True):
        observe("model_started", provider=model.provider, model=model.id)
        try:
            stream = send(options) if meter is None else meter(model, context, options, send)
        except Exception:
            observe("model_finished", status="error", duration_ms=round((time.perf_counter() - started) * 1000))
            raise

        def finished(future):
            duration = round((time.perf_counter() - started) * 1000)
            if future.cancelled() or future.exception() is not None:
                observe("model_finished", status="aborted" if future.cancelled() else "error", duration_ms=duration)
                return
            message = future.result()
            if message is None:
                observe("model_finished", status="unknown", duration_ms=duration)
                return
            usage = message.usage
            observe("model_finished", status=message.stopReason, duration_ms=duration,
                    usage={"input": usage.input, "output": usage.output, "cache_read": usage.cacheRead,
                           "cache_write": usage.cacheWrite, "total": usage.totalTokens} if usage.totalTokens else None)

        stream.result().add_done_callback(finished)
        return stream


def markMetered[F](stream: F) -> F:
    """Mark ``stream`` as metering its own requests."""
    stream.__misaka_metered__ = True  # type: ignore[attr-defined]
    return stream


def isMetered(stream: Any) -> bool:
    return getattr(stream, "__misaka_metered__", False) is True


__all__ = ["isMetered", "markMetered", "metered", "set_request_meter"]

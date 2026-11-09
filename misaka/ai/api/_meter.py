"""MISAKA fork (no upstream file): where a model request is metered against the token cap.

Every built-in API is built by ``lazyApi`` (``api/lazy.py``), and built-in providers call those streams directly, as
pi's do; extension and test APIs reach requests through the api registry (``api_registry.py``). Both paths ask this
module: ``lazyApi`` meters the streams it builds and marks them, and the registry meters only streams that are not
marked, so a request is metered once whichever way it came. The meter is ``meter(model, context, options, send)``,
``send(options)`` the API's own call; ``misaka.core.platform.metering`` installs it. Without one installed the streams
are pi's.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_request_meter = None


def set_request_meter(meter) -> None:
    global _request_meter
    _request_meter = meter


def metered(model: Any, context: Any, options: Any, send: Callable[[Any], Any]) -> Any:
    meter = _request_meter
    return send(options) if meter is None else meter(model, context, options, send)


def markMetered[F](stream: F) -> F:
    """Mark ``stream`` as metering its own requests."""
    stream.__misaka_metered__ = True  # type: ignore[attr-defined]
    return stream


def isMetered(stream: Any) -> bool:
    return getattr(stream, "__misaka_metered__", False) is True


__all__ = ["isMetered", "markMetered", "metered", "set_request_meter"]

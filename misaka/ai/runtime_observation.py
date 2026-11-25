"""Local metadata observations for the request currently handled by the runtime."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_sink: ContextVar[Callable[[str, dict[str, Any]], None] | None] = ContextVar("runtime_observation_sink", default=None)
_context: ContextVar[dict[str, Any] | None] = ContextVar("runtime_observation_context", default=None)
logger = logging.getLogger(__name__)


@contextmanager
def observation_sink(callback: Callable[[str, dict[str, Any]], None]) -> Iterator[None]:
    token = _sink.set(callback)
    try:
        yield
    finally:
        _sink.reset(token)


@contextmanager
def observation_context(**metadata: Any) -> Iterator[None]:
    token = _context.set({**(_context.get() or {}), **metadata})
    try:
        yield
    finally:
        _context.reset(token)


def has_observation_sink() -> bool:
    return _sink.get() is not None


def request_identity() -> str | None:
    return (_context.get() or {}).get("request_id")


def observe(kind: str, **metadata: Any) -> None:
    callback = _sink.get()
    if callback is not None:
        try:
            callback(kind, {**(_context.get() or {}), **metadata})
        except Exception as error:  # noqa: BLE001 - observation cannot change request behavior
            logger.warning("Runtime observation incomplete (%s)", type(error).__name__)

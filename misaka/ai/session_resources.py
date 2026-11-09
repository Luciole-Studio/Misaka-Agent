"""Translated from pi's ``packages/ai/src/session-resources.ts`` (v1.1.0).

MISAKA fork: the set is an insertion-ordered dict (a Python set has hash order), iterated as a snapshot (a JS Set
tolerates removal while it is iterated); ``AggregateError`` is ``ExceptionGroup``.
"""

from __future__ import annotations

from collections.abc import Callable

type SessionResourceCleanup = Callable[[str | None], None]

_sessionResourceCleanups: dict[SessionResourceCleanup, None] = {}


def registerSessionResourceCleanup(cleanup: SessionResourceCleanup) -> Callable[[], None]:
    _sessionResourceCleanups[cleanup] = None

    def unregister() -> None:
        _sessionResourceCleanups.pop(cleanup, None)

    return unregister


def cleanupSessionResources(sessionId: str | None = None) -> None:
    errors: list[Exception] = []
    for cleanup in tuple(_sessionResourceCleanups):
        try:
            cleanup(sessionId)
        except Exception as error:  # noqa: BLE001 - every cleanup runs; the errors are raised together below
            errors.append(error)
    if len(errors) > 0:
        raise ExceptionGroup("Failed to cleanup session resources", errors)


__all__ = ["SessionResourceCleanup", "cleanupSessionResources", "registerSessionResourceCleanup"]

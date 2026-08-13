"""Session-scoped cleanup hooks used by provider integrations."""

from __future__ import annotations

from collections.abc import Callable

SessionResourceCleanup = Callable[[str | None], None]

_session_resource_cleanups: dict[SessionResourceCleanup, None] = {}


def register_session_resource_cleanup(cleanup: SessionResourceCleanup) -> Callable[[], None]:
    _session_resource_cleanups[cleanup] = None

    def unregister() -> None:
        _session_resource_cleanups.pop(cleanup, None)

    return unregister


def cleanup_session_resources(session_id: str | None = None) -> None:
    errors: list[Exception] = []
    for cleanup in tuple(_session_resource_cleanups.keys()):
        try:
            cleanup(session_id)
        except Exception as error:
            errors.append(error)
    if errors:
        raise ExceptionGroup("Failed to cleanup session resources", errors)


registerSessionResourceCleanup = register_session_resource_cleanup
cleanupSessionResources = cleanup_session_resources

__all__ = [
    "SessionResourceCleanup",
    "registerSessionResourceCleanup",
    "cleanupSessionResources",
]

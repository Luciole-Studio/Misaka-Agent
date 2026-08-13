"""Shared helpers for session repositories and storage adapters."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from misaka.agent.harness.session.uuid import uuidv7
from misaka.agent.harness.types import FileError, Result, SessionError, SessionStorage


def create_session_id() -> str:
    return uuidv7()


def create_timestamp() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def to_session[TMetadata](storage: SessionStorage[TMetadata]):
    from misaka.agent.harness.session.session import Session

    return Session(storage)


def get_file_system_result_or_throw[TValue](result: Result[TValue, FileError], message: str) -> TValue:
    if not result.ok:
        code = "not_found" if result.error.code == "not_found" else "storage"
        raise SessionError(code, f"{message}: {result.error}", result.error)
    return result.value


async def get_entries_to_fork(
    storage: SessionStorage[Any],
    options: dict[str, Any] | None = None,
) -> list[Any]:
    opts = dict(options or {})
    entry_id = opts.get("entryId")
    if not entry_id:
        return await storage.getEntries()
    target = await storage.getEntry(entry_id)
    if target is None:
        raise SessionError("invalid_fork_target", f"Entry {entry_id} not found")

    position = opts.get("position", "before")
    if position == "at":
        effective_leaf_id = _entry_field(target, "id")
    else:
        if _entry_field(target, "type") != "message" or _message_role(_entry_field(target, "message")) != "user":
            raise SessionError("invalid_fork_target", f"Entry {entry_id} is not a user message")
        effective_leaf_id = _entry_field(target, "parentId")
    return await storage.getPathToRoot(effective_leaf_id)


def _entry_field(entry: Any, name: str) -> Any:
    if isinstance(entry, dict):
        return entry.get(name)
    return getattr(entry, name)


def _message_role(message: Any) -> Any:
    if isinstance(message, dict):
        return message.get("role")
    return getattr(message, "role", None)


createSessionId = create_session_id
createTimestamp = create_timestamp
getFileSystemResultOrThrow = get_file_system_result_or_throw
getEntriesToFork = get_entries_to_fork
toSession = to_session

__all__ = [
    "createSessionId",
    "createTimestamp",
    "create_session_id",
    "create_timestamp",
    "getEntriesToFork",
    "getFileSystemResultOrThrow",
    "get_entries_to_fork",
    "get_file_system_result_or_throw",
    "toSession",
    "to_session",
]

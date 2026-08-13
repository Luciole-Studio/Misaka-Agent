"""In-memory session repository implementation."""

from __future__ import annotations

from misaka.agent.harness.session.memory_storage import InMemorySessionStorage
from misaka.agent.harness.session.repo_utils import (
    create_session_id,
    create_timestamp,
    get_entries_to_fork,
    to_session,
)
from misaka.agent.harness.types import SessionError


class InMemorySessionRepo:
    def __init__(self) -> None:
        self._sessions: dict[str, object] = {}

    async def create(self, options: dict[str, str] | None = None):
        opts = dict(options or {})
        session_id = opts["id"] if "id" in opts else create_session_id()
        metadata = {"id": session_id, "createdAt": create_timestamp()}
        storage = InMemorySessionStorage({"metadata": metadata})
        session = to_session(storage)
        self._sessions[metadata["id"]] = session
        return session

    async def open(self, metadata: dict[str, str]):
        session = self._sessions.get(metadata["id"])
        if session is None:
            raise SessionError("not_found", f"Session not found: {metadata['id']}")
        return session

    async def list(self):
        return [await session.getMetadata() for session in self._sessions.values()]

    async def delete(self, metadata: dict[str, str]) -> None:
        self._sessions.pop(metadata["id"], None)

    async def fork(self, sourceMetadata: dict[str, str], options: dict[str, str] | None = None):
        opts = dict(options or {})
        source = await self.open(sourceMetadata)
        forked_entries = await get_entries_to_fork(source.getStorage(), opts)
        session_id = opts["id"] if "id" in opts else create_session_id()
        metadata = {"id": session_id, "createdAt": create_timestamp()}
        storage = InMemorySessionStorage({"metadata": metadata, "entries": forked_entries})
        session = to_session(storage)
        self._sessions[metadata["id"]] = session
        return session


__all__ = ["InMemorySessionRepo"]

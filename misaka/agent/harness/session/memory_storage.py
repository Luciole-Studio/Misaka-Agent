"""In-memory session storage implementation."""

from __future__ import annotations

from typing import Any

from misaka.agent.harness.session.repo_utils import create_timestamp
from misaka.agent.harness.session.uuid import uuidv7
from misaka.agent.harness.types import SessionError


def _update_label_cache(labels_by_id: dict[str, str], entry: Any) -> None:
    if _entry_field(entry, "type") != "label":
        return
    label = _entry_field(entry, "label")
    label = label.strip() if isinstance(label, str) else None
    if label:
        labels_by_id[str(_entry_field(entry, "targetId"))] = label
    else:
        labels_by_id.pop(str(_entry_field(entry, "targetId")), None)


def _build_labels_by_id(entries: list[Any]) -> dict[str, str]:
    labels_by_id: dict[str, str] = {}
    for entry in entries:
        _update_label_cache(labels_by_id, entry)
    return labels_by_id


def _generate_entry_id(by_id: dict[str, Any]) -> str:
    for _ in range(100):
        entry_id = uuidv7()[:8]
        if entry_id not in by_id:
            return entry_id
    return uuidv7()


def _leaf_id_after_entry(entry: Any) -> str | None:
    if _entry_field(entry, "type") == "leaf":
        return _entry_field(entry, "targetId")
    return _entry_field(entry, "id")


def _entry_field(entry: Any, name: str) -> Any:
    if isinstance(entry, dict):
        return entry.get(name)
    return getattr(entry, name)


class InMemorySessionStorage:
    def __init__(self, options: dict[str, Any] | None = None) -> None:
        opts = dict(options or {})
        self._entries = list(opts.get("entries") or [])
        self._by_id = {str(_entry_field(entry, "id")): entry for entry in self._entries}
        self._labels_by_id = _build_labels_by_id(self._entries)
        self._leaf_id: str | None = None
        for entry in self._entries:
            self._leaf_id = _leaf_id_after_entry(entry)
        if self._leaf_id is not None and self._leaf_id not in self._by_id:
            raise SessionError("invalid_session", f"Entry {self._leaf_id} not found")
        self._metadata = opts["metadata"] if "metadata" in opts else {"id": uuidv7(), "createdAt": create_timestamp()}

    async def getMetadata(self):
        return self._metadata

    async def getLeafId(self) -> str | None:
        if self._leaf_id is not None and self._leaf_id not in self._by_id:
            raise SessionError("invalid_session", f"Entry {self._leaf_id} not found")
        return self._leaf_id

    async def setLeafId(self, leafId: str | None) -> None:
        if leafId is not None and leafId not in self._by_id:
            raise SessionError("not_found", f"Entry {leafId} not found")
        entry = {
            "type": "leaf",
            "id": _generate_entry_id(self._by_id),
            "parentId": self._leaf_id,
            "timestamp": create_timestamp(),
            "targetId": leafId,
        }
        self._entries.append(entry)
        self._by_id[entry["id"]] = entry
        self._leaf_id = leafId

    async def createEntryId(self) -> str:
        return _generate_entry_id(self._by_id)

    async def appendEntry(self, entry: Any) -> None:
        self._entries.append(entry)
        self._by_id[str(_entry_field(entry, "id"))] = entry
        _update_label_cache(self._labels_by_id, entry)
        self._leaf_id = _leaf_id_after_entry(entry)

    async def getEntry(self, id: str):
        return self._by_id.get(id)

    async def findEntries(self, type: str) -> list[Any]:
        return [entry for entry in self._entries if _entry_field(entry, "type") == type]

    async def getLabel(self, id: str) -> str | None:
        return self._labels_by_id.get(id)

    async def getPathToRoot(self, leafId: str | None) -> list[Any]:
        if leafId is None:
            return []
        path: list[Any] = []
        current = self._by_id.get(leafId)
        if current is None:
            raise SessionError("not_found", f"Entry {leafId} not found")
        while current is not None:
            path.insert(0, current)
            parent_id = _entry_field(current, "parentId")
            if not parent_id:
                break
            parent = self._by_id.get(str(parent_id))
            if parent is None:
                raise SessionError("invalid_session", f"Entry {parent_id} not found")
            current = parent
        return path

    async def getEntries(self) -> list[Any]:
        return list(self._entries)


__all__ = ["InMemorySessionStorage"]

"""Append-only JSONL-backed session storage."""

from __future__ import annotations

import json
from typing import Any

from misaka.agent.harness.session.repo_utils import get_file_system_result_or_throw
from misaka.agent.harness.session.uuid import uuidv7
from misaka.agent.harness.types import SessionError, to_error


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


def _invalid_session(file_path: str, message: str, cause: Exception | None = None) -> SessionError:
    return SessionError("invalid_session", f"Invalid JSONL session file {file_path}: {message}", cause)


def _invalid_entry(file_path: str, line_number: int, message: str, cause: Exception | None = None) -> SessionError:
    return SessionError("invalid_entry", f"Invalid JSONL session file {file_path}: line {line_number} {message}", cause)


def _parse_header_line(line: str, file_path: str) -> dict[str, Any]:
    try:
        parsed = json.loads(line)
    except Exception as error:
        raise _invalid_session(file_path, "first line is not a valid session header", to_error(error)) from error
    if not isinstance(parsed, dict):
        raise _invalid_session(file_path, "first line is not a valid session header")
    if parsed.get("type") != "session":
        raise _invalid_session(file_path, "first line is not a valid session header")
    if parsed.get("version") != 3:
        raise _invalid_session(file_path, "unsupported session version")
    if not isinstance(parsed.get("id"), str) or not parsed["id"]:
        raise _invalid_session(file_path, "session header is missing id")
    if not isinstance(parsed.get("timestamp"), str) or not parsed["timestamp"]:
        raise _invalid_session(file_path, "session header is missing timestamp")
    if not isinstance(parsed.get("cwd"), str) or not parsed["cwd"]:
        raise _invalid_session(file_path, "session header is missing cwd")
    parent_session = parsed.get("parentSession")
    if parent_session is not None and not isinstance(parent_session, str):
        raise _invalid_session(file_path, "session header parentSession must be a string")
    return {
        "type": "session",
        "version": 3,
        "id": parsed["id"],
        "timestamp": parsed["timestamp"],
        "cwd": parsed["cwd"],
        "parentSession": parent_session,
    }


def _parse_entry_line(line: str, file_path: str, line_number: int) -> dict[str, Any]:
    try:
        parsed = json.loads(line)
    except Exception as error:
        raise _invalid_entry(file_path, line_number, "is not valid JSON", to_error(error)) from error
    if not isinstance(parsed, dict):
        raise _invalid_entry(file_path, line_number, "is not a valid session entry")
    if not isinstance(parsed.get("type"), str):
        raise _invalid_entry(file_path, line_number, "is missing entry type")
    if not isinstance(parsed.get("id"), str) or not parsed["id"]:
        raise _invalid_entry(file_path, line_number, "is missing entry id")
    parent_id = parsed.get("parentId")
    if parent_id is not None and not isinstance(parent_id, str):
        raise _invalid_entry(file_path, line_number, "has invalid parentId")
    if not isinstance(parsed.get("timestamp"), str) or not parsed["timestamp"]:
        raise _invalid_entry(file_path, line_number, "is missing timestamp")
    if parsed["type"] == "leaf":
        target_id = parsed.get("targetId")
        if target_id is not None and not isinstance(target_id, str):
            raise _invalid_entry(file_path, line_number, "has invalid targetId")
    return parsed


def _leaf_id_after_entry(entry: Any) -> str | None:
    if _entry_field(entry, "type") == "leaf":
        return _entry_field(entry, "targetId")
    return _entry_field(entry, "id")


def _header_to_session_metadata(header: dict[str, Any], path: str) -> dict[str, Any]:
    return {
        "id": header["id"],
        "createdAt": header["timestamp"],
        "cwd": header["cwd"],
        "path": path,
        "parentSessionPath": header.get("parentSession"),
    }


async def load_jsonl_session_metadata(fs: Any, file_path: str) -> dict[str, Any]:
    lines = get_file_system_result_or_throw(
        await fs.readTextLines(file_path, {"maxLines": 1}),
        f"Failed to read session header {file_path}",
    )
    line = lines[0] if lines else None
    if line and line.strip():
        return _header_to_session_metadata(_parse_header_line(line, file_path), file_path)
    raise _invalid_session(file_path, "missing session header")


async def _load_jsonl_storage(fs: Any, file_path: str) -> dict[str, Any]:
    content = get_file_system_result_or_throw(await fs.readTextFile(file_path), f"Failed to read session {file_path}")
    lines = [line for line in content.split("\n") if line.strip()]
    if not lines:
        raise _invalid_session(file_path, "missing session header")

    header = _parse_header_line(lines[0], file_path)
    entries: list[Any] = []
    leaf_id: str | None = None
    for index, line in enumerate(lines[1:], start=2):
        entry = _parse_entry_line(line, file_path, index)
        entries.append(entry)
        leaf_id = _leaf_id_after_entry(entry)
    return {"header": header, "entries": entries, "leafId": leaf_id}


class JsonlSessionStorage:
    def __init__(
        self,
        fs: Any,
        file_path: str,
        header: dict[str, Any],
        entries: list[Any],
        leaf_id: str | None,
    ) -> None:
        self._fs = fs
        self._file_path = file_path
        self._metadata = _header_to_session_metadata(header, file_path)
        self._entries = entries
        self._by_id = {str(_entry_field(entry, "id")): entry for entry in entries}
        self._labels_by_id = _build_labels_by_id(entries)
        self._current_leaf_id = leaf_id

    @classmethod
    async def open(cls, fs: Any, file_path: str) -> JsonlSessionStorage:
        loaded = await _load_jsonl_storage(fs, file_path)
        return cls(fs, file_path, loaded["header"], loaded["entries"], loaded["leafId"])

    @classmethod
    async def create(cls, fs: Any, file_path: str, options: dict[str, Any]) -> JsonlSessionStorage:
        header = {
            "type": "session",
            "version": 3,
            "id": options["sessionId"],
            "timestamp": _create_timestamp(),
            "cwd": options["cwd"],
            "parentSession": options.get("parentSessionPath"),
        }
        get_file_system_result_or_throw(
            await fs.writeFile(file_path, f"{json.dumps(header, separators=(',', ':'))}\n"),
            f"Failed to create session {file_path}",
        )
        return cls(fs, file_path, header, [], None)

    async def getMetadata(self):
        return self._metadata

    async def getLeafId(self) -> str | None:
        if self._current_leaf_id is not None and self._current_leaf_id not in self._by_id:
            raise SessionError("invalid_session", f"Entry {self._current_leaf_id} not found")
        return self._current_leaf_id

    async def setLeafId(self, leafId: str | None) -> None:
        if leafId is not None and leafId not in self._by_id:
            raise SessionError("not_found", f"Entry {leafId} not found")
        entry = {
            "type": "leaf",
            "id": _generate_entry_id(self._by_id),
            "parentId": self._current_leaf_id,
            "timestamp": _create_timestamp(),
            "targetId": leafId,
        }
        get_file_system_result_or_throw(
            await self._fs.appendFile(self._file_path, f"{json.dumps(entry, separators=(',', ':'))}\n"),
            f"Failed to append session leaf {entry['id']}",
        )
        self._entries.append(entry)
        self._by_id[entry["id"]] = entry
        self._current_leaf_id = leafId

    async def createEntryId(self) -> str:
        return _generate_entry_id(self._by_id)

    async def appendEntry(self, entry: Any) -> None:
        payload = _to_jsonable(entry)
        get_file_system_result_or_throw(
            await self._fs.appendFile(self._file_path, f"{json.dumps(payload, separators=(',', ':'))}\n"),
            f"Failed to append session entry {payload['id']}",
        )
        self._entries.append(entry)
        self._by_id[str(_entry_field(entry, "id"))] = entry
        _update_label_cache(self._labels_by_id, entry)
        self._current_leaf_id = _leaf_id_after_entry(entry)

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

def _to_jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    if hasattr(value, "__dict__"):
        return {key: _to_jsonable(item) for key, item in vars(value).items()}
    return value


def _entry_field(entry: Any, name: str) -> Any:
    if isinstance(entry, dict):
        return entry.get(name)
    return getattr(entry, name)


def _create_timestamp() -> str:
    from misaka.agent.harness.session.repo_utils import create_timestamp

    return create_timestamp()


loadJsonlSessionMetadata = load_jsonl_session_metadata

__all__ = [
    "JsonlSessionStorage",
    "loadJsonlSessionMetadata",
    "load_jsonl_session_metadata",
]

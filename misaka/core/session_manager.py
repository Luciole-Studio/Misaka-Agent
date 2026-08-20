"""Append-only JSONL session storage and tree traversal helpers."""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from misaka.agent.harness.messages import (
    BashExecutionMessage,
    CustomMessage,
    create_branch_summary_message,
    create_compaction_summary_message,
    create_custom_message,
)
from misaka.agent.harness.session.uuid import uuidv7
from misaka.agent.harness.types import SessionContext
from misaka.agent.types import AgentMessage
from misaka.ai.types import ImageContent, MessageValue, TextContent

from misaka.config import get_agent_dir, get_sessions_dir
from misaka.utils.paths import normalize_path, resolve_path

CURRENT_SESSION_VERSION = 3

type SessionHeader = dict[str, Any]
type SessionEntryBase = dict[str, Any]
type SessionMessageEntry = dict[str, Any]
type ThinkingLevelChangeEntry = dict[str, Any]
type ModelChangeEntry = dict[str, Any]
type CompactionEntry = dict[str, Any]
type BranchSummaryEntry = dict[str, Any]
type CustomEntry = dict[str, Any]
type LabelEntry = dict[str, Any]
type SessionInfoEntry = dict[str, Any]
type CustomMessageEntry = dict[str, Any]
type SessionEntry = dict[str, Any]
type FileEntry = dict[str, Any]
type SessionModelInfo = dict[str, str]
type SessionListProgress = Callable[[int, int], None]

_LEAF_UNSET = object()
_UNSET = object()
_SAFE_PATH_LEADING_SEPARATORS = re.compile(r"^[/\\]+")
_SAFE_PATH_SEPARATORS = re.compile(r"[/\\:]")
MAX_CONCURRENT_SESSION_INFO_LOADS = 10


@dataclass(slots=True, frozen=True)
class _InvalidSessionDate:
    raw: str | None = None

    def timestamp(self) -> float:
        return float("nan")

    def __str__(self) -> str:
        return "Invalid Date"

    def __repr__(self) -> str:
        return "Invalid Date"


@dataclass(slots=True)
class NewSessionOptions:
    id: str | None = None
    parentSession: str | None = None


@dataclass(slots=True)
class SessionTreeNode:
    entry: SessionEntry
    children: list[SessionTreeNode] = field(default_factory=list)
    label: str | None = None
    labelTimestamp: str | None = None


@dataclass(slots=True)
class SessionInfo:
    path: str
    id: str
    cwd: str
    created: datetime | _InvalidSessionDate
    modified: datetime
    messageCount: int
    firstMessage: str
    allMessagesText: str
    name: str | None = None
    parentSessionPath: str | None = None


@runtime_checkable
class ReadonlySessionManager(Protocol):
    def getCwd(self) -> str: ...

    def getSessionDir(self) -> str: ...

    def getSessionId(self) -> str: ...

    def getSessionFile(self) -> str | None: ...

    def getLeafId(self) -> str | None: ...

    def getLeafEntry(self) -> SessionEntry | None: ...

    def getEntry(self, id: str) -> SessionEntry | None: ...

    def getLabel(self, id: str) -> str | None: ...

    def getBranch(self, fromId: str | None = None) -> list[SessionEntry]: ...

    def getHeader(self) -> SessionHeader | None: ...

    def getEntries(self) -> list[SessionEntry]: ...

    def getTree(self) -> list[SessionTreeNode]: ...

    def getSessionName(self) -> str | None: ...


def create_session_id() -> str:
    return uuidv7()


def generate_id(existing: Mapping[str, Any] | set[str]) -> str:
    existing_ids = set(existing.keys()) if isinstance(existing, Mapping) else set(existing)
    for _ in range(100):
        candidate = uuid.uuid4().hex[:8]
        if candidate not in existing_ids:
            return candidate
    return uuid.uuid4().hex


def migrate_v1_to_v2(entries: list[FileEntry]) -> None:
    existing_ids: set[str] = set()
    previous_id: str | None = None

    for entry in entries:
        if entry.get("type") == "session":
            entry["version"] = 2
            continue

        entry["id"] = generate_id(existing_ids)
        existing_ids.add(str(entry["id"]))
        entry["parentId"] = previous_id
        previous_id = str(entry["id"])

        if entry.get("type") == "compaction" and isinstance(entry.get("firstKeptEntryIndex"), int):
            first_kept_entry_index = entry["firstKeptEntryIndex"]
            target_entry = entries[first_kept_entry_index] if 0 <= first_kept_entry_index < len(entries) else None
            if isinstance(target_entry, dict) and target_entry.get("type") != "session" and isinstance(
                target_entry.get("id"), str
            ):
                entry["firstKeptEntryId"] = target_entry["id"]
            entry.pop("firstKeptEntryIndex", None)


def migrate_v2_to_v3(entries: list[FileEntry]) -> None:
    for entry in entries:
        if entry.get("type") == "session":
            entry["version"] = 3
            continue

        if entry.get("type") == "message":
            message = entry.get("message")
            if _message_role(message) == "hookMessage":
                _set_message_role(message, "custom")


def _migrate_to_current_version(entries: list[FileEntry]) -> bool:
    header = next((entry for entry in entries if entry.get("type") == "session"), None)
    version = int(header.get("version", 1)) if isinstance(header, dict) else 1
    if version >= CURRENT_SESSION_VERSION:
        return False

    if version < 2:
        migrate_v1_to_v2(entries)
    if version < 3:
        migrate_v2_to_v3(entries)
    return True


def migrate_session_entries(entries: list[FileEntry]) -> None:
    _migrate_to_current_version(entries)


def parse_session_entries(content: str) -> list[FileEntry]:
    return _parse_jsonl_entries(content)


def get_latest_compaction_entry(entries: list[SessionEntry]) -> SessionEntry | None:
    for entry in reversed(entries):
        if entry.get("type") == "compaction":
            return entry
    return None


def build_session_context(
    entries: list[SessionEntry],
    leaf_id: str | None | object = _LEAF_UNSET,
    by_id: Mapping[str, SessionEntry] | None = None,
) -> SessionContext:
    if by_id is None:
        by_id = {
            entry_id: entry
            for entry in entries
            if isinstance((entry_id := entry.get("id")), str)
        }

    if leaf_id is None:
        return SessionContext(messages=[], thinkingLevel="off", model=None)

    leaf: SessionEntry | None = None
    if isinstance(leaf_id, str):
        leaf = by_id.get(leaf_id)
    if leaf is None and entries:
        leaf = entries[-1]
    if leaf is None:
        return SessionContext(messages=[], thinkingLevel="off", model=None)

    path: list[SessionEntry] = []
    current: SessionEntry | None = leaf
    while current is not None:
        path.insert(0, current)
        parent_id = current.get("parentId")
        current = by_id.get(parent_id) if isinstance(parent_id, str) else None

    thinking_level = "off"
    model: SessionModelInfo | None = None
    compaction: SessionEntry | None = None
    for entry in path:
        entry_type = entry.get("type")
        if entry_type == "thinking_level_change":
            thinking_level = str(entry.get("thinkingLevel"))
        elif entry_type == "model_change":
            provider = entry.get("provider")
            model_id = entry.get("modelId")
            if isinstance(provider, str) and isinstance(model_id, str):
                model = {"provider": provider, "modelId": model_id}
        elif entry_type == "message" and _message_role(entry.get("message")) == "assistant":
            provider = _message_field(entry.get("message"), "provider")
            model_id = _message_field(entry.get("message"), "model")
            if isinstance(provider, str) and isinstance(model_id, str):
                model = {"provider": provider, "modelId": model_id}
        elif entry_type == "compaction":
            compaction = entry

    messages: list[AgentMessage] = []

    def append_message(entry: SessionEntry) -> None:
        entry_type = entry.get("type")
        if entry_type == "message":
            message = entry.get("message")
            if message is not None:
                messages.append(message)
        elif entry_type == "custom_message":
            messages.append(
                create_custom_message(
                    str(entry.get("customType")),
                    entry.get("content"),
                    bool(entry.get("display")),
                    entry.get("details"),
                    str(entry.get("timestamp")),
                )
            )
        elif entry_type == "branch_summary" and entry.get("summary"):
            messages.append(
                create_branch_summary_message(
                    str(entry.get("summary")),
                    str(entry.get("fromId")),
                    str(entry.get("timestamp")),
                )
            )

    if compaction is not None:
        messages.append(
            create_compaction_summary_message(
                str(compaction.get("summary")),
                int(compaction.get("tokensBefore", 0)),
                str(compaction.get("timestamp")),
            )
        )
        compaction_id = compaction.get("id")
        compaction_index = next(
            (
                index
                for index, entry in enumerate(path)
                if entry.get("type") == "compaction" and entry.get("id") == compaction_id
            ),
            -1,
        )
        first_kept_entry_id = compaction.get("firstKeptEntryId")
        found_first_kept = False
        for entry in path[:compaction_index]:
            if entry.get("id") == first_kept_entry_id:
                found_first_kept = True
            if found_first_kept:
                append_message(entry)
        for entry in path[compaction_index + 1 :]:
            append_message(entry)
    else:
        for entry in path:
            append_message(entry)

    return SessionContext(messages=messages, thinkingLevel=thinking_level, model=model)


def get_default_session_dir(cwd: str, agent_dir: str | None = None) -> str:
    resolved_cwd = resolve_path(cwd)
    resolved_agent_dir = resolve_path(get_agent_dir() if agent_dir is None else agent_dir)
    normalized_cwd = _SAFE_PATH_LEADING_SEPARATORS.sub("", resolved_cwd)
    safe_path = f"--{_SAFE_PATH_SEPARATORS.sub('-', normalized_cwd)}--"
    session_dir = os.path.join(resolved_agent_dir, "sessions", safe_path)
    os.makedirs(session_dir, exist_ok=True)
    return session_dir


def load_entries_from_file(file_path: str) -> list[FileEntry]:
    resolved_file_path = normalize_path(file_path)
    path = Path(resolved_file_path)
    if not path.exists():
        return []

    content = path.read_text(encoding="utf-8")
    entries = _parse_jsonl_entries(content)
    if not entries:
        return entries

    header = entries[0]
    if header.get("type") != "session" or not isinstance(header.get("id"), str):
        return []
    return entries


def find_most_recent_session(session_dir: str) -> str | None:
    resolved_dir = normalize_path(session_dir)
    try:
        files = [
            os.path.join(resolved_dir, name)
            for name in os.listdir(resolved_dir)
            if name.endswith(".jsonl")
        ]
    except OSError:
        return None

    valid_files = []
    for file_path in files:
        if _is_valid_session_file(file_path):
            try:
                valid_files.append((file_path, os.stat(file_path).st_mtime))
            except OSError:
                continue
    if not valid_files:
        return None

    valid_files.sort(key=lambda item: item[1], reverse=True)
    return valid_files[0][0]


class SessionManager:
    def __init__(
        self,
        cwd: str,
        sessionDir: str,
        sessionFile: str | None = None,
        persist: bool = True,
    ) -> None:
        self.cwd = resolve_path(cwd)
        self.sessionDir = normalize_path(sessionDir)
        self.persist = persist
        self.sessionId = ""
        self.sessionFile: str | None = None
        self.flushed = False
        self.fileEntries: list[FileEntry] = []
        self.byId: dict[str, SessionEntry] = {}
        self.labelsById: dict[str, str] = {}
        self.labelTimestampsById: dict[str, str] = {}
        self.leafId: str | None = None

        if self.persist and self.sessionDir and not os.path.exists(self.sessionDir):
            os.makedirs(self.sessionDir, exist_ok=True)

        if sessionFile:
            self.setSessionFile(sessionFile)
        else:
            self.newSession()

    def setSessionFile(self, sessionFile: str) -> None:
        self.sessionFile = resolve_path(sessionFile)
        if os.path.exists(self.sessionFile):
            self.fileEntries = load_entries_from_file(self.sessionFile)
            if not self.fileEntries:
                explicit_path = self.sessionFile
                self.newSession()
                self.sessionFile = explicit_path
                self._rewriteFile()
                self.flushed = True
                return

            header = next((entry for entry in self.fileEntries if entry.get("type") == "session"), None)
            self.sessionId = (
                str(header.get("id"))
                if isinstance(header, dict) and header.get("id")
                else create_session_id()
            )
            if _migrate_to_current_version(self.fileEntries):
                self._rewriteFile()
            self._buildIndex()
            self.flushed = True
            return

        explicit_path = self.sessionFile
        self.newSession()
        self.sessionFile = explicit_path

    def newSession(self, options: NewSessionOptions | None = None) -> str | None:
        self.sessionId = options.id if options and options.id else create_session_id()
        timestamp = _iso_now()
        header: SessionHeader = {
            "type": "session",
            "version": CURRENT_SESSION_VERSION,
            "id": self.sessionId,
            "timestamp": timestamp,
            "cwd": self.cwd,
        }
        if options is not None and options.parentSession is not None:
            header["parentSession"] = options.parentSession

        self.fileEntries = [header]
        self.byId.clear()
        self.labelsById.clear()
        self.leafId = None
        self.flushed = False

        if self.persist:
            file_timestamp = timestamp.replace(":", "-").replace(".", "-")
            self.sessionFile = os.path.join(self.getSessionDir(), f"{file_timestamp}_{self.sessionId}.jsonl")
        return self.sessionFile

    def _buildIndex(self) -> None:
        self.byId.clear()
        self.labelsById.clear()
        self.labelTimestampsById.clear()
        self.leafId = None
        for entry in self.fileEntries:
            if entry.get("type") == "session":
                continue
            entry_id = entry.get("id")
            if isinstance(entry_id, str):
                self.byId[entry_id] = entry
                self.leafId = entry_id
            if entry.get("type") == "label":
                target_id = entry.get("targetId")
                if isinstance(target_id, str):
                    if entry.get("label"):
                        self.labelsById[target_id] = str(entry["label"])
                        self.labelTimestampsById[target_id] = str(entry.get("timestamp"))
                    else:
                        self.labelsById.pop(target_id, None)
                        self.labelTimestampsById.pop(target_id, None)

    def _rewriteFile(self) -> None:
        if not self.persist or not self.sessionFile:
            return
        Path(self.sessionFile).write_text(_dump_jsonl(self.fileEntries), encoding="utf-8")

    def isPersisted(self) -> bool:
        return self.persist

    def getCwd(self) -> str:
        return self.cwd

    def getSessionDir(self) -> str:
        return self.sessionDir

    def getSessionId(self) -> str:
        return self.sessionId

    def getSessionFile(self) -> str | None:
        return self.sessionFile

    def _persist(self, entry: SessionEntry) -> None:
        if not self.persist or not self.sessionFile:
            return

        has_assistant = any(
            file_entry.get("type") == "message" and _message_role(file_entry.get("message")) == "assistant"
            for file_entry in self.fileEntries
        )
        if not has_assistant:
            self.flushed = False
            return

        if not self.flushed:
            self._rewriteFile()
            self.flushed = True
            return

        with Path(self.sessionFile).open("a", encoding="utf-8") as handle:
            handle.write(f"{_dump_json(entry)}\n")

    def _appendEntry(self, entry: SessionEntry) -> None:
        self.fileEntries.append(entry)
        entry_id = entry.get("id")
        if isinstance(entry_id, str):
            self.byId[entry_id] = entry
            self.leafId = entry_id
        self._persist(entry)

    def appendMessage(self, message: MessageValue | dict[str, Any] | CustomMessage[Any] | BashExecutionMessage) -> str:
        entry: SessionEntry = {
            "type": "message",
            "id": generate_id(self.byId),
            "parentId": self.leafId,
            "timestamp": _iso_now(),
            "message": message,
        }
        self._appendEntry(entry)
        return str(entry["id"])

    def appendThinkingLevelChange(self, thinkingLevel: str) -> str:
        entry: SessionEntry = {
            "type": "thinking_level_change",
            "id": generate_id(self.byId),
            "parentId": self.leafId,
            "timestamp": _iso_now(),
            "thinkingLevel": thinkingLevel,
        }
        self._appendEntry(entry)
        return str(entry["id"])

    def appendModelChange(self, provider: str, modelId: str) -> str:
        entry: SessionEntry = {
            "type": "model_change",
            "id": generate_id(self.byId),
            "parentId": self.leafId,
            "timestamp": _iso_now(),
            "provider": provider,
            "modelId": modelId,
        }
        self._appendEntry(entry)
        return str(entry["id"])

    def appendCompaction(
        self,
        summary: str,
        firstKeptEntryId: str,
        tokensBefore: int,
        details: Any = _UNSET,
        fromHook: bool | None | object = _UNSET,
    ) -> str:
        entry: SessionEntry = {
            "type": "compaction",
            "id": generate_id(self.byId),
            "parentId": self.leafId,
            "timestamp": _iso_now(),
            "summary": summary,
            "firstKeptEntryId": firstKeptEntryId,
            "tokensBefore": tokensBefore,
        }
        if details is not _UNSET:
            entry["details"] = details
        if fromHook is not _UNSET:
            entry["fromHook"] = fromHook
        self._appendEntry(entry)
        return str(entry["id"])

    def appendCustomEntry(self, customType: str, data: Any = _UNSET) -> str:
        entry: SessionEntry = {
            "type": "custom",
            "customType": customType,
            "id": generate_id(self.byId),
            "parentId": self.leafId,
            "timestamp": _iso_now(),
        }
        if data is not _UNSET:
            entry["data"] = data
        self._appendEntry(entry)
        return str(entry["id"])

    def appendSessionInfo(self, name: str) -> str:
        entry: SessionEntry = {
            "type": "session_info",
            "id": generate_id(self.byId),
            "parentId": self.leafId,
            "timestamp": _iso_now(),
            "name": name.strip(),
        }
        self._appendEntry(entry)
        return str(entry["id"])

    def getSessionName(self) -> str | None:
        for entry in reversed(self.getEntries()):
            if entry.get("type") == "session_info":
                name = entry.get("name")
                return str(name).strip() or None if name is not None else None
        return None

    def appendCustomMessageEntry(
        self,
        customType: str,
        content: str | list[TextContent | ImageContent | dict[str, Any]],
        display: bool,
        details: Any = _UNSET,
    ) -> str:
        entry: SessionEntry = {
            "type": "custom_message",
            "customType": customType,
            "content": content,
            "display": display,
            "id": generate_id(self.byId),
            "parentId": self.leafId,
            "timestamp": _iso_now(),
        }
        if details is not _UNSET:
            entry["details"] = details
        self._appendEntry(entry)
        return str(entry["id"])

    def getLeafId(self) -> str | None:
        return self.leafId

    def getLeafEntry(self) -> SessionEntry | None:
        return self.byId.get(self.leafId) if self.leafId else None

    def getEntry(self, id: str) -> SessionEntry | None:
        return self.byId.get(id)

    def getChildren(self, parentId: str) -> list[SessionEntry]:
        return [entry for entry in self.byId.values() if entry.get("parentId") == parentId]

    def getLabel(self, id: str) -> str | None:
        return self.labelsById.get(id)

    def appendLabelChange(self, targetId: str, label: str | None) -> str:
        if targetId not in self.byId:
            raise Exception(f"Entry {targetId} not found")
        entry: SessionEntry = {
            "type": "label",
            "id": generate_id(self.byId),
            "parentId": self.leafId,
            "timestamp": _iso_now(),
            "targetId": targetId,
            "label": label,
        }
        self._appendEntry(entry)
        if label:
            self.labelsById[targetId] = label
            self.labelTimestampsById[targetId] = str(entry["timestamp"])
        else:
            self.labelsById.pop(targetId, None)
            self.labelTimestampsById.pop(targetId, None)
        return str(entry["id"])

    def getBranch(self, fromId: str | None = None) -> list[SessionEntry]:
        path: list[SessionEntry] = []
        start_id = fromId if fromId is not None else self.leafId
        current = self.byId.get(start_id) if isinstance(start_id, str) else None
        while current is not None:
            path.insert(0, current)
            parent_id = current.get("parentId")
            current = self.byId.get(parent_id) if isinstance(parent_id, str) else None
        return path

    def buildSessionContext(self) -> SessionContext:
        return build_session_context(self.getEntries(), self.leafId, self.byId)

    def getHeader(self) -> SessionHeader | None:
        header = next((entry for entry in self.fileEntries if entry.get("type") == "session"), None)
        return header if isinstance(header, dict) else None

    def getEntries(self) -> list[SessionEntry]:
        return [entry for entry in self.fileEntries if entry.get("type") != "session"]

    def getTree(self) -> list[SessionTreeNode]:
        entries = self.getEntries()
        node_map: dict[str, SessionTreeNode] = {}
        roots: list[SessionTreeNode] = []

        for entry in entries:
            entry_id = entry.get("id")
            if not isinstance(entry_id, str):
                continue
            node_map[entry_id] = SessionTreeNode(
                entry=entry,
                children=[],
                label=self.labelsById.get(entry_id),
                labelTimestamp=self.labelTimestampsById.get(entry_id),
            )

        for entry in entries:
            entry_id = entry.get("id")
            if not isinstance(entry_id, str) or entry_id not in node_map:
                continue
            node = node_map[entry_id]
            parent_id = entry.get("parentId")
            if parent_id is None or parent_id == entry_id:
                roots.append(node)
                continue
            parent = node_map.get(parent_id)
            if parent is None:
                roots.append(node)
            else:
                parent.children.append(node)

        stack = list(roots)
        while stack:
            node = stack.pop()
            node.children.sort(key=lambda child: _timestamp_ms(child.entry.get("timestamp")))
            stack.extend(node.children)
        return roots

    def branch(self, branchFromId: str) -> None:
        if branchFromId not in self.byId:
            raise Exception(f"Entry {branchFromId} not found")
        self.leafId = branchFromId

    def resetLeaf(self) -> None:
        self.leafId = None

    def branchWithSummary(
        self,
        branchFromId: str | None,
        summary: str,
        details: Any = _UNSET,
        fromHook: bool | None | object = _UNSET,
    ) -> str:
        if branchFromId is not None and branchFromId not in self.byId:
            raise Exception(f"Entry {branchFromId} not found")
        self.leafId = branchFromId
        entry: SessionEntry = {
            "type": "branch_summary",
            "id": generate_id(self.byId),
            "parentId": branchFromId,
            "timestamp": _iso_now(),
            "fromId": branchFromId or "root",
            "summary": summary,
        }
        if details is not _UNSET:
            entry["details"] = details
        if fromHook is not _UNSET:
            entry["fromHook"] = fromHook
        self._appendEntry(entry)
        return str(entry["id"])

    def createBranchedSession(self, leafId: str) -> str | None:
        previous_session_file = self.sessionFile
        path = self.getBranch(leafId)
        if not path:
            raise Exception(f"Entry {leafId} not found")

        path_without_labels = [entry for entry in path if entry.get("type") != "label"]
        new_session_id = create_session_id()
        timestamp = _iso_now()
        file_timestamp = timestamp.replace(":", "-").replace(".", "-")
        new_session_file = os.path.join(self.getSessionDir(), f"{file_timestamp}_{new_session_id}.jsonl")
        header: SessionHeader = {
            "type": "session",
            "version": CURRENT_SESSION_VERSION,
            "id": new_session_id,
            "timestamp": timestamp,
            "cwd": self.cwd,
        }
        if self.persist and previous_session_file is not None:
            header["parentSession"] = previous_session_file

        path_entry_ids = {
            entry_id
            for entry in path_without_labels
            if isinstance((entry_id := entry.get("id")), str)
        }
        labels_to_write: list[tuple[str, str, str]] = []
        for target_id, label in self.labelsById.items():
            if target_id in path_entry_ids and target_id in self.labelTimestampsById:
                labels_to_write.append((target_id, label, self.labelTimestampsById[target_id]))

        label_entries: list[SessionEntry] = []
        parent_id = path_without_labels[-1].get("id") if path_without_labels else None
        existing_ids = set(path_entry_ids)
        for target_id, label, label_timestamp in labels_to_write:
            label_entry: SessionEntry = {
                "type": "label",
                "id": generate_id(existing_ids),
                "parentId": parent_id,
                "timestamp": label_timestamp,
                "targetId": target_id,
                "label": label,
            }
            label_entries.append(label_entry)
            existing_ids.add(str(label_entry["id"]))
            parent_id = label_entry["id"]

        self.fileEntries = [header, *path_without_labels, *label_entries]
        self.sessionId = new_session_id
        if self.persist:
            self.sessionFile = new_session_file
        self._buildIndex()

        has_assistant = any(
            entry.get("type") == "message" and _message_role(entry.get("message")) == "assistant"
            for entry in self.fileEntries
        )
        if self.persist and has_assistant:
            self._rewriteFile()
            self.flushed = True
            return new_session_file

        self.flushed = False
        return new_session_file if self.persist else None

    @classmethod
    def create(cls, cwd: str, sessionDir: str | None = None) -> SessionManager:
        directory = normalize_path(sessionDir) if sessionDir else get_default_session_dir(cwd)
        return cls(cwd, directory, None, True)

    @classmethod
    def open(
        cls,
        path: str,
        sessionDir: str | None = None,
        cwdOverride: str | None = None,
    ) -> SessionManager:
        resolved_path = resolve_path(path)
        entries = load_entries_from_file(resolved_path)
        header = next((entry for entry in entries if entry.get("type") == "session"), None)
        cwd = (
            cwdOverride
            if cwdOverride is not None
            else (str(header.get("cwd")) if isinstance(header, dict) and isinstance(header.get("cwd"), str) else os.getcwd())
        )
        directory = normalize_path(sessionDir) if sessionDir else str(Path(resolved_path).parent)
        return cls(cwd, directory, resolved_path, True)

    @classmethod
    def continueRecent(cls, cwd: str, sessionDir: str | None = None) -> SessionManager:
        directory = normalize_path(sessionDir) if sessionDir else get_default_session_dir(cwd)
        most_recent = find_most_recent_session(directory)
        if most_recent:
            return cls(cwd, directory, most_recent, True)
        return cls(cwd, directory, None, True)

    @classmethod
    def inMemory(cls, cwd: str | None = None) -> SessionManager:
        return cls(os.getcwd() if cwd is None else cwd, "", None, False)

    @classmethod
    def forkFrom(cls, sourcePath: str, targetCwd: str, sessionDir: str | None = None) -> SessionManager:
        resolved_source_path = resolve_path(sourcePath)
        resolved_target_cwd = resolve_path(targetCwd)
        source_entries = load_entries_from_file(resolved_source_path)
        if not source_entries:
            raise Exception(f"Cannot fork: source session file is empty or invalid: {resolved_source_path}")

        source_header = next((entry for entry in source_entries if entry.get("type") == "session"), None)
        if source_header is None:
            raise Exception(f"Cannot fork: source session has no header: {resolved_source_path}")

        directory = normalize_path(sessionDir) if sessionDir else get_default_session_dir(resolved_target_cwd)
        os.makedirs(directory, exist_ok=True)
        new_session_id = create_session_id()
        timestamp = _iso_now()
        file_timestamp = timestamp.replace(":", "-").replace(".", "-")
        new_session_file = os.path.join(directory, f"{file_timestamp}_{new_session_id}.jsonl")
        header: SessionHeader = {
            "type": "session",
            "version": CURRENT_SESSION_VERSION,
            "id": new_session_id,
            "timestamp": timestamp,
            "cwd": resolved_target_cwd,
            "parentSession": resolved_source_path,
        }
        copied_entries = [entry for entry in source_entries if entry.get("type") != "session"]
        # 原子发布（pi #7707/f4bbbdb）：直写正式文件名时，写到一半被打断（Ctrl-C／
        # 崩溃／磁盘满）会留下半截 JSONL 且已占着正式名字，下次当成有效会话加载。
        # 先写同目录临时文件再 os.replace——同一文件系统上是原子的。
        temp_file = f"{new_session_file}.{os.getpid()}.tmp"
        try:
            Path(temp_file).write_text(_dump_jsonl([header, *copied_entries]), encoding="utf-8")
            os.replace(temp_file, new_session_file)
        except BaseException:
            try:
                os.unlink(temp_file)
            except OSError:
                pass
            raise
        return cls(resolved_target_cwd, directory, new_session_file, True)

    @classmethod
    async def list(
        cls,
        cwd: str,
        sessionDir: str | None = None,
        onProgress: SessionListProgress | None = None,
    ) -> list[SessionInfo]:
        directory = normalize_path(sessionDir) if sessionDir else get_default_session_dir(cwd)
        sessions = await _list_sessions_from_dir(directory, onProgress)
        sessions.sort(key=lambda session: session.modified, reverse=True)
        return sessions

    @classmethod
    async def listAll(cls, onProgress: SessionListProgress | None = None) -> list[SessionInfo]:
        sessions_dir = get_sessions_dir()
        try:
            if not os.path.exists(sessions_dir):
                return []
            directories = [
                os.path.join(sessions_dir, entry)
                for entry in os.listdir(sessions_dir)
                if os.path.isdir(os.path.join(sessions_dir, entry))
            ]
        except OSError:
            return []

        total_files = 0
        directory_files: list[list[str]] = []
        for directory in directories:
            try:
                files = [
                    os.path.join(directory, name)
                    for name in os.listdir(directory)
                    if name.endswith(".jsonl")
                ]
                directory_files.append(files)
                total_files += len(files)
            except OSError:
                directory_files.append([])

        sessions: list[SessionInfo] = []
        all_files = [file_path for files in directory_files for file_path in files]
        loaded_ref = {"value": 0}

        def on_loaded() -> None:
            loaded_ref["value"] += 1
            if onProgress is not None:
                onProgress(loaded_ref["value"], total_files)

        results = await _build_session_infos_with_concurrency(all_files, on_loaded)
        for info in results:
            if info is not None:
                sessions.append(info)
        sessions.sort(key=lambda session: session.modified, reverse=True)
        return sessions


def _parse_jsonl_entries(content: str) -> list[FileEntry]:
    entries: list[FileEntry] = []
    for line in content.splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            entries.append(parsed)
    return entries


def _is_valid_session_file(file_path: str) -> bool:
    try:
        with Path(file_path).open("rb") as handle:
            first_line = handle.read(512).splitlines()[0].decode("utf-8")
        header = json.loads(first_line)
    except (IndexError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(header, dict) and header.get("type") == "session" and isinstance(header.get("id"), str)


def _message_field(message: Any, name: str) -> Any:
    if isinstance(message, dict):
        return message.get(name)
    return getattr(message, name, None)


def _message_role(message: Any) -> str | None:
    role = _message_field(message, "role")
    return role if isinstance(role, str) else None


def _is_message_with_content(message: Any) -> bool:
    if not isinstance(_message_role(message), str):
        return False
    if isinstance(message, dict):
        return "content" in message
    return hasattr(message, "content")


def _set_message_role(message: Any, role: str) -> None:
    if isinstance(message, dict):
        message["role"] = role
    elif message is not None:
        message.role = role


def _text_content(message: Any) -> str:
    content = _message_field(message, "content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        block_type = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
        if block_type == "text":
            text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
    return " ".join(parts)


def _last_activity_time(entries: list[FileEntry]) -> int | None:
    last_activity: int | None = None
    for entry in entries:
        if entry.get("type") != "message":
            continue

        message = entry.get("message")
        if not _is_message_with_content(message):
            continue
        if _message_role(message) not in {"user", "assistant"}:
            continue

        message_timestamp = _message_field(message, "timestamp")
        if isinstance(message_timestamp, (int, float)) and not isinstance(message_timestamp, bool):
            current = int(message_timestamp)
        else:
            current = _timestamp_ms(entry.get("timestamp"))

        if current > 0:
            last_activity = current if last_activity is None else max(last_activity, current)
    return last_activity


def _session_modified_date(entries: list[FileEntry], header: SessionHeader, stats_mtime: float) -> datetime:
    last_activity = _last_activity_time(entries)
    if last_activity is not None:
        return datetime.fromtimestamp(last_activity / 1000, UTC)

    header_timestamp = _timestamp_ms(header.get("timestamp"))
    if header_timestamp > 0:
        return datetime.fromtimestamp(header_timestamp / 1000, UTC)
    return datetime.fromtimestamp(stats_mtime, UTC)


def _build_session_info_sync(file_path: str) -> SessionInfo | None:
    try:
        path = Path(file_path)
        content = path.read_text(encoding="utf-8")
        entries = _parse_jsonl_entries(content)
        if not entries:
            return None

        header = entries[0]
        if header.get("type") != "session":
            return None

        stats = path.stat()
        message_count = 0
        first_message = ""
        all_messages: list[str] = []
        name: str | None = None

        for entry in entries:
            if entry.get("type") == "session_info":
                raw_name = entry.get("name")
                name = str(raw_name).strip() or None if raw_name is not None else None

            if entry.get("type") != "message":
                continue

            message_count += 1
            message = entry.get("message")
            if _message_role(message) not in {"user", "assistant"}:
                continue

            text = _text_content(message)
            if not text:
                continue

            all_messages.append(text)
            if not first_message and _message_role(message) == "user":
                first_message = text

        cwd = str(header.get("cwd")) if isinstance(header.get("cwd"), str) else ""
        parent_session_path = header.get("parentSession")
        header_timestamp = str(header.get("timestamp"))
        created = _datetime_from_iso(header_timestamp) or _InvalidSessionDate(header_timestamp)
        modified = _session_modified_date(entries, header, stats.st_mtime)
        return SessionInfo(
            path=file_path,
            id=str(header.get("id")),
            cwd=cwd,
            name=name,
            parentSessionPath=str(parent_session_path) if isinstance(parent_session_path, str) else None,
            created=created,
            modified=modified,
            messageCount=message_count,
            firstMessage=first_message or "(no messages)",
            allMessagesText=" ".join(all_messages),
        )
    except Exception:
        return None


async def _build_session_info(file_path: str) -> SessionInfo | None:
    return await asyncio.to_thread(_build_session_info_sync, file_path)


async def _build_session_infos_with_concurrency(
    files: list[str],
    on_loaded: Callable[[], None],
) -> list[SessionInfo | None]:
    results: list[SessionInfo | None] = [None] * len(files)
    in_flight: set[asyncio.Task[None]] = set()
    next_index = 0

    def start_next() -> None:
        nonlocal next_index
        index = next_index
        if index >= len(files):
            return
        next_index += 1

        async def run() -> None:
            try:
                results[index] = await _build_session_info(files[index])
            except Exception:
                results[index] = None
            finally:
                on_loaded()

        task = asyncio.create_task(run())
        in_flight.add(task)
        task.add_done_callback(lambda completed: in_flight.discard(completed))

    while next_index < len(files) or in_flight:
        while next_index < len(files) and len(in_flight) < MAX_CONCURRENT_SESSION_INFO_LOADS:
            start_next()
        if in_flight:
            await asyncio.wait(in_flight, return_when=asyncio.FIRST_COMPLETED)

    return results


async def _list_sessions_from_dir(
    session_dir: str,
    on_progress: SessionListProgress | None = None,
    progress_offset: int = 0,
    progress_total: int | None = None,
) -> list[SessionInfo]:
    resolved_dir = normalize_path(session_dir)
    try:
        files = [
            os.path.join(resolved_dir, name)
            for name in os.listdir(resolved_dir)
            if name.endswith(".jsonl")
        ]
    except OSError:
        return []

    sessions: list[SessionInfo] = []
    total = progress_total if progress_total is not None else len(files)
    loaded_ref = {"value": 0}

    def on_loaded() -> None:
        loaded_ref["value"] += 1
        if on_progress is not None:
            on_progress(progress_offset + loaded_ref["value"], total)

    results = await _build_session_infos_with_concurrency(files, on_loaded)
    for info in results:
        if info is not None:
            sessions.append(info)
    return sessions


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _datetime_from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _timestamp_ms(value: Any) -> int:
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    if not isinstance(value, str):
        return 0
    parsed = _datetime_from_iso(value)
    return int(parsed.timestamp() * 1000) if parsed is not None else 0


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump())
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    return value


def _dump_json(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, separators=(",", ":"))


def _dump_jsonl(entries: list[FileEntry]) -> str:
    return "".join(f"{_dump_json(entry)}\n" for entry in entries)


createSessionId = create_session_id
generateId = generate_id
migrateV1ToV2 = migrate_v1_to_v2
migrateV2ToV3 = migrate_v2_to_v3
migrateSessionEntries = migrate_session_entries
parseSessionEntries = parse_session_entries
getLatestCompactionEntry = get_latest_compaction_entry
buildSessionContext = build_session_context
getDefaultSessionDir = get_default_session_dir
loadEntriesFromFile = load_entries_from_file
findMostRecentSession = find_most_recent_session

__all__ = [
    "CURRENT_SESSION_VERSION",
    "SessionHeader",
    "NewSessionOptions",
    "SessionEntryBase",
    "SessionMessageEntry",
    "ThinkingLevelChangeEntry",
    "ModelChangeEntry",
    "CompactionEntry",
    "BranchSummaryEntry",
    "CustomEntry",
    "LabelEntry",
    "SessionInfoEntry",
    "CustomMessageEntry",
    "SessionEntry",
    "FileEntry",
    "SessionTreeNode",
    "SessionContext",
    "SessionInfo",
    "SessionListProgress",
    "ReadonlySessionManager",
    "migrateSessionEntries",
    "parseSessionEntries",
    "getLatestCompactionEntry",
    "buildSessionContext",
    "getDefaultSessionDir",
    "loadEntriesFromFile",
    "findMostRecentSession",
    "SessionManager",
]

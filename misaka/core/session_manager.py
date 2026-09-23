"""Append-only JSONL session storage and tree traversal helpers."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, NotRequired, Protocol, TypedDict, runtime_checkable

from misaka.agent.harness.messages import (
    BashExecutionMessage,
    CustomMessage,
    convert_to_llm,
    create_branch_summary_message,
    create_compaction_summary_message,
    create_custom_message,
)
from misaka.agent.harness.session.uuid import uuidv7
from misaka.agent.harness.types import SessionContext
from misaka.agent.types import AgentMessage
from misaka.ai.types import ImageContent, MessageValue, TextContent, Usage
from misaka.config import get_sessions_dir
from misaka.utils import atomic
from misaka.utils.paths import canonicalize_path, normalize_path, resolve_path
from misaka.utils.values import read_field

CURRENT_SESSION_VERSION = 3

type SessionHeader = dict[str, Any]
type SessionEntryBase = dict[str, Any]
type SessionMessageEntry = dict[str, Any]
type ThinkingLevelChangeEntry = dict[str, Any]
type ModelChangeEntry = dict[str, Any]
type CompactionEntry = dict[str, Any]


class BranchSummaryEntry(TypedDict):
    type: Literal["branch_summary"]
    id: str
    parentId: str | None
    timestamp: str
    fromId: str
    summary: str
    details: NotRequired[Any]
    usage: NotRequired[Usage | Mapping[str, Any]]
    fromHook: NotRequired[bool | None]


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
MAX_CONCURRENT_SESSION_INFO_LOADS = 10


class InvalidSessionFileError(ValueError):
    """Raised when an explicitly selected session is not valid JSONL."""

    def __init__(self, file_path: str, reason: str = "empty or invalid") -> None:
        super().__init__(f"Invalid session file ({reason}): {file_path}")
        self.file_path = file_path


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

    def buildContextEntries(self) -> list[SessionEntry]: ...

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


def require_current_version(entries: list[FileEntry], file_path: str) -> None:
    """A session file is version 3 or it is refused.

    pi carries v1->v2->v3 rewrites for files its own older releases wrote; MISAKA never wrote
    an earlier version, so a file at one was made by something else and is not reshaped here.
    """
    header = next((entry for entry in entries if entry.get("type") == "session"), None)
    version = int(header.get("version", 1)) if isinstance(header, dict) else 1
    if version != CURRENT_SESSION_VERSION:
        raise InvalidSessionFileError(file_path, f"session version {version}; this build reads v{CURRENT_SESSION_VERSION}")


def get_latest_compaction_entry(entries: list[SessionEntry]) -> SessionEntry | None:
    for entry in reversed(entries):
        if entry.get("type") == "compaction":
            return entry
    return None


_FENCE = re.compile(r'\A<<<([A-Z-]+) name="[^"]*">>>\n(.*)\n<<<END-\1>>>\n.*\Z', re.DOTALL)


def _unfenced(text: str) -> str:
    """The text inside a prompt-guard fence, or the text itself: the fence is for the model."""
    match = _FENCE.match(text)
    return match.group(2) if match else text


def _content_text(message: Any) -> str:
    content = read_field(message, "content")
    if isinstance(content, str):
        return content
    return "\n".join(str(read_field(block, "text") or "") for block in (content or [])
                     if read_field(block, "type") == "text")


def session_entry_to_display_messages(entry: SessionEntry) -> list[AgentMessage]:
    """What the transcript shows for one entry.

    The same as the model's view, with one exception. A compaction whose engine wrote a
    complete context (``contextMessages``) carries that engine's summaries as user turns,
    because that is where the model expects them; on screen they are compaction summaries,
    shown the way pi shows its own and never remembered as something the user typed
    (2026-09-18, B30: the LCM summaries were being replayed into the editor's input
    history on every reopen). The engine names its own messages by index in
    ``details``; nothing here reads their text to guess.
    """
    if entry.get("type") != "compaction" or entry.get("contextMessages") is None:
        return session_entry_to_context_messages(entry)
    details = entry.get("details") or {}
    scaffolds = set((details.get("lcm") or {}).get("scaffolds") or []) if isinstance(details, dict) else set()
    shown: list[AgentMessage] = []
    for index, message in enumerate(_copy_context_messages(entry["contextMessages"])):
        if index in scaffolds and _message_role(message) == "user":
            shown.append(create_compaction_summary_message(
                _unfenced(_content_text(message)), int(entry.get("tokensBefore", 0) or 0),
                entry.get("timestamp")))
        else:
            shown.append(message)
    return shown


def _entry_starts_transcript(entry: SessionEntry) -> bool:
    """Keep empty UI/drafts lazy, but persist actual output and adopted checkpoints."""
    return (entry.get("type") == "custom_message"
            or (entry.get("type") == "compaction" and entry.get("contextMessages") is not None)
            or (entry.get("type") == "message" and _message_role(entry.get("message")) == "assistant"))


def _copy_context_messages(messages: list[AgentMessage]) -> list[AgentMessage]:
    """Validate a complete replay view before publishing it, and detach its ownership."""
    if not isinstance(messages, list):
        raise TypeError("contextMessages must be a list")
    supported = {"user", "assistant", "toolResult", "custom", "bashExecution",
                 "branchSummary", "compactionSummary"}
    if any(_message_role(message) not in supported for message in messages):
        raise ValueError("contextMessages contains an unsupported message role")
    snapshot = copy.deepcopy(messages)
    # Use the actual provider projection, including native content-block validation.
    # Keep the original native shapes, not its lossy user-role projection.
    convert_to_llm(snapshot)
    return snapshot


def session_entry_to_context_messages(entry: SessionEntry) -> list[AgentMessage]:
    """Project one session entry into the messages consumed by the active context."""
    entry_type = entry.get("type")
    if entry_type == "message":
        message = entry.get("message")
        if message is None:
            return []
        # Session files are parsed without message validation; old versions, forks, or
        # hand-edited files can contain standard messages with null/missing content.
        if (
            _message_role(message) in {"user", "assistant", "toolResult"}
            and read_field(message, "content") is None
        ):
            if isinstance(message, Mapping):
                return [{**message, "content": []}]
            model_copy = getattr(message, "model_copy", None)
            if callable(model_copy):
                return [model_copy(update={"content": []})]
        return [message]
    if entry_type == "custom_message":
        content = entry.get("content")
        return [
            create_custom_message(
                str(entry.get("customType")),
                [] if content is None else content,
                bool(entry.get("display")),
                entry.get("details"),
                entry.get("timestamp"),
            )
        ]
    if entry_type == "branch_summary" and entry.get("summary"):
        return [
            create_branch_summary_message(
                str(entry.get("summary")),
                str(entry.get("fromId")),
                entry.get("timestamp"),
            )
        ]
    if entry_type == "compaction":
        if entry.get("contextMessages") is not None:
            return _copy_context_messages(entry["contextMessages"])
        return [
            create_compaction_summary_message(
                str(entry.get("summary")),
                int(entry.get("tokensBefore", 0)),
                entry.get("timestamp"),
            )
        ]
    return []


def _build_session_path(
    entries: list[SessionEntry],
    leaf_id: str | None | object = _LEAF_UNSET,
    by_id: Mapping[str, SessionEntry] | None = None,
) -> list[SessionEntry]:
    if by_id is None:
        by_id = {
            entry_id: entry
            for entry in entries
            if isinstance((entry_id := entry.get("id")), str)
        }

    if leaf_id is None:
        return []

    leaf: SessionEntry | None = None
    if isinstance(leaf_id, str):
        leaf = by_id.get(leaf_id)
    if leaf is None and entries:
        leaf = entries[-1]
    if leaf is None:
        return []

    path: list[SessionEntry] = []
    current: SessionEntry | None = leaf
    while current is not None:
        path.append(current)
        parent_id = current.get("parentId")
        current = by_id.get(parent_id) if isinstance(parent_id, str) else None
    path.reverse()
    return path


def _context_entries_from_path(path: list[SessionEntry]) -> list[SessionEntry]:
    compaction = next(
        (entry for entry in reversed(path) if entry.get("type") == "compaction"),
        None,
    )
    if compaction is None:
        return path

    compaction_id = compaction.get("id")
    compaction_index = next(
        index for index, entry in enumerate(path) if entry.get("id") == compaction_id
    )
    context_entries = [compaction]
    if compaction.get("contextMessages") is not None:
        return context_entries + path[compaction_index + 1 :]
    first_kept_entry_id = compaction.get("firstKeptEntryId")
    found_first_kept = False
    for entry in path[:compaction_index]:
        if entry.get("id") == first_kept_entry_id:
            found_first_kept = True
        if found_first_kept:
            context_entries.append(entry)
    context_entries.extend(path[compaction_index + 1 :])
    return context_entries


def build_context_entries(
    entries: list[SessionEntry],
    leaf_id: str | None | object = _LEAF_UNSET,
    by_id: Mapping[str, SessionEntry] | None = None,
) -> list[SessionEntry]:
    """Return the active leaf path after applying the latest compaction boundary."""
    return _context_entries_from_path(_build_session_path(entries, leaf_id, by_id))


def build_session_context(
    entries: list[SessionEntry],
    leaf_id: str | None | object = _LEAF_UNSET,
    by_id: Mapping[str, SessionEntry] | None = None,
) -> SessionContext:
    path = _build_session_path(entries, leaf_id, by_id)

    thinking_level = "off"
    model: SessionModelInfo | None = None
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
            provider = read_field(entry.get("message"), "provider")
            model_id = read_field(entry.get("message"), "model")
            if isinstance(provider, str) and isinstance(model_id, str):
                model = {"provider": provider, "modelId": model_id}
    messages = [
        message
        for entry in _context_entries_from_path(path)
        for message in session_entry_to_context_messages(entry)
    ]

    return SessionContext(messages=messages, thinkingLevel=thinking_level, model=model)


def _canonical_cwd(cwd: str) -> str:
    return os.path.normcase(canonicalize_path(resolve_path(cwd)))


def encode_cwd(cwd: str) -> str:
    """Encode a canonical working directory as a readable, collision-resistant segment."""
    canonical_cwd = _canonical_cwd(cwd)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", canonical_cwd.strip("/\\")).strip("-")
    digest = hashlib.sha256(os.fsencode(canonical_cwd)).hexdigest()
    return f"--{(slug or 'root')[-64:]}-{digest}--"


def get_session_dir_for_cwd(cwd: str, sessions_root: str) -> str:
    """The canonical bucket for this working directory."""
    session_dir = os.path.join(resolve_path(sessions_root), encode_cwd(_canonical_cwd(cwd)))
    os.makedirs(session_dir, mode=0o700, exist_ok=True)
    try:
        os.chmod(session_dir, 0o700)              # buckets from before privacy was enforced
    except OSError:
        pass
    return session_dir


def get_default_session_dir(cwd: str, agent_dir: str | None = None) -> str:
    """The bucket for ``cwd`` when no session directory was given.

    With an ``agent_dir`` named by the caller -- an embedder running the engine out of its
    own home, or a test -- the layout is pi's: ``<agent_dir>/sessions/<bucket>``. Without
    one, the store is ``get_sessions_dir()``: MISAKA's product tree (see
    ``config.sessions``), never ``~/.misaka/agent/sessions``, which nothing lists.
    """
    if agent_dir is not None:
        return get_session_dir_for_cwd(cwd, os.path.join(resolve_path(agent_dir), "sessions"))
    return get_session_dir_for_cwd(cwd, get_sessions_dir())


def sessions_root_of(session_dir: str | None) -> str | None:
    """The store the "any bucket" lookup must search, given this run's session directory.

    Chats pass a cwd bucket (``--<slug>-<sha256>--``, see :func:`encode_cwd`), so the
    same role's other folders are its siblings: the store is the bucket's parent.
    Flat directories -- DM, cards and the ``sessionDir`` setting -- stay as they are.
    ``None`` keeps the engine default.

    Both ends of "all sessions" go through here: the CLI picker (``cli/engine.py``) and the TUI
    ``/resume`` selector (``ui/tui/interactive/interactive_mode.py``), which is why it lives with
    ``encode_cwd`` rather than in either caller.
    """
    if not session_dir:
        return None
    normalized = os.path.normpath(normalize_path(session_dir))
    parent, name = os.path.split(normalized)
    return parent if name.startswith("--") and name.endswith("--") else normalized


def iter_session_files(root: str, *, recursive: bool = False) -> Iterator[str]:
    """Enumerate transcript files without reading, creating or migrating a store."""
    for directory, dirs, files in os.walk(normalize_path(root)):
        dirs[:] = [d for d in dirs if d not in {".git", ".catalog"}]
        for name in files:
            path = os.path.join(directory, name)
            if name.endswith(".jsonl") and os.path.isfile(path):
                yield path
        if not recursive:
            break


def _is_session_header(value: Any) -> bool:
    return isinstance(value, dict) and value.get("type") == "session" and isinstance(value.get("id"), str)


def _session_cwd_matches(actual: str | None, cwd: str) -> bool:
    return isinstance(actual, str) and bool(actual) and _canonical_cwd(actual) == _canonical_cwd(cwd)


def read_session_header(file_path: str) -> dict[str, Any]:
    """The header line of a session file (``id``, ``cwd``, ``timestamp``), or ``{}``.
    Reads one line: the panel calls this for every file it lists."""
    try:
        with open(normalize_path(file_path), encoding="utf-8") as handle:
            entry = json.loads(handle.readline())
    except (OSError, ValueError):
        return {}
    return entry if _is_session_header(entry) else {}


def load_entries_from_file(
    file_path: str,
    *,
    strict: bool = False,
    repair_unterminated: bool = False,
) -> list[FileEntry]:
    """Load a session, optionally terminating a fully validated writable JSONL file."""
    resolved_file_path = normalize_path(file_path)
    path = Path(resolved_file_path)
    if not path.exists():
        return []

    content = path.read_text(encoding="utf-8")
    try:
        entries = _parse_jsonl_entries(content, strict=strict)
    except (json.JSONDecodeError, TypeError) as error:
        raise InvalidSessionFileError(resolved_file_path, str(error)) from error
    if not entries:
        if strict:
            raise InvalidSessionFileError(resolved_file_path)
        return entries

    header = entries[0]
    if not _is_session_header(header):
        if strict:
            raise InvalidSessionFileError(resolved_file_path, "missing session header")
        return []
    if strict and repair_unterminated and not content.endswith("\n"):
        with path.open("ab") as handle:
            handle.write(b"\n")
    return entries


def find_most_recent_session(session_dir: str, cwd: str | None = None) -> str | None:
    valid_files = []
    for path in iter_session_files(session_dir):
        header = read_session_header(path)
        if not header or (cwd is not None and not _session_cwd_matches(header.get("cwd"), cwd)):
            continue
        try:
            valid_files.append((path, os.stat(path).st_mtime))
        except OSError:
            continue
    return max(valid_files, key=lambda item: item[1])[0] if valid_files else None


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
            os.makedirs(self.sessionDir, mode=0o700, exist_ok=True)

        if sessionFile:
            self.setSessionFile(sessionFile)
        else:
            self.newSession()

    def setSessionFile(self, sessionFile: str) -> None:
        self.sessionFile = resolve_path(sessionFile)
        if os.path.exists(self.sessionFile):
            self.fileEntries = load_entries_from_file(
                self.sessionFile,
                strict=True,
                repair_unterminated=True,
            )
            if not self.fileEntries:
                raise InvalidSessionFileError(self.sessionFile)

            header = next((entry for entry in self.fileEntries if entry.get("type") == "session"), None)
            self.sessionId = (
                str(header.get("id"))
                if isinstance(header, dict) and header.get("id")
                else create_session_id()
            )
            require_current_version(self.fileEntries, self.sessionFile)
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
        self.labelTimestampsById.clear()
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

    def rewrite_file(self) -> None:
        """Persist the in-memory entries to the session file now (public entry for extensions)."""
        self._rewriteFile()

    def _rewriteFile(self) -> None:
        if not self.persist or not self.sessionFile:
            return
        atomic.write_text(self.sessionFile, _dump_jsonl(self.fileEntries), mode=0o600)

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

    def _persist(self, entry: SessionEntry) -> bool:
        """Persist a candidate entry without changing the live session state."""
        if not self.persist or not self.sessionFile:
            return self.flushed

        serialized_entry = _dump_json(entry)

        if (not self.flushed and not _entry_starts_transcript(entry)
                and not any(_entry_starts_transcript(item) for item in self.fileEntries)):
            return False

        if not self.flushed:
            payload = f"{_dump_jsonl(self.fileEntries)}{serialized_entry}\n"
            atomic.write_text(self.sessionFile, payload, mode=0o600)
            return True

        if not os.path.exists(self.sessionFile):
            atomic.write_text(self.sessionFile, f"{serialized_entry}\n", mode=0o600)
        else:
            with Path(self.sessionFile).open("a", encoding="utf-8") as handle:
                handle.write(f"{serialized_entry}\n")
        return True

    def _appendEntry(self, entry: SessionEntry) -> None:
        flushed = self._persist(entry)
        self.fileEntries.append(entry)
        entry_id = entry.get("id")
        if isinstance(entry_id, str):
            self.byId[entry_id] = entry
            self.leafId = entry_id
        self.flushed = flushed

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
        usage: Usage | Mapping[str, Any] | None | object = _UNSET,
        *,
        contextMessages: list[AgentMessage] | None = None,
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
        if usage is not _UNSET and usage is not None:
            entry["usage"] = usage
        if contextMessages is not None:
            entry["contextMessages"] = _copy_context_messages(contextMessages)
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
            raise ValueError(f"Entry {targetId} not found")
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

    def buildContextEntries(self) -> list[SessionEntry]:
        return build_context_entries(self.getEntries(), self.leafId, self.byId)

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
            raise ValueError(f"Entry {branchFromId} not found")
        self.leafId = branchFromId

    def resetLeaf(self) -> None:
        self.leafId = None

    def branchWithSummary(
        self,
        branchFromId: str | None,
        summary: str,
        details: Any = _UNSET,
        fromHook: bool | None | object = _UNSET,
        usage: Usage | Mapping[str, Any] | None | object = _UNSET,
    ) -> str:
        if branchFromId is not None and branchFromId not in self.byId:
            raise ValueError(f"Entry {branchFromId} not found")
        from_id = self.leafId if self.leafId is not None else "root"
        entry: SessionEntry = {
            "type": "branch_summary",
            "id": generate_id(self.byId),
            "parentId": branchFromId,
            "timestamp": _iso_now(),
            "fromId": from_id,
            "summary": summary,
        }
        if details is not _UNSET:
            entry["details"] = details
        if fromHook is not _UNSET:
            entry["fromHook"] = fromHook
        if usage is not _UNSET and usage is not None:
            entry["usage"] = usage
        self._appendEntry(entry)
        return str(entry["id"])

    def createBranchedSession(self, leafId: str) -> str | None:
        previous_session_file = self.sessionFile
        path = self.getBranch(leafId)
        if not path:
            raise ValueError(f"Entry {leafId} not found")

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

        candidate = [header, *path_without_labels, *label_entries]
        flush = self.persist and any(_entry_starts_transcript(entry) for entry in candidate)
        if flush:
            # Do the fallible work before replacing the original session owner/state.
            atomic.write_text(new_session_file, _dump_jsonl(candidate), mode=0o600)

        self.fileEntries = candidate
        self.sessionId = new_session_id
        if self.persist:
            self.sessionFile = new_session_file
        self._buildIndex()
        self.flushed = flush
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
        if not os.path.isfile(resolved_path):
            raise FileNotFoundError(resolved_path)
        entries = load_entries_from_file(resolved_path, strict=True)
        header = next((entry for entry in entries if entry.get("type") == "session"), None)
        cwd = (
            cwdOverride
            if cwdOverride is not None
            else (str(header.get("cwd")) if isinstance(header, dict) and isinstance(header.get("cwd"), str) else os.getcwd())
        )
        directory = normalize_path(sessionDir) if sessionDir else str(Path(resolved_path).parent)
        return cls(cwd, directory, resolved_path, True)

    @classmethod
    def openInMemory(
        cls,
        path: str,
        cwdOverride: str | None = None,
    ) -> SessionManager:
        """Load a session snapshot without retaining or modifying its source file."""
        resolved_path = resolve_path(path)
        if not os.path.isfile(resolved_path):
            raise FileNotFoundError(resolved_path)
        try:
            entries = load_entries_from_file(
                resolved_path,
                strict=True,
                repair_unterminated=False,
            )
        except UnicodeError as error:
            raise InvalidSessionFileError(resolved_path, str(error)) from error
        header = entries[0]
        cwd = (
            cwdOverride
            if cwdOverride is not None
            else (str(header.get("cwd")) if isinstance(header.get("cwd"), str) else os.getcwd())
        )
        manager = cls.inMemory(cwd)
        manager.fileEntries = entries
        manager.sessionId = str(header["id"]) if header.get("id") else create_session_id()
        require_current_version(manager.fileEntries, resolved_path)
        manager._buildIndex()
        return manager

    @classmethod
    def continueRecent(cls, cwd: str, sessionDir: str | None = None) -> SessionManager:
        directory = normalize_path(sessionDir) if sessionDir else get_default_session_dir(cwd)
        most_recent = find_most_recent_session(directory, cwd=cwd)
        if most_recent:
            return cls(cwd, directory, most_recent, True)
        return cls(cwd, directory, None, True)

    @classmethod
    def inMemory(cls, cwd: str | None = None) -> SessionManager:
        return cls(os.getcwd() if cwd is None else cwd, "", None, False)

    @classmethod
    def forkFrom(
        cls,
        sourcePath: str,
        targetCwd: str,
        sessionDir: str | None = None,
        options: NewSessionOptions | None = None,
    ) -> SessionManager:
        resolved_source_path = resolve_path(sourcePath)
        resolved_target_cwd = resolve_path(targetCwd)
        try:
            source_entries = load_entries_from_file(resolved_source_path, strict=True)
        except (OSError, UnicodeError, InvalidSessionFileError) as error:
            raise RuntimeError(
                f"Cannot fork: source session file is empty or invalid: {resolved_source_path}"
            ) from error
        if not source_entries:
            raise RuntimeError(f"Cannot fork: source session file is empty or invalid: {resolved_source_path}")

        source_header = next((entry for entry in source_entries if entry.get("type") == "session"), None)
        if source_header is None:
            raise RuntimeError(f"Cannot fork: source session has no header: {resolved_source_path}")

        directory = normalize_path(sessionDir) if sessionDir else get_default_session_dir(resolved_target_cwd)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        new_session_id = options.id if options and options.id else create_session_id()
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
        # Atomic publish (pi #7707/f4bbbdb): writing the final name directly and getting
        # interrupted halfway (Ctrl-C, crash, disk full) leaves a truncated JSONL under the
        # real name that loads as a valid session next time. Write a temp file in the same
        # directory, then os.replace, which is atomic on one filesystem.
        atomic.write_text(new_session_file, _dump_jsonl([header, *copied_entries]), mode=0o600)
        return cls(resolved_target_cwd, directory, new_session_file, True)

    @classmethod
    async def list(
        cls,
        cwd: str,
        sessionDir: str | None = None,
        onProgress: SessionListProgress | None = None,
    ) -> list[SessionInfo]:
        directory = normalize_path(sessionDir) if sessionDir else get_default_session_dir(cwd)
        sessions = [s for s in await _list_sessions_from_dir(directory, onProgress)
                    if _session_cwd_matches(s.cwd, cwd)]
        sessions.sort(key=lambda session: session.modified, reverse=True)
        return sessions

    @classmethod
    async def listAll(
        cls,
        sessionsRoot: str | SessionListProgress | None = None,
        onProgress: SessionListProgress | None = None,
    ) -> list[SessionInfo]:
        """Every session under one sessions root (its per-cwd buckets), newest first.

        pi session-manager.ts:1655-1663 overloads this the same way: the root is optional and a
        lone callback is the progress reporting, so ``listAll(onProgress)`` keeps working."""
        if callable(sessionsRoot):
            sessionsRoot, onProgress = None, sessionsRoot
        explicit_root = sessionsRoot is not None
        sessions_dir = normalize_path(sessionsRoot) if sessionsRoot else get_sessions_dir()
        try:
            directories = [os.path.join(sessions_dir, entry) for entry in os.listdir(sessions_dir)
                           if entry not in {".git", ".catalog"} and os.path.isdir(os.path.join(sessions_dir, entry))]
        except OSError:
            return []
        if explicit_root:
            directories.insert(0, sessions_dir)  # explicit stores may contain files and cwd buckets
        all_files = list(dict.fromkeys(path for directory in directories for path in iter_session_files(directory)))
        total_files = len(all_files)
        sessions: list[SessionInfo] = []
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


def _parse_jsonl_entries(content: str, *, strict: bool = False) -> list[FileEntry]:
    entries: list[FileEntry] = []
    # JSONL records end at LF; U+0085/U+2028/U+2029 are valid JSON string content.
    for line in content.split("\n"):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            if strict:
                raise
            continue
        if isinstance(parsed, dict):
            entries.append(parsed)
        elif strict:
            raise TypeError("session entries must be JSON objects")
    return entries


def _message_role(message: Any) -> str | None:
    role = read_field(message, "role")
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
    content = read_field(message, "content")
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

        message_timestamp = read_field(message, "timestamp")
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
        if not _is_session_header(header):
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
    except Exception:  # noqa: BLE001 - a corrupt session file is skipped in the picker
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
            except Exception:  # noqa: BLE001 - a corrupt session file is skipped in the picker
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
    files = list(iter_session_files(session_dir))

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
        return datetime.fromisoformat(str(value))
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
    r"""Serialize one entry, keeping non-ASCII readable but never failing to write.

    A provider can stream a lone surrogate: OpenAI-compatible endpoints emit one when an
    emoji is split across token boundaries, which is why every request builder here runs
    `sanitize_surrogates` on the way out. Nothing sanitizes on the way *in*, and upstream
    does not either -- `JSON.stringify` escapes a lone surrogate to `\udXXX` and the write
    succeeds. Python's `ensure_ascii=False` leaves it in the str, and encoding that to
    UTF-8 raises, so a single bad chunk used to take down the whole session file.

    Falling back to escaped output for just that entry reproduces upstream's result: the
    character survives the round trip (`json.loads` gives it back), and every other entry
    keeps its unescaped CJK.
    """
    jsonable = _jsonable(value)
    dumped = json.dumps(jsonable, ensure_ascii=False, separators=(",", ":"))
    try:
        dumped.encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(jsonable, ensure_ascii=True, separators=(",", ":"))
    return dumped


def _dump_jsonl(entries: list[FileEntry]) -> str:
    return "".join(f"{_dump_json(entry)}\n" for entry in entries)


buildContextEntries = build_context_entries
buildSessionContext = build_session_context

__all__ = [
    "CURRENT_SESSION_VERSION",
    "BranchSummaryEntry",
    "CompactionEntry",
    "CustomEntry",
    "CustomMessageEntry",
    "FileEntry",
    "LabelEntry",
    "ModelChangeEntry",
    "NewSessionOptions",
    "ReadonlySessionManager",
    "SessionContext",
    "SessionEntry",
    "SessionEntryBase",
    "SessionHeader",
    "SessionInfo",
    "SessionInfoEntry",
    "SessionListProgress",
    "SessionManager",
    "SessionMessageEntry",
    "SessionTreeNode",
    "ThinkingLevelChangeEntry",
    "buildContextEntries",
    "buildSessionContext",
    "build_context_entries",
    "session_entry_to_context_messages",
    "sessions_root_of",
]

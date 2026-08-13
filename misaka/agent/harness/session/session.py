"""Session tree helpers and high-level session API."""

from __future__ import annotations

from typing import Any

from misaka.agent.harness.messages import (
    create_branch_summary_message,
    create_compaction_summary_message,
    create_custom_message,
)
from misaka.agent.harness.types import SessionContext, SessionError


def build_session_context(path_entries: list[Any]) -> SessionContext:
    thinking_level = "off"
    model: dict[str, str] | None = None
    compaction: Any | None = None

    for entry in path_entries:
        entry_type = _entry_field(entry, "type")
        if entry_type == "thinking_level_change":
            thinking_level = str(_entry_field(entry, "thinkingLevel"))
        elif entry_type == "model_change":
            model = {
                "provider": str(_entry_field(entry, "provider")),
                "modelId": str(_entry_field(entry, "modelId")),
            }
        elif entry_type == "message" and _message_role(_entry_field(entry, "message")) == "assistant":
            assistant_message = _entry_field(entry, "message")
            model = {
                "provider": str(_message_field(assistant_message, "provider")),
                "modelId": str(_message_field(assistant_message, "model")),
            }
        elif entry_type == "compaction":
            compaction = entry

    messages: list[Any] = []

    def append_message(entry: Any) -> None:
        entry_type = _entry_field(entry, "type")
        if entry_type == "message":
            messages.append(_entry_field(entry, "message"))
        elif entry_type == "custom_message":
            messages.append(
                create_custom_message(
                    str(_entry_field(entry, "customType")),
                    _entry_field(entry, "content"),
                    bool(_entry_field(entry, "display")),
                    _entry_field(entry, "details"),
                    str(_entry_field(entry, "timestamp")),
                )
            )
        elif entry_type == "branch_summary" and _entry_field(entry, "summary"):
            messages.append(
                create_branch_summary_message(
                    str(_entry_field(entry, "summary")),
                    str(_entry_field(entry, "fromId")),
                    str(_entry_field(entry, "timestamp")),
                )
            )

    if compaction is not None:
        messages.append(
            create_compaction_summary_message(
                str(_entry_field(compaction, "summary")),
                int(_entry_field(compaction, "tokensBefore")),
                str(_entry_field(compaction, "timestamp")),
            )
        )
        compaction_id = _entry_field(compaction, "id")
        compaction_index = next(
            index
            for index, entry in enumerate(path_entries)
            if _entry_field(entry, "type") == "compaction" and _entry_field(entry, "id") == compaction_id
        )
        first_kept_entry_id = _entry_field(compaction, "firstKeptEntryId")
        found_first_kept = False
        for entry in path_entries[:compaction_index]:
            if _entry_field(entry, "id") == first_kept_entry_id:
                found_first_kept = True
            if found_first_kept:
                append_message(entry)
        for entry in path_entries[compaction_index + 1 :]:
            append_message(entry)
    else:
        for entry in path_entries:
            append_message(entry)

    return SessionContext(messages=messages, thinkingLevel=thinking_level, model=model)


class Session[TMetadata]:
    def __init__(self, storage: Any) -> None:
        self._storage = storage

    async def getMetadata(self) -> TMetadata:
        return await self._storage.getMetadata()

    def getStorage(self) -> Any:
        return self._storage

    async def getLeafId(self) -> str | None:
        return await self._storage.getLeafId()

    async def getEntry(self, id: str):
        return await self._storage.getEntry(id)

    async def getEntries(self) -> list[Any]:
        return await self._storage.getEntries()

    async def getBranch(self, fromId: str | None = None) -> list[Any]:
        leaf_id = fromId if fromId is not None else await self._storage.getLeafId()
        return await self._storage.getPathToRoot(leaf_id)

    async def buildContext(self) -> SessionContext:
        return build_session_context(await self.getBranch())

    async def getLabel(self, id: str) -> str | None:
        return await self._storage.getLabel(id)

    async def getSessionName(self) -> str | None:
        entries = await self._storage.findEntries("session_info")
        if not entries:
            return None
        name = _entry_field(entries[-1], "name")
        if isinstance(name, str) and name.strip():
            return name.strip()
        return None

    async def _append_typed_entry(self, entry: dict[str, Any]) -> str:
        await self._storage.appendEntry(entry)
        return str(entry["id"])

    async def appendMessage(self, message: Any) -> str:
        return await self._append_typed_entry(
            {
                "type": "message",
                "id": await self._storage.createEntryId(),
                "parentId": await self._storage.getLeafId(),
                "timestamp": _create_timestamp(),
                "message": message,
            }
        )

    async def appendThinkingLevelChange(self, thinkingLevel: str) -> str:
        return await self._append_typed_entry(
            {
                "type": "thinking_level_change",
                "id": await self._storage.createEntryId(),
                "parentId": await self._storage.getLeafId(),
                "timestamp": _create_timestamp(),
                "thinkingLevel": thinkingLevel,
            }
        )

    async def appendModelChange(self, provider: str, modelId: str) -> str:
        return await self._append_typed_entry(
            {
                "type": "model_change",
                "id": await self._storage.createEntryId(),
                "parentId": await self._storage.getLeafId(),
                "timestamp": _create_timestamp(),
                "provider": provider,
                "modelId": modelId,
            }
        )

    async def appendCompaction(
        self,
        summary: str,
        firstKeptEntryId: str,
        tokensBefore: int,
        details: Any | None = None,
        fromHook: bool | None = None,
    ) -> str:
        return await self._append_typed_entry(
            {
                "type": "compaction",
                "id": await self._storage.createEntryId(),
                "parentId": await self._storage.getLeafId(),
                "timestamp": _create_timestamp(),
                "summary": summary,
                "firstKeptEntryId": firstKeptEntryId,
                "tokensBefore": tokensBefore,
                "details": details,
                "fromHook": fromHook,
            }
        )

    async def appendCustomEntry(self, customType: str, data: Any | None = None) -> str:
        return await self._append_typed_entry(
            {
                "type": "custom",
                "id": await self._storage.createEntryId(),
                "parentId": await self._storage.getLeafId(),
                "timestamp": _create_timestamp(),
                "customType": customType,
                "data": data,
            }
        )

    async def appendCustomMessageEntry(
        self,
        customType: str,
        content: str | list[Any],
        display: bool,
        details: Any | None = None,
    ) -> str:
        return await self._append_typed_entry(
            {
                "type": "custom_message",
                "id": await self._storage.createEntryId(),
                "parentId": await self._storage.getLeafId(),
                "timestamp": _create_timestamp(),
                "customType": customType,
                "content": content,
                "display": display,
                "details": details,
            }
        )

    async def appendLabel(self, targetId: str, label: str | None) -> str:
        if await self._storage.getEntry(targetId) is None:
            raise SessionError("not_found", f"Entry {targetId} not found")
        return await self._append_typed_entry(
            {
                "type": "label",
                "id": await self._storage.createEntryId(),
                "parentId": await self._storage.getLeafId(),
                "timestamp": _create_timestamp(),
                "targetId": targetId,
                "label": label,
            }
        )

    async def appendSessionName(self, name: str) -> str:
        return await self._append_typed_entry(
            {
                "type": "session_info",
                "id": await self._storage.createEntryId(),
                "parentId": await self._storage.getLeafId(),
                "timestamp": _create_timestamp(),
                "name": name.strip(),
            }
        )

    async def moveTo(self, entryId: str | None, summary: dict[str, Any] | None = None) -> str | None:
        if entryId is not None and await self._storage.getEntry(entryId) is None:
            raise SessionError("not_found", f"Entry {entryId} not found")
        await self._storage.setLeafId(entryId)
        if summary is None:
            return None
        return await self._append_typed_entry(
            {
                "type": "branch_summary",
                "id": await self._storage.createEntryId(),
                "parentId": entryId,
                "timestamp": _create_timestamp(),
                "fromId": entryId or "root",
                "summary": summary["summary"],
                "details": summary.get("details"),
                "fromHook": summary.get("fromHook"),
            }
        )


def _entry_field(entry: Any, name: str) -> Any:
    if isinstance(entry, dict):
        return entry.get(name)
    return getattr(entry, name)


def _message_role(message: Any) -> Any:
    if isinstance(message, dict):
        return message.get("role")
    return getattr(message, "role", None)


def _message_field(message: Any, name: str) -> Any:
    if isinstance(message, dict):
        return message.get(name)
    return getattr(message, name)


def _create_timestamp() -> str:
    from misaka.agent.harness.session.repo_utils import create_timestamp

    return create_timestamp()


buildSessionContext = build_session_context

__all__ = ["Session", "buildSessionContext", "build_session_context"]

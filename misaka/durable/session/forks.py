"""Select the documents a conversation fork copies.

Translated from pi's ``packages/durable/src/session/forks.ts``.
"""

from __future__ import annotations

from typing import Any

from misaka.chord.context import Context

from ..documents import addressId
from ..types import ConversationId, Cursor, DocumentPoint, EntryId, Storage

SCAN_PAGE_SIZE = 256


async def prepareForkDocumentCopies(
    storage: Storage,
    parentConversationId: ConversationId,
    at: EntryId,
    childConversationId: ConversationId,
    context: Context,
) -> list[dict[str, Any]]:
    """Select every persisted conversation document copied by one fork: a list of ``{"record": create, "source":
    {"id", "at"}}``."""
    entry = await storage.entry(parentConversationId, at, context)
    if entry is None:
        raise RuntimeError(f"Entry {at} is not visible from conversation {parentConversationId}")

    copies: list[dict[str, Any]] = []
    copiedAddresses: set[str] = set()
    await collectCopies(
        storage,
        {"kind": "conversation", "conversationId": entry["entry"]["conversationId"]},
        entry["commitSeq"],
        "asOf",
        childConversationId,
        copies,
        copiedAddresses,
        context,
    )
    await collectCopies(
        storage,
        {"kind": "conversation", "conversationId": parentConversationId},
        "current",
        "current",
        childConversationId,
        copies,
        copiedAddresses,
        context,
    )
    return copies


async def collectCopies(
    storage: Storage,
    scope: dict[str, Any],
    at: DocumentPoint,
    policy: str,
    childConversationId: ConversationId,
    copies: list[dict[str, Any]],
    copiedAddresses: set[str],
    context: Context,
) -> None:
    cursor: Cursor | None = None
    while True:
        page = await storage.scanDocuments({"scope": scope, "at": at}, SCAN_PAGE_SIZE, cursor, context)
        for source in page["items"]:
            if source["scope"]["kind"] != "conversation" or source["fork"] != policy:
                continue
            id = await storage.mintId()
            identity: dict[str, Any] = {
                "id": id,
                "kind": source["kind"],
                **({} if "key" not in source else {"key": source["key"]}),
                "scope": {"kind": "conversation", "conversationId": childConversationId},
            }
            record = (
                {**identity, "history": "latest", "fork": source["fork"]}
                if source["history"] == "latest"
                else {**identity, "history": "rewindable", "fork": source["fork"]}
            )
            copyAddress = addressId(record)
            if copyAddress in copiedAddresses:
                member = record["kind"] if "key" not in record else f"{record['kind']}/{record['key']}"
                raise RuntimeError(f"Fork selects multiple source documents for {member}")
            copiedAddresses.add(copyAddress)
            copies.append({"record": record, "source": {"id": source["id"], "at": at}})
        cursor = page.get("next")
        if cursor is None:
            break


__all__ = ["prepareForkDocumentCopies"]

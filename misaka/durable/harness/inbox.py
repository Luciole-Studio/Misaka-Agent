"""The built-in queue of one conversation's submissions waiting for a boundary.

Translated from pi's ``packages/durable/src/harness/inbox.ts`` (v1.3.0). An item is ``{"id", "mode": "steer" |
"followUp", "content", "entry"?}`` (user input for a run, with the ``InputEntry`` it is placed as when not ``pi.user``) or
``{"id", "mode": "write", "entry"}`` (an entry write, an ``EntryDraft`` stored as plain JSON); a boundary is ``{"conversationId", "inbox", "steeringMode", "followUpMode", "head"}`` and a boundary result
``{"users", "reset"}``.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

from misaka.chord.json import copyJson
from misaka.utils.js import UNDEFINED

from ..documents import defineDoc
from ..entries import UserEntry
from ..types import ConversationId, EntryRecord, SubmissionId, Tx

InboxDoc = defineDoc(
    {
        "kind": "pi.inbox",
        "version": 1,
        "scope": "conversation",
        "history": "latest",
        "fork": "initial",
        "initial": lambda: {"items": []},
        "checkpointWhen": lambda value, *_: len(value["items"]) == 0,
    }
)
"""Built-in queue of one conversation's submissions waiting for a boundary, in ID order."""


async def prepareBoundary(tx: Tx, conversationId: ConversationId, modes: dict[str, Any]) -> dict[str, Any]:
    """Read what a boundary needs: the settings' queue modes (read on the Session line) and the newest head marker's
    ``head``, the start of the active range. Table reads must precede the commit's first table write, so callers
    prepare the boundary at the start of their commit."""
    marker = await tx.latestHeadMarker(conversationId)
    head = None if marker is None else marker["head"]
    inbox = await tx.doc(InboxDoc, conversationId)
    return {
        "conversationId": conversationId,
        "inbox": inbox,
        "steeringMode": modes["steeringMode"],
        "followUpMode": modes["followUpMode"],
        "head": head,  # Advanced by heads written in the commit.
    }


async def applyBoundary(tx: Tx, boundary: dict[str, Any], at: str, now: int) -> dict[str, Any]:
    """Place the queued items a boundary selects (spec section 6): every write, the first or all steers, and at ``final``
    the first or all follow-ups. A selected reset turns a ``postTools`` boundary into ``final``. Writes are placed first
    and user items after them, each in ID order, so user items queued before a reset run in the new context. A write
    whose head targets an entry before the active range, including a range started earlier in this commit, is stale.
    Selected and stale items are removed positionally."""
    conversationId = boundary["conversationId"]
    items = boundary["inbox"]["items"]
    reset = hasQueuedReset(items)
    final = at == "final" or reset

    def pick(mode: str, queueMode: str) -> list[int]:
        indexes = [index for index, item in enumerate(items) if item["mode"] == mode]
        return indexes if queueMode == "all" else indexes[:1]

    writes = [index for index, item in enumerate(items) if item["mode"] == "write"]
    users = [*pick("steer", boundary["steeringMode"]), *(pick("followUp", boundary["followUpMode"]) if final else [])]
    users.sort()

    # ``appendEntry()`` copies the drafts' values; the items are removed only afterwards.
    for index in writes:
        item = items[index]
        draft: dict[str, Any] = copyJson(item["entry"])  # type: ignore[assignment]
        if isStale(boundary, draft):
            tx.settleSubmission(item["id"], {"status": "unanswered", "reason": "stale"})
            continue
        entry = await tx.appendEntry(conversationId, draft)
        if draft.get("head") is not None:
            boundary["head"] = entry["id"] if draft["head"] == "self" else draft["head"]
        tx.placeSubmission(item["id"], entry["id"])
    placed: list[SubmissionId] = []
    for index in users:
        item = items[index]
        entry = await appendInput(tx, conversationId, copyJson(item["content"]), item.get("entry", None), now)
        tx.placeSubmission(item["id"], entry["id"])
        placed.append(item["id"])
    for index in sorted([*writes, *users], reverse=True):
        items.splice(index, 1)
    return {"users": placed, "reset": reset}


def appendInput(
    tx: Tx, conversationId: ConversationId, content: Any, entry: dict[str, Any] | None, now: int
) -> Awaitable[EntryRecord]:
    """Append the entry an input is placed as: ``entry``'s kind and data, or ``pi.user``, with the user message of
    ``content``."""
    message = {"role": "user", "content": content, "timestamp": now}
    if entry is None:
        return tx.appendEntry(UserEntry, conversationId, {"model": [message]})
    return tx.appendEntry(conversationId, {**pickInputEntry(entry), "model": [message]})


def pickInputEntry(entry: Any) -> dict[str, Any]:
    """The kind and data of an input's entry as strict JSON, without other fields an untyped caller might pass."""
    kind = entry["kind"]
    data = entry.get("data", UNDEFINED)
    picked = {"kind": kind} if data is UNDEFINED else {"kind": kind, "data": data}
    return copyJson(picked, {"omitUndefinedProperties": True})  # type: ignore[return-value]


def isDisplayOnly(entry: dict[str, Any]) -> bool:
    """Whether a write is display-only: it has no ``model``, ``head``, or ``edits`` (present counts, even if empty), so
    it changes nothing the model sees and needs no boundary."""
    return (
        entry.get("model", UNDEFINED) is UNDEFINED
        and entry.get("head", UNDEFINED) is UNDEFINED
        and entry.get("edits", UNDEFINED) is UNDEFINED
    )


def hasQueuedReset(items: Any) -> bool:
    """Whether a reset (``head: "self"`` write) is queued; entries appended before it leave the active range when it
    lands."""
    return any(item["mode"] == "write" and item["entry"].get("head") == "self" for item in items)


def isStale(boundary: dict[str, Any], entry: dict[str, Any]) -> bool:
    """Whether a head write targets an entry before the active range, so placing it would bring back cut history."""
    head = entry.get("head")
    return (
        isinstance(head, int)
        and not isinstance(head, bool)
        and boundary["head"] is not None
        and head < boundary["head"]
    )


async def removeInboxItem(tx: Tx, conversationId: ConversationId, id: SubmissionId) -> None:
    """Remove a withdrawn submission's item; the caller settles the submission."""
    items = (await tx.doc(InboxDoc, conversationId))["items"]
    index = next((at for at, item in enumerate(items) if item["id"] == id), -1)
    if index >= 0:
        items.splice(index, 1)


async def withdrawQueuedInputs(tx: Tx, conversationId: ConversationId) -> None:
    """Withdraw every queued input of a conversation, as ``Conversation.abort()`` and abort cascades do: each settles
    ``unanswered`` with ``aborted`` and leaves the inbox; queued writes stay for later placement."""
    items = (await tx.doc(InboxDoc, conversationId))["items"]
    for index in range(len(items) - 1, -1, -1):
        item = items[index]
        if item["mode"] == "write":
            continue
        tx.settleSubmission(item["id"], {"status": "unanswered", "reason": "aborted"})
        items.splice(index, 1)


__all__ = [
    "InboxDoc",
    "appendInput",
    "applyBoundary",
    "hasQueuedReset",
    "isDisplayOnly",
    "isStale",
    "pickInputEntry",
    "prepareBoundary",
    "removeInboxItem",
    "withdrawQueuedInputs",
]

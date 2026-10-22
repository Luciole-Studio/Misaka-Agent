"""The active transcript and the model context derived from it.

Translated from pi's ``packages/durable/src/harness/context.ts`` (v1.3.0). Records, messages and views are plain
``dict`` objects with the upstream keys; a view's ``head`` is ``None`` (upstream ``undefined``) when there is no head
marker. Python has no frozen objects: upstream freezes a kept range's entries so no reader can change them, while here
the range keeps them private and ``readContextFrom()`` returns a detached copy of its view.
"""

from __future__ import annotations

from typing import Any

from misaka.chord.context import Context
from misaka.utils.js import UNDEFINED

from ..entries import SystemEntry
from ..ids import idFromNumber
from ..types import ConversationId, Cursor, EntryId, EntryRecord, Storage

SCAN_PAGE_SIZE = 256
EXCLUDED_STOP_REASONS = frozenset({"aborted", "error", "deferred"})
MISSING_RESULT_TEXT = "Tool result unavailable: history ends before this call completed."

ContextBounds = dict[str, Any]
"""Head marker (``None`` without one) and newest visible entry that fix one committed context range:
``{"head": EntryRecord | None, "tail": EntryId}``."""


async def captureContextBounds(
    storage: Storage, conversationId: ConversationId, context: Context, at: EntryId | None = None
) -> ContextBounds | None:
    """Capture the bounds of the current context, or of the context cut off at the visible entry ``at``, with two O(1)
    reads. Run this on the Session line; entries at or below the tail are immutable, so ``deriveContext()`` can then
    scan them off the line."""
    tail: EntryId
    if at is None:
        page = await storage.scanEntries({"conversationId": conversationId}, 1, None, context)
        if len(page["items"]) == 0:
            return None
        tail = page["items"][0]["id"]
    else:
        if await storage.entry(conversationId, at, context) is None:
            raise RuntimeError(f"Entry {at} is not visible from conversation {conversationId}")
        tail = at
    return {"head": await storage.findLatestHeadMarker(conversationId, tail, context), "tail": tail}


ContextRange = dict[str, Any]
"""One context read: the visible entries it scanned, from the head marker's head, or transcript start, through
``bounds["tail"]``, oldest first, and the view derived from them. Entries at or below the tail never change, so a later
read with the same head marker extends both. Keys: ``bounds``, ``entries``, ``view``, ``edited`` (targets of the edits
in ``entries``), ``settled`` (``view["messages"]`` from the contributed messages before the last assistant message; tool
results are ordered within the messages up to the next assistant message, so later entries cannot change these),
``open`` (contributed messages from the last assistant message on, before tool result ordering) and ``baseline`` (the
baseline's system message; see ``findBaseline()``)."""

type Baseline = dict[str, Any] | None | Any
"""A baseline's system message, ``None`` when the range cannot have one, or ``UNDEFINED`` while a later entry may be
one."""


async def readContextFrom(
    session: Any,
    storage: Storage,
    conversationId: ConversationId,
    context: Context,
    at: EntryId | None,
    previous: ContextRange | None,
) -> dict[str, Any]:
    """``readContext()`` that reuses ``previous``, an earlier range of the same conversation: with the same head marker,
    only entries after its tail are scanned. Returns the view and the range to pass to the next read (``{"view",
    "range"}``, ``range`` ``None`` without entries). A range outlives the read, so it keeps its entries to itself and
    the returned view is a detached copy."""
    bounds = await session.readOnLine(lambda: captureContextBounds(storage, conversationId, context, at))
    if bounds is None:
        return {"view": emptyView(), "range": None}
    range_: ContextRange
    previousHead = None if previous is None or previous["bounds"]["head"] is None else previous["bounds"]["head"]["id"]
    if previous is None or previousHead != (None if bounds["head"] is None else bounds["head"]["id"]):
        range_ = deriveRange(bounds, await scanRange(storage, conversationId, rangeQuery(bounds), context))
    elif bounds["tail"] == previous["bounds"]["tail"]:
        range_ = previous
    elif bounds["tail"] < previous["bounds"]["tail"]:
        range_ = deriveRange(bounds, [entry for entry in previous["entries"] if entry["id"] <= bounds["tail"]])
    else:
        minEntryId = idFromNumber(previous["bounds"]["tail"] + 1)
        added = await scanRange(storage, conversationId, {"minEntryId": minEntryId, "maxEntryId": bounds["tail"]}, context)
        range_ = extendRange(previous, bounds, added)
    # MISAKA fork: a detached copy in place of upstream's frozen objects; one memo keeps the view's sharing (a message
    # in `contributions` is the one in `messages`, as upstream).
    view = range_["view"]
    memo: dict[int, Any] = {}
    return {
        "view": {
            **view,
            "head": _detach(view["head"], memo),
            "entries": _detach(view["entries"], memo),
            "contributions": _detach(view["contributions"], memo),
            "messages": _detach(view["messages"], memo),
        },
        "range": range_,
    }


async def readContext(
    session: Any, storage: Storage, conversationId: ConversationId, context: Context, at: EntryId | None = None
) -> dict[str, Any]:
    """Committed context of one conversation: bounds captured on the Session line, entries derived off it."""
    bounds = await session.readOnLine(lambda: captureContextBounds(storage, conversationId, context, at))
    return await deriveContext(storage, conversationId, bounds, context)


async def deriveContext(
    storage: Storage, conversationId: ConversationId, bounds: ContextBounds | None, context: Context
) -> dict[str, Any]:
    """Derive the active transcript and model context of one conversation within captured bounds.

    H = newest visible head marker; the range runs from ``H.head`` (or transcript start) through the tail. Per target,
    the newest edit in the range wins. Context entries are H followed by the range's non-head entries."""
    if bounds is None:
        return emptyView()
    view: dict[str, Any] = deriveRange(bounds, await scanRange(storage, conversationId, rangeQuery(bounds), context))["view"]
    return view


def emptyView() -> dict[str, Any]:
    return {"head": None, "entries": [], "contributions": [], "messages": []}


def deriveRange(bounds: ContextBounds, entries: list[EntryRecord]) -> ContextRange:
    """Derive the context view from the entries scanned within ``bounds``."""
    edits: dict[EntryId, dict[str, Any]] = {}
    # Edits of every entry in the range count, including older head markers that ``selectActive()`` drops.
    for entry in entries:
        for edit in entry.get("edits") or []:
            edits[edit["target"]] = edit
    active = selectActive(bounds["head"], entries)
    contributions = [contribute(entry, edits.get(entry["id"])) for entry in active]
    settled, open_ = settle([], [message for contributed in contributions for message in contributed])
    baseline = findBaseline(bounds["head"], active)
    messages = leadWithSystem([*settled, *orderToolResults(open_)], baseline)
    view = {"head": bounds["head"], "entries": active, "contributions": contributions, "messages": messages}
    return {
        "bounds": bounds,
        "entries": entries,
        "view": view,
        "edited": set(edits),
        "settled": settled,
        "open": open_,
        "baseline": baseline,
    }


def extendRange(previous: ContextRange, bounds: ContextBounds, added: list[EntryRecord]) -> ContextRange:
    """``previous`` extended by the entries ``added`` after its tail under the same head marker: only their
    contributions and the open messages are derived. An added edit can change an earlier entry, so it derives the whole
    range again."""
    entries = [*previous["entries"], *added]
    if any(
        entry.get("edits") is not None or entry.get("head") is not None or entry["id"] in previous["edited"]
        for entry in added
    ):
        return deriveRange(bounds, entries)
    contributions = [contribute(entry, None) for entry in added]
    settled, open_ = settle(
        previous["settled"], [*previous["open"], *[message for contributed in contributions for message in contributed]]
    )
    baseline = previous["baseline"] if previous["baseline"] is not UNDEFINED else findBaseline(bounds["head"], added)
    view = {
        "head": bounds["head"],
        "entries": [*previous["view"]["entries"], *added],
        "contributions": [*previous["view"]["contributions"], *contributions],
        "messages": leadWithSystem([*settled, *orderToolResults(open_)], baseline),
    }
    return {
        "bounds": bounds,
        "entries": entries,
        "view": view,
        "edited": previous["edited"],
        "settled": settled,
        "open": open_,
        "baseline": baseline,
    }


def findBaseline(head: dict[str, Any] | None, entries: list[EntryRecord]) -> Baseline:
    """The baseline among ``entries``, oldest first: the system message of the first ``pi.system`` entry after the head
    marker, or of the transcript without one, when generation wrote it with every section and tool in effect. After a
    head marker it always does, and omits the retained ``pi.system`` entries. Without one it does when it saw no system
    message before, which holds unless an earlier entry carries one, even if an edit omits it later. An edit that
    targets the baseline contributes other messages, or none, so none leads; a later entry does not take its place."""
    for entry in entries:
        if head is not None and entry["id"] <= head["id"]:
            continue
        if SystemEntry.is_(entry):
            return next((message for message in entry.get("model") or [] if message["role"] == "system"), None)
        if head is None and carriesSystemMessage(entry):
            return None
    return UNDEFINED


def carriesSystemMessage(entry: EntryRecord) -> bool:
    """Whether an entry's own messages, or the replacement messages of its edits, include a system message."""
    return any(message["role"] == "system" for message in entry.get("model") or []) or any(
        edit["action"] == "replace" and any(message["role"] == "system" for message in edit["messages"])
        for edit in entry.get("edits") or []
    )


def leadWithSystem(messages: list[dict[str, Any]], baseline: Baseline) -> list[dict[str, Any]]:
    """Move the first system message to the front when only user messages precede it, or when it is the baseline's. A
    run's input is committed before generation renders the system prompt, so a transcript starts with the input followed
    by the baseline, and the range after a compaction with the kept history, assistant messages included. Providers
    treat only a leading system message as the initial prompt and tool set; without it, a later tool change rewrites the
    request's tool list and invalidates the whole prompt cache. Only the first system message moves, so replaying system
    messages in order gives the same sections and tools. Any other system message, before an assistant message, stays
    where it was committed: moving it would change what it means to the model."""
    index = next((index for index, message in enumerate(messages) if message["role"] == "system"), -1)
    if index <= 0:
        return messages
    system = messages[index]
    if system is not baseline and any(message["role"] != "user" for message in messages[:index]):
        return messages
    return [system, *messages[:index], *messages[index + 1 :]]


def contribute(entry: EntryRecord, edit: dict[str, Any] | None) -> list[dict[str, Any]]:
    """One active entry's model messages after its edit and excluded stop reasons, before tool result ordering."""
    if edit is not None and edit["action"] == "omit":
        return []
    contributed = edit["messages"] if edit is not None and edit["action"] == "replace" else (entry.get("model") or [])
    return [
        message
        for message in contributed
        if message["role"] != "assistant" or message["stopReason"] not in EXCLUDED_STOP_REASONS
    ]


def _detach(value: Any, memo: dict[int, Any]) -> Any:
    """Copy a JSON value; a list or dict met twice through ``memo`` is copied once, so the copy keeps its sharing."""
    if not isinstance(value, list | dict):
        return value
    copied = memo.get(id(value))
    if copied is not None:
        return copied
    if isinstance(value, list):
        result: Any = []
        memo[id(value)] = result
        result.extend(_detach(item, memo) for item in value)
    else:
        result = {}
        memo[id(value)] = result
        for key, item in value.items():
            result[key] = _detach(item, memo)
    return result


def settle(
    settled: list[dict[str, Any]], open: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Move the ordered messages before the last assistant message of ``open_`` to ``settled``. ``orderToolResults()``
    of a sequence equals the concatenation over its parts when each later part starts with an assistant message."""
    last = next((index for index in range(len(open) - 1, -1, -1) if open[index]["role"] == "assistant"), -1)
    if last <= 0:
        return settled, open
    return [*settled, *orderToolResults(open[:last])], open[last:]


async def activeEntries(
    storage: Storage, conversationId: ConversationId, bounds: ContextBounds | None, context: Context
) -> list[EntryRecord]:
    """The raw active entries within captured bounds, without deriving model context."""
    if bounds is None:
        return []
    return selectActive(bounds["head"], await scanRange(storage, conversationId, rangeQuery(bounds), context))


def rangeQuery(bounds: ContextBounds) -> dict[str, Any]:
    """The visible range of ``bounds``: from the head marker's head, or transcript start, through the tail."""
    head = bounds["head"]
    return {"maxEntryId": bounds["tail"]} if head is None else {"minEntryId": head["head"], "maxEntryId": bounds["tail"]}


async def scanRange(
    storage: Storage, conversationId: ConversationId, range: dict[str, Any], context: Context
) -> list[EntryRecord]:
    """Visible entries within ``range_``, oldest first."""
    entries: list[EntryRecord] = []
    cursor: Cursor | None = None
    while True:
        page = await storage.scanEntries({"conversationId": conversationId, **range}, SCAN_PAGE_SIZE, cursor, context)
        entries.extend(page["items"])
        cursor = page.get("next")
        if cursor is None:
            break
    entries.reverse()
    return entries


def selectActive(head: EntryRecord | None, range: list[EntryRecord]) -> list[EntryRecord]:
    """The head marker followed by the range's non-head entries, or the whole range without a marker."""
    return range if head is None else [head, *[entry for entry in range if entry.get("head") is None]]


def orderToolResults(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Place each assistant's tool results directly after it in call order. Results are taken from the messages before
    the next assistant; a missing result is synthesized and unmatched results are dropped."""
    ordered: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if message["role"] == "toolResult":
            continue
        ordered.append(message)
        if message["role"] != "assistant":
            continue
        calls = [content for content in message["content"] if content["type"] == "toolCall"]
        if len(calls) == 0:
            continue
        results: dict[str, int] = {}
        following = index + 1
        while following < len(messages) and messages[following]["role"] != "assistant":
            candidate = messages[following]
            if candidate["role"] == "toolResult" and candidate["toolCallId"] not in results:
                results[candidate["toolCallId"]] = following
            following += 1
        for call in calls:
            resultIndex = results.get(call["id"])
            ordered.append(
                missingResult(call, message["timestamp"]) if resultIndex is None else messages[resultIndex]
            )
    return ordered


def missingResult(call: dict[str, Any], timestamp: int) -> dict[str, Any]:
    return {
        "role": "toolResult",
        "toolCallId": call["id"],
        "toolName": call["name"],
        "content": [{"type": "text", "text": MISSING_RESULT_TEXT}],
        "isError": True,
        "details": {"reason": "missing_result"},
        "timestamp": timestamp,
    }


__all__ = [
    "ContextBounds",
    "ContextRange",
    "activeEntries",
    "captureContextBounds",
    "deriveContext",
    "orderToolResults",
    "readContext",
    "readContextFrom",
]

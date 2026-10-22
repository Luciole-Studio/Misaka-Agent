"""The per-conversation ledger of spend.

Translated from pi's ``packages/durable/src/harness/usage.ts``. A usage is the strict-JSON ``dict`` of a pi-ai ``Usage``.
"""

from __future__ import annotations

from typing import Any, TypedDict

from misaka.chord.json import copyJson
from misaka.utils.js import UNDEFINED, nullishCoalesce

from ..documents import defineDoc
from ..types import ConversationId, Tx


class UsageState(TypedDict):
    """Ledger of one conversation's own spend: its entries, and compaction summarization attempts, which have none."""

    models: dict[str, Any]  # Assistant entries and summarization attempts, keyed ``provider/modelId``.
    tools: dict[str, Any]  # Tool results, keyed by tool name; their usage has no model identity.


UsageDoc = defineDoc(
    {
        "kind": "pi.usage",
        "version": 1,
        "scope": "conversation",
        "history": "latest",
        "fork": "initial",
        "initial": lambda: {"models": {}, "tools": {}},
        "checkpointWhen": lambda *_: True,
    }
)


async def recordUsage(tx: Tx, conversationId: ConversationId, bucket: str, key: str, usage: dict[str, Any]) -> None:
    """Add ``usage`` to one bucket of the conversation's ``pi.usage``, in the commit that records the response."""
    totals = (await tx.doc(UsageDoc, conversationId))[bucket]
    # A tool may be called ``toString``; dictionaries have no inherited keys, so a plain lookup is exact here.
    total = totals.get(key, UNDEFINED)
    # Providers may leave optional counters ``undefined``; drafts take strict JSON.
    if total is UNDEFINED:
        totals[key] = copyJson(usage, {"omitUndefinedProperties": True})
    else:
        addUsage(total, usage)


def addUsage(total: Any, usage: dict[str, Any]) -> None:
    """Add every counter of ``usage`` to ``total``; optional counters are added once either side reports them."""
    total["input"] += usage["input"]
    total["output"] += usage["output"]
    total["cacheRead"] += usage["cacheRead"]
    total["cacheWrite"] += usage["cacheWrite"]
    total["totalTokens"] += usage["totalTokens"]
    if usage.get("cacheWrite1h") is not None:
        total["cacheWrite1h"] = nullishCoalesce(total.get("cacheWrite1h"), 0) + usage["cacheWrite1h"]
    if usage.get("reasoning") is not None:
        total["reasoning"] = nullishCoalesce(total.get("reasoning"), 0) + usage["reasoning"]
    total["cost"]["input"] += usage["cost"]["input"]
    total["cost"]["output"] += usage["cost"]["output"]
    total["cost"]["cacheRead"] += usage["cost"]["cacheRead"]
    total["cost"]["cacheWrite"] += usage["cost"]["cacheWrite"]
    total["cost"]["total"] += usage["cost"]["total"]


def addUsageState(sum: UsageState, state: UsageState) -> None:
    """Add every bucket of ``state`` into ``sum``."""
    for bucket in ("models", "tools"):
        for key, usage in list(state[bucket].items()):  # type: ignore[literal-required]
            total = sum[bucket].get(key, UNDEFINED)  # type: ignore[literal-required]
            if total is not UNDEFINED:
                addUsage(total, usage)
            else:
                sum[bucket][key] = copyJson(usage)  # type: ignore[literal-required]


__all__ = ["UsageDoc", "UsageState", "addUsage", "addUsageState", "recordUsage"]

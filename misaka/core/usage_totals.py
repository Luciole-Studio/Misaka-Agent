"""Session usage aggregation shared by stats and interactive displays."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from misaka.utils.values import read_field

if TYPE_CHECKING:
    from misaka.ai.types import Usage
    from misaka.core.session_manager import SessionEntry


@dataclass(slots=True)
class UsageTotals:
    input: int = 0
    output: int = 0
    cacheRead: int = 0
    cacheWrite: int = 0
    cost: float = 0.0


@dataclass(slots=True)
class UsageCostBreakdownEntry:
    key: str
    cost: float
    tokens: int


def createUsageTotals() -> UsageTotals:
    return UsageTotals()


def addUsageToTotals(totals: UsageTotals, usage: Usage | Mapping[str, Any]) -> None:
    cost = read_field(usage, "cost") or {}
    totals.input += int(read_field(usage, "input", 0) or 0)
    totals.output += int(read_field(usage, "output", 0) or 0)
    totals.cacheRead += int(read_field(usage, "cacheRead", 0) or 0)
    totals.cacheWrite += int(read_field(usage, "cacheWrite", 0) or 0)
    totals.cost += float(read_field(cost, "total", 0) or 0)


def getUsageCostBreakdown(
    entries: Sequence[SessionEntry],
) -> list[UsageCostBreakdownEntry]:
    totals_by_key: dict[str, UsageTotals] = {}

    for entry in entries:
        key: str | None = None
        usage: Any = None
        entry_type = read_field(entry, "type")
        if entry_type == "message":
            message = read_field(entry, "message")
            role = read_field(message, "role")
            if role == "assistant":
                model = read_field(message, "model", "")
                response_model = read_field(message, "responseModel", model)
                key = f"{read_field(message, 'provider', '')}/{response_model}"
                usage = read_field(message, "usage")
            elif role == "toolResult":
                usage = read_field(message, "usage")
                if usage is not None:
                    key = "Tools/summaries"
        elif entry_type == "usage":
            key = f"{read_field(entry, 'provider', '')}/{read_field(entry, 'model', '')}"
            usage = read_field(entry, "usage")
        elif entry_type in ("branch_summary", "compaction"):
            usage = read_field(entry, "usage")
            if usage is not None:
                key = "Tools/summaries"

        if key is None or usage is None:
            continue
        totals = totals_by_key.get(key)
        if totals is None:
            totals = createUsageTotals()
            totals_by_key[key] = totals
        addUsageToTotals(totals, usage)

    result = [
        UsageCostBreakdownEntry(
            key=key,
            cost=totals.cost,
            tokens=totals.input + totals.output + totals.cacheRead + totals.cacheWrite,
        )
        for key, totals in totals_by_key.items()
        if totals.cost > 0
        or totals.input + totals.output + totals.cacheRead + totals.cacheWrite > 0
    ]
    result.sort(key=lambda entry: entry.cost, reverse=True)
    return result


__all__ = [
    "UsageCostBreakdownEntry",
    "UsageTotals",
    "addUsageToTotals",
    "createUsageTotals",
    "getUsageCostBreakdown",
]

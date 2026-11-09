"""Translated from pi's ``coding-agent/src/core/cache-stats.ts`` (v1.1.0).

MISAKA fork: ``collectCacheMisses`` keys its result by ``id(message)``: pydantic messages are unhashable, and upstream's
``Map`` is keyed by reference anyway.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from misaka.utils.values import read_field

# Prompt-cache TTL: idle gaps longer than this are worth mentioning as the
# likely cause of a miss. Anthropic's default cache TTL is 5 minutes.
CACHE_TTL_MS = 5 * 60 * 1000

# Per-turn misses at or below this are cache breakpoint granularity noise.
NOISE_FLOOR_TOKENS = 1024


@dataclass(slots=True)
class CacheMiss:
    """A counted cache miss on a single assistant message."""

    # Prompt tokens that were in the previous turn's prompt but not read from cache.
    missedTokens: float
    # Extra dollars paid vs. a full cache hit; 0 when pricing is unknown.
    missedCost: float
    # Milliseconds since the previous request (which last refreshed the cache).
    idleMs: float
    # True when the model changed relative to the previous request.
    modelChanged: bool


@dataclass(slots=True)
class CacheWasteTotals:
    missedTokens: float
    missedCost: float
    # Number of counted misses (turns above the noise floor).
    missCount: int


class ModelPriceSource(Protocol):
    """Minimal pricing lookup, satisfied by ModelRuntime. Cost is $/million tokens."""

    def getModel(self, provider: str, modelId: str) -> Any: ...


@dataclass(slots=True)
class PreviousRequest:
    """The last request seen by the scan; everything in its prompt should be cached."""

    promptTokens: float
    modelKey: str
    timestamp: float
    # Sticky: some earlier request in this scan segment reported cache activity.
    # Distinguishes a total miss on a cache-read-only provider (OpenAI-style,
    # writes unreported) from a provider that never reports caching at all.
    reportedCache: bool


def _usage(source: Any) -> tuple[float, float, float]:
    usage = read_field(source, "usage")
    return (read_field(usage, "input", 0), read_field(usage, "cacheRead", 0), read_field(usage, "cacheWrite", 0))


def _dateParse(value: Any) -> float:
    """``Date.parse(value)``: milliseconds since the epoch, NaN when the text is not a date."""
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000
    except ValueError:
        return math.nan


def detectMiss(prev: PreviousRequest | None, message: Any, models: Any) -> CacheMiss | None:
    """Compute the cache miss for one assistant message relative to the previous
    request. Returns undefined when nothing is counted: first turn, after a
    reset, no cache activity ever reported (provider without cache support), or
    miss below the noise floor."""
    usage = read_field(message, "usage")
    input, cacheRead, cacheWrite = _usage(message)
    promptTokens = input + cacheRead + cacheWrite
    # A zero-cache turn only counts when cache activity was reported before:
    # on cache-read-only providers that is a total miss, while on providers
    # that never report caching it means nothing.
    if not prev or promptTokens <= 0 or (cacheRead + cacheWrite == 0 and not prev.reportedCache):
        return None

    missedTokens = min(prev.promptTokens, promptTokens) - cacheRead
    if missedTokens <= NOISE_FLOOR_TOKENS:
        return None

    # Extra cost = missed tokens billed at the actual paid rate (input/cacheWrite,
    # incl. write premium) instead of the cache-read rate. Missed tokens can only
    # land in the input or cacheWrite buckets, so the paid rate comes straight
    # from this message's own cost breakdown.
    cost = read_field(usage, "cost")
    paidTokens = input + cacheWrite
    paidPerToken = (
        (read_field(cost, "input", 0) + read_field(cost, "cacheWrite", 0)) / paidTokens if paidTokens > 0 else 0
    )
    if cacheRead > 0:
        readPerToken = read_field(cost, "cacheRead", 0) / cacheRead
    else:
        model = models.getModel(read_field(message, "provider"), read_field(message, "model"))
        modelCacheRead = read_field(read_field(model, "cost"), "cacheRead") if model is not None else None
        readPerToken = (modelCacheRead if modelCacheRead is not None else 0) / 1_000_000

    return CacheMiss(
        missedTokens=missedTokens,
        missedCost=missedTokens * max(0, paidPerToken - readPerToken),
        idleMs=max(0, read_field(message, "timestamp", 0) - prev.timestamp),
        modelChanged=f"{read_field(message, 'provider')}/{read_field(message, 'model')}" != prev.modelKey,
    )


def asPreviousRequest(message: Any, reportedCache: bool) -> PreviousRequest | None:
    input, cacheRead, cacheWrite = _usage(message)
    promptTokens = input + cacheRead + cacheWrite
    if promptTokens <= 0:
        return None
    return PreviousRequest(
        promptTokens=promptTokens,
        modelKey=f"{read_field(message, 'provider')}/{read_field(message, 'model')}",
        timestamp=read_field(message, "timestamp", 0),
        reportedCache=reportedCache or cacheRead + cacheWrite > 0,
    )


def scan(entries: list[Any], models: Any) -> tuple[PreviousRequest | None, CacheWasteTotals, dict[int, CacheMiss]]:
    prev: PreviousRequest | None = None
    totals = CacheWasteTotals(missedTokens=0, missedCost=0, missCount=0)
    misses: dict[int, CacheMiss] = {}

    for entry in entries:
        entryType = read_field(entry, "type")
        if entryType == "compaction" or entryType == "branch_summary":
            # The context legitimately changed; the next turn's prompt is new content,
            # not re-billed content. Model switches are NOT exempt: they re-bill the
            # full prompt and should be counted.
            prev = None
            continue
        if entryType == "usage" and read_field(entry, "kind") == "cache_warm":
            input, cacheRead, cacheWrite = _usage(entry)
            promptTokens = input + cacheRead + cacheWrite
            if promptTokens > 0:
                prev = PreviousRequest(
                    promptTokens=promptTokens,
                    modelKey=f"{read_field(entry, 'provider')}/{read_field(entry, 'model')}",
                    timestamp=_dateParse(read_field(entry, "timestamp")),
                    reportedCache=True,
                )
        elif entryType == "message" and read_field(read_field(entry, "message"), "role") == "assistant":
            message = read_field(entry, "message")
            miss = detectMiss(prev, message, models)
            if miss:
                totals.missedTokens += miss.missedTokens
                totals.missedCost += miss.missedCost
                totals.missCount += 1
                misses[id(message)] = miss
            previous = asPreviousRequest(message, prev.reportedCache if prev else False)
            prev = previous if previous is not None else prev
    return prev, totals, misses


def computeCacheWaste(entries: list[Any], models: Any) -> CacheWasteTotals:
    """Cumulative cache waste across a session: prompt tokens that should have been
    cache reads (they were in the previous turn's prompt) but were re-billed."""
    return scan(entries, models)[1]


def collectCacheMisses(entries: list[Any], models: Any) -> dict[int, CacheMiss]:
    """All counted cache misses across a session, keyed by the assistant message
    (by reference) that paid for them. Used to re-derive transcript notices when
    rebuilding the chat from entries (resume, post-compaction rebuild)."""
    return scan(entries, models)[2]


def detectCacheMiss(entries: list[Any], message: Any, models: Any) -> CacheMiss | None:
    """Detect a cache miss on a just-completed assistant message.
    `entries` must not yet contain `message` (message_end fires before persistence)."""
    return detectMiss(scan(entries, models)[0], message, models)


__all__ = [
    "CACHE_TTL_MS",
    "CacheMiss",
    "CacheWasteTotals",
    "ModelPriceSource",
    "collectCacheMisses",
    "computeCacheWaste",
    "detectCacheMiss",
]

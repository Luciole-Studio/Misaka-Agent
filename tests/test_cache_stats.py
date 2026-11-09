"""Translated from pi's ``coding-agent/test/cache-stats.test.ts`` (v1.1.0)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.types import AssistantMessage
from misaka.core.cache_stats import (
    collectCacheMisses,
    computeCacheWaste,
    detectCacheMiss,
)

zeroCost = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}

# $/million tokens; used as cache-read price fallback on full-miss turns
models = SimpleNamespace(getModel=lambda *_args: SimpleNamespace(cost=SimpleNamespace(cacheRead=0.3)))


def assistant(
    input: int = 0,
    cacheRead: int = 0,
    cacheWrite: int = 0,
    cost: dict[str, float] | None = None,
    model: str = "test-model",
    timestamp: int = 0,
) -> AssistantMessage:
    return AssistantMessage.model_validate(
        {
            "role": "assistant",
            "content": [],
            "api": "anthropic-messages",
            "provider": "test",
            "model": model,
            "usage": {
                "input": input,
                "output": 10,
                "cacheRead": cacheRead,
                "cacheWrite": cacheWrite,
                "totalTokens": 0,
                "cost": {**zeroCost, **(cost or {})},
            },
            "stopReason": "stop",
            "timestamp": timestamp,
        }
    )


def entry(message: AssistantMessage) -> dict[str, Any]:
    return {"type": "message", "id": "x", "parentId": None, "timestamp": "", "message": message}


def usageEntry(kind: str, timestamp: int) -> dict[str, Any]:
    return {
        "type": "usage",
        "id": f"usage-{kind}",
        "parentId": None,
        "timestamp": datetime.fromtimestamp(timestamp / 1000, UTC).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        ),
        "kind": kind,
        "provider": "test",
        "model": "test-model",
        "usage": {
            "input": 0,
            "output": 0,
            "cacheRead": 100_000,
            "cacheWrite": 0,
            "totalTokens": 100_000,
            "cost": zeroCost,
        },
    }


# Turn 1: fresh 100k cache write at $3.75/M
turn1 = assistant(cacheWrite=100_000, cost={"cacheWrite": 0.375}, timestamp=0)
# Turn 2: healthy, everything read back at $0.30/M
turn2 = assistant(cacheRead=100_000, cacheWrite=5_000, cost={"cacheRead": 0.03, "cacheWrite": 0.019}, timestamp=60_000)


def test_compute_cache_waste_accumulates_missed_tokens_and_cost_across_turns():
    # Turn 3: full miss, previous 105k prompt re-billed at $3.75/M write
    turn3 = assistant(cacheWrite=110_000, cost={"cacheWrite": 0.4125}, timestamp=120_000)
    totals = computeCacheWaste([entry(turn1), entry(turn2), entry(turn3)], models)
    assert totals.missedTokens == 105_000
    # 105k at ($3.75 - $0.30)/M
    assert totals.missedCost == pytest.approx(0.36225, abs=1e-5)


def test_compute_cache_waste_counts_nothing_for_healthy_sessions():
    totals = computeCacheWaste([entry(turn1), entry(turn2)], models)
    assert totals.missedTokens == 0
    assert totals.missedCost == 0


def test_compute_cache_waste_skips_the_turn_after_a_compaction_reset():
    reset = {"type": "compaction", "id": "c", "parentId": None, "timestamp": ""}
    afterReset = assistant(cacheWrite=20_000, cost={"cacheWrite": 0.075})
    totals = computeCacheWaste([entry(turn1), reset, entry(afterReset)], models)
    assert totals.missedTokens == 0


def test_compute_cache_waste_counts_misses_caused_by_model_switches():
    otherModel = assistant(cacheWrite=100_000, cost={"cacheWrite": 0.375}, model="other-model")
    totals = computeCacheWaste([entry(turn1), entry(otherModel)], models)
    assert totals.missedTokens == 100_000
    assert totals.missCount == 1


def test_compute_cache_waste_skips_providers_that_report_no_cache_activity():
    a = assistant(input=100_000)
    b = assistant(input=110_000)
    totals = computeCacheWaste([entry(a), entry(b)], models)
    assert totals.missedTokens == 0


def test_collect_cache_misses_maps_counted_misses_to_their_assistant_messages_by_reference():
    missTurn = assistant(cacheWrite=110_000, cost={"cacheWrite": 0.4125}, timestamp=120_000)
    misses = collectCacheMisses([entry(turn1), entry(turn2), entry(missTurn)], models)
    assert len(misses) == 1
    miss = misses.get(id(missTurn))
    assert miss is not None and miss.missedTokens == 105_000


def test_detect_cache_miss_detects_a_miss_on_a_just_completed_message_with_idle_time():
    missMessage = assistant(cacheWrite=110_000, cost={"cacheWrite": 0.4125}, timestamp=600_000)
    miss = detectCacheMiss([entry(turn1), entry(turn2)], missMessage, models)
    assert miss is not None
    assert miss.missedTokens == 105_000
    assert miss.missedCost == pytest.approx(0.36225, abs=1e-5)
    # 600s - 60s since the previous request
    assert miss.idleMs == 540_000
    assert miss.modelChanged is False


def test_detect_cache_miss_flags_model_switches_on_detected_misses():
    otherModel = assistant(cacheWrite=110_000, cost={"cacheWrite": 0.4125}, model="other-model", timestamp=120_000)
    miss = detectCacheMiss([entry(turn1), entry(turn2)], otherModel, models)
    assert miss is not None and miss.missedTokens == 105_000
    assert miss.modelChanged is True


def test_detect_cache_miss_uses_only_cache_warm_usage_entries_as_cache_refreshes():
    missMessage = assistant(cacheWrite=110_000, cost={"cacheWrite": 0.4125}, timestamp=600_000)

    afterCacheWarm = detectCacheMiss([entry(turn1), usageEntry("cache_warm", 500_000)], missMessage, models)
    afterOtherUsage = detectCacheMiss([entry(turn1), usageEntry("custom_operation", 500_000)], missMessage, models)

    assert afterCacheWarm is not None and afterCacheWarm.idleMs == 100_000
    assert afterOtherUsage is not None and afterOtherUsage.idleMs == 600_000


def test_detect_cache_miss_returns_undefined_for_healthy_turns():
    healthy = assistant(cacheRead=105_000, cacheWrite=2_000, cost={"cacheRead": 0.0315, "cacheWrite": 0.0075}, timestamp=120_000)
    assert detectCacheMiss([entry(turn1), entry(turn2)], healthy, models) is None


def test_detect_cache_miss_returns_undefined_for_the_first_turn_of_a_session():
    assert detectCacheMiss([], turn1, models) is None

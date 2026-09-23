"""Prompt-cache warming, translated from pi's ``core/cache-warmer.ts``.

Keeps one prompt cache entry alive by re-sending its request with a one-token output cap
before the entry expires, when a refresh is expected to save more than it costs.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from misaka.ai.models import calculate_cost
from misaka.ai.types import Model, Usage
from misaka.ai.utils.provider_env import get_provider_env_value
from misaka.utils.values import read_field

# Streaming warming never continues past this long after the real request that started it.
MAX_WARMING_AGE_MS = 60 * 60_000
# Idle warming uses a shorter horizon because continuation estimates become less reliable with age.
MAX_IDLE_WARMING_AGE_MS = 30 * 60_000
# A refresh is sent only when it is expected to save at least this many dollars.
CACHE_WARMING_MINIMUM_EXPECTED_SAVINGS = 0.05
# Chance that a real request arrives before the cache entry expires while the
# agent sits idle. Measured from our own usage; per-session estimates were not
# better than this constant.
IDLE_CONTINUATION_PROBABILITY = 0.15

CacheWarmingMode = Literal["off", "streaming", "idle"]
CacheWarmingAction = Literal["warm", "stop"]


def get_cache_warming_delay_ms(ttl_ms: float) -> int | None:
    """Refresh at 90% of the TTL while preserving at least ten seconds of margin."""
    if ttl_ms <= 10_000:
        return None
    return max(1, math.floor(min(ttl_ms * 0.9, ttl_ms - 10_000)))


def get_prompt_cache_ttl_ms(model: Model, options: Any) -> float | None:
    """Lifetime of the prompt cache entry a request writes, from the model's
    `promptCache` tier for the retention the request used. None when the
    model has no lifetime for that tier or caching is off."""
    retention = read_field(options, "cacheRetention")
    if retention is None:
        retention = "long" if get_provider_env_value("PI_CACHE_RETENTION", read_field(options, "env")) == "long" else "short"
    if retention == "none":
        return None
    seconds = (model.promptCache or {}).get(retention)
    return None if seconds is None else seconds * 1000


def is_replayable(model: Model, options: Any) -> bool:
    """Whether replaying the request with a one-token output cap leaves its cache
    entry untouched. Anthropic's budget-based thinking (Claude models without
    adaptive thinking) derives `budget_tokens` from `max_tokens`; the replay
    would get a different budget, which Anthropic keys the message cache on,
    and the model could still think for thousands of tokens."""
    if not read_field(options, "reasoning") or model.api != "anthropic-messages":
        return True
    return getattr(model.compat, "forceAdaptiveThinking", None) is True


def _last_prompt_tokens(entries: list[Any]) -> int:
    """Prompt size of the most recent real request on the branch, as reported by the provider."""
    for entry in reversed(entries):
        if entry.get("type") == "message" and read_field(entry.get("message"), "role") == "assistant":
            usage = read_field(entry.get("message"), "usage")
            return int(
                (read_field(usage, "input") or 0)
                + (read_field(usage, "cacheRead") or 0)
                + (read_field(usage, "cacheWrite") or 0)
            )
    return 0


def _price(model: Model, **tokens: int) -> float:
    usage = Usage(
        input=0,
        output=0,
        cacheRead=0,
        cacheWrite=0,
        totalTokens=0,
        cost={"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
    ).model_copy(update=tokens)
    return calculate_cost(model, usage).total


@dataclass(slots=True)
class CacheWarmingDecision:
    phase: Literal["streaming", "idle"]
    warmCost: float
    missCost: float
    continuationProbability: float
    expectedSavings: float
    economicsAvailable: bool
    action: CacheWarmingAction


@dataclass(slots=True)
class CacheWarmingStatus:
    state: Literal["inactive", "scheduled", "refreshing"]
    reason: str | None = None
    nextWarmAt: float | None = None
    decision: CacheWarmingDecision | None = None
    extensionOverride: bool = False


@dataclass(slots=True)
class CacheWarmingRequest:
    model: Model
    context: Any
    options: Any


@dataclass(slots=True)
class _Run:
    model: Model
    context: Any
    options: Any
    isCurrent: Callable[[], bool]
    ttlMs: float
    delayMs: int
    refreshDeadlineAt: float
    startedAt: float
    controller: Any
    phase: Literal["streaming", "idle"]
    nextWarmAt: float
    extensionOverride: bool
    timer: asyncio.TimerHandle | None = None
    task: asyncio.Task[None] | None = field(default=None)


def _now_ms() -> float:
    return time.time() * 1000


class CacheWarmer:
    """Keeps one prompt cache entry alive by re-sending its request with a
    one-token output cap before the entry expires. `start` replaces any
    previous run; warm requests never extend the fixed safety windows."""

    def __init__(
        self,
        models: Any,
        session_manager: Any,
        get_mode: Callable[[], str],
        decide: Callable[[dict[str, Any]], Awaitable[Any]] | None = None,
    ) -> None:
        self.models = models
        self.sessionManager = session_manager
        self.getMode = get_mode
        # Lets extensions override `event.action`; failures fall back to pi's decision.
        self.decide = decide if decide is not None else self._default_decide
        # Called with the persisted usage entry after each successful refresh.
        self.onWarmed: Callable[[Any], None] | None = None
        self.run: _Run | None = None
        self.inactive = CacheWarmingStatus(state="inactive", reason="waiting for first request")

    @staticmethod
    async def _default_decide(event: dict[str, Any]) -> Any:
        return event["action"]

    @property
    def status(self) -> CacheWarmingStatus:
        if self.getMode() == "off":
            return CacheWarmingStatus(state="inactive", reason="cache warming disabled")
        run = self.run
        if run is None:
            return self.inactive
        if not run.isCurrent():
            return CacheWarmingStatus(state="inactive", reason="conversation context changed")
        decision = self.evaluate(run)
        refreshing = run.timer is None
        if not decision.economicsAvailable and not refreshing:
            return CacheWarmingStatus(state="inactive", reason="cache economics unavailable")
        return CacheWarmingStatus(
            state="refreshing" if refreshing else "scheduled",
            nextWarmAt=run.nextWarmAt,
            decision=decision,
            extensionOverride=run.extensionOverride,
        )

    def start(self, request: CacheWarmingRequest, is_current: Callable[[], bool]) -> None:
        """Keep the prompt cache entry written by `request` warm while `is_current` holds."""
        from misaka.ai.utils.abort import AbortController

        self.clearRun()
        mode = self.getMode()
        if mode == "off":
            self.stop("cache warming disabled")
            return
        if not is_replayable(request.model, request.options):
            self.stop("request cannot be replayed safely")
            return
        ttl_ms = get_prompt_cache_ttl_ms(request.model, request.options)
        if ttl_ms is None:
            self.stop(
                "request disabled prompt caching"
                if read_field(request.options, "cacheRetention") == "none"
                else "cache lifetime unavailable"
            )
            return
        delay_ms = get_cache_warming_delay_ms(ttl_ms)
        if delay_ms is None:
            self.stop("cache lifetime unavailable")
            return
        self.run = _Run(
            model=request.model,
            context=request.context,
            options=request.options,
            isCurrent=is_current,
            ttlMs=ttl_ms,
            delayMs=delay_ms,
            refreshDeadlineAt=0,
            startedAt=_now_ms(),
            controller=AbortController(),
            phase="streaming",
            nextWarmAt=0,
            extensionOverride=False,
        )
        self.schedule(self.run)

    def onAgentSettled(self) -> None:
        run = self.run
        if run is None:
            return
        if self.getMode() == "streaming":
            self.stop("agent run settled")
            return
        run.phase = "idle"
        deadline = run.startedAt + MAX_IDLE_WARMING_AGE_MS
        if run.nextWarmAt > deadline or _now_ms() >= deadline:
            self.stop("30-minute idle safety limit reached")

    def onModeChanged(self) -> None:
        """Reconcile an active run after the persisted warming mode changes."""
        run = self.run
        if run is None:
            return
        reason = self.getModeStopReason(run)
        if reason:
            self.stop(reason)

    def cancel(self) -> None:
        self.stop("inactive")

    def clearRun(self) -> None:
        run = self.run
        if run is None:
            return
        self.run = None
        if run.timer is not None:
            run.timer.cancel()
        run.controller.abort()

    def stop(self, reason: str, stopped: dict[str, Any] | None = None) -> None:
        self.clearRun()
        self.inactive = CacheWarmingStatus(state="inactive", reason=reason, **(stopped or {}))

    def schedule(self, run: _Run) -> None:
        run.extensionOverride = False
        run.nextWarmAt = _now_ms() + run.delayMs
        # A timer can run late after sleep or event-loop blockage. Keep half of
        # the planned pre-expiry margin for that delay and request dispatch; a
        # late refresh is likely a full-price cache write, not a cache warm.
        run.refreshDeadlineAt = run.nextWarmAt + math.floor((run.ttlMs - run.delayMs) / 2)
        deadline = run.startedAt + (MAX_IDLE_WARMING_AGE_MS if run.phase == "idle" else MAX_WARMING_AGE_MS)
        if run.nextWarmAt > deadline or _now_ms() >= deadline:
            self.stop("30-minute idle safety limit reached" if run.phase == "idle" else "one-hour safety limit reached")
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop to schedule on (a synchronous caller outside the session's loop): the
            # entry simply expires, as it would without warming.
            self.stop("no event loop for cache warming")
            return

        def fire() -> None:
            run.task = loop.create_task(self.refresh(run))

        run.timer = loop.call_later(max(0.0, (run.nextWarmAt - _now_ms()) / 1000), fire)

    async def refresh(self, run: _Run) -> None:
        run.timer = None
        if not self.validateRun(run):
            return
        if self.refreshDeadlineMissed(run):
            return
        decision = self.evaluate(run)
        action: Any = decision.action
        try:
            action = await self.decide(
                {
                    "type": "cache_warming_decision",
                    "warmCost": decision.warmCost,
                    "missCost": decision.missCost,
                    "continuationProbability": decision.continuationProbability,
                    "action": action,
                }
            )
        except Exception:  # noqa: BLE001,S110 - extension failures fall back to pi's own decision
            pass
        if not self.validateRun(run) or self.refreshDeadlineMissed(run):
            return
        extension_override = action != decision.action
        if action == "stop":
            reason = (
                "stopped by extension"
                if extension_override
                else "expected savings below threshold"
                if decision.economicsAvailable
                else "cache economics unavailable"
            )
            self.stop(reason, {"decision": decision, "extensionOverride": extension_override})
            return
        run.extensionOverride = extension_override
        try:
            options = _with_options(run.options, maxTokens=1, maxRetries=0, signal=run.controller.signal)
            message = await self.models.streamSimple(run.model, run.context, options).result()
            if not self.validateRun(run):
                return
            if message.stopReason not in {"error", "aborted"}:
                entry = self.sessionManager.appendUsage(
                    "cache_warm",
                    message.provider,
                    message.responseModel if message.responseModel is not None else message.model,
                    message.usage,
                    "extension override" if extension_override else None,
                )
                if self.onWarmed is not None:
                    self.onWarmed(entry)
        except Exception:  # noqa: BLE001,S110 - cache warming is best-effort and must not affect the active agent run
            pass
        if self.run is run:
            self.schedule(run)

    def refreshDeadlineMissed(self, run: _Run) -> bool:
        if _now_ms() <= run.refreshDeadlineAt:
            return False
        self.stop("cache refresh deadline missed")
        return True

    def validateRun(self, run: _Run) -> bool:
        if self.run is not run:
            return False
        reason = self.getModeStopReason(run)
        if reason is None and not run.isCurrent():
            reason = "conversation context changed"
        if not reason:
            return True
        self.stop(reason)
        return False

    def getModeStopReason(self, run: _Run) -> str | None:
        mode = self.getMode()
        if mode == "off":
            return "cache warming disabled"
        if mode == "streaming" and run.phase == "idle":
            return "agent run settled"
        return None

    def evaluate(self, run: _Run) -> CacheWarmingDecision:
        model = run.model
        prompt_tokens = _last_prompt_tokens(self.sessionManager.getBranch())
        cache_hit_cost = _price(model, cacheRead=prompt_tokens)
        cache_miss_cost = _price(
            model, **({"cacheWrite": prompt_tokens} if model.cost.cacheWrite > 0 else {"input": prompt_tokens})
        )
        warm_cost = _price(model, cacheRead=prompt_tokens, output=1)
        miss_cost = max(0.0, cache_miss_cost - cache_hit_cost)
        continuation_probability = IDLE_CONTINUATION_PROBABILITY if run.phase == "idle" else 1.0
        economics_available = prompt_tokens > 0 and (cache_hit_cost > 0 or cache_miss_cost > 0)
        expected_savings = continuation_probability * miss_cost - warm_cost
        return CacheWarmingDecision(
            phase=run.phase,
            warmCost=warm_cost,
            missCost=miss_cost,
            continuationProbability=continuation_probability,
            expectedSavings=expected_savings,
            economicsAvailable=economics_available,
            action="warm" if expected_savings >= CACHE_WARMING_MINIMUM_EXPECTED_SAVINGS else "stop",
        )


def _with_options(options: Any, **updates: Any) -> Any:
    if options is None:
        return updates
    if isinstance(options, dict):
        return {**options, **updates}
    if hasattr(options, "model_copy"):
        return options.model_copy(update=updates)
    return {**vars(options), **updates}


def _format_dollars(value: float) -> str:
    return f"-${abs(value):.3f}" if value < 0 else f"${value:.3f}"


def _format_cache_warming_economics(decision: CacheWarmingDecision) -> str:
    if not decision.economicsAvailable:
        return "cache economics unavailable"
    probability = round(decision.continuationProbability * 100)
    probability_text = (
        f"{probability}% continuation probability while agent is running"
        if decision.phase == "streaming"
        else f"{probability}% continuation probability"
    )
    comparison = ">=" if decision.action == "warm" else "<"
    return (
        f"{probability_text}, expected savings {_format_dollars(decision.expectedSavings)} "
        f"{comparison} ${CACHE_WARMING_MINIMUM_EXPECTED_SAVINGS:.3f}"
    )


def _format_cache_warming_decision_time(next_warm_at: float | None, now: float) -> str:
    if next_warm_at is None or next_warm_at <= now:
        return "Decision now"
    remaining_seconds = math.ceil((next_warm_at - now) / 1000)
    hours = remaining_seconds // 3600
    remaining_seconds %= 3600
    minutes = remaining_seconds // 60
    seconds = remaining_seconds % 60
    parts: list[str] = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if seconds > 0 or not parts:
        parts.append(f"{seconds}s")
    return f"Decision in {' '.join(parts)}"


def format_cache_warming_status(status: CacheWarmingStatus, now: float | None = None) -> str:
    """One-line status for `/session`."""
    if now is None:
        now = _now_ms()
    decision = status.decision
    # A decision is attached once pi (or an extension) acted on it; "inactive"
    # without one never got that far.
    if decision is None or (status.state == "inactive" and not decision.economicsAvailable and not status.extensionOverride):
        return f"Inactive ({status.reason if status.reason is not None else 'unknown reason'})"
    details = (
        f"extension override, {_format_cache_warming_economics(decision)}"
        if status.extensionOverride
        else f"{_format_cache_warming_economics(decision)} -> {decision.action}"
    )
    if status.state == "inactive":
        return f"Stopped ({details})"
    if status.state == "refreshing":
        return f"Warming cache ({details})"
    return f"{_format_cache_warming_decision_time(status.nextWarmAt, now)} ({details})"


def format_cache_warming_usage(entry: Any) -> str:
    """One-line transcript text for persisted cache-warming usage."""
    note = f" ({entry['note']})" if entry.get("note") else ""
    total = read_field(read_field(entry.get("usage"), "cost"), "total") or 0
    cost = f"{total:.6f}"
    # Trim trailing zeros past the third decimal, as pi's regex does.
    whole, _, fraction = cost.partition(".")
    fraction = fraction[:3] + fraction[3:].rstrip("0")
    return f"Cache warmed{note}: ${whole}.{fraction}"


__all__ = [
    "CACHE_WARMING_MINIMUM_EXPECTED_SAVINGS",
    "CacheWarmer",
    "CacheWarmingDecision",
    "CacheWarmingRequest",
    "CacheWarmingStatus",
    "format_cache_warming_status",
    "format_cache_warming_usage",
    "get_cache_warming_delay_ms",
    "get_prompt_cache_ttl_ms",
    "is_replayable",
]

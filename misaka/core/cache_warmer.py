"""Prompt-cache warming.

Translated from pi's ``coding-agent/src/core/cache-warmer.ts``. Keeps one prompt cache entry alive by re-sending its
request with a one-token output cap before the entry expires, when a refresh is expected to save more than it costs.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from misaka.ai.models import calculateCost
from misaka.ai.types import Model, Usage
from misaka.ai.utils.provider_env import getProviderEnvValue
from misaka.utils.js import AbortController, jsRound, spawn, toFixed
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

# MISAKA fork: asyncio holds tasks weakly; `void this.refresh(run)` keeps its task here until it settles.
_background: set[asyncio.Task[Any]] = set()


def getCacheWarmingDelayMs(ttlMs: float) -> int | None:
    """Refresh at 90% of the TTL while preserving at least ten seconds of margin."""
    if ttlMs <= 10_000:
        return None
    return max(1, math.floor(min(ttlMs * 0.9, ttlMs - 10_000)))


def getPromptCacheTtlMs(model: Model, options: Any) -> float | None:
    """Lifetime of the prompt cache entry a request writes, from the model's
    `promptCache` tier for the retention the request used. None when the
    model has no lifetime for that tier or caching is off."""
    retention = read_field(options, "cacheRetention")
    if retention is None:
        retention = "long" if getProviderEnvValue("PI_CACHE_RETENTION", read_field(options, "env")) == "long" else "short"
    if retention == "none":
        return None
    seconds = (model.promptCache or {}).get(retention)
    return None if seconds is None else seconds * 1000


def isReplayable(model: Model, options: Any) -> bool:
    """Whether replaying the request with a one-token output cap leaves its cache
    entry untouched. Anthropic's budget-based thinking (Claude models without
    adaptive thinking) derives `budget_tokens` from `max_tokens`; the replay
    would get a different budget, which Anthropic keys the message cache on,
    and the model could still think for thousands of tokens."""
    if not read_field(options, "reasoning") or model.api != "anthropic-messages":
        return True
    return getattr(model.compat, "forceAdaptiveThinking", None) is True


def lastPromptTokens(entries: list[Any]) -> int:
    """Prompt size of the most recent real request on the branch, as reported by the provider."""
    for index in range(len(entries) - 1, -1, -1):
        entry = entries[index]
        if read_field(entry, "type") == "message" and read_field(read_field(entry, "message"), "role") == "assistant":
            usage = read_field(read_field(entry, "message"), "usage")
            return (read_field(usage, "input") or 0) + (read_field(usage, "cacheRead") or 0) + (read_field(usage, "cacheWrite") or 0)
    return 0


def price(model: Model, tokens: dict[str, int]) -> float:
    usage = Usage(
        input=0,
        output=0,
        cacheRead=0,
        cacheWrite=0,
        totalTokens=0,
        cost={"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
    ).model_copy(update=tokens)
    return calculateCost(model, usage).total


CacheWarmingAction = Literal["warm", "stop"]


@dataclass(slots=True)
class CacheWarmingDecision:
    """Inputs and outcome of one warm-or-stop decision, as shown by `/session`."""

    # "streaming" while the agent run that sent the request is still active.
    phase: Literal["streaming", "idle"]
    # Price of this refresh: a cache read of the prompt plus one output token.
    warmCost: float
    # Extra price of the next real request if the cache entry is lost.
    missCost: float
    # Estimated chance that a real request arrives before the entry expires.
    continuationProbability: float
    # `continuationProbability * missCost - warmCost`.
    expectedSavings: float
    # False when the prompt size or the model's prices are unknown.
    economicsAvailable: bool
    # Pi's decision: "warm" when `expectedSavings` is at least $0.05.
    action: CacheWarmingAction


class CacheWarmingDecisionEvent(TypedDict):
    """Fired before each refresh with pi's decision filled in. Everything else an
    extension might want (model, idle state, context size) is on the context."""

    type: Literal["cache_warming_decision"]
    warmCost: float
    missCost: float
    continuationProbability: float
    action: CacheWarmingAction


class CacheWarmingDecisionEventResult(TypedDict, total=False):
    # Override whether this refresh is sent. "stop" ends warming until the next real request.
    action: CacheWarmingAction


@dataclass(slots=True)
class CacheWarmingStatus:
    # "scheduled": a refresh timer is armed; "refreshing": a warm request is in flight.
    state: Literal["inactive", "scheduled", "refreshing"]
    # Why nothing is scheduled.
    reason: str | None = None
    nextWarmAt: float | None = None
    # The pending decision, or the decision that stopped warming.
    decision: CacheWarmingDecision | None = None
    # True when an extension changed `decision.action`.
    extensionOverride: bool | None = None


@dataclass(slots=True)
class CacheWarmRequest:
    """The request whose prompt cache entry should be kept warm, exactly as it was sent."""

    model: Model
    context: Any
    options: Any


@dataclass(slots=True)
class ActiveRun:
    model: Model
    context: Any
    options: Any
    # False once the session's model or messages no longer match the request.
    isCurrent: Callable[[], bool]
    ttlMs: float
    delayMs: int
    # Latest safe time to send this refresh, leaving half the original expiry margin.
    refreshDeadlineAt: float
    startedAt: float
    controller: AbortController
    phase: Literal["streaming", "idle"]
    nextWarmAt: float
    # Set while a refresh that an extension forced is in flight.
    extensionOverride: bool
    timer: asyncio.TimerHandle | None = None


def _now() -> float:
    """``Date.now()``."""
    return math.floor(time.time() * 1000)


async def _defaultDecide(event: CacheWarmingDecisionEvent) -> CacheWarmingAction:
    return event["action"]


class CacheWarmer:
    """Keeps one prompt cache entry alive by re-sending its request with a
    one-token output cap before the entry expires. `start` replaces any
    previous run; warm requests never extend the fixed safety windows."""

    def __init__(
        self,
        models: Any,
        sessionManager: Any,
        getMode: Callable[[], CacheWarmingMode],
        decide: Callable[[CacheWarmingDecisionEvent], Awaitable[CacheWarmingAction]] = _defaultDecide,
    ) -> None:
        self.run: ActiveRun | None = None
        self.models = models
        self.sessionManager = sessionManager
        self.getMode = getMode
        # Lets extensions override `event.action`; failures fall back to pi's decision.
        self.decide = decide
        # Called with the persisted usage entry after each successful refresh.
        self.onWarmed: Callable[[Any], None] | None = None
        self.inactive = CacheWarmingStatus(state="inactive", reason="waiting for first request")

    @property
    def status(self) -> CacheWarmingStatus:
        if self.getMode() == "off":
            return CacheWarmingStatus(state="inactive", reason="cache warming disabled")
        run = self.run
        if not run:
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

    def start(self, request: CacheWarmRequest, isCurrent: Callable[[], bool]) -> None:
        """Keep the prompt cache entry written by `request` warm while `isCurrent` holds."""
        self.clearRun()
        mode = self.getMode()
        if mode == "off":
            self.stop("cache warming disabled")
            return
        if not isReplayable(request.model, request.options):
            self.stop("request cannot be replayed safely")
            return
        ttlMs = getPromptCacheTtlMs(request.model, request.options)
        if ttlMs is None:
            self.stop(
                "request disabled prompt caching"
                if read_field(request.options, "cacheRetention") == "none"
                else "cache lifetime unavailable"
            )
            return
        delayMs = getCacheWarmingDelayMs(ttlMs)
        if delayMs is None:
            self.stop("cache lifetime unavailable")
            return
        self.run = ActiveRun(
            model=request.model,
            context=request.context,
            options=request.options,
            isCurrent=isCurrent,
            ttlMs=ttlMs,
            delayMs=delayMs,
            refreshDeadlineAt=0,
            startedAt=_now(),
            controller=AbortController(),
            phase="streaming",
            nextWarmAt=0,
            extensionOverride=False,
        )
        self.schedule(self.run)

    def onAgentSettled(self) -> None:
        run = self.run
        if not run:
            return
        if self.getMode() == "streaming":
            self.stop("agent run settled")
            return
        run.phase = "idle"
        deadline = run.startedAt + MAX_IDLE_WARMING_AGE_MS
        if run.nextWarmAt > deadline or _now() >= deadline:
            self.stop("30-minute idle safety limit reached")

    def onModeChanged(self) -> None:
        """Reconcile an active run after the persisted warming mode changes."""
        run = self.run
        if not run:
            return
        reason = self.getModeStopReason(run)
        if reason:
            self.stop(reason)

    def cancel(self) -> None:
        self.stop("inactive")

    def clearRun(self) -> None:
        run = self.run
        if not run:
            return
        self.run = None
        if run.timer:
            run.timer.cancel()
        run.controller.abort()

    def stop(self, reason: str, stopped: dict[str, Any] | None = None) -> None:
        """``stopped`` is ``{"decision"?, "extensionOverride"?}``."""
        self.clearRun()
        self.inactive = CacheWarmingStatus(state="inactive", reason=reason, **(stopped or {}))

    def schedule(self, run: ActiveRun) -> None:
        run.extensionOverride = False
        run.nextWarmAt = _now() + run.delayMs
        # A timer can run late after sleep or event-loop blockage. Keep half of
        # the planned pre-expiry margin for that delay and request dispatch; a
        # late refresh is likely a full-price cache write, not a cache warm.
        run.refreshDeadlineAt = run.nextWarmAt + math.floor((run.ttlMs - run.delayMs) / 2)
        deadline = run.startedAt + (MAX_IDLE_WARMING_AGE_MS if run.phase == "idle" else MAX_WARMING_AGE_MS)
        if run.nextWarmAt > deadline or _now() >= deadline:
            self.stop("30-minute idle safety limit reached" if run.phase == "idle" else "one-hour safety limit reached")
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # MISAKA fork: no loop to schedule on (a synchronous caller outside the session's loop): the
            # entry simply expires, as it would without warming.
            self.stop("no event loop for cache warming")
            return
        run.timer = loop.call_later(
            max(0, run.nextWarmAt - _now()) / 1000, lambda: spawn(self.refresh(run), _background)
        )

    async def refresh(self, run: ActiveRun) -> None:
        run.timer = None
        if not self.validateRun(run):
            return
        if self.refreshDeadlineMissed(run):
            return
        decision = self.evaluate(run)
        warmCost, missCost, continuationProbability = (
            decision.warmCost,
            decision.missCost,
            decision.continuationProbability,
        )
        action = decision.action
        try:
            action = await self.decide(
                {
                    "type": "cache_warming_decision",
                    "warmCost": warmCost,
                    "missCost": missCost,
                    "continuationProbability": continuationProbability,
                    "action": action,
                }
            )
        except Exception:  # noqa: BLE001, S110
            # Extension failures fall back to pi's own decision.
            pass
        if not self.validateRun(run) or self.refreshDeadlineMissed(run):
            return
        extensionOverride = action != decision.action
        if action == "stop":
            reason = (
                "stopped by extension"
                if extensionOverride
                else "expected savings below threshold"
                if decision.economicsAvailable
                else "cache economics unavailable"
            )
            self.stop(reason, {"decision": decision, "extensionOverride": extensionOverride})
            return

        run.extensionOverride = extensionOverride
        try:
            message = await self.models.streamSimple(
                run.model,
                run.context,
                _spread(run.options, maxTokens=1, maxRetries=0, signal=run.controller.signal),
            ).result()
            if not self.validateRun(run):
                return
            if message.stopReason != "error" and message.stopReason != "aborted":
                entry = self.sessionManager.appendUsage(
                    "cache_warm",
                    message.provider,
                    message.responseModel if message.responseModel is not None else message.model,
                    message.usage,
                    "extension override" if extensionOverride else None,
                )
                if self.onWarmed is not None:
                    self.onWarmed(entry)
        except Exception:  # noqa: BLE001, S110
            # Cache warming is best-effort and must not affect the active agent run.
            pass
        if self.run is run:
            self.schedule(run)

    def refreshDeadlineMissed(self, run: ActiveRun) -> bool:
        if _now() <= run.refreshDeadlineAt:
            return False
        self.stop("cache refresh deadline missed")
        return True

    def validateRun(self, run: ActiveRun) -> bool:
        if self.run is not run:
            return False
        reason = self.getModeStopReason(run)
        if reason is None:
            reason = "conversation context changed" if not run.isCurrent() else None
        if not reason:
            return True
        self.stop(reason)
        return False

    def getModeStopReason(self, run: ActiveRun) -> str | None:
        mode = self.getMode()
        if mode == "off":
            return "cache warming disabled"
        if mode == "streaming" and run.phase == "idle":
            return "agent run settled"
        return None

    def evaluate(self, run: ActiveRun) -> CacheWarmingDecision:
        model = run.model
        promptTokens = lastPromptTokens(self.sessionManager.getBranch())
        cacheHitCost = price(model, {"cacheRead": promptTokens})
        cacheMissCost = price(model, {"cacheWrite": promptTokens} if model.cost.cacheWrite > 0 else {"input": promptTokens})
        warmCost = price(model, {"cacheRead": promptTokens, "output": 1})
        missCost = max(0, cacheMissCost - cacheHitCost)
        continuationProbability = IDLE_CONTINUATION_PROBABILITY if run.phase == "idle" else 1
        economicsAvailable = promptTokens > 0 and (cacheHitCost > 0 or cacheMissCost > 0)
        expectedSavings = continuationProbability * missCost - warmCost
        return CacheWarmingDecision(
            phase=run.phase,
            warmCost=warmCost,
            missCost=missCost,
            continuationProbability=continuationProbability,
            expectedSavings=expectedSavings,
            economicsAvailable=economicsAvailable,
            action="warm" if expectedSavings >= CACHE_WARMING_MINIMUM_EXPECTED_SAVINGS else "stop",
        )


def _spread(options: Any, **updates: Any) -> Any:
    """``{...options, ...updates}`` for the options shapes a request carries (a dict, a pydantic model or a namespace)."""
    if options is None:
        return updates
    if isinstance(options, dict):
        return {**options, **updates}
    if hasattr(options, "model_copy"):
        return options.model_copy(update=updates)
    return {**vars(options), **updates}


def formatDollars(value: float) -> str:
    return f"-${toFixed(abs(value), 3)}" if value < 0 else f"${toFixed(value, 3)}"


def formatCacheWarmingEconomics(decision: CacheWarmingDecision) -> str:
    if not decision.economicsAvailable:
        return "cache economics unavailable"
    probability = jsRound(decision.continuationProbability * 100)
    probabilityText = (
        f"{probability}% continuation probability while agent is running"
        if decision.phase == "streaming"
        else f"{probability}% continuation probability"
    )
    comparison = ">=" if decision.action == "warm" else "<"
    return (
        f"{probabilityText}, expected savings {formatDollars(decision.expectedSavings)} {comparison} "
        f"${toFixed(CACHE_WARMING_MINIMUM_EXPECTED_SAVINGS, 3)}"
    )


def formatCacheWarmingDecisionTime(nextWarmAt: float | None, now: float) -> str:
    if nextWarmAt is None or nextWarmAt <= now:
        return "Decision now"
    remainingSeconds = math.ceil((nextWarmAt - now) / 1000)
    hours = math.floor(remainingSeconds / 3600)
    remainingSeconds %= 3600
    minutes = math.floor(remainingSeconds / 60)
    seconds = remainingSeconds % 60
    parts: list[str] = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if seconds > 0 or len(parts) == 0:
        parts.append(f"{seconds}s")
    return f"Decision in {' '.join(parts)}"


def formatCacheWarmingStatus(status: CacheWarmingStatus, now: float | None = None) -> str:
    """One-line status for `/session`."""
    if now is None:
        now = _now()
    decision = status.decision
    # A decision is attached once pi (or an extension) acted on it; "inactive"
    # without one never got that far.
    if not decision or (status.state == "inactive" and not decision.economicsAvailable and not status.extensionOverride):
        return f"Inactive ({status.reason if status.reason is not None else 'unknown reason'})"
    details = (
        f"extension override, {formatCacheWarmingEconomics(decision)}"
        if status.extensionOverride
        else f"{formatCacheWarmingEconomics(decision)} -> {decision.action}"
    )
    if status.state == "inactive":
        return f"Stopped ({details})"
    if status.state == "refreshing":
        return f"Warming cache ({details})"
    return f"{formatCacheWarmingDecisionTime(status.nextWarmAt, now)} ({details})"


def formatCacheWarmingUsage(entry: Any) -> str:
    """One-line transcript text for persisted cache-warming usage."""
    note = f" ({read_field(entry, 'note')})" if read_field(entry, "note") else ""
    cost = re.sub(r"(\.\d{3}\d*?)0+\Z", r"\1", toFixed(read_field(read_field(read_field(entry, "usage"), "cost"), "total"), 6))
    return f"Cache warmed{note}: ${cost}"


__all__ = [
    "CACHE_WARMING_MINIMUM_EXPECTED_SAVINGS",
    "CacheWarmRequest",
    "CacheWarmer",
    "CacheWarmingAction",
    "CacheWarmingDecision",
    "CacheWarmingDecisionEvent",
    "CacheWarmingDecisionEventResult",
    "CacheWarmingStatus",
    "formatCacheWarmingStatus",
    "formatCacheWarmingUsage",
    "getCacheWarmingDelayMs",
    "getPromptCacheTtlMs",
    "isReplayable",
]

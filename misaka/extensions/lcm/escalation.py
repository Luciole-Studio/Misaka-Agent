"""Bounded escalation path for difficult LCM compactions."""
import threading
import time

from misaka.extensions.lcm.tokens import count_tokens

L1_PROMPT = """Summarize the conversation below. Keep the details that matter: decisions and the reasons behind them, constraints, tasks still in progress, file paths, commands, and concrete values and names. Do not invent anything that is not in the record.

End with exactly one line in this form:
Expandable: <one-sentence hint describing what was left out>

Conversation:
{text}"""

L2_PROMPT = """Reduce the conversation below to terse bullet points under four headings only: Decisions | Files changed | Errors hit | Current status. Drop all reasoning and rejected alternatives. Do not invent anything that is not in the record.

End with exactly one line in this form:
Expandable: <one-sentence hint describing what was left out>

Conversation:
{text}"""

TRUNCATION_MARKER = "\n\n[… truncated deterministically; details recoverable via lcm_expand …]\n\n"


class SummaryCircuitBreaker:
    """Pause summary calls after repeated failures until the cooldown expires."""

    def __init__(self, failure_threshold=2, cooldown_seconds=300.0):
        self._threshold = max(1, int(failure_threshold))
        self._cooldown = float(cooldown_seconds)
        self._failures = 0
        self._open_until = 0.0
        self._lock = threading.Lock()

    def allows(self):
        with self._lock:
            if self._open_until and time.monotonic() >= self._open_until:
                self._open_until = 0.0
                self._failures = 0
            return not self._open_until

    def record_failure(self):
        with self._lock:
            self._failures += 1
            if self._failures >= self._threshold:
                self._open_until = time.monotonic() + self._cooldown

    def record_success(self):
        with self._lock:
            self._failures = 0
            self._open_until = 0.0


class SummarySpendGuard:
    """Rate-limit summary calls within a rolling window; zero disables the limit."""

    def __init__(self, max_calls=24, window_seconds=600.0, backoff_seconds=1800.0):
        self._max = int(max_calls)
        self._window = float(window_seconds)
        self._backoff = float(backoff_seconds)
        self._calls = []
        self._blocked_until = 0.0
        self._lock = threading.Lock()

    def allows(self):
        if self._max <= 0:
            return True
        with self._lock:
            now = time.monotonic()
            if self._blocked_until:
                if now < self._blocked_until:
                    return False
                self._blocked_until = 0.0
                self._calls = []
            self._calls = [t for t in self._calls if now - t < self._window]
            if len(self._calls) >= self._max:
                self._blocked_until = now + self._backoff
                return False
            return True

    def record_call(self):
        if self._max > 0:
            with self._lock:
                self._calls.append(time.monotonic())


def deterministic_truncate(text, target_tokens):
    """L3 fallback: keep the head and tail halves around a marker, shrinking by binary search.

    Binary search is required because the token estimator is not additive: the head,
    marker, and tail can each fit the budget while their concatenation does not
    (CJK text and ASCII are estimated with different divisors).
    """
    if count_tokens(text) <= target_tokens:
        return text
    lo, hi, best = 0, len(text) // 2, TRUNCATION_MARKER.strip()
    while lo <= hi:
        half = (lo + hi) // 2
        candidate = text[:half] + TRUNCATION_MARKER + (text[-half:] if half else "")
        if count_tokens(candidate) <= target_tokens:
            best = candidate
            lo = half + 1
        else:
            hi = half - 1
    return best


def summarize_with_escalation(text, *, source_tokens, token_budget, call_llm,
                              l2_budget_ratio=0.5, l3_truncate_tokens=512,
                              timeout=60.0, circuit_breaker=None, spend_guard=None):
    """Return a smaller summary and the escalation level that produced it."""
    attempts = (
        (1, L1_PROMPT, max(1, int(token_budget))),
        (2, L2_PROMPT, max(1, int(token_budget * l2_budget_ratio))),
    )
    for level, template, budget in attempts:
        if circuit_breaker is not None and not circuit_breaker.allows():
            break
        if spend_guard is not None and not spend_guard.allows():
            break
        if spend_guard is not None:
            spend_guard.record_call()
        try:
            result = call_llm(template.format(text=text), budget * 2, timeout)
        except Exception:  # noqa: BLE001 - a failed level falls through to the next; never abort compaction
            result = None
        if result and result.strip() and count_tokens(result) < source_tokens:
            if circuit_breaker is not None:
                circuit_breaker.record_success()
            return result.strip(), level
        if circuit_breaker is not None:
            circuit_breaker.record_failure()
    return deterministic_truncate(text, l3_truncate_tokens), 3

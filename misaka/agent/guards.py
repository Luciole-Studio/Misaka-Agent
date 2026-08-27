"""Three guards against model pathologies the agent loop cannot fix by itself.

Pure logic and nothing else: a guard is handed one turn's observation and
answers with a :class:`GuardDecision`. No guard reads the database, the clock,
or the loop -- the caller supplies the observation and decides what to do with
the answer (inject the message as a steering turn, stop the loop, strip tools).

State is per-session and in memory. Losing it when the process dies is
harmless: every streak restarts at zero, which is the same state a fresh
session would have.
"""

from __future__ import annotations

import inspect
import json
import re
from collections import deque
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, Literal

from misaka.utils.values import read_field


@dataclass(frozen=True, slots=True)
class GuardDecision:
    """``hint`` = tell the model; ``stop`` = the model must stop calling tools."""

    action: Literal["none", "hint", "stop"]
    message: str = ""


_NONE = GuardDecision("none")


# --------------------------------------------------------------------------
# 1. Byte-identical tool calls on consecutive turns
# --------------------------------------------------------------------------

# borrowed from FrontierAgent(components/observers/repetition_guard.py), see its
# comment for the measured basis; revisit with local data.
# Its basis: 71% of sub-agents that exhausted their turn budget did so inside a
# run of ten or more consecutive byte-identical calls, median 87, worst 198/200.
REPEATED_CALL_HINT_STREAK = 3
REPEATED_CALL_STOP_STREAK = 6


def _batch_signature(tool_calls: Any) -> str:
    """A stable string for a whole turn's tool-call batch.

    No-raise by contract: this runs on the turn path, where an exception costs
    the turn. ``default=str`` covers what ``json.dumps`` rejects; a circular
    reference falls through to ``repr``.
    """
    parts: list[str] = []
    for call in tool_calls:
        name = str(read_field(call, "name", "") or "")
        args = read_field(call, "arguments")
        try:
            payload = json.dumps(args, sort_keys=True, default=str)
        except (TypeError, ValueError):
            payload = repr(args)
        parts.append(f"{name}:{payload}")
    return "\x00".join(parts)


def _batch_names(tool_calls: Any) -> str:
    return "、".join(sorted({str(read_field(call, "name", "") or "") for call in tool_calls}))


class RepeatedToolCallGuard:
    """Count consecutive turns whose tool-call batch is byte-identical.

    Feed it *every* assistant turn, tool-free ones included. A turn with no
    tool call clears the streak, and that is the only thing keeping one request
    from bleeding into the next: misaka's inner loop ends on a tool-free turn,
    so the assistant message that closes a request is exactly the reset. Skip
    those turns and three separate user requests that each open with the same
    ``git status`` add up to a "you are in a dead loop" accusation.

    Laundering a repeat through a narrated turn is not a way around it. To keep
    the loop alive past a tool-free turn the caller must have a steering or
    follow-up message pending, so the model cannot manufacture the reset on its
    own; the guard's own hint buys at most one, and the model's answer to it
    either carries the repeated call (streak intact) or ends the request.
    """

    def __init__(
        self,
        *,
        hint_streak: int = REPEATED_CALL_HINT_STREAK,
        stop_streak: int = REPEATED_CALL_STOP_STREAK,
    ) -> None:
        # 2 is the floor: a hint threshold of 1 would fire on every tool call.
        self.hint_streak = max(2, int(hint_streak))
        # Always at least one turn of hint before the stop, so the model gets a
        # chance to act on the warning.
        self.stop_streak = max(self.hint_streak + 1, int(stop_streak))
        self._signature = ""
        self._streak = 0

    def observe(self, tool_calls: Any) -> GuardDecision:
        """``tool_calls``: this turn's batch, each item exposing ``name``/``arguments``."""
        if not tool_calls:
            self._signature = ""
            self._streak = 0
            return _NONE

        signature = _batch_signature(tool_calls)
        if signature != self._signature:
            self._signature = signature
            self._streak = 1
            return _NONE

        self._streak += 1
        if self._streak >= self.stop_streak:
            return GuardDecision(
                "stop",
                f"你已经连续 {self._streak} 次发出完全相同的工具调用"
                f"({_batch_names(tool_calls)}),这已经是死循环。"
                "现在停止调用工具,用手上已有的信息给出结论。",
            )
        if self._streak == self.hint_streak:
            return GuardDecision(
                "hint",
                f"你已经连续 {self._streak} 次发出完全相同的工具调用"
                f"({_batch_names(tool_calls)}),参数一字未改,结果不会变化。"
                "请改参数、换工具,或者直接用已有信息继续往下做。",
            )
        return _NONE


# --------------------------------------------------------------------------
# 2. Near-verbatim assistant prose across recent turns
# --------------------------------------------------------------------------

# borrowed from FrontierAgent(components/observers/text_repetition_guard.py), see
# its comment for the measured basis; revisit with local data.
TEXT_SIMILARITY_THRESHOLD = 0.85
TEXT_WINDOW_SIZE = 4
TEXT_MIN_CHARS = 60
TEXT_HINT_STREAK = 4
TEXT_STOP_STREAK = 6

# One token per CJK character, one token per run of word characters elsewhere.
# FrontierAgent splits on whitespace, which collapses a whole Chinese sentence
# into a single token and leaves the guard comparing punctuation-delimited
# chunks. Splitting CJK per character turns the bigrams below into the
# character-bigrams Chinese actually needs, with no segmentation dependency.
_CJK = "぀-ヿ㐀-䶿一-鿿豈-﫿가-힯"
_TOKEN_RE = re.compile(f"[{_CJK}]|[^\\W{_CJK}]+")


def _shingles(text: str) -> frozenset[str]:
    """Token bigrams: robust to small wording edits in a way char n-grams are not."""
    tokens = _TOKEN_RE.findall(text.lower())
    if len(tokens) < 2:
        return frozenset(tokens)
    return frozenset(f"{a} {b}" for a, b in pairwise(tokens))


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    # An empty side means the text carried no comparable tokens at all -- a rule
    # of dashes, a row of emoji, a box-drawing progress bar. Set-theoretic
    # Jaccard calls two empty sets identical, which would make any two such
    # turns "near-verbatim" no matter how different they look. Absence of
    # evidence is not evidence, so an empty side never matches.
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def similarity(left: str, right: str) -> float:
    """Jaccard overlap of the two texts' token bigrams, in ``[0, 1]``.

    0 when either side tokenises to nothing, however similar the two look.
    """
    return _jaccard(_shingles(left), _shingles(right))


class TextRepetitionGuard:
    """Flag an assistant that keeps re-writing the same paragraph.

    Compares against a sliding window of recent turns rather than only the
    previous one: the pathology is usually an A/B alternation, not a literal
    repeat. Texts under ``min_chars`` are ignored entirely and never enter the
    window -- a short acknowledgement resembles every other short
    acknowledgement.
    """

    def __init__(
        self,
        *,
        threshold: float = TEXT_SIMILARITY_THRESHOLD,
        window_size: int = TEXT_WINDOW_SIZE,
        min_chars: int = TEXT_MIN_CHARS,
        hint_streak: int = TEXT_HINT_STREAK,
        stop_streak: int = TEXT_STOP_STREAK,
    ) -> None:
        self.threshold = float(threshold)
        self.min_chars = max(1, int(min_chars))
        self.hint_streak = max(2, int(hint_streak))
        self.stop_streak = max(self.hint_streak + 1, int(stop_streak))
        self._history: deque[frozenset[str]] = deque(maxlen=max(1, int(window_size)))
        self._streak = 0
        self._hinted = False

    def observe(self, assistant_text: str) -> GuardDecision:
        """``assistant_text``: the visible text of this turn's assistant message."""
        text = " ".join((assistant_text or "").split())
        if len(text) < self.min_chars:
            return _NONE

        current = _shingles(text)
        matched = any(_jaccard(current, prior) >= self.threshold for prior in self._history)
        self._history.append(current)

        if not matched:
            # 1, not 0: this turn is itself the seed of the next possible run,
            # so the first turn that matches it brings the streak to 2.
            self._streak = 1
            self._hinted = False
            return _NONE

        self._streak += 1
        if self._streak >= self.stop_streak:
            return GuardDecision(
                "stop",
                f"你已经连续 {self._streak} 轮输出几乎相同的内容,再写下去不会有新进展。"
                "现在停止调用工具,直接给出完整的最终答复。",
            )
        if self._streak >= self.hint_streak and not self._hinted:
            self._hinted = True
            return GuardDecision(
                "hint",
                f"你最近 {self._streak} 轮的回复彼此高度雷同"
                f"(重合度 ≥ {self.threshold:.0%}),说明你在原地打转。"
                "请用已经掌握的信息推进:写出阶段性结论、换一个工具,或者直接收尾。",
            )
        return _NONE


# --------------------------------------------------------------------------
# 3. Finalization reserve on a shrinking budget or lease
# --------------------------------------------------------------------------

# borrowed from FrontierAgent(components/observers/finalization_reserve.py and
# last_turn_forcer.py), see their comments for the measured basis; revisit with
# local data. FA reserves 8 of ~50 turns (~15%) for finalization and forces the
# penultimate turn; the two ratios below are those two tiers expressed as a
# fraction of the remaining allowance, so one guard serves any unit.
FINALIZATION_HINT_RATIO = 0.15
FINALIZATION_STOP_RATIO = 0.05


class FinalizationReserve:
    """Two-tier wind-down driven by whatever allowance the caller can measure.

    ``remaining``/``total`` must share a unit; the guard only ever uses their
    ratio. Both of misaka's readable allowances reduce to that shape:
    ``TurnBudgetLimiter`` (``limit - accounted`` over ``limit``, see
    ``misaka.agent.request_budget``) and a card lease (``claim_expires - now``
    over its TTL, see ``misaka.platform.tasks``). Deliberately no lookup here:
    a guard that opened the board database would be untestable and would tie
    the loop to a store it may not have.

    Each tier fires at most once. A budget that grows again (a renewed lease)
    does not re-arm a tier that already fired -- one wind-down instruction per
    session is the point; repeating it every turn is how a model learns to
    scroll past it.
    """

    def __init__(
        self,
        *,
        hint_ratio: float = FINALIZATION_HINT_RATIO,
        stop_ratio: float = FINALIZATION_STOP_RATIO,
    ) -> None:
        self.hint_ratio = float(hint_ratio)
        self.stop_ratio = min(float(stop_ratio), float(hint_ratio))
        self._tier = 0

    def observe(self, remaining: float, total: float) -> GuardDecision:
        """``remaining`` and ``total`` in any single unit (tokens, seconds, turns)."""
        try:
            total = float(total)
            ratio = max(0.0, float(remaining)) / total if total > 0 else -1.0
        except (TypeError, ValueError):
            ratio = -1.0
        if ratio < 0:
            # An unknown or nonsensical allowance is not evidence of scarcity.
            return _NONE

        tier = 2 if ratio <= self.stop_ratio else 1 if ratio <= self.hint_ratio else 0
        if tier <= self._tier:
            return _NONE
        self._tier = tier

        if tier == 2:
            return GuardDecision(
                "stop",
                f"额度只剩最后一档(剩余 {ratio:.0%})。立刻停止一切工具调用,"
                "现在就输出完整的最终交付;没做完的部分照实写明,不要留空。",
            )
        return GuardDecision(
            "hint",
            f"额度已进入收尾档(剩余 {ratio:.0%})。从现在开始写交付,不要再开新的调查。"
            "把已有结论、产出物和未完成项整理清楚,只做必要的核对。",
        )


class _SessionGuards:
    """The three guards wired to one engine session's turn cycle."""

    def __init__(self, session: Any, limiter: Any = None):
        self._session = session
        self._limiter = limiter
        self.repeated = RepeatedToolCallGuard()
        self.text = TextRepetitionGuard()
        self.reserve = FinalizationReserve()
        self.stopped: str = ""

    async def after_turn(self, context: Any) -> bool:
        message = read_field(context, "message")
        content = read_field(message, "content", []) or []
        tool_calls = [block for block in content if read_field(block, "type", "") == "toolCall"]
        text = "".join(
            str(read_field(block, "text", "") or "")
            for block in content
            if read_field(block, "type", "") == "text"
        )

        # Every turn is observed, tool-free ones included: that call is what tells
        # RepeatedToolCallGuard one request ended, so the next request may legitimately
        # open with the same call.
        decisions = [self.repeated.observe(tool_calls), self.text.observe(text)]
        if self._limiter is not None:
            decisions.append(
                self.reserve.observe(self._limiter.limit - self._limiter.accounted, self._limiter.limit)
            )

        stop = next((d for d in decisions if d.action == "stop"), None)
        if stop is not None:
            self.stopped = stop.message
            return True
        # A hint is only worth queueing while the loop is still going. A tool-free turn
        # ends the request; waking it back up to deliver advice would answer nobody.
        if tool_calls:
            for decision in decisions:
                if decision.action == "hint":
                    await self._session.steer(decision.message)
                    break
        return False


def install_guards(session: Any, limiter: Any = None) -> _SessionGuards | None:
    """Install one idempotent set of pathology guards on an engine session.

    Composed onto whatever ``shouldStopAfterTurn`` the session already carries, never
    replacing it: a guard stop and a caller stop are independent reasons to end a run.
    """

    agent = getattr(session, "agent", None)
    if agent is None:
        return None
    existing_guards = getattr(agent, "_misaka_guards", None)
    if isinstance(existing_guards, _SessionGuards):
        return existing_guards

    guards = _SessionGuards(session, limiter)
    original = agent.shouldStopAfterTurn

    async def should_stop(context: Any) -> bool:
        if await guards.after_turn(context):
            return True
        if original is None:
            return False
        result = original(context)
        return bool(await result if inspect.isawaitable(result) else result)

    agent.shouldStopAfterTurn = should_stop
    agent._misaka_guards = guards
    return guards


__all__ = [
    "FinalizationReserve",
    "GuardDecision",
    "RepeatedToolCallGuard",
    "TextRepetitionGuard",
    "install_guards",
    "similarity",
]

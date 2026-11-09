"""Four guards against model pathologies the agent loop cannot fix by itself.

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
import time
from collections import deque
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, Literal

from misaka.agent.types import AgentTurnDecision
from misaka.utils.values import call_with_optional_second_arg, maybe_await, read_field


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


def _call_names(tool_calls: Any) -> list[str]:
    """Just the names out of a turn's batch, in call order."""
    return [str(read_field(call, "name", "") or "") for call in tool_calls]


def _join_names(names: Any) -> str:
    return "、".join(sorted(set(names)))


def _batch_names(tool_calls: Any) -> str:
    return _join_names(_call_names(tool_calls))


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
    ratio. Both of misaka's readable allowances reduce to that shape: the token
    cap as the meter last read it (``misaka.core.platform.metering.allowance``)
    and a card lease (``claim_expires - now`` over its TTL, see
    ``misaka.core.platform.tasks``). Deliberately no lookup here:
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


# --------------------------------------------------------------------------
# 4. Turns that spend the allowance without doing any work
# --------------------------------------------------------------------------

# FrontierAgent e4cb2397109c957a2f99c3a5d850b9fc479dbef6 /
# workflows/agent_team/observers/no_progress_guard.py (Apache-2.0; THIRD_PARTY_NOTICES.md):
# its thresholds, gate, resets and nudge, with its four tools named in MISAKA's vocabulary.
NO_PROGRESS_HINT_STREAK = 6            # soft_streak
NO_PROGRESS_STOP_STREAK = 12           # hard_streak
NO_PROGRESS_MIN_REPEATED_CREATES = 3
NO_PROGRESS_COOLDOWN_TURNS = 3
# A task prompt shorter than this (after stripping trailing punctuation) is too terse to be a
# real research instruction -- one of the trivial wind-down markers a spin assigns to
# throwaway agents ("End.", "OK", "Done", ...).
_MIN_MEANINGFUL_PROMPT_LEN = 8
# Trivial task prompts a wind-down spin assigns to throwaway agents. Used only to ACCELERATE
# the hard stop (strong signal the agent is not doing real work), never as the sole trigger.
_DEGENERATE_PROMPTS = frozenset({
    "end", "done", "complete", "completed", "final", "finalize", "finish",
    "finished", "x", "ok", "okay", "stop", "confirm", "nothing", "na", "n/a",
})


class NoProgressGuard:
    """Break a coordinator's create/assign spin (FrontierAgent's ``NoProgressGuard``).

    A turn calling ONLY sub-agent management tools does no first-hand work: it just manages
    the roster. Everything else -- reading, searching, the board and card tools, writing --
    is progress and resets the streak, and so does a sub-agent's fresh substantive report.
    Without REPEATED new-agent creation a management streak is normal fan-out or waiting on
    running sub-agents, so nothing happens until ``min_repeated_creates`` of its turns have
    created one. A tool-free turn resets it too: misaka's inner loop ends there.

    ``vocabulary`` names the tools by role -- ``management`` (upstream's four), ``create``
    (``create_subagent``), ``assign`` (``assign_task``) and ``collect`` (``collect_reports``) --
    and the caller supplies it (:mod:`misaka.core.platform.vocabulary`); without one the
    guard has no opinion.
    """

    def __init__(
        self,
        vocabulary: Any = None,
        *,
        hint_streak: int = NO_PROGRESS_HINT_STREAK,
        stop_streak: int = NO_PROGRESS_STOP_STREAK,
        min_repeated_creates: int = NO_PROGRESS_MIN_REPEATED_CREATES,
        cooldown_turns: int = NO_PROGRESS_COOLDOWN_TURNS,
    ) -> None:
        vocabulary = vocabulary or {}
        self.management, self.create, self.assign, self.collect = (
            frozenset(str(name).casefold() for name in vocabulary.get(role, ()))
            for role in ("management", "create", "assign", "collect")
        )
        self._delegating = _join_names([*vocabulary.get("create", ()), *vocabulary.get("assign", ())])
        self.hint_streak = max(1, int(hint_streak))
        self.stop_streak = max(self.hint_streak + 1, int(stop_streak))
        self.min_repeated_creates = max(2, int(min_repeated_creates))
        self.cooldown_turns = max(1, int(cooldown_turns))
        self._turn = 0
        self._last_hint_turn = -(10**9)
        # Agents whose most recent assignment was a trivial wind-down prompt. A report from one
        # of these is completion of the spin itself, not evidence that substantive work happened.
        self._degenerate_agents: set[str] = set()
        self._reset()

    def _reset(self) -> None:
        self._streak = 0
        self._creates = 0
        self._degenerate = 0

    def observe(self, tool_calls: Any, tool_results: Any = ()) -> GuardDecision:
        """``tool_calls`` and ``tool_results``: this turn's batch and what it returned."""
        self._turn += 1
        calls = list(tool_calls or ())
        results = list(tool_results or ())
        # Which agents were assigned substantive vs. trivial work, before reading reports:
        # a turn can both assign and collect (and a foreground Agent does both in one call).
        self._record_assignment_quality(calls, results)
        reports = self._report_agents(results)
        fresh_report = any(agent not in self._degenerate_agents for agent in reports)
        self._degenerate_agents.difference_update(reports)
        if fresh_report:
            self._reset()
            return _NONE

        names = {str(read_field(call, "name", "") or "").casefold() for call in calls}
        # Empty turn, or any non-management tool: real work or a different mode.
        if not names or not names <= self.management:
            self._reset()
            return _NONE

        self._streak += 1
        if names & self.create:
            self._creates += 1
        if names & self.assign and self._assign_is_degenerate(calls):
            self._degenerate += 1
        # Not the pathology yet: without REPEATED new-agent creation this is just normal
        # fan-out / waiting on running sub-agents.
        if self._creates < self.min_repeated_creates:
            return _NONE

        degenerate = self._degenerate >= 2
        if self._streak >= self.stop_streak or (degenerate and self._streak >= self.hint_streak + 2):
            return GuardDecision(
                "stop",
                f"你已经连续 {self._streak} 轮只在新建、派活和收取子代理,没有拿到任何新信息。"
                "现在停止调用工具,把子代理已交回的报告综合成完整的最终答复。",
            )
        if self._streak >= self.hint_streak and self._turn - self._last_hint_turn >= self.cooldown_turns:
            self._last_hint_turn = self._turn
            return GuardDecision(
                "hint",
                "你一再新建子代理、给它们派活,却没有拿到任何新信息——子代理已经交回了覆盖这个问题的"
                f"完整报告。停止委派:不要再调用 {self._delegating}。把已有的全部内容综合起来,"
                "现在就以纯文本给出完整的最终答复(不调用工具)。",
            )
        return _NONE

    def _record_assignment_quality(self, calls: list[Any], results: list[Any]) -> None:
        """Track whether each agent's latest assignment is substantive."""
        created = {str(read_field(result, "toolCallId", "")): read_field(read_field(result, "details", {}) or {},
                                                                        "agentId")
                   for result in results}
        for call in calls:
            if str(read_field(call, "name", "") or "").casefold() not in self.assign:
                continue
            args = read_field(call, "arguments", {}) or {}
            agent = read_field(args, "to") or created.get(str(read_field(call, "id", "")))
            if not agent:
                continue
            if self._is_degenerate(_assignment(args)):
                self._degenerate_agents.add(str(agent))
            else:
                self._degenerate_agents.discard(str(agent))

    def _assign_is_degenerate(self, calls: list[Any]) -> bool:
        """True when an assignment's prompt is trivial wind-down text."""
        return any(self._is_degenerate(_assignment(read_field(call, "arguments", {}) or {}))
                   for call in calls if str(read_field(call, "name", "") or "").casefold() in self.assign)

    def _report_agents(self, results: list[Any]) -> set[str]:
        """The agents whose report came back this turn: a foreground ``Agent`` that completed,
        or a ``TaskOutput`` that retrieved a finished task. Status-only answers are not reports."""
        agents = set()
        for result in results:
            if read_field(result, "isError", False):
                continue
            if str(read_field(result, "toolName", "") or "").casefold() not in self.collect:
                continue
            details = read_field(result, "details", {}) or {}
            if read_field(details, "status") == "completed" and read_field(details, "agentId"):
                agents.add(str(read_field(details, "agentId")))
            elif read_field(details, "retrieval_status") == "success":
                task = read_field(details, "task", {}) or {}
                if read_field(task, "task_id"):
                    agents.add(str(read_field(task, "task_id")))
        return agents

    @staticmethod
    def _is_degenerate(prompt: str) -> bool:
        s = prompt.strip().lower().strip(".!?。！ ")
        return len(s) < _MIN_MEANINGFUL_PROMPT_LEN or s in _DEGENERATE_PROMPTS


def _assignment(args: Any) -> str:
    """The work an assignment hands over: ``Agent``'s prompt or ``SendMessage``'s message."""
    return str(read_field(args, "prompt", "") or read_field(args, "message", "") or "")


class _SessionGuards:
    """The four guards wired to one engine session's turn cycle.

    A ``stop`` verdict does not cut the loop where it stands. Every stop message is an
    instruction to *say* something ("stop calling tools and write the deliverable now"),
    and a run killed mid-batch ends on a toolResult: the session has no assistant text to
    return, so a card is marked failed, a DM is acked and silently dropped, and a research
    call gets "no json in output". So a stop steers the message in and strips the tools
    off the next turn instead, letting the model spend one tool-free turn on the wrap-up
    it was just told to write. The hard return only happens if that turn is somehow not
    the end of it.
    """

    def __init__(
        self,
        session: Any,
        allowance: Any = None,
        wall_seconds: float | None = None,
        no_progress_vocabulary: Any = None,
    ):
        self._session = session
        self._allowance = allowance
        # The wall clock is the one allowance every headless run has: `run_session`
        # cuts the whole prompt off with `asyncio.wait_for`, and a run cut there has
        # no assistant text to return. A token cap only exists when one is configured.
        self._wall_total = float(wall_seconds) if wall_seconds else 0.0
        self._wall_start = time.monotonic() if self._wall_total > 0 else 0.0
        self.repeated = RepeatedToolCallGuard()
        self.text = TextRepetitionGuard()
        self.no_progress = NoProgressGuard(no_progress_vocabulary)
        self.reserve = FinalizationReserve()
        self.stopped: str = ""
        self._forcing = False
        self._forced_turn = False
        self._previous_calls = ""

    def _scarcest_allowance(self) -> tuple[float, float] | None:
        """Whichever of the token budget and the wall clock is closer to running out."""
        pairs: list[tuple[float, float]] = []
        tokens = self._allowance() if self._allowance is not None else None
        if tokens is not None:
            pairs.append(tokens)
        if self._wall_total > 0:
            pairs.append((self._wall_total - (time.monotonic() - self._wall_start), self._wall_total))
        if not pairs:
            return None
        return min(pairs, key=lambda pair: pair[0] / pair[1] if pair[1] > 0 else 1.0)

    def _resolve(self, decisions: list[GuardDecision], tool_calls: Any) -> GuardDecision | None:
        """The one verdict worth acting on, after vetoing a text stop that has progress.

        ``decisions[1]`` is the text guard's, by the order ``after_turn`` builds the list.

        ``RepeatedToolCallGuard`` stops on proof of a loop: the same call, byte for byte.
        ``NoProgressGuard`` stops on proof of its own: a whole run of turns that only created,
        assigned and collected sub-agents without one fresh report. ``TextRepetitionGuard`` has no such
        proof -- a model that narrates each step with
        the same sentence and a different filename measures 0.879 similar while doing
        genuinely different work, and CJK per-character bigrams sit higher again. A
        changed tool-call signature is the evidence of progress the text guard lacks, so
        it downgrades that stop to a hint rather than killing a working run.
        """
        signature = _batch_signature(tool_calls) if tool_calls else ""
        progressing = bool(signature) and signature != self._previous_calls
        self._previous_calls = signature

        text_decision = decisions[1]
        if progressing and text_decision.action == "stop":
            decisions[1] = GuardDecision("hint", text_decision.message)
        return next((d for d in decisions if d.action == "stop"), None) or next(
            (d for d in decisions if d.action == "hint"), None
        )

    def _observe_prose_turn(self, text: str) -> None:
        """Exactly what an ordinary tool-free turn does to the guards, verdicts discarded.

        Three of the four have per-request state and are handled here: the two streak
        guards clear on an empty batch, and the text guard takes the turn into its window
        the way it would any other prose. ``FinalizationReserve`` is absent on purpose --
        its tier is monotonic by design (one wind-down per session, not per request), so
        it has nothing to reset and cannot fire twice. ``_previous_calls`` is cleared with
        them, so the next request's first batch reads as progress in ``_resolve``.
        """
        self.repeated.observe(())
        self.text.observe(text)
        self.no_progress.observe(())
        self._previous_calls = ""

    async def after_turn(self, context: Any) -> bool:
        message = read_field(context, "message")
        content = read_field(message, "content", []) or []
        tool_calls = [block for block in content if read_field(block, "type", "") == "toolCall"]
        text = "".join(
            str(read_field(block, "text", "") or "")
            for block in content
            if read_field(block, "type", "") == "text"
        )

        # Take and clear both flags before anything can return. A request can die
        # between arming and firing -- an errored or aborted turn returns straight out
        # of `_run_loop` without ever consulting `finishTurn` -- so a flag left
        # standing would otherwise be waiting for the *next* request. Whoever reaches
        # `after_turn` first owns the flags.
        forced, self._forcing, self._forced_turn = self._forced_turn, False, False

        # Clearing alone is not enough: the stranded flag would still be spent on the
        # next request's first turn, ending it after its tools had already run but
        # before the model wrote anything. So the flag is confirmed against the one
        # observable consequence of arming it -- `next_turn` handed this turn an empty
        # tool list, and a turn with no tools cannot come back with tool calls. A turn
        # that did call tools was never the wrap-up turn, whatever the flag says; it
        # falls through and is judged on its own merits.
        if forced and not tool_calls:
            # The wrap-up turn ran with no tools available. Whatever it produced is the
            # answer, so no verdict of its own is acted on -- but the guards must still
            # see it. This is the only tool-free turn the whole request has, and a
            # tool-free turn is the documented reset (see RepeatedToolCallGuard and
            # NoProgressGuard): `_run_session` keeps the session alive for follow-up
            # turns, so a streak left standing here stops the next request's first call.
            self._observe_prose_turn(text)
            return True

        # Every turn is observed, tool-free ones included: that call is what tells
        # RepeatedToolCallGuard one request ended, so the next request may legitimately
        # open with the same call.
        decisions = [
            self.repeated.observe(tool_calls),
            self.text.observe(text),  # index 1: _resolve downgrades this one
            self.no_progress.observe(tool_calls, read_field(context, "toolResults", []) or []),
        ]
        allowance = self._scarcest_allowance()
        if allowance is not None:
            decisions.append(self.reserve.observe(*allowance))

        verdict = self._resolve(decisions, tool_calls)
        if verdict is None:
            return False
        if verdict.action == "stop":
            self.stopped = verdict.message
            if not tool_calls:
                # Already a prose turn: the request is ending on its own and the model
                # has had its say. Nothing left to force.
                return True
            self._forcing = True
            await self._session.steer(verdict.message)
            return False
        # A hint is only worth queueing while the loop is still going. A tool-free turn
        # ends the request; waking it back up to deliver advice would answer nobody.
        if tool_calls:
            await self._session.steer(verdict.message)
        return False

    def next_turn(self, context: Any) -> Any:
        """Strip the tools off the wrap-up turn, so the model can only answer in prose.

        Arming (``_forcing``) and firing (``_forced_turn``) are two flags rather than
        one so the wrap-up turn can be recognised in ``after_turn`` even after the
        context snapshot has moved on. Neither survives a request: a run can die
        between arming and firing -- an errored or aborted turn returns out of the loop
        without consulting ``finishTurn`` -- so ``after_turn`` clears both on
        entry, and the next request starts from a clean slate.
        """
        if not self._forcing:
            return None
        from misaka.agent.types import AgentContext, AgentLoopTurnUpdate

        current = read_field(context, "context")
        if current is None:
            # No snapshot to rewrite: the tools were never stripped, so this turn is not
            # the wrap-up turn and must not be marked as one.
            return None
        self._forced_turn = True
        # pi 0.87: the transcript's system messages carry the prompt and the tool declarations;
        # an empty executable set is declared to the model as every tool removed.
        return AgentLoopTurnUpdate(
            context=AgentContext(messages=read_field(current, "messages", []) or [], tools=[])
        )


def install_guards(
    session: Any,
    allowance: Any = None,
    wall_seconds: float | None = None,
    no_progress_vocabulary: Any = None,
) -> _SessionGuards | None:
    """Install one idempotent set of pathology guards on an engine session.

    Composed onto whatever ``finishTurn`` the session already carries, never replacing it:
    a guard stop and a caller stop are independent reasons to end a run.

    ``no_progress_vocabulary`` is ``NoProgressGuard``'s, empty by default: a caller that cannot
    name its sub-agent tools gets no opinion about a spin rather than a guess.
    ``misaka.core.platform.session`` passes the real one.
    """

    agent = getattr(session, "agent", None)
    if agent is None:
        return None
    existing_guards = getattr(agent, "_misaka_guards", None)
    if isinstance(existing_guards, _SessionGuards):
        return existing_guards

    guards = _SessionGuards(session, allowance, wall_seconds, no_progress_vocabulary)
    original_prepare = getattr(agent, "prepareNextTurn", None)
    original_prepare_with_context = getattr(agent, "prepareNextTurnWithContext", None)
    finish_turn = finish_turn_from_stop_predicate(guards.after_turn, getattr(agent, "finishTurn", None))

    async def prepare_next_turn(context: Any, signal: Any = None) -> Any:
        prepared = None
        if original_prepare_with_context is not None:
            prepared = original_prepare_with_context(context, signal)
        elif original_prepare is not None:
            prepared = original_prepare(signal)
        if inspect.isawaitable(prepared):
            prepared = await prepared

        prepared_context = read_field(prepared, "context")
        forced = guards.next_turn(
            {
                "context": (
                    prepared_context
                    if prepared_context is not None
                    else read_field(context, "context")
                )
            }
        )
        if forced is not None:
            forced.model = read_field(prepared, "model")
            forced.thinkingLevel = read_field(prepared, "thinkingLevel")
            return forced
        return prepared

    agent.finishTurn = finish_turn
    agent.prepareNextTurnWithContext = prepare_next_turn
    agent._misaka_guards = guards
    return guards


def finish_turn_from_stop_predicate(predicate: Any, previous: Any = None) -> Any:
    """A ``finishTurn`` from a stop-after-turn predicate (pi 0.87 removed ``shouldStopAfterTurn``).

    ``finishTurn`` also receives error and aborted responses; those remain hard exits, so the
    predicate is not consulted for them. Composed onto ``previous``: an ``end`` from either
    side ends the run, and the previous hook's other decisions are preserved.
    """

    async def finish_turn(turn: Any, signal: Any = None) -> Any:
        message = read_field(turn, "message")
        if read_field(message, "stopReason") not in ("error", "aborted"):
            stop = await maybe_await(call_with_optional_second_arg(predicate, turn, signal))
            if stop:
                return AgentTurnDecision(action="end")
        if previous is None:
            return None
        return await maybe_await(call_with_optional_second_arg(previous, turn, signal))

    return finish_turn


__all__ = [
    "FinalizationReserve",
    "GuardDecision",
    "NoProgressGuard",
    "RepeatedToolCallGuard",
    "TextRepetitionGuard",
    "finish_turn_from_stop_predicate",
    "install_guards",
    "similarity",
]

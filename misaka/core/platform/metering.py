"""Every model request a metered session makes, leased against the token cap and recorded.

All of them pass one point, ``misaka.ai.api._meter``: the built-in APIs (``lazyApi``) and the api registry ask it --
the agent's own turns, compaction summaries, the vision bridge, MoA's member calls, LCM's summaries, the cache
warmer. :func:`install` puts :func:`meter` there. A request finds the ledger it is billed to
(``budget.ledger``: the window turn's usage context, else the ``MISAKA_USAGE_*`` environment of a
card, node or sub-agent process); with none it passes through untouched, as a plain chat does.

Billed, it leases its worst case -- the context's upper bound, the output it may write, and the
thinking an adapter asks for beyond that (``thinking_allowance``) -- waits while other requests in
flight fill the cap, stops with ``budget.EXHAUSTED_MESSAGE`` when spending already has, sends with
``maxTokens`` cut to the lease, and records what it used when it ends. Reasoning is left as the
caller asked: the lease pays for it. 2026-10-07 (issue #10 plan, token_cap): a run used to get one
fixed 32,768-token slice for its whole life, smaller than one of its requests, and only the
session's own turns were bounded at all.
"""
from __future__ import annotations

import asyncio
import contextvars
import copy
import logging
import time
from contextlib import contextmanager
from typing import Any

from misaka.agent.request_budget import context_token_upper_bound, usage_tokens
from misaka.ai.api._thinking_allowance import thinking_allowance
from misaka.ai.runtime_observation import observe
from misaka.ai.types import AssistantMessage, ErrorEvent
from misaka.ai.utils.event_stream import AssistantMessageEventStream, spawn_stream_task
from misaka.core.platform import budget
from misaka.utils.values import read_field

logger = logging.getLogger(__name__)

MIN_OUTPUT_TOKENS = 1024
FALLBACK_OUTPUT_TOKENS = 32_000
RENEW_SECONDS = 300
WAIT_FIRST, WAIT_MOST = 0.5, 5.0

_last: dict[tuple, tuple[int, int]] = {}
_local: contextvars.ContextVar = contextvars.ContextVar("misaka_local_allowance", default=None)
_warned_uncapped: set[str] = set()


class _Local:
    """A session's own allowance with no board behind it (the skill review's input budget)."""

    def __init__(self, limit: int):
        self.limit, self.used, self.in_flight = int(limit), 0, 0


@contextmanager
def local_allowance(limit: int):
    """Bound the model requests made inside this block to ``limit`` tokens in all."""
    token = _local.set(_Local(limit))
    try:
        yield
    finally:
        _local.reset(token)


def allowance() -> tuple[int, int] | None:
    """``(remaining, cap)`` -- the cap less what the run has spent, as the meter last read and then
    settled it -- for the guards' wind-down hint; None when nothing caps this turn. Leases in flight
    are not taken off: they are worst cases that mostly come back, and counting them (0.18.9) wound
    sessions down with most of the cap unspent. No lookup: the reading is the meter's own."""
    local = _local.get()
    if local is not None:
        return local.limit - local.used, local.limit
    current = budget.ledger()
    return _last.get((current[0], current[1])) if current else None


def install() -> None:
    from misaka.ai.api import _meter
    _meter.set_request_meter(meter)


def _limited(options: Any, max_tokens: int) -> Any:
    """The caller's options with ``maxTokens`` cut to the lease: a shallow copy, so the provider
    still holds the caller's ``signal`` (0.18.9 copied deep: the copy's signal never fired, and an
    Event a provider had already waited on dragged the running loop into the copy -- "cannot
    pickle '_asyncio.Task' object" on a card's second request). A provider's own retry is left as
    configured: it repeats a request that never started, which spends nothing."""
    updates = {"maxTokens": max(1, int(max_tokens))}
    if options is None:
        from misaka.ai.types import SimpleStreamOptions
        return SimpleStreamOptions(**updates)
    if hasattr(options, "model_copy"):
        return options.model_copy(update=updates)
    if isinstance(options, dict):
        return {**options, **updates}
    limited = copy.copy(options)
    for key, value in updates.items():
        setattr(limited, key, value)
    return limited


def _ended(out: AssistantMessageEventStream, model: Any, reason: str, text: str | None, startedAt: int) -> None:
    """End ``out`` with no reply: an error (or an abort) the agent loop reads like a provider's. Like a provider's
    reply it carries the time the request started, and ``out`` times it from there."""
    from misaka.ai.api._common import _empty_usage
    message = AssistantMessage(content=[], api=model.api, provider=model.provider, model=model.id,
                               usage=_empty_usage(), stopReason=reason, errorMessage=text,
                               timestamp=startedAt)
    out.push(ErrorEvent(reason=reason, error=message))
    out.end(message)


def _exhausted_text(reading: dict, need: int) -> str:
    left = max(0, int(reading.get("cap", 0)) - int(reading.get("used", 0)))
    return (f"{budget.EXHAUSTED_MESSAGE}: the next request needs up to {need:,} tokens and this run "
            f"has {left:,} of its {int(reading.get('cap', 0)):,} left. It stops here; raise the cap and resume it "
            f"to go on.")


def _observe_admission(event: str, reading: dict, estimate: dict) -> None:
    cap = reading.get("cap")
    used = reading.get("used")
    leased = reading.get("lease") is not None
    granted = reading.get("tokens", 0)
    balance = {}
    if cap or reading.get("local"):
        # The Board returns held after inserting our lease. Report other requests' holds at admission.
        balance = {
            "budget_scope": "local" if reading.get("local") else "board",
            "budget_cap": cap,
            "budget_used": used,
            "budget_remaining": max(0, cap - used),
            "budget_held": reading["held"] - (granted if leased else 0),
        }
    observe(event, status=reading["status"], budget_granted=granted, budget_leased=leased, **estimate, **balance)


async def _lease(current, local, need: int, want: int, signal: Any, on_wait) -> dict:
    path, task_id, generation, cap = current if current else (None, None, None, 0)
    wait = WAIT_FIRST
    waiting = False
    while True:
        if local is not None:
            local_reading = {"used": local.used, "held": local.in_flight, "cap": local.limit, "local": True}
            room = local.limit - local.used - local.in_flight
            if room < need:
                return {"status": "exhausted", **local_reading}
            want = min(want, room)
        if current and cap:
            reading = await asyncio.to_thread(budget.lease_request_path, path, cap, task_id, generation, need, want)
        else:
            reading = {"status": "granted", "lease": None, "tokens": want}
        if reading["status"] != "wait":
            if reading["status"] == "granted" and local is not None:
                local.in_flight += reading["tokens"]
            if current and reading.get("cap"):
                _last[(path, task_id)] = (reading["cap"] - reading["used"], reading["cap"])
            if local is not None and not reading.get("cap"):
                return {**reading, **local_reading}
            return reading
        if not waiting:
            on_wait(reading)
            waiting = True
        if read_field(signal, "aborted"):
            return {**reading, "status": "aborted"}
        await asyncio.sleep(wait)
        wait = min(WAIT_MOST, wait * 2)


def meter(model: Any, context: Any, options: Any, send) -> AssistantMessageEventStream:
    """The registry's request meter: ``send(options)`` is the provider's own stream call."""
    current = budget.ledger()
    local = _local.get()
    if current is None and local is None:
        return send(options)
    out = AssistantMessageEventStream()
    startedAt = time.time_ns() // 1_000_000

    async def run() -> None:
        bound = context_token_upper_bound(context)
        model_max = int(getattr(model, "maxTokens", 0) or 0)
        configured = int(read_field(options, "maxTokens", 0) or 0) if options is not None else 0
        output = min(configured or model_max or FALLBACK_OUTPUT_TOKENS, model_max or FALLBACK_OUTPUT_TOKENS)
        if model.api == "openai-codex-responses" and model.api not in _warned_uncapped:
            _warned_uncapped.add(model.api)
            logger.warning("%s takes no output limit: the token cap leases its whole output, "
                           "but cannot cut a reply short", model.api)
        extra = thinking_allowance(model, options)
        minimum_output = min(MIN_OUTPUT_TOKENS, output)
        need = bound + minimum_output + extra
        want = bound + output + extra
        estimate = {"budget_input_bound": bound, "budget_minimum_output": minimum_output,
                    "budget_thinking_allowance": extra, "budget_need": need, "budget_desired": want}

        def waiting(reading):
            _observe_admission("budget_wait", reading, estimate)

        try:
            reading = await _lease(current, local, need, want, read_field(options, "signal"), waiting)
        except Exception as error:  # noqa: BLE001 - a ledger that cannot be read sends nothing: fail closed, visibly
            _observe_admission("budget_unavailable", {"status": "unavailable"}, estimate)
            _ended(out, model, "error", f"token ledger unavailable: {type(error).__name__}: {error}", startedAt)
            return
        if reading["status"] == "aborted":
            _observe_admission("budget_aborted", reading, estimate)
            _ended(out, model, "aborted", "Request was aborted", startedAt)
            return
        if reading["status"] == "exhausted":
            _observe_admission("budget_refused", reading, estimate)
            if reading.get("local"):
                _ended(out, model, "error", f"this session's allowance of {reading['cap']:,} tokens is used up", startedAt)
                return
            if current and current[3]:
                await asyncio.to_thread(budget.cap_reached_path, current[0], current[1], current[3])
            _ended(out, model, "error", _exhausted_text(reading, need), startedAt)
            return
        lease, granted = reading.get("lease"), int(reading["tokens"])
        _observe_admission("budget_admitted", reading, estimate)
        streamed, started = 0, False
        message = terminal = None
        cause = None
        try:
            inner = send(_limited(options, granted - bound - extra))
            renewed = time.monotonic()
            async for event in inner:
                started = started or read_field(event, "type") == "start"
                delta = read_field(event, "delta")
                if isinstance(delta, str):
                    streamed += len(delta)
                if lease and time.monotonic() - renewed > RENEW_SECONDS:
                    renewed = time.monotonic()
                    await asyncio.to_thread(budget.renew_request_path, current[0], lease)
                if read_field(event, "type") in ("done", "error"):
                    terminal = event
                    cause = getattr(inner, "error_cause", None)
                    continue
                out.push(event)
            message = await inner.result()
        except Exception as error:  # noqa: BLE001 - a provider that raised instead of reporting still ends the stream
            failure = f"{type(error).__name__}: {error}"
        else:
            failure = None
        finally:
            used = usage_tokens(read_field(message, "usage", {}) or {}) if message is not None else 0
            if used <= 0 and started:
                used = bound + streamed
            if local is not None:
                local.in_flight -= granted
                local.used += used
            key = (current[0], current[1]) if current else None
            if key in _last:
                _last[key] = (_last[key][0] - used, _last[key][1])
            if current:
                try:
                    await asyncio.shield(asyncio.to_thread(
                        budget.settle_request_path, current[0], lease, current[1], current[2], used))
                except Exception:
                    logger.warning("token ledger: a request's usage (%s tokens) was not recorded", used, exc_info=True)
        if terminal is not None:
            out.push(terminal, cause=cause)
        if failure is not None:
            _ended(out, model, "error", failure, startedAt)
        else:
            out.end(message)

    spawn_stream_task(run(), stream=out)
    return out


__all__ = ["allowance", "install", "local_allowance", "meter"]

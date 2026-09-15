"""Own off-loop LCM work, including cancellation across its auxiliary loops."""
from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
from contextlib import contextmanager

from misaka.utils.async_lifecycle import settle
from misaka.utils.values import signal_aborted

logger = logging.getLogger(__name__)
_SIGNAL = contextvars.ContextVar("lcm_operation_signal", default=None)
_TOOL_OWNER = contextvars.ContextVar("lcm_tool_worker_owner", default=None)


@contextmanager
def worker_owner(owner):
    token = _TOOL_OWNER.set(owner)
    try:
        yield
    finally:
        _TOOL_OWNER.reset(token)


def install_worker_ownership():
    """Keep upstream deadlines; retain ownership of workers that outlive them."""
    from functools import wraps

    from ..vendor import tools
    original = tools._run_within_deadline
    if getattr(original, "_misaka_owned", False):
        return

    @wraps(original)
    def run(fn, **kwargs):
        owner = _TOOL_OWNER.get()
        if owner is None or float(kwargs["remaining_s"]) <= 0:
            return original(fn, **kwargs)
        condition, pending = vars(owner).setdefault("_misaka_tool_workers", (threading.Condition(), set()))
        ticket = object()
        with condition:
            pending.add(ticket)
        def finish():
            with condition:
                pending.discard(ticket)
                condition.notify_all()
        def owned():
            try:
                return fn()
            finally:
                finish()
        try:
            return original(owned, **kwargs)
        except TimeoutError:
            raise  # The abandoned worker releases its own ticket when it settles.
        except (RuntimeError, OSError):
            finish()  # Capacity/start failure; no worker owns the ticket.
            raise
    run._misaka_owned = True
    tools._run_within_deadline = run


def drain_tool_workers(owner, timeout=25):
    workers = getattr(owner, "_misaka_tool_workers", None)
    if workers is not None:
        condition, pending = workers
        with condition:
            if not condition.wait_for(lambda: not pending, timeout=timeout):
                raise TimeoutError("MISAKA LCM tool workers are still active; retain the cache for next-start cleanup")


class _Signal:
    def __init__(self, parent):
        self.parent = parent
        self.cancelled = threading.Event()

    @property
    def aborted(self):
        # Read the parent's flag, never await an Event owned by another loop.
        return self.cancelled.is_set() or signal_aborted(self.parent)


def current_signal():
    return _SIGNAL.get()


def check_cancelled():
    if signal_aborted(current_signal()):
        # Exception would enter upstream's next-model/deterministic fallback.
        raise asyncio.CancelledError("LCM operation cancelled")


async def off_loop(work, *args, ctx=None, signal=None, must_finish=False):
    """Interrupt ordinary work; final flush/close and cursor repair must finish."""
    from . import context_engine, llm

    state = _Signal(signal)
    if state.aborted:
        raise asyncio.CancelledError("LCM operation cancelled")

    def locked():
        token = _SIGNAL.set(state)
        try:
            with llm.runtime(ctx):
                check_cancelled()
                with context_engine.operation(ctx):
                    check_cancelled()
                    from misaka.utils.values import read_field

                    from ..native.image_token_cost import (
                        image_cost_context,
                        learned_image_token_cost,
                    )
                    model = read_field(ctx, 'model')
                    cost = learned_image_token_cost(read_field(model, 'id'), read_field(model, 'baseUrl'))
                    with image_cost_context(cost):
                        result = work(*args)
                    check_cancelled()
                    return result
        finally:
            _SIGNAL.reset(token)

    future = asyncio.get_running_loop().run_in_executor(None, contextvars.copy_context().run, locked)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        if not must_finish:
            state.cancelled.set()
        try:
            await settle(future)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("LCM worker failed while settling cancellation", exc_info=True)
        raise

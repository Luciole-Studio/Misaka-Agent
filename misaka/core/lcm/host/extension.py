"""The harness registration that puts the vendored engine on misaka's compaction seam.

Deliberately thin, and deliberately not a copy of the pre-port ``extension.py`` next to
it: that file registers four ``lcm_*`` tools which read the mini implementation's schema
and would go blind against an upstream database. Upstream's own fifteen replace them
here, and only here -- ``activate`` picks one register function, so the two sets of
``lcm_*`` names can never both be on the model's tool list.

Every handler here is a thin ``async def`` around a synchronous call into the vendored
engine, and none of that work may run on the event loop -- see ``_off_loop``.
"""

from __future__ import annotations

import asyncio
import logging

from misaka.core.wiring import ToolCollector

from . import context_engine, externalize, preanswer, tools

logger = logging.getLogger(__name__)


async def _off_loop(work, *args):
    """One synchronous engine call, in a worker thread and under the engine lock.

    Both halves are load-bearing, and both are ``tools.py``'s answer to the same problem
    (its module docstring states it, ``_answer`` implements it) applied to the event
    handlers, which had gone on calling the engine inline.

    *The thread*, because the work is not merely sqlite. A compaction escalates to the
    auxiliary summariser, and that seam (``host/llm.py`` -> ``platform.session.run_coro``)
    runs a whole nested model turn, up to three of them, blocking whichever thread called
    it. On the loop that is every session in this process frozen -- no keystroke, no
    render, no abort -- for the length of a model round trip. The pre-answer brief has the
    same shape on the ``context`` event, i.e. before *every* request rather than only
    before a compaction.

    *The lock*, because moving the work off the loop is what removes the serialisation
    the loop was providing. Upstream's connections take one caller at a time (see
    ``context_engine.ENGINE_LOCK``), and ``context_engine._host_driven_boundary`` pins
    four config fields for the length of one compaction -- fields ``externalize`` and
    ``preanswer`` read. Holding the same lock across all of it is what keeps that window
    invisible without anyone having to reason about which handlers can overlap.

    What this depends on, stated because an earlier version of this note got it wrong:
    these handlers *do* run inside ``platform.session.run_session``'s environment window.
    Bundled extensions reach a session as ``extension_factories``, and two live callers
    pass exactly that to ``run_session`` -- ``misaka/cli/dm.py`` and
    ``misaka/core/network/worker.py``, both under ``run_coro``. So the nesting is real:
    ``to_thread`` copies ``_ENV_WINDOW_OWNER`` into this worker, the summariser's
    ``run_coro`` finds no loop here, and ``_env_window`` must recognise the nested session
    as re-entrant or it will wait on the ``_ENV_LOCK`` its own caller holds. The no-loop
    branch of ``run_coro`` stamps the re-entry token for that reason; do not remove it.
    The engine lock is not re-entered along the same path: the summariser runs through
    ``run_text``, which passes no ``extension_factories``, so the nested session has no
    LCM handlers of its own.
    """

    def _locked():
        with context_engine.ENGINE_LOCK:
            return work(*args)

    return await asyncio.to_thread(_locked)


class LcmPart:
    """The engine's tools for one session of ``kind``, and what it does at the kernel's moments.

    ``kind`` is the session kind ``core.wiring`` assembled this part for; ``tools.withheld``
    turns it into the subset of the fifteen this session is offered. The moment methods
    are what the kernel calls (``core.moments``). Each fails open, three times over: a
    session must start, a missed ingest is repaired at the next bind, and a failed
    compaction leaves pi's native summariser to it.
    """

    def __init__(self, *, kind: str) -> None:
        collector = ToolCollector()
        try:
            tools.register(collector, withhold=tools.withheld(kind, context_engine.engine()))
        except Exception:
            logger.warning("LCM tools were not registered; this session runs without them.", exc_info=True)
        self.tools = collector.tools

    async def session_start(self, event, ctx):
        try:
            await _off_loop(context_engine.start, ctx)
        except Exception:
            logger.warning("LCM session bind failed; this session runs without LCM.", exc_info=True)

    async def _sync(self, ctx):
        try:
            await _off_loop(context_engine.sync, ctx)
        except Exception:
            logger.warning("LCM ingest failed; the transcript is unaffected.", exc_info=True)

    async def before_agent_start(self, event, ctx):
        await self._sync(ctx)

    async def agent_end(self, event, ctx):
        await self._sync(ctx)

    async def session_shutdown(self, event, ctx):
        await self._sync(ctx)

    async def session_compact(self, event, ctx):
        await self._sync(ctx)

    async def session_before_compact(self, event, ctx):
        try:
            return await _off_loop(context_engine.compact, event, ctx)
        except Exception:
            logger.warning("LCM compaction failed; keeping the native summariser.", exc_info=True)
            return None

    async def session_compact_failed(self, event, ctx):
        # The engine committed its side of a compaction pi then threw away (an abort
        # during the round, or a later failure), so its ingest cursor now points past
        # messages that are still in the live context. Rebinding the session is how
        # upstream repairs a cursor from the store; without it the next ingest writes
        # that whole region a second time.
        try:
            await _off_loop(context_engine.start, ctx)
        except Exception:
            logger.warning("LCM could not reset its ingest cursor after a discarded "
                           "compaction; the store may take duplicate rows.", exc_info=True)

    async def context(self, event, ctx):
        # The last seam before the provider, and the only one that can rewrite a message
        # `pi` is keeping. Two steps, in this order on purpose: the stubber's protected
        # fresh tail is counted from the end of the list, and appending the brief first
        # would move that boundary. The stubber is off unless
        # LCM_LARGE_OUTPUT_ACTIVE_REPLAY_STUBBING_ENABLED says otherwise.
        messages = event.get("messages", []) if isinstance(event, dict) else []
        for step, failure in (
            (externalize.stub_replay, "LCM could not stub the live context; it goes out in full."),
            (preanswer.inject, "LCM pre-answer evidence failed; the turn goes out unchanged."),
        ):
            try:
                result = await _off_loop(step, {"type": "context", "messages": messages}, ctx)
            except Exception:
                logger.warning(failure, exc_info=True)
                continue
            replaced = result.get("messages") if isinstance(result, dict) else None
            if replaced is not None:
                messages = replaced
        return {"messages": messages}

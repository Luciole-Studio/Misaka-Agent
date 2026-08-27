"""The harness registration that puts the vendored engine on misaka's compaction seam.

Deliberately thin, and deliberately not a copy of the pre-port ``extension.py`` next to
it: that file registers four ``lcm_*`` tools which read the mini implementation's schema
and would go blind against an upstream database. Upstream's own fifteen replace them
here, and only here -- ``activate`` picks one register function, so the two sets of
``lcm_*`` names can never both be on the model's tool list.
"""

from __future__ import annotations

import logging

from . import context_engine, externalize, tools

logger = logging.getLogger(__name__)


def register(harn):
    """Register the engine's tools and subscribe it to the session events it needs."""

    async def session_start(event, ctx):
        try:
            context_engine.start(ctx)
        # Fail open, three times over: a session must start, a missed ingest is repaired
        # at the next bind, and a failed compaction leaves pi's native summariser to it.
        except Exception:
            logger.warning("LCM session bind failed; this session runs without LCM.", exc_info=True)

    async def sync_event(event, ctx):
        try:
            context_engine.sync(ctx)
        except Exception:
            logger.warning("LCM ingest failed; the transcript is unaffected.", exc_info=True)

    async def before_compact(event, ctx):
        try:
            return context_engine.compact(event, ctx)
        except Exception:
            logger.warning("LCM compaction failed; keeping the native summariser.", exc_info=True)
            return None

    async def compact_failed(event, ctx):
        # The engine committed its side of a compaction pi then threw away (an abort
        # during the round, or a later failure), so its ingest cursor now points past
        # messages that are still in the live context. Rebinding the session is how
        # upstream repairs a cursor from the store; without it the next ingest writes
        # that whole region a second time.
        try:
            context_engine.start(ctx)
        except Exception:
            logger.warning("LCM could not reset its ingest cursor after a discarded "
                           "compaction; the store may take duplicate rows.", exc_info=True)

    async def transform_context(event, ctx):
        # The last seam before the provider, and the only one that can rewrite a message
        # `pi` is keeping -- which is what active-replay stubbing is. Off unless
        # LCM_LARGE_OUTPUT_ACTIVE_REPLAY_STUBBING_ENABLED says otherwise.
        try:
            return externalize.stub_replay(event, ctx)
        except Exception:
            logger.warning("LCM could not stub the live context; it goes out in full.", exc_info=True)
            return None

    try:
        tools.register(harn)
    except Exception:
        logger.warning("LCM tools were not registered; this session runs without them.", exc_info=True)

    harn.on("session_start", session_start)
    harn.on("before_agent_start", sync_event)
    harn.on("agent_end", sync_event)
    harn.on("session_shutdown", sync_event)
    harn.on("session_before_compact", before_compact)
    harn.on("session_compact", sync_event)
    harn.on("session_compact_failed", compact_failed)
    harn.on("context", transform_context)

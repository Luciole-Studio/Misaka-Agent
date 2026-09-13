"""The harness registration that puts the vendored engine on misaka's compaction seam.

Every handler here is a thin ``async def`` around a synchronous call into the vendored
engine, and none of that work may run on the event loop -- see ``_off_loop``.
"""

from __future__ import annotations

import logging
import os
from functools import partial

from misaka.utils.values import read_field

from . import context_engine, execution, preanswer, slash, tools

logger = logging.getLogger(__name__)


async def _off_loop(work, *args, signal=None, must_finish=False):
    """Run storage/model work off the UI loop with the caller's provider registry."""

    return await execution.off_loop(
        work, *args, ctx=args[-1] if args else None, signal=signal, must_finish=must_finish)


def register(harn, *, kind: str):
    """Register the original tool surface and adapt MISAKA lifecycle events."""

    from .nous import flow
    harn.registerProvider('nous', {'oauth': flow})

    async def session_start(event, ctx):
        try:
            await _off_loop(context_engine.bound_engine, ctx)
        # Binding failure does not prevent opening saved history. The next operation
        # retries the bind; owned compaction errors never invoke a different engine.
        except Exception:
            logger.warning("LCM session bind failed; the next operation retries it.", exc_info=True)

    async def sync_event(event, ctx):
        try:
            await _off_loop(partial(context_engine.sync, transcript=context_engine.snapshot(ctx)), ctx)
        except Exception:
            logger.warning("LCM ingest failed; the transcript is unaffected.", exc_info=True)

    async def session_tree(event, ctx):
        # Rebind and ingest before another prompt is appended to the selected branch.
        def rebind(transcript, ctx):
            context_engine.start(ctx)
            context_engine.sync(ctx, transcript=transcript)
        try:
            await _off_loop(rebind, context_engine.snapshot(ctx), ctx)
        except Exception:
            logger.warning("LCM tree rebind failed; the next sync retries it.", exc_info=True)

    async def context_prepare(event, ctx):
        transcript = context_engine.snapshot(ctx)
        frozen = context_engine.freeze_context(ctx, transcript)
        prepared = await execution.off_loop(
            partial(context_engine.prepare, transcript=transcript), event, frozen,
            ctx=frozen, signal=read_field(event, "signal"))
        async def execute():
            return await execution.off_loop(context_engine.compact, prepared, ctx=frozen,
                                            signal=read_field(event, "signal"))
        return {"execute": execute if prepared is not None else None}

    async def compact_failed(event, ctx):
        # The engine committed its side of a compaction pi then threw away (an abort
        # during the round, or a later failure), so its ingest cursor now points past
        # messages that are still in the live context. Rebinding the session is how
        # upstream repairs a cursor from the store; without it the next ingest writes
        # that whole region a second time.
        try:
            await _off_loop(context_engine.start, ctx, must_finish=True)
        except Exception:
            logger.warning("LCM could not reset its ingest cursor after a discarded "
                           "compaction; the store may take duplicate rows.", exc_info=True)

    async def session_end(event, ctx):
        if read_field(event, 'contextCarried', False):
            return  # The explicit rollover already ran end/reset/start/carry.
        try:
            # Upstream on_session_end already performs one bounded final flush.
            await _off_loop(partial(context_engine.end, transcript=context_engine.snapshot(ctx),
                                    reset=read_field(event, "reason") == "new"),
                            ctx, must_finish=True)
        except Exception:
            logger.warning("LCM session end failed; the store keeps what was ingested.", exc_info=True)

    async def session_carry(event, ctx):
        from . import carry
        count = await _off_loop(carry.rollover, event['sessionManager'], context_engine.snapshot(ctx), ctx,
                                must_finish=True)
        return {'handled': True, 'carriedNodes': count}

    async def response_usage(event, ctx):
        message = event.get("message") if isinstance(event, dict) else getattr(event, "message", None)
        if getattr(message, "role", None) != "assistant" and not (isinstance(message, dict) and message.get("role") == "assistant"):
            return
        try:
            await _off_loop(context_engine.usage, message, ctx)
        except Exception:
            logger.warning("LCM usage update failed; the next one repairs it.", exc_info=True)

    async def discover_resources(event, ctx):
        # hermes registers its bundled skill through the plugin's register_skill; pi's door
        # for a skill root an extension ships is resources_discover.
        return {"skillPaths": [os.path.join(os.path.dirname(os.path.dirname(__file__)), "vendor", "skills")]}

    async def lcm_command(args, ctx):
        try:
            text = await _off_loop(slash.run, args or "", ctx)
        except (SystemExit, ValueError):
            text = slash.USAGE
        ctx.ui.notify(text, "info")

    async def preanswer_context(event, ctx):
        # Keep the native user turn and protected-tail positions intact.
        try:
            return await _off_loop(partial(preanswer.inject, active_tools=harn.getActiveTools()),
                                   event, ctx, signal=read_field(ctx, "signal"))
        except Exception:
            logger.warning("LCM pre-answer evidence failed; the turn goes out unchanged.", exc_info=True)
            return None

    try:
        tools.register(harn)
    except Exception:
        logger.warning("LCM tools were not registered; this session runs without them.", exc_info=True)

    async def model_select(event, ctx):
        await _off_loop(context_engine.update_model, ctx)

    harn.on("session_start", session_start)
    harn.on("session_tree", session_tree)
    harn.on("model_select", model_select)
    harn.on("before_agent_start", sync_event)
    harn.on("agent_end", sync_event)
    harn.on("session_shutdown", session_end)
    harn.on("session_context_carry", session_carry)
    harn.on("message_end", response_usage)
    harn.on("resources_discover", discover_resources)
    from ..vendor import _env_flag_enabled
    if _env_flag_enabled("LCM_ENABLE_SLASH_COMMAND", default=False):
        harn.registerCommand("lcm", {
            "description": "Inspect, back up, and maintain the LCM context database.",
            "handler": lcm_command,
        })
    harn.on("session_context_prepare", context_prepare)
    harn.on("session_compact", sync_event)
    harn.on("session_compact_failed", compact_failed)
    harn.on("context", preanswer_context)

"""Nudge the original rollup scheduler after a long-lived MISAKA session compacts.

Hermes rebinds frequently; MISAKA keeps its session engine alive between compactions.
Operator commands use vendor.command directly, not a second rebuild implementation.
"""
from __future__ import annotations


def nudge(engine) -> None:
    """Ask for one bounded rollup pass over the session this engine is bound to.

    Silent and free when the feature is off, which is its default: the whole family is
    opt-in behind `LCM_TEMPORAL_ROLLUPS_ENABLED`, and an engine built without it has no
    rollup tables, no mutation triggers and nothing to maintain.
    """
    config = getattr(engine, "_config", None)
    session_id = str(getattr(engine, "current_session_id", "") or "")
    if config is None or not config.temporal_rollups_enabled or not session_id:
        return
    # Upstream's own scheduler call. It swallows its own failures (maintenance is
    # opportunistic and must never fail a foreground turn), so there is nothing to catch.
    engine._schedule_rollup_maintenance(session_id)

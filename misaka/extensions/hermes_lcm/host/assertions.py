"""``/lcm assertions`` -- upstream's own assertion rebuild, as the extension's command.

The V4 assertion sidecar needs no host wiring to *run*: ``LCM_ASSERTIONS_ENABLED``
materialises the store inside ``_bind_storage``, ``LCM_ASSERTION_EXTRACTION_ENABLED``
builds the extractor beside it, and ``compaction.py`` queues one bounded batch per
compaction round -- all of which the host already reaches by calling ``compress``.
``lcm_query_state`` is likewise one of the fifteen schemas ``host/tools.py`` registers.

What upstream reaches through its slash command and misaka had no door for is the
operator side: re-deriving assertions for source rows that were ingested before the
family was switched on, or after ``CURRENT_EXTRACTION_VERSION`` moves. That is
``/lcm assertions rebuild``, and this module forwards it the way ``host/embed.py``
forwards ``/lcm embed`` -- the upstream implementation is the whole contract (the plan
digest, the late source-hash CAS, the bounded batch), and a rewrite would be a second
copy of it that costs model calls when it drifts.

Dry run is the default at both ends. Upstream's plan mode never constructs an extractor,
so `/lcm assertions rebuild` cannot spend anything; ``--apply`` is refused outright
unless extraction is enabled, which is the same gate the compaction path answers to.

``--apply`` is also the one place in this family where the per-pass bound is *not* four:
the compaction path caps a batch at ``LCM_ASSERTION_EXTRACTION_MAX_SOURCES_PER_PASS``
(default 4, hard ceiling 8), while a rebuild walks the whole selected plan in one
synchronous pass -- upstream's ``--limit``, default 100 and capped at 500, is the only
thing between the operator and 100 auxiliary calls. So the CLI names the number.
"""

from __future__ import annotations

from . import config_bridge, context_engine


def rebuild(*, apply: bool = False, limit: int | None = None) -> str:
    """One ``/lcm assertions rebuild`` run against the configured database."""
    built = context_engine.engine()
    if built is None:
        return (
            f"No usable LCM engine for {config_bridge.database_path()}."
        )
    tokens = ["assertions", "rebuild"]
    if apply:
        tokens.append("--apply")
    if limit is not None:
        tokens += ["--limit", str(limit)]
    try:
        from ..vendor.command import handle_lcm_command

        return handle_lcm_command(" ".join(tokens), built)
    finally:
        # A CLI run owns the engine it just built: leaving its sqlite connections open
        # would hold a WAL lock past the point the command has printed its answer.
        context_engine.close_all()

"""``misaka lcm embed`` -- upstream's own embedding operations, on misaka's CLI.

Upstream ships warmup and backfill as ``/lcm embed ...`` inside ``vendor/command.py``,
and that implementation is the whole of the contract: provider resolution, the dimension
lock that stops two models' vectors from mixing, the dry-run estimate, and an apply run
that is leased, resumable and honest about ambiguous remote acceptance. So this module
resolves an engine, forwards the subcommand, and gets out of the way. Restating any of
it here would be a second copy of a contract upstream keeps changing -- and the estimate
a rewrite got wrong is money.

Only ``embed`` is forwarded. The rest of upstream's operations surface (``status``,
``doctor``, ``rotate``, ``preset``, ...) is a later phase's; ``misaka lcm`` keeps its own
operations until then.
"""

from __future__ import annotations

from . import context_engine, switch


def run(subcommand: str, *, apply: bool = False, limit: int | None = None) -> str:
    """One ``/lcm embed <subcommand>`` run against the configured database."""
    built = context_engine.engine()
    if built is None:
        return (
            f"No usable LCM engine for {switch.database_path()}. A pre-port database has "
            "to be rebuilt first: `misaka lcm migrate --apply`."
        )
    tokens = ["embed", subcommand]
    # `warmup` takes no flags and upstream answers a flagged one with its help text, so
    # the two flags `misaka lcm` shares across its ops are forwarded only where they mean
    # something.
    if subcommand == "backfill":
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

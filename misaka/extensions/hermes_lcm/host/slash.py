"""``/lcm``: the operator surface, registered by the extension the way pi's llama registers ``/llama``.

Everything upstream's own ``/lcm`` offers, plus the four opt-in families upstream's doctor
predates. ``run(argv)`` returns the report text; a usage error raises ``SystemExit(2)`` with
the usage line already in the text.
"""

from __future__ import annotations

import argparse
import sys

USAGE = ("/lcm [status|doctor|backup|embed warmup|backfill|rollups [--rebuild]|externalize-backfill"
         "|assertions rebuild|rotate SESSION|preset show|suggest|apply [NAME]] [--apply] [--limit N]")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="/lcm", add_help=False, exit_on_error=False)
    parser.add_argument("op", nargs="?", default="status",
                        choices=["status", "doctor", "backup", "embed",
                                 "rollups", "externalize-backfill", "assertions", "rotate", "preset"])
    parser.add_argument("target", nargs="?")
    parser.add_argument("name", nargs="?")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--rebuild", action="store_true")
    return parser


def run(argv: list[str]) -> str:
    """The report for one ``/lcm`` invocation."""
    lines: list[str] = []
    try:
        args = _parser().parse_args(argv)
    except (argparse.ArgumentError, SystemExit):
        lines.append(USAGE)
        raise SystemExit(2) from None

    def out(text: str = "") -> None:
        lines.append(str(text))

    try:
        _run(args, out)
    except SystemExit:
        sys.stdout.write("\n".join(lines) + "\n")
        raise
    return "\n".join(lines)


def _run(args, out):
    """The LCM operator surface: everything upstream's own `/lcm` offers, plus the four
    opt-in families upstream's doctor predates."""
    from misaka.extensions.hermes_lcm.host import config_bridge as lcm_config
    from misaka.extensions.hermes_lcm.host import operations as lcm_ops

    lcm_db = lcm_config.database_path()

    if args.op in {"status", "doctor", "backup"}:
        out(lcm_ops.report(args.op))
    elif args.op == "externalize-backfill":
        # Old rows do not benefit from switching externalization on; this is how they
        # catch up. Dry run first, like migrate: `--apply` is what rewrites anything.
        from misaka.extensions.hermes_lcm.host import externalize as lcm_externalize
        result = (lcm_externalize.run(args.limit) if args.apply
                  else lcm_externalize.plan(args.limit))
        out(f"Database {result['database'] or lcm_db}")
        if result["note"]:
            out(result["note"])
        out(f"Payload directory {result['directory'] or '(unset)'} | threshold "
              f"{result['threshold_chars']:,} chars")
        if result.get("applied"):
            out(f"{result['moved']} tool result(s) moved to payload files; "
                  f"{result['chars_moved']:,} characters left the database, "
                  f"{result['bytes']:,} bytes reclaimed on disk.")
        else:
            out(f"{result['rows']} tool result(s) totalling {result['chars']:,} characters "
                  "would move to payload files.")
            if not args.apply:
                out("Dry run; nothing was written. Re-run with --apply to externalize.")
    elif args.op == "embed":
        # Upstream's own `/lcm embed` implementation, forwarded whole: the dry run is the
        # default and `--apply` is what spends anything.
        from misaka.extensions.hermes_lcm.host import embed as lcm_embed
        if args.target not in {"warmup", "backfill"}:
            out("Usage: /lcm embed warmup|backfill [--apply] [--limit N]")
            sys.exit(2)
        out(lcm_embed.run(args.target, apply=args.apply, limit=args.limit))
    elif args.op == "assertions":
        # Upstream's own `/lcm assertions rebuild`, forwarded whole. The dry run is the
        # default and never constructs an extractor; `--apply` is what calls a model, once
        # per source row it re-derives.
        from misaka.extensions.hermes_lcm.host import assertions as lcm_assertions
        if args.target not in {None, "rebuild"}:
            out("Usage: /lcm assertions rebuild [--apply] [--limit N]")
            sys.exit(2)
        out(lcm_assertions.rebuild(apply=args.apply, limit=args.limit))
    elif args.op == "rollups":
        from misaka.extensions.hermes_lcm.host import rollups as lcm_rollups
        # The engine resolves its database through `config_bridge.database_path()`, which lets
        # upstream's own `LCM_DATABASE_PATH` win. Reporting on `lcm_db` instead would
        # answer "nothing has been built" about a file the engine never opened -- and
        # `--rebuild` would build into one database and print a status from another.
        rollup_db = lcm_db
        if args.rebuild:
            outcome = lcm_rollups.rebuild(rollup_db)
            if outcome.get("error"):
                out(f"Database {rollup_db}\n{outcome['error']}")
                sys.exit(2)
            out(f"Seeded {sum(outcome['seeded'].values())} periods in "
                  f"{len(outcome['seeded'])} scopes; built {outcome['built']}.")
            for scope in outcome["exhausted"]:
                out(f"  {scope}: still had work when the pass budget ran out; run it again.")
            report = outcome["status"]
        else:
            report = lcm_rollups.status(rollup_db)
        state = "enabled" if report["enabled"] else "disabled (set LCM_TEMPORAL_ROLLUPS_ENABLED=true)"
        out(f"Database {report['database']} | temporal rollups {state} | "
              f"{report['pending_invalidations']} pending invalidations")
        if not report["installed"]:
            out("No rollup tables in this database yet; nothing has been built.")
        for scope, kinds in sorted(report["scopes"].items()):
            counted = " | ".join(
                f"{kind}: " + ", ".join(f"{state} {count}" for state, count in sorted(states.items()))
                for kind, states in sorted(kinds.items())
            )
            oldest = report["oldest_stale"].get(scope)
            out(f"  {scope}  {counted}" + (f"  (oldest stale {oldest})" if oldest else ""))
        if report["last_error"]:
            out(f"Last build error: {report['last_error']}")
    elif args.op == "rotate":
        # Upstream's own in-place compact, forwarded whole: same session id, same
        # conversation id, no summariser call, and the raw rows stay recoverable. The
        # preview is the default and `--apply` is what writes the rolling backup and
        # advances the lifecycle frontier. The session is an argument because a CLI has
        # no active session for upstream to rotate.
        from misaka.extensions.hermes_lcm.host import operations as lcm_operations
        out(lcm_operations.rotate(args.target or "", apply=args.apply))
    elif args.op == "preset":
        # Upstream's benchmarked model-family presets, forwarded whole. Its `apply`
        # writes no configuration in any mode, so `--apply` gets the same preview plus a
        # line saying it had nothing to commit.
        from misaka.extensions.hermes_lcm.host import operations as lcm_operations
        if args.target not in {"show", "suggest", "apply"}:
            out("Usage: /lcm preset show|suggest|apply [NAME] [--apply]")
            sys.exit(2)
        out(lcm_operations.preset(args.target, args.name or "", apply=args.apply))



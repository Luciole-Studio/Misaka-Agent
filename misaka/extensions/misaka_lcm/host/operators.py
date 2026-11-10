"""Explicit operator entry points; never open a runtime engine before a dry run."""
from __future__ import annotations

import re
import sys
from pathlib import Path

COMMANDS = {"import": "scripts.import_lossless_claw",
            "externalize-backfill": "scripts.backfill_externalized_tool_outputs",
            "state-embedding-backfill": "benchmarking.state_embedding_backfill"}

# The dispatcher is upstream's and writes its slash command. At a terminal the same words follow
# `misaka lcm`. A path is not the command: `.misaka/lcm/lcm.db`, or a project folder named `lcm`.
_SLASH = re.compile(r"""(?<![^\s`"'(])/lcm(?= [a-z])""")
_HELP = "LCM command help"
# What this file adds to upstream's commands, which its help cannot know of.
_OWN_HELP = """\
Only at the command line:
- misaka lcm clear: remove this project's LCM cache; session files are untouched
- misaka lcm repair [--apply]: report, or with --apply name, the summaries older checkpoints show as things you typed
- misaka lcm import | externalize-backfill | state-embedding-backfill: operator scripts; each takes --help"""


def _for_terminal(text):
    """Upstream's reply as a terminal reads it: `misaka lcm ...` for `/lcm ...`, and this file's
    own commands under the help."""
    # Bare `misaka lcm` prints the help, where bare `/lcm` is the status.
    text = _SLASH.sub("misaka lcm", text.replace("- /lcm or /lcm status:", "- /lcm status:"))
    return f"{text}\n\n{_OWN_HELP}" if _HELP in text else text


def _owned_option(options, name, value):
    """Operator imports/backfills own the current project's cache, not another DB."""
    for index, item in enumerate(options):
        if item == name or item.startswith(name + "="):
            raw = options[index + 1] if item == name and index + 1 < len(options) else item.partition("=")[2]
            if not raw or Path(raw).expanduser().resolve() != Path(value).resolve():
                raise ValueError(f"{name} must name this project's LCM path: {value}")
    # Keep the canonical value last: argparse also accepts abbreviated flags,
    # which must not override the project's path after an injected default.
    return [*options, name, value]


def main(argv=None):
    import importlib

    from . import config_bridge, context_engine, operations, storage

    args = list(sys.argv[1:] if argv is None else argv)
    # Nothing here knew a help flag: `misaka lcm --help` was "Unknown subcommand: --help", and
    # `misaka lcm clear --help` cleared. The operator scripts have a help of their own.
    if ("--help" in args or "-h" in args) and args[0] not in COMMANDS:
        args = ["help"]
    if args[:1] == ["clear"]:
        workspace = storage.project()
        if storage.clear(workspace):
            print(f"MISAKA LCM cache of {workspace} removed. Session files are untouched.")
            return 0
        print(f"MISAKA LCM cache of {workspace} is in use; nothing was removed.")
        return 1
    if args[:1] == ["repair"]:
        from . import repair
        return repair.run(apply="--apply" in args[1:])
    if args and args[0] in COMMANDS:
        module = importlib.import_module("misaka.extensions.misaka_lcm.vendor." + COMMANDS[args[0]])
        options = args[1:]
        database = config_bridge.database_path()
        flag = {"import": "--target-db", "externalize-backfill": "--database", "state-embedding-backfill": "--db"}[args[0]]
        options = _owned_option(options, flag, database)
        root = str(Path(database).parent)
        if args[0] == "externalize-backfill":
            options = _owned_option(options, "--hermes-home", root)
            options = _owned_option(options, "--manifest", str(Path(root) / "externalization-manifest.json"))
        elif args[0] == "state-embedding-backfill":
            options = _owned_option(options, "--asset-root", root)
            options = _owned_option(options, "--ledger", str(Path(root) / "embedding-ledger.jsonl"))
            options = _owned_option(options, "--summary", str(Path(root) / "embedding-summary.json"))
        if "--help" in options or "-h" in options:
            return module.main(options)
        workspace = storage.project()
        storage.acquire(workspace)
        try:
            if args[0] == "import" and module._build_parser().parse_args(options).apply:
                # Seed the same generation's IDs before the importer allocates
                # rows; dry runs still never open a writable runtime database.
                context_engine.engine()
                context_engine.close()
            return module.main(options)
        finally:
            context_engine.release_project()
    print(_for_terminal(operations.command(" ".join(args) or "help")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

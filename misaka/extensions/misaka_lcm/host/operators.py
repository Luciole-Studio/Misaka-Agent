"""Explicit operator entry points; never open a runtime engine before a dry run."""
from __future__ import annotations

import sys
from pathlib import Path

COMMANDS = {"import": "scripts.import_lossless_claw",
            "externalize-backfill": "scripts.backfill_externalized_tool_outputs",
            "state-embedding-backfill": "benchmarking.state_embedding_backfill"}


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
    print(operations.command(" ".join(args) or "help"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

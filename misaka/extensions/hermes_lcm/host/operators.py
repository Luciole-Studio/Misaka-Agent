"""Explicit operator entry points; never open a runtime engine before a dry run."""
from __future__ import annotations

import sys

COMMANDS = {"import": "scripts.import_lossless_claw",
            "externalize-backfill": "scripts.backfill_externalized_tool_outputs",
            "state-embedding-backfill": "benchmarking.state_embedding_backfill"}


def main(argv=None):
    import importlib

    from . import config_bridge, operations

    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in COMMANDS:
        module = importlib.import_module("misaka.extensions.hermes_lcm.vendor." + COMMANDS[args[0]])
        options = args[1:]
        if args[0] == "import" and not any(item == "--target-db" or item.startswith("--target-db=") for item in options):
            options = ["--target-db", config_bridge.database_path(), *options]
        return module.main(options)
    print(operations.command(" ".join(args) or "help"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""``python -m misaka.cli.subagent_child``: the sub-agent child process.

The runtime (``misaka.core.subagent.runtime``) starts every child with this module so the
process goes through the same entry wiring as the CLI does -- the bundled extensions a
child needs (its parent's provider may be one of them) are composed in by
``misaka.cli.bootstrap``, which core cannot do for itself.
"""

from __future__ import annotations

import asyncio
import os

from misaka.cli import bootstrap
from misaka.core.subagent import child
from misaka.utils import loop_watchdog


def main() -> int:
    bootstrap.install()
    owner = os.environ.get("MISAKA_SISTER_OWNER_TASK_ID")
    loop_watchdog.configure(f"card-{owner}" if owner else "subagent", exit_on_stall=True)
    return asyncio.run(loop_watchdog.watched(child.amain()))


if __name__ == "__main__":
    raise SystemExit(main())

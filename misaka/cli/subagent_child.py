"""``python -m misaka.cli.subagent_child``: the sub-agent child process.

The runtime (``misaka.core.subagent.runtime``) starts every child with this module so the
process goes through the same entry wiring as the CLI does -- the bundled extensions a
child needs (its parent's provider may be one of them) are composed in by
``misaka.cli.bootstrap``, which core cannot do for itself.
"""

from __future__ import annotations

import asyncio

from misaka.cli import bootstrap
from misaka.core.subagent import child


def main() -> int:
    bootstrap.install()
    return asyncio.run(child.amain())


if __name__ == "__main__":
    raise SystemExit(main())

"""``python -m misaka.cli.research_node --run-card TASK_ID``: a research card's child process.

Node executors use ``misaka research --node`` in the background; a card child needs
no CLI surface beyond this entry. It runs the same entry wiring as the CLI
(``misaka.cli.bootstrap``) before
``misaka.core.research.node`` builds its session.
"""

from __future__ import annotations

import sys

from misaka.cli import bootstrap
from misaka.core.research import node


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != "--run-card":
        sys.exit("usage: python -m misaka.cli.research_node --run-card TASK_ID")
    bootstrap.install()
    return node.main_card(args[1])


if __name__ == "__main__":
    raise SystemExit(main())

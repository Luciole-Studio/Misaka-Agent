"""``python -m misaka.cli.research_node --run-card TASK_ID``: a research card's child process.

A node and a fork are user-facing and keep their CLI sub-command; a card child is the
research workflow's own and needs no CLI surface beyond this entry, which exists so the
process runs the same entry wiring as the CLI (``misaka.cli.bootstrap``) before
``misaka.core.research.node`` builds its session.
"""

from __future__ import annotations

import sys

from misaka.cli import bootstrap
from misaka.core.research import node


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] != ["--run-card"] or len(args) != 2:
        sys.exit("usage: python -m misaka.cli.research_node --run-card TASK_ID")
    bootstrap.install()
    return node.main_card(args[1])


if __name__ == "__main__":
    raise SystemExit(main())

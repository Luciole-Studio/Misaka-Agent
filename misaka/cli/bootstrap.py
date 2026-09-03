"""What a process entry wires before any session is built.

Pi's ``main.ts`` composes its bundled extensions ahead of the caller's factories and
hands the list down into core, which never imports them. MISAKA also builds sessions
from inside core -- a card under the daemon, a sub-agent child -- where the bundled
package cannot be imported without core referring to it. So the entry assigns the
composition function into ``misaka.core.wiring.bundled`` instead, and core calls it
with each session's spec. Every process entry calls ``install`` first: the CLI
(``misaka.cli.app``), the sub-agent child (``misaka.cli.subagent_child``) and the
research node (``misaka.cli.research_node``). A process that never runs an entry -- a
bare kernel run in a test -- builds sessions without bundled extensions.
"""

from __future__ import annotations

from misaka import extensions
from misaka.core import wiring


def install() -> None:
    wiring.bundled = extensions.discover


__all__ = ["install"]

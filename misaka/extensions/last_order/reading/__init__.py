"""Reading mode: read materials closely with Last Order, a question at a time.

Last Order leads; for each reading action (overview, reconstruct, presuppose, transform, compare,
translate, reread, generate) a study reader works with that action's method, in a session of its
own that a line of reading keeps. Lines fork under different lenses, are compared, and send the
reader back to earlier material; the reading graph records where each understanding came from.
A port of PR #12 to MISAKA's own engine. See ``docs/guide/reading.md``.

- ``/read`` in Last Order's window, and the window as the reading's leader: :mod:`.window`
- ``misaka read``: :mod:`.cli`
- the operations and the study readers: :mod:`.engine`
- the reading's graph, in the board's database: :mod:`.store`; what it leaves in the project: :mod:`.views`
- materials: :mod:`.materials`; everything the models read: :mod:`.prompts` and ``methods/``

A bundled extension that can be switched off: ``"extensions": ["-builtin:reading"]`` in settings.
"""

from __future__ import annotations

EXTENSION_NAME = "reading"
BUILTIN = True
SESSION_KINDS = {"foreground"}
COMMAND = "read"


def command(argv):
    """Co-read materials with a question: open, act, fork, compare, reread, graph, chat."""
    from .cli import main

    return main(argv)


def activate(_spec):
    from .window import register

    return register

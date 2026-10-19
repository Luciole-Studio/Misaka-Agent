"""Reading action mode: co-read a question against real materials.

The user-facing entry is ``misaka read ...`` (:func:`misaka.reading.host.main`) and the
TUI's ``/read`` command (:mod:`misaka.extensions.last_order.reading`). Both drive the same
Node engine in ``packages/reading`` through a child process; this package owns the parts
that must stay in Python -- the workspace, the document corpus, and the model parameters
MISAKA already resolved.

The engine is TypeScript and must be built before first use::

    npm --prefix packages/reading install
    npm --prefix packages/reading run build

A run lives at ``<workspace>/reading/<run-id>/`` -- ``graph.json`` (the reading map),
``graph.md`` and ``reading.md`` (readable renderings), ``reader.md`` (the reader's
question), ``nodes/`` (one file per node, the actual text), and ``sessions/`` (the engine's
own conversation files, so a later command resumes where the reading stopped).
"""

from __future__ import annotations

__version__ = "0.1.0"

from misaka.reading.host import (
    ReadingClient,
    ReadingError,
    main,
)

__all__ = ["ReadingClient", "ReadingError", "__version__", "main"]

"""A mutable transaction-scoped view of a JSON value, preserving tuple positions.

Translated from pi's ``packages/chord/src/delta/draft.ts``. The TypeScript type maps readonly properties to
mutable ones and stops at a depth of 8. Python's type system has no such mapping, so a draft is typed as the
value it views. At run time a draft is a ``DraftObject`` or a ``DraftArray`` (``tracker.py``).
"""

from __future__ import annotations

type Draft[T] = T

__all__ = ["Draft"]

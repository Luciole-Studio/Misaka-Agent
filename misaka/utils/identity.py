"""Identity sets over objects that cannot be weakly referenced.

pi's chord keeps ``WeakSet<object>`` of the JSON containers it has already validated or copied. A plain
``dict`` or ``list`` cannot be weakly referenced, and a set of ``id()`` values alone would go stale when a
container is freed and its id reused. :class:`IdentityWeakSet` therefore keeps the containers alive and
lets go of the ones nothing else refers to, measured by CPython's reference counts, which is what a weak
set observes through the collector.
"""

from __future__ import annotations

import sys
from typing import Any

_MINIMUM_SWEEP = 1024


def _baseline() -> int:
    """The reference count of an object that only the set refers to, as the sweep below sees it."""
    items: dict[int, Any] = {0: [0]}
    return next(sys.getrefcount(value) for _key, value in items.items())


# Measured once, with the same loop shape the sweep uses, so a different interpreter's counting still agrees.
_BASELINE = _baseline()


class IdentityWeakSet:
    """A set of objects compared by identity. An object that nothing else refers to leaves the set at the
    next sweep, so the set does not keep a revision of a large state alive."""

    def __init__(self) -> None:
        self._items: dict[int, Any] = {}
        self._sweepAt = _MINIMUM_SWEEP

    def add(self, value: Any) -> None:
        self._items[id(value)] = value
        if len(self._items) >= self._sweepAt:
            self._sweep()

    def __contains__(self, value: Any) -> bool:
        return self._items.get(id(value)) is value

    def __len__(self) -> int:
        return len(self._items)

    def discard(self, value: Any) -> None:
        if self._items.get(id(value)) is value:
            del self._items[id(value)]

    def _sweep(self) -> None:
        # A container held only by a swept parent becomes unreferenced with it, so repeat while anything goes.
        while True:
            dead = [key for key, value in self._items.items() if sys.getrefcount(value) <= _BASELINE]
            if not dead:
                break
            for key in dead:
                del self._items[key]
        self._sweepAt = max(_MINIMUM_SWEEP, len(self._items) * 2)


__all__ = ["IdentityWeakSet"]

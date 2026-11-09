"""One FIFO queue for every observer notification (state events, shape pushes, registry tables, listener
invocations).

Translated from pi's ``packages/chord/src/publish.ts``.

Problem: notifying observers synchronously from inside a write lets a re-entrant write overtake the write being
published. Example: subscriber A reacts to n = 1 by writing n = 2; subscriber B then sees 2 before 1.
Solution: a publication first adopts every revision it makes (cells and derivations), then enqueues its
notifications. Notifications run in enqueue order, so every observer sees revisions in adoption order.
`holdPublications` defers delivery until a multi-step change (a reload commit) is complete.

MISAKA fork: upstream's queue is a module variable, so there is one per JavaScript realm, and each worker thread is its
own realm. A Python module is shared by every thread, so the queue and its ``held``/``draining`` flags live in a
``threading.local``: one queue per thread, as upstream has one per realm. (A module-global queue would run a notification
enqueued on one thread on another thread that happens to be draining or holding.)
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable


class _Realm(threading.local):
    """One thread's ``queue``, ``held`` and ``draining`` (upstream's module variables of one realm)."""

    def __init__(self) -> None:
        self.queue: deque[Callable[[], None]] = deque()
        self.held = 0
        self.draining = False


_realm = _Realm()


def enqueue(task: Callable[[], None]) -> None:
    """Runs ``task`` after everything enqueued before it. Tasks must not throw (report instead)."""
    _realm.queue.append(task)
    _drain()


def holdPublications[T](fn: Callable[[], T]) -> T:
    """Delivers nothing until ``fn`` returns, then everything it enqueued, in order."""
    realm = _realm
    realm.held += 1
    try:
        return fn()
    finally:
        realm.held -= 1
        _drain()


def _drain() -> None:
    realm = _realm
    if realm.draining or realm.held > 0:
        return
    realm.draining = True
    try:
        while realm.queue:
            realm.queue.popleft()()
    finally:
        realm.draining = False


__all__ = ["enqueue", "holdPublications"]

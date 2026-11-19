"""Central timing instrumentation for startup profiling.

Enable with the ``MISAKA_TIMING=1`` environment variable.

Ported from pi's ``src/core/timings.ts``. The namespace split is the part that earns
its keep: pi times extension loading under its own namespace (``extensions/loader.ts``
calls ``time(..., "extensions")``), so a slow third-party extension shows up as its own
group rather than as an unexplained gap in startup.
"""

from __future__ import annotations

import os
import sys
import time as _time
from dataclasses import dataclass, field
from typing import Literal

ENABLED = os.environ.get("MISAKA_TIMING") == "1"

TimingLabel = Literal["main", "extensions"]


@dataclass(slots=True)
class _TimingNamespace:
    timings: list[tuple[str, int]] = field(default_factory=list)
    lastTime: int = 0


_timingNamespaces: dict[str, _TimingNamespace] = {}


def _now_ms() -> int:
    return int(_time.time() * 1000)


def resetTimings(namespace: TimingLabel = "main") -> None:
    if not ENABLED:
        return
    _timingNamespaces[namespace] = _TimingNamespace(timings=[], lastTime=_now_ms())


def time(label: str, namespace: TimingLabel = "main") -> None:
    if not ENABLED:
        return
    now = _now_ms()
    if namespace not in _timingNamespaces:
        resetTimings(namespace)
    entry = _timingNamespaces[namespace]
    entry.timings.append((label, now - entry.lastTime))
    entry.lastTime = now


def _printTimingGroup(title: str, timings: list[tuple[str, int]]) -> None:
    # A negative span means the namespace was reset after the mark was taken; pi drops
    # those rather than printing a nonsense duration.
    printable = [(label, ms) for label, ms in timings if ms >= 0]
    if not printable:
        return
    print(f"\n--- {title} ---", file=sys.stderr)
    for label, ms in printable:
        print(f"  {label}: {ms}ms", file=sys.stderr)
    print(f"  TOTAL: {sum(ms for _, ms in printable)}ms", file=sys.stderr)
    print("-" * (len(title) + 8) + "\n", file=sys.stderr)


def printTimings() -> None:
    if not ENABLED:
        return
    for namespace, entry in _timingNamespaces.items():
        _printTimingGroup(f"Startup Timings: {namespace}", entry.timings)


__all__ = ["ENABLED", "printTimings", "resetTimings", "time"]

"""Simple startup timing instrumentation."""

from __future__ import annotations

import os
import sys
import time as _time
from typing import TypedDict


class _TimingEntry(TypedDict):
    label: str
    ms: int


_ENABLED = os.environ.get("MISAKA_TIMING") == "1"
_timings: list[_TimingEntry] = []
_last_time_ms = int(_time.time() * 1000)


def reset_timings() -> None:
    global _last_time_ms
    if not _ENABLED:
        return
    _timings.clear()
    _last_time_ms = int(_time.time() * 1000)


def time(label: str) -> None:
    global _last_time_ms
    if not _ENABLED:
        return
    now_ms = int(_time.time() * 1000)
    _timings.append({"label": label, "ms": now_ms - _last_time_ms})
    _last_time_ms = now_ms


def print_timings() -> None:
    if not _ENABLED or not _timings:
        return
    print("\n--- Startup Timings ---", file=sys.stderr)
    total_ms = 0
    for entry in _timings:
        total_ms += entry["ms"]
        print(f"  {entry['label']}: {entry['ms']}ms", file=sys.stderr)
    print(f"  TOTAL: {total_ms}ms", file=sys.stderr)
    print("------------------------\n", file=sys.stderr)


printTimings = print_timings
resetTimings = reset_timings

__all__ = [
    "resetTimings",
    "time",
    "printTimings",
]

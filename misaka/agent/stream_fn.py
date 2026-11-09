"""Translated from pi's ``packages/agent/src/stream-fn.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.agent.types import StreamFn

_defaultStreamFn: StreamFn | None = None


def setDefaultStreamFn(streamFn: StreamFn | None) -> None:
    """Configure the fallback used by Agent and low-level loops when callers omit streamFn.

    Hosts that provide a default model runtime can install its stream function here
    without making pi-agent-core depend on a provider catalog or compatibility layer.
    """
    global _defaultStreamFn
    _defaultStreamFn = streamFn


def getDefaultStreamFn() -> StreamFn:
    if not _defaultStreamFn:
        raise RuntimeError("No default stream function configured. Pass streamFn explicitly or call setDefaultStreamFn().")
    return _defaultStreamFn


__all__ = ["getDefaultStreamFn", "setDefaultStreamFn"]

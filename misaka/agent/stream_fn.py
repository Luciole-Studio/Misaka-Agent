"""Process-wide fallback stream function for Agent and the low-level loops."""

from __future__ import annotations

from misaka.agent.types import StreamFn
from misaka.ai.stream import stream_simple

_default_stream_fn: StreamFn | None = stream_simple


def set_default_stream_fn(stream_fn: StreamFn | None) -> None:
    global _default_stream_fn
    _default_stream_fn = stream_fn


def get_default_stream_fn() -> StreamFn:
    if _default_stream_fn is None:
        raise RuntimeError(
            "No default stream function configured. Pass streamFn explicitly or call "
            "set_default_stream_fn()."
        )
    return _default_stream_fn


setDefaultStreamFn = set_default_stream_fn
getDefaultStreamFn = get_default_stream_fn

__all__ = [
    "getDefaultStreamFn",
    "get_default_stream_fn",
    "setDefaultStreamFn",
    "set_default_stream_fn",
]

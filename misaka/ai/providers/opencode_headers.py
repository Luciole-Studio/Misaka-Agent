"""OpenCode's per-conversation routing header, translated from pi's
``providers/opencode-headers.ts``."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

OPENCODE_SESSION_HEADER = "x-opencode-session"


def _has_header(headers: Mapping[str, Any] | None, name: str) -> bool:
    expected = name.lower()
    return any(key.lower() == expected for key in (headers or {}))


def _with_session_header(options: Any) -> Any:
    session_id = _read(options, "sessionId")
    if not session_id or _has_header(_read(options, "headers"), OPENCODE_SESSION_HEADER):
        return options
    headers = {**(_read(options, "headers") or {}), OPENCODE_SESSION_HEADER: session_id}
    if isinstance(options, Mapping):
        return {**options, "headers": headers}
    # Options arrive as pydantic models from the runtime and as plain mappings from
    # callers; a model is copied the way `{...options}` copies an object.
    if hasattr(options, "model_copy"):
        return options.model_copy(update={"headers": headers})
    return {**vars(options), "headers": headers}


def _read(options: Any, name: str) -> Any:
    if options is None:
        return None
    if isinstance(options, Mapping):
        return options.get(name)
    return getattr(options, name, None)


class _WithOpenCodeSessionHeader:
    def __init__(self, streams: Any) -> None:
        self._streams = streams

    def stream(self, model, context, options=None):
        return self._streams.stream(model, context, _with_session_header(options))

    def streamSimple(self, model, context, options=None):
        return self._streams.streamSimple(model, context, _with_session_header(options))


def with_opencode_session_header(streams: Any) -> _WithOpenCodeSessionHeader:
    """Adds OpenCode's required per-conversation routing header before API dispatch."""
    return _WithOpenCodeSessionHeader(streams)


withOpenCodeSessionHeader = with_opencode_session_header

__all__ = ["OPENCODE_SESSION_HEADER", "withOpenCodeSessionHeader", "with_opencode_session_header"]

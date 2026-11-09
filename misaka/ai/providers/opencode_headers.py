"""Translated from pi's ``packages/ai/src/providers/opencode-headers.ts`` (v1.1.0).

MISAKA fork: stream options arrive as pydantic models from the runtime and as plain mappings from callers; each is
copied the way ``{...options}`` copies an object, and the wrapped ``ProviderStreams`` (a ``SimpleNamespace``) is spread
by its attributes.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from misaka.ai.types import ProviderHeaders, ProviderStreams
from misaka.utils.values import read_field

_OPENCODE_SESSION_HEADER = "x-opencode-session"


def _hasHeader(headers: ProviderHeaders | None, name: str) -> bool:
    expected = name.lower()
    return any(key.lower() == expected for key in (headers or {}))


def _withSessionHeader(options: Any) -> Any:
    if not read_field(options, "sessionId") or _hasHeader(read_field(options, "headers"), _OPENCODE_SESSION_HEADER):
        return options
    headers = {**(read_field(options, "headers") or {}), _OPENCODE_SESSION_HEADER: read_field(options, "sessionId")}
    if isinstance(options, Mapping):
        return {**options, "headers": headers}
    if hasattr(options, "model_copy"):
        return options.model_copy(update={"headers": headers})
    return {**vars(options), "headers": headers}


def withOpenCodeSessionHeader(streams: ProviderStreams) -> ProviderStreams:
    """Adds OpenCode's required per-conversation routing header before API dispatch."""
    return SimpleNamespace(  # type: ignore[return-value]
        **{
            **vars(streams),
            "stream": lambda model, context, options=None: streams.stream(model, context, _withSessionHeader(options)),
            "streamSimple": lambda model, context, options=None: streams.streamSimple(
                model, context, _withSessionHeader(options)
            ),
        }
    )


__all__ = ["withOpenCodeSessionHeader"]

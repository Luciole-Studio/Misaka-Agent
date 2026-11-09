"""The execution environment of a tool call.

Translated from pi's ``packages/durable/src/tools/env.ts``.
"""

from __future__ import annotations

from typing import Any


def requireEnv(api: Any) -> Any:
    """The call's execution environment; a tool without one fails with an ordinary error result."""
    if api.env is None:
        raise RuntimeError("No execution environment is configured")
    return api.env


__all__ = ["requireEnv"]

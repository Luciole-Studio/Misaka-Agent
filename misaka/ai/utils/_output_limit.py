"""MISAKA fork: how research callers report a reply that spent the whole output cap.

pi's own recovery covers a reply cut *below* the output cap (``utils.overflow.isRecoverableLength``); a reply that spent
the whole cap -- adaptive thinking at effort max can -- is not retried by pi and used to reach research callers as
"request length", which Last Order read as "the request was too long" and shrank her plan. These name the cause and the
fact that no tool call happened, so the driver can nudge one more turn and the model knows what to do.
"""

from __future__ import annotations

from typing import Any

OUTPUT_LIMIT_MARK = "output token limit"


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def output_limit_error(message: Any) -> str:
    """What a ``length`` stop means to the caller, in words the model can act on."""
    usage = _field(message, "usage")
    output = _field(usage, "output", 0) or 0
    return (
        f"the reply hit the model's {OUTPUT_LIMIT_MARK} ({output} tokens) before it finished; "
        "no tool call was made"
    )


def hit_output_limit(error: Any) -> bool:
    return bool(error) and OUTPUT_LIMIT_MARK in str(error)


__all__ = ["OUTPUT_LIMIT_MARK", "hit_output_limit", "output_limit_error"]

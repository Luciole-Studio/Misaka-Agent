"""One unmodified upstream command dispatcher for both live sessions and the CLI."""
from __future__ import annotations

from . import context_engine, llm


def command(raw: str, ctx=None) -> str:
    from ..vendor.command import handle_lcm_command

    with context_engine.operation(ctx), llm.runtime(ctx):
        built = context_engine.bound_engine(ctx) if ctx is not None else context_engine.engine()
        try:
            return handle_lcm_command(raw, built)
        finally:
            if ctx is None:
                # The empty-key engine is CLI-owned, never a conversation's runtime.
                context_engine.close()

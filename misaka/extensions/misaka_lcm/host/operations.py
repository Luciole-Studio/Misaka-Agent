"""One unmodified upstream command dispatcher for both live sessions and the CLI."""
from __future__ import annotations

from . import context_engine, llm, storage


def command(raw: str, ctx=None) -> str:
    from ..vendor.command import handle_lcm_command

    with context_engine.operation(ctx), llm.runtime(ctx):
        built = context_engine.bound_engine(ctx) if ctx is not None else context_engine.engine()
        try:
            result = handle_lcm_command(raw, built)
            if raw.strip() == "status":
                result = result.replace("LCM status\n", "MISAKA LCM status\n"
                                        f"project: {storage.project(ctx)}\n"
                                        "storage_lifetime: project-runtime\n", 1)
                result = result.replace("no active Hermes session", "no active MISAKA session")
            return result
        finally:
            if ctx is None:
                # The empty-key engine is CLI-owned, never a conversation's runtime.
                context_engine.close()
                context_engine.release_project()

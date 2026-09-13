"""Subagent runtime and the upstream child-management tool boundary."""

import os

from misaka.core.platform.vocabulary import MANAGEMENT_TOOLS

ROLES = {"sisters"}


def disallowed_management_tools(kind, *, is_async=None):
    """CCB 77a7934e: ordinary children have no Agent/TaskOutput/TaskStop.

    USER_TYPE=ant permits Agent for synchronous children only. MISAKA's Board
    Sister is a root (kind=card), even though she shares the child-process runner.
    SendMessage belongs to the shared messaging layer, not this filter.
    """
    if kind != "child":
        return ()
    if is_async is None:
        is_async = os.environ.get("MISAKA_SUBAGENT_BACKGROUND", "0") != "0"
    can_spawn = (os.environ.get("USER_TYPE") == "ant" and not is_async) or os.environ.get("MISAKA_FORK_CHILD") == "1"
    return tuple(name for name in MANAGEMENT_TOOLS
                 if name != "SendMessage" and not (name == "Agent" and can_spawn))


def part(spec):
    # Built late by the assembly, after the worker's budget/identity environment is in place.
    from .extension import part_for
    return part_for(spec.profile_dir, spec.role, spec.workspace,
                    mcp_role=spec.mcp_role or spec.role, tool_ceiling=spec.tool_ceiling,
                    disallowed_tools=disallowed_management_tools(spec.kind))

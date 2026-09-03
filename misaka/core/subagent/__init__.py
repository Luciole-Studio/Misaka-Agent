"""Runtime modules for the bundled recursive ``subagent`` extension."""

ROLES = {"sisters"}


def part(spec):
    # Built late by the assembly, after the worker's budget/identity environment is in place.
    from .extension import part_for
    return part_for(spec.profile_dir, spec.role, spec.workspace,
                    mcp_role=spec.mcp_role or spec.role, tool_ceiling=spec.tool_ceiling)

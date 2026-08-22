"""Runtime modules for the bundled recursive ``subagent`` extension."""

def activate(spec):
    # Bind late, after the worker's budget/identity environment is in place.
    def bound(harn):
        from .extension import bind
        bind(spec.profile_dir, spec.role, spec.workspace,
             mcp_role=spec.mcp_role or spec.role, tool_ceiling=spec.tool_ceiling)(harn)
    return bound

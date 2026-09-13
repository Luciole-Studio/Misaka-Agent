"""CCB 77a7934e filterToolsForAgent, with explicit MISAKA tool-name adapters.

Source: AgentTool/agentToolUtils.ts:70-115 and src/constants/tools.ts:44-105.
Keep the MCP, plan, global-deny, custom-deny, async, teammate branch order.
Sisters are MISAKA roots, not CCB in-process teammates.
"""

from __future__ import annotations

import os

ALL_AGENT_DISALLOWED_TOOLS = frozenset({
    "TaskOutput", "ExitPlanMode", "EnterPlanMode", "AskUserQuestion", "TaskStop",
    "LocalMemoryRecall", "VaultHttpFetch",
})
ASYNC_AGENT_ALLOWED_TOOLS = frozenset({
    "Read", "WebSearch", "TodoWrite", "Grep", "WebFetch", "Glob", "Bash", "PowerShell",
    "Edit", "Write", "NotebookEdit", "Skill", "StructuredOutput", "SearchExtraTools", "ExecuteExtraTool",
    "EnterWorktree", "ExitWorktree",
})
IN_PROCESS_TEAMMATE_ALLOWED_TOOLS = frozenset({
    "TaskCreate", "TaskGet", "TaskList", "TaskUpdate", "SendMessage",
    "CronCreate", "CronDelete", "CronList",
})

# Native MISAKA equivalents. Unknown extensions are NOT automatically trusted as
# an async-safe tool. These aliases do not bypass role/skill/project restrictions.
_ALIASES = {
    "read": "Read", "edit": "Edit", "write": "Write", "bash": "Bash",
    "powershell": "PowerShell", "grep": "Grep", "find": "Glob", "glob": "Glob", "ls": "Glob",
    "web_search": "WebSearch", "x_search": "WebSearch", "web_extract": "WebFetch",
    "skills_list": "Skill", "skill_view": "Skill", "skill_manage": "Skill",
    "doc_list": "Glob", "doc_outline": "Read", "doc_read": "Read", "doc_page_image": "Read",
    "doc_find": "Grep", "doc_verify": "Grep", "doc_add": "Write",
    "browser_exec": "ExecuteExtraTool", "ask_user": "AskUserQuestion",
}
_CANONICAL = {name.casefold(): name for name in (
    ALL_AGENT_DISALLOWED_TOOLS | ASYNC_AGENT_ALLOWED_TOOLS | IN_PROCESS_TEAMMATE_ALLOWED_TOOLS | {"Agent", "Workflow"}
)}


def canonical_tool_name(name: str) -> str:
    return _ALIASES.get(name.casefold(), _CANONICAL.get(name.casefold(), name))


def tool_allowed_for_agent(
    name: str, *, is_builtin: bool = False, is_async: bool = False,
    permission_mode: str | None = None, user_type: str | None = None,
    workflow_scripts: bool = False, agent_swarms: bool = False,
    in_process_teammate: bool = False,
) -> bool:
    if name.startswith("mcp__"):
        return True
    name = canonical_tool_name(name)
    if name == "ExitPlanMode" and permission_mode == "plan":
        return True
    if name in ALL_AGENT_DISALLOWED_TOOLS:
        return False
    if name == "Agent" and (os.environ.get("USER_TYPE") if user_type is None else user_type) != "ant":
        return False
    if workflow_scripts and name == "Workflow":
        return False
    # CUSTOM_AGENT_DISALLOWED_TOOLS is an exact copy of the global set at this pin.
    if is_async and name not in ASYNC_AGENT_ALLOWED_TOOLS:
        return agent_swarms and in_process_teammate and (name == "Agent" or name in IN_PROCESS_TEAMMATE_ALLOWED_TOOLS)
    return True

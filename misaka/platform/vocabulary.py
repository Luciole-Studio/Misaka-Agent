"""The names of the sub-agent management tools, in one place.

This tuple had four copies -- ``network/worker.py``, ``subagent/runtime.py``,
``subagent/child.py`` and ``subagent/policy.py`` -- plus a fifth restatement in
Last Order's charter, which is where it drifted: the charter told her she had no
``SendMessage`` while the messaging layer was registering one for every role.
Four constants and a paragraph cannot be kept in step by discipline, so they are
not kept in step; they read from here.

``SendMessage`` is the odd one out and the reason the drift was possible: it
belongs to the unified messaging layer (``network/messages.py``) and exists in
every session, sub-agents or not. It appears here because the sub-agent path
*reuses* it to continue a child, not because it is a sub-agent tool.
"""

from __future__ import annotations

# The child-management vocabulary, in the casing the tools register under.
MANAGEMENT_TOOLS = ("Agent", "TaskOutput", "SendMessage", "TaskStop")

# The same set for the permission and hook paths, which compare lowered names.
MANAGEMENT_TOOL_NAMES = frozenset(name.casefold() for name in MANAGEMENT_TOOLS)

# Spawning a descendant is work; reading, steering or stopping one is paperwork.
# ``platform.session``'s idle test needs the second group alone, and deriving it
# here keeps the reason for the split beside the set it splits.
BOOKKEEPING_MANAGEMENT_TOOLS = tuple(
    name for name in MANAGEMENT_TOOLS if name != "Agent"
)

__all__ = [
    "BOOKKEEPING_MANAGEMENT_TOOLS",
    "MANAGEMENT_TOOLS",
    "MANAGEMENT_TOOL_NAMES",
]

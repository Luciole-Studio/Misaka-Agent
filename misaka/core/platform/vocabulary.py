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

MANAGEMENT_TOOLS = ("Agent", "TaskOutput", "SendMessage", "TaskStop")

MANAGEMENT_TOOL_NAMES = frozenset(name.casefold() for name in MANAGEMENT_TOOLS)

NO_PROGRESS_VOCABULARY = {
    "management": MANAGEMENT_TOOLS,
    "create": ("Agent",),
    "assign": ("Agent", "SendMessage"),
    "collect": ("Agent", "TaskOutput"),
}

DRIVER_OWNED_TOOLS = frozenset({
    "misaka_card", "misaka_dispatch", "misaka_sister", "misaka_card_link",
    "misaka_card_request_review", "misaka_card_review", "misaka_card_unblock",
    "misaka_card_requeue", "misaka_card_delete",
})

__all__ = [
    "DRIVER_OWNED_TOOLS",
    "MANAGEMENT_TOOLS",
    "MANAGEMENT_TOOL_NAMES",
    "NO_PROGRESS_VOCABULARY",
]

"""web_search: the model's own way onto the network, for every role.

Until this existed the only search a session could reach was the one Anthropic runs
server-side, so every other provider -- and every Sister pinned to one -- researched
blind. The tool is registered only when a key is configured, so a session without one
sees no dead tool it can call and be refused by.
"""
from __future__ import annotations

from misaka.core.tools.web_search import create_web_search_tool_definition

SESSION_KINDS = {"foreground", "dm", "card", "child", "beast", "bare"}


def activate(spec):
    # The key is read here, once: no key configured means the extension does not exist
    # for this session, rather than a tool that always answers "not configured".
    definition = create_web_search_tool_definition()
    if definition is None:
        return None

    def register(harn):
        harn.registerTool(definition)

    return register

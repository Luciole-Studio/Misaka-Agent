"""Interactive AskUserQuestion extension."""

from .extension import ASK_USER_QUESTION_TOOL_NAME, register

__all__ = ["ASK_USER_QUESTION_TOOL_NAME", "register"]

import os

SESSION_KINDS = {"foreground", "card"}


def activate(spec):
    if spec.kind == "card" and (not os.environ.get("MISAKA_NET_PANE") or os.environ.get("MISAKA_SUBAGENT_ID")):
        return None
    return register

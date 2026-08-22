"""Interactive AskUserQuestion extension."""

from .extension import ASK_USER_QUESTION_TOOL_NAME, register

__all__ = ["ASK_USER_QUESTION_TOOL_NAME", "register"]

SESSION_KINDS = {"foreground"}  # a structured question needs a person at the keyboard


def activate(spec):
    return register

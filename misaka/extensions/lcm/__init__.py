"""LCM context-engine extension."""

SESSION_KINDS = {"foreground", "dm", "card", "beast"}


def activate(spec):
    from .extension import register
    return register

"""Mixture-of-Agents provider and command extension."""

from misaka.extensions import KINDS

SESSION_KINDS = KINDS  # a configured ``provider=moa`` must resolve in every session, bare ones included


def activate(spec):
    from .extension import register_command, register_provider
    if spec.kind not in ("foreground", "dm"):
        return register_provider

    def register(harn):
        register_provider(harn)
        register_command(harn)
    return register

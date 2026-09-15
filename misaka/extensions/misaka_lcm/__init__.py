"""MISAKA LCM: the project-scoped fork of hermes-lcm.

The upstream compression/retrieval implementation lives in vendor/ and native/;
MISAKA's project ownership, disposable storage and session provenance live in
host/. Upstream notices and pins remain intact. Namespaced imports at existing
host seams are the only relocation changes in the upstream Python sources.
"""
EXTENSION_NAME = "misaka_lcm"

SESSION_KINDS = {"foreground", "dm", "card", "beast", "child", "bare"}


def activate(spec):
    import os

    from .host.extension import register
    workspace = (os.environ.get("MISAKA_LCM_PROJECT") if os.environ.get("MISAKA_SUBAGENT_ID") else None) or spec.workspace

    def register_for_session(harn):
        return register(harn, kind=spec.kind, workspace=workspace)

    return register_for_session

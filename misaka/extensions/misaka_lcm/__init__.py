"""MISAKA LCM: the project-scoped fork of hermes-lcm.

The upstream compression/retrieval implementation lives in vendor/ and native/;
MISAKA's project ownership, disposable storage and session provenance live in
host/. Upstream notices and pins remain intact. Namespaced imports at existing
host seams are the only relocation changes in the upstream Python sources.
"""
EXTENSION_NAME = "misaka_lcm"
# `builtin:misaka_lcm`, like every bundled extension: hidden from the startup list, and disabled with
# `-builtin:misaka_lcm` in the `extensions` setting.
BUILTIN = True

SESSION_KINDS = {"foreground", "dm", "card", "beast", "child", "bare"}

COMMAND = "lcm"


def command(argv):
    """Inspect LCM and run explicit history/backfill operators.

    ``misaka lcm ...`` runs before any session exists or the project layout is created: a dry run
    or a read-only operator must not bootstrap directories or open an engine first."""
    from .host.operators import main
    return main(argv)


def identity(spec) -> str:
    """Who this session is, as the rows it stores will say it: the address its mail goes by
    (``last-order``, or a Sister's name), with ``@<card>`` when it works a card and
    ``/<agent id>`` when it is a sub-agent -- the id its parent's ``Agent`` call returned, since a
    sub-agent shares its parent's address. Upstream keeps this in each row's ``source`` -- its
    "which platform" -- and here "which platform" is "which agent", so ``lcm_grep(source=...)``
    narrows a project-wide search to one of them."""
    import os

    from misaka.core.wiring import sender_address

    if getattr(spec, "sender", None) is None and not getattr(spec, "role", None):
        return "misaka"  # a spec with no address at all (a bare fixture): the engine's own name
    address = sender_address(spec)
    if task := getattr(spec, "task_id", None):
        return f"{address}@{task}"
    agent = os.environ.get("MISAKA_SUBAGENT_ID") if getattr(spec, "kind", None) == "child" else None
    return f"{address}/{agent}" if agent else address


def activate(spec):
    import os

    from .host import storage
    from .host.extension import register
    # A subagent joins its parent session's project store, which the parent set on subagent_start.
    workspace = (os.environ.get(storage.PROJECT_ENV) if os.environ.get("MISAKA_SUBAGENT_ID") else None) or spec.workspace
    source = identity(spec)

    def register_for_session(harn):
        return register(harn, kind=spec.kind, workspace=workspace, source=source)

    return register_for_session

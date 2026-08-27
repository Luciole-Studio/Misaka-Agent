"""The misaka side of the hermes-lcm port: every adapter that upstream must not contain.

`../vendor/` is upstream, verbatim and resynced by diff. This package is ours, written to
repository standards (ruff and deadcheck check it), and it is where every misaka-shaped
concern lives:

    switch.py          which implementation serves this install, and does the db fit
    context_engine.py  upstream's ContextEngine protocol -> misaka's compaction seam
    llm.py             upstream's auxiliary-model calls -> misaka's provider stack
    config_bridge.py   MISAKA_LCM_* (the existing user contract) -> upstream LCM_*
    ingest.py          misaka message/session shapes -> upstream ingest
    (tools.py)         upstream tool schemas -> misaka ToolDefinition: not yet ported
    migrate.py         the existing mini-implementation database -> upstream schema

Keeping the two apart is what makes "port" mean something: an upstream release changes
only `vendor/`, and a misaka refactor changes only `host/`.
"""

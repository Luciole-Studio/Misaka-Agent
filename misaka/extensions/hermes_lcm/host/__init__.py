"""The misaka side of the hermes-lcm port: every adapter that upstream must not contain.

`../vendor/` is upstream, verbatim and resynced by diff. This package is ours, written to
repository standards (ruff and deadcheck check it), and it is where every misaka-shaped
concern lives:

    context_engine.py     upstream's ContextEngine protocol -> misaka's compaction seam
    context_engine_abc.py the protocol itself, as misaka's compaction seam names it
    llm.py                upstream's auxiliary-model calls -> misaka's provider stack
    config_bridge.py      MISAKA_LCM_* (the existing user contract) -> upstream LCM_*
    ingest.py             misaka message/session shapes -> upstream ingest
    tools.py              upstream tool schemas -> misaka ToolDefinition, one dispatch
    fence.py              misaka's untrusted-data fence, put back on what LCM hands over
    rollups.py            temporal rollups: the build nudge misaka's session shape needs
    externalize.py        large-output refs in the live prompt, and a backfill for old rows
    preanswer.py          V4 pre-answer evidence: one bounded cited brief before the answer
    embed.py              upstream's own `/lcm embed`, forwarded whole
    assertions.py         upstream's own `/lcm assertions rebuild`, forwarded whole
    operations.py         the `/lcm` ops: status/doctor/backup/rotate/preset
    slash.py              the `/lcm` command: the operator surface, as pi's llama exposes `/llama`
    extension.py          the event subscriptions all of the above are reached through

Keeping the two apart is what makes "port" mean something: an upstream release changes
only `vendor/`, and a misaka refactor changes only `host/`.
"""

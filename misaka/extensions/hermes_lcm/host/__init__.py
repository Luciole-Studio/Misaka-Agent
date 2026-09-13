"""The misaka side of the hermes-lcm port: every adapter that upstream must not contain.

`../vendor/` and `../native/` are pinned upstream sources with registered host seams. This package is ours, written to
repository standards (ruff and deadcheck check it), and it is where every misaka-shaped
concern lives:

    context_engine.py     upstream's ContextEngine protocol -> misaka's compaction seam
    context_engine_abc.py plugin import aliases for the real ABC and lazy native compressor
    native.py             native compressor's model, auxiliary, clock and replay-fence boundaries
    llm.py                upstream's auxiliary-model calls -> misaka's provider stack
    profiles.py           lazy pinned provider wire profiles, no Hermes user plugin discovery
    routing.py            native catalogue/auth scopes for the pinned fallback ladder
    carry.py              explicit native continuity receipts and original rollover
    pool.py               original selection/cooldown over atomic native named credentials
    nous.py               native Nous OAuth, entitlement and token healing
    catalog.py            endpoint-scoped original reasoning/recommendation metadata
    acp.py                original stdio ACP under native cancellation/process ownership
    acp_environment.py    ACP child environment and permission boundary
    auxiliary.py          sync-call recovery policy -> native async requests and cancellation
    execution.py          shared off-loop ownership, cancellation and publish checkpoints
    config_bridge.py      native paths/settings and original LCM_* algorithm configuration
    ingest.py             misaka message/session shapes -> upstream ingest
    tools.py              upstream tool schemas -> misaka ToolDefinition, one dispatch
    fence.py              misaka's untrusted-data fence, put back on what LCM hands over
    rollups.py            temporal rollups: the build nudge misaka's session shape needs
    operators.py          original import, externalization and trajectory operator CLIs
    preanswer.py          V4 pre-answer evidence: one bounded cited brief before the answer
    operations.py         the original `/lcm` dispatcher and command-owned lifecycle
    slash.py              the `/lcm` command: the operator surface, as pi's llama exposes `/llama`
    extension.py          the event subscriptions all of the above are reached through

Keep the policies and host boundaries separate: source pins/selection manifests cover
`vendor/` and `native/`; a MISAKA refactor belongs in `host/`.
"""

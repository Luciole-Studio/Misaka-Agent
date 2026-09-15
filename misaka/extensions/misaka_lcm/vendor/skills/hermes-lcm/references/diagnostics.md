# Diagnostics

Use read-only product tools before changing configuration or running an apply path.

## Fast path

1. `hermes plugins`: confirm `hermes-lcm` is enabled and the selected context engine is `lcm`.
2. Send one normal message if the session has not been bound since restart.
3. `lcm_status`: inspect runtime identity, database path, context pressure, summary/store counts, filters, and lifecycle state.
4. `lcm_inspect`: inspect current-session lineage, frontiers, fresh tail, externalized-ref readability, and skip/no-op reasons without retrieving content.
5. `lcm_doctor`: run database, FTS, lifecycle, configuration, and context-pressure diagnostics.

If optional slash commands are enabled, `/lcm status` and `/lcm doctor` expose the corresponding operator views.

## Safe mutation order

For cleanup, repair, source normalization, or rotate:

1. run the read-only preview;
2. inspect exact candidates and paths;
3. create/confirm a backup;
4. obtain user authorization for the specific apply operation;
5. run one bounded apply and verify integrity afterward.

Cleanup apply is separately feature-gated. Never infer permission to enable it from a diagnosis request.

## Common states

- Unbound status after restart: send a normal message, then check again.
- Database exists but stays empty: verify plugin enablement, `context.engine`, profile, database path, and ignore/stateless patterns.
- Weak exact recall: verify source rows exist, query construction/scope is correct, summary health is sound, and embedding coverage/provenance matches the requested mode.
- Conflicting summary and raw evidence: prefer the newer exact raw evidence and inspect lineage.
- Path B/context-engine schema log: expected on hosts where plugin-registry handlers do not receive active messages; context-engine schemas and dispatch remain the healthy route.

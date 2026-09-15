# Architecture

Hermes-LCM keeps raw messages in profile-local SQLite and builds a summary DAG to keep active context bounded.

## Core flow

1. The active context engine ingests messages into `lcm.db`.
2. Older eligible messages are compacted into leaf summaries.
3. Summary nodes can be condensed to higher DAG depths.
4. Context assembly combines selected summaries with a protected fresh raw tail.
5. Recall tools recover exact source rows or bounded expanded context when summaries are insufficient.

Raw messages are source truth. Summary nodes, embeddings, temporal rollups, query views, and assertions are derived and rebuildable layers with explicit provenance.

## Scope model

- Current-session DAG operations use the active engine/session binding.
- `lcm_recall` searches all conversations already stored in the local LCM database.
- `lcm_load_session` enumerates a known LCM session.
- Hermes `session_search` covers host-tracked history outside `lcm.db`.

Do not silently treat those stores or scopes as interchangeable.

## V4 derived state

The V4 branch adds same-database, default-off assertion/query-view state and provider-neutral reasoning/evidence components. They remain subordinate to raw messages:

- assertions require exact message IDs, spans, quotes, and lifecycle provenance;
- query views cache evidence dependencies and coverage, never final prose;
- computation validates exact operands and emits an immutable trace;
- evidence packs return bounded evidence/computation, not an authoritative answer.

Unknown source/event time and unresolved conflict are valid states. Derived data must fail closed rather than manufacture certainty.

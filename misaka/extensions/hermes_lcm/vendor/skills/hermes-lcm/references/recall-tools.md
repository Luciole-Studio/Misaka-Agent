# Recall tools

Use recall tools when the answer depends on historical evidence that may have been compacted or lives in another LCM session.

## Current compacted conversation

### `lcm_grep`

Use for discovery across current-session raw messages and summary nodes.

- `query` is FTS5 text by default; it is not a regex.
- Prefer 1-3 distinctive terms or one quoted phrase because FTS5 combines extra terms with AND.
- Keep `sort='recency'` for recent events, use `sort='relevance'` for the strongest older match, and use `sort='hybrid'` when both matter.
- `mode='semantic'` or `'hybrid'` is useful when embeddings are configured; degraded coverage is reported.
- Broader `session_scope='all'|'session'` is explicit, bounded, raw-message-only archive recovery inside `lcm.db`.
- Exact role/time/source/conversation filters apply before limiting where supported.

Do not treat a short search snippet as sufficient evidence for a detail-heavy answer.

### `lcm_describe`

Use for inexpensive inspection of a known current-session summary node or externalized payload reference. With no handle it returns a current-session DAG overview. It is a planning step, not broad discovery.

### `lcm_expand_query`

Use when current-session compacted material must be expanded and synthesized into a precise bounded answer.

- Always provide `prompt`.
- Provide either a small `query` or explicit `node_ids` when known.
- `query` follows the same narrow FTS construction rules as `lcm_grep`.
- The expansion path is model-backed and bounded by answer/context token limits.

Recommended current-session escalation:

1. `lcm_grep` to locate relevant material.
2. `lcm_describe` when a known handle needs inspection.
3. `lcm_expand_query` when exact detail was compressed away.

### `lcm_expand`

Use as low-level drill-down after a known handle:

- `node_id` expands a current-session summary with source pagination;
- `store_id` recovers one raw message and works across LCM sessions;
- `externalized_ref` opens a current-session payload with content pagination.

Do not use it as broad first-step discovery.

Set `include_exact_ref=true` with `store_id` when the recovered slice will be
cited or passed to `lcm_evidence_pack`/`lcm_compile_evidence`/`lcm_compute`. The default remains off
for byte compatibility.

## Cross-conversation memory

### `lcm_recall`

Use for semantic discovery across all conversations stored in the local LCM database. It fuses raw full-text, summary-vector, and verbatim-chunk arms when available and degrades honestly when embeddings are unavailable.

- `scope_bias` and recency are ranking boosts, never hard filters.
- `include` selects all, summary, or verbatim hits.
- `detail='snippets'` is the byte-compatible default.
- `detail='answer_ready'` adds bounded per-session diversity and exact-ref hydration.
- Follow each result's `expand_hint`: current/verbatim hits normally use `lcm_expand`; cross-session summaries normally use `lcm_load_session`.

### `lcm_load_session`

Use after a session ID is known. It enumerates raw rows in chronological `store_id` pages; it is not search.

- Continue with `after_store_id` from `next_cursor`.
- Increase `max_content_chars` only within its hard bound.
- Recover a truncated individual row with `lcm_expand(store_id=..., content_offset=...)`.
- Set `include_exact_ref=true` when transcript rows will feed exact evidence or computation.

Use Hermes `session_search` for host-tracked sessions that are not present in `lcm.db`.

## Time-bounded recall

### `lcm_recent`

Use for `today`, `yesterday`, `Nd`, `week`, `month`, `date:YYYY-MM-DD`, or `last Nh`. Periods are UTC. Ready rollups are used only when they cover the entire requested window; otherwise the tool falls back to bounded summaries and reports provenance.

For exact raw-message windows, use `lcm_grep` with explicit `time_from`/`time_to` instead.

## Exact evidence and computation

### `lcm_compile_evidence`

Use when a question needs multiple named facets, exact operands, conflict handling, or latest-state reasoning. Supply one bounded semantic proposal over already retrieved exact refs. When deterministic parsing yields only the catch-all `answer` facet, the proposal may provide up to 12 unique generic `requested_facets`; otherwise those facets can only extend, never erase, deterministic requirements. The proposal is only a hint: every requested facet must be closed by product-validated exact evidence, and product code revalidates every ref, unique quote span, entity, date, value, unit, distinct key, role, and source. Use `answer_sufficient` for a specifically answered open-ended question; require `finite_coverage` or `computation_sufficient` for exhaustive counts, lists, or arithmetic. On `partial`, `conflicted`, or `unknown`, preserve uncertainty and the ordinary answer path.

### `lcm_evidence_pack`

Use after bounded baseline refs exist. It validates/hydrates exact refs, keeps occurrence and observation time distinct, deduplicates, and may return a canonical computation trace. It returns evidence, not final prose. Open-cardinality stays partial unless coverage is product-verifiable.

### `lcm_compute`

Use only over exact cited evidence validated by the compiler for supported date intervals/filters, distinct counts, compatible-unit sums, directed or absolute differences, ordering, and latest-state selection. Invalid spans, mixed units, ambiguity, or unsupported closure fail closed.

## Default-off advanced paths

- `lcm_query_state` queries the same-database assertion sidecar when that feature is enabled.
- `lcm_retrieve` is the default-off bounded adaptive controller. It is not required for ordinary recall and must not replace the stable workflow above without measured benefit.

## Operator tools

`lcm_status`, `lcm_inspect`, and `lcm_doctor` report health and metadata. They do not replace content retrieval.

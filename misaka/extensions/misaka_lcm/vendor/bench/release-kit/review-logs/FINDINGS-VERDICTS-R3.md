# Round 3 findings verdicts

Base: `fork/bench/w3b-on-wave1` at `4bd8401d96fb00fbcf37cc0ad0852a4ec7e0ea12`.

1. `query_view_store.py` query-family discovery — **CONFIRMED-FIXED**. The verifier now discovers the full gated `lcm_query%` prefix. The `lcm_querycache` regression classifies the database as genuinely newer and refuses downgrade.
2. `tools.py` reference-strict delta after response-cap eviction — **CONFIRMED-FIXED**. Delta refs and progress fields are rebuilt from surviving delivered hits after whole-hit eviction. The regression asserts delivered refs equal delta refs and omitted refs remain unseen. **V1-DELIVERY-AFFECTING: cap-eviction path only.**
3. `trajectory_store.py` chunked-state spend progress — **CONFIRMED-FIXED**. Every successful chunk request emits cumulative progress before the next request. The low-cap regression stops after one chunk and preserves its provider-call and billed-token spend in the callback ledger.
4. `tools.py` conditional `rows` binding — **CONFIRMED-FIXED**. `rows` is always bound to the batch result or `{}`. The related shallow-copy nit is also aligned by replacing `read_store._write_lock`.
5. `vector_store.py` deadline clock seam — **CONFIRMED-FIXED**. `_monotonic` is module-local and used by deadline/budget paths; affected tests patch the seam. The summary mid-prescreen test is no longer coupled to process-wide time or an exact call sequence.
6. Chunk-side mid-prescreen deadline coverage — **CONFIRMED-FIXED**. The chunk test now expires after the first meaningful prescreen batch and asserts `coverage="bounded"` with `scanned == 1`; `bounded_scan_rows=1` now controls that exercised batch.

Validation:

- Exact regressions: 7 passed.
- Touched-area modules: 144 passed.
- Clock-seam delta: 2 passed.
- Full CI replica: 2689 passed, 35 failed, 1 skipped, 12 xfailed.
- Baseline comparison: 35 actual failure names exactly match 35 expected; zero new and zero missing.
- `git diff --check`: clean.

No commit or push performed.

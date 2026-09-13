# DELTA REVIEW — PR #169 fix commits only (review ONLY, no modifications)

Prior full review (sol·max) returned APPROVE-WITH-FIXES with 6 findings; the author fixed all six
(commits c13f4a2, 902fc37, c6fb1da, 4de8727, 530bd98+e4577f1, 5b58154 — one per finding, regression test each,
built from your repros). Scope: `git diff f960d9f..e4577f1` (the fixes only). The PR comment maps commits to
findings. Verify:

1. Each finding actually closed by its commit (re-run your original repro logic mentally or via in-memory
   SQLite probes; e.g. requires_like_fallback("art-related") now False; NFD naïve matches; "Portland, OR
   hotel" has no operator semantics under the default mode).
2. **The mode split (finding 5's real fix):** `search(..., allow_operators=False)` default with harness
   opt-in — sweep EVERY caller of search/sanitizer entry points: does any raw-user-query path get
   allow_operators=True? Does any deliberate-FTS caller silently lose operators? (The author found
   benchmarking/longmemeval.build_fts_query relies on OR — verify its opt-in is correct and no other caller
   was missed.)
3. **Finding 2's fix:** multi-batch sweeps now stream past the LRU — verify peak memory is truly one batch
   (no reference retention), single-batch paths still use the cache byte-identically, and no double-fetch.
4. The two flagged semantic changes: compound queries now conjunctive-on-index (was disjunctive-on-LIKE) —
   any real caller for whom that is a regression? And the four retargeted LIKE-trigger tests — do they still
   test their original contracts?
5. Any NEW hole opened by the fixes themselves.

Output: VERDICT APPROVE / APPROVE-WITH-FIXES (mandatory list) / REJECT, findings with file:line + severity,
max 8, real defects only. No hardening suggestions.

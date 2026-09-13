# REVIEW PACKET — adversarial correctness review of PR #169 (review ONLY, no code changes)

You are the cross-model reviewer of Claude-authored, release-critical fixes. Your job: find real defects.
Do NOT propose gratuitous hardening, style changes, or extra tests — only defects that would produce wrong
behavior, wrong Phase 1B measurements, or regressions. Verdict + findings with file:line and severity.

## What this PR is
Two fixes on `fix/phase1b-scan-and-query` (diff = `git diff e99f342..HEAD`, read-only git allowed):
1. `b2f228c` (#168): FTS5 query sanitization moved into the product — any non-alphanumeric char outside a
   balanced phrase quote becomes a separator (matching unicode61 tokenizer splitting), applied at both FTS
   entry points (store.py messages, dag.py summaries); LIKE fallback only for CJK/emoji/empty-after-sanitize.
2. `f960d9f` (#167): the 25k-most-recent vector scan window replaced by a batched FULL scan
   (recall_scan_rows = batch size; running top-k across batches; new recall_scan_max_rows (0=unlimited) and
   recall_scan_budget_s (0.0=no stop); degraded/degraded_reason only on actual truncation; lcm_grep path
   byte-identical by default).

## Context you should trust
Motivating measurements: FINDING-F31 in /Volumes/LEXAR/hermes-work/wt-ci-fix/bench/ — recall hit 0.000 at
185k vectors because golds aged out of the 25k window; raw questions returned 100% empty at scale via
FTS5-reject → LIKE full-scan → 8s timeout. Acceptance for the fixes is a zero-LLM instrument re-run
(Phase 1B), NOT this PR's tests — so your review should especially protect measurement correctness.

## Attack surfaces (minimum)
1. **Sanitizer correctness**: characters FTS5 rejects vs what the transform handles; phrase-quote balancing
   edge cases (unbalanced quote, quote-inside-word, empty phrase); can ANY input still reach fts5 MATCH as a
   syntax error or as an unintended OPERATOR (NEAR/AND/OR/NOT semantics, column filters `col:`, `*` prefix)?
   Does term-splitting match unicode61 exactly (unicode categories, diacritics, underscores, digits)? Is the
   LIKE fallback condition right (CJK detection)?
2. **Batched top-k merge**: correctness across batch boundaries (ties, score ordering, duplicate rowids),
   the final k composition vs the old single-window semantics on small corpora (must be byte-identical for
   <= one batch); memory at 185k×384 (peak allocation per batch, no accidental full materialization);
   numpy vs pure-Python path divergence; recall_scan_budget_s early-stop: is the partial result labelled
   degraded EVERY time it truncates, and never otherwise?
3. **Behavior changes the author disclosed**: (a) `_search_like` now scores punctuation-only queries on raw
   text — regression risk for existing callers? (b) the rewritten test
   `test_bounded_chunk_coverage_surfaces_as_degraded` — does the new version still test the same contract?
   (c) new config knob interactions (batch size 1, max_rows < batch, budget tiny).
4. **Caller sweep verification**: the author claims no product caller assumed the 25k cap — verify
   independently (grep/trace knn/knn_chunks/recall_scan_rows consumers).
5. **Phase 1B instrument safety**: anything in these diffs that changes what `degraded_reason` strings say
   (the instrument parses/records them), or that alters latency semantics in a way that would make the
   Phase 1B latency curve incomparable to F31's (e.g., per-batch cache interactions the author flagged).

## Output format
VERDICT: APPROVE / APPROVE-WITH-FIXES (list mandatory) / REJECT (why), then numbered findings:
severity CRITICAL/HIGH/MED/LOW · file:line · the defect · a minimal repro or reasoning. Max 15 findings,
real ones only. End with: the 3 riskiest lines of the diff, quoted.

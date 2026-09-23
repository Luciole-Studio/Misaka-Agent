# MISAKA runtime repair validation — 2026-09-20

## Implemented
- Sister card roots no longer force low; managed Sister roots no longer inherit generic child off. Native defaults, per-model preferences, saved-session levels and explicit overrides are retained. Generic child behavior is unchanged.
- Compaction permits only contiguous non-context bookkeeping appends; owner/session/branch/model/settings or real message changes still reject stale publication, now with field diagnostics.
- Deliverable contracts separate filenames from requirements; legacy Chinese-colon, review and space-containing names are covered. Missing/empty files, invalid paths and symlink escape still block completion.
- Child lifecycle and associated card status are distinct in catalog and panel labels.
- Message and summary LIKE queries use balanced OR / integer-sum expressions, preserving all terms and query semantics rather than truncating long inputs.
- Temporary SQLite fixtures close their owned connections and pane fixtures isolate mailbox and roster paths.

## Validation
- Original checkout after guarded application: 507 passed, 49 subtests passed under -W error (final-main.log).
- Isolated combined regression: 507 passed, 49 subtests passed (final-isolated.log).
- Vendored core and embedding suite: 343 passed, with pre-existing import deprecation warnings (lcm-vendor.log).
- Syntax, Ruff and scoped diff checks pass. Original target hashes checked before merge; 24 task files merged, 1942 other inventoried paths and index unchanged. Final source bytes match isolated tree.

## Live-runtime boundary and test-isolation incident
No messages, stop/restart, hot reload, model changes or card repairs were sent to the current MISAKA run. Existing in-memory sessions were not upgraded or repaired in place.
An initial old pane test had a pre-existing isolation hole: lazy mailbox initialization could open the default messages database and run its cleanup of messages delivered more than seven days ago. Actual deleted rows were not established. This was identified, reported to the user, and repaired; final combined runs used an isolated HOME, mailbox, roster, session and agent directory. No claim is made that the initial old-test run was entirely read-only.
The precise field causing the earlier live compaction error and the precise earlier LCM query were not retained in logs; the repaired paths were reproduced independently.

## Files
- misaka/core/agent_session.py
- misaka/core/network/card_contract.py
- misaka/core/network/sister_runtime.py
- misaka/core/network/worker.py
- misaka/core/research/planner.py
- misaka/core/research/report.py
- misaka/core/session_catalog.py
- misaka/extensions/misaka_lcm/CORE_INTEGRITY.json
- misaka/extensions/misaka_lcm/PORT_NOTES.md
- misaka/extensions/misaka_lcm/vendor/dag.py
- misaka/extensions/misaka_lcm/vendor/search_query.py
- misaka/extensions/misaka_lcm/vendor/store.py
- misaka/ui/panel/panel.py
- tests/test_card_completion.py
- tests/test_card_contract_defaults.py
- tests/test_card_first_attempt.py
- tests/test_card_pane_reuse.py
- tests/test_card_reconcile_event.py
- tests/test_card_requeue.py
- tests/test_compaction_source_guard.py
- tests/test_lcm_search_expression_depth.py
- tests/test_project_lcm.py
- tests/test_session_catalog_child_status.py
- tests/test_sister_steering_by_mail.py

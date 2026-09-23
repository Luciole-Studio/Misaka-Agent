# Combined-regression warning attribution

## Confirmed by baseline/patched comparison

An isolated original-source overlay restores all six U05/U06 source targets from the pre-repair baseline. The same two original test files run against that overlay and the patched checkout with `-W error`, `PYTHONTRACEMALLOC=5`, and an external pytest plugin whose only operation is `gc.collect()` after each test. No source or repository test was changed and no warning was suppressed.

Both runs: **19 functional passes, 15 teardown errors**, with exactly the same failing test-name set. The errors are unclosed `SerializedConnection` ResourceWarnings raised as unraisable warnings. Allocation stacks point to these existing allocations:

- `tests/test_research_mode_notice.py:22`: board fixture returns connection at line 24 without registering close.
- `tests/test_research_mode_notice.py:41`: standalone plain database allocation has no close.
- `tests/test_sister_notification_wakes_lo.py:16`: board fixture returns connection without registering close.

All allocations pass through unchanged `misaka/core/platform/tasks.py:374`. SHA256 checks confirm the two tests and tasks.py match the captured baseline. Both logs show the same allocation sites. This is direct evidence that these fixture leaks predate U05/U06, rather than inference from an old report.

The original broad run collected cyclic SQLite objects later, so warnings were attributed to whichever subsequent test triggered garbage collection (including compaction/card pane tests). The isolated comparison proves the two supplied files independently reproduce the warning sources; it does not claim an allocation traceback for every original combined-run object, since that run did not enable tracemalloc.

## Evidence

- `leak-baseline.log` — original-source overlay, 19 passed / 15 errors, 28.84 seconds.
- `leak-patched.log` — repaired-source checkout, 19 passed / 15 errors, 26.95 seconds.
- `leak-comparison.json` — exact error-name-set equality and baseline hashes.
- `leak_gc.py` — external GC-only probe.
- `run-leak-baseline.py`, `run-leak-patched.py` — isolated HOME/MISAKA/XDG test runners.

No product source, repository test, live session, live database, or active MISAKA process was modified by this diagnostic follow-up.

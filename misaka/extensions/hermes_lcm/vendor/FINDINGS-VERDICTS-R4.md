# Round 4 findings verdicts

Merge: `d9a1ab79d9a65470ed3501ff8b7eabaeed1d80cb` fast-forwarded to `fece680d5f016b2a22cefb69e7b634a88e4f70d2`.

| Item | Verdict | Disposition |
|---|---|---|
| 1 strict response cap | CONFIRMED-FIXED | Evict hits, then summary leads; rebuild delta refs from surviving hits only. |
| 2 in-memory deadline load | CONFIRMED-FIXED | `:memory:` and memory-URI loads use the current interruptible connection. |
| 3 profile rebuild continuity | CONFIRMED-FIXED | Keep the old profile active until the completed replacement cuts over atomically. |

V1-DELIVERY-AFFECTING: item 1 only, at cap eviction.
Focused regressions: 3 passed (`laneR4-logs/focused-r4.xml`).
Touched area: 181 passed (`laneR4-logs/touched-area-r4.xml`).
Ruff on changed source/tests: clean (`laneR4-logs/ruff-r4.txt`).
Full replica: 2691 passed, 35 failed, 1 skipped, 12 xfailed (`laneR4-logs/full-suite-r4.xml`).
Baseline comparison: 35 actual = 35 expected; zero new/missing failure names (`laneR4-logs/full-suite-r4-comparison.txt`).
`git diff --check`: clean. No commit or push performed.

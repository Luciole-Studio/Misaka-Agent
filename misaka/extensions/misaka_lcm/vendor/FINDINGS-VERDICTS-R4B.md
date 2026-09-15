# Round 4B findings verdicts

PR `100yenadmin/hermes-lcm#175` @ `fece680d5f016b2a22cefb69e7b634a88e4f70d2`.
The spec's single-comment URL returned 404; all listed bodies were fetched by the canonical `pulls/comments/<id>` route.
Comment `3671346839` is the already-fixed R4 profile-continuity item; the five R4B items follow.

| Item | Verdict | Disposition |
|---|---|---|
| 1 query objects | CONFIRMED-FIXED | Reject extra `lcm_query*` indexes/triggers; both probes classify genuinely newer. Trajectory already rejects both extra object types. |
| 2 probe ledger | CONFIRMED-FIXED | Emit the probe's calls/tokens before any document request; callback can stop spend immediately. |
| 3 scan budget | CONFIRMED-FIXED | Config promises a hard scan budget; unlimited enumeration had no deadline. Start at operation entry and interrupt summary/chunk candidate reads. |
| 4 H3 golden gate | CONFIRMED-FIXED | Print failure and return 1 before ground truth, sweep, latency work, or artifact writing. |
| 5 temporal tokens | CONFIRMED-FIXED | Match temporal terms as whole tokens; `update` no longer matches `date`. |

Item 5 affects only sharp-compilation V2 (`TrajectoryStore.query` with `sharp_token_budget > 0`); V1 message-store recall is untouched.
Focused modules: 125 passed (`laneR4B-logs/focused-modules-final.xml`); Ruff and `git diff --check`: clean.
Full replica: 2697 passed, 35 failed, 1 skipped, 12 xfailed (`laneR4B-logs/full-suite-r4b.xml`).
Baseline: exact same 35 R4 failure names; zero new/missing (`laneR4B-logs/full-suite-r4b-comparison.txt`).
No commit or push performed.

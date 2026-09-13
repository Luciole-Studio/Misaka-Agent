# Round 7 findings verdicts
1. Fixed: forced same-profile rebuilds delete prior profile rows before refill; partial-rebuild regression passes.
2. Fixed: state documents use the smaller document/request token budget for chunk routing; request-cap regression passes.
3. Fixed: Policy D arm-quota cells measure recoverability from their fused candidate pool; scoped-row regression passes.
4. Fixed: evidence requirement descriptions reject surrogate code points before canonical UTF-8 digesting; regression passes.
5. Fixed: the H5 out-dir test now fails if provider construction occurs before the API-key guard.
6. Fixed: termless queries skip source-semantic embedding while state-semantic seeding and telemetry remain intact.
Validation: 6 focused R7 regressions passed.
Validation: full suite 2705 passed, 35 failed, 1 skipped, 12 xfailed.
Baseline comparison: 35/35 prior failure names; 0 new and 0 missing — PASS.
Issue-filing note: generation-scoped/profile-scoped embedding rows remain the larger option for availability during rebuild; not included here.
Scope: default-off/quota-gated subsystem, bench tooling, and tests only; no delivery path changes.
Proof boundary: local CI-replica source/test evidence only; no commit, push, PR update, release, or runtime proof.

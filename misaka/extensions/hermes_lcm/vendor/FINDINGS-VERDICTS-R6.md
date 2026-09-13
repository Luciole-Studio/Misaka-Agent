# Findings verdicts — R6

- FIXED — adaptive_retrieval.py P1 (Codex 3671928491; EvaOS 3671869848): digest capacity is derived from all declared requirement limits with one max-item headroom; 12 max-sized requirements validate and start.
- FIXED — trajectory_store.py P2 (Codex 3671928473): normal-batch provider spend and progress emit before vector validation/persistence; forced persist failure retains the full spend ledger.
- FIXED — trajectory_store.py P2 (Codex 3671928480): termless lexical queries become an empty lexical pool when state semantics are enabled; emoji-only semantic recall succeeds and quota-zero telemetry stays identical.
- FIXED — benchmarking/h5_state_semantic_replay.py P2 (Codex 3671928484): output parents are created immediately after argument parsing, before provider construction, the golden gate, warm-up, or sweep.
- VALIDATION — 4 focused regressions and 38 touched-area tests pass; Ruff passes; full suite has 35/35 baseline failure names with zero new names.
- PROOF BOUNDARY — source and CI-replica local test proof only; no commit, push, PR update, merge, release, or runtime change.

# Regression report — stress CLI canary false negatives (fixed)

## Affected test

`tests/test_stress_release_check.py::test_stress_cli_smoke_writes_results_summary_and_uses_output_sandbox`

## Verdict

GENUINE REGRESSION, FIXED. Before the fix, the stress CLI exited 1 even though
`lcm_grep` returned the requested canary rows. The final smoke test exits 0.

## Mechanism

The #168 retrieval redesign keeps compound canary queries on FTS. FTS snippets
insert `>>>` and `<<<` around matched terms, splitting a planted token such as
`CANARY_SCOPE_A_000` into
`>>>CANARY<<<_>>>SCOPE<<<_>>>A<<<_>>>000<<<`.

`benchmarking/stress.py` checks the serialized grep response for the original
contiguous token. The correct row is present, but the marker-split snippet makes
that string check false. The smoke run therefore reports:

- `grep_canary_recall_miss`
- `all_scope_missing_cross_session_hit`
- `explicit_session_scope_missing_hit`

This was a release-check CLI defect, not a retrieval miss and not a test
environment assumption. `benchmarking/stress.py` now strips the FTS markers
before every containment/scope assertion and reads all supported `lcm_grep`
result containers (`results`, `matches`, and `data`).

## Minimal repro

Set `HERMES_CI_REPRO_ROOT` to the local CI-replica artifact directory described
in the session-notes recipe, then run from the repository root:

```sh
PYTHONPATH="$HERMES_CI_REPRO_ROOT/agent-stub" \
"$HERMES_CI_REPRO_ROOT/venv-ci-repro/bin/python" \
scripts/lcm_stress_check.py \
  --output .artifacts/stress-cli-repro \
  --tier smoke \
  --json
```

Pre-fix: exit 1, `failure_count: 3`, correct grep rows present with marker-split
canary snippets, and empty stderr. Final: exit 0, `failure_count: 0`, and
`tests/test_stress_release_check.py::test_stress_cli_smoke_writes_results_summary_and_uses_output_sandbox`
passes.

## Evidence

- `laneA-logs/stress-cli-stdout.log`
- `laneA-logs/stress-cli-stderr.log`
- `laneA-logs/stress-cli-repro/results/stress-results.json`
- `laneR2-logs/touched-area-final.xml`

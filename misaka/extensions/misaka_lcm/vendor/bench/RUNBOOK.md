# Hermes-LCM benchmark runbook

This runbook exists so the benchmark program never restarts from scratch-knowledge. It covers the
three scoring protocols in use against this repo, how to run each one, and the operational discipline
that keeps scores honest and reproducible.

Conventions used below: `$HERMES_WORK` = a working root containing checkouts of this repo, the
memorybench harness, and (for the official protocol) a LongMemEval-V2 harness checkout.
`$RUN_ROOT` = the output directory for one run. `$ART_DIR` = wherever you keep this run's evidence
packet (raw outputs, manifests, reports) — any durable, non-ephemeral location works; keep it out of
`/tmp` since that is not durable storage on most machines.

## (a) The three protocols — what each measures, and comparability rules

**You will see three different kinds of numbers in this repo's history. They are NOT interchangeable
and must never be presented as if they were.**

1. **Sol protocol (internal, non-official).** This repo's own benchmark harness
   (`benchmarking/longmemeval.py` + `benchmarks/qa-harness/`) run end-to-end with a CLI-model transport
   (e.g. a coding-agent CLI in "exec" mode) standing in for both the answering model and the judge. It
   is fast to iterate, cheap, and useful for internal ranking of candidates — but the reader/judge
   models, prompts, and scoring normalization are all product-repo choices, not the published
   benchmark's. **Sol-protocol scores are LABELED "non-official" everywhere they appear** — in
   commit messages, docs, tags, and any external-facing text. Never state a bare Sol score next to a
   published leaderboard number without that label.
2. **Official LongMemEval-V2 protocol.** The published benchmark's own evaluation harness, Memory API
   contract, and submission validator, run against a fixed reader model (Qwen3.5-9B) with a fixed judge
   (used only for the categories that require LLM judgment — see (c)). This is the only protocol whose
   numbers may be compared to the public leaderboard, and only when every pinned choice (dataset
   revision, reader model string, precision, judge model) matches what the leaderboard requires.
3. **memorybench V1 protocol.** A third-party general memory-benchmark harness
   (`memorybench-benchmark-tool`) with its own CLI answerer/judge pipeline, checkpoint/resume model, and
   scoring. Used for the V1 (non-V2) benchmark track. Its scores are comparable to other
   memorybench-tool submissions using the same benchmark, provider, and model pins — not to the
   LongMemEval-V2 numbers from protocols 1 or 2, and not across memorybench runs with different
   answerer/judge model pins.

**Labeling rule:** any number produced by protocol 1 gets "(Sol, non-official)" or equivalent in every
place it is written down — READMEs, PR descriptions, release notes, dashboards. Any number produced by
protocol 2 that is intended for public comparison must record every pinned choice (dataset revision
hash, reader model string exactly as served, precision, judge model+version) alongside the score, and
match those to what the target leaderboard's submission validator requires. Any number produced by
protocol 3 must record the provider/benchmark/answerer/judge pins used.

**Comparability rules, concretely:**
- Sol-protocol scores compare validly to OTHER Sol-protocol scores from the same repo state and same
  scoring code — never to official-protocol or memorybench-V1 scores.
- A scoring/normalization fix applied to the Sol comparator must be rescored on BOTH the old and new run
  before any delta is claimed (see discipline rule below) — otherwise the "improvement" partly measures
  the scoring fix, not the change under test.
- Official-protocol scores compare to the public leaderboard only under the exact pinned configuration
  the leaderboard's submission validator checks; anything else (different reader precision, a different
  provider serving the same model, a different judge) must be disclosed as a deviation, not presented as
  a leaderboard-equivalent number.

## (b) Running a V2 Sol-protocol run

1. **Runner pattern.** The Sol-protocol runner lives in the product repo's own benchmarking module. A
   run is invoked against a target commit/branch of the product and a target commit of any adapter code
   that bridges retrieval telemetry into the run output. Always know and record BOTH commits before
   launching — a run's provenance is only as good as the exact code that produced it.
2. **Frozen DB copies, never originals.** Any run that reuses a pre-built corpus/database must operate
   on a COPY (e.g. `cp` into a scratch directory under `$HERMES_WORK`), never the original frozen asset.
   Frozen run roots and their source databases are read-only forever once a run has completed against
   them.
3. **Manifest freeze.** Before launching a full run, freeze a manifest recording: the exact product
   commit, the exact adapter commit, the reader/model pins, the dataset/question set, and (for any
   paired/blind comparison) the exact question-ID sampling — written BEFORE any run output is inspected.
   Changing the manifest after inspecting even one result voids the run for gate purposes and requires
   re-freezing and re-running.
4. **Telemetry.** A production-quality Sol run should emit, per query: the retrieval/semantic-attempt
   outcome (success/fallback/reason), the ranked candidate list actually considered, and the delivered
   evidence references actually used to answer. Without this telemetry, failure-mode analysis after the
   run is not possible — build it into the adapter layer, not bolted on after.
5. **Run-summary gates.** Every run's summary must report, loudly: semantic/retrieval attempts as
   `N_succeeded/N_total` (e.g. `451/451` attempted — not "most attempts", the literal count), any
   fallback reasons with their counts (never a silent/bare fallback counter with no reason), and the
   final score. A run whose summary cannot show `attempts == total questions` did not actually exercise
   retrieval for every question and its score should be treated as suspect until that gap is explained.

## (c) Running the official protocol

The official LongMemEval-V2 harness needs (i) a reader model, (ii) optionally a judge model for
LLM-scored categories, and (iii) the harness's own Memory API adapter wired to your product.

**Reader — two supported paths:**

- **Local vLLM (self-hosted reader).** Serve Qwen3.5-9B via vLLM on a GPU host reachable from the
  machine running the harness (tunnel the OpenAI-compatible port over SSH if remote).
  - This is a hybrid linear-attention model; vLLM's cudagraph capture set and KV/state cache scale with
    `max_num_seqs`, not just raw VRAM. On a VRAM-constrained box, the fix that lets cudagraph capture
    succeed (instead of OOM-ing in `profile_cudagraph_memory`) is **`--max-num-seqs 1`** — this only
    makes sense for a sequential run; don't set it low on a box with room for concurrency.
  - **fp8 online quantization** is the practical choice on a VRAM-constrained single GPU (~half the bf16
    footprint) — decide fp8 vs bf16 deliberately and disclose the choice; the official
    leaderboard/validator does not require a specific precision, but precision changes reader behavior
    and must be recorded per-response for parity disclosure.
  - Serve under the model string EXACTLY as any downstream harness logic expects it. This repo's harness
    has at least one code path that gates behavior (e.g. a disable-thinking switch) on an EXACT string
    match against the reader-model argument — a servable alias that "looks the same" (e.g. lowercased)
    can silently disable that gate. Check the harness source for any exact-match gates before picking a
    served name, and separately check any leaderboard/submission validator's requirement (often a
    case-insensitive substring check, which is more forgiving) — satisfy the STRICTER of the two.
  - Set `--max-model-len` generously above (max observed prompt tokens) + (full completion token budget)
    — a token budget that's too tight silently truncates hard questions (`finish_reason=length`) and
    corrupts the score for exactly the hardest cases.
  - Bound the harness's own memory-context token budget (a harness CLI flag) so that
    input + full completion budget stays under `max-model-len` with headroom — measure actual prompt
    token sizes across your question set first, don't guess.
- **Hosted alternative (e.g. via a model-routing/aggregator API).** Faster to stand up, but you give up
  some precision control:
  - **Provider pinning:** hosted routing APIs may serve the same model at different quantizations across
    providers. If you need a specific precision (to match a local cross-check or a disclosure
    commitment), pin the provider AND quantization explicitly rather than accepting the router's default
    fastest/cheapest choice.
  - **Precision disclosure:** record the actual serving precision per response (most routing APIs return
    provider metadata) and disclose it alongside the score — "official protocol, reader precision X via
    provider Y" — never present a hosted-router score as unqualified-official without this.
  - **200-with-error-body trap:** some hosted routing APIs return HTTP 200 with an in-body error and no
    valid choices when the underlying provider fails. A harness that only handles non-200 errors will
    crash on `response.choices[0]`. Handle this as its own typed error class and treat it like any other
    single-question failure (zero that question, keep the run alive) rather than letting it crash the
    whole run.
  - **Provider 429s under concurrency:** hosted providers commonly rate-limit well below what you'd
    expect from a "concurrency 5" setting. If you see exhausted-retries crashes, drop concurrency (a
    concurrency of 3 has been reliable against at least one aggregator provider) before assuming the
    account/plan is the problem.
  - Multi-image and per-domain content requirements vary by provider — verify your provider accepts the
    image count your hardest questions need before a long run, not after it fails partway through.

**Judge model:** for the protocol's LLM-scored categories (typically abstention-checking and
gotcha/premise categories — a minority of the question set; the rest score via deterministic matchers),
use the pinned judge model the protocol/leaderboard specifies. Do not substitute a different judge for
these categories if you intend the score to be leaderboard-comparable.

**Enterprise-domain data:** if your question set spans multiple domains (e.g. "web" and "enterprise"),
check that domain-specific auxiliary assets (e.g. a large screenshots archive for image-grounded
questions) are actually present before launching — a web-only smoke test will not surface a missing
enterprise-domain asset, and the run will crash partway through once it reaches those questions.

**Enterprise/submission needs:** if you intend to submit to a public leaderboard, read that
leaderboard's submission-packet requirements early — they commonly require a specific evidence bundle
(e.g. a screenshots tarball for enterprise/image questions) alongside the score files, and a submission
validator that checks pinned-field formats (model string substrings, evaluator string substrings,
dataset revision, etc.) BEFORE the score is considered submittable. Run the validator against your
output locally before treating a run as submission-ready.

**Checkpointing risk:** many official-style harnesses (this one included) do NOT checkpoint mid-run —
they hold all question results in memory and write output files only once, at the very end after all
questions and scoring complete. For a multi-hour run this means a crash at question N loses the ENTIRE
run, not just the tail. Mitigations: keep the run host awake and its storage mounted for the full
duration; prefer relaunching as several smaller batches (separate output dirs, merged afterward) over
one long unchecksummed run when the run will take many hours; verify your network tunnel (if the reader
is remote) has aggressive keepalive settings.

## (d) Running V1 memorybench

The V1 track uses a third-party CLI benchmark harness (memorybench-benchmark-tool) with its own
provider adapter for this product.

- **CLI shape:** `-p/--provider -b/--benchmark -j/--judge -r/--run-id -m/--answering-model --force`.
  `--force` clears any existing checkpoint for that run-id and starts fresh — never pass it on a run you
  mean to resume.
- **Env vars (product adapter):** a workdir variable (base directory for per-question isolated product
  DBs — required), an embedding-provider selector (fastembed as a free local default, or a paid API
  provider), an embedding-model override, and a path to the product repo checkout being benchmarked.
- **Env vars (answerer/judge transport):** since API-key-gated model access may not be available for a
  direct SDK call, this harness supports driving the answerer and judge through a coding-agent CLI in
  "exec" mode instead — an env var selects which CLI backend to use, with separate overrides for the
  model string, reasoning effort (answerer and judge may use different effort levels — keep the judge's
  effort LOW and deterministic; a higher-effort judge is not automatically a *better* judge and costs
  more), and a call timeout.
- **Checkpoint/resume pattern:** the harness persists per-question ingest/evaluate progress keyed by
  run-id. Launching with the same run-id and no `--force` resumes automatically — it will report how
  many questions were already ingested/evaluated and continue from there. If you change the question
  limit or target-question-set between launches under the same run-id, check the harness's log line
  describing what it decided to do with the existing checkpoint (it may only continue in-progress
  questions rather than adding new ones) — don't assume it silently reconciles to your new intent.
- **Split-phase execution:** this harness's phases (ingest, then answer, then evaluate) are checkpoint
  points individually — if a candidate arm differs from a control arm ONLY in a later phase (e.g.
  answer-layer or evaluate-layer changes, not retrieval), clone the control arm's completed
  ingest-phase state and resume the candidate from the answer phase, rather than re-running ingest
  identically twice. This is a large wall-clock saving on any run where retrieval/ingest is unchanged
  between arms.

## (e) Discipline rules

These are the rules that keep this program's scores trustworthy. Treat every one as load-bearing, not
optional process.

- **Snapshot-first.** The moment a run reports complete, copy its raw output directory verbatim to your
  evidence-packet location BEFORE resuming, rerunning, or force-relaunching anything. Harness output
  directories are volatile — a same-run-id resume, a `--force` rerun, or a scripted output-dir wipe can
  silently destroy completed scores that took hours/dollars to produce. Treat the snapshot, not the live
  harness directory, as the source of truth from that point forward. Never relaunch pointed at an output
  directory that ever held a completed run.
- **Manifest-freeze-before-inspection.** For any run whose result will gate a decision (component test,
  paired comparison, blind holdout), freeze the manifest (exact code commits, model pins, question-ID
  sampling) and write it down BEFORE inspecting even one result from that run. Any change made after
  inspecting a result — including "just fixing one thing" — voids the gate and requires re-freezing +
  re-running from scratch. This is what makes a gate a gate rather than a post-hoc story.
- **Both-sides rescoring.** A scoring/normalization/comparator fix is an INSTRUMENT change, not a
  product change. Apply it to every run you intend to compare — old baseline included — before
  publishing any delta or per-category breakdown. Applying a scoring fix only to the new run and
  comparing against an unfixed old score manufactures a false delta (measured concretely: a comparator
  bug fix, applied asymmetrically, produced a wildly inflated apparent gain and even reversed the sign
  of a per-category result versus the correctly-rescored-both-sides comparison). Monotonicity-check any
  rescore (a scoring fix should only ever flip incorrect→correct, never the reverse) and land the fix as
  committed, tested code scoped to the exact protocol path it belongs to — prove the other protocol
  paths are untouched.
- **Gate predeclaration + the proxy-calibration lesson.** Freeze every gate's pass/fail threshold and
  measurement method BEFORE seeing any result it will be applied to. If a frozen gate then fails for
  EVERY variant of a candidate you test — including the smallest/most conservative one — that is
  evidence the gate's *proxy metric* is measuring the wrong thing (e.g. a cheap structural-identity proxy
  like "did the exact set of delivered items change" will fail almost any composition/ranking change by
  construction, because reordering candidates changes which items get delivered even when answer
  correctness is unaffected). In that situation: (1) rule the gate miscalibrated IN WRITING with the
  reasoning, before changing anything; (2) predeclare a REPLACEMENT gate at the correct measurement
  level (actual answer correctness/risk, not proxy churn) before running it; (3) fix the candidate's
  configuration a priori — no picking a favorable knob after seeing results; (4) keep the original
  failed-proxy result on the record rather than deleting it. What you must never do is silently relax a
  failed gate's threshold after seeing it fail — that is gate-shopping, indistinguishable from moving the
  goalposts.
- **Never re-ingest identical state across arms.** If a candidate arm's change is scoped to a layer
  downstream of retrieval/ingest (e.g. answer formatting, a deterministic post-processing step), clone
  the control arm's completed ingest/retrieval state and resume only the changed phases. Redundant
  identical ingest across every arm has been measured to cost many hours of wall-clock for zero
  information gain.
- **File-keyed polling, never process-exit.** Long-running benchmark processes have been observed to sit
  idle for hours AFTER writing their final report, with the process never exiting. A supervisor/launcher
  that blocks on process exit (rather than watching for the report file to appear, with an independent
  liveness heartbeat) can deadlock indefinitely behind a lingering process. Always poll for the
  completion artifact file, not the process lifecycle.
- **Serialize only latency-reporting runs.** A run whose result includes a claimed latency/throughput
  number must run alone on its compute lane (no contention) or the latency claim is meaningless.
  Correctness-only paired/blind gate runs have no such requirement and should run CONCURRENTLY where
  compute allows (2-3 concurrent streams is a safe default; more has been proven stable in this program
  under favorable conditions) — don't serialize work that doesn't need it.
- **Spend caps + credit checks.** Before launching any run that spends against a metered API (embedding
  provider, hosted judge/reader), set an explicit spend cap and check remaining headroom against it
  before AND during a long run, especially one with an image-heavy or judge-heavy question mix where
  per-question cost varies widely. Disclose actual spend in the run's evidence packet.

## (f) Known traps

| Trap | Symptom | Fix / mitigation |
|---|---|---|
| Query-path SpendGuard-style throttling | A tight retrieval loop silently loses retrieval after N calls in a short window; product code swallows the failure via a bare `except` and just increments a fallback counter with no reason recorded | Give the query path its own generous (or exempt) rate-limit configuration, separate from the bulk/backfill path's stricter default; on a genuine provider rate-limit, bounded wait-and-retry BEFORE any fallback; never let a fallback path discard the reason — record a typed attempt/outcome record for every call so a broken instrument is visible in the run summary, not silently averaged away |
| Comparator LaTeX/markup normalization | A deterministic string-match comparator scores a semantically-correct answer as wrong because of stray markup wrappers (e.g. a `\text{}`-style wrapper glued onto a word, breaking a word-boundary match) | Fix at the Sol-protocol comparator layer ONLY — this is an internal-scoring concern, not something to touch in an official-protocol comparator, which is fixed (or not) by the benchmark's own maintainers. Apply per the both-sides-rescoring rule above; monotonicity-check |
| Hosted-router 200-with-error-body | A response comes back HTTP 200 but with no valid choices (provider-side failure embedded in the body); code that assumes 200⇒success crashes on `response.choices[0]` | Add a typed exception for "200 but no choices"; catch it alongside real API errors at the single-question level so one bad question zeros out rather than killing the whole run |
| Provider 429s under concurrency | Exhausted-retries crash mid-run on a hosted router even at modest concurrency (conc 5 observed to fail against at least one provider) | Lower concurrency (conc 3 has been reliable); pin a single well-behaved provider rather than accepting fallback-to-anywhere routing when you also need a specific precision |
| No-mid-run-checkpointing harnesses | A multi-hour run holds all results in memory and only writes output at the very end; any crash/sleep/network drop loses the entire run, not just the tail | Keep the run host awake + storage mounted for the full duration; for very long runs, batch into several smaller independent runs and merge outputs afterward instead of one long run |
| Lingering process after report write | A completed run's process sits alive for hours after its report file is written; anything blocking on process-exit hangs indefinitely | Poll for the report file's existence, not process exit; add an independent liveness heartbeat check separate from PID status |
| Bun/Node process lingering after report | Same class as above, specific to a Bun-based harness process | Same fix — file-keyed polling; kill and relaunch directly (bypassing any launcher that itself blocks on the hung process) if a hang is confirmed |

# Benchmark methodology

This is the methodology reference for the two memory-quality layers hermes-lcm
benchmarks. It documents what each harness measures, the fairness rules both
must honor, the configurations they run under, and how to reproduce a run.
Scores land separately as runs complete (see [Results index](#results-index));
this file describes the method, not a verdict.

## What we measure, two layers

### Layer 1 — retrieval quality

Session-level and turn-level recall@k / NDCG@10 against LongMemEval's labeled
evidence, computed by the in-tree offline harness
(`benchmarking/longmemeval.py`, CLI at `scripts/lcm_longmemeval.py`).

- **Dataset**: the pinned `LongMemEval_S` split of `xiaowu0162/longmemeval` on
  Hugging Face, revision `2ec2a557f339b6c0369619b1ed5793734cc87533` (`fetch`
  downloads it once; `run` never touches the network for the stub/fastembed
  path). LongMemEval_S labels each question with its evidence session(s), so
  recall@k / NDCG@k are computable without an LLM judge.
- **Isolation**: every question ingests into a **fresh temporary LCM store**
  (a throwaway SQLite DB under a `tempfile.TemporaryDirectory`) — no store is
  shared or reused across questions. That per-question isolation is the
  harness's fairness contract; a template DB may be cloned per question purely
  to skip repeated schema-migration cost (`--no-db-template` disables the
  clone and re-bootstraps from scratch).
- **Summaries**: one deterministic, non-LLM summary per session
  (`deterministic_session_summary`) — collapsed whitespace, truncated at a
  fixed character cap, no provider call. This keeps the retrieval layer's
  inputs reproducible byte-for-byte across runs.
- **Arms**: `fts` (raw-message full-text search), `summary_vectors`
  (session-summary KNN), `hybrid_rrf` (FTS + summary RRF fusion),
  `hybrid_rerank` (RRF fusion reranked — cosine placeholder or the real
  Voyage cross-encoder with `--rerank`), `chunk_vectors` (raw conversational-
  chunk KNN), `hybrid_rrf3` (FTS + summary + chunk RRF fusion), and
  `lcm_recall` — the **production** `tools.lcm_recall` tool (weighted RRF over
  the FTS/summary/chunk arms, the scope/recency prior, chunk-vs-FTS dedup),
  invoked directly rather than reimplemented, so this arm scores what a user
  actually calls.
- **Session vs. turn scoring**: session-level recall/NDCG treat each evidence
  session as a hit; turn-level recall/NDCG additionally credit a
  `(session, turn_index)` key. A summary-based hit cannot localize below its
  session, so it counts as a session-granularity marker that covers every
  evidence turn of that session at once — reported markdown flags any arm
  with this coarseness using a trailing `*`.

### Layer 2 — judged QA accuracy

End-task answer correctness through the full ingest → search → answer →
judge → report pipeline, run by the org's forked benchmarking harness,
[electricsheephq/memorybench-benchmark-tool](https://github.com/electricsheephq/memorybench-benchmark-tool)
(a fork of [supermemoryai/memorybench](https://github.com/supermemoryai/memorybench)),
branch `adapter/hermes-lcm`.

- The `hermes-lcm` Provider drives a long-lived Python bridge process
  (`bridge/hermes_lcm_bridge.py`) over newline-delimited JSON on stdin/stdout.
  `ingest` accumulates each harness session into a **fresh, per-container
  `lcm.db`** (one SQLite store per question-container — the same
  fresh-store isolation contract as the retrieval harness, just per QA
  container instead of per retrieval question); `search` calls the
  **production `tools.lcm_recall`** tool through the bridge, never a
  harness-reimplemented stand-in.
- The judge grades each answer with LongMemEval's own per-question-type judge
  prompts (`getJudgePromptForType`) — hermes-lcm ships no bespoke prompt (see
  `benchmarks/qa-harness/src/providers/hermes-lcm/prompts.ts`), so its answers
  are graded on the same rubric as every other provider.
- Full reproduction workflow, exact env vars, and the vendored adapter source
  are in [`qa-harness/REPLICATION.md`](qa-harness/REPLICATION.md).

## Fairness rules

Both layers hold to the same three rules:

1. **The adapter sees only what the harness gives every provider** — the
   session messages and the query. No dataset-specific logic, no evidence
   peeking.
2. **Fresh, dataset-disjoint session id.** The production `lcm_recall` tool
   applies a scope prior that boosts hits from the *current* conversation, so
   both harnesses invoke it with a synthetic `current_session_id` that is
   guaranteed absent from the question's haystack (or from any evidence
   session) — the scope prior can never silently lift an evidence session by
   conversation membership. (The recency prior still applies honestly to
   every hit; that is real production behavior, not a harness artifact.)
3. **Single-shot retrieval payload, no agentic re-expansion.** The QA layer
   scores `tools.lcm_recall`'s one-shot snippet payload (≤25 hits, 300 chars
   each) exactly as a caller would receive it — the adapter never re-queries
   or re-expands a hit to inflate the answer context.

## Configurations

| Axis | Floor | Recommended |
|---|---|---|
| Embeddings | local FastEmbed `BAAI/bge-small-en-v1.5` — free, fully offline | `voyage-context-3` (+ optional `rerank-2.5-lite` rerank arm via `--rerank`) |
| Answerer / judge (QA layer only) | — | CLI-backed (see below) |

The embeddings floor exists so retrieval-quality numbers are reproducible by
anyone with no API key and no network at query time. The recommended
configuration is what we'd point a production deployment at.

For the QA layer's answerer and judge, runs to date use a **CLI-backed
transport** — a subscription-authenticated CLI (`codex exec` or `claude -p`)
in place of a metered API SDK call — via `HERMES_MB_LLM_CLI=codex|claude`.
**Disclose the exact model id per run** in that run's results/notes file: the
CLI backend reports it through `cliLlmModelId()` (e.g. `gpt-5.6-sol (codex
default) (via codex exec)`). Any judge other than the LongMemEval leaderboard
default (`gpt-4o`) makes accuracy numbers **directional only**, until a
judge-parity rerun with `-j gpt-4o -m gpt-4o` on a metered key.

## Reproduction

### Retrieval layer

```bash
# One-time: download the pinned dataset file (operator step; run itself is offline).
python scripts/lcm_longmemeval.py fetch --output /path/to/dataset-dir

# Deterministic offline plumbing check (stub embedder; scores are meaningless).
python scripts/lcm_longmemeval.py run \
  --dataset /path/to/dataset-dir/longmemeval_s \
  --output benchmarks/runs/longmemeval-stub \
  --provider stub \
  --json

# Local FastEmbed floor (CI-grade, no network at query time once cached).
python scripts/lcm_longmemeval.py run \
  --dataset /path/to/dataset-dir/longmemeval_s \
  --output benchmarks/runs/longmemeval-fastembed \
  --provider fastembed \
  --model BAAI/bge-small-en-v1.5 \
  --json

# Recommended embeddings, with the real cross-encoder rerank arm.
python scripts/lcm_longmemeval.py run \
  --dataset /path/to/dataset-dir/longmemeval_s \
  --output benchmarks/runs/longmemeval-voyage \
  --provider voyage \
  --model voyage-context-3 \
  --rerank \
  --json
```

`--limit N` scores only the first N questions (useful for a smoke run);
`--no-db-template` disables the reused pre-migrated DB template (bootstraps
each question's store from scratch instead of cloning a template — mainly
for measuring the template-clone speedup, not for normal runs). Output is
`longmemeval_metrics.json` + `longmemeval_metrics.md` in `--output`.

### Judged QA layer

Full clone/install/env/run steps, exact flags, and the vendored adapter
source are in [`qa-harness/REPLICATION.md`](qa-harness/REPLICATION.md). In
short, from a checkout of the `adapter/hermes-lcm` branch:

```bash
export HERMES_LCM_REPO=/path/to/hermes-lcm
export HERMES_LCM_PYTHON=$HERMES_LCM_REPO/.venv-fastembed/bin/python
export HERMES_MB_WORKDIR=/fresh/empty/workdir
export HERMES_MB_PROVIDER=fastembed
export LCM_LONGMEMEVAL_FASTEMBED_CACHE=/path/to/fastembed-cache
export HERMES_MB_LLM_CLI=codex   # or: claude

bun run src/index.ts run -p hermes-lcm -b longmemeval \
  -j gpt-4o -m gpt-4o \
  -r <run-id> \
  --concurrency-answer 4 --concurrency-evaluate 4
```

Re-running the identical command with the same `-r <run-id>` (no `--force`)
resumes from the last completed phase instead of restarting.

## Caveats

- **MemDelta config-exactness.** Every number in a results file describes
  *that exact configuration* — dataset revision, provider/model, rerank
  on/off, harness version. It is never a universal verdict on hermes-lcm's
  retrieval quality; a different embedding model, chunk policy, or dataset
  slice is a different measurement, not a contradiction.
- **Retrieval recall ≠ leaderboard QA accuracy.** The two layers measure
  different things — recall@k/NDCG against labeled evidence sessions/turns
  vs. judged end-task answer correctness — and are not comparable to each
  other or interchangeable as a stand-in for one another.
- **n is disclosed per run.** Every results file states how many questions
  were scored (LongMemEval_S's abstention questions are excluded from
  recall/NDCG scoring and reported separately as an abstention count).
- **Judge model is disclosed per run.** QA-layer numbers carry the exact
  answerer/judge model id used; non-`gpt-4o` judges are directional until a
  judge-parity rerun.

## Results index

| File | Layer | Config | Status |
|---|---|---|---|
| [`results/longmemeval-v2-500q-fastembed.md`](results/longmemeval-v2-500q-fastembed.md) | Retrieval | 500q, FastEmbed `bge-small`, per-arm harness | landed |
| [`results/longmemeval-v3-500q-fastembed.md`](results/longmemeval-v3-500q-fastembed.md) | Retrieval | 500q, FastEmbed `bge-small`, harness turn-scoring fix + `lcm_recall` production-arm subset | landed |
| `results/longmemeval-voyage-100q.md` | Retrieval | 100q, `voyage-context-3` | pending |
| `results/qa-accuracy-<run-id>.md` | Judged QA | 500q, FastEmbed `bge-small`, CLI-backed judge | pending |

Scores are appended to this index as runs complete; this file documents the
method, not a running scoreboard.

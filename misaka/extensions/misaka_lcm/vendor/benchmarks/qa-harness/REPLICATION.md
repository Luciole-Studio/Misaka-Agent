# Judged QA harness — replication

This directory vendors the hermes-lcm-specific pieces of the judged-QA
benchmark harness so the exact code that ran a reported number lives next to
this repo, not only on a branch of a separate fork. **The vendored copies
below are the source of truth for what actually ran; the fork branch is the
runnable form** (it needs the rest of the harness — orchestrator, benchmark
loaders, UI — that doesn't belong in this repo).

## Harness identity

- Harness repo: [`electricsheephq/memorybench-benchmark-tool`](https://github.com/electricsheephq/memorybench-benchmark-tool),
  a fork of upstream [`supermemoryai/memorybench`](https://github.com/supermemoryai/memorybench).
- Branch: `adapter/hermes-lcm`.
- Commit this package was vendored from:
  `b249c9bf9441a351e32595da3bf84f0c734e49e6` (get the current tip yourself with
  `git -C /path/to/memorybench-benchmark-tool rev-parse HEAD` if replicating
  against a newer commit).

## Vendored files

```
qa-harness/
└── src/
    ├── providers/hermes-lcm/
    │   ├── index.ts        the memorybench Provider (spawns the Python bridge)
    │   ├── prompts.ts       deliberately empty — no bespoke answer/judge prompt
    │   ├── README.md        provider-level docs (prerequisites, env vars, run)
    │   └── bridge/
    │       └── hermes_lcm_bridge.py   JSON-line bridge: initialize/ingest/search/clear
    ├── utils/
    │   └── cli-llm.ts       CLI-backed (codex|claude) answerer/judge transport
    └── judges/
        └── cli.ts           Judge implementation wired to cli-llm.ts
```

These are byte-identical copies of the files at the commit above — no header
comments were added to any of them so they stay diffable against the fork.

## Clone + install

```bash
git clone https://github.com/electricsheephq/memorybench-benchmark-tool.git
cd memorybench-benchmark-tool
git checkout b249c9bf9441a351e32595da3bf84f0c734e49e6   # or the current adapter/hermes-lcm tip
bun install
```

## Environment

hermes-lcm's embedding path needs `fastembed` in a dedicated venv (the plugin
itself has no pip deps — it's imported via `sys.path`, never installed):

```bash
uv venv --python 3.13 /path/to/hermes-lcm/.venv-fastembed
uv pip install --python /path/to/hermes-lcm/.venv-fastembed/bin/python fastembed numpy
```

| Var | Required | Purpose |
|---|---|---|
| `HERMES_LCM_REPO` | yes | Path to the hermes-lcm checkout being benchmarked. |
| `HERMES_LCM_PYTHON` | yes | Interpreter with `fastembed` installed (the venv above). |
| `HERMES_MB_WORKDIR` | yes | Base dir for per-container `lcm.db` files. **Use a fresh/empty dir per run** — this is the harness's isolation boundary between containers. |
| `HERMES_MB_PROVIDER` | no (default `fastembed`) | `fastembed` or `voyage`. |
| `HERMES_MB_MODEL` | no | Embedding model id; defaults to `BAAI/bge-small-en-v1.5` (fastembed) or `voyage-context-3` (voyage). |
| `LCM_LONGMEMEVAL_FASTEMBED_CACHE` | no | Redirect the FastEmbed model cache (e.g. to a roomy volume). |
| `VOYAGE_API_KEY` | only if `HERMES_MB_PROVIDER=voyage` | — |
| `HERMES_MB_LLM_CLI` | no (enables the CLI-backed transport) | `codex` or `claude`. When set, the answer and judge phases route through a subscription-authenticated CLI instead of a metered API SDK. |
| `HERMES_MB_CODEX_MODEL` | no | Overrides the codex default model. |
| `HERMES_MB_CODEX_EFFORT` | no (default `low`) | `model_reasoning_effort` passed to `codex exec`. |
| `HERMES_MB_CLAUDE_MODEL` | no (default `claude-sonnet-5`) | Model passed to `claude -p`. |
| `HERMES_MB_CLI_TIMEOUT_MS` | no (default `180000`) | Per-call timeout for the CLI transport. |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` | only if **not** using `HERMES_MB_LLM_CLI` | Harness-level answerer/judge key (hermes-lcm itself needs no provider key — retrieval is fully local). |

## Run

```bash
export HERMES_LCM_REPO=/path/to/hermes-lcm
export HERMES_LCM_PYTHON=$HERMES_LCM_REPO/.venv-fastembed/bin/python
export HERMES_MB_WORKDIR=/fresh/empty/workdir
export HERMES_MB_PROVIDER=fastembed
export LCM_LONGMEMEVAL_FASTEMBED_CACHE=/path/to/fastembed-cache
export HERMES_MB_LLM_CLI=codex   # or: claude

bun run src/index.ts run -p hermes-lcm -b longmemeval \
  -j gpt-4o -m gpt-4o \
  -r hermes-lcm-fastembed-<run-label> \
  --concurrency-answer 4 --concurrency-evaluate 4
```

`-j`/`-m` are recorded on the run's checkpoint as labels even when
`HERMES_MB_LLM_CLI` is set; the model that actually answered/judged is the
one `cliLlmModelId()` reports (disclose that value, not `-j`/`-m`, in the
results writeup). Add `-l <n>` to limit the question count for a smoke run
before committing to a full 500q pass.

### Resume semantics

Re-running the **identical command with the same `-r <run-id>`** (no
`--force`) resumes from the last completed phase — the orchestrator finds the
existing checkpoint, validates provider/benchmark match, and continues.
Passing `--force` clears that checkpoint and starts over. Check progress at
any time with:

```bash
bun run src/index.ts status -r <run-id>
```

### Where results land

Relative to the harness repo root: `./data/runs/<run-id>/checkpoint.json`
(run state, resumable) and `./data/runs/<run-id>/results/` (per-question and
aggregate output).

## What we changed and why

The `adapter/hermes-lcm` branch adds three things on top of upstream
`supermemoryai/memorybench`, none of which touch how any other provider is
scored:

- **`hermes-lcm` provider + Python bridge.** hermes-lcm is Python/SQLite
  native and the harness is TypeScript/Bun, so the provider spawns a
  long-lived Python bridge process and speaks newline-delimited JSON over
  stdin/stdout — the same "persistent backend handle" shape the harness's Zep
  provider uses for its SDK client. The bridge is crash-loud: if it exits,
  the pending call rejects and every later call throws rather than silently
  degrading.
- **Fresh, per-container `lcm.db` isolation.** `ingest` accumulates one
  harness session at a time into a store keyed by `containerTag`, and
  `search` invokes the **production** `tools.lcm_recall` (never a
  harness-reimplemented arm) through a `SimpleNamespace` engine with a fresh,
  dataset-disjoint `current_session_id` — the same scope-prior fairness rule
  the retrieval harness (`benchmarking/longmemeval.py`) uses, applied per QA
  container. The bridge imports `benchmarking.longmemeval`'s deterministic
  session-summary function rather than reimplementing it, so both layers
  build identical session summaries from identical inputs.
- **CLI-backed answerer/judge (`cli-llm.ts` + `judges/cli.ts`).** Added so a
  full 500-question run doesn't require a funded per-token API key: it routes
  the answer and judge phases through a subscription-authenticated CLI
  (`codex exec` or `claude -p`) instead. Prompts are piped over stdin (avoids
  an argv/`E2BIG` failure mode on long contexts) and the transport is
  crash-loud (nonzero exit or empty output rejects) with **one retry** on a
  transient failure — added after a measured burst of simultaneous transient
  CLI failures killed an otherwise-healthy run partway through. This is a
  transport swap only: the judge still grades with the harness's standard
  LongMemEval per-question-type prompts (`buildJudgePrompt` /
  `parseJudgeResponse`), the same as every SDK-backed judge.

**Fairness held constant:** the adapter/bridge only ever receives what the
harness gives every provider (session messages + query, no evidence
peeking); `tools.lcm_recall`'s single-shot snippet payload (≤25 hits, 300
chars each) is scored as-is, with no agentic re-expansion of a hit.

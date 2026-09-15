# hermes-lcm provider

Scores [hermes-lcm](https://github.com/) — a Python/SQLite lossless
context-management memory plugin — on this harness's full
ingest → search → answer → judge → report pipeline, for leaderboard-comparable
LongMemEval_S QA accuracy.

## How it works

hermes-lcm is Python-native, so the provider (`index.ts`) drives a long-lived
Python bridge (`bridge/hermes_lcm_bridge.py`) over newline-delimited JSON on
stdin/stdout — the same "persistent backend handle" shape as the Zep provider's
SDK client. Requests are serialized (one line in flight at a time) and the
provider is crash-loud: if the bridge exits, the pending call rejects and every
later call throws.

| Provider method | Bridge behavior |
|---|---|
| `initialize` | Warms the embedding model once (so the model load happens here, not inside a per-question path). |
| `ingest` | Accumulates ONE harness session at a time into a per-container LCM store on disk: appends the messages to `MessageStore` (session ids/order preserved), builds the **same** deterministic per-session summary the in-house harness uses (`benchmarking.longmemeval.deterministic_session_summary`, imported — never reimplemented), records the summary embedding + conversational-chunk embeddings (batched). |
| `awaitIndexing` | No-op — ingest is fully synchronous. |
| `search` | Calls the **production** `tools.lcm_recall` over the container store through a `SimpleNamespace` engine with a fresh, dataset-disjoint `current_session_id`, then maps each hit → `{content, metadata}` and returns the top-k the harness asks for. |
| `clear` | Deletes the container's db + date sidecar. |

The hermes-lcm plugin repo is **never modified**: it is made importable via the
same `sys.path` + package-spec bootstrap the repo's own harness uses.

**Fairness:** the adapter only ever sees what the harness gives every provider
(the session messages + the query). No dataset-specific logic, no evidence
peeking. `tools.lcm_recall`'s snippet (300 chars/hit, ≤25 hits) is the honest
single-shot production payload — the adapter does not agentically re-expand hits.

**Session dates:** the plugin's `append_batch` stamps ingest wall-clock time and
takes no per-message timestamp (and must not be modified), so the
harness-provided session date — data every provider receives — is preserved in a
per-container sidecar and surfaced onto each search hit's `metadata.date` for
temporal questions.

## Prerequisites

hermes-lcm's embedding path needs `fastembed` (optional dep). Create a dedicated
venv once (the plugin itself has no pip deps — it is imported via `sys.path`):

```bash
uv venv --python 3.13 /path/to/hermes-lcm/.venv-fastembed
uv pip install --python /path/to/hermes-lcm/.venv-fastembed/bin/python fastembed numpy
```

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `HERMES_LCM_REPO` | `/Volumes/LEXAR/hermes-work/hermes-lcm` | Path to the hermes-lcm checkout. |
| `HERMES_LCM_PYTHON` | `$HERMES_LCM_REPO/.venv-fastembed/bin/python` | Python interpreter that has `fastembed`. |
| `HERMES_MB_WORKDIR` | `$TMPDIR/hermes-lcm-mb` | Base dir for per-container LCM dbs. |
| `HERMES_MB_PROVIDER` | `fastembed` | Embedding provider: `fastembed` or `voyage`. |
| `HERMES_MB_MODEL` | `BAAI/bge-small-en-v1.5` (fastembed) / `voyage-context-3` (voyage) | Embedding model id. |
| `LCM_LONGMEMEVAL_FASTEMBED_CACHE` | fastembed default | FastEmbed model cache dir (point at a roomy volume). |
| `VOYAGE_API_KEY` | — | Required only when `HERMES_MB_PROVIDER=voyage`. |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` | — | Answerer + judge key (harness-level, not provider-level). |

hermes-lcm needs **no provider API key** — retrieval is fully local.

## Run

```bash
export HERMES_LCM_REPO=/Volumes/LEXAR/hermes-work/hermes-lcm
export HERMES_LCM_PYTHON=$HERMES_LCM_REPO/.venv-fastembed/bin/python
export HERMES_MB_WORKDIR=/Volumes/LEXAR/hermes-work/mb-workdir
export HERMES_MB_PROVIDER=fastembed
export LCM_LONGMEMEVAL_FASTEMBED_CACHE=/Volumes/LEXAR/hermes-work/fastembed-cache
export OPENAI_API_KEY=sk-...            # answerer + judge

# 5-question smoke
bun run src/index.ts run -p hermes-lcm -b longmemeval -j gpt-4o -m gpt-4o -l 5 -r smoke-hermes-lcm-5q --force

# full LongMemEval_S (500q)
bun run src/index.ts run -p hermes-lcm -b longmemeval -j gpt-4o -m gpt-4o -r hermes-lcm-fastembed-500q
```

Swap `-j sonnet-4 -m sonnet-4` (Anthropic) or `-j gemini-2.5-flash -m
gemini-2.5-flash` (Google) to change the judge/answerer.

## ⚠ Result-comparability disclosures

Bake these into any numbers reported from a hermes-lcm run:

- **Judge/answerer = `gemini-2.5-flash`** in the runs produced so far (no funded
  OpenAI/Anthropic key was available: the OpenAI key was `insufficient_quota` and
  no `ANTHROPIC_API_KEY` existed). This is **non-standard** vs the usual `gpt-4o`
  LongMemEval leaderboard judge, so the accuracy numbers are **directional until a
  judge-parity rerun** with `-j gpt-4o -m gpt-4o`.
- **Retrieval = production `tools.lcm_recall` single-shot snippets** (≤25 hits,
  300 chars each). The adapter does not agentically re-expand hits, so this scores
  hermes-lcm's one-shot recall payload — not a multi-hop recall→expand agent loop.

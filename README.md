# MISAKA

A multi-agent research system for the humanities and social sciences. Last Order (the coordinator) breaks a research question into task cards; Sisters (worker agents) execute them in their own processes and hand back `report.json`; accepted results are committed to the project's git repository and indexed into a document corpus.

## Install

```sh
uv venv .venv --python 3.13
uv sync                        # development: product, every provider SDK, PDF outline extraction, test tooling
pip install 'misaka[anthropic]'   # a user install picks its provider SDKs: anthropic / openai / google / bedrock / mistral
                                  # (misaka[providers] = all five; misaka[pageindex] = PDF outline extraction)
```

`git` is required: `misaka init` creates the project repository, and accepted results are
committed into it. Install it first (`xcode-select --install`, `apt install git`).

The `grep` and `find` tools shell out to [ripgrep](https://github.com/BurntSushi/ripgrep)
and [fd](https://github.com/sharkdp/fd). MISAKA does not fetch them for you — install them
(`brew install ripgrep fd`, `apt install ripgrep fd-find`), or drop the binaries in
`~/.misaka/agent/bin`.

## First run

A fresh install talks to `anthropic` / `claude-sonnet-4-5`. Give it a credential either way, then check it:

```sh
export ANTHROPIC_API_KEY=sk-ant-...   # the environment is honoured for every builtin provider
misaka chat                           # or, inside the chat: /login stores an OAuth token or an API key in ~/.misaka/agent/auth.json
misaka auth check                     # ✓/✗ per provider, through the same resolver every session uses
```

Change provider and model with `/model` inside the chat. Picking one from the selector saves it as `defaultProvider` / `defaultModel` in `~/.misaka/agent/settings.json`, where it becomes the default for every Sister; `/model <name>` switches only the session in front of you.

## Run

```sh
misaka                 # panel in a terminal, plain chat when piped
misaka chat            # talk to Last Order
misaka research "..."  # start a research run
misaka board           # the task board
misaka doc add x.pdf   # index a document
misaka web status      # web search: active backend, keyless ring, configured credentials
```

Web search works with no configuration at all — a keyless vendor ring serves it. To pin a
backend or add a vendor key: `misaka web set backend tavily`, `misaka web set env.TAVILY_API_KEY tvly-…`
(written 0600 to `~/.misaka/web.json`; an exported variable always wins over the file).

## Configure

Everything lives under `~/.misaka/`:

| Where | What |
|---|---|
| `agent/settings.json` | engine settings; `defaultProvider` / `defaultModel` are the product defaults (picking a model in the `/model` selector writes them; `/model <name>` only switches this session) |
| `agent/auth.json` | stored credentials (`/login`), kept at mode 0600 |
| `agent/models.json` | custom providers and models (an OpenAI-compatible gateway, a local server); their IDs are valid `defaultModel` values |
| `profiles/last_order/` | Last Order: persona (`SOUL.md`), MCP servers (`config.yaml`, `mcp/`), `skills/`, and `config.json` `{"model": "..."}` to pin her model |
| `profiles/sisters/<id>/` | one directory per Sister (`misaka create`): `DESCRIBE.md` for routing, `SOUL.md`, `config.json` to pin a model, `skills/` |
| `allies.json` | the recognised ally CLIs |

Environment variables override the files (all optional):

| Variable | Default | Meaning |
|---|---|---|
| `MISAKA_PROVIDER` / `MISAKA_MODEL` | `settings.json`, then `anthropic` / `claude-sonnet-4-5` | provider and model for Sisters and chat |
| `MISAKA_LO_MODEL` | Last Order's pinned model, then `MISAKA_MODEL` | Last Order's model |
| `MISAKA_SUBAGENT_MODEL` | the parent's model | model for subagents a Sister spawns |
| `MISAKA_SMALL_FAST_MODEL` | the provider's own small model | model for cheap internal calls |
| `MISAKA_FORCE_MODEL` | none | overrides every model choice, card configuration included |
| `MISAKA_CACHE_RETENTION` | `short` | `long` asks the provider for long prompt-cache retention |
| `MISAKA_WEB_CONFIG` | `~/.misaka/web.json` | web search: backend choice, keyless tier, and vendor credentials |
| `MISAKA_WEB_CACHE` | `~/.misaka/cache/web` | where web_extract keeps page text for its TTL |
| `MISAKA_ALLOW_PRIVATE_URLS` | off | lets the web tools reach private and loopback addresses; cloud metadata endpoints stay blocked either way |

Paths — each is a directory or file MISAKA owns:

| Variable | Default | Meaning |
|---|---|---|
| `MISAKA_CODING_AGENT_DIR` | `~/.misaka/agent` | the engine directory: settings, auth, models, sessions |
| `MISAKA_DB` / `MISAKA_MESSAGES` / `MISAKA_LCM_DB` | `~/.misaka/{board,messages,lcm}.db` | task board, message queue, compaction store |
| `MISAKA_TASKS` | `~/.misaka/tasks` | per-card state: sessions, reports, locks |
| `MISAKA_SUBAGENT_DIR` | `~/.misaka/subagents` | subagent state |
| `MISAKA_PAGEINDEX` | `~/.misaka/pageindex` | the document corpus index |
| `MISAKA_OCR_LANGS` | `eng+chi_sim+jpn` | tesseract language codes for scanned PDFs, joined with `+`; needs `ocrmypdf` on PATH (`brew install ocrmypdf`) |
| `MISAKA_RUNS_HOME` | `~/.misaka/runs` | research run artifacts |
| `MISAKA_WORKTREE_DIR` | `~/.misaka/worktrees` | git worktrees for isolated agents |
| `MISAKA_AGENT_MEMORY_HOME` | `~/.misaka/memory` | agent memory files |
| `MISAKA_NET_SOCK` / `MISAKA_NET_SNAPSHOT` | `~/.misaka/net.sock` / `net.json` | the panel daemon's socket and roster snapshot |
| `MISAKA_INPUT_HISTORY` | none — the feature is off unless set | file for persistent chat input history |
| `MISAKA_TELEMETRY` | unset — the `enableInstallTelemetry` setting decides (default on) | whether this install may be identified to an outside service; set at all (`0` included) and it wins over the setting |
| `MISAKA_TIMING` | `0` | `1` prints startup timings to stderr, grouped by namespace (`main`, `extensions`) |
| `MISAKA_MCP_CONFIG` | the profile's `mcp/` | MCP server configuration |
| `MISAKA_MCP_CACHE` | `~/.misaka/cache/mcp_schema_cache.json` | cached MCP tool schemas |

Budget, concurrency, and limits:

| Variable | Default | Meaning |
|---|---|---|
| `MISAKA_TOKEN_CAP` | `0` (off) | token budget shown and enforced on the board |
| `MISAKA_BEAST_AT` | `0.85` | fraction of the cap at which a card drops to beast mode |
| `MISAKA_SUBAGENT_TOKEN_RESERVATION` | `32768` | tokens held back for a subagent |
| `MISAKA_TURN_TOKEN_LIMIT` | none | per-turn token ceiling |
| `MISAKA_MAX_CONCURRENT_SISTERS` | host CPUs | Sisters running at once |
| `MISAKA_MAX_CONCURRENT_PER_SISTER` | `min(2, CPUs)` | cards one Sister runs at once |
| `MISAKA_MAX_CONCURRENT_SUBAGENTS` | host CPUs | subagents one session runs at once |
| `MISAKA_SUBAGENT_TOOL_CEILING` | none | comma-separated tools a subagent may not exceed |
| `MISAKA_TASK_MAX_OUTPUT` | `32000` (max `160000`) | characters of a subagent's output kept |
| `MISAKA_SKILL_COPY_CAP_MB` | `200` | size ceiling when copying a skill into a sandbox |
| `MISAKA_JUDGE_TIMEOUT` | `600` | seconds a research planner / judge call may take |
| `MISAKA_MCP_INIT_TIMEOUT` / `MISAKA_MCP_CALL_TIMEOUT` | `30` / `120` | seconds for MCP startup and per call |
| `MISAKA_MCP_REQUIRED_WAIT` | `30` | seconds to wait for a required MCP server |

Context engine:

| Variable | Default | Meaning |
|---|---|---|
| `MISAKA_LCM_SUMMARY_PROVIDER` / `MISAKA_LCM_SUMMARY_MODEL` / `MISAKA_LCM_SUMMARY_FALLBACK_MODELS` | the product provider / model | the summariser; fallbacks are comma-separated |
| `MISAKA_LCM_SUMMARY_TIMEOUT` | `60` | seconds per summary |
| `MISAKA_LCM_RETRIEVAL_MODE` / `MISAKA_LCM_EMBEDDING_MODEL` | `fts` / none | retrieval over compacted history; `hybrid` plus a model name is also the on-switch for semantic retrieval, as a local `fastembed` provider (`uv sync --extra lcm-semantic`, then `misaka lcm embed warmup` and `misaka lcm embed backfill --apply`). Upstream's `LCM_EMBEDDING_PROVIDER`/`LCM_EMBEDDING_MODEL` reach `voyage` and `ollama` instead |

#### The context engine

Long sessions are compacted by [hermes-lcm](https://github.com/stephenschoettler/hermes-lcm),
ported whole into `misaka/extensions/hermes_lcm/vendor/` (60 modules, byte-identical to the
commit in `UPSTREAM_COMMIT`, with misaka's adapters quarantined in `host/`). It keeps every
message in `~/.misaka/lcm.db`, compacts older context into a hierarchical summary DAG, and
rebuilds the prompt from the best summaries plus a protected fresh tail -- so nothing is
lost, only moved out of the way and retrievable through the fifteen `lcm_*` tools.

It reads upstream's own `LCM_*` environment variables -- all of them, documented upstream --
and the `MISAKA_LCM_*` names above fill in as lower-precedence aliases for the four they
overlap. `PORT_NOTES.md` records every deviation from upstream (currently none) and
`docs/plans/hermes-lcm-sync.md` is the procedure for taking a newer upstream release.

#### The four opt-in families

Everything the port added beyond compaction is off by default, which is upstream's
posture. Each family is one switch, and each has a price:

| Family | Switch | What it does | What it costs when on |
|---|---|---|---|
| Large-output externalization | `LCM_LARGE_OUTPUT_EXTERNALIZATION_ENABLED` | tool results over `LCM_LARGE_OUTPUT_EXTERNALIZATION_THRESHOLD_CHARS` (12,000) move to side files next to the database, leaving a reference the model can expand | no model calls at all; disk beside `lcm.db` instead of megabytes inside it. Add `LCM_LARGE_OUTPUT_ACTIVE_REPLAY_STUBBING_ENABLED` to stub them in the live prompt too |
| Temporal rollups | `LCM_TEMPORAL_ROLLUPS_ENABLED` | day/week/month summaries beside the DAG, so `lcm_recent("this week")` answers from a rollup instead of re-reading leaves | one summariser call per period built, `LCM_ROLLUP_BUILDS_PER_PASS` (2) per maintenance pass. A quiet session builds a handful a day |
| Semantic retrieval | `LCM_EMBEDDINGS_ENABLED` + `LCM_EMBEDDING_PROVIDER`/`LCM_EMBEDDING_MODEL` | `lcm_grep` gains `semantic` and `hybrid` modes, which find a paraphrase full-text cannot | one vector per summary node and chunk. `fastembed` runs locally and costs nothing per call (`uv sync --extra lcm-semantic`, ~130 MB model download); `voyage` is billed per token, and `misaka lcm embed backfill` prints the estimate before `--apply` spends it |
| V4 assertions | `LCM_ASSERTIONS_ENABLED` + `LCM_ASSERTION_EXTRACTION_ENABLED` | claims with exact provenance, extracted from stored messages and queryable through `lcm_query_state` | one auxiliary model call per source row, bounded at `LCM_ASSERTION_EXTRACTION_MAX_SOURCES_PER_PASS` (4, ceiling 8) per compaction. `misaka lcm assertions rebuild --apply` is the unbounded one: `--limit` (100, max 500) is all that stands between it and 500 calls |

Turning a family on helps traffic from that moment; history already in the database
catches up only when told to. `misaka lcm externalize-backfill`, `misaka lcm rollups
--rebuild`, `misaka lcm embed backfill` and `misaka lcm assertions rebuild` are those
four commands, and all of them print a plan first and write only on `--apply`.

#### Operator commands

`misaka lcm <op>`: `status` and `doctor` report; `backup` snapshots; `rotate SESSION_ID` compacts one session in place
(advancing the lifecycle frontier past its pre-tail raw rows without changing its
identity, deleting nothing, calling no model, backup-first on `--apply`); `preset
show|suggest|apply` reads upstream's benchmarked model-family settings, and never writes
any -- upstream's apply is preview-only, so `--apply` has nothing to commit.
`--apply` is what commits, everywhere.
The ported ops answer in upstream's own text, which names its slash command: read
`/lcm X` there as `misaka lcm X`.

**Prompt caching.** Every compaction rewrites the front of the context and so invalidates
an Anthropic prompt-cache prefix. `LCM_CACHE_FRIENDLY_CONDENSATION_ENABLED=1` keeps the
engine from also rebuilding its higher-level summaries in the same round as a leaf, which
costs one rewrite per compaction instead of two.

Terminal and panel:

| Variable | Default | Meaning |
|---|---|---|
| `MISAKA_THEME` | the terminal's | `dark` or `light` |
| `MISAKA_APP_TITLE` / `MISAKA_TAGLINE` | `MISAKA` | what the header shows |
| `MISAKA_PANEL_PREFIX` | `ctrl+b` | the panel's prefix chord |
| `MISAKA_TUI_ESC_TIMEOUT` | `10` ms, `100` under ssh | how long a lone ESC waits for an Alt+key second byte |
| `MISAKA_ALLOW_NESTED` | unset | `1` allows opening the panel inside one of its own panes |
| `MISAKA_HARDWARE_CURSOR` / `MISAKA_CLEAR_ON_SHRINK` | `settings.json`, else off | `1` enables; the settings file wins when it names them |
| `MISAKA_OAUTH_CALLBACK_HOST` | `127.0.0.1` | host the OAuth loopback listener binds |
| `MISAKA_INHERIT_PROCESS_GROUP` | unset | `1` keeps child agents in MISAKA's process group |

A number that does not parse stops the command with the variable's name and value. Every
`MISAKA_*` name not in these tables is set by MISAKA for its own child processes — a test
(`tests/test_env_documented.py`) keeps that claim true in both directions.

## Diagnose

`/debug` in the chat writes the rendered screen and the whole conversation to
`~/.misaka/agent/misaka-debug.log` (mode 0600) and prints the path. That is the only
diagnostic switch — there are no debug environment variables.

## Check

```sh
make check             # tests, -W error, compileall, import sweep, wheel build
```

`misaka/documents/pageindex/flash` is vendored from [PageIndex](https://github.com/VectifyAI/PageIndex) (MIT); see `UPSTREAM.md` there.

# MISAKA

A multi-agent research system for the humanities and social sciences. Last Order (the coordinator) breaks a research question into task cards; Sisters (worker agents) execute them in their own processes and hand back `report.json`; accepted results are committed to the project's git repository and indexed into a document corpus.

## Install

```sh
uv venv .venv --python 3.13
uv sync                        # development: product, every provider SDK, PDF outline extraction, test tooling
pip install 'misaka[anthropic]'   # a user install picks its provider SDKs: anthropic / openai / google / bedrock / mistral
                                  # (misaka[providers] = all five; misaka[pageindex] = PDF outline extraction)
```

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

Change provider and model with `/model` inside the chat; the choice is saved as `defaultProvider` / `defaultModel` in `~/.misaka/agent/settings.json` and becomes the default for every Sister.

## Run

```sh
misaka                 # panel in a terminal, plain chat when piped
misaka chat            # talk to Last Order
misaka research "..."  # start a research run
misaka board           # the task board
misaka doc add x.pdf   # index a document
```

## Configure

Everything lives under `~/.misaka/`:

| Where | What |
|---|---|
| `agent/settings.json` | engine settings; `defaultProvider` / `defaultModel` are the product defaults (`/model` writes them) |
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
| `MISAKA_CODING_AGENT_DIR` | `~/.misaka/agent` | the engine directory: settings, auth, models, sessions |
| `MISAKA_DB` / `MISAKA_MESSAGES` / `MISAKA_LCM_DB` | `~/.misaka/{board,messages,lcm}.db` | task board, message queue, compaction store |
| `MISAKA_TASKS` | `~/.misaka/tasks` | per-card state: sessions, reports, locks |
| `MISAKA_NET_SOCK` / `MISAKA_NET_SNAPSHOT` | `~/.misaka/net.sock` / `net.json` | the panel daemon's socket and roster snapshot |
| `MISAKA_JUDGE_TIMEOUT` | `600` | seconds a research planner / judge call may take |
| `MISAKA_TOKEN_CAP` | `0` (off) | token budget shown and enforced on the board |
| `MISAKA_CONTEXT_ENGINE` | `lcm` | `lcm` (lossless compaction) or `native` (the engine's one-shot summary) |
| `MISAKA_LCM_SUMMARY_PROVIDER` / `MISAKA_LCM_SUMMARY_MODEL` / `MISAKA_LCM_SUMMARY_FALLBACK_MODELS` | the product provider / model | the summariser; fallbacks are comma-separated |
| `MISAKA_LCM_SUMMARY_TIMEOUT` | `60` | seconds per summary |
| `MISAKA_LCM_RETRIEVAL_MODE` / `MISAKA_LCM_EMBEDDING_MODEL` | `fts` / none | retrieval over compacted history |
| `MISAKA_THEME` | the terminal's | `dark` or `light` |

A number that does not parse stops the command with the variable's name and value. Other `MISAKA_*` variables are set by MISAKA for its own child processes and are not configuration.

## Check

```sh
make check             # tests, -W error, compileall, import sweep, wheel build
```

`misaka/documents/pageindex/flash` is vendored from [PageIndex](https://github.com/VectifyAI/PageIndex) (MIT); see `UPSTREAM.md` there.

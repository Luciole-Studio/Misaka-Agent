# Configuration

Every setting has a working default. This page is reference material: read it when you
want to change something, not before your first run. [README.md](README.md) covers
installing and running.

Files live under `~/.misaka/`. Environment variables override the files, and all of
them are optional.

## Files

| Where | What |
|---|---|
| `agent/settings.json` | engine settings; `defaultProvider` / `defaultModel` are the product defaults (picking a model in the `/model` selector writes them; `/model <name>` only switches this session) |
| `agent/auth.json` | stored credentials (`/login`), kept at mode 0600 |
| `agent/models.json` | custom providers and models (an OpenAI-compatible gateway, a local server); their IDs are valid `defaultModel` values |
| `profiles/last_order/` | Last Order: persona (`SOUL.md`), MCP servers (`config.yaml`, `mcp/`), `skills/`, and `config.json` `{"model": "..."}` to pin her model |
| `profiles/sisters/<id>/` | one directory per Sister (`misaka create`): `DESCRIBE.md` for routing, `SOUL.md`, `config.json` to pin a model, `skills/` |
| `allies.json` | the recognised ally CLIs |

## Models and agent behaviour

| Variable | Default | Meaning |
|---|---|---|
| `MISAKA_PROVIDER` / `MISAKA_MODEL` | `settings.json`, then `anthropic` / `claude-sonnet-4-5` | provider and model for Sisters and chat |
| `MISAKA_LO_MODEL` | Last Order's pinned model, then `MISAKA_MODEL` | Last Order's model |
| `MISAKA_SUBAGENT_MODEL` | the parent's model | model for subagents a Sister spawns |
| `MISAKA_DISABLE_BACKGROUND_TASKS` | unset | disable subagent background launch and transitions |
| `MISAKA_AUTO_BACKGROUND_TASKS` | unset | move foreground subagents into the background after 120 seconds, after the child acknowledges its policy change |
| `MISAKA_FORK_SUBAGENT` | unset | opt-in interactive implicit Agent forks; `/agents fork <directive>` preserves native `/fork` navigation |
| `MISAKA_COORDINATOR_MODE` | unset | reserves coordinator ownership and disables implicit forks; does not enable an upstream coordinator implementation |
| `MISAKA_MANAGED_AGENTS_DIR` | unset | operator-managed agent definitions, highest definition precedence |
| `MISAKA_EFFORT_LEVEL` | unset | subagent effort payload fallback, independent of thinking mode |
| `MISAKA_DISABLE_AUTO_MEMORY` | unset | explicit true/false override for automatic agent memory |
| `MISAKA_SIMPLE` | unset | disable automatic agent memory in simple mode |
| `MISAKA_REMOTE` | unset | remote memory context; automatic memory requires an explicit remote memory directory |
| `MISAKA_REMOTE_MEMORY_DIR` | unset | remote-host agent-memory directory |
| `MISAKA_AGENT_MEMORY_SNAPSHOT` | unset | enable agent user-memory snapshot initialization checks; existing memory is not silently overwritten |
| `MISAKA_SUBAGENT_HOOKS_DISABLED` | internal | parent-to-child hook-disable fence; applies to frontmatter hooks too |
| `MISAKA_SUBAGENT_MANAGED_HOOKS_ONLY` | internal | parent-to-child managed-hook-only fence |
| `MISAKA_SMALL_FAST_MODEL` | the provider's own small model | model for cheap internal calls |
| `MISAKA_FORCE_MODEL` | none | overrides every model choice, card configuration included |
| `MISAKA_CACHE_RETENTION` | `short` | `long` asks the provider for long prompt-cache retention |
| `MISAKA_WEB_CONFIG` | `~/.misaka/web.json` | web search: backend choice, keyless tier, and vendor credentials |
| `MISAKA_WEB_CACHE` | `~/.misaka/cache/web` | where web_extract keeps page text for its TTL |
| `MISAKA_ALLOW_PRIVATE_URLS` | off | lets the web tools reach private and loopback addresses; cloud metadata endpoints stay blocked either way |

## Paths

Each is a directory or file MISAKA owns.

| Variable | Default | Meaning |
|---|---|---|
| `MISAKA_CODING_AGENT_DIR` | `~/.misaka/agent` | the engine directory: settings, auth and models (an explicit SDK engine home may also own sessions) |
| `MISAKA_DB` / `MISAKA_MESSAGES` | `~/.misaka/{board,messages}.db` | task board and message queue |
| `MISAKA_TASKS` | `~/.misaka/tasks` | per-card locks and read-only skill copies; no transcripts or research reports |
| `MISAKA_PROFILES` | `~/.misaka/profiles` | roles: personalities, skills, MCP config (created on first run) |
| `MISAKA_SESSIONS` | `~/.misaka/sessions` | every conversation: `<role>/<folder bucket>/`, with a card's under `cards/<id>/`, research conversations under `research/<run>--<scope>/`, nested agents beside their parent, or under `subagents/<parent>/` for in-memory parents |
| `MISAKA_PAGEINDEX` | `<cwd>/.pageindex` | unscoped library calls only; CLI and session tools always use `<workspace>/.pageindex` |
| `MISAKA_OFFICE_CACHE` | `~/.misaka/cache/office` | unscoped library cache; session tools use `<workspace>/.office-cache` |
| `MISAKA_OFFICE_INTENT` | `~/.misaka/office_intent` | unscoped library archive; session tools use `<workspace>/.office-intent` |
| `MISAKA_OCR_LANGS` | `eng+chi_sim+jpn` | tesseract language codes for scanned PDFs, joined with `+`; needs `ocrmypdf` on PATH (`brew install ocrmypdf`) |
| `MISAKA_WORKTREE_DIR` | `~/.misaka/worktrees` | git worktrees for isolated agents |
| `MISAKA_AGENT_MEMORY_HOME` | `~/.misaka/memory` | agent memory files |
| `MISAKA_NET_SOCK` / `MISAKA_NET_SNAPSHOT` | `~/.misaka/net.sock` / `net.json` | the panel daemon's socket and roster snapshot |
| `MISAKA_GHOSTTY_VT` | `misaka/ui/panel/lib/libghostty-vt.<dylib\|so>` | the terminal emulator behind every pane (libghostty-vt, herdr's; `misaka/ui/panel/lib/README.md` has the rebuild recipe) |
| `MISAKA_INPUT_HISTORY` | none — the feature is off unless set | file for persistent chat input history |
| `MISAKA_TELEMETRY` | unset — the `enableInstallTelemetry` setting decides (default on) | whether this install may be identified to an outside service; set at all (`0` included) and it wins over the setting |
| `MISAKA_TIMING` | `0` | `1` prints startup timings to stderr, grouped by namespace (`main`, `extensions`) |
| `MISAKA_MCP_CONFIG` | the profile's `mcp/` | MCP server configuration |
| `MISAKA_MCP_CACHE` | `~/.misaka/cache/mcp_schema_cache.json` | cached MCP tool schemas |

`misaka update` reports whether this install is behind the repository's `main` branch, and
`--apply` fast-forwards it. It follows the branch rather than release tags, the way Hermes
updates itself: a fast-forward or nothing. A checkout with uncommitted changes, one that has
commits `main` does not, or a detached HEAD is refused with the reason rather than resolved.
How the install was made is read from PEP 610 metadata, not guessed, so the command it offers
matches the tool that made it. Nothing polls: the check runs only when you ask.

A checkout asks git for the branch head and needs no token. Any other install shape asks the
GitHub API, which sees a private repository only with a credential: `GITHUB_TOKEN`, `GH_TOKEN`,
or whatever `gh` is signed in as, in that order. None is prompted for or stored, and without
one the check reports why it could not compare instead of failing the command.

`misaka uninstall` removes every path in this section, after listing what each holds and
what it costs. It refuses any path that resolves to your home directory or a filesystem
root, never touches a project folder, and prints the command for removing the package
itself rather than trying to remove the code it is running from. `--dry-run` lists and
stops; `--yes` skips the confirmation.

## Budget, concurrency and limits

| Variable | Default | Meaning |
|---|---|---|
| `MISAKA_TOKEN_CAP` | `0` (off) | token budget shown and enforced on the board |
| `MISAKA_BEAST_AT` | `0.85` | fraction of the cap at which a card drops to beast mode |
| `MISAKA_SUBAGENT_TOKEN_RESERVATION` | `32768` | tokens held back for a subagent |
| `MISAKA_TURN_TOKEN_LIMIT` | none | per-turn token ceiling |
| `MISAKA_MAX_CONCURRENT_SISTERS` | free memory / 256 MiB, 4–12 | cards running at once on this host |
| `MISAKA_MAX_CONCURRENT_PER_SISTER` | the host cap | cards one Sister runs at once |
| `MISAKA_MAX_CONCURRENT_SUBAGENTS` | host CPUs | subagents one session runs at once |
| `MISAKA_SUBAGENT_TOOL_CEILING` | none | comma-separated tools a subagent may not exceed |
| `MISAKA_TASK_MAX_OUTPUT` | `32000` (max `160000`) | characters of a subagent's output kept |
| `MISAKA_SKILL_COPY_CAP_MB` | `200` | size ceiling when copying a skill into a sandbox |
| `MISAKA_JUDGE_TIMEOUT` | `600` | seconds a research planner / judge call may take |
| `MISAKA_RESEARCH_PLAN_APPROVAL` | `1` (on) | a research node's plan waits for the user's go-ahead in conversation before its cards exist; `0` for unattended runs |
| `MISAKA_MCP_INIT_TIMEOUT` / `MISAKA_MCP_CALL_TIMEOUT` | `30` / `120` | seconds for MCP startup and per call |
| `MISAKA_MCP_REQUIRED_WAIT` | `30` | seconds to wait for a required MCP server |

## Terminal and panel

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

## Context engine

| Variable | Default | Meaning |
|---|---|---|
| `LCM_SUMMARY_MODEL` / `LCM_SUMMARY_FALLBACK_MODELS` | upstream LCM defaults | summary model overrides; provider routing lives in global `settings.json` under `auxiliary.compression` |
| `LCM_SUMMARY_TIMEOUT_MS` | upstream task timeout | milliseconds per summary; `auxiliary.<task>.timeout` uses seconds |
| `LCM_EMBEDDINGS_ENABLED` / `LCM_EMBEDDING_PROVIDER` / `LCM_EMBEDDING_MODEL` | upstream LCM defaults | semantic retrieval is explicit; install `lcm-semantic` for fastembed, then run `misaka lcm embed warmup` |
| `LCM_PROACTIVE_RECALL_ENABLED` | off | at context assembly, inject one budget-capped block of cross-session memories; needs `LCM_EMBEDDINGS_ENABLED` as well, and does nothing without it |
| `LCM_PREANSWER_EVIDENCE_ENABLED` | off | validate evidence at the `pre_llm_call` seam before the model answers |

LCM algorithm settings use the upstream `LCM_*` names, with no product aliases. Native
paths, authentication and `auxiliary` task settings belong to MISAKA. Provider routing
for summaries lives in the global `settings.json` under `auxiliary.compression`.

### Project cache lifetime

MISAKA LCM is the project-scoped fork of hermes-lcm. Its database and content sidecars
live in `<project>/.misaka/lcm/`. The session's project is fixed at startup and inherited
by its workers; changing a tool's directory does not change its LCM project. A native
`lcm-project` session metadata entry retains the owner project for resumed worktrees.

Each process holds a project storage lease. Closing/reloading one session keeps the
cache available to the project's other sessions. Once the last project runtime exits,
the host closes SQLite and background maintenance before removing the cache directory.
A killed process can leave an abandoned cache: the next owner clears it before reuse.
The sibling gate/lease files contain coordination metadata, not conversation content.
Do not synchronize, commit or back up the disposable `.misaka/lcm/` directory.

Native session JSONL, adopted checkpoints, Board state and project outputs are not
removed. Reopening a saved session ingests its originals and reconstructs its summary
frontier. Carry-over checkpoints reference source session entries and verify their
identity on reconstruction; source files must remain available. No global history is
automatically imported. Old global LCM caches and database-row-only carry receipts
have no compatibility fallback.

Storage is host-owned: `MISAKA_LCM_DB`, `LCM_DATABASE_PATH`,
`LCM_LARGE_OUTPUT_EXTERNALIZATION_PATH` and `LCM_EXTRACTION_OUTPUT_PATH` do not redirect
MISAKA's runtime. All algorithm options retain their upstream meanings. `misaka lcm`
uses the current project; import/backfill target paths must match that project's cache.
No permanent user-memory service is created by this cache.

### Optional features

Each is configured the upstream way: `LCM_LARGE_OUTPUT_EXTERNALIZATION_ENABLED`,
`LCM_TEMPORAL_ROLLUPS_ENABLED`, `LCM_EMBEDDINGS_ENABLED`, `LCM_PROACTIVE_RECALL_ENABLED`,
`LCM_PREANSWER_EVIDENCE_ENABLED`, and `LCM_ASSERTIONS_ENABLED` with
`LCM_ASSERTION_EXTRACTION_ENABLED`. Retrieval, preanswer, extraction and rollups keep
their individual upstream defaults and budgets. Semantic retrieval is explicit: install
the extra with `uv sync --extra lcm-semantic`, then run `misaka lcm embed warmup`.

Every one of them is off until you set it, and MISAKA overrides none of them. A fresh
install and one that has been running for months therefore read the same values; a `false`
in `misaka lcm status` is the shipped default, not something that turned itself off.

Proactive recall is gated twice. With embeddings off it returns before doing any work, so
setting `LCM_PROACTIVE_RECALL_ENABLED` on its own changes nothing and reports no error.
Configure an embedding provider and enable embeddings first.

`LCM_*` is the only way in. An `lcm` section in the global `settings.json` is read for
`context_threshold` alone (`compression.threshold` is the fallback); every other key under
it is ignored, and `misaka lcm status` lists what it ignored as
`ignored_config_yaml_lcm_keys`.

These features do not add an independent user-profile or long-term memory service.

### Operator commands

`misaka lcm status`, `doctor`, `backup`, `rotate`, `preset`, `embed`, `assertions` and
`rollups` use the original dispatcher and its command-specific grammar.
`misaka lcm import --help`, `externalize-backfill --help` and
`state-embedding-backfill --help` expose the original dedicated operators.

Backfill reads the database without rewriting old rows or changing history. It writes
sidecars and tracks them in an ownership manifest, and rollback removes only matching,
unreferenced files owned by that manifest. A dry run can write a dry-run manifest: that
does not mean every command has zero filesystem effect. Check each command's help.

The optional interactive `/lcm` command requires `LCM_ENABLE_SLASH_COMMAND=1`.

### Prompt caching

Every compaction rewrites the front of the context and so invalidates an Anthropic
prompt-cache prefix. `LCM_CACHE_FRIENDLY_CONDENSATION_ENABLED=1` keeps the engine from
also rebuilding its higher-level summaries in the same round as a leaf, which costs one
rewrite per compaction instead of two.

## Notes

A number that does not parse stops the command and names the variable and its value.

Every `MISAKA_*` name not in these tables is set by MISAKA for its own child processes.
A test in the development tree keeps that claim true in both directions.

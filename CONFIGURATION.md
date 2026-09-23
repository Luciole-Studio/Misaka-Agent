# Configuration

Every setting has a working default. This page is reference material: read it when you
want to change something, not before your first run. [README.md](README.md) covers
installing and running.

Files live under the home (`~/.misaka/`, see [Paths](#paths)). Environment variables override the files, and all of
them are optional.

## Files

| Where | What |
|---|---|
| `settings.json` | every setting: pi's (`defaultProvider` / `defaultModel` are the product defaults -- picking a model in the `/model` selector writes them; `/model <name>` only switches this session; `theme`, `compaction`, ...) and MISAKA's sections `allies`, `skills`, `moa`, `web`, `lcm`, `auxiliary` |
| `credentials/auth.json` | stored provider credentials (`/login`), kept at mode 0600 |
| `.env` | environment for code that is not MISAKA, loaded into every MISAKA process at start (dotenv syntax, mode 0600): web vendor keys (`misaka web set env.X` writes them here), the keys a skill's script expects, a plugin's knobs such as `LCM_*`, an SDK's `AZURE_OPENAI_*`. Never a `MISAKA_*` variable -- those are settings, or a parent's hand-off to a child, and the loader ignores them |
| `models.json` | custom providers and models (an OpenAI-compatible gateway, a local server); their IDs are valid `defaultModel` values |
| `profiles/last_order/` | Last Order: persona (`SOUL.md`), `settings.json` with what is hers alone -- `defaultProvider`/`defaultModel` (her pinned model), `mcpServers`, `web` -- her own `.env` (vendor keys and skill secrets that are hers, over the home's), and `skills/`, `subagents/` |
| `profiles/sisters/<id>/` | one directory per Sister (`misaka create`): `DESCRIBE.md` for routing, `SOUL.md`, the same `settings.json` and `.env`, `skills/`, `subagents/` |

## Models and agent behaviour

Everything here is a section of `settings.json`. No `MISAKA_*` environment variable sets a
knob: a `MISAKA_*` name in the environment is what a parent process hands a child (its role,
its card, its leases), and the `.env` loader ignores such names. Code that is not MISAKA
(plugins, SDKs, skills' scripts) reads its own variables from `.env`.

| Setting | Default | Meaning |
|---|---|---|
| `defaultProvider` / `defaultModel` | `anthropic` / `claude-sonnet-4-5` | provider and model for every role without a pin of her own; `/model` writes them |
| `profiles/<role>/settings.json` `defaultProvider` / `defaultModel` | none | that role's pinned model (Last Order's included); `/model` Ctrl+S in her window writes it |
| `subagents.small_fast_model` | the provider's own small model | model for cheap internal calls |
| `subagents.max_concurrent` | host CPUs | subagents one session runs at once |
| `subagents.task_max_output` | `32000` (max `160000`) | characters of a subagent's output kept |
| `subagents.effort_level` | `auto` | subagent effort payload, independent of thinking mode (`low`/`medium`/`high` or a number on Claude) |
| `subagents.background_tasks` | `true` | `false` disables subagent background launch and transitions |
| `subagents.auto_background_tasks` | `false` | move foreground subagents into the background after 120 seconds, after the child acknowledges its policy change |
| `subagents.coordinator_mode` | `false` | reserves coordinator ownership and disables implicit forks; does not enable an upstream coordinator implementation |
| `subagents.managed_agents_dir` | none | operator-managed agent definitions, highest definition precedence |
| `subagents.builtin_agents` | `true` | `false` hides the built-in agent types from non-interactive sessions |
| `subagents.agent_list_in_messages` | `false` | list the available agents in messages |
| `subagents.verification_agent` | `false` | offer the built-in `verification` agent |
| `subagents.auto_memory` | `true` | automatic agent memory (`autoMemoryEnabled` in the pi settings still applies) |
| `subagents.simple` | `false` | simple mode: no automatic agent memory |
| `subagents.memory_home` | `state/agent-memory` | where agent memory files live |
| `subagents.memory_snapshot` | `false` | agent user-memory snapshot initialization checks; existing memory is never silently overwritten |
| `subagents.inherit_process_group` | `false` | `true` keeps child agents in MISAKA's process group |
| `web.allow_private_urls` | `false` | lets the web tools reach private and loopback addresses; cloud metadata endpoints stay blocked either way |

Hand-offs a parent sets for its child, never for you to set: `MISAKA_SUBAGENT_*` (model, effort,
hooks fences, permissions), `MISAKA_FORK_SUBAGENT`, `MISAKA_REMOTE` / `MISAKA_REMOTE_MEMORY_DIR`,
`MISAKA_PROFILE_DIR` / `MISAKA_WHO` / `MISAKA_WORKSPACE`, the `MISAKA_USAGE_*` leases.
Variables the pi engine itself reads stay theirs: `MISAKA_CACHE_RETENTION` (`long` asks the
provider for long prompt-cache retention), `MISAKA_OAUTH_CALLBACK_HOST`.

## Paths

Everything MISAKA owns on this machine lives under one directory, the home. `MISAKA_HOME` moves
it (default `~/.misaka`); there is no per-path override. The layout is declared once, in
`misaka/config/home.py`.

```
~/.misaka/
  settings.json  models.json  keybindings.json  .env                 what you edit (.env: owner-only)
  MISAKA.md  skills/  themes/  prompts/  extensions/  subagents/     shared by every role
  profiles/          last_order/  sisters/<id>/     one directory per role
  credentials/       auth.json  vault/  mcp-auth/                    owner-only
  state/             board.db  messages.db  sessions/  tasks/  ...   what the program writes; back this up
  shared/            the one place an agent may create things of its own
  cache/  logs/      safe to delete
  run/               the panel daemon's socket, its roster snapshot, locks
```

Every setting is a section of `settings.json`, as in Hermes's one `config.yaml`: pi's own keys
(`defaultModel`, `theme`, `compaction`, ...) beside MISAKA's `allies` (the hand-launched ally
allow-list), `skills`, `moa`, `web`, `lcm` and `auxiliary`. A project's `.misaka/settings.json`
overlays it, and a role's `profiles/<role>/settings.json` overlays that -- but a role's file may
hold only what is the role's own: `defaultProvider`/`defaultModel` (the model she starts on),
`mcpServers` and `web`. Everything else a role session changes is written to the home's file,
because it is yours, not hers. Credentials are the one thing kept out of `settings.json`:
`credentials/auth.json` for providers (`/login`; OAuth tokens rotate, so they never live in a
hand-edited file), and `.env` for everything other code reads from the environment -- as Hermes
keeps `~/.hermes/.env`. Every MISAKA process loads the home's `.env` at start (the shell's own
variables win), and a role session lays `profiles/<role>/.env` over it (over the home's file,
under the shell): her own vendor keys, the secrets her skills asked for. `misaka web set env.X`
and a skill's credential prompt write there. What a skill's script actually receives still passes
the skills' environment policy (its blocklist and the `skills.env_passthrough` allow-list).

`state/sessions/` holds every conversation: `<role>/<folder bucket>/`, with a card's under
`cards/<id>/`, research conversations under `research/<run>--<scope>/`, nested agents beside
their parent, or under `subagents/<parent>/` for in-memory parents. A role directory keeps what
you edit flat (`SOUL.md`, `settings.json`, `.env`, `skills/`, `subagents/`); what the program writes for
that role follows the home's layout inside it (`cache/`, `logs/`). When the home is too deep for a unix socket path, the socket moves to
`/tmp/misaka-<uid>-<hash>/` on its own.

A project's own configuration is `<project>/.misaka/` (`settings.json`, `prompts/`, `themes/`,
`subagents/`, gated by project trust). The home is never a project: run from your home directory,
there is no project scope.

| Variable | Default | Meaning |
|---|---|---|
| `MISAKA_HOME` | `~/.misaka` | the home: every path above |
| `MISAKA_PAGEINDEX` | `<cwd>/.pageindex` | unscoped library calls only; CLI and session tools always use `<workspace>/.pageindex` |
| `MISAKA_GHOSTTY_VT` | `misaka/ui/panel/lib/libghostty-vt.<dylib\|so>` | the terminal emulator behind every pane (libghostty-vt, herdr's; `misaka/ui/panel/lib/README.md` has the rebuild recipe) |
| `MISAKA_INPUT_HISTORY` | none — the feature is off unless set | file for persistent chat input history |
| `MISAKA_TELEMETRY` | unset — the `enableInstallTelemetry` setting decides (default off) | whether this install may be identified to an outside service; set at all (`0` included) and it wins over the setting |
| `MISAKA_TIMING` | `0` | `1` prints startup timings to stderr, grouped by namespace (`main`, `extensions`) |
| `MISAKA_MCP_CONFIG` | none | a parent's hand-off: extra MCP servers for a child |

Document reading: `documents.ocr_langs` in `settings.json` (default `eng+chi_sim+jpn`) gives the
tesseract language codes for scanned PDFs, joined with `+`; it needs `ocrmypdf` on PATH
(`brew install ocrmypdf`).

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

| Setting | Default | Meaning |
|---|---|---|
| `research.token_cap` | `0` (off) | token budget shown and enforced on the board |
| `research.beast_at` | `0.85` | fraction of the cap at which a card drops to beast mode |
| `research.plan_approval` | `true` | a research node's plan waits for the user's go-ahead in conversation before its cards exist; `false` for unattended runs |
| `network.max_concurrent_sisters` | free memory / 256 MiB, 4–12 | cards running at once on this host |
| `network.max_concurrent_per_sister` | the host cap | cards one Sister runs at once |
| `skills.copy_cap_mb` | `200` | size ceiling when copying a skill into a sandbox |
| `mcp.init_timeout` / `mcp.call_timeout` | `30` / `120` | seconds for MCP startup and per call |
| `mcp.required_wait` | `30` | seconds to wait for a required MCP server |

Hand-offs, set by a parent for its child: `MISAKA_SUBAGENT_TOKEN_RESERVATION` (tokens held back
for a subagent), `MISAKA_TURN_TOKEN_LIMIT` (a card's per-turn ceiling), `MISAKA_SUBAGENT_TOOL_CEILING`.

## Terminal and panel

| Setting | Default | Meaning |
|---|---|---|
| `panel.prefix` | `ctrl+b` | the panel's prefix chord (`ctrl+g` under tmux, whose `ctrl+b` is taken) |
| `tui.esc_timeout_ms` | `10`, `100` under ssh or in a pane | how long a lone ESC waits for an Alt+key second byte |
| `showHardwareCursor` / `terminal.clearOnShrink` | off | pi's own; `MISAKA_HARDWARE_CURSOR=1` / `MISAKA_CLEAR_ON_SHRINK=1` are pi's environment fallbacks when the file does not name them |

Hand-offs the panel sets for its panes: `MISAKA_THEME` (`dark` / `light`), `MISAKA_APP_TITLE` /
`MISAKA_TAGLINE` (what a pane's header shows), `MISAKA_NET_PANE`, `MISAKA_INPUT_HISTORY`.
Escape hatches, never settings: `MISAKA_ALLOW_NESTED=1` opens the panel inside one of its own
panes; `MISAKA_GHOSTTY_VT` points at a libghostty-vt you built yourself.

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
live in `<project>/.misaka/lcm/`; a session whose directory is no project (the home is
never one) keeps them in the plugin's own directory, `state/plugins/misaka-lcm/lcm/`.
The session's project is fixed at startup and inherited by its workers; changing a
tool's directory does not change its LCM project. A native `lcm-project` session
metadata entry retains the owner project for resumed worktrees.

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

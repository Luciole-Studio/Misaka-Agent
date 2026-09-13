# MISAKA

A multi-agent research system for the humanities and social sciences. Last Order (the coordinator) breaks a research question into task cards; Sisters (worker agents) execute them in their own processes, and a normally settled run is recorded on its card automatically. Research products are written directly into the selected project, one folder per node: `nodes/<node>/` holds that node's plan, conclusion and review, and `nodes/<node>/cards/<card>/` each of its Sisters' outputs (the root node's folder is named by the run id); run-level products -- question, survey, draft, final report -- go to `final/<run>-<file>`. Evidence and extracted pages stay in that project too. When a card settles, a node closes, or the run ends, misaka writes a `SOURCES.md` beside the products and hard-links every file they cite into a `sources/` folder next to them (a copy only where linking is impossible; originals never move), so a folder holds a conclusion and what it rests on; these bundles are derived and are not registered, indexed, or committed. Git records optional history (one commit per closed node and one at the end); delivery requires no worktree or merge.

Before a node's plan becomes cards, it waits for the user: the root Last Order presents it in the user's window, a fork's in its own tab (in the panel the fork node process is that tab: its Last Order runs as an interactive window beside its parent's), the user and she talk it over, she revises it as needed, and she records the start once the user agrees; no keyword or approval command is involved; `MISAKA_RESEARCH_PLAN_APPROVAL=0` turns the wait off, and a command-line `misaka research` run (whose root has no conversation) runs unattended as a whole. A node may plan more than once: at its conclusion turn Last Order can send her Sisters out for another round instead of concluding, up to the run's follow-up limit (`--followups N`, default 2: how many more times after the first cards are back; talking a plan over with the user is never counted), and every round's plan waits for the user like the first; the conclusion is then written from all rounds, and the red team comes after it. A fork's first plan can instead be skipped at the user's decision (`misaka_research_skip`): the node closes unresearched and its issue stays parked for final adjudication with the reason on record. Research assignments become cards directly. Each Sister briefly outlines her approach in ordinary prose, then uses her tools and completes the work in that same task session; there is no separate preflight process, planning JSON handoff, or required planning file. Red-team cards follow the same rule at every depth. Dependencies, concurrency limits, evidence checks, and final submission still apply.

A red-team review with no material issues, or at the depth limit, is recorded in its node's existing Last Order conversation without a model turn. The driver closes the node and parks depth-limited issues for final adjudication; Last Order is asked to dispatch children only when there is research to assign.

Fork Last Orders keep one AgentSession throughout their node execution, including waits for Sisters and the red team. In the panel a fork node is a tab of its own: the node process runs its Last Order as an interactive window in a new tab beside its parent's (the same chat the root has, on the node's own session), its routine driven from inside that window, its Sisters gridded into that tab; what you type there is a turn of that very Last Order. The window stays open after the node closes, so you can go on asking her about what she found; closing it mid-run ends the node, and `/research resume` retries it. A command-line `misaka research` run has no panel, so its nodes are background processes; Sessions shows the model state separately from the node phase/depth, and opening such a live background conversation uses `misaka chat --attach --session PATH`: the normal conversation screen and editor send input directly to the original owner, without another model or writer. Enter steers a busy session or starts a turn in an idle one; `/pause` holds its next request/tool/workflow boundary and `/resume` releases it. Already running tools and other agents are not stopped. Closing an attached window only detaches it. Completed sessions open as saved history; they do not silently restart a worker. Original Sister card tabs and research stop/resume retain their own lifecycle.

## Install

MISAKA is not on PyPI (the name `misaka` there is an unrelated package); install it from the repository.

```sh
# a user install, with the provider SDKs you talk to: anthropic / openai / google / bedrock / mistral
pip install "misaka[anthropic] @ git+https://github.com/Luciole-Studio/Misaka-Agent.git"
#   misaka[providers] = all five; misaka[pageindex] = PDF outline extraction; misaka[browser] = browser tools
# or from a checkout:
git clone https://github.com/Luciole-Studio/Misaka-Agent.git && cd Misaka-Agent && pip install ".[anthropic]"
# development:
uv venv .venv --python 3.13 && uv sync   # product, every provider SDK, PDF outline extraction, test tooling
```

Then run the wizard: `misaka setup`. It checks the environment (git, ripgrep, fd, pdftotext), stores a
provider credential and default model and sends one test request, creates the first Sisters, offers the
PDF outline extra, pins a web-search backend if you have a key, and initializes a project folder. Each
section can be re-run alone (`misaka setup model`). A bare `misaka` with no credential configured starts the
wizard by itself.

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
`misaka web setup` provides the provider/tier picker and hidden credential prompts;
`--profile DIR` writes `DIR/web.json` over shared defaults. `misaka web --help` covers
discovery, enable/disable, reload and current limits.

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

Paths — each is a directory or file MISAKA owns:

| Variable | Default | Meaning |
|---|---|---|
| `MISAKA_CODING_AGENT_DIR` | `~/.misaka/agent` | the engine directory: settings, auth and models (an explicit SDK engine home may also own sessions) |
| `MISAKA_DB` / `MISAKA_MESSAGES` / `MISAKA_LCM_DB` | `~/.misaka/{board,messages,lcm}.db` | task board, message queue, compaction store |
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

Budget, concurrency, and limits:

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

Context engine:

| Variable | Default | Meaning |
|---|---|---|
| `LCM_SUMMARY_MODEL` / `LCM_SUMMARY_FALLBACK_MODELS` | upstream LCM defaults | summary model overrides; provider routing lives in global `settings.json` under `auxiliary.compression` |
| `LCM_SUMMARY_TIMEOUT_MS` | upstream task timeout | milliseconds per summary; `auxiliary.<task>.timeout` uses seconds |
| `LCM_EMBEDDINGS_ENABLED` / `LCM_EMBEDDING_PROVIDER` / `LCM_EMBEDDING_MODEL` | upstream LCM defaults | semantic retrieval is explicit; install `lcm-semantic` for fastembed, then run `misaka lcm embed warmup` |

LCM algorithm settings use the upstream `LCM_*` names, without product aliases.
`misaka lcm import --help`, `misaka lcm externalize-backfill --help` and
`misaka lcm state-embedding-backfill --help` expose the original operator grammars.
Backfill reads the DB without changing history and tracks sidecars in an ownership manifest.


#### The context engine

Long sessions use the pinned [hermes-lcm](https://github.com/stephenschoettler/hermes-lcm)
policy, with the complete plugin tree in `misaka/extensions/hermes_lcm/vendor/` and
registered native-host adaptations in `host/`. LCM owns triggering, fresh-tail
selection, chunking and summary assembly. MISAKA stores the adopted full replay in
its ordinary session checkpoint; original session entries remain append-only.
Derived history and summaries use `~/.misaka/lcm.db` and the fifteen `lcm_*` tools.
LCM's configured redaction, ignore, retention and explicit GC policies still apply;
this is not a promise of unconditional permanent raw retention.

Algorithm settings use the upstream `LCM_*` names, without old product aliases.
Native paths, authentication and `auxiliary` task settings belong to MISAKA.
`misaka/extensions/hermes_lcm/PORT_NOTES.md` records the source adaptations,
known upstream fixes and remaining host differences. Source coverage is not a claim
that every Hermes host behavior has been reproduced.

#### Optional context features

Use each feature's upstream configuration: `LCM_LARGE_OUTPUT_EXTERNALIZATION_ENABLED`,
`LCM_TEMPORAL_ROLLUPS_ENABLED`, `LCM_EMBEDDINGS_ENABLED`, and
`LCM_ASSERTIONS_ENABLED` / `LCM_ASSERTION_EXTRACTION_ENABLED`. Retrieval, preanswer,
extraction and rollups retain their individual upstream defaults and budgets.
The semantic extra supplies fastembed: `uv sync --extra lcm-semantic`.
These features do not add an independent user-profile or long-term memory service.

#### Operator commands

`misaka lcm status`, `doctor`, `backup`, `rotate`, `preset`, `embed`, `assertions`
and `rollups` use the original dispatcher and its command-specific grammar.
`misaka lcm import --help`, `externalize-backfill --help` and
`state-embedding-backfill --help` expose the original dedicated operators.
Historical externalization reads the DB without rewriting old rows; it writes
sidecars and ownership manifests, and rollback only removes matching, unreferenced
files owned by that manifest. A dry run can write a dry-run manifest: it does not
mean that every command has zero filesystem effects. Check each command's help.
The optional interactive `/lcm` command requires `LCM_ENABLE_SLASH_COMMAND=1`.

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
in the development tree keeps that claim true in both directions.

## Diagnose

`/debug` in the chat writes the rendered screen and the whole conversation to
`~/.misaka/agent/misaka-debug.log` (mode 0600) and prints the path. That is the only
diagnostic switch — there are no debug environment variables.

## Check

This distribution ships the `misaka/` package only. The test suite and the `make check`
gate it runs behind (tests, `-W error`, compileall, import sweep, wheel build) live in the
development tree and are not included here.

`misaka/core/documents/pageindex/flash` is vendored from [PageIndex](https://github.com/VectifyAI/PageIndex) (MIT); see `UPSTREAM.md` there.

### Sub-agent management and compatibility

`/agents list` includes shadowed definitions grouped by source. `/agents create
<user|role|project> <name>`, `/agents edit <name>`, and `/agents delete <name>`
manage file-backed definitions through the native editor. Create/edit also accept
`--file PATH`; delete accepts an explicit `--yes`. Existing files are addressed by
actual discovered path and checked for concurrent edits. Built-in, plugin,
managed and JSON-only definitions remain read-only in this editor.

`/agents memory <agent-id> [status|replace|keep]` inspects or explicitly resolves
project memory snapshot updates. Replacement requires confirmation and retains
other local files; active agents using the same memory must finish first.

`MISAKA_AGENT_LIST_IN_MESSAGES=1` opts into the upstream catalog-delta behavior:
the Agent description stays static, and filtered types are announced separately.
Compaction reconstructs the list; mid-turn changes use a deterministic request-local
projection rather than rewriting an active transcript. The default remains off.
`MISAKA_SUBAGENT_LIVE_PERMISSIONS` is an internal parent/child protocol capability,
set by the parent, not a user permission override. Children refresh inherited
allow/ask/deny settings before tool admission. `permissions.additionalDirectories`
are resolved before crossing working-directory boundaries; protected paths,
plan-mode restrictions and the native role tool ceiling still apply.

Network MCP supports `headersHelper` (10-second timeout, JSON string headers),
static headers and WebSocket IDE `authToken`. Helpers receive
`MISAKA_MCP_SERVER_NAME` and `MISAKA_MCP_SERVER_URL`; project/local helpers require
project trust, including headless runs. This does not imply that the upstream
OAuth login lifecycle, Teams/CCR or cross-type task registry has been ported.

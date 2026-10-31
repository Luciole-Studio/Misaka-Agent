# Configuration reference

Every setting has a working default, so you can skip this page until you want to change
something. Most settings also have a command that writes them for you (`/model`, `misaka web`,
`misaka setup`); editing the files by hand is always allowed.

## Where settings live

MISAKA keeps what is yours, across projects, in one folder, the **home**: `~/.misaka/` (set
`MISAKA_HOME` to move it). A project folder holds only what belongs to that project.

| File | What it holds |
|---|---|
| `~/.misaka/settings.json` | every setting on this page |
| `~/.misaka/.env` | API keys for web services and skills, and other environment variables (owner-only) |
| `~/.misaka/credentials/auth.json` | provider sign-ins from `/login` (owner-only; don't edit by hand) |
| `~/.misaka/models.json` | providers and models the built-in catalog does not know, such as a local server |
| `~/.misaka/keybindings.json` | your changes to the chat box's keys |
| `~/.misaka/MISAKA.md` | the identity every agent shares |
| `~/.misaka/profiles/last_order/` | Last Order's own files |
| `~/.misaka/profiles/sisters/<id>/` | one folder per Sister |
| `<project>/.misaka/` | the project's own settings (`settings.json`) and its conversation-search cache |
| `<project>/.pageindex/` | the project's document index |

The rest of the home:

```text
~/.misaka/
  skills/  skill-bundles/  subagents/  themes/  prompts/  extensions/   shared by every role
  state/        the task board, messages, conversations, backups: back this up
  shared/       the one place agents may keep material of their own
  cache/  logs/ safe to delete
  run/          the panel's socket and locks
```

### Which file wins

MISAKA's own sections on this page (`research`, `network`, `subagents`, `skills`, `mcp`,
`documents`, `panel`, `tui`, `allies`, `moa`, `lcm`, `auxiliary`, `web`) are read from
`~/.misaka/settings.json`. A Sister's `web` overrides in her own `settings.json` are the one
exception.

The model and display settings (`defaultProvider`, `defaultModel`, `defaultThinkingLevel`,
`modelThinkingLevels`, `theme`, `terminal.*` and the like) are read in layers, later ones
winning:

1. `~/.misaka/settings.json`
2. the project's `.misaka/settings.json`, once you have trusted the project (`/trust`)
3. the role's `profiles/<role>/settings.json`

MISAKA writes only what is that role's own into a role's file: `defaultProvider` /
`defaultModel`, `defaultThinkingLevel` and `web`. A role's MCP servers have a file of their own,
`profiles/<role>/mcp.json` (see [Skills and MCP](#skills-and-mcp)).

A role folder may also have its own `.env`, laid over the home's. A variable already set in
your shell wins over both files.

A value of the wrong type (text where a number belongs) stops MISAKA at start and names the
setting; `misaka setup`, `update` and `uninstall` still run so you can fix it.

### Schemas for your editor

MISAKA ships JSON schemas for `settings.json`, `models.json`, `keybindings.json` and themes, so an
editor can complete keys and flag mistakes as you type. Name one in the file's `$schema` key (MISAKA
ignores the key itself):

```json
{ "$schema": "https://raw.githubusercontent.com/Luciole-Studio/Misaka-Agent/vX.Y.Z/misaka/schemas/settings.schema.json" }
```

Put your version (`misaka --version`) in place of `X.Y.Z`; the other files are
`models.schema.json`, `keybindings.schema.json` and `theme.schema.json`. The same files are in the
installed package, under `misaka/schemas/`, and the built-in themes name theirs. In `models.json`
the key must be text: any other value makes the file invalid.

## Models and thinking

| Setting | Default | Meaning |
|---|---|---|
| `defaultProvider` / `defaultModel` | none | the model every role uses unless she has her own; `misaka setup model` writes these. Without them, a chat takes a signed-in provider's own default model, and Sisters' cards fall back to `anthropic` / `claude-sonnet-4-5`. |
| `defaultThinkingLevel` | `medium` | the thinking level a new session starts at, for every role without her own |
| `modelThinkingLevels` | none | a thinking level per `provider/model` |
| `vision.model` | `browser.vision_model`, if set | one vision model (`provider/model`) for the whole home, not per role: it reads every image a request carries to a model that cannot see, and a model that can see is sent the image itself |

In a role's own `settings.json`, `defaultProvider` / `defaultModel` pin that role's model and
`defaultThinkingLevel` sets her thinking level. In her window, `/model` with Ctrl+S and
`/thinking --default LEVEL` write them; so do `misaka create --model` and `--thinking` for a new
Sister. [Models](../guide/models.md) has the everyday commands.

A session's thinking level comes from the first of: a `--thinking` flag (or a `:level` suffix on
`--model`); for a conversation you resume, the level it last had; the role's own default;
`modelThinkingLevels`; the home's `defaultThinkingLevel`; `medium`. It is then limited to what
the model supports.

| Setting | Default | Meaning |
|---|---|---|
| `enabledModels` | none | the models Ctrl+P cycles through, in the same form as `--models` |

When several providers offer a model of the same name, `--model NAME` takes the one of them you
have a key or a sign-in for; when that is more than one, or none, or with `--api-key`, it stops and
lists them, and you write `provider/model` (or add `--provider`) instead. A name in
`enabledModels` or `--models` that several providers offer adds each of them, with a warning.

With an OpenRouter key, MISAKA lists only the OpenRouter models, limits and prices that key allows:
it asks OpenRouter when it refreshes the model list (at the `/model` list, for example) and keeps the
answer in `state/models-store.json`, which Sisters' cards read. A pinned OpenRouter model that the key
may not use is then unknown, and the error says so. MISAKA sends nothing to any other catalog service.

`models.json` is checked as a whole: one wrong value, a `contextWindow` or `maxTokens` of `0`, or a
known `compat` setting of the wrong type, such as `"supportsMidConvoSystemMessages": "yes"`, makes
MISAKA ignore the file and show the error. Unknown `compat` keys are kept. A `kimi-coding` base URL
in `models.json` must end in `/v1`.

## Research, budget and concurrency

| Setting | Default | Meaning |
|---|---|---|
| `research.plan_approval` | `true` | every research plan waits for your go-ahead; `false` for unattended runs (a plan that changes the question still waits) |
| `research.token_cap` | `0` (no cap) | token budget of one research run, its cards and sub-agents included; a run that reaches it stops with a partial report and goes on when resumed under a higher cap |
| `research.beast_at` | `0.85` | share of the cap at which a card is told to wrap up |
| `network.max_concurrent_sisters` | free memory ÷ 256 MiB, between 4 and 12 | cards running at once on this machine |
| `network.max_concurrent_per_sister` | the machine limit | cards one Sister runs at once |
| `allies` | none | other vendors' agents that can take cards: see the [team guide](../guide/team.md#allies) |

## Sub-agents

Sisters can hand work to sub-agents. These settings rarely need changing.

| Setting | Default | Meaning |
|---|---|---|
| `subagents.max_concurrent` | `20` | sub-agents one session runs at once |
| `subagents.task_max_output` | `32000` (max `160000`) | how much of a background task's output, in characters from the end, the output tool shows; the whole output stays in its file |
| `subagents.effort_level` | `auto` | `low`, `medium`, `high`, `xhigh` or `max`, for models that take an effort setting |
| `subagents.small_fast_model` | none | the model a sub-agent type's hooks use when they name none (a model of the same provider) |
| `subagents.background_tasks` | `true` | allow sub-agents to run in the background |
| `subagents.auto_background_tasks` | `false` | move a foreground sub-agent to the background after two minutes |
| `subagents.verification_agent` | `false` | offer the built-in `verification` type |
| `subagents.agent_list_in_messages` | `false` | list the available types in messages |
| `subagents.auto_memory` | `true` | let sub-agents keep memory between tasks |
| `subagents.memory_home` | `state/agent-memory` | where that memory is kept |
| `subagents.managed_agents_dir` | none | a folder of sub-agent definitions that take precedence over all others |

### Approvals

Whether a tool call asks first works as in Claude Code: every session (Last Order's, a Sister's,
a card, a contact turn) runs in a permission mode, and its sub-agents inherit it.

| Setting | Default | Meaning |
|---|---|---|
| `permissions.defaultMode` | unset: `bypassPermissions` | the mode sessions start in: `bypassPermissions` (nothing asks), `default` (commands, edits and other tools ask), `acceptEdits` (edits in the project do not), `plan` (only reading), `dontAsk` (what would ask is refused) |
| `permissions.disableBypassPermissionsMode` | none | `"disable"` keeps sessions out of `bypassPermissions`; an unset `defaultMode` is then `default` |
| `permissions.allow` / `ask` / `deny` | none | rules such as `"Bash(npm run:*)"` or `"Read(/path/**)"`; `deny` holds in every mode |
| `permissions.additionalDirectories` | none | folders besides the project that tools may work in |

What asks shows a dialog in a window you are at: **Yes**, **Yes, and don't ask again …** (a
command's rule saved in the project's `.misaka/settings.json`, or a folder or edits granted for
this session), **No**. A session nobody watches, such as a card or a contact turn, refuses what
would ask. `/agents mode <mode>` changes a Sister's live session; `auto` needs the classifier that
runs in sub-agents, so a top-level session cannot use it.

## Tools and extensions

| Setting | Default | Meaning |
|---|---|---|
| `defaultTools` | `read`, `bash`, `edit`, `write`, `office`, `grep`, `find`, `ls` | the built-in tools a new session starts with |
| `extensions` | none | extension files to load, and `+`/`-` entries that turn built-in extensions on or off |
| `quietStartup` | `false` | `"header"` keeps the header and hides the startup details (model scope, loaded resources); `true` hides both. `--verbose` shows everything |

`defaultTools` takes a list of tool names, which replaces the default set, or a list of only
`+name` and `-name` entries, which changes it: `["+codemode", "-ls"]` adds `codemode` and drops
`ls`. A file later in the [layers](#which-file-wins) with a list of only `+`/`-` entries changes the
selection the earlier files made; a list with a plain name replaces it. `--tools` takes the same two
forms for one run; a list of names there is also every tool that run may use. `/reload` activates
tools a changed `defaultTools` newly adds; a tool it removes stays on until the next session.

Every extension MISAKA ships is a built-in extension, on by default:

| Extension | What it adds |
|---|---|
| `builtin:mcp` | the MCP servers below |
| `builtin:codemode`, `builtin:tool-search` | the two tools below, for the Sisters |
| `builtin:llama.cpp` | the `/llama` command and the llama.cpp provider |
| `builtin:misaka_lcm` | [LCM](#long-conversations-lcm): compaction of long conversations, and the `lcm_*` tools |
| `builtin:vision` | images read to a model that cannot see them, by the `vision.model` above |
| `builtin:coverage` | `coverage_scan` and the coverage maps |
| `builtin:reading` | [reading mode](../guide/reading.md), in Last Order's window |

`-builtin:<name>` in the home's `extensions` setting turns one off, for example
`"extensions": ["-builtin:llama.cpp"]`; a trusted project's own `extensions` setting may turn it
back on with `+builtin:<name>` or off for that project only. `-e builtin:<name>` loads one for a
single run even when the setting turns it off, and `--no-extensions` leaves them all out. Turning
one off takes it out of sessions; its terminal command (`misaka mcp`, `misaka lcm`, `misaka read`)
still works. A skill an extension brings is named after it, such as `builtin:coverage:coverage_maps`.

## Skills and MCP

| Setting | Default | Meaning |
|---|---|---|
| `skills.external_dirs` | none | more folders of skills to read (`~/.agents/skills` is always read when it exists) |
| `skills.disabled` | none | skill names to hide from every role |
| `mcp.required_wait` | `30` | seconds a sub-agent waits for an MCP server its definition requires |

MCP servers are configured in `mcp.json` files, in the `mcpServers` shape other MCP clients use:

1. `~/.misaka/profiles/last_order/mcp.json`, Last Order's servers
2. `~/.misaka/profiles/sisters/<id>/mcp.json`, one Sister's servers
3. the project's `.misaka/mcp.json`, once you have trusted the project, for every role

Each role connects the servers of her own file and the project's, and no other role's. The home has
no `mcp.json`; a session that belongs to no role uses Last Order's file. An entry in the project file
replaces the role's entry of the same name. `misaka mcp add` writes to Last Order's file, to a
Sister's with `--as <id>`, or to the project's with `-l`; `misaka mcp list` connects and checks the
servers that role gets. `/mcp` in a window manages that window's role ([commands](commands.md)).

A project entry without `command`, `url` or `type` changes only `enabled`, `exposure` and
`toolExposure` of the server of that name from the role's file, and keeps the rest of it,
`env`, `headers` and `auth` included. This turns a server off in one project:

```json
{ "mcpServers": { "internal-tools": { "enabled": false } } }
```

In a trusted project, `/mcp` offers *Enable in this project* and *Disable in this project* for the
servers of the role's own file. They write such an entry, and later changes to that server go to it.

```json
{
  "mcpServers": {
    "filesystem": { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."] },
    "docs": { "url": "https://example.com/mcp", "headers": { "Authorization": "Bearer ${DOCS_TOKEN}" } }
  }
}
```

| Key | Meaning |
|---|---|
| `command`, `args`, `env`, `cwd` | a server started as a process, speaking on its standard streams; `~` expands, `cwd` is relative to the working directory |
| `url`, `headers` | a Streamable HTTP server. Without an `Authorization` header it signs in with OAuth when the server asks (`/mcp login`, `misaka mcp login`); `oauth` holds a pre-registered client (`clientId`, `clientSecret`, `callbackUrl`, `callbackPort`, `scope`, `clientName`, `authServerMetadataUrl`); `auth: {"provider": "..."}` sends a `/login` provider's token instead (a role's file only) |
| `exposure` | how the model reaches the tools: `codemode` (from codemode scripts; the default for a Sister's servers), `deferred` (declared once `tool_search` loads them), `direct` (declared like built-in tools; Last Order's servers are always reached this way) or `hidden` |
| `toolExposure` | the exposure of single tools, by name or `*` pattern |
| `description` | one line about what the server offers; the model sees it |
| `enabled` | `false` keeps the entry without connecting it |
| `timeout` | seconds per request, `60` by default |

Values may use `${VAR}` for an environment variable and `!command` for a command's output, read when
the server connects. Each server's tools are named `mcp__<server>__<tool>`. The top-level
`autoEnableCodemode` (default `true`) activates the codemode tool when a `codemode` server connects.

Each role keeps her sign-ins in her own folder, in `credentials/mcp-auth.json`. What servers log goes to
`~/.misaka/logs/mcp/mcp.log`.

### Codemode

With the `codemode` tool the model writes a JavaScript script that calls the other tools, so it can
run calls in parallel and filter large results before it reads them. MCP tools reach the model this
way by default. Codemode is a Sister's tool: Last Order coordinates and does not have it, so every tool
of her MCP servers is declared to her directly, whatever `exposure` says (`hidden` still hides it). For a Sister the tool is off
until something names it: `defaultTools` (`["+codemode"]`) or an MCP server with `codemode` exposure.
Scripts run in a QuickJS sandbox with no
file system, network or timers; they call tools through `tools.<name>(args)`. The reference the
model reads is shipped with MISAKA as `codemode.md`.

| Setting | Default | Meaning |
|---|---|---|
| `codemode.mode` | `on` | how the other tools are presented while codemode is active: `on` keeps them declared and tells the model how scripts call them; `only` hides them and lists them in the codemode description instead |
| `codemode.inlineBudget` | `3000` | estimated tokens the codemode description may spend on tool declarations; tools that do not fit are found with `searchTools()` |

### Tool search

The `tool_search` tool lets the model find tools that are not declared to it up front and load the
ones it needs for its next request. MCP servers with `deferred` exposure reach the model this way,
and `tool_search` is turned on for them. Like codemode, it is a Sister's tool; Last Order does not have it. Otherwise it is off
until something names it:
`defaultTools` (`["+tool_search"]`) or `--tools`. It searches the names and descriptions of the
deferred tools; a tool it loads is then called like any other.

## Documents and the web

| Setting | Default | Meaning |
|---|---|---|
| `documents.ocr_langs` | `eng+chi_sim+jpn` | languages for reading scanned PDFs, joined with `+` (needs `ocrmypdf` and each language's data) |
| `web.*` | search works with no setup | set with `misaka web`; see [Documents and the web](../guide/sources.md#the-web) |
| `web.allow_private_urls` | `false` | let web tools reach private and loopback addresses; cloud metadata addresses stay blocked |

## Panel and terminal

| Setting | Default | Meaning |
|---|---|---|
| `panel.prefix` | `ctrl+b` | the panel's prefix key, `ctrl+` and a letter (use `ctrl+g` inside tmux, which takes `ctrl+b`); read when the panel starts |
| `tui.esc_timeout_ms` | `10`; `100` over ssh or inside a pane | how long Esc waits to see whether it starts an Alt+key |
| `showHardwareCursor` | `false` | use the terminal's own cursor instead of the one MISAKA draws; MISAKA still places it where input methods need it |
| `terminal.clearOnShrink` | `false` | clear the screen when the window shrinks |
| `sessionShareDestination` | `auto` | where `/share` uploads: `radius`, `gist` (a private GitHub gist), or `auto`, which uses Radius when you are signed in to it and a gist otherwise. `radius` never falls back to a gist: without a Radius sign-in, `/share` says so and stops |

Chat also tells a terminal that supports the
[Program Status Protocol](https://www.superlogical.com/rex/docs/build/program-status) (OSC 7501)
what it is doing: `working` during a run or a compaction, `blocked` while a dialog or a sign-in
waits for you, `done`, `error` (with the error's first line) and `idle`. A report carries at most
the conversation's name or the dialog's title, never your prompts or the model's output. It is sent
only after the terminal answers the protocol's query; tmux and screen do not pass it on.
`MISAKA_PROGRAM_STATUS` changes that.

### Themes

`theme` names the color theme: `dark`, `light`, a theme of your own, or `LIGHT/DARK` (two names) to
follow the terminal's background. A theme of your own is a file `~/.misaka/themes/NAME.json` whose
`name` is `NAME`; copy the built-in `dark.json` or `light.json` (in the installed package, under
`misaka/modes/interactive/theme/`) to start from. A color is `#RRGGBB` or `#RGB`, a palette index
from 0 to 255, `""` for the terminal's own color, or the name of a color under `vars`.

Theme files are strict: only `$schema`, `name`, `appearance`, `vars`, `colors` and `export` are
allowed at the top, and only the known color names under `colors` and `export`. Define colors of
your own under `vars` and use them by name; any other key makes the theme invalid. MISAKA's own
`sectionTitle`, `appTitle` and `appTitleOnLight` (the startup screen's section titles, and the agent's
name in a "MISAKA · NAME" startup title, on dark and on light backgrounds) may be left out; they then
take `accent`, `text` and `text`.
An invalid theme is reported when MISAKA starts and at `/reload`, and is not used. `theme.schema.json`
(see [Schemas for your editor](#schemas-for-your-editor)) lists every color.

## Long conversations (LCM)

When a conversation grows too long for the model, LCM summarises its older part, and the agent
can still search what was said earlier: in her own conversation,
and in the other conversations running in the same project. Your conversations themselves are
kept in `~/.misaka/state/sessions/`; the search index is a cache in `<project>/.misaka/lcm/`,
filled from the conversations as they open and removed when the last MISAKA process in the
project exits. Don't commit or sync it. A project on a drive that cannot keep that folder
private, such as a Windows drive opened from WSL, keeps it in
`~/.misaka/state/plugins/misaka-lcm/projects/` instead.

| Setting | Default | Meaning |
|---|---|---|
| `lcm.context_threshold` | `0.5` | share of the context window at which a session compacts; a research run can set its own. Unset, it is `compression.threshold`, whose default is Hermes' `0.5` (with `"compression": {"enabled": false}`, LCM's own `0.35`) |
| `auxiliary.compression` | the session's model | which provider and model write the summaries |

To do without LCM, add `"-builtin:misaka_lcm"` to `extensions`. A conversation LCM has already
compacted keeps its summaries, and later compactions use MISAKA's own summary instead; with LCM
back on, it takes the conversation up again.

`/lcm-settings` in chat turns automatic recall from earlier conversations on or off. Recall
searches by meaning, so it needs the `lcm-semantic` extra; turning it on picks a small local model
for that.

Other behaviour is switched with `LCM_*` variables in `~/.misaka/.env`, for example:

| Variable | What it turns on |
|---|---|
| `LCM_EMBEDDINGS_ENABLED` | semantic search over the history. Needs the `lcm-semantic` extra and a model (`LCM_EMBEDDING_PROVIDER`, `LCM_EMBEDDING_MODEL`); `/lcm-settings on` picks a small local one. |
| `LCM_PROACTIVE_RECALL_ENABLED` | bring relevant memories from other conversations into context; needs embeddings as well |
| `LCM_LARGE_OUTPUT_EXTERNALIZATION_ENABLED` | store very long tool outputs outside the summaries, retrievable on demand |
| `LCM_TEMPORAL_ROLLUPS_ENABLED` | summaries of the history by day, week and month |
| `LCM_CONTEXT_THRESHOLD` | the compaction threshold, over `lcm.context_threshold` |


## Environment variables

`~/.misaka/.env` is for variables that code other than MISAKA reads: provider keys such as
`ANTHROPIC_API_KEY`, web service keys (`misaka web set env.NAME` writes them), keys a skill's
script expects, an SDK's settings, and `OPENALEX_API_KEY` (a free OpenAlex key that keeps
literature scans working when OpenAlex limits anonymous use). A line that does not parse, or a
file that is a symbolic link, disables the whole file, and the warning says why. Names starting
with `MISAKA_` are ignored there: set those in your shell.

Variables for MISAKA itself:

| Variable | Default | Meaning |
|---|---|---|
| `MISAKA_HOME` | `~/.misaka` | where the home is |
| `MISAKA_TELEMETRY` | off | `1` lets MISAKA tell gateways such as OpenRouter that the requests come from MISAKA (`enableInstallTelemetry` in `settings.json` does the same) |
| `MISAKA_OAUTH_CALLBACK_HOST` | `127.0.0.1` | the address a browser sign-in returns to, for signing in on a remote machine |
| `MISAKA_SETUP_PLAIN` | off | `1` makes the setup wizard ask numbered questions instead of drawing menus |
| `MISAKA_TIMING` | `0` | `1` prints startup timings |
| `MISAKA_CODEMODE_MAX_WORKERS` | `2` | codemode workers alive at once across all sessions of one MISAKA process (each Sister runs in a process of its own). Each worker's script may use up to 256 MB. Further scripts wait for a free worker |
| `MISAKA_GHOSTTY_VT` | the bundled library | a terminal library you built yourself, for platforms without a bundled one |
| `MISAKA_ALLOW_NESTED` | off | `1` lets the panel open inside one of its own panes |
| `MISAKA_PROGRAM_STATUS` | ask the terminal | `1` always sends program status reports (OSC 7501), `0` never does |
| `MISAKA_CLIPBOARD_OSC52` | off | `1` also sends copied text to the terminal with OSC 52 after the local clipboard write, as MISAKA does over SSH: for a remote client that reaches MISAKA without SSH variables, such as a multiplexer attached from another machine or a remote IDE |
| `MISAKA_TUI_WRITE_LOG` | off | a file, or a folder, that gets a copy of everything written to the terminal; for tracking down display problems |
| `MISAKA_TUI_DEBUG_REDRAW` | off | `1` records why each full redraw happened, in `pi-tui-debug.log` in the home |
| `MISAKA_TUI_DEBUG` | off | `1` writes the state of every screen update to `/tmp/tui` |
| `GITHUB_TOKEN` / `GH_TOKEN` | none | a GitHub token for `misaka update` (it also asks `gh auth token`): for a private copy of the repository, or a higher rate limit |

Other `MISAKA_*` names are set by MISAKA for its own processes; leave them alone.

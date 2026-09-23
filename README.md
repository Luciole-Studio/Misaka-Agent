<h1 align="center">
  <img src="assets/logo.svg" alt="" width="96" height="96"><br>
  MISAKA
</h1>

<p align="center"><strong>A multi-agent research system for the humanities and social sciences.</strong></p>

<p align="center">English · <a href="README.zh-CN.md">简体中文</a> · <a href="README.ja.md">日本語</a></p>

You give it a research question. It breaks the question into assignments, runs a
team of agents against them in parallel, has a red team attack the result, and
writes the findings into your project folder next to the sources they rest on.

It is built for work where the answer has to be defensible: every conclusion ships
with a `SOURCES.md` and a folder of the exact files it cites.

```sh
misaka init                              # make this folder a project
misaka doc add sources/                  # index the PDFs you already have
misaka research "How did the Japanese public library movement change between 1920 and 1950?"
```

That last command opens a conversation, not a progress bar. Read on for why.

<p align="center">
  <img src="assets/setup.png" alt="misaka setup — environment checks and provider configuration" width="820">
</p>

## How a run works

Two roles do the work.

| Role | What it does |
|---|---|
| **Last Order** | The coordinator. Turns a question into a plan, then into task cards, then writes the conclusion. |
| **Sisters** | The workers. Each takes one card and runs it in its own process with its own tools. |

A run moves through five steps.

1. **Plan.** Last Order drafts an approach and shows it to you.
2. **Plan approval.** Bare `/research` asks you to choose **Require approval** or
   **Automatic** after depth, LO parallelism, Sister cards per LO and follow-ups.
   Require approval waits for your go-ahead in an ordinary conversation with Last Order;
   Automatic proceeds after an accepted plan, while genuine clarification still needs input.
   The choice is saved for this run, its forks and follow-up rounds, including resume.
   `research.plan_approval` in `settings.json` supplies the default (true; false for automatic).
   Direct `/research QUESTION` and command-line `misaka research` use that default;
   the CLI prints how to attach to its resident root conversation when a plan waits.
3. **Cards.** The plan becomes task cards. Sisters pick them up and work in parallel,
   up to the run's per-LO Sister card limit (`--sister-parallel`, default 4). Each Sister
   outlines her approach in ordinary prose and then does the work in that same session. There is no separate planning
   file or JSON handoff to fill in.
4. **More rounds, if needed.** Instead of concluding, Last Order can send her Sisters
   out again. `--followups N` caps how many extra rounds she gets after the first cards
   come back, default 2. Talking a plan over with you is never counted against it.
   Every round uses the same plan-approval mode as the first.
5. **Conclusion and red team.** The conclusion is written from all rounds. A red team
   then attacks it. A review that finds no material issue, or that hits the depth
   limit, is recorded without spending a model turn.

A branch you do not want researched can be dropped at your decision. The node closes
unresearched, and the reason stays on record for final adjudication.

The root Last Order stays in the same session from planning through the global
report draft, independent red-team review, and final adjudication. `/research`
reuses your chat; command-line Research owns a headless root until the driver ends.
Only fork nodes get separate LO processes. A paused or failed run keeps its saved
conversation for resume; final reporting never falls back to a one-shot LO.

Ordinary and Research sessions load the same role base: shared `MISAKA.md`, role
`SOUL.md`, and role duties. Research adds its mode-specific rules through the shared
prompt assembly; foreground versus headless changes presentation and lifecycle,
not the identity prompt. Tool guidance still follows the actual available tools.

Research has two independent concurrency settings: `--parallel N` limits LO nodes
(default 4), while `--sister-parallel N` limits active Sister cards per LO (default 4),
including multiple sessions of the same Sister. Both are saved with the run and
reused on resume and in fork nodes; old runs without the Sister setting keep 4.
Global/per-Sister admission limits and task dependencies can reduce actual concurrency.
These are card slots, not a count of all windows or nested subagents.
For example, `/research --parallel 4 --sister-parallel 8 QUESTION` (or
`misaka research --parallel 4 --sister-parallel 8 "QUESTION"`). Bare `/research`
lets you choose both in the options picker.

## What lands on disk

Products are written into the project you selected, one folder per node.

```
your-project/
├── nodes/<node>/              plan, conclusion, review
│   ├── cards/<card>/          each Sister's output
│   ├── SOURCES.md             what the conclusion rests on
│   └── sources/               the cited files themselves
└── final/<run>-<file>         question, survey, draft, final report
```

The `sources/` folders are hard links, so they cost nothing and the originals never
move. Where linking is impossible a copy is made instead. These bundles are derived:
they are not registered, indexed, or committed.

Git history is optional and cheap: one commit when a node closes and one at the end
of the run. Delivery needs no worktree and no merge.

## Install

MISAKA is not on PyPI. The name there belongs to an unrelated package. Install from
this repository.

```sh
# pick the provider SDKs you actually talk to
pip install "misaka[anthropic] @ git+https://github.com/Luciole-Studio/Misaka-Agent.git"

# or from a checkout
git clone https://github.com/Luciole-Studio/Misaka-Agent.git
cd Misaka-Agent && pip install ".[anthropic]"

# development
uv venv .venv --python 3.13 && uv sync
```

Extras: `anthropic`, `openai`, `google`, `bedrock`, `mistral`, or `providers` for all
five. `pageindex` adds PDF outline extraction, `browser` adds the browser tools.

Two things MISAKA will not install for you:

- **git** is required. `misaka init` creates the project repository and accepted
  results are committed into it. Install it with `xcode-select --install` or
  `apt install git`.
- **ripgrep** and **fd** back the `grep` and `find` tools. Install them with
  `brew install ripgrep fd` or `apt install ripgrep fd-find`, or drop the binaries
  into `~/.misaka/cache/bin`.

## First run

```sh
misaka setup
```

The wizard checks your environment, stores a provider credential and default model
and sends one test request, creates your first Sisters, offers the PDF extra, pins a
web-search backend if you have a key, and initializes a project folder. Each section
can be re-run alone, for example `misaka setup model`. Running a bare `misaka` with
no credential configured starts the wizard by itself.

`misaka update` says whether this install is behind the repository's `main`
branch, and `--apply` fast-forwards it. It follows the branch rather than release
tags, and refuses rather than resolves a checkout it cannot fast-forward.

`misaka uninstall` is the other end of it: it removes everything under `~/.misaka`
(credentials, the board, the context engine's memory, caches) after showing you what
goes and what it costs. Your project folders are never touched, and it lists them to
say so. Removing the package itself is your installer's job, and the command is printed.

A fresh install talks to `anthropic` / `claude-sonnet-4-5`. To set a credential by
hand:

```sh
export ANTHROPIC_API_KEY=sk-ant-...   # honoured for every builtin provider
misaka auth check                     # per-provider, through the resolver sessions use
```

Inside a chat, `/login` stores an OAuth token or API key in `~/.misaka/credentials/auth.json`
at mode 0600. `/model` opens the model selector; picking from it saves the choice as
the default for every Sister, while `/model <name>` switches only the session in front
of you.

## Commands

```sh
misaka                 # the panel, or plain chat when piped
misaka chat            # talk to Last Order
misaka research "..."  # start a research run
misaka board           # the task board
misaka doc add x.pdf   # index a document
misaka create          # add a Sister
misaka web status      # active search backend and credentials
```

`misaka --help` lists the rest: `task`, `tell`, `dm`, `net`, `skills`, `bundles`,
`moa`, `lcm`, `auth`, `remove`, `uninstall`, `update`.

## The panel

<p align="center">
  <img src="assets/tui.png" alt="the misaka panel — spaces, sessions and the Sisters roster beside an interactive Last Order" width="820">
</p>

Running `misaka` in a terminal opens a multi-pane panel. A research branch that forks
gets its own tab: the node process runs its Last Order as an interactive window there,
with her Sisters gridded in beside her. What you type in that tab is a turn of that
Last Order. The window stays open after the node closes, so you can keep asking her
about what she found. Closing it mid-run ends the node, and `/research resume` retries.

A command-line run has no panel, so its nodes are background processes. Open one with
`misaka chat --attach --session PATH` and your input goes to the original owner
directly, with no second model in between. Enter steers a busy session or starts a turn
in an idle one. `/pause` holds the session at its next request, tool, or workflow
boundary and `/resume` releases it. Tools and agents already running are not stopped,
and closing an attached window only detaches it.

## Documents and the web

`doc_add`, `doc_find`, `doc_read`, `doc_outline`, `doc_page_image` and `doc_verify`
give agents a corpus they can cite from and quote-check against. `misaka doc` is the
same thing from the shell.

Web search has a no-key fallback. Configure all Web tools with the same interactive
settings menu from either entrypoint:

```sh
misaka web                 # interactive menu in a terminal; status when piped
misaka setup web           # the identical menu, also included in full setup
misaka web --profile ~/.misaka/profiles/sisters/10032  # this Sister's overrides
misaka web status          # local configuration/readiness, not a network test
```

The menu covers separate search/extraction providers, free/paid/automatic tiers,
provider enable/disable, hidden credentials, browser connections and engines,
proxy/TLS, cache, blocklists, timeouts, xAI/X search, vault settings, OAuth accounts,
and optional tool installation. Provider choices come from the live registry,
including explicitly loaded trusted extensions (`--extension PATH`). Login, install,
and online account checks require an explicit choice; opening the menu sends no
requests and writes nothing. Confirmed edits save individually, so cancelling later
does not undo earlier saves.

Vendor keys are written with mode 0600 to `~/.misaka/.env` only when saved; the rest of the web configuration goes to `settings.json`.
Profile edits do not copy shared secrets; removing an override reveals shared values.
An exported variable always wins over files, including an empty export. Automatic
routing is not a promise of free-only service: existing credentials take precedence.
The existing `misaka web setup <provider>`, `set`, `unset`, and other scripting commands
remain available; `misaka web --help` lists them.

Agents also get the ordinary working tools: `bash`, `read`, `write`, `edit`, `grep`,
`find`, `web_fetch`, `download_file`, and `office` for `.docx`, `.xlsx` and `.pptx`.

## The context engine

Long sessions use **MISAKA LCM**, the project-scoped fork of pinned
[hermes-lcm](https://github.com/stephenschoettler/hermes-lcm). Compression and retrieval
algorithms remain unchanged. Each project's agents share `<project>/.misaka/lcm/`;
other projects use separate caches. The last owner removes the cache on exit, and the
next startup clears any abandoned cache after an interrupted run.

Native session entries and adopted checkpoints remain durable. Reopening a session
rebuilds its LCM context; carry-over source sessions must remain available. Cleanup
never removes native session history, Board state or project deliverables.

LCM's configured redaction, ignore, retention and GC policies still apply. This is not
a promise of unconditional permanent raw retention. Algorithm settings keep the
upstream `LCM_*` names with no product aliases, and `misaka lcm --help` exposes the
original operator grammars. Source coverage is not a claim that every upstream host
behaviour has been reproduced; `misaka/extensions/misaka_lcm/PORT_NOTES.md` records
what differs.

## Configuration

Everything lives under one home, `~/.misaka/` (`MISAKA_HOME` moves it). MISAKA's own switches are sections of `settings.json`; `.env` holds what other code reads from the environment (vendor keys, a plugin's knobs).

| Where | What |
|---|---|
| `settings.json` | every setting, as sections: pi's own keys (`defaultProvider`, `defaultModel`, ...), `allies`, `skills`, `moa`, `web` |
| `credentials/auth.json` | stored credentials, mode 0600 |
| `models.json` | custom providers and models, such as an OpenAI-compatible gateway |
| `MISAKA.md`, `skills/`, `subagents/` | what every role shares: identity, skills, sub-agent types |
| `profiles/last_order/` | Last Order's persona, own skills and `settings.json` (model pin, `mcpServers`) |
| `profiles/sisters/<id>/` | one directory per Sister, same layout |

The most useful few settings:

| Setting | Default | Meaning |
|---|---|---|
| `defaultProvider` / `defaultModel` | `anthropic` / `claude-sonnet-4-5` | provider and model for every role without a pin of her own |
| `network.max_concurrent_sisters` | free memory / 256 MiB, 4–12 | cards running at once on this host |
| `research.token_cap` | `0`, off | token budget shown and enforced on the board |
| `research.plan_approval` | `true` | whether a plan waits for your go-ahead |
| `lcm.context_threshold` | `0.35` | the share of the context window at which LCM compacts |

**[CONFIGURATION.md](CONFIGURATION.md) documents every setting**, grouped by what it
controls. A value that does not fit stops the command and names the setting and value.
`MISAKA_*` names in the environment are what MISAKA sets for its own child processes, never
settings; `MISAKA_HOME` alone is yours.

## Diagnose

`/debug` in a chat writes the rendered screen and the whole conversation to
`~/.misaka/logs/misaka-debug.log` at mode 0600 and prints the path. That is the only
diagnostic switch. There are no debug environment variables.

## Built on

MISAKA's kernel is a Python port of [pi](https://github.com/earendil-works/pi), and its
panel is a port of [herdr](https://github.com/herdrdev/herdr). It vendors
[hermes-lcm](https://github.com/stephenschoettler/hermes-lcm) for context management,
[PageIndex](https://github.com/VectifyAI/PageIndex) for PDF structure, and
[ghostty](https://github.com/ghostty-org/ghostty)'s VT library as the terminal
emulator behind every pane.

[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) is the full index: what came from
where, at which commit, and what was changed.

## Licence

[Apache License 2.0](LICENSE). Third-party components keep their own licences, all
recorded in THIRD_PARTY_NOTICES.md.

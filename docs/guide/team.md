# The team

MISAKA has two kinds of agent. **Last Order** is the coordinator: she frames questions with you,
plans, assigns work, reads what comes back and writes the conclusions. The **Sisters** are
specialists: each takes an assignment, chooses her own methods, does the work and reports back.
You create the Sisters; Last Order comes with the install.

## Creating and removing Sisters

```sh
misaka create 10032 --desc "History and social research: archives, periodicals, oral history"
misaka create 10036 --desc "Econometrics and causal identification" --model PROVIDER/MODEL --thinking high
misaka remove 10036
```

In a terminal, `misaka create` asks for whatever you leave out: the ID, her specialty, a model of
her own and her thinking level (Enter on the last two keeps the team's defaults).

- `--desc` is her specialty, written to her `DESCRIBE.md`.
- `--model` pins her model, as `provider/model` or a bare model ID. A bare ID offered by several
  providers is taken from your default provider; if that one lacks it, the command lists the
  candidates and stops.
- `--thinking` sets her default thinking level (`off` to `max`; see [Models](models.md#thinking-levels)).

In Last Order's window, `/create` does the same with menus. `misaka setup sisters` creates
Sisters from the setup wizard, numbering them from 10032.

`misaka remove` (or `/remove` in Last Order's window) deletes her profile after asking you
(`--yes`, or `/remove ID!`, skips the question). Her past cards, conversations and workspaces
stay. A Sister with unfinished cards can be removed once they are finished, reassigned or
deleted.

Two Sisters are enough to start, since one can red-team the other. Give them different
specialties: Last Order picks Sisters by fit, and a critic works best when she sees the question
from somewhere else.

## A Sister's profile

Each role is a folder in `~/.misaka/profiles/`: `last_order/` for Last Order, `sisters/<id>/` for
each Sister.

| File | What it does |
|---|---|
| `DESCRIBE.md` | Her specialty. Last Order reads it to decide who gets what. |
| `SOUL.md` | Her personality and voice, optional. It shapes how she talks and thinks; her duties stay the same. |
| `settings.json` | What is hers alone: her model (`defaultProvider` / `defaultModel`), `defaultThinkingLevel`, her MCP servers (`mcpServers`) and `web` overrides. |
| `.env` | Keys that are hers, laid over the home's `.env`. |
| `skills/` | Skills only she sees. |
| `skill-bundles/` | Named sets of skills saved for her (`misaka bundles save`). |
| `subagents/` | Sub-agent types only she can start. |

MCP servers are set per role, in that role's own `settings.json`, in the shape Hermes uses:

```json
"mcpServers": {"camofox": {"command": "npx", "args": ["-y", "camofox-mcp"]}}
```

`/mcp` in her window lists the servers and their tools.

A server starts with a small part of MISAKA's environment: `PATH`, `HOME`, the locale, the proxy
and certificate variables, the settings of npm, uv and pip (a registry mirror reaches `npx` and
`uvx`) and the like, never an API key or a registry's token. A key it needs goes in its `env`, where
`${NAME}` takes the value from the environment (or the home's `.env`):

```json
"mcpServers": {"github": {"command": "npx", "args": ["-y", "github-mcp"], "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}}}
```

What every role shares lives in the home: `~/.misaka/MISAKA.md` (the shared identity),
`~/.misaka/skills/` and `~/.misaka/subagents/`. The [configuration reference](../reference/configuration.md)
has the full layout.

## How a prompt is put together

Every agent's system prompt is assembled in the same order, whether she is chatting with you,
working a card, or running inside a research run:

1. MISAKA's own opening: who the agents are and how the two roles relate.
2. `MISAKA.md`, the identity every role shares. MISAKA writes two lines into it on first use; the
   rest is yours. Put the conventions you want everyone to keep here.
3. The role's `SOUL.md`, if it has one.
4. The shared working agreement: stay within the agreed scope, report what was actually done,
   keep evidence, inference, interpretation and uncertainty apart, and the research and reasoning
   norms that go with them.
5. The role's charter, the coordinator's or the Sister's, which holds her duties whatever her
   `SOUL.md` says.
6. The tools available in this session, and the rules for using them.
7. Instructions: the first of `PROJECT.md`, `AGENTS.md` or `CLAUDE.md` found in the home, then in
   the project folder and each folder above it.
8. The working directory.
9. The index of skills she can use.

A research run adds a section of its own: orchestration rules for Last Order, working rules for
the Sisters.

## Choosing models

Each Sister can run on a different model, so a team can mix, say, a Claude coordinator with GPT
and Gemini specialists. In an agent's own window, `/model` and then Ctrl+S makes the highlighted
model hers; `misaka setup model` sets the team's default. [Models](models.md) covers signing in,
local models and thinking levels.

## Talking to one agent

- `/sister` lists the roles. In the panel, `/sister 10032` opens a conversation with Sister 10032
  in a pane beside you. In plain chat it switches this window to her (after asking;
  `/sister 10032!` skips the question), and `/sister last-order` switches back to a new Last Order
  conversation. The conversation you leave stays saved.
- `misaka chat --as 10032` starts a chat with her from the shell. Her conversations are kept per
  folder: `-c` continues the latest, `--pick` chooses one.
- `misaka dm 10032 "MESSAGE"` delivers a message to her **contact session**, a standing
  conversation for messages, and runs one turn there. Without a message it delivers what is
  already waiting for her.

Agents message each other with the `SendMessage` tool, one recipient per message:

- **a name** inside a research run means that Sister's card on the sender's node (else in the
  run), and `last-order` the Last Order of the sender's node. Otherwise it reaches that role's
  session in the sender's panel space, or her contact session when she has none open there. If
  several are open, the sender is shown them and picks one;
- **a card ID** (`t_3cfb45`) reaches the session working that card. A card that has not started
  gets the message when it does. A finished card is woken: the message is her next turn in her
  own conversation (in her window, if it is still open), she answers, and completes again. Waking
  her does not send the cards built on her work back to be redone;
- **a session ID** reaches that live session; a finished card's session, open or closed, wakes
  its card.

A contact session's answer goes back to the window that wrote to it. Last Order can wait for a
woken Sister's answer with `misaka_sister_output` (`block: true`) before she goes on.

A Sister who needs a decision she can't make herself asks Last Order this way, and her card waits
for the answer. Last Order brings the question to you when it is yours to decide.

## Cards and the board outside research

In ordinary chat, Last Order can split a request into cards for the Sisters. She lays the cards
out and waits for your go-ahead before starting work that costs money. Cards are Markdown files in
the project's `cards/` folder, each with its dependencies. `/board` or `misaka board` shows them;
`misaka task add TITLE --to SISTER --body-file FILE` adds one by hand, `misaka task start ID`
runs a ready one in the terminal, and `misaka task ID --delete` removes one with its history. Research runs use the same board and
cards.

In the panel, each card she starts opens in a pane beside her, in her tab. A finished card's pane
stays so you can read its last output, and Last Order closes it once she has the results. She
leaves alone panes still working a card (stopping one takes your confirmation), panes you opened,
and everything outside her own tab.

## Skills and sub-agents

A skill is a folder with a `SKILL.md`, in the [agentskills](https://agentskills.io) format.
Skills come from the project's `skills/`, `.agents/skills/`, `.claude/skills/`,
`.codex/skills/` and `.grok/skills/` folders, then the role's, then the home's, then
`~/.agents/skills` and configured `skills.external_dirs`. Project skills take precedence;
independent copies with the same name require an exact indexed `source` path.
`misaka skills` lists, reviews, approves and installs
them, including an optional catalog that ships with MISAKA. `misaka bundles` saves a set of skills
under one name for a role.

`skill_manage` updates an existing skill at the same source that `skill_view` reads.
Each operation accepts `source` with the exact indexed `SKILL.md` path to select a
particular copy. The write result lists the directories actually changed. A new
skill is still created in the role's own `skills/` folder. Selecting a source does
not export a skill or synchronize independent host copies, and a project edit does
not update host directories in the user's home. Those directories must be configured
as external roots to select their existing skills. If two copies have the same
relative directory name, edit them in separate calls.

Sisters can hand work to sub-agents. The built-in research types are `explorer` (finds things in
the indexed documents and on the web), `reader` (reads one section closely and quotes it
verbatim), `verifier` (checks quotations against the source text) and `general` (odd jobs, in the
Sister's own persona); `Explore` and `Plan` are for work on code. Add your own types in
`subagents/`: in the home, in a role's folder, or in a trusted project's `.misaka/subagents/`.
`/agents` in a Sister's window lists the types and the running sub-agents, and creates, edits or
deletes types. Last Order coordinates, so she has no sub-agents of her own.

## Allies

Allies are other vendors' coding agents, Claude Code and Codex, taking cards from the board the
way the Sisters do. Last Order gives an ally cards (with your go-ahead), messages it, stops it and
reads its results as she would a Sister's. In a research run an ally can take research, red-team
and divergence-review cards; a card's reviewer is always a Sister.

An ally runs in a pane of the panel, connected through the
[Agent Client Protocol](https://agentclientprotocol.com). The pane shows its turns as they happen.
Type a line there to send it to the ally; Ctrl-C cancels the turn that is running, and the card
carries on. The ally works with its card's tools as a Sister does, so its deliverables, findings
and conversation are recorded the same way. It reads messages between turns: mail to a busy ally,
and a line typed in its pane, go in as its next turn. Its usage counts against the vendor's quota,
outside MISAKA's token budget.

Enable allies in `settings.json`:

```json
"allies": {"claude": {}, "codex": {}}
```

`claude` and `codex` run through `npx` (install Node.js) and use the login this machine already
has. Codex runs with full access: no approval prompts, network on. Any other agent that speaks
the Agent Client Protocol can be added with its own command:

```json
"allies": {"opencode": {"command": ["opencode", "acp"], "description": "What it is good at."}}
```

`misaka allies` lists the enabled allies and whether each one can start, without spending any
quota. An ally is reached by its running card; it has no contact session.

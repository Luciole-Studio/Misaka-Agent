# The team

MISAKA has two kinds of agent. **Last Order** is the coordinator: she frames questions with you,
plans, assigns work, reads what comes back and writes the conclusions. The **Sisters** are
specialists: each takes an assignment, chooses her own methods, does the work and reports back.
You create the Sisters; Last Order comes with the install.

## Creating and removing Sisters

```sh
misaka create 10032 --desc "History and social research: archives, periodicals, oral history"
misaka create 10036 --desc "Econometrics and causal identification" --model PROVIDER/MODEL
misaka remove 10036
```

With no arguments, `misaka create` asks for the ID, the specialty and the model. `--desc` is
written to her `DESCRIBE.md`. `--model` pins her model: give it as `provider/model`, or as a bare
model ID on your default provider. An unknown or ambiguous model stops the command before
anything is written, and an ambiguous one comes with the candidates. In chat, `/create` does the
same with a menu of your default provider's models. `misaka remove` deletes her profile, her
sessions and her workspace after asking you (`--yes` skips the question). `misaka setup sisters`
creates Sisters from the setup wizard, numbering them from 10032.

Two Sisters are enough to start, since one can red-team the other. Give them specialties that
differ; Last Order picks Sisters by fit, and the red team works best when the critic sees the
question from somewhere else.

## A Sister's profile

Each role is a folder in `~/.misaka/profiles/`: `last_order/` for Last Order, `sisters/<id>/` for
each Sister.

| File | What it does |
|---|---|
| `DESCRIBE.md` | Her specialty, for routing. Last Order reads every Sister's `description` line and the start of the body to decide who gets what. Last Order has none. |
| `SOUL.md` | Her personality and voice. It shapes how she talks and thinks; it cannot remove her duties. Optional. |
| `settings.json` | What is hers alone: `defaultProvider` / `defaultModel` (her pinned model), `mcpServers`, and `web` overrides. |
| `.env` | Vendor keys and skill secrets that are hers, laid over the home's `.env`. |
| `skills/` | Skills only she sees. |
| `subagents/` | Sub-agent types only she can start. |

An MCP server entry has the shape Hermes uses:

```json
"mcpServers": {"camofox": {"command": "npx", "args": ["-y", "camofox-mcp"]}}
```

What every role shares lives in the home itself: `~/.misaka/MISAKA.md` (the shared identity),
`~/.misaka/skills/` and `~/.misaka/subagents/`. [CONFIGURATION.md](../../CONFIGURATION.md)
has the full layout.

## How a prompt is put together

Every agent's system prompt is assembled in the same order, whether she is chatting with you,
working a card, or running inside a research run:

1. MISAKA's own opening: who the agents are and how the two roles relate.
2. `MISAKA.md`, the identity every role shares. It is created on first use with two lines, and
   never overwritten afterwards; put the conventions you want everyone to keep here.
3. The role's `SOUL.md`, if it has one.
4. The shared working agreement: stay within the agreed scope, report what was actually done,
   keep evidence, inference, interpretation and uncertainty apart, and the research and reasoning
   norms that go with them.
5. The role's charter, the coordinator's or the Sister's. A `SOUL.md` cannot replace it.
6. The tools available in this session, and the rules for using them.
7. Project instructions: the first of `PROJECT.md`, `AGENTS.md` or `CLAUDE.md` found in the
   project folder, and in each folder above it.
8. The working directory.

A research run adds its own section: orchestration rules for Last Order, card discipline for the
Sisters. It never changes the identity above.

## Choosing models

`/model` opens the model selector. Picking a model there makes it the default for every role
that has no pin of her own. In a role's own window, Ctrl+S in the selector pins the model for her
instead. `/model NAME` switches only the session in front of you.

Each Sister can run on a different provider, so a team can mix, say, a Claude coordinator with
GPT and Gemini specialists. `/login` stores OAuth tokens and API keys in
`~/.misaka/credentials/auth.json`; providers the catalog does not know go in
`~/.misaka/models.json`. `misaka moa` sets up Mixture-of-Agents presets, which appear as models of
their own.

## Talking to one agent

- `/sister` lists the roles; `/sister 10032` switches your window to Sister 10032 (her own
  session; the current conversation is not copied), and `/sister last-order` switches back.
- `misaka chat --as 10032` starts a chat with her from the shell.
- `misaka dm 10032 "MESSAGE"` delivers a message to her contact session and runs one turn.
- Agents message each other with the `SendMessage` tool; a Sister stuck on a decision only you
  can make asks for it the same way. `misaka tell` sends a message from inside a running card.

## Cards and the board outside research

In ordinary chat, Last Order can split a request into task cards for the Sisters. She lays the
cards out and waits for your go-ahead before starting work that costs money. Cards are Markdown
files in the project's `cards/` folder, and their dependencies are part of each card. `/board` or
`misaka board` shows them; `misaka task --delete ID` removes one with its history. Research runs
use the same board and the same cards.

## Skills and sub-agents

A skill is a `SKILL.md` folder in the [agentskills](https://agentskills.io) format. Skills resolve
from the project's `skills/` folder, then the role's, then the home's, then external sources; the
first match wins. `misaka skills` lists, reviews, approves and installs them, including the
optional catalog that ships with MISAKA and installs on request. `misaka bundles` saves a set of
skills under one name for a role.

Sisters can hand work to sub-agents. The built-in research types are `explorer` (finds things in
the indexed documents and on the web), `reader` (reads one section closely and quotes it
verbatim), `verifier` (checks quotations against the source text) and `general` (odd jobs, in the
Sister's own persona). Add your own types in `subagents/`, in the home or in a role's folder.
Last Order has no sub-agents: her work is coordination, and the Sisters do the research.

## Allies

Allies are other agent programs, such as Codex, Claude Code or Gemini CLI, run in panes of the
panel. Last Order launches them with a command line you allow in the `allies` list of
`settings.json`, and their replies arrive in the same mailbox as the Sisters'.

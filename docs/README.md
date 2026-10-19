# MISAKA documentation

Find the page for what you want to do. If you have not installed MISAKA yet, start with
[Getting started](getting-started.md).

## Start here

| I want to | Read |
|---|---|
| install MISAKA and run my first research question | [Getting started](getting-started.md) |
| run research, approve plans, follow a run, find the report | [Research runs](guide/research.md) |
| create Sisters, talk to one, add Claude Code or Codex to the team | [The team](guide/team.md) |
| find my way around the panel: tabs, panes, keys, saved conversations | [The panel](guide/panel.md) |
| sign in to a provider, pick models, use a local model | [Models](guide/models.md) |
| give agents my PDFs and books, set up web search | [Documents and the web](guide/sources.md) |
| read materials with an agent, rebuild and compare readings | [Reading materials](guide/reading.md) |
| fix something that went wrong | [Troubleshooting](guide/troubleshooting.md) |
| look up a command | [Commands](reference/commands.md) |
| change a setting | [Configuration](reference/configuration.md) |
| update or uninstall | [Getting started: updating and uninstalling](getting-started.md#updating) |

## Words you will meet

| Word | Meaning |
|---|---|
| **Last Order** | the coordinator. You talk to her; she plans the research, hands out the work and writes the conclusions. |
| **Sister** | a specialist agent you create, known by an ID such as `10032`. Each has her own specialty and skills, and can have her own model. |
| **ally** | another vendor's agent (Claude Code, Codex) that takes work from Last Order the way a Sister does. |
| **card** | one assignment for one Sister (or ally), written as a Markdown file in the project's `cards/` folder. |
| **board** | the list of the project's cards, with their status (`/board`). |
| **research run** | everything that happens for one question, from the first plan to the final report. |
| **node** | one piece of research inside a run: your question itself, or an alternative that an earlier node passed over, researched on its own. Each node has its own plan, conclusion and review. |
| **red team** | the Sister who challenges a node's conclusion, then reads it again for the possibilities it did not take and the gaps it left. Last Order has to answer every objection. |
| **research graph** | the nodes of a run and how they connect: which node opened which, which were joined, and how their conclusions relate. `final/<run>-graph.md` draws it. |
| **project** | the folder you run MISAKA in. Research output is written there. |
| **home** | `~/.misaka/` (or `MISAKA_HOME`), where MISAKA keeps your settings, Sisters, credentials and history. |
| **panel** | the terminal interface that opens when you type `misaka`: Last Order's window with the agents' panes beside it. `ctrl+b` then `?` lists its keys; [the panel guide](guide/panel.md) explains it. |

## All pages

- [Getting started](getting-started.md) (also in [简体中文](getting-started.zh-CN.md) and [日本語](getting-started.ja.md)): install, first run, first research question, updating and uninstalling
- Guides: [research runs](guide/research.md), [the team](guide/team.md), [the panel](guide/panel.md),
  [models](guide/models.md), [documents and the web](guide/sources.md),
  [reading materials](guide/reading.md) (also in [简体中文](guide/reading.zh-CN.md)),
  [troubleshooting](guide/troubleshooting.md)
- Reference: [commands](reference/commands.md), [configuration](reference/configuration.md)
- [Third-party notices](../THIRD_PARTY_NOTICES.md): what MISAKA is built on, and under which licences

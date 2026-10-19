<p align="center"><img src="assets/banner.jpg" alt="MISAKA — designed by Luciole Studio" width="100%"></p>

<h1 align="center">
  <img src="assets/logo.svg" alt="" width="96" height="96"><br>
  MISAKA
</h1>

<p align="center"><strong>Pairing a DAG with symptomatic reading to unearth plural narratives: an AI agent architecture built for the humanities and social sciences.</strong></p>

<p align="center"><em>More important than presence is absence! says Misaka Misaka, rapping you on the head.</em></p>

<p align="center">
  <a href="LICENSE"><img alt="Licence: Apache 2.0" src="https://img.shields.io/badge/licence-Apache_2.0-blue"></a>
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-3776AB">
  <img alt="macOS, Linux and Windows" src="https://img.shields.io/badge/runs_on-macOS_%7C_Linux_%7C_Windows-555">
</p>

<p align="center">English · <a href="README.zh-CN.md">简体中文</a> · <a href="README.ja.md">日本語</a></p>

Break the question down with **Last Order**, the coordinator, and refine the research plan; send
each branch of the inquiry to **Sisters**, specialists who report, liaise and consult; gather
their reports, read them and write; defend the conclusion before the reviewers; and turn whatever
it cannot cover into new research. Graph algorithms manage the graph of research branches, and
the final report arrives in your project folder as a research article, every citation traceable.

<p align="center">
  <img src="assets/tui.png" alt="The MISAKA panel: spaces, sessions and agents beside Last Order's window" width="820">
</p>

## Core design

<table>
<tr><td><b>Specialists invoked like skills</b></td><td>Last Order calls on a Sister the way an agent invokes a skill. Skills load in tiers, as in Hermes: the system prompt lists only each skill's name and summary, and the full SKILL.md and its references are read when needed. Sisters are exposed in the same tiers. Her DESCRIBE.md plays the part of a skill's summary, so Last Order ordinarily sees only its description and opening lines, and reads the full introduction and the Sister's list of skills when planning research; her SOUL.md and the bodies of her skills load only in her own session, never in Last Order's context.</td></tr>
<tr><td><b>A division of means and ends</b></td><td>Each Sister brings her own specialty, skills and model (Claude, GPT, Gemini or a local model) to searching and reading within her field, and Claude Code and Codex can join as members to take on tasks. Because the specialists carry most of the searching and tool work, Last Order's context is reserved for studying the material they submit.</td></tr>
<tr><td><b>Symptomatic reading</b></td><td>Beyond examining facts and reasoning, the red team reads Last Order's reasoning for what the conclusion leaves unsaid yet relies on. The divergence review that follows sorts these absences in two. Oversights the line itself can repair are made good within the node; directions its premises occlude pass to Last Order, who either opens them as new research or records why she does not. These are the two forms of invisibility Althusser distinguished in symptomatic reading: what is overlooked, and what the problematic does not permit to be seen.</td></tr>
<tr><td><b>A directed acyclic graph of possibilities</b></td><td>Each new direction forks from Last Order's session, inheriting all prior reasoning, and becomes a node with its own team and red team. The graph unfolds level by level: each possibility is opened once, and a question raised by several lines is researched once. Lines that converge are confronted where they meet, integrated where they can be, and where they cannot, their disagreement is stated precisely.</td></tr>
<tr><td><b>Dissolving the question</b></td><td>Research may show that a question rests on a conceptual confusion or an ideological presupposition and cannot stand as posed. That is a legitimate conclusion in its own right. Before it alters the question you asked, Last Order seeks your consent.</td></tr>
<tr><td><b>Beyond doxa</b></td><td>A single exchange tends to stop at a model's most frequent answer: a doxa, widely held and seldom examined. Every plan carries a coverage table whose empty cells are declared gaps. The coverage maps derive from the schemes disciplines use to classify their own literature, whose blank spaces mark what a field has not counted as a question, and literature scans locate the question within the scholarship.</td></tr>
<tr><td><b>Traceability</b></td><td>Citations trace to the page and character offset, and context to the original conversation; the mechanisms are described below, under Context and materials for humanities research.</td></tr>
<tr><td><b>An auditable research process</b></td><td>Plans, task cards, every version of a conclusion, critiques and source lists are kept as Markdown files in the project, which is itself a git repository. MISAKA commits only at your request and after you have reviewed the files, so the way an argument evolved under criticism is preserved in its history.</td></tr>
<tr><td><b>Under your direction</b></td><td>No plan proceeds without your approval, which you give in ordinary conversation. Each branch has its own tab in the panel, and every agent runs in a pane you can enter at any time. Runs can be halted and resumed.</td></tr>
</table>

## Quick start

```sh
uv tool install "misaka[providers] @ git+https://github.com/Luciole-Studio/Misaka-Agent.git"

mkdir ~/Documents/my-research
cd ~/Documents/my-research
misaka setup     # sign in, choose a model, create your first two Sisters
misaka           # start MISAKA and type /research
```

MISAKA runs on macOS, Linux and Windows (x86_64 or arm64); macOS is recommended. It requires
[uv](https://docs.astral.sh/uv/), git, [ripgrep](https://github.com/BurntSushi/ripgrep),
[fd](https://github.com/sharkdp/fd) and poppler, and access to a model provider: an API key, or a
ChatGPT or GitHub Copilot subscription. Claude accounts can also sign in, with that usage billed
by Anthropic per token as extra usage. Install from this repository: the `misaka` package on PyPI
is an unrelated project.

Each GitHub release also has a prebuilt archive for those architectures. The archive includes
uv, git, ripgrep (`rg`), fd and poppler (`pdftotext`); running `misaka` puts them on `PATH` for
that process. ocrmypdf, DjVuLibre and LibreOffice stay optional and are not in the archive.

```sh
curl -fsSL https://raw.githubusercontent.com/Luciole-Studio/Misaka-Agent/main/scripts/install.sh | sh
```

Windows (PowerShell):

```powershell
irm https://raw.githubusercontent.com/Luciole-Studio/Misaka-Agent/main/scripts/install.ps1 | iex
```

`brew install luciole-studio/tap/misaka` and `winget install Luciole-Studio.Misaka` use the same
archives once the tap and the winget package are published. [release/README.md](release/README.md)
describes the archive layout and those publish steps.

Run MISAKA in a working folder under `Documents` (such as `~/Documents/my-research` above),
never in the root directory.

[Getting started](docs/getting-started.md) covers installation step by step and walks you through
a first research question.

## Research: `/research`

> *The plan's ready! Misaka Misaka starts the moment you say so, says Misaka Misaka, holding it out with both hands.*

`/research` is the core of MISAKA. A run takes one question and explores the different ways it
could be answered. Your question is the first node: Last Order agrees the plan with you, the
Sisters research it, and Last Order writes the conclusion; a red team then reviews that conclusion
and brings out the possibilities it set aside. Errors and gaps are corrected within the node,
while genuine alternatives resting on different premises open as new nodes, each with its own
Last Order, Sisters and red team. The run thus unfolds level by level into a research graph and
concludes in a research article.

<p align="center">
  <img src="assets/research-graph.svg" alt="A research run as a graph: your question; at level 1, another hypothesis, another method and a critique of the question; at level 2, a path one of them passed over, a question two lines raised and researched once, and two lines joined where they meet; then the report" width="820">
</p>

### Starting a run

In Last Order's window, type `/research`. A picker confirms nine parameters in turn, then takes
your research question.

| Parameter | Meaning |
|---|---|
| Research depth | how many levels of alternatives may open beneath the question |
| Node parallelism | how many nodes run at the same time |
| Card parallelism | how many cards each node runs at the same time |
| Follow-up rounds | how many further rounds of cards may follow the first |
| Revisions | how many times a conclusion may be revised in each review loop (the red team's, then the divergence review's) |
| Node limit | how many nodes the run may hold, the root included |
| Plan approval | whether plans wait for your consent |
| Compaction threshold | the share of the context at which conversation is compressed, for this run only |
| Output limit | the longest reply a model may write, for this run only |

Parameters and question may also be given on one line, as in
`/research --depth 2 --max-nodes 12 QUESTION`. Parameters left unspecified take their defaults:
depth 3, four nodes and four cards at a time, two follow-up rounds, two revisions and a limit of
30 nodes. From the shell, `misaka research` accepts the same parameters. A running run's
parameters are changed with `/research limits --sister-parallel 2` (and the other options).

### Inside a node

Every node, your question included, passes through the same seven stages:

1. **Plan.** The plan is a research design. It establishes what the question actually asks and
   what else it could mean, identifies untested premises, sets out the evidence, methods and
   sources required together with their limits, and justifies each Sister's assignment. Each
   task receives its own card; a coverage table maps every sub-question, actor and dimension to
   the cards serving it, so that a gap appears as an empty cell; and the red team is appointed
   here. Where the question admits mutually exclusive ways of answering it, the plan may record a
   decision with at least two options and their premises.
2. **Cards.** The Sisters, and allies such as Claude Code, work their cards in parallel in
   separate sessions, declaring each finding: the claim, its type (fact, inference,
   interpretation or value judgement), the file and page it rests on, and the quotation. Once the
   cards return, Last Order may send out further rounds within the follow-up limit.
3. **Conclusion.** Last Order studies the deliverables in full and writes the conclusion: shared
   and competing findings, key evidence and counterevidence, and the limits of the methods; the
   premises the conclusion rests on and what each gains and gives up, what the materials could
   not reach and whose voices are absent; and how it answers the node it came from and its rival
   options. A conclusion may be a position that accepts stated costs, or the dissolution of the
   question. Factual errors found in an earlier node are recorded as errata, shown beside the
   original conclusion in every node below it and in the final report.
4. **Red-team review.** The red-team Sister studies the conclusion, the plans, the evidence, the
   graph and Last Order's reasoning. She checks the facts, raises gaps, tests whether the
   conclusion answers its parent and its rivals, and identifies what the conclusion leaves unsaid
   yet relies on.
5. **Response to review.** Last Order gives each material objection exactly one response: revise,
   rebut, concede a cost, refer it to the node that owns it, park it, or take it to a decision.
   Before answering an objection about a card's material she can ask the Sister who wrote it and
   hear her out. Where something is revised, the conclusion is issued in a new version and
   returned to the same red team, until nothing material remains or the revisions are exhausted.
6. **Divergence review.** Once the red team's loop is over, the same Sister conducts a divergence
   review in a fresh session. She separates possibilities not taken, which rest on different
   premises, from gaps that any answer must fill, and brings out the presuppositions behind each
   of the conclusion's choices and what each gains and gives up. Last Order fills or answers every
   gap within the node; each version that fills one is reviewed again, until no gap is left or
   the revisions are exhausted.
7. **Decision.** Each possibility not taken is recorded as an option to open, as already covered,
   or as declined with a reason. A presupposition on which the answer depends may itself become a
   fork: the node opened for it examines whether it holds, and how the answer changes if it does
   not.

### How the graph grows

The run proceeds level by level: every node at one depth finishes before the next depth begins.
Between levels, Last Order surveys the whole graph and reconciles it:

- Waiting options open as new nodes; options that ask the same question are merged into one
  node, researched once.
- Lines that reach the same place by different routes may be joined and carried forward by a new
  node. At the join they confront each other: what can be integrated is integrated, and where
  they cannot be reconciled the disagreement is drawn sharply.
- Relations between nodes (convergence, divergence, resonance, appropriation and displacement)
  are recorded for the final report.

A paths list records every possibility raised in the run and what became of it, so that each is
opened only once. A new node forks from the session of the node it came from and inherits the
history of its question.

### Taking part and following a run

- **Plan approval.** Every plan, the reconciliations between levels included, can wait for your
  consent, given in ordinary conversation. Plans may instead proceed automatically, but a plan
  that changes the question itself always waits for you. You may also skip a node, cancel a
  follow-up round or stop the run.
- **Conversation at any point.** Each node has its own tab in the panel, where you can speak with
  that node's Last Order at any time.
- **Commands and recovery.** `/research status` shows progress, `/research stop` ends the run with
  a partial report, and `/research resume` resumes a stopped or failed run. A failed card is
  retried up to three times; when a node fails, the rest of its level continues.

### The final report

When every node has closed, Last Order writes a survey of each node, a draft report, an
independent red-team review of the draft and the final version. The report is a research article
for readers in the humanities and social sciences, written in the language of the question, with
a title, abstract and keywords, an introduction, argued chapters and a conclusion. Notes and a
bibliography carry its traceability; the appendices record every line of inquiry, the costs the
answer accepts together with errata and paths not taken, the materials used, and the handling of
the review. The theses, arguments and connections between lines are drawn from the research
materials: what can be integrated is integrated, and positions whose premises cannot be
reconciled are kept distinct.

The [research guide](docs/guide/research.md) covers every stage in full.

## Context and materials for humanities research

The materials of humanities and social science research are often voluminous and multilingual,
and their provenance must be verifiable; together these exceed what a single model's context
window can hold. MISAKA addresses the following problems by design.

| Problem | Approach |
|---|---|
| Long works exceed the capacity of the context window | PageIndex builds a hierarchical chapter index of long documents, which are then read on demand by section or page range |
| Context compression over a long inquiry distorts earlier material | LCM summarises earlier conversation hierarchically, each summary traceable to the original text; all sessions in a project share one retrieval index |
| Skill and tool instructions consume the coordinator's context | Skills and tools are assigned to Sisters by specialty, and cards run in separate sessions; Last Order's context is reserved for the materials submitted to her |
| Citations are hard to verify, and sources may be fabricated | Quotations are located to the page and character offset; conclusions cite only sources actually read, and source lists and original files are archived with them |
| Scanned documents, DjVu and multilingual materials | OCR in Chinese, English and Japanese, DjVu and legacy Office formats; plain text is structured by headings such as CHAPTER or LIVRE |
| Model output gravitates toward prevailing views | Disciplinary coverage maps and literature scans mark research gaps, and the divergence review proposes directions not taken |

### PageIndex: hierarchical indexing and reading on demand

- Each project keeps its own document index. Documents of 20 pages or more receive a
  hierarchical table of contents giving the page range of each section.
- An agent first consults the outline (`doc_outline`) and then reads by section node or page
  range (`doc_read`), so that only the required text enters the context.
- For PDF and DjVu a page is a page of the file, counted from 1, which is not always the number
  printed on it (the printed number is shown beside it where the PDF labels its pages or the
  page heads print it); other formats are divided at paragraph boundaries into pages of about 3,000
  characters, to which page citations refer.
- `doc_find` performs literal search; `doc_verify` returns the page, character offset and
  checksum of a quotation.
- Scanned pages are found one by one and read by OCR, so a book that mixes typeset and scanned
  pages loses neither.
- `doc_read` names the figures its text does not carry: images and drawings on a PDF page, a
  scanned page that is likely a map or a plate, a slide whose meaning is in a picture, a picture
  a Word document or an EPUB embeds. `doc_page_image` shows the page, the slide or the one
  figure. For a model without vision, every image is read to it in words by one vision model
  (`vision.model`).
- Texts produced during the research are indexed on completion. The final report retrieves each
  node's conclusion as a document, without loading everything into the context at once.

### LCM: traceable hierarchical compression

- When a session reaches a set share of the context window (35% by default), earlier
  conversation is compressed into summaries, which are in turn merged level by level.
- The original conversation is preserved in full. Every summary can be expanded to the text it
  rests on, and the original can be searched directly, so compression entails no irreversible
  loss of information.
- All sessions in a project share one retrieval index; any agent can search the conversations of
  the others.
- Optional features: semantic retrieval with a local embedding model, proactive recall of
  relevant history, external storage of very long tool outputs, and daily, weekly and monthly
  roll-ups.
- Each research run may set its own compaction threshold and output limit without affecting the
  global configuration.

See [Documents and the web](docs/guide/sources.md) and the
[configuration reference](docs/reference/configuration.md).

## Outputs

> *Every source is filed where you can check it, Misaka reports.*

All output is written to your project folder:

```text
my-research/
├── final/<run>-final.md     the final report, with the costs it accepts and the paths not taken
├── final/<run>-sources/     every file the report cites, linked in place
└── nodes/<node>/            one directory per piece of research
    ├── plan.md              Last Order's plan and her reasons for choosing each Sister
    ├── cards/<card>/        each Sister's work and the red team's critique
    ├── synthesis.md         the conclusion (synthesis-2.md and onward once revised)
    └── SOURCES.md           every file the conclusion cites, and the claims each one supports
```

MISAKA commits only at your request. If the project is a git repository, `/commit` lists the files
for your confirmation before committing.

## Common commands

| Purpose | Command |
|---|---|
| Start MISAKA | `misaka` |
| Begin a research run | `/research`, followed by your question |
| Check, halt or resume a run | `/research status`, `/research stop`, `/research resume` |
| Say something to a running node | `/research tell [--to NODE_ID] MESSAGE` |
| Change a running run's limits | `/research limits --sister-parallel 2` |
| See what a run spent | `misaka usage --run RUN_ID` |
| Talk to a single Sister | `/sister 10032` |
| Create a Sister | `misaka create 10036 --desc "Econometrics and causal inference"` |
| Index your documents | `misaka doc scan sources/` |
| Read materials with an agent | `misaka read open SOURCE --question "..."` |
| Choose a model, sign in | `/model`, `/login` |
| List every command | `/` in chat, `misaka --help` in the terminal |
| Show the panel's key bindings | `ctrl+b`, then `?` (see the [panel guide](docs/guide/panel.md)) |
| Update | `misaka update --apply` |

The [command reference](docs/reference/commands.md) lists every command.

## Documentation

| Topic | Guide |
|---|---|
| Installing and running a first question | [Getting started](docs/getting-started.md) |
| Running and steering research | [Research runs](docs/guide/research.md) |
| Building a team, adding Claude Code or Codex | [The team](docs/guide/team.md) |
| The panel: tabs, panes and key bindings | [The panel](docs/guide/panel.md) |
| Signing in, choosing models, local models | [Models](docs/guide/models.md) |
| Working with documents and the web | [Documents and the web](docs/guide/sources.md) |
| Reading materials with an agent | [Reading materials](docs/guide/reading.md) |
| Troubleshooting | [Troubleshooting](docs/guide/troubleshooting.md) |
| What MISAKA does and does not protect | [Running MISAKA safely](docs/guide/security.md) |
| Command and configuration reference | [Commands](docs/reference/commands.md), [Configuration](docs/reference/configuration.md) |

[docs/README.md](docs/README.md) indexes every page and defines the terms MISAKA uses.

## Data and cost

All data MISAKA keeps stays on your machine: settings, credentials and history in `~/.misaka/`,
research output in your project folder. Prompts are sent only to the model providers you
configure. Web searches go to the search services you configure, or, when none is configured or
one fails, to the free public tiers of Exa, Parallel, Firecrawl and Keenable
(`misaka web set keyless_fallback false` disables this). Literature scans send a question's search
terms to OpenAlex. MISAKA sends no telemetry.

A research run operates at scale: by default up to four branches run at once, each with up to four
Sisters working in parallel as memory allows, so a deep run makes many model calls. A smaller
depth reduces cost, and `research.token_cap` in `~/.misaka/settings.json` sets a hard token
budget for each run.

## About the name

MISAKA takes its names from Kazuma Kamachi's *A Certain Magical Index* and *A Certain
Scientific Railgun*, in which the Sisters, clones of the Railgun Misaka Mikoto, share their
memories through the Misaka Network.

| In the story | In MISAKA |
|---|---|
| **Misaka Mikoto**, the original every Sister comes from | `MISAKA.md`, the shared identity every agent loads before her own |
| **The Sisters**, known by serial number: Misaka 10032, 10033, … | your specialists, each with a number, a specialty and her own `SOUL.md` |
| **Last Order**, Misaka 20001, who commands the network | the coordinator you talk to |
| **The Misaka Network**, through which what one Sister learns the others can recall | the project's conversations, searchable by every agent in it |

The "says Misaka" lines in this README are ornamental; each agent speaks as her `SOUL.md`
specifies, and a single line there gives her the Sisters' manner of speech.

MISAKA is an independent project. It is not affiliated with or endorsed by the author or the
publishers of the series.

## Foundations

MISAKA's agent kernel is a Python port of [pi](https://github.com/earendil-works/pi), and its
panel is a port of [herdr](https://github.com/herdrdev/herdr), with
[ghostty](https://github.com/ghostty-org/ghostty)'s terminal library behind every pane. It builds
on [hermes-lcm](https://github.com/stephenschoettler/hermes-lcm) for long conversations and
[PageIndex](https://github.com/VectifyAI/PageIndex) for document structure, and ports web tools
and skills from [Hermes Agent](https://github.com/NousResearch/hermes-agent) and Office support
from [FrontierAgent](https://github.com/ApodexAI/FrontierAgent).
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) records the origin of each part.

MISAKA builds on pi for its simple kernel and its mature, actively maintained community, which lets
MISAKA follow upstream directly. MISAKA's graph governs the content and possibilities of research:
a node is a completed piece of research, and an edge is a line of inquiry that forked off carrying
its reasoning.

## Licence

MISAKA is released under the [Apache License 2.0](LICENSE). Third-party components keep their own
licences, recorded in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

If you redistribute MISAKA or build on it, keep the attribution in [NOTICE](NOTICE): Apache-2.0
requires it to accompany your distribution.

<p align="center"><em>Misaka Network, signing off, says Misaka Misaka.</em></p>

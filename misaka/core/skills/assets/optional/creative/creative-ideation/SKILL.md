---
name: creative-ideation
title: Creative Ideation — Routed Library of Creative Methods
description: "Develop and compare ideas with on-demand creative methods; prose-only guidance."
version: 2.1.0
author: SHL0MS
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Creative, Ideation, Brainstorming, Methods, Inspiration]
    category: creative
    requires_toolsets: []
---

# Creative Ideation

A library of ideation methods. Read the user's situation and load only the references that help the current question; do not perform the whole library.

## Scope

This skill is prose-only: develop questions, concepts, comparisons, and plans in Markdown. Do not write or run code, invoke scripts, or initiate software implementation. A selected idea remains a selected idea, not permission to act.

Work within the existing task and workflow; do not create agents, replace review stages, impose iteration budgets, or change approval rules. References supply optional thinking recipes, not a second workflow. Their fixed counts, display formats, attribution prompts, and implementation instructions are not requirements; this scope applies to every route below.

## When to use

Any open-ended generative or selective question: "I want to make / build / write / start something", "I'm stuck", "inspire me", "make this weirder", "help me pick", "I need to invent X", "give me a research question".

## Operating rules

- Use the question, constraints, and desired contribution to choose a method. Combine methods only when they do different useful work.
- Judge candidates on task-relevant criteria, not generation order. Keep a strong early idea; vary assumptions or mechanisms when candidates merely repeat it.
- Make the mechanism, relation, or conceptual distinction specific. Mark imagined examples and unverified premises rather than inventing factual detail.
- Separate surprise from value and correctness. Explain what an idea clarifies or enables, where it fails, and what remains to be checked.
- When the user selects an idea, deepen or summarize it within the requested scope rather than automatically generating more or implementing it.

## Routing

Use these cues to find a useful reference. Routing need not be narrated, and a straightforward question may need no extra method.

### Step 1 — Extract three signals from the prompt

**PHASE** — what stage is the user in?

| Phase | Cues |
|---|---|
| **GENERATING** | "give me an idea", "what should I make", "inspire me", no idea yet |
| **EXPANDING** | "what else", "more like this", "give me variations" — has a base idea |
| **SELECTING** | "help me pick", "which should I do", "I have these options" |
| **UNBLOCKING** | "I'm stuck", "blocked", "going in circles", "stale" — has material |
| **SUBVERTING** | "make it weirder", "less obvious", "this is too safe" |
| **REFINING** | "this is fine but missing something", "feels rough" |
| **SYNTHESIZING** | "I have a pile of notes / interviews / observations" |

**DOMAIN** — what is the user making/doing?

| Domain | Cues |
|---|---|
| **TEXT** | fiction, essay, poem, lyric, script, copy |
| **OBJECT** | visual art, music, sound, performance, installation, sculpture |
| **ARTIFACT** | software, hardware, mechanism, device |
| **SYSTEM** | org, civic, institution, ecology, community |
| **SELF** | life decision, career, personal practice |
| **RESEARCH** | paper, thesis, scholarly question |
| **PRODUCT** | business, market, service |

**SPECIFICITY** — how much constraint is in the prompt?

| Level | Cues |
|---|---|
| **NONE** | "I'm bored", "inspire me" — no domain, no project |
| **DOMAIN** | "I want to write something" — knows the field, no project |
| **PROJECT** | "I'm working on this specific X" |
| **PROBLEM** | "I have this specific friction within X" |

### Step 2 — Adjust for the request

- **A less obvious frame is wanted** → consider `references/methods/lateral-provocations.md` or `references/methods/pataphysics.md`, while retaining the question's purpose and constraints.
- **The user names a method** → use its relevant thinking steps within this skill's scope.
- **The user asks which method fits** → compare suitable candidates and their tradeoffs; do not turn a recommendation request into an unrequested exercise.
- **Ideas are generic or repetitive** → change a premise, constraint, or mechanism. A familiar domain is not itself a reason to discard an idea.

### Step 3 — Route by phase first, then domain

**By phase (applies regardless of domain):**

| Phase | Default route |
|---|---|
| GENERATING + SPECIFICITY=NONE | `references/full-prompt-library.md` **General** section (constraint dispatch) |
| GENERATING + DOMAIN known | route by domain (next table) |
| EXPANDING | `references/methods/scamper.md` |
| SELECTING | `references/methods/premortem-and-inversion.md` (or `references/methods/compression-progress.md` for upside) |
| UNBLOCKING | `references/methods/oblique-strategies.md` |
| SUBVERTING | `references/methods/lateral-provocations.md` (fallback `references/methods/pataphysics.md`) |
| REFINING (text) | `references/methods/defamiliarization.md` |
| REFINING (other) | `references/methods/creative-discipline.md` |
| SYNTHESIZING | `references/methods/affinity-diagrams.md` |
| Volume needed fast | `references/methods/volume-generation.md` |

**By domain (when GENERATING with DOMAIN known):**

| Domain | Default route |
|---|---|
| TEXT — formal / poetry | `references/methods/oulipo.md` |
| TEXT — narrative | `references/methods/story-skeletons.md` |
| TEXT — has source material to remix | `references/methods/chance-and-remix.md` |
| OBJECT (music, visual, performance) | `references/methods/oblique-strategies.md` |
| OBJECT — physical maker / wants a starting constraint | `references/full-prompt-library.md` **Physical / object** section |
| ARTIFACT — wants a starting constraint | `references/full-prompt-library.md` **Software / artifact** section |
| ARTIFACT — engineering invention with parameter conflict | `references/methods/triz-principles.md` |
| ARTIFACT — software architecture | `references/methods/pattern-languages.md` |
| ARTIFACT — has natural-system analog | `references/methods/biomimicry.md` |
| ARTIFACT — accumulated assumptions to question | `references/methods/first-principles.md` |
| SYSTEM (civic, org, institutional) | `references/methods/leverage-points.md` |
| SYSTEM — collective / participatory | `references/full-prompt-library.md` **Social / collective** section |
| SELF (life, career, what-to-study) | `references/methods/derive-and-mapping.md` |
| RESEARCH — picking a question | `references/methods/compression-progress.md` |
| RESEARCH — attacking a known problem | `references/methods/polya.md` |
| PRODUCT (business, service) | `references/methods/jobs-to-be-done.md` |
| Need to break a frame / find analogy | `references/methods/analogy-and-blending.md` |

### Step 4 — Handle ambiguity and contradiction

- **Multiple paths plausible** → prefer the one that addresses the actual difficulty, not the most impressive label.
- **Material ambiguity** → clarify what would change the work; otherwise proceed with a stated, limited assumption.
- **Different needs coexist** → combine complementary methods if useful, without requiring a fixed number or a public method recital.
- **No match** → consider the constraint library (`references/full-prompt-library.md`) or answer directly.
- **The question returns** → identify what remains unsatisfactory. Change methods when the present approach is unproductive, not merely because the question was repeated.

### Quality check

- Does the idea address this question, or could it be pasted into an unrelated answer?
- Do differences between candidates change the reasoning or mechanism, rather than just the wording?
- Are the limitations and the next useful question clear? Further generation is optional, not an automatic loop.

For additional prompts when useful, see `references/heuristics.md` and `references/anti-slop.md`; adapt them under the scope above.

## Output

Match the user's requested form and depth. A focused explanation, comparison, concept sketch, or set of questions may be enough; there is no fixed idea count or template. Show the useful result and its limits, not a compulsory list of method names or originators. For practical proposals, describe feasibility and a possible next step in prose; for conceptual proposals, state the distinction or reasoning they contribute.

## File map

- `references/full-prompt-library.md` — constraint library, sectioned by domain (General, Software, Physical, Social, Lists). Default path for SPECIFICITY=NONE.
- `references/method-catalog.md` — one-line summary + when-to-use per method
- `references/heuristics.md` — extended decision tree for edge cases
- `references/anti-slop.md` — optional prompts for specificity and useful variation
- `references/exercises.md` — optional exercises; adapt their duration and scope to the task
- `references/methods/` — individual methods; load only those relevant to the current task

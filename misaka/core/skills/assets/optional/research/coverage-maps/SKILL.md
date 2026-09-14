---
name: coverage-maps
description: Scan maps of knowledge for dimensions a research plan missed
version: 1.0.0
author: Luciole Studio
license: Apache-2.0
---

# Coverage maps

A research plan can only cover what its author thought of. This skill is a set of maps of
knowledge to scan *before* dividing a question into tasks, and again when red-teaming a
conclusion. Every map is somebody's theory of what matters; none is complete, and no
map is evidence. Use several, and say which ones you used.

## When

- Planning (Last Order, root node): after framing the question and before writing tasks.
- Red-teaming (the Sister named as red team): when hunting for omitted actors, processes,
  time horizons, and consequences.

## Procedure

0. Call `coverage_scan` with two or three phrasings of the question: it returns which subfields
   and topics of the literature (OpenAlex) actually discuss it. A neighbouring field with many
   works is a dimension you may be missing; a field with few is not thereby irrelevant.
1. Read `references/facets.md` first. Cut the question along the five facets:
   discipline, period, place, source type / genre, method. Write one line per facet.
2. Scan `references/disciplines.md`: which fields discuss this question, and which
   neighbouring field would look at it differently?
3. Scan `references/questions.md`: which kinds of question (mechanism, origin, function,
   history; which level of analysis; which paradigm) have not been asked?
4. When the object is a non-Western tradition, or the corpus is Chinese, scan
   `references/traditions.md` for how that tradition classifies its own knowledge.
   `references/internal.md` holds the classifications disciplines keep for their own
   bibliographies (deeper than any general map); `references/societies.md` holds learned
   societies' section lists — a field's own, most current self-description.
5. In the plan (`plan_markdown`), add a section **Coverage maps used**: which maps you
   scanned, which you did not and why, and which dimensions they surfaced. This section is
   part of the honest boundary of the research, not decoration.

## Rules

- A map is a prompt for overlooked dimensions, not a checklist to fill: a category with no
  plausible, material connection to the question creates no task.
- Do not force the question into any one map's grid. Where two maps disagree about what
  a thing is, note it; that disagreement is often the research question.
- Names and codes in the references were transcribed from published schemes and may lag
  the current editions; cite the scheme, not the code, and check the source when a code matters.

## Extending

Put a `coverage.md` (or a `skills/coverage-maps/` override) in the project folder with the
maps of your own field: a society's section list, a bibliography's classification, a
period scheme. Project-level maps shadow these.

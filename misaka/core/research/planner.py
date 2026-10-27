"""Last Order's research calls and the card contracts, with a deliberately small machine-checked envelope.

The prose is open-ended. Python validates only the fields needed to route
work; it never judges whether a method, source, interpretation, or conclusion is sound.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from misaka.ai.utils._output_limit import hit_output_limit
from misaka.config import home
from misaka.core.network.card_contract import format_deliverable, one_file, split_deliverable
from misaka.core.platform import prompt_guard
from misaka.core.platform import tasks as task_store
from misaka.core.research import commands, ledger, runs
from misaka.core.session_manager import findMostRecentSession, readSessionHeader
from misaka.utils import atomic

ROOT_CONTRACT = """# Research plan — design only
Do not answer the user's question at this stage. Your only deliverable is a research design.
Do not assume that textbooks, mass media, the mainstream view, or the contrarian view is correct.

Start by working out what the user is actually asking, what else the question could mean, and which of its
premises are untested. Identify the relevant objects, processes, interactions, prior knowledge, time horizons
and disciplines; stay inside the agreed question, and inside it decompose completely. Specify evidence needs,
deliverables, dependencies and acceptance criteria. Offer suitable methods, theories and source strategies with their
blind spots and competing approaches; Sisters choose and refine the specialist implementation.
Use relevant available method Skills as a menu, not a mandatory template. Specify `method`, `source_strategy`
and `falsifiers` where useful; assign conceptual analysis or explanation testing as well as material gathering.
Check for important missing dimensions, with the coverage aids available -- maps of a field's dimensions, scans of
the literature -- when they would improve the design, not a fixed number of times on every node. What an index
counts is a discovery signal, not proof of relevance or completeness, particularly across languages, kinds of source
and schools of thought. Delegate substantive investigation to Sisters instead of doing their assignments during
planning.
Choose Sisters by fit from the system catalog and explain each choice. End `plan_markdown` with "Coverage check":
record what informed it (or why none was needed or available), useful dimensions and remaining gaps.

Granularity: a card is one assignment that one Sister can carry through in one session. When the question turns
on how several actors, regions, institutions, periods or dimensions behave, each gets its own card, not a share
of one; a card that bundles several such units is under-decomposed and comes back shallow. A dependency makes a
card wait until another has finished: give one only when a card needs another's output as its input. Definitions,
standards or a baseline every card should share belong in `plan_markdown`, so the cards start together. The number of cards
follows the question's structure, not the number of Sisters (one Sister holds many cards, each in its own session)
and not the run's concurrency limit, which only paces dispatch. The first plan is the whole design of this node's
own line: follow-up rounds are for gaps the evidence reveals, not for breadth already visible now.
`plan_markdown` includes a coverage table -- every sub-question and every actor or dimension the question names,
against the cards that serve it -- so that an empty cell is a declared gap rather than an oversight.

Branches are possibilities. When the question admits genuinely different ways to answer it -- competing hypotheses,
methods, theoretical frameworks, ideologies or concepts, perspectives, scenarios, readings, ways of decomposing it,
or a critique of the question itself (genealogical, symptomatic, ideological, and more) -- record each such fork in
`decisions`: a question with its stakes and at least two options, each stating its premise (what that path assumes
or changes). This node's own line may be one option (`own: true`) beside a single new possibility. Record every
genuine possibility and invent none to fill a count. Complementary work is not a fork: parts that serve one answer
together are tasks of this plan, and parts that together make one other path are a single option. Options open as
nodes of their own -- each with its own Last Order, Sisters and red team. Timing is part of the design: beside this
node's own cards, forks open once this node has concluded, forked from its concluded conversation, so they answer its
conclusion; a plan with decisions and no tasks is a pure branch point whose options open right away, in parallel and
independently of one another. Lines whose agreement should count as independent -- competing frames, readings or
methods meant to triangulate -- belong in a pure branch point, the line you would call the mainline included as an
option. Corrections and gaps are never forks: follow-up rounds, and revisions after the red team, handle them inside
this node before it forks, and nothing it leaves open is handed to the nodes opened from it. Decisions belong to a
node's first plan below max_depth only.
A node that pursues another framework, theory or concept tests it on the material it makes you look at: say in
`plan_markdown` which material this frame directs you to that its parents' lines did not read, and what under it
would count against their conclusions, and give that material cards. Recoding their evidence under new names alone
is not a test of the frame.

Call `misaka_research_assign` with your plan and assignments. A plan too large for one reply is submitted in
several calls: the first with `plan_markdown` and the first tasks, the rest with `append: true`; never shrink a
plan to fit one call.
This tool records your dispatch command; research cards are created only from an accepted call, never from
JSON in your final answer or a submission file.
After the tool accepts your command, end with a short plain-text summary.

Rules:
- Never force a question into PICO, a causal-variable model, or any other single-discipline template.
- A required human choice is asked in conversation when a user is talking to you; status=clarify with
  clarifying_questions is only for a run nobody is talking to. Uncertainty that can be researched belongs in a research task.
- Tasks must be substantive research assignments written for this question, not mechanical templates.
- The red team holds two jobs on this node, and you choose her for both; only you decide who that is. She critiques
  every version of the conclusion until her review finds nothing material or the revisions are spent, and then runs
  the divergence review in a fresh session: the frames, readings, sources, actors and possibilities the conclusion
  did not take, and the gaps it left -- reviewing again every version that fills a gap. A
  critic who only checks the evidence is half of that; `red_team.reason` says why her profile fits both, and
  `plan_markdown` names her with both jobs.
"""


OPTIONAL_MATERIAL_TOOLS = ("x_search", "browser_navigate", "browser_snapshot", "browser_get_images", "browser_vision")
MATERIAL_TOOLS = ("read", "misaka_research_view", "web_search", "web_fetch", "web_extract", "download_file",
                  "doc_list", "doc_outline", "doc_read", "doc_find", "doc_verify", "doc_page_image", "doc_add",
                  *OPTIONAL_MATERIAL_TOOLS)
RESEARCH_TOOLS = (*MATERIAL_TOOLS, "skills_list", "skill_view")

# A research Sister's working rules, appended once to her system prompt (worker.card_session_setup)
# rather than repeated in every card. The card body keeps only what is specific to that task.
RESEARCH_SISTER_DISCIPLINE = """[Research card]
You are one researcher on a research graph that Last Order coordinates. How to work:
- Read relevant available method Skills as needed to generate, compare or revise explanations for this assignment.
- Back every empirical claim with a traceable source and an exact quotation or precise location; read the saved
  material before citing it.
- Keep facts, inferences, interpretations and normative judgements apart, and say which is which.
- Record counterevidence, competing explanations and unresolved questions as you meet them. At the start of a
  substantive investigation, identify evidence or reasoning that could overturn the working premise and test it deliberately;
  revise that check when the premise changes rather than repeating a ritual before every search.
- Note whether your sources are independent of one another: three retellings of one source are one source.
- Authority, mainstream or contrarian opinion, and the task's own premise are not evidence.
- When material cannot be obtained you may still conclude, with reservations: name what is missing and what it
  would settle.
- Declare findings and concrete uncertainties with `misaka_card_note` as you work: `findings` is a list, each item
  `text`, `claim_type` (fact | inference | interpretation | normative), `source_file` or `doc_id` + `page`, optional
  `quote`; `uncertain` lists concrete uncertainties; the note's own `text` is one line for the card's log.
  Notes accumulate; a correction says which earlier declaration it revises. The ledger records without judging;
  the red team and Last Order weigh it later.
- Use the available colleague directory and communication tools for advice or missing material. Follow the
  communication tool's input-request protocol only when an external decision or input is indispensable.
- Work from deliverables: what the cards delivered -- their files and declared findings -- and the node's own texts
  are what the work stands on, and you must read them first. Only where one is unclear or needs the context it came
  from, turn to the conversation behind it: this run's conversations -- Last Order's with the user, every Sister's --
  are listed under Conversations in `misaka_research_view(view="workspace", run_id=...)`, each with its session
  file. Search one with the conversation tools you have; without them, read its session file. A conversation
  supplements a deliverable, never replaces it. Sessions not listed there belong to other work; leave them alone.
"""


def session_tools(worker, tools=RESEARCH_TOOLS):
    """Inherit enabled tools in session order; new registrations join the next phase."""
    from misaka.core.research.tool_policy import research_tools

    session = getattr(worker, "session", None)
    return research_tools(session.getActiveToolNames() if session is not None else tools)

SOURCES_FOOTER = """
End with a `## Sources` section listing every source this text rests on, one per line: the file's path inside the
project (`downloads/...`, `nodes/...`) or its `doc:<id>#p<n>` locator, plus the URL it was fetched from when it came
from the web. A program reads that section: whatever it does not list is not bundled beside this text.
"""

MARKDOWN_OUTPUT = """\nReturn the complete answer as free-form assistant Markdown, not JSON, in this turn.
The workflow saves your final assistant message automatically, exactly as written, as the file others read: begin it
with the answer itself, not a remark about what you are about to write. Do not call a tool to submit or save this answer.
"""


def navigation(run_id):
    return f"""\n# Research context
Current workspace lookup: misaka_research_view(view="workspace", run_id="{run_id}").
"""


SYNTHESIS_FOLLOWUP = """
# Round {round}: conclude, or request follow-up research ({left} more round(s) may still be asked for)
If returned work leaves the same question unanswerable, use `misaka_research_assign` for missing evidence,
conceptual distinctions or reasoning, including developing competing explanations. In `plan_markdown`, identify
the gap and why each new card addresses it; the node's red team stays the Sister its first plan named, so leave
`red_team` out. Then end this turn with a short note -- no conclusion yet: conclude after those cards return,
using all rounds together. Follow-ups complete this question's research; they do not replace independent review.
The red team reviews the conclusion next and its issues come back to you to dispose of in this node;
do not use follow-up rounds to review yourself.
"""

SYNTHESIS_LAST_ROUND = """
# Round {round}: the last round
No further cards can be assigned on this node. Write the conclusion from what there is, and name what remains
unsupported and what evidence or reasoning would settle it.
"""

SYNTHESIS_REVISION = """
# Revision {version}: rework the conclusion after the red team's review
For review round {review} you recorded the dispositions below. Rework the conclusion for every `revise`: change what
must change, keep what still stands, and end with a short "Revisions" section saying what changed and why. What the
review did not touch is carried over from the previous conclusion as it stands, at full length -- read that file
rather than rewriting from memory; shortening or dropping any of it is a change and goes in "Revisions". Rebuttals,
concessions, covered and parked issues stand as you recorded them; do not reverse them silently. Write the complete
revised conclusion, not a list of edits. The red team reviews this version next.
- Previous conclusion: `{previous}`
- The review: {critiques}
"""

SYNTHESIS_GAP_REVISION = """
# Revision {version}: fill the gaps the divergence review found
For the divergence review of version {review} you recorded the dispositions below. Rework the conclusion for every
`revise`: fill each such gap with what the research found, keep what still stands, and end with a short "Revisions"
section saying what changed and why. What no gap touched is carried over from the previous conclusion as it stands, at
full length -- read that file rather than rewriting from memory; shortening or dropping any of it is a change and goes
in "Revisions". Concessions, rebuttals, covered and parked gaps stand as you recorded them. Write the complete revised
conclusion, not a list of edits. The divergence review reviews this version next.
- Previous conclusion: `{previous}`
- The review: {critiques}
"""

SYNTHESIS_ERRATA = """
# Corrections recorded against the nodes this one descends from
Each supersedes the claim it corrects; write from the corrected claim:
{errata}
"""

INTERLOCUTORS = """
# This node on the graph
Its parents' conclusions and its rival options are interlocutors, not premises. Say where this path departs from its
parents and answer their strongest objections rather than adopting them;
answer the strongest case of each rival option too -- its premise now, its conclusion where the graph already holds
one. A path that argues only for its own premise knows little of its question. The graph: `{graph}`. Every
possibility raised so far and where it went: `{paths}` -- a possibility on it is not decided again.
"""

JOIN_CONFRONTATION = """
# This node carries several paths forward together
Each parent keeps its own premises, evidence and history; do not merge them into one world. Confront them: sublate
what can be sublated, and where they cannot be reconciled, draw the disagreement sharply. Never average them into a
position none of them argued. Say what the shared continuation rests on, and what each parent brings to it.
"""


def errata_above(con, run, node):
    """Corrections recorded against the nodes this one descends from, as a prompt section; empty
    when there are none."""
    from misaka.core.research import views

    above = views.ancestors(con, node["id"])
    errata = [e for e in runs.errata(con, run["id"]) if e["node"] in above]
    return SYNTHESIS_ERRATA.format(errata="\n".join(
        f"- `{e['node']}` says \"{e['claim']}\" -> {e['correction']} ({e['grounds']})" for e in errata)) if errata else ""


def position(con, run, node):
    """Where a node stands on the graph, for its own Last Order: how it was reached, the rival
    options it was opened against (with their outcome and conclusion where they have one), and the
    stance it owes its parents -- a join confronts them, any other node answers them."""
    from misaka.core.research import views

    lines = []
    for option in runs.origin_options(con, node["id"]):
        lines.append(f"- at node {option['decided_at']}, deciding \"{option['decision_question']}\" (stakes: "
                     f"{option['decision_stakes']}): option \"{option['label']}\" -- premise: {option['premise']}")
        lines += [f"  - takes up `{issue['id']}` (raised at node {issue['branch_id']}): {issue['question']}"
                  for issue in runs.issues(con, run["id"], option_id=option["id"])]
        for rival in runs.options(con, run["id"], decision_id=option["decision_id"]):
            if rival["id"] == option["id"]:
                continue
            if rival["node_id"] == node["id"]:
                outcome = "also pursued by this node"
            elif rival["node_id"]:
                conclusions = views.syntheses(con, run, runs.node(con, rival["node_id"]))
                who = ("the deciding node's own line" if rival["node_id"] == option["decided_at"]
                       else f"pursued by node {rival['node_id']}")
                outcome = who + (f", conclusion `{runs.artifact_path(conclusions[-1])}`" if conclusions
                                 else ", not concluded yet")
            elif rival["reason"]:
                outcome = f"not pursued: {rival['reason']}"
            else:
                outcome = "not decided yet"
            lines.append(f"  - rival \"{rival['label']}\" -- premise: {rival['premise']} ({outcome})")
    parents = runs.parents(con, node["id"])
    if not lines:
        lines.append(f"- a join of nodes {', '.join(row['id'] for row in parents)}: their work is carried forward "
                     "together here (the context packet gives the reconciliation's reasons)")
    stance = JOIN_CONFRONTATION if len(parents) > 1 else ""
    return "\n".join(lines) + "\n" + stance + INTERLOCUTORS.format(graph=views.graph_path(run), paths=views.paths_path(run))


SYNTHESIS_CONTRACT = """# Node conclusion — synthesize the submitted research output
Read full sources in context; summaries and the ledger are researchers' declarations, not certified evidence.

Check links between cards: align concepts, test shared premises and distinguish independent grounds from repeated
support. Read relevant available Skills for these checks when useful.
Separate shared findings, competing findings, key evidence, counterevidence,
methodological limits, value premises, and unresolved questions. Give traceable source paths, document locations or URLs.
Name a source only for what you or a card actually read in it; what rests on general knowledge of the field says so
rather than borrowing an author or a title it was never checked against. Do not vote or hide competing interpretations or insufficient evidence. Source checks and limited supplementary
retrieval needed to assess returned evidence are part of synthesis; preserve and cite any new material for the red team.
Use the available follow-up assignment for substantive new research; when none remains, state the unresolved gap.
State what new evidence or reasoning could change the judgement. Name the presuppositions the conclusion rests on
and what each gains, gives up and trades; say what it does not know -- what its sources could not reach, whose voices
are absent -- and, as far as you can see it, what its frame keeps from being asked. A conclusion may be a position held at a cost (name
the costs it accepts) or a dissolution: when the research shows that the question rests on a conceptual confusion or
an ideological presupposition, show why it does not stand as asked. That is a legitimate result, not a failure.
Record it with `misaka_research_dissolve` in this turn (which question it dissolves, and the grounds), then write the
conclusion. A dissolution of the research question the user asked changes their question: the final report waits
for their consent to it.
Where the cards show that an earlier node's conclusion states something wrong -- a fact, a date, an attribution, a
term or a reference that does not exist -- record it with `misaka_research_erratum` (that conclusion is not rewritten;
the correction travels with it), and write from the corrected claim.
The cards' deliverables -- their files and declared findings -- are what the conclusion stands on: you must read them
first. Only where one is unclear or needs the context it came from, turn to the conversation behind it: this run's
conversations -- yours with the user, every Sister's -- are listed under Conversations in
`misaka_research_view(view="workspace", run_id=...)`, each with its session file. Search one with the conversation
tools you have; without them, read its session file. A conversation supplements a deliverable, never replaces it.
Sessions not listed there belong to other work; leave them alone.
""" + SOURCES_FOOTER + MARKDOWN_OUTPUT


RED_TEAM_CONTRACT = """## goal
Red-team the conclusion at `{synthesis_path}` for "{question}". Hunt for reasoning failures; do not extend the report and do not
polish prose. Deliver `{deliverable}` for the node Last Order to read. Record your issues with `misaka_card_note(issues=...)`.
Last Order answers every material issue inside this node -- revising the conclusion, rebutting, conceding a cost, pointing
to the node that owns it, parking it, or taking it to a decision when it reveals a genuine alternative.
Your review opens no node by itself.

## material
- Conclusion under review: `{synthesis_path}`
- Plan (every round of this node, oldest first): {plan_path}
- The research graph: `{graph_path}` -- every node of this run, what it pursues and where its conclusion is.
- Source-task artifacts: read whichever the conclusion cites. Each card's `SOURCES.md` lists what that card actually
  cited and consulted; a claim from a card that consulted nothing rests on search snippets or memory, whatever label it carries.
{own_cards}{deliberation}{evidence}{rereview}
## what to inspect
Test the premises carrying key conclusions and the strongest competing explanation. Separate a local repair from
a flaw that overturns the conclusion; read relevant available review Skills as needed.
Facts and quotations, inference and causation, concepts and scope, methods and sampling, standpoint and bias, omitted actors or
processes, interactions, time horizons, consequences, and normative claims disguised as facts.
Check the facts the conclusion rests on against sources of your own -- dates, numbers, events, attributions,
quotations, and whether a cited term or work exists -- first those from cards that consulted nothing; a claim you
could not check is not confirmed by reading it twice. A fact that fails is a material issue: say what you checked
it against.
Gaps are yours to raise: whatever the conclusion neglected, did not know or did not ask that any line answering this
question would have to fill -- a kind of source, an actor, a period, a body of evidence -- is a material issue, with
why no answer stands without it. This node fills it, before anything is opened from it; nothing is handed down.
Use the coverage aids available -- maps of a field's dimensions, scans of the literature -- when useful to assess a
suspected omission; an unmentioned dimension is
not automatically a defect. Explain how an omission materially affects this question within its agreed scope. A
question that another node of the graph owns is not a gap of this one: when you raise it, name that node's id so Last
Order can check whether its work covers it.
Check that the conclusion answers its parents and the strongest case of the rival options it was opened against (its
NODE.md and the graph name them), rather than arguing only for its own premise. A node opened to question its parent's
framing itself -- its categories, its frame, the genealogy of its question -- is not held to answering that question
as its parent put it: check instead that it says what the question becomes under its critique (dissolved, recast,
narrowed) and on what grounds; that it does not answer the original question is no objection to it. Read what the
conclusion and the deliberation leave unsaid -- directions the plan did not take, questions the deliberation skirts --
where that silence carries the argument.
Different frameworks can yield different interpretations without either side being automatically wrong. A false
objection does as much damage as a false claim.

## acceptance criteria
- `{deliverable}` exists and every criticism explains its significance and the evidence, correction or concrete check needed to resolve it.
- Call `misaka_card_note` with the complete `issues` list: kind, question, rationale, priority, material.
  Use issues=[] explicitly if there are no issues. Only material=true issues require an answer from Last Order.
- Write the review under the card's deliverable directory. Do not create a machine-readable submission file.

"""

RED_TEAM_REREVIEW = """
## re-review (round {round})
You reviewed an earlier version of this conclusion in this conversation; this is version {round}. Go on from your
earlier review; your earlier critiques are also on file: {critiques}. Write this round's review to its own file, as
named above, and record this round's issues again: an earlier round's list does not carry over.
The previous version: `{previous}`. Last Order's dispositions of your earlier issues:
{dispositions}
Check whether each revision resolved what it set out to, whether each rebuttal holds, and what the revision itself
introduced or dropped: compare it with the previous version, the parts no issue touched included. Do not raise again what she conceded, marked covered or parked unless this version changes it; a rebuttal
you still reject is raised again, with why the reply does not answer it.
"""

DISPOSE_CONTRACT = """# Dispose of the red team's review: every material issue, exactly one disposition
Read the whole review and the recorded issues below, then call `misaka_research_dispose` once with one disposition per
issue:
- revise: this node reworks its conclusion -- with new cards while follow-up rounds remain, or by rewriting it. {revise}
  A gap is filled with research: while a follow-up round remains, a card, not a paragraph.
- rebut: the objection does not hold; the red team reads your reason when she reviews again.
- concede: the position stands and pays this cost; the final report lists it with the costs the answer accepts.
- covered: another node of this run owns this question (covered_by = its id; see the graph). Owning is not resolving: say how
  that node's work answers it, or what stays open.
- park: it stays open for the final adjudication, with why.
- branch: it reveals a genuine alternative rather than an error -- the framework breaks down, or a genealogical,
  symptomatic or ideological critique aims at the question itself; rework cannot repair that. It goes to this node's
  decision after the divergence review.{branch}
A correction is revise, never branch. A principled disagreement is rebut or concede, never revise: the review loop runs
again only while something is revised. Nothing here is handed to the nodes opened from this one: what this node does
not fill it concedes or parks, on the record.
An issue about material a card delivered can be put to the Sister who delivered it before you decide: `SendMessage`
her card id (a finished card is woken in her own session), then wait for her with
`misaka_sister_output(task_id, block=true)` and dispose in this same turn; her reply arrives in this conversation.
End in plain text after the tool accepts.
"""

DISPOSE_FINAL = """
No revision is left. A plain error the review names -- a wrong figure, date, name or reference, a sentence a revision
should have removed -- is `correct`: state the corrected text in `reason`; it is appended to the conclusion as a
correction, the record the red team does not review again. Do not edit the conclusion file yourself: the reviewed
text stays as reviewed, and the appendix carries the correction. Everything else is rebut, concede, covered or park."""

DIVERGENCE_CONTRACT = """## goal
Read the conclusion at `{synthesis_path}` for "{question}" through the choices it made: every choice gained something
and gave something up. Bring to light, and say of each proposal which it is:
- the possibilities it did not take -- those it gave up and those it never chose: other hypotheses or explanations,
  methodologies, theoretical frameworks, ideologies and concepts, perspectives, scenarios, readings, bodies of evidence
  and whose voices they carry (other sources, languages and actors -- those affected as well as those deciding), ways
  of decomposing the question, and critiques of the question itself (genealogy, a symptomatic reading of what the plan
  and the conclusion leave unsaid, ideology critique, rhizomatic connections), not limited to these. A possibility not
  taken cannot simply be added to the conclusion's line: it rests on another presupposition, or takes the answer where
  that line cannot go.
- the gaps it left -- what it neglected, did not know or did not ask, which any line answering this question would
  have to fill. A gap goes back to this node, which fills it before anything is opened from it; mark it `gap: true`.
You are not reviewing this conclusion for errors; the red team has done that. Deliver `{deliverable}` for the
node Last Order.
{rereview}

## presuppositions
Your work here is excavation, not verdict. The conclusion -- and the question and plan behind it -- stands on
presuppositions: concepts and categories taken as given, units of analysis, time frame and scope, the standpoint it
speaks from, what it counts as evidence and what authorises that, value premises, received views. For each that carries
weight, show where the conclusion uses it and what it gains, gives up and trades by it. Do not rule on whether it
holds: that is research. Propose the path that would investigate it -- what it would examine, and what the answer could
become if the presupposition does not hold -- and leave to Last Order which to pursue. A received view is one of them:
a consensus is a fact about the literature, not about the matter itself; ask whose view it is, formed when, from which
sources and interests, and what it leaves out.
Ways in, among others; use one where it bites, never as a label:
- The same word may no longer mean the same thing: what a key term signifies in its sources, in the literature and
  in the conclusion's own usage may have slid apart (the signified sliding under the signifier).
- What makes a claim count as knowledge may itself be in question: the episteme, the kind of record, the genre or the
  discipline that authorises the evidence can be investigated rather than trusted (archaeology and genealogy of
  knowledge).
- Suspend what is taken for granted and describe how an object is constituted in the sources before assuming it
  exists as named (phenomenological reduction).
- Take inherited concepts apart back to the experiences and questions they were formed to answer, and read what a
  frame presents as necessity or nature as a situation and a choice -- whose, and what it forecloses (destruktion;
  existential analysis).

## material
- Conclusion: `{synthesis_path}`
- Plan (every round of this node, oldest first): {plan_path}
- The paths list: `{paths_path}` -- every possibility raised in this run so far, under the node that raised it, with
  where it went. Read it before anything else. What is on it is not proposed again: when a node pursues it -- from
  whatever frame it was reached -- or an option waiting to be opened takes it up, name that node or option in
  `covered_by`; a path declined or left unopened comes back only with what answers the reason recorded for it.
  Its last part groups every possibility under what answers it now, so one reached from another frame shows beside
  the node that took it up: compare premises there, not labels.
- The research graph: `{graph_path}` -- every node and what it pursues.

## what makes a proposal
A proposal changes a premise, a method, a reading, a perspective or the decomposition, or names a gap, and says what it
could change in the answer and what research it would take. A possibility not taken names what in the conclusion it
cannot stand with; one the conclusion's own line could take in -- another tool, case or body of evidence under the same
premises -- is not one: it is a gap if the answer needs it, and not a proposal otherwise. Propose every genuine one you
find and none to fill a count; an error in the conclusion is the red team's, not yours. That the question itself
dissolves is a legitimate proposal.

## acceptance criteria
- `{deliverable}` exists: each proposal with whether it is a possibility not taken or a gap, its premise, what it could
  change in the answer, and what research it would take.
- Call `misaka_card_note` with the complete `alternatives` list (kind, proposal, premise, rationale, gap, covered_by).
  Use alternatives=[] explicitly if there are none.
- Write it under the card's deliverable directory. Do not create a machine-readable submission file.

"""

DIVERGENCE_REREVIEW = """
## re-review (round {round})
You reviewed an earlier version of this conclusion in this conversation; your earlier reviews are on file: {reviews}.
Last Order revised it to fill gaps you found. The previous version: `{previous}`. What she did with every gap so far:
{dispositions}
Check whether each gap she revised for is now filled, and raise again any that is not, with what is still missing;
raise what the revision itself left open. Do not raise again a gap she conceded, rebutted, marked covered or parked
unless this version changes it. The possibilities not taken you proposed are on record for the node's decision: do not
propose them again; propose only possibilities this version newly reveals. Record this round's list again (gaps and
new possibilities only; alternatives=[] when there are none): an earlier round's list does not carry over.
"""

GAPS_CONTRACT = """# Fill or answer the gaps the divergence review found: every gap, exactly one disposition
Read the whole review and the recorded gaps below, then call `misaka_research_dispose` once with one disposition per
gap. A gap is this node's own: it is filled here or answered here, never handed to the nodes opened from this one.
- revise: this node fills it -- with research: while a follow-up round remains, a card, not a paragraph; else by
  rewriting the conclusion. {revise} The divergence review reviews the revised version again.
- rebut: it is not a gap of this question; the reviewer reads your reason when she reviews again.
- concede: the conclusion stands without it and pays this cost; the final report lists it with the costs the answer
  accepts.
- covered: another node of this run owns it (covered_by = its id; see the graph). Say how that node's work answers it.
- park: it stays open for the final adjudication, with why.
A gap is never a fork. The review's possibilities not taken are not yours to answer here: they come to the node's
decision once no gap is left to fill. A gap about material a card delivered can be put to the Sister who delivered it
before you decide: `SendMessage` her card id (a finished card is woken in her own session), then wait for her with
`misaka_sister_output(task_id, block=true)` and dispose in this same turn. End in plain text after the tool accepts.
"""

DECIDE_CONTRACT = """# Decide the forks this node reveals
Below are the divergence review's proposals of possibilities your line did not take and the red-team issues you
disposed as `branch` (its gaps were filled or answered in the divergence rounds, and are settled). Read them
against the choices your conclusion made -- what each gained, gave up and traded -- before you record anything: a
possibility your line did not take -- one it gave up or one it never chose -- cannot stand together with it because it
rests on another presupposition or takes the answer where your line cannot go.
- Only a possibility not taken is a fork; a proposal your line already takes is none -- `decline` it, saying where
  your conclusion takes it. A decision is a question with its stakes and at least two options that exclude one
  another, each with its premise; this node's own line may be one option (`own: true`) beside a single possibility it
  did not take -- that is a real fork. Proposals that together make one other path are one option. A
  proposal that brings to light a presupposition your line rests on is a fork when the answer depends on it: the node
  opened for it researches whether the presupposition holds and what the answer becomes if it does not. A proposal
  aimed at the question itself (genealogy, symptomatic reading, ideology critique) is a possibility like any other,
  and so is one that questions a received view: that view is no reason to decline it -- only a reason that answers
  its challenge is.
- A possibility is opened once. One on the paths list that a node already pursues -- at any depth, reached from any
  frame -- or that an option waiting to be opened takes up is `covered` by that node or option, not a new option; what
  your line adds to it is for the reconciliation to join or relate. The same label is not the same possibility:
  compare premises -- the list and `on_your_line` give each one's.
- Your own line you know from the inside. `on_your_line` below lists what the nodes this one descends from already
  decided, each with where it was decided: in the conversation this one continues, or -- for the parents of a join --
  in the session file named. Before you take a proposal for one of them, read why it went where it went: above in
  this conversation, or, where that part was compacted or lies in another session, with the conversation tools you
  have or in the session file itself. A node of another line -- a sibling opened by the same decision as yours is
  one -- you know only by what it wrote: a proposal you mark `covered` by it, or by another node's waiting option, is
  a claim the reconciliation reviews with both lines in view -- say in `reason` which premise you compared and how
  that node's research answers it.
- A path the list shows declined or left unopened becomes an option again only with what answers the reason recorded
  then: name it in the option's `answers`, and let the option's premise say what answers that reason. Being covered
  is not being answered: a covered path whose covering node does not answer it comes back the same way.
Record every possibility not taken and invent none to fill a count; you may add forks you see yourself. Options open as
nodes when the root reconciles this level. Every proposal and every branch issue appears exactly once: in one option's
`sources` or in `declined` -- `covered` when a node of the graph already pursues it or an option waiting to be opened
takes it up (covered_by = its id), `decline` otherwise, with the reason that answers it; the final report records it
among the paths not taken.
Call `misaka_research_decide` once -- with decisions=[] when nothing forks -- then end in plain text.
"""

RECONCILE_CONTRACT = """# Reconcile this level of the research graph
Nodes of this level have finished their own work. Give every pending option below an outcome, then look across all
finished nodes. Call `misaka_research_reconcile` once, then end in plain text. Each finished node's conclusion is an
indexed document (`conclusion_doc`): `doc_outline` shows its sections, `doc_read` reads one, `doc_find` finds where the
conclusions say something. What relates two nodes is what their conclusions say, not whether they name each other.
- Options open as nodes. Options of different nodes that ask the same question go to ONE node (list all their ids):
  it has each of their nodes as a parent, and nobody researches that question twice. The same label is not the same
  question: two options named alike whose premises differ -- two readings under one name that rest on different
  definitions of their key concept -- are different possibilities. Write each new node's question for its Last Order.
- An option stays unopened only for a structural reason: it complements a node that exists or opens now (name it), a
  node already pursues it, it is a correction rather than a possibility, the depth or node limit stops it, or a limit
  the user set for this run does -- how many forks may open, say: then open the options whose reasons weigh most within
  it. Record that reason in `not_pursued`, and when a node answers the option -- it pursues it, or the option
  complements it -- that node's id in `covered_by` (the pending option you open for it, when it opens now). "Not
  worth it" is not a reason: a genuine possibility is opened.
- A possibility is opened once. An option whose premise a node already pursues -- at any depth, reached from any
  frame, as the paths list shows -- stays unopened: that node is its answer (name it). If
  what the option adds is that possibility meeting the proposing node's line, and the meeting is itself worth
  researching, join the two nodes (below): the join carries forward the research both already did instead of
  starting it again; otherwise record the link as a relation. A path the list shows declined or left unopened
  opens only when the option answers the reason recorded then: a pending option that brings such paths back shows
  them under `answers`, as the node that proposed it named them, each with where it went.
- Coverage claims: a node of this level closed a proposal as covered by a node -- or a waiting option -- of another
  line, so nothing was opened for it. That node knew the other line only by what it wrote, at the time it decided;
  here both are in view. Give every claim below a verdict in `coverage`: read the covering node's conclusion (as it
  stands now, and whether it was revised since the claim) against the proposal's premise. `holds` when that research
  answers the proposal -- say how; `reopen` when it does not -- say what that research leaves unanswered. `note` is the
  record of the verdict; the proposal keeps its own premise. A reopened proposal gets an outcome like a pending
  option: its id in a node's `options` -- that node opens under the node that raised the proposal, with pending
  options that ask the same question, if any -- or in `not_pursued`. A claim that holds may still call for a join or
  a relation, as an option does.
- Finished nodes whose paths arrive at the same place -- the same state, the same sub-question, the same finding by
  different routes -- may be carried forward together: a join is a new node with each of them as a parent. It shares
  their future work; their histories, evidence and premises stay their own, and it may fork again. Conclusions that
  merely agree are no reason to join: record `converge` (independent routes agreeing is itself a finding). In
  philosophical and interpretive work a join is a confrontation: it sublates what can be sublated and draws the
  disagreement sharply where it cannot; it never averages.
- Record relations between any nodes: converge, diverge, and the rhizomatic links -- resonates, appropriates,
  displaces. They schedule nothing; the final report reads them.
- A reconciliation that opens, joins and relates nothing is valid when nothing calls for it.
"""

DISSOLUTION_CONTRACT = """# The research question may dissolve: ask the user
Nodes of this run concluded that the research question itself does not stand as asked -- the node conclusions and
their grounds are below. That changes the user's question, so the final report waits for the user's decision. Present
it to the user in plain language: what the question rests on, why it dissolves, and what could be asked instead, with
the strongest reason to answer it as asked all the same. Ask whether the final answer may be this dissolution. Once
they have decided in conversation, record it with `misaka_research_consent`; it is available from the next turn on, so
this turn ends with your question to them. If they do not agree, the final report answers the question as asked and
gives the dissolution as one reading.
"""

RECONCILE_WAITS = """
# The reconciliation waits for the user
It is applied only once the user agrees. After the tool accepts, present it in plain language -- the nodes it opens and
why, the options it leaves unopened and why, the joins, and each coverage claim it upholds or reopens -- and talk it
over. Revise it with `misaka_research_reconcile`
(a call replaces the recorded one). Once the user has said it should go ahead, call `misaka_research_start`; it is
available from the next turn on, so this turn ends with your summary.
"""


def sister_catalog(root=None, *, workspace=None):
    # Compatibility entry for CLI callers; parsing and filtering have one owner.
    from misaka.core.network.roster import capability_catalog
    return capability_catalog(root, workspace=workspace)


def _catalog_text(items):
    return json.dumps(items, ensure_ascii=False, indent=2)


def _call(worker, cfg, prompt, *, cwd, session_dir, continue_session=False,
          profile="last_order", raw=False, task_id=None,
          model=None, extra_tools=(), con=None, session_file=None, sister_catalog=None):
    kwargs = {
        "cwd": cwd,
        "timeout": None,
        "raw": raw, "usage_db": cfg.get("db"), "usage_task_id": task_id,
        "usage_generation": 1, "usage_token_cap": cfg.get("token_cap"),
        "session_dir": session_dir, "continue_session": continue_session,
    }
    if session_file:
        kwargs["session_file"] = session_file
    if extra_tools:
        kwargs["extra_tools"] = extra_tools
    if sister_catalog is not None:
        kwargs["sister_catalog"] = sister_catalog
    if model:
        kwargs["model"] = model
    # Waiting for room under the cap happens per request, in the meter; a run whose cap is
    # spent comes back with budget.EXHAUSTED_MESSAGE and stops there.
    return worker.run_llm_json(
        os.path.join(cfg["roles_root"], profile), prompt,
        cfg["provider"], cfg["default_model"], **kwargs,
    )


def publish_project_brief(workspace, plan, *, round=1):
    """The accepted root plan is also the brief; no separate model or conversation. A later
    round of the root is appended as its own section, never written over the brief."""
    path = Path(workspace) / "PROJECT.md"
    if plan["status"] != "ready":
        return str(path)
    if not path.is_file():
        atomic.write_text(str(path), plan["plan_markdown"].rstrip() + "\n")
    elif round > 1:
        marker = f"\n\n## Round {round}\n\n"
        current = path.read_text(encoding="utf-8")
        if marker.strip() not in current:
            atomic.write_text(str(path), current.rstrip() + marker + plan["plan_markdown"].rstrip() + "\n")
    return str(path)


def _validate_task(raw, roster, index):
    if not isinstance(raw, dict):
        raise ValueError(f"Task {index} is not an object.")  # noqa: TRY004 - callers treat bad input as ValueError
    required = ("local_id", "title", "question", "rationale", "deliverable", "assignee")
    task = {k: raw.get(k) for k in raw}
    for key in required:
        if not isinstance(task.get(key), str) or not task[key].strip():
            raise ValueError(f"Task {index} is missing {key!r}.")
        task[key] = task[key].strip()
    try:
        one_file(split_deliverable(task["deliverable"])[0])  # validate the artifact gate before any cards are created
    except ValueError as error:
        raise ValueError(f"Task {task['local_id']}: {error}") from error
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", task["local_id"]):
        raise ValueError(
            f"Task {index} local_id may contain only letters, digits, dots, underscores, and hyphens."
        )
    if task["assignee"] not in roster:
        raise ValueError(
            f"Task {task['local_id']} names a Sister outside the roster: {task['assignee']}"
        )
    for key in ("method", "source_strategy", "falsifiers", "assignee_reason"):
        task[key] = str(task.get(key) or "").strip()
    for key in ("dependencies", "capabilities"):
        value = task.get(key)
        task[key] = [str(x).strip() for x in value or [] if str(x).strip()] if isinstance(value, list) else []
    try:
        task["priority"] = int(task.get("priority") or 0)
    except (TypeError, ValueError):
        task["priority"] = 0
    task["extensions"] = raw.get("extensions") if isinstance(raw.get("extensions"), dict) else {}
    return task


def _validate_tasks(raw_tasks, roster_ids):
    tasks = [_validate_task(raw, roster_ids, i) for i, raw in enumerate(raw_tasks or [], 1)]
    local_ids = [task["local_id"] for task in tasks]
    if len(local_ids) != len(set(local_ids)):
        raise ValueError("Last Order returned duplicate task local_id values.")
    known = set(local_ids)
    for task in tasks:
        unknown = set(task["dependencies"]) - known
        if unknown:
            raise ValueError(
                f"Task {task['local_id']} refers to unknown dependencies: {sorted(unknown)}"
            )
        if task["local_id"] in task["dependencies"]:
            raise ValueError(f"Task {task['local_id']} depends on itself.")
    pending = {task["local_id"]: set(task["dependencies"]) for task in tasks}
    while pending:
        ready = {local_id for local_id, deps in pending.items() if not (deps & pending.keys())}
        if not ready:
            raise ValueError("Research task dependencies contain a cycle.")
        for local_id in ready:
            pending.pop(local_id)
    return tasks


def _validate_decisions(raw, *, tasks, allowed):
    """The plan's forks: each a question, its stakes and two or more options with premises, at most
    one of them this node's own line -- which needs this node's own cards to pursue it."""
    decisions = []
    for index, item in enumerate(raw or [], 1):
        if not allowed:
            raise ValueError("Decisions belong to a node's first plan below max_depth; this plan cannot fork. "
                             "Assign the work as cards, or leave the alternative to the node's decision after review.")
        if not isinstance(item, dict):
            raise ValueError(f"Decision {index} is not an object.")  # noqa: TRY004 - callers treat bad input as ValueError
        options = item.get("options") or []
        if len(options) < 2:
            raise ValueError(f"Decision {index} needs at least two options; a single path is a card, not a fork.")
        if sum(bool(option.get("own")) for option in options) > 1:
            raise ValueError(f"Decision {index} marks more than one option as this node's own line.")
        if any(option.get("own") for option in options) and not tasks:
            raise ValueError(f"Decision {index} names this node's own line, but the plan gives the node no cards to pursue it.")
        decisions.append({"question": str(item["question"]).strip(), "stakes": str(item["stakes"]).strip(),
                          "options": [{"label": str(o["label"]).strip(), "premise": str(o["premise"]).strip(),
                                       "own": bool(o.get("own"))} for o in options]})
    return decisions


def validate_plan(obj, roster, *, decisions_allowed=False, root=False, red_team_required=True):
    if not isinstance(obj, dict):
        raise ValueError("Last Order planning output is not an object.")  # noqa: TRY004 - callers treat bad input as ValueError
    status = obj.get("status")
    if status not in {"ready", "clarify"}:
        raise ValueError("Last Order returned an invalid planning status.")
    plan_markdown = obj.get("plan_markdown")
    if not isinstance(plan_markdown, str) or not plan_markdown.strip():
        raise ValueError("Last Order returned an incomplete plan_markdown value.")
    roster_ids = {r["id"] if isinstance(r, dict) else str(r) for r in roster}
    tasks = _validate_tasks(obj.get("tasks"), roster_ids)
    decisions = _validate_decisions(obj.get("decisions"), tasks=tasks, allowed=decisions_allowed)
    if status == "ready" and not tasks and not decisions:
        raise ValueError("A ready research plan must contain at least one task or one decision.")
    questions = obj.get("clarifying_questions") or []
    if not isinstance(questions, list) or any(not isinstance(q, str) or not q.strip() for q in questions):
        raise ValueError("clarifying_questions must be a list of non-empty strings.")
    if status == "clarify" and not questions:
        raise ValueError("A clarify plan must ask at least one question.")
    red_team = obj.get("red_team") if isinstance(obj.get("red_team"), dict) else {}
    if not red_team_required:
        red_team = {}   # a follow-up round keeps the red team the node's first plan named
    # The root's red team also reviews the final report, so the root names one even as a pure branch point.
    elif status == "ready" and (tasks or root) and red_team.get("assignee") not in roster_ids:
        raise ValueError("Last Order must name one roster Sister as the red team.")
    return {
        **obj, "status": status, "plan_markdown": plan_markdown.strip(), "tasks": tasks, "decisions": decisions,
        "red_team": ({"assignee": red_team.get("assignee"), "reason": str(red_team.get("reason") or "")}
                     if red_team.get("assignee") else None),
        "clarifying_questions": [q.strip() for q in questions],
        "reframed_question": " ".join(str(obj.get("reframed_question") or "").split()),
        "methods": obj.get("methods") if isinstance(obj.get("methods"), list) else [],
        "extensions": obj.get("extensions") if isinstance(obj.get("extensions"), dict) else {},
    }


def decisions_allowed(con, run, node, round):
    """A node forks in its first plan, and only where a child could still open."""
    return int(round) <= 1 and int(node["depth"]) < runs.current_limits(con, run)["max_depth"]


def _roster(cfg):
    roster = sister_catalog(cfg.get("profiles_root"), workspace=cfg.get("workspace"))
    if not roster:
        raise RuntimeError("The Sister roster is empty; research tasks cannot be assigned.")
    return roster


def _lo_session(run, node, *parts):
    if runs.is_root(node) and run["root_session"]:
        return os.path.join(os.path.dirname(run["root_session"]), *parts)
    return runs.session_dir(run, "root-lo" if runs.is_root(node) else f"node-{node['id']}", *parts)


def lo_session_file(run, node):
    """The conversation a node's Last Order lives in: the root's is the run's window."""
    return run["root_session"] if runs.is_root(node) else node["session_file"]


def plan_waits_for_user(cfg, run=None):
    """Persisted run policy wins for every root, node and follow-up plan and every reconciliation,
    including resume."""
    saved = runs.limits(run) if run is not None else {}
    return saved.get("plan_approval", bool(cfg.get("research_plan_approval", True)))


def plan_needs_user(cfg, run, plan):
    """Whether this plan waits for the user's go-ahead: under the run's approval policy, and in any
    run when it changes the question -- a reframing takes effect only once the user agrees."""
    return plan_waits_for_user(cfg, run) or bool(plan.get("reframed_question"))


PLAN_WAITS = """
# The plan waits for the user
An accepted plan is not executed until the user agrees to it -- this plan, here, whether it is the root's, another
node's, or a follow-up round's. An ancestor's approval does not carry over, and the driver never starts a node on
its own: it waits for your `misaka_research_start`. After the tool accepts your command, present the
plan to the user in plain language and talk it over with them. Revise it with `misaka_research_assign` (a call
replaces the recorded plan; with `append: true` it adds to it). Once the user has said the plan should go ahead, call `misaka_research_start`; it
is available from the next turn on, so this turn ends with your summary. While a follow-up round waits,
`misaka_research_withdraw` drops it if the user would rather have the conclusion from what there is. If the user
would rather not research a non-root node at all, `misaka_research_skip` (its first plan only) closes it
unresearched: no cards, no conclusion, the reason you record on file for the final adjudication. Never
fake a completion or write one into project files instead. Anything
you would otherwise put in
`clarifying_questions`, ask the user in that conversation instead; `status=clarify` is for runs nobody is
talking to. If the question itself seems wrong, propose the reframing in `plan_markdown` and put the new
wording in `reframed_question`: it takes effect only once the user agrees.
"""

PLAN_AUTOMATIC = """
# Plan execution
This phase has no additional plan-approval wait. After an accepted ready plan, the driver proceeds within the
run's approved scope and configured limits. Report the handoff; do not request a redundant go-ahead or claim the
research is complete. A genuine clarification, pause or change of scope still needs the appropriate decision: a
plan that sets `reframed_question` waits for the user's go-ahead (`misaka_research_start`) even in this run.
"""


def plan_approval_prompt(cfg, run=None):
    """Describe the same gate the driver applies, including follow-up plans."""
    return PLAN_WAITS if plan_waits_for_user(cfg, run) else PLAN_AUTOMATIC


def user_instructions(run):
    """The run's question in the user's own words, for the phases that decide who works and what opens.
    What it says about carrying the research out -- which or how many Sisters, how many forks, how fast --
    holds for every node, not only the root that read it first (B122: a node's plan took a third Sister
    and the root's reconciliation opened six forks where the user had allowed two)."""
    return (f"\n# The user's question for this run, in their words\n{run['question']}\n"
            "What it asks of how the research is carried out -- which or how many Sisters work, how many forks "
            "open, how soon the run should end -- holds here as it did at the root.\n")


def plan(run, cfg, worker, node, *, con, context_path=None):
    """Open or continue the node's Last Order session and return its research plan."""
    session_dir = _lo_session(run, node)
    roster = _roster({**cfg, "workspace": run["workspace"]})
    forks = decisions_allowed(con, run, node, 1)
    prompt = ROOT_CONTRACT + "\n# Artifact layout\nWithin the current workspace, every node's files live under " \
        "nodes/<node>/ and each of its cards under nodes/<node>/cards/<card>/; run-level products go to final/. " \
        "The runtime assigns these paths. Put one deliverable filename on the first line, not a directory; " \
        "use backticks for spaces. Put requirements on following lines; a card delivers one file, so other files " \
        "it writes (a script, data) go there too.\n" \
        + f"\n# Current node depth\n{node['depth']} (root = 0; max_depth = {runs.current_limits(con, run)['max_depth']})" \
        + ("" if forks else " -- this node cannot fork: no `decisions` here.") + "\n"
    if runs.is_root(node):
        brief = Path(run["workspace"]) / "PROJECT.md"
        brief_instruction = (f"PROJECT.md already exists. Read `{brief}` and respect its scope."
                             if brief.is_file() else
                             "PROJECT.md does not exist yet. Do not try to read it. Begin plan_markdown "
                             "with the original question, research goal, assumptions to test, and boundaries. "
                             "After your plan is accepted, the driver writes it to PROJECT.md before any "
                             "Sister task starts; no separate write call or intake session is needed.")
        prompt += f"""
# Original question
{run['question']}

# Project brief and root plan are ONE deliverable
{brief_instruction}
Do not make a separate intake call or delegate this design to another Last Order.
Assumptions are questions, not conclusions.
"""
    else:
        prompt += f"""
This node pursues one possibility on the research graph. Plan its own research without replanning the whole project,
and without planning what another node of the graph already owns.

# This node's question
{node['question']}

# How this node was reached
{position(con, run, node)}
# Context packet (its parents' conclusions, critiques, dispositions and decisions; read it first)
{context_path}
""" + user_instructions(run)
    prompt += navigation(run["id"]) + """
Read the live workspace view before planning.
"""
    prompt += plan_approval_prompt(cfg, run)
    action, raw = _command(
        con, run, cfg, worker, node, prompt, key="plan", name="misaka_research_assign",
        description="Last Order: assign the research plan, record its forks, and choose its red-team Sister",
        model=commands.Plan,
        validate=lambda value: validate_plan(value, roster, decisions_allowed=forks, root=runs.is_root(node)),
        session_dir=session_dir, sister_catalog=roster, merge=commands.merge_plan,
        reply=lambda payload: "Accepted misaka_research_assign. " + commands.plan_receipt(payload, forks=forks),
    )
    return action["payload"], raw, action["session_file"]


def _command(con, run, cfg, worker, node, prompt, *, key, name, description, model, validate,
             session_dir, sister_catalog=None, merge=None, reply=None):
    previous = runs.action(con, run["id"], node["id"], key)
    if previous:
        return previous, ""
    session_file = lo_session_file(run, node)
    command = commands.tool(con, run, node, key=key, name=name, description=description,
                            model=model, validate=validate, session_dir=session_dir,
                            session_file=session_file, merge=merge, reply=reply)
    return commanded(
        worker, cfg, prompt, command=command, name=name, what=f"{name} for {key}",
        recorded=lambda: runs.action(con, run["id"], node["id"], key),
        cwd=run["workspace"], session_dir=session_dir, raw=True, sister_catalog=sister_catalog,
        continue_session=bool(findMostRecentSession(session_dir)), task_id=run["id"], con=con,
        session_file=session_file)


def commanded(worker, cfg, prompt, *, command, name, recorded, what=None, **call):
    """One Last Order turn that must end in ``command``; ``recorded()`` is what it records. A reply
    that ends without it -- it spent the whole output cap (thinking included), or it answered in
    prose: a dispose turn that wrote the revised conclusion instead (2026-09-28) -- gets one more turn
    in the same session, told exactly what happened; a second miss fails the phase. Failing at once
    failed the run and cost a manual resume."""
    call["extra_tools"] = (command, *call.get("extra_tools", ()))
    _obj, text, err = _call(worker, cfg, prompt, **call)
    if recorded() is None and missed_command(err):
        _obj, text, err = _call(worker, cfg, command_nudge(name, err), **{**call, "continue_session": True})
    accepted = recorded()
    if accepted is None:
        raise RuntimeError(f"Last Order did not call {what or name}: {err or 'no command accepted'}")
    return accepted, text


def missed_command(err):
    """Whether a turn that recorded no command only missed it -- ran out of output, or ended in
    prose -- rather than failed: a provider error or a stop is not retried."""
    return not err or hit_output_limit(err)


def command_nudge(name, err):
    """The follow-up prompt after a reply that ended without calling ``name``."""
    if hit_output_limit(err):
        return output_limit_nudge(name)
    return (f"Your previous reply ended without calling `{name}`; nothing was recorded, and this phase "
            f"does not move on without it. Call `{name}` now with what this phase asked for, and nothing "
            "beyond it. Keep any reasoning brief: the work you already did is in this conversation.")


def output_limit_nudge(name):
    """The follow-up prompt after a reply that hit the output cap without calling ``name``. A plan
    is never told to shrink: it can be submitted in several appending calls instead."""
    size = (" A plan too large for one reply goes in several calls: the first with `plan_markdown` and "
            "the first tasks, the rest with `append: true`; do not shrink it to fit one call."
            if name == "misaka_research_assign" else " Keep the payload compact.")
    return (f"Your previous reply reached the model's output token limit before it called `{name}`; "
            f"nothing was recorded. Call `{name}` now. Keep any reasoning brief: the work you already "
            "did is in this conversation, so do not redo it." + size)


def task_sources(con, run, rows):
    """What each done card delivered, from frozen records only: its submitted event (this
    generation), its registered artifacts, and the evidence ledger."""
    from misaka.core.platform import cards

    parts = []
    for row in rows:
        payload = json.loads(task_store.latest_payload(
            con, row["id"], "submitted", generation=row["generation"]) or "{}")
        artifacts = []
        for item in runs.artifacts(con, run["id"], task_id=row["id"]):
            path = runs.artifact_path(item)
            if os.path.isfile(path):
                artifacts.append(path)
        finds = []
        for finding in ledger.findings(con, run["id"], task_id=row["id"]):
            finds.append({"text": finding["text"], "claim_type": finding["claim_type"],
                          "claims": [_located(claim, run) for claim in ledger.claims(con, finding["id"])]})
        # The ledger already holds what it accepted; only a submitted finding it does not hold
        # (rejected, or never ingested) is worth a second listing.
        recorded = {item["text"] for item in finds}
        submitted = [item for item in payload.get("findings", [])
                     if not (isinstance(item, dict) and item.get("text") in recorded)]
        parts.append({"task_id": row["id"], "title": row["title"],
                      "card_path": cards.card_path(row["workspace"], row["id"]),
                      "summary": str(payload.get("summary") or ""), "artifacts": artifacts,
                      "findings": finds, "submitted_findings": submitted,
                      "uncertain": payload.get("uncertain") or []})
    return parts


def _located(claim, run):
    """A claim as Last Order and the red team read it: where its quotation actually is, if anywhere."""
    out = dict(claim)
    where = ledger.locate(claim, run["workspace"])
    if where:
        out["located"] = where
    return out


def evidence_block(con, run, node):
    """The node's declarations and source locators, wrapped as untrusted data."""
    findings = []
    for finding in ledger.findings(con, run["id"], branch_id=node["id"]):
        findings.append({**dict(finding),
                         "claims": [_located(row, run) for row in ledger.claims(con, finding["id"])]})
    return prompt_guard.untrusted("evidence-ledger", json.dumps(findings, ensure_ascii=False, indent=2))


def deliberation_text(session_file, fork_entry=None, *, given=(), earlier=None):
    """Last Order's own reasoning on this node, for the red team to probe: the thinking blocks and
    working prose of her assistant turns on the conversation's current branch after ``fork_entry``
    -- the last entry the node inherited (a fork's parent turns, the root window's turns before the
    run), or for a re-review the last entry the previous deliberation covered, whose file is
    ``earlier`` -- and nothing else. Everything the red team already holds by other means is left
    out on purpose -- the user turns (the workflow's own prompts), tool calls and their arguments,
    prose that is part of a text she gets by path (``given``: the conclusions and plans, which a
    synthesis turn's final message is), what other nodes own (on the graph), tool results (the
    evidence and Sister artifacts are given separately), custom entries and redacted thinking (no
    text to read). A branch the conversation left is not read either. Returns "" when the node
    holds no such reasoning."""
    from misaka.core.session_manager import SessionManager
    if not session_file or not os.path.isfile(session_file):
        return ""
    branch = SessionManager.open(session_file).getBranch()
    note = ""
    if fork_entry:
        position = next((index for index, entry in enumerate(branch) if entry.get("id") == fork_entry), None)
        if position is None:
            note = (f"The node's starting point (entry {fork_entry}) is not on this conversation's current branch, "
                    "so everything on the branch is shown: part of it may precede this node.")
        else:
            branch = branch[position + 1:]
    turns = []
    for entry in branch:
        if entry.get("type") != "message":
            continue
        message = entry.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "thinking" and not block.get("redacted"):
                text = block.get("thinking")
                if isinstance(text, str) and text.strip():
                    parts.append(("thinking", text.strip()))
            elif block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip() and not any(text.strip() in held for held in given):
                    parts.append(("text", text.strip()))
        if parts:
            turns.append(parts)
    if not turns:
        return ""
    lines = ["# Last Order's deliberation", "",
             (f"Her thinking and working notes on this node since the previous review, in order; what came before "
              f"is in `{earlier}`." if earlier else
              "Her thinking and working notes on this node since it began, in order; turns the node inherited "
              "from its parent (or the conversation before the run) are not included."),
             "Probe them for reasoning failures. They are not the conclusion and carry no authority.", ""]
    if note:
        lines += [f"> {note}", ""]
    for index, parts in enumerate(turns, 1):
        lines.append(f"## turn {index}")
        for kind, text in parts:
            lines.append(f"### {kind}")
            lines.append(text)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def disposition_table(rows):
    """Issues with what Last Order did with each, as Markdown for a prompt."""
    lines = ["| Issue | Question | Disposition | To | Reason |", "| --- | --- | --- | --- | --- |"]
    lines += [f"| `{row['id']}` | {_md_cell(row['question'])} | {row['disposition'] or 'undisposed'} "
              f"| {runs.destination(row) or '-'} | {_md_cell(row['reason'])} |" for row in rows]
    return "\n".join(lines)


def _md_cell(value):
    return " ".join(str(value or "").splitlines()).strip().replace("|", r"\|")


def synthesize(con, run, cfg, worker, node, task_rows, *, followup=None, round=1, left=0, revision=None,
               dissolve=None, erratum=None):
    """Last Order writes the node's conclusion from its accepted cards. Returns Markdown. With
    ``followup`` (the next round's plan tool) she may instead assign more cards; the caller sees
    that as the recorded action, not in the text. ``left`` is how many more rounds the node may
    still ask for; a node that could never ask (or is on its first and only round) hears nothing
    about rounds. ``revision`` (version, review round, previous conclusion path, critique paths,
    dispositions) makes this the rework of a reviewed conclusion. ``dissolve`` records a conclusion
    dissolving a question instead of answering it; ``erratum`` a correction to an earlier node's."""
    if followup is not None:
        rounds = (SYNTHESIS_FOLLOWUP.format(round=round, left=left)
                  + "\nThe following approval policy applies only if you submit a follow-up plan; "
                  "it does not block writing the current conclusion.\n" + plan_approval_prompt(cfg, run))
    elif round > 1:
        rounds = SYNTHESIS_LAST_ROUND.format(round=round)
    else:
        rounds = ""
    if revision is not None:
        template = SYNTHESIS_GAP_REVISION if revision.get("reviewer") == "divergence" else SYNTHESIS_REVISION
        rounds = template.format(
            version=revision["version"], review=revision["review"], previous=revision["previous"],
            critiques=", ".join(f"`{path}`" for path in revision["critiques"]) or "(none)") \
            + prompt_guard.untrusted("dispositions", revision["dispositions"]) + "\n" + rounds
    if not runs.is_root(node):
        rounds += "\n# How this node was reached\n" + position(con, run, node) + errata_above(con, run, node)
    # The material map lists every card's ledger findings with their claims; the ledger block
    # the red team gets would repeat all of them here, so it is left out of this turn.
    prompt = (SYNTHESIS_CONTRACT + rounds + f"""
# Question
{node['question']}

# Material map (read the artifacts, not just this map; each card's findings and their source claims are here)
""" + prompt_guard.untrusted("research-material-map",
                            json.dumps(task_sources(con, run, task_rows), ensure_ascii=False, indent=2))
              + navigation(run["id"]))
    session_dir = _lo_session(run, node)
    _obj, text, err = _call(
        worker, cfg, prompt, cwd=run["workspace"], session_dir=session_dir, raw=True,
        continue_session=bool(findMostRecentSession(session_dir)), task_id=run["id"], con=con,
        session_file=lo_session_file(run, node),
        extra_tools=tuple(tool for tool in (followup, dissolve, erratum) if tool is not None),
    )
    if followup is not None and runs.plan_round(con, run["id"], node["id"]) > round:
        return ""                                     # she asked for another round instead of concluding
    if err or not text or not text.strip():
        raise RuntimeError(f"Synthesis for node {node['id']} is empty or failed: {err or ''}")
    return text.strip() + "\n"


def red_team_body(node, *, synthesis_path, plan_path, graph_path, evidence="", deliberation_path=None, own_cards=(),
                  rereview=None, deliverable="critique.md"):
    own = ("- Cards on this node you researched yourself, in an earlier session of yours (this review is not "
           "third-party to them; hold them to the same standard): "
           + ", ".join(f"[{row['id']}] {row['title']}" for row in own_cards) + "\n") if own_cards else ""
    deliberation = (
        "- Last Order's deliberation on this node -- her thinking and working notes since "
        + ("your previous review" if rereview is not None else "the node began")
        + ", to probe for reasoning failures; not the conclusion, and no authority over it: "
        f"`{deliberation_path}`\n" if deliberation_path else "")
    again = ""
    if rereview is not None:
        again = RED_TEAM_REREVIEW.format(
            round=rereview["round"], previous=rereview["previous"],
            critiques=", ".join(f"`{path}`" for path in rereview["critiques"]) or "(none)",
            dispositions=rereview["dispositions"])
    return RED_TEAM_CONTRACT.format(
        question=node["question"], synthesis_path=synthesis_path, plan_path=plan_path, graph_path=graph_path,
        deliberation=deliberation, own_cards=own, rereview=again, deliverable=deliverable,
        evidence=f"- Evidence ledger:\n{evidence}\n" if evidence else "")


def divergence_body(node, *, synthesis_path, plan_path, graph_path, paths_path, deliverable="divergence.md",
                    rereview=None):
    again = DIVERGENCE_REREVIEW.format(
        round=rereview["round"], previous=rereview["previous"],
        reviews=", ".join(f"`{path}`" for path in rereview["reviews"]) or "(none)",
        dispositions=rereview["dispositions"]) if rereview is not None else ""
    return DIVERGENCE_CONTRACT.format(question=node["question"], synthesis_path=synthesis_path,
                                      plan_path=plan_path, graph_path=graph_path, paths_path=paths_path,
                                      deliverable=deliverable, rereview=again)


def fork_session(source, target_dir):
    """Fork the owning session, never the newest unrelated chat in its directory. Returns
    ``(path, fork_entry)``: the new session file and the last entry it inherited -- the last
    non-label entry of the parent's branch, since forking re-creates labels under new ids."""
    from misaka.core.session_manager import SessionManager
    if not source:
        return None, None
    os.makedirs(target_dir, exist_ok=True)
    manager = SessionManager.open(source, target_dir)
    leaf = manager.getLeafId()
    if not leaf:
        return None, None
    inherited = next((entry.get("id") for entry in reversed(manager.getBranch(leaf)) if entry.get("type") != "label"), None)
    branched = manager.createBranchedSession(leaf)
    if branched and not os.path.isfile(branched):
        manager.rewrite_file()  # the write is deferred until an assistant turn; the fork resumes from disk
    return branched, inherited


def session_leaf(session_file):
    """The last non-label entry on a conversation's current branch: where a node that starts in
    it now begins. None for a conversation with nothing in it yet."""
    from misaka.core.session_manager import SessionManager
    if not session_file or not os.path.isfile(session_file):
        return None
    branch = SessionManager.open(session_file).getBranch()
    return next((entry.get("id") for entry in reversed(branch) if entry.get("type") != "label"), None)


def review_context(con, run, red, issues, *, round, kind="critique"):
    """One round's frozen review: a review card (the red team's, or the divergence review's when
    ``kind`` is ``divergence``) reviews every version, each round in its own file."""
    review = task_sources(con, run, [red])
    name = runs.versioned(f"{kind}.md", round)
    review[0]["review_text"] = [
        {"path": runs.artifact_path(item), "content": runs.artifact_text(item)}
        for item in runs.artifacts(con, run["id"], kind=kind, task_id=red["id"])
        if Path(item["path"]).name == name
    ]
    heading = "Red-team review" if kind == "critique" else "Divergence review"
    return (f"\n# {heading}\n" + prompt_guard.untrusted("red-team-results", _catalog_text(review))
            + "\n# Recorded material issues\n" + prompt_guard.untrusted("issues", _catalog_text([dict(i) for i in issues])))


def dispose(con, run, cfg, worker, node, red, issues, *, round, revisions_left):
    """Last Order's dispositions for one review round: every material issue exactly once."""
    from misaka.core.research import views
    key = runs.dispose_key(round)
    accepted = runs.action(con, run["id"], node["id"], key)
    if accepted:
        return accepted["payload"]["dispositions"]
    expected = {item["id"] for item in issues}
    forks = int(node["depth"]) < runs.current_limits(con, run)["max_depth"]
    known = {row["id"] for row in runs.nodes(con, run["id"])}

    def validate(value):
        ids = [item["issue_id"] for item in value["dispositions"]]
        if len(ids) != len(set(ids)) or set(ids) != expected:
            raise ValueError("Give every recorded material issue of this review exactly one disposition; "
                             "use only this review's issue ids.")
        for item in value["dispositions"]:
            if item["disposition"] == "revise" and revisions_left <= 0:
                raise ValueError("No revision is left on this node: correct a plain error, else rebut, concede, park, "
                                 "or mark the issue covered.")
            if item["disposition"] == "correct" and revisions_left > 0:
                raise ValueError("A revision is left on this node: a correction is a revise.")
            if item["disposition"] == "branch" and not forks:
                raise ValueError("This node is at max_depth and cannot fork: rebut, concede or park the issue.")
            if item["disposition"] == "covered" and (item["covered_by"] not in known or item["covered_by"] == node["id"]):
                raise ValueError(f"covered needs the id of another node of this run as covered_by (issue {item['issue_id']}).")
            if item["disposition"] != "covered" and item["covered_by"]:
                raise ValueError(f"Only covered takes covered_by (issue {item['issue_id']}).")
        return value

    revise = (f"{revisions_left} revision(s) left on this node." if revisions_left > 0 else
              "No revision is left on this node: answer every issue another way.")
    prompt = (DISPOSE_CONTRACT.format(revise=revise, branch="" if forks else " Not available: this node is at max_depth.")
              + (DISPOSE_FINAL if revisions_left <= 0 else "")
              + f"\n# Round {round} of review\nThe graph: `{views.graph_path(run)}`\n"
              + review_context(con, run, red, issues, round=round) + navigation(run["id"]))
    action, _raw = _command(
        con, run, cfg, worker, node, prompt, key=key, name="misaka_research_dispose",
        description="Last Order: dispose of every material issue of this red-team review",
        model=commands.Dispositions, validate=validate, session_dir=_lo_session(run, node),
    )
    return action["payload"]["dispositions"]


def fill_gaps(con, run, cfg, worker, node, card, gaps, *, version, round, revisions_left):
    """Last Order's answer to divergence round ``round``: every gap it found in version ``version``,
    exactly once -- filled by a revision while the node has gap revisions left, else answered."""
    from misaka.core.research import views
    key = runs.gaps_key(version)
    accepted = runs.action(con, run["id"], node["id"], key)
    if accepted:
        return accepted["payload"]["dispositions"]
    expected = {item["id"] for item in gaps}
    known = {row["id"] for row in runs.nodes(con, run["id"])}

    def validate(value):
        ids = [item["issue_id"] for item in value["dispositions"]]
        if len(ids) != len(set(ids)) or set(ids) != expected:
            raise ValueError("Give every gap of this divergence review exactly one disposition; "
                             "use only this review's gap ids.")
        for item in value["dispositions"]:
            if item["disposition"] in ("branch", "correct"):
                raise ValueError(f"A gap is filled or answered: revise, rebut, concede, covered or park "
                                 f"(issue {item['issue_id']}).")
            if item["disposition"] == "revise" and revisions_left <= 0:
                raise ValueError("No gap revision is left on this node: rebut, concede, park, or mark the gap covered.")
            if item["disposition"] == "covered" and (item["covered_by"] not in known or item["covered_by"] == node["id"]):
                raise ValueError(f"covered needs the id of another node of this run as covered_by (issue {item['issue_id']}).")
            if item["disposition"] != "covered" and item["covered_by"]:
                raise ValueError(f"Only covered takes covered_by (issue {item['issue_id']}).")
        return value

    revise = (f"{revisions_left} gap revision(s) left on this node." if revisions_left > 0 else
              "No gap revision is left on this node: answer every gap another way.")
    prompt = (GAPS_CONTRACT.format(revise=revise)
              + f"\n# Divergence review of version {version}\nThe graph: `{views.graph_path(run)}`\n"
              + review_context(con, run, card, gaps, round=round, kind="divergence") + navigation(run["id"]))
    action, _raw = _command(
        con, run, cfg, worker, node, prompt, key=key, name="misaka_research_dispose",
        description="Last Order: fill or answer every gap of this divergence review",
        model=commands.Dispositions, validate=validate, session_dir=_lo_session(run, node),
    )
    return action["payload"]["dispositions"]


CARDS_FAILED_CONTRACT = """# Cards of this node failed
The cards below failed for good -- each after its Sister's own attempts -- and the cards waiting on them were stopped.
Decide what the node does now with `misaka_research_cards_failed`, then end in plain text:
- retry: the failure looks passing -- a provider refusing or overloaded, a process that died. The cards go back to
  their Sisters and go on in their own conversations.
- conclude: go on with the research cards that finished. Your conclusion says what the missing cards were for and what
  their absence leaves open.
- fail: the node fails; the root Last Order may run it again later.
"""

RETRY_CARDS_LIMIT = 2   # ponytail: a fixed cap; cards failing a third time are the root's and the user's to judge


def cards_failed(con, run, cfg, worker, node, *, kind, lost):
    """The node's Last Order decides what becomes of its cards that failed for good: ``retry``
    them, ``conclude`` without them (research cards, when some finished), or ``fail`` the node.
    With nothing but failing left to choose, no turn is taken."""
    retried = sum(d["decision"] == "retry" for d in runs.failed_card_decisions(con, run["id"], node["id"]))
    finished = [row for row in runs.tasks(con, run["id"], kind=kind, node_id=node["id"]) if row["status"] == "done"]
    choices = [choice for choice, open_ in (("retry", retried < RETRY_CARDS_LIMIT),
                                             ("conclude", kind == "research" and bool(finished)),
                                             ("fail", True)) if open_]
    if choices == ["fail"]:
        return {"decision": "fail", "reason": f"no retry left after {retried}, and nothing finished to go on with",
                "cards": [row["id"] for row in lost]}

    def validate(value):
        if value["decision"] not in choices:
            raise ValueError(f"{value['decision']} is not open here; choose one of: {', '.join(choices)}.")
        return {**value, "cards": [row["id"] for row in lost]}

    def failure(row):
        failed = task_store.latest_payload(con, row["id"], "failed", generation=row["generation"])
        waited = task_store.latest_payload(con, row["id"], "dependency_failed", generation=row["generation"])
        return failed or (f"stopped: waits on {waited}" if waited else "no failure detail recorded")

    material = {"failed": [{"card": row["id"], "title": row["title"], "sister": row["assignee"], "status": row["status"],
                            "why": failure(row)} for row in lost],
                "finished": [{"card": row["id"], "title": row["title"]} for row in finished],
                "open_to_you_now": choices, "retries_so_far": retried}
    prompt = (CARDS_FAILED_CONTRACT + f"\n# Node\n{node['id']}: {node['question']}\n"
              + prompt_guard.untrusted("failed-cards", _catalog_text(material)) + navigation(run["id"]))
    action, _raw = _command(
        con, run, cfg, worker, node, prompt, key=runs.cards_failed_key(lost), name="misaka_research_cards_failed",
        description="Last Order: decide what this node does about its cards that failed for good",
        model=commands.CardsFailed, validate=validate, session_dir=_lo_session(run, node),
    )
    return action["payload"]


def decide(con, run, cfg, worker, node, *, divergence, proposals, branches):
    """Last Order's decisions after the divergence review: every proposal and every branch-disposed
    red-team issue is taken up by an option or declined with its reason."""
    from misaka.core.research import views
    accepted = runs.action(con, run["id"], node["id"], runs.DECIDE_KEY)
    if accepted:
        return accepted["payload"]
    expected = {item["id"] for item in (*proposals, *branches)}
    known = {row["id"] for row in (*runs.nodes(con, run["id"]), *runs.pending_options(con, run["id"]))}
    reopenable = views.reopenable(con, run)

    def validate(value):
        cited = [source for decision in value["decisions"] for option in decision["options"] for source in option["sources"]]
        cited += [item["issue_id"] for item in value["declined"]]
        if len(cited) != len(set(cited)) or set(cited) != expected:
            raise ValueError("Every divergence proposal and every branch issue appears exactly once: in one option's "
                             "sources or in declined; use only the ids listed.")
        for index, decision in enumerate(value["decisions"], 1):
            if sum(option["own"] for option in decision["options"]) > 1:
                raise ValueError(f"Decision {index} marks more than one option as this node's own line.")
            if any(option["own"] and option["sources"] for option in decision["options"]):
                raise ValueError(f"Decision {index}: this node's own line is its concluded conclusion, made of no "
                                 "proposal.")
            for option in decision["options"]:
                if option["own"] and option["answers"]:
                    raise ValueError(f"Decision {index}: this node's own line is its concluded conclusion; it brings "
                                     "back no path.")
                stray = [record for record in option["answers"] if record not in reopenable]
                if stray:
                    raise ValueError(f"Decision {index}, option \"{option['label']}\": answers names "
                                     f"{', '.join(stray)}, which the paths list does not show declined, covered or "
                                     "left unopened.")
        for item in value["declined"]:
            if item["disposition"] == "covered" and (item["covered_by"] not in known or item["covered_by"] == node["id"]):
                raise ValueError(f"covered needs the id of another node of this run, or of an option waiting to be "
                                 f"opened, as covered_by ({item['issue_id']}).")
            if item["disposition"] != "covered" and item["covered_by"]:
                raise ValueError(f"Only covered takes covered_by (issue {item['issue_id']}).")
        return value

    material = {"divergence_review": divergence,
                "proposals": [dict(row) for row in proposals],
                "branch_issues": [dict(row) for row in branches],
                "pending_options_of_earlier_decisions": [
                    {"option": row["id"], "label": row["label"], "premise": row["premise"],
                     "decision": row["decision_question"]}
                    for row in runs.pending_options(con, run["id"]) if row["decided_at"] == node["id"]],
                "on_your_line": on_your_line(con, run, node)}
    prompt = (DECIDE_CONTRACT + f"\n# Node\n{node['id']}: {node['question']}\nThe paths list: `{views.paths_path(run)}`\nThe graph: `{views.graph_path(run)}`\n"
              + f"Depth {node['depth']} of {runs.current_limits(con, run)['max_depth']}.\n"
              + prompt_guard.untrusted("divergence", _catalog_text(material)) + navigation(run["id"]))
    action, _raw = _command(
        con, run, cfg, worker, node, prompt, key=runs.DECIDE_KEY, name="misaka_research_decide",
        description="Last Order: decide which alternatives this node forks into, and why the others are not opened",
        model=commands.Decide, validate=validate, session_dir=_lo_session(run, node),
    )
    return action["payload"]


def _decided_where(con, run, decision, lineage):
    """Where the turn that made ``decision`` is, for a node whose conversation continues ``lineage``."""
    if decision["origin"] == "reconcile":
        entry = runs.reconcile(con, run["id"], decision["round"])
        return {"in": f"the root Last Order's conversation (reconciliation {decision['round']})",
                "session_file": entry["session_file"] if entry else run["root_session"],
                "tool_call_id": entry["tool_call_id"] if entry else None}
    key = runs.DECIDE_KEY if decision["origin"] == "decide" else runs.plan_key(decision["round"])
    recorded = runs.action(con, run["id"], decision["node_id"], key)
    inside = decision["node_id"] in lineage
    return {"in": "this conversation, above" if inside else f"node {decision['node_id']}'s conversation",
            "session_file": recorded["session_file"] if recorded else None,
            "tool_call_id": recorded["tool_call_id"] if recorded else None}


def on_your_line(con, run, node):
    """What the nodes ``node`` descends from already decided -- every option of their decisions and every
    possibility their decisions disposed -- each with where it went and where the turn that decided it is:
    in the conversation the node continues (its lineage), or in another session (a join's parents' lines)."""
    from misaka.core.research import views

    above = views.ancestors(con, node["id"])
    if not above:
        return []
    lineage = set(views.lineage(con, node))
    found, decided = [], {}
    for decision in runs.decisions(con, run["id"]):
        if decision["node_id"] not in above:
            continue
        where = _decided_where(con, run, decision, lineage)
        for option in runs.options(con, run["id"], decision_id=decision["id"]):
            found.append({"id": option["id"], "what": "option", "label": option["label"], "premise": option["premise"],
                          "decided_at": decision["node_id"], "decision": decision["question"],
                          "went": ("this node" if option["node_id"] == node["id"]
                                   else "its deciding node's own line" if option["node_id"] == decision["node_id"]
                                   else views._outcome(con, option, decision["node_id"])),
                          "answers": option["answers"],
                          "deliberation": where})
        if decision["origin"] == "decide":
            decided[decision["node_id"]] = where
    for issue in views.possibility_issues(con, run):
        if issue["branch_id"] not in above or issue["disposition"] not in ("covered", "decline"):
            continue
        where = decided.get(issue["branch_id"]) or _decided_where(
            con, run, {"origin": "decide", "node_id": issue["branch_id"], "round": 1}, lineage)
        found.append({"id": issue["id"], "what": "proposal", "question": issue["question"],
                      "premise": views.proposal_premise(issue), "decided_at": issue["branch_id"],
                      "went": views._proposal_outcome(con, run, issue), "deliberation": where})
    return found


def present_dissolution(con, run, cfg, worker, *, material):
    """One turn of the root Last Order putting a dissolution of the research question to the user."""
    root = runs.root(con, run["id"])
    session_dir = _lo_session(run, root)
    _obj, _text, err = _call(
        worker, cfg, DISSOLUTION_CONTRACT + prompt_guard.untrusted("dissolutions", _catalog_text(material)),
        cwd=run["workspace"], session_dir=session_dir, raw=True,
        continue_session=bool(findMostRecentSession(session_dir)), task_id=run["id"], con=con,
        session_file=run["root_session"])
    if err:
        raise RuntimeError(f"Putting the dissolution to the user failed: {err}")


def reconcile(con, run, cfg, worker, *, round, material, validate):
    """The root Last Order's reconciliation of a level, in the run's own conversation. Recorded
    as a run-level command; returns the recorded row."""
    recorded = runs.reconcile(con, run["id"], round)
    if recorded is not None:
        return recorded
    root = runs.root(con, run["id"])
    session_dir = _lo_session(run, root)
    command = commands.reconcile_tool(con, run, round=round, validate=validate, session_file=run["root_session"])
    prompt = (RECONCILE_CONTRACT + (RECONCILE_WAITS if plan_waits_for_user(cfg, run) else "")
              + user_instructions(run)
              + prompt_guard.untrusted("reconciliation", _catalog_text(material)) + navigation(run["id"]))
    recorded, _text = commanded(
        worker, cfg, prompt, command=command, name="misaka_research_reconcile",
        recorded=lambda: runs.reconcile(con, run["id"], round),
        cwd=run["workspace"], session_dir=session_dir, raw=True,
        continue_session=bool(findMostRecentSession(session_dir)), task_id=run["id"], con=con,
        session_file=run["root_session"])
    return recorded


def task_body(task, *, run_id=None, node=None, siblings=(), previous=(), lo_session=None):
    """The card's own contract: only what this task needs to say. The researcher's working rules
    are RESEARCH_SISTER_DISCIPLINE (her system prompt, once per session) and the tools' own
    guidelines, so they are not repeated here. ``node`` is the branch the card belongs to (its
    question is the larger one this card serves); ``siblings`` are the plan's other task specs on
    that node, so she knows whom she can ask; ``previous`` are the node's cards from earlier rounds
    (rows with title and output_dir), whose outputs this round builds on. ``lo_session`` is the
    node Last Order's session file: a run's space holds a Last Order per node, so the card is
    told which one is hers by session id."""
    approach = """## Execution approach
Briefly outline your approach in ordinary prose: sources and methods, important risks, and when to stop.
Then use your tools and carry out the task in this same session; do not stop after the outline. Revise the approach
when evidence warrants it and explain why. No separate planning submission, file, or approval is required.
"""
    if run_id is not None:
        approach += navigation(run_id)
    if "instructions" in task:
        return task["instructions"].rstrip() + "\n\n" + approach
    methods = ""
    for key, label in (("method", "method"), ("source_strategy", "source strategy"), ("falsifiers", "falsifiers")):
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            methods += f"\n## {label}\n{value.strip()}\n"
    larger = ""
    question = node["question"] if node is not None else None
    if question:
        larger = (f"\n## the larger question\n{question}\n"
                  "This card is one piece of it; the rationale above says which piece.\n")
    lo_id = (readSessionHeader(str(home.from_stored(lo_session))) or {}).get("id") if lo_session else None
    if lo_id:
        larger += (f"\nYour node's Last Order is session `{lo_id}`: send `SendMessage` to that id, or to "
                   "`last-order`, which inside the run means her.\n")
    others = [spec for spec in siblings if spec.get("local_id") != task.get("local_id")]
    company = ""
    if previous:
        company += ("\n## earlier cards on this node\nAn earlier round already brought these back; read them "
                    "before doing anything they already did:\n"
                    + "\n".join(f"- [{row['id']}] {row['title']} → `{row['output_dir']}`" for row in previous) + "\n")
    if others:
        waits_for = set(task.get("dependencies") or [])

        def timing(spec):
            if spec.get("local_id") in waits_for:
                return "yours starts after it is done"
            if task.get("local_id") in (spec.get("dependencies") or []):
                return "starts after yours is done"
            return "in parallel with yours"

        # Cards get their ids one by one as they are created, so a sibling made after this one has none
        # yet: name her Sister, which inside the run reaches her card on this node (2026-10-01).
        company += ("\n## sibling cards\nOther cards on the same node. To reach one's Sister, `SendMessage` "
                    "her number: inside the run it reaches her card on this node, and if she holds several "
                    "the refusal lists their card ids. A card that has not started gets the message when it "
                    "does, and a finished one is woken by it.\n"
                   + "\n".join(f"- {spec.get('title')} → Sister {spec.get('assignee')} ({timing(spec)})"
                               for spec in others) + "\n")
    return f"""## research question
{task['question']}

## rationale
{task['rationale']}
{methods}{larger}{company}
{approach}
## deliverable
{format_deliverable(task['deliverable'])}
Write it under the deliverable location the card names; successful writes and fetched source files are recorded
automatically.

## boundaries
Where material cannot be obtained you may still conclude -- with the gap named, and what it would settle.

## acceptance criteria
- At least one Markdown deliverable exists.
- Findings and concrete uncertainties are declared with `misaka_card_note` (`findings`: each `text`, `claim_type`
  fact|inference|interpretation|normative, `source_file` or `doc_id` + `page`, optional `quote`; `uncertain`).
"""

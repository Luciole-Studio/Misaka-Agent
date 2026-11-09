"""The final report of a finished run: survey, draft, independent review, adjudication.

The run's own products -- every conclusion, critique, divergence review, plan and card
deliverable -- are indexed in the workspace corpus first, so each stage reads them through
``doc_outline`` / ``doc_read`` / ``doc_find`` by document id, the way Sisters read a downloaded
book. The prompt itself carries a catalog of ids, never the texts: twenty nodes' texts came to
2.3 MB, which compacted the turn before its instructions were read and left a two-line reply
saved as the draft (2026-09-28). What a stage delivers is what its command records; a reply
without the command is not a deliverable. Python orders the stages and checks artifact identity;
it never judges or rewrites research prose.
"""
from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import time
from pathlib import Path

from misaka.core.documents import index as corpus
from misaka.core.documents import workspace as ws_index
from misaka.core.platform import prompt_guard
from misaka.core.platform import tasks as task_store
from misaka.core.research import bundle, commands, planner, runs, views
from misaka.core.session_manager import findMostRecentSession
from misaka.utils import atomic

_LOG = logging.getLogger(__name__)

STAGE_OUTPUT = """
Read the run through the catalog below: every conclusion, critique, divergence review, plan and card deliverable is an
indexed document. The catalog gives each node's conclusion, critiques and divergence review by id, and its
`sources_manifest` lists the rest of its folder; `doc_outline(doc_id)` shows a document's sections, `doc_read(doc_id,
node=...)` reads one, `doc_find(query)` finds where the run says something; a node's review issues, with what Last
Order did with each, are `misaka_research_view(view="issues", node=...)`. Deliver the text with `{tool}`: the Markdown you pass is the file others read,
exactly as written, beginning with the text itself. Only that call is recorded; a reply without it delivers nothing.
"""

SURVEY_CONTRACT = """# Finished-run survey — the section on node `{node}`
Write this node's section of the survey: its question and how it was reached (its parents, the decision and option,
the premise); its methods; its conclusion (the latest version, and what each revision changed); the key evidence; the
red team's objections and what Last Order did with each, the corrections recorded against it; the divergence review's
proposals and the decisions taken; and what its children and any join that carried it forward found. Include competing
interpretations and uncertainty. Statuses and dispositions describe execution, not truth. Preserve the node's
definitions, premises and conditions; make its differences from other nodes visible without resolving them.
Do not vote, rank nodes or adjudicate the original question here. Where reading this node beside the others shows
a connection the graph does not record -- paths converging or diverging, a node resonating with, appropriating or
displacing another -- record it with `misaka_research_relate`; relations join the graph the report reads.
Begin the section with a heading naming the node.
"""

DRAFT_CONTRACT = """# Report draft — not delivered; awaiting independent red-team review
Draft the answer to the original research question. The planning-stage prohibition on answering
is lifted. This is a working draft: an independent red team reviews it before you adjudicate its objections. The
survey is your map of every line of inquiry, one section each: read it whole first (`doc_outline` lists its
sections), then the conclusions and critiques it points to, and the sources behind them, where the answer turns on them.
The ledger records declarations, not machine-certified truths.

## What this stage is
The research has formed its conclusions; this stage brings them to articulation. Draw the article's theses,
arguments and connections out of the run's materials -- the lines' conclusions, what each revised, rebutted or
conceded under critique, the decisions, joins and relations between them, the errata -- and argue every connection
you draw from them, including those the graph does not record. Judge as a scholar judges a body of evidence: whether
claims are supported, whether findings bear on one another, whether positions can be reconciled. Do not overturn or
reweigh a line's conclusion on your own authority; that takes evidence, disclosed as retrieved here. Keep what the
research established distinct in the prose from what the article infers in bringing it together.

## The materials and what you already know
The materials may hold knowledge you lack and positions far from received opinion. Divergence from consensus is not
an error: do not normalise such positions or soften them toward the mainstream. Nor is a claim true for being in the
materials. Use what you know as a check on facts -- dates, figures, names, attributions, whether a work exists -- not
as a verdict on interpretations. Where the materials and what you know conflict on a fact, verify it against sources;
if it cannot be settled, state the conflict rather than choose silently. Background knowledge that informs the
exposition is marked as such and cites no work the run did not read.

## Integration
Integrate; do not catalogue. Build the argument from how the findings bear on one another -- how a line answers the
conclusion it departed from, how a join confronted its parents, where lines converge, diverge or displace one
another, which evidence and premises they share -- so that each finding does work in the argument instead of
standing in a list of results. Where premises are irreconcilable, keep the positions distinct and state precisely
where and why they part: do not average them, and do not present incompatible claims as jointly true.

## The reader and the form
Write for a reader of the humanities and social sciences: a research article in the language of the question, argued
rather than narrated -- organized by the question and its argument, not by how the research was run. A title, an
abstract and keywords; an introduction that states the question, the concepts it turns on, and the research design and
materials in scholarly terms; chapters that argue; a conclusion. Prose in paragraphs; lists and tables only where a
comparison needs them.
The body carries no run vocabulary or identifiers -- nodes, cards, rounds, the red team, dispositions, their ids, file
paths. Name a line of inquiry by what it argued and a source by its author and title.
Every line's final conclusion has its place in the argument -- as a finding, an objection, a qualification or a rival
reading. What the run produced and read is the article's material: none of it is dropped for being inconvenient.

## The apparatus, where traceability lives
- Notes cite author, title, year and page or section; a primary source names its edition or archive; what the research
  read only at second hand is cited as quoted in its source. A local file or `doc:` locator may follow in the note.
- The bibliography lists every work the article cites: primary sources, scholarship, web and database material.
- Appendix I, the lines of inquiry: every node of the catalog, one row each -- its question in plain words, its
  conclusion in a sentence, where the body takes it up, and its id. A line that reached no conclusion says so.
- Appendix II, the record: the costs the answer accepts (the conceded issues); the corrections recorded against
  conclusions (errata), each applied in the body; the paths not taken, each with its reason (options not opened,
  proposals declined, nodes skipped); what stays open (parked and unanswered issues); where independent lines converge
  and where they diverge.
- Appendix III, the materials: one line naming misaka's complete list of everything the run opened and downloaded,
  given under the catalog below. Do not copy it.

## Judgement
Do not vote or let a majority erase a minority view. Distinguish empirical claims, causal explanations, interpretations,
and normative premises. Where lines disagree, examine differences in definitions, scope, period and method before
judging whether the disagreement is real; resolve it only where the evidence decides it, and otherwise keep it
standing. Give a direct answer grounded in the research -- conditional on its premises where lines rest on different
ones -- with the strongest competing accounts, evidentiary limits, unresolved questions, and what would change the
answer. Depth limits describe execution, not whether an objection is correct. Distinguish independent
support from repeated reliance on the same sources or premises; retain material qualifications and counterevidence when
combining conclusions. Read relevant available Skills when useful for these checks.
Where the research showed a question rests on a confusion or an ideological presupposition, give the dissolution -- a
legitimate result, not a failure to answer. A dissolution of the research question itself is the answer only if the
user agreed to it (`dissolution_consent` in the record); if they did not, answer the question as asked and give the
dissolution as one reading, with its grounds.

## Retrieval
You may retrieve material needed to verify or compare the submitted evidence; preserve it in the workspace and identify
its source and path so the red team can read it. Evidence retrieved here that changes a line's conclusion is disclosed
as such, so the red team can weigh it. Do not turn this draft into an undisclosed new research assignment;
identify substantial unfilled evidence needs as limits. Do not force interpretation or normative reasoning into a
verbatim quotation. Do not hide corrections or unsupported leaps behind reference markers.
""" + planner.SOURCES_FOOTER

FINAL_CONTRACT = """# Final report — adjudicate the independent review of the draft
Adjudicate the independent red team's review of the saved report draft. The planning-stage prohibition on answering is
lifted. Read the full draft, the review, and the sources relevant to each objection. Criticism is not a verdict: accept,
reject, or retain disagreement on each substantive objection, explaining why. The measure is fidelity to the research
and to the evidence, not a prior view of the question. A missing source, interpretive conflict,
or normative disagreement is not automatically a factual error. Do not rank views by vote or authority. After accepting
a correction, recheck affected passages and the conclusion; preserve independently supported content.

Write the complete final report in the draft's form -- a research article for a reader of the humanities and social
sciences, its body free of run vocabulary and identifiers, its notes, bibliography and Appendices I-III as the draft
has them -- revised where the review holds, completed where it found a line of inquiry missing. Add Appendix IV, the
review: which objections changed the answer, which you rejected and why, which remain unsettled. You may consult
sources to adjudicate the review, but do not conceal fresh evidence or present a new claim as having been reviewed in
the draft. This is a terminal review; it opens no new node of the graph.
""" + planner.SOURCES_FOOTER

# Stage contracts change the deliverable, not the basic research/Skill capabilities.
SURVEY_TOOLS = FINAL_TOOLS = planner.RESEARCH_TOOLS
REPORT_TOOL = "misaka_research_report"


def doc_id(run, path):
    """The corpus id of one of the run's texts, indexing it first if need be; None for a file the
    corpus does not take. Ingest is by content hash, so a file already indexed keeps its id."""
    if os.path.splitext(path)[1].lower() not in corpus.SCAN_SUFFIXES or not os.path.isfile(path):
        return None
    try:
        return corpus.ingest(path, workspace=run["workspace"])[0]
    except (OSError, ValueError):
        return None


def index_run(con, run, node=None):
    """Every registered text product of the run, or of one ``node``, in the workspace corpus:
    ``{path: doc_id}``. A file saved by hand under a card's ``sources/`` was offered to the corpus
    when the card finished: it keeps the id it got, and a scan refused then is not read again. A
    card's working files stay out here as they did then, unless the card indexed one itself."""
    rows = runs.artifacts(con, run["id"], branch_id=node["id"] if node is not None else None)

    @functools.cache
    def card(task_id):
        return task_store.get(con, task_id)

    def offered(row):
        if json.loads(row["metadata_json"] or "{}").get("saved_by_hand"):
            return False
        task = card(row["task_id"]) if row["task_id"] else None
        return task is None or ws_index.belongs_in_corpus(task, row["path"])

    def indexed(row):
        if offered(row):
            return doc_id(run, row["path"])
        known = row["sha256"][:12]
        return known if corpus.resolve_doc(known, workspace=run["workspace"]) else None

    return {row["path"]: found for row in rows if (found := indexed(row)) is not None}


def _catalog(con, run, docs, focus=None):
    """Every node with the ids of its documents -- what a stage reads through the doc tools. A row
    gives a node's place, its latest conclusion, critiques and divergence review, and its folder's
    SOURCES.md; the ``focus`` node, the one a survey section is about, also gets how it was reached,
    every version, its plans, deliberation and cards. Every node in full came to 137 KB, sent again
    with each of fifteen survey turns until the draft no longer fit the window (2026-09-29)."""
    doc = lambda path: docs.get(path, path)  # a file the corpus would not take is read by path
    out = []
    for node in runs.nodes(con, run["id"]):
        rows = runs.artifacts(con, run["id"], branch_id=node["id"])
        by_kind = {}
        for row in rows:
            by_kind.setdefault(row["kind"], []).append(doc(row["path"]))
        entry = {"node": node["id"], "kind": views.kind(con, node), "depth": node["depth"], "status": node["status"],
                 "parents": [p["id"] for p in runs.parents(con, node["id"])],
                 "children": [c["id"] for c in runs.children(con, node["id"])],
                 "question": node["question"],
                 "conclusion": (by_kind.get("synthesis") or [None])[-1],
                 "critiques": by_kind.get("critique", []),
                 "divergence_review": by_kind.get("divergence", []),
                 # the node's folder in one page: its products, the sources they cite, hard-linked beside them
                 "sources_manifest": os.path.join(run["workspace"], runs.node_dir(node["id"]), "SOURCES.md"),
                 # Counts only: every issue's text made the catalog 280 KB (2026-09-29, 293 issues); the
                 # issues view lists a node's.
                 "issues": _issue_counts(runs.issues(con, run["id"], node_id=node["id"])),
                 "dissolves": views.dissolution(con, run, node)}
        if node["id"] == focus:
            entry.update({"reached_by": [{"decided_at": o["decided_at"], "decision": o["decision_question"],
                                          "option": o["label"], "premise": o["premise"]}
                                         for o in runs.origin_options(con, node["id"])],
                          "rounds": runs.plan_round(con, run["id"], node["id"]),
                          "plans": by_kind.get("plan", []),
                          "conclusions": by_kind.get("synthesis", []),     # every version, oldest first
                          "deliberation": by_kind.get("deliberation", []),
                          "cards": [{"id": t["id"], "title": t["title"], "kind": t["research_kind"], "status": t["status"],
                                     "deliverables": [doc(a["path"]) for a in rows if a["task_id"] == t["id"]]}
                                    for t in runs.tasks(con, run["id"], node_id=node["id"])]})
        out.append(entry)
    return out


def _issue_counts(issues):
    counts = {}
    for issue in issues:
        counts[issue["disposition"] or "undisposed"] = counts.get(issue["disposition"] or "undisposed", 0) + 1
    return {"total": len(issues), "by_disposition": counts}


def _boundary(con, run):
    """What the run leaves open: issues parked for the final adjudication, and issues nobody answered."""
    return [{"issue": i["id"], "node": i["branch_id"], "disposition": i["disposition"] or "undisposed",
             "question": i["question"], "rationale": i["rationale"]}
            for i in runs.issues(con, run["id"]) if i["disposition"] in {None, "park"}]


def _record(con, run):
    """The rest of the account the report gives: the costs the answer accepts, the corrections,
    the paths not taken with their reasons, the decisions with every option, and the relations."""
    decisions = []
    for decision in runs.decisions(con, run["id"]):
        decisions.append({"node": decision["node_id"], "origin": decision["origin"], "question": decision["question"],
                          "stakes": decision["stakes"],
                          "options": [{"label": o["label"], "premise": o["premise"], "node": o["node_id"],
                                       "not_pursued": o["reason"], "answered_by": o["covered_by"],
                                       "brings_back": o["answers"]}
                                      for o in runs.options(con, run["id"], decision_id=decision["id"])]})
    issues = runs.issues(con, run["id"])
    return {
        "costs": [{"issue": i["id"], "node": i["branch_id"], "question": i["question"], "cost": i["reason"]}
                  for i in issues if i["disposition"] == "concede"],
        "corrections": [{"issue": i["id"], "node": i["branch_id"], "question": i["question"], "correction": i["reason"]}
                        for i in issues if i["disposition"] == "correct"],
        "errata": runs.errata(con, run["id"]),
        "not_taken": [{"issue": i["id"], "node": i["branch_id"], "origin": i["origin"], "question": i["question"],
                       "disposition": i["disposition"], "to": runs.destination(i), "reason": i["reason"],
                       **({"coverage_review": i["coverage_review"]} if i.get("coverage_review") else {})}
                      for i in issues if i["disposition"] in {"decline", "covered"}],
        "skipped_nodes": [{"node": n["id"], "question": n["question"]}
                          for n in runs.nodes(con, run["id"]) if n["status"] == "parked"],
        "decisions": decisions,
        "relations": runs.relations(con, run["id"]),
        "dissolutions": [{"node": n["id"], **views.dissolution(con, run, n)} for n in runs.nodes(con, run["id"])
                         if views.dissolution(con, run, n)],
        "dissolution_consent": (runs.action(con, run["id"], runs.root(con, run["id"])["id"], runs.CONSENT_KEY)
                                or {}).get("payload"),
        "graph": os.path.join(run["workspace"], views.graph_path(run)),
        "paths": os.path.join(run["workspace"], views.paths_path(run)),
    }


def _stage(con, run, cfg, worker, prompt, *, key, model, reply, extra_tools=(), check_active=None):
    """One root Last Order turn that must end in the stage's command; returns what it recorded."""
    root = runs.root(con, run["id"])
    session_dir = os.path.dirname(run["root_session"]) if run["root_session"] else runs.session_dir(run, "root-lo")
    tool = commands.report_tool(con, run, name=REPORT_TOOL, description=f"Deliver this part of the final report: {key}",
                                model=model, key=lambda payload: key, session_file=run["root_session"], reply=reply)
    if check_active:
        check_active()
    action, _text = planner.commanded(
        worker, cfg, prompt, command=tool, name=REPORT_TOOL, what=f"{REPORT_TOOL} for {key}",
        recorded=lambda: runs.action(con, run["id"], root["id"], key),
        cwd=run["workspace"], session_dir=session_dir, raw=True, task_id=run["id"], con=con, session_file=run["root_session"],
        continue_session=bool(findMostRecentSession(session_dir)), extra_tools=tuple(extra_tools))
    if check_active:
        check_active()
    return action["payload"]


def _checkpoint(con, run, kind):
    rows = runs.artifacts(con, run["id"], kind=kind, run_level=True)
    if not rows:
        return None
    row = rows[-1]
    _intact(con, run, row)  # A modified checkpoint is an integrity error, not permission to redraft.
    return row


def _recorded_text(con, run, kind):
    """The text a checkpoint was saved with, rebuilt from the commands it came from, or None."""
    root = runs.root(con, run["id"])
    if kind == "draft":
        action = runs.action(con, run["id"], root["id"], "draft")
        return action["payload"]["markdown"].strip() + "\n" if action else None
    if kind == "survey":
        sections = []
        for node in runs.nodes(con, run["id"]):
            action = runs.action(con, run["id"], root["id"], f"survey:{node['id']}")
            if action is None:
                return None
            sections.append(action["payload"]["markdown"].strip())
        return f"# Survey — run {run['id']}\n\n" + "\n\n".join(sections) + "\n"
    return None


def _intact(con, run, row):
    """Check a checkpoint against its frozen digest. One changed afterwards -- outside the phase
    that writes it -- is put back from the commands it was saved from, the changed file kept beside
    it, and the run goes on: a draft edited during its final review failed the run, and every
    resume failed the same way (B126). With nothing to rebuild it from, it stays an error."""
    try:
        runs.artifact_text(row)
        return
    except (ValueError, FileNotFoundError):
        # Moved or deleted is rebuilt the same way: a missing draft failed the final review and
        # every resume after it, until the file was put back by hand (0.18.10 sweep).
        text = _recorded_text(con, run, row["kind"])
        if text is None or hashlib.sha256(text.encode("utf-8")).hexdigest() != row["sha256"]:
            raise
    path = Path(row["path"])
    if path.exists():
        kept = path.with_name(f"{path.stem}.edited-{int(time.time())}{path.suffix}")
        os.replace(path, kept)
        _LOG.warning("Research %s: %s was changed after it was saved; restored it, the changed file is %s",
                     run["id"], path, kept)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        _LOG.warning("Research %s: %s was missing; restored it from the run's record", run["id"], path)
    atomic.write_text(str(path), text)


def _save(con, run, kind, text, **metadata):
    with runs.owned_txn(con, run):
        runs.write_text(con, run["id"], kind, kind.title(), runs.run_path(run, f"{kind}.md"), text,
                        metadata=metadata or None)
    return _checkpoint(con, run, kind)


def _common(con, run, docs, focus=None):
    return (f"\n# Original question\n{run['question']}\n\n# Catalog: the nodes of the graph and their documents "
            "(root first, then by depth)\n"
            + prompt_guard.untrusted("research-catalog", json.dumps(_catalog(con, run, docs, focus), ensure_ascii=False,
                                                                    indent=2))
            + f"\nThe graph: `{os.path.join(run['workspace'], views.graph_path(run))}`; every possibility raised and where "
            f"it went: `{os.path.join(run['workspace'], views.paths_path(run))}`; the complete list of materials the run "
            f"opened and downloaded, which misaka writes when the report is delivered (Appendix III): "
            f"`{os.path.join(run['workspace'], runs.run_path(run, bundle.MANIFEST))}`.\n")


def prepare(con, run, cfg, worker, *, check_active=None):
    """The survey, one node per turn, then the draft; each recorded as delivered, so a resume goes
    on from the section it lacks and never redrafts a saved checkpoint."""
    docs = index_run(con, run)
    root = runs.root(con, run["id"])
    if _checkpoint(con, run, "survey") is None:
        relate = commands.relate_tool(con, run, session_file=run["root_session"],
                                      validate=lambda value: views.resolve_relations(con, run, value))
        sections = []
        for node in runs.nodes(con, run["id"]):
            key = f"survey:{node['id']}"
            recorded = runs.action(con, run["id"], root["id"], key)
            payload = recorded["payload"] if recorded else _stage(
                con, run, cfg, worker,
                SURVEY_CONTRACT.format(node=node["id"]) + STAGE_OUTPUT.format(tool=REPORT_TOOL)
                + _common(con, run, docs, focus=node["id"])
                + f"\n# Corrections recorded against or by `{node['id']}`, and the relations it is part of\n"
                + prompt_guard.untrusted("research-record", json.dumps(
                    {"errata": [e for e in runs.errata(con, run["id"]) if node["id"] in (e["node"], e["raised_by"])],
                     "relations": [r for r in runs.relations(con, run["id"]) if node["id"] in r["nodes"]]},
                    ensure_ascii=False, indent=2)) + planner.navigation(run["id"]),
                key=key, model=commands.ReportText, extra_tools=(relate,), check_active=check_active,
                reply=lambda _payload, node=node: f"Recorded the survey section on {node['id']}.")
            sections.append(payload["markdown"].strip())
        _save(con, run, "survey", f"# Survey — run {run['id']}\n\n" + "\n\n".join(sections) + "\n")
    survey = _checkpoint(con, run, "survey")
    if _checkpoint(con, run, "draft") is None:
        payload = _stage(
            con, run, cfg, worker,
            DRAFT_CONTRACT + STAGE_OUTPUT.format(tool=REPORT_TOOL) + _common(con, run, docs)
            + "\n# Costs, corrections, paths not taken, decisions and relations\n"
            + prompt_guard.untrusted("research-record", json.dumps(_record(con, run), ensure_ascii=False, indent=2))
            + "\n# Honest boundary: issues parked for this adjudication or never answered\n"
            + prompt_guard.untrusted("research-boundary", json.dumps(_boundary(con, run), ensure_ascii=False, indent=2))
            + f"\n# The survey\n`{survey['path']}`, document `{doc_id(run, survey['path'])}`\n"
            + planner.navigation(run["id"]),
            key="draft", model=commands.ReportText, check_active=check_active,
            reply=lambda _payload: "Recorded the draft; the independent red team reviews it next.")
        _save(con, run, "draft", payload["markdown"].strip() + "\n")
    return _checkpoint(con, run, "draft")


def review_body(con, run, draft):
    survey = _checkpoint(con, run, "survey")
    return (f"""## goal
Independently red-team the saved final-report draft at `{draft['path']}` for this research question:
{run['question']}
Review target: artifact `{draft['id']}`, sha256 `{draft['sha256']}`.
Read the whole draft, the survey (`{survey['path']}`), node critiques and full sources
(including supplements cited in the draft): every conclusion, critique and card deliverable of the run is an indexed
document -- `doc_outline`, `doc_read` and `doc_find` read them by the ids in the catalog below, and each node's
`sources_manifest` lists the rest of its folder.
Inspect quotations and attribution in context, reasoning, causal and conceptual claims, methods, omissions,
competing interpretations and normative premises. Check the facts the answer rests on against sources of your own.
Do not infer that a claim is false merely because a number or wording is absent from a short excerpt. Your own
objections also require reasons and may be wrong. Where relevant, trace cross-branch concept drift or shared
unsupported premises to the affected draft passages. Compare the draft against node records for lost qualifications,
counterevidence or conflated explanations; read relevant available Skills as needed.
Check fidelity: every thesis, argument and connection is argued from the run's materials or from evidence retrieved
and disclosed here. Flag conclusions overturned or reweighed without such evidence; positions normalised toward
received opinion; doubtful facts accepted for being in the materials; background knowledge presented as the run's
finding or cited to a work the run did not read; lines catalogued where their findings bear on one another;
incompatible positions merged or presented as jointly true.
Check completeness against the catalog below: Appendix I lists every node, and each line's final conclusion is argued
in the body, not only listed; a line missing from either, or a conclusion the body misstates, is a material issue.
Check the form: the body is written for a reader -- run vocabulary, ids and file paths belong in the notes and appendices.
You may search/read supplementary sources; preserve and locate anything you use. Do not edit the draft, and do
not send your critique to Last Order: completing this card delivers it to the adjudication that weighs it.

## deliverable
`critique.md`
Write it under your card's deliverable directory. For each objection identify the draft passage,
source/context, reasoning, and a suggested correction or an explicit unresolved disagreement.
Call `misaka_card_note` with your complete `issues` list (kind, question, rationale, priority, material).
Use issues=[] explicitly if none. The root Last Order will adjudicate this review; no new research branches
are created from it and material=true is not an automatic verdict.

## catalog
""" + prompt_guard.untrusted("research-catalog", json.dumps(_catalog(con, run, index_run(con, run)),
                                                            ensure_ascii=False, indent=2)))


def review_receipt(con, run, draft, task_id):
    """Require the current generation's submitted review of this exact saved draft."""
    rows = [r for r in runs.tasks(con, run["id"], kind="final_review") if r["id"] == task_id]
    if not rows or rows[0]["status"] != "done":
        raise ValueError("Final red-team review has not completed.")
    task = rows[0]
    target = json.loads(task_store.latest_payload(con, task_id, "research_review_target") or "{}")
    if target != {"artifact": draft["id"], "sha256": draft["sha256"]}:
        raise ValueError("Final review target does not match the saved draft.")
    _intact(con, run, draft)
    payload = json.loads(task_store.latest_payload(con, task_id, "submitted", generation=task["generation"]) or "{}")
    if not isinstance(payload, dict) or not isinstance(payload.get("issues"), list):
        raise TypeError("Final red-team review requires a submitted issues array (issues=[] if none).")
    issues = [commands.Issue.model_validate(i).model_dump() for i in payload["issues"]]
    artifacts = runs.artifacts(con, run["id"], kind="final_critique", task_id=task_id)
    critique = [a for a in artifacts if Path(a["path"]) == Path(task["output_dir"], "critique.md")]
    if not critique:
        raise ValueError("Final red-team review has no submitted critique.md.")
    text = runs.artifact_text(critique[-1])
    if not text.strip():
        raise ValueError("Final red-team critique.md is empty.")
    return {"task_id": task_id, "generation": task["generation"], "target": target,
            "issues": issues, "critique": {"path": critique[-1]["path"], "content": text},
            "artifacts": [{"path": a["path"], "sha256": a["sha256"]} for a in artifacts]}


def finalize(con, run, cfg, worker, *, review_task_id, check_active=None):
    """Adjudicate an actual red-team receipt; preserve the model's Markdown exactly."""
    if check_active:
        check_active()
    draft, survey = _checkpoint(con, run, "draft"), _checkpoint(con, run, "survey")
    if draft is None or survey is None:
        raise ValueError("Report draft and survey are required before final adjudication.")
    review = review_receipt(con, run, draft, review_task_id)
    metadata = {"review_task_id": review_task_id, "review_generation": review["generation"], **review["target"]}
    final = _checkpoint(con, run, "final")
    if final is not None and json.loads(final["metadata_json"]) != metadata:
        raise ValueError("Saved final report belongs to a different review.")
    if final is None:
        payload = _stage(
            con, run, cfg, worker,
            FINAL_CONTRACT + STAGE_OUTPUT.format(tool=REPORT_TOOL) + _common(con, run, index_run(con, run))
            + f"\n# The survey: `{survey['path']}`\n"
            + "\n# Saved draft\n" + prompt_guard.untrusted("report-draft", runs.artifact_text(draft))
            + "\n# Independent red-team receipt\n"
            + prompt_guard.untrusted("final-review", json.dumps(review, ensure_ascii=False, indent=2))
            + planner.navigation(run["id"]),
            key="final", model=commands.ReportText, check_active=check_active,
            reply=lambda _payload: "Recorded the final report.")
        final = _save(con, run, "final", payload["markdown"].strip() + "\n", **metadata)
    return {"artifact": final["id"], "path": final["path"], "content": runs.artifact_text(final),
            "survey_path": survey["path"], "draft_path": draft["path"], "review_task_id": review_task_id}

"""The end of a run: a survey that introduces every node's result without judging (survey.md), then the
adjudication that answers from all of them, keeping disagreement visible (final.md).

The adjudication is the one document that leaves the run, so it is the one document whose citations
are checked mechanically: the ledger's sha-locked quotations go in numbered, and what comes back is
audited by ``research/citation.py`` before it is written. A report that fails gets one rewrite with
the defect list; a report that fails twice is still delivered, with the defects listed at its foot.
Delivery is never blocked and never silent."""
from __future__ import annotations

import json
import os
import re

from misaka.core.platform import prompt_guard
from misaka.core.research import citation, ledger, runs
from misaka.core.session_manager import find_most_recent_session

FINAL_CONTRACT = """You are Last Order at the final adjudication stage of a research run. The planning-stage rule against answering is now lifted.
Every node's conclusion is quoted below in full (the root, then each node that re-researched an undermined point). Read the
red-team critiques at their paths, and whichever source-task artifacts the conclusions cite, then answer the original question.

Do not vote, and do not let a majority erase a minority view. Work out whether apparent conflicts come from different definitions,
scopes, periods, methods, evidence, or value premises. Resolve only what the material supports. Keep competing
conclusions side by side when the evidence cannot decide between them. Separate empirical facts, causal interpretations,
broader interpretations, and normative choices.

The reader must be able to identify:
- a direct answer to the original question;
- supported common findings and their evidentiary limits;
- the strongest competing conclusions and their premises;
- factual, causal, methodological, interpretive, and normative disagreements;
- claims overturned or downgraded by critical review;
- the honest boundary: issues probed but left inconclusive, issues parked by the depth limit, material that was unavailable;
- the evidence that would change the conclusion.

Cite the evidence. The run's ledger is listed below as numbered, verbatim quotations: every factual
claim -- and every figure without exception -- carries the [N] of the entry it rests on, written as
[1], or as [2][5] when several carry it. Citations are checked mechanically against those
quotations, so a number that is not in the source you cite comes back to you to fix. Do not cite an
[N] the list does not have, and do not carry a node's internal [task_id/path] reference into this
document: a path is not a citation here. Your own analysis, where no quotation supports it, stays
unmarked rather than borrowing a number. Do not write a source list at the end -- the run appends
the numbered sources itself, so a list of your own would only be a second, staler copy.

Write free-form Markdown only. Do not output JSON or describe these instructions.
"""


SURVEY_CONTRACT = """You are Last Order writing the survey of a finished research run: one section per node, in tree order
(the root, then each node that re-researched an undermined point, saying which issue opened it). Every node's conclusion is
quoted below in full; read each node's red-team critique at its path.

For each node, report -- without adjudicating: the question it researched; how it went about it; what it concluded, in its
own terms and key evidence; what the red team objected to; what each issue's fork found (supports / inconclusive /
undermines, with the reason); and which issues opened child nodes or were parked. Do not rank the nodes, do not vote, do not
answer the original question here: this document shows the reader the whole tree so the adjudication can be checked against it.

Write free-form Markdown only. Do not output JSON or describe these instructions.
"""


# -- what the two closing passes may reach -------------------------------------------------------

# The survey restates each node's conclusion and adjudicates nothing, so anything it went and found
# would be a second adjudication written in the one document nothing checks.
SURVEY_TOOLS = ("read",)
# The adjudication is the owner's product call, and this tuple is where it is made.
#
# Against searching: FINAL_CONTRACT above makes every factual claim -- and every figure without
# exception -- carry the [N] of a ledger entry, and the ledger holds only what the cards gathered.
# Nothing found at this desk has an entry to cite, so the gate below reports the new figure as
# ungrounded and the run's single rewrite is spent deleting it instead of repairing a real defect.
# For searching: a last pass that can look something up sometimes catches a factual slip that every
# earlier stage missed -- it just cannot put what it found into the report.
# Default: no search. To take the other side, add "web_search" to this one tuple and say in
# FINAL_CONTRACT that search may check wording only and is never the basis of a claim.
FINAL_TOOLS = ("read",)


def _nodes(con, run):
    out = []
    for node in runs.nodes(con, run["id"]):
        synthesis = runs.artifacts(con, run["id"], kind="synthesis",
                                   **({"branch_id": node["id"]} if node["parent_id"] else {"root_only": True}))
        critique = runs.artifacts(con, run["id"], kind="critique",
                                  **({"branch_id": node["id"]} if node["parent_id"] else {"root_only": True}))
        out.append({"node": node["id"], "parent": node["parent_id"], "depth": node["depth"], "status": node["status"],
                    "question": node["trigger_text"],
                    "conclusion_path": [a["path"] for a in synthesis],
                    "critique": [a["path"] for a in critique],
                    "issues": [{"issue": i["id"], "verdict": i["status"], "question": i["question"],
                                "reason": i["reason"], "child": i["child_branch_id"]}
                               for i in runs.issues(con, run["id"], node_id=node["id"])],
                    # the conclusion itself, so every node is in front of her rather than behind a path
                    "conclusion": "\n\n".join(runs.read_artifact(con, a["id"]) or "" for a in synthesis)})
    return out


def _boundary(con, run):
    return [{"issue": i["id"], "node": i["branch_id"], "status": i["status"], "question": i["question"],
             "rationale": i["rationale"]}
            for i in runs.issues(con, run["id"]) if i["status"] in {"inconclusive", "parked", "open"}]


def _write(con, run, cfg, worker, contract, *, tools, extra=""):
    """One Last Order call in the root session over the whole tree; returns the Markdown it wrote.

    ``tools`` has no default on purpose: the two contracts that run through here differ in what
    they let her introduce, so each call site names its own surface (SURVEY_TOOLS, FINAL_TOOLS).
    """
    root = runs.run_dir(run)
    # Both blocks below are model-written text that started life on a fetched page (a node's
    # synthesis, a red-team card's issue questions), so they get the same fence as the evidence
    # ledger in _source_block -- otherwise half this prompt is guarded and half is not.
    prompt = (contract + f"""
# Original question
{run['question']}

# Nodes: conclusions, critiques, and what each issue's fork found (root first, then by depth)
{prompt_guard.untrusted("research-nodes", json.dumps(_nodes(con, run), ensure_ascii=False, indent=2))}
# Honest boundary: issues that stayed open, inconclusive, or parked
{prompt_guard.untrusted("research-boundary", json.dumps(_boundary(con, run), ensure_ascii=False, indent=2))}
""" + extra)
    session_dir = runs.session_dir(run, "root-lo")
    _obj, text, err = worker.run_llm_json(
        os.path.join(cfg["roles_root"], "last_order"), prompt,
        cfg["provider"], cfg["default_model"], cwd=root, tools=list(tools),
        timeout=runs.call_timeout(cfg, max(900, int(cfg.get("judge_timeout", 600)))), soul=False, raw=True,
        usage_db=cfg.get("db"), usage_task_id=run["id"], usage_generation=1,
        usage_token_cap=cfg.get("token_cap"), session_dir=session_dir,
        continue_session=bool(find_most_recent_session(session_dir)), thinking="high",
    )
    if err or not text or len(text.strip()) < 80:
        raise RuntimeError("Final report output is too short.")
    return text.strip()


# -- the citation gate ---------------------------------------------------------------------------

# How long a provenance header may be. A fetched page carries its block at the very top and closes
# it within a few lines; a block that never closes is not a header, and reading a URL out of a
# page's prose would put a link the run never fetched from next to a citation.
_PROVENANCE_LINES = 40
# Priority order, not just membership: a redirect chain ends at final_url, but source_url is the
# address the report will have been written against.
_PROVENANCE_KEYS = ("source_url", "final_url", "url")
# Every finding of the run has to be citable. A conclusion Last Order can read in the node synthesis
# but cannot cite is one the gate then reports as uncited, which spends the single rewrite on a
# defect the source list caused. ledger.findings defaults to 80 rows -- two and a half cards at
# ledger.MAX_FINDINGS -- and a run has no card cap, so the default cut the list silently. This is
# 300 full cards' worth: past any real run, and a run beyond it has a bigger problem than its
# source list.
_LEDGER_LIMIT = ledger.MAX_FINDINGS * 300

_NO_LEDGER = """
# Numbered sources
The evidence ledger recorded no verbatim quotation for this run, so there is nothing to cite: write
without [N] markers, and say plainly where a statement rests on a node's conclusion rather than on
quoted evidence.
"""


def _provenance_url(con, artifact_id):
    """The URL a fetched page records in its own header, or "" for anything written by hand.

    A downloaded page opens with a frontmatter block (``web_fetch._save_page``); a Sister's own
    markdown opens with her own first line, and an artifact written before that block existed opens
    with the page text. So the sweep reads a real block only -- ``---`` on line 1, keys until the
    closing ``---`` -- rather than any ``key: value`` line it can find.

    The looser sweep it replaces read a URL out of arbitrary prose: hand-written notes listing
    "- url: https://..." gave *every* claim from that file that one URL, printed in the delivered
    source list as where a quote came from, and turning a correct citation in the report into a
    url_mismatch that spends the single rewrite on making it wrong. A URL guessed wrong here is
    reported back to the model as *its* mistake, so finding nothing is the only safe way to be wrong.
    """
    try:
        text = runs.read_artifact(con, artifact_id) or ""
    except (OSError, ValueError):
        return ""            # a moved or rewritten artifact costs the citation its URL, not the run
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    found = {}
    for line in lines[1:_PROVENANCE_LINES]:
        if line.strip() == "---":
            break            # past the header is the page's own text, whatever it looks like
        key, _sep, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip().strip("\"'")
        if key in _PROVENANCE_KEYS and key not in found and value.startswith(("http://", "https://")):
            found[key] = value
    return next((found[key] for key in _PROVENANCE_KEYS if key in found), "")


# A claim verified against a corpus document rather than a card artifact records its location as
# ``doc:<doc_id>#p<N>`` (``ledger.ingest_report``'s second findings shape). That is a database key,
# not a place: no file was fetched for it, so it also carries no URL.
_DOC_SOURCE_RE = re.compile(r"doc:([^#\s]+)#p(\d+)")


def _human_source(source_file):
    """A claim's location as a reader can act on it.

    Every other row of the delivered list ends in a real URL, so a bare ``doc:8c1f2a#p42`` beside
    them reads as a link that failed to render -- and a reader who does try it has nowhere to go.
    The document is in the corpus and ``doc_read`` is how one reaches it, so the row says which
    document and which page instead. Anything that is not exactly that shape is passed through
    untouched: this renames one machine key, it does not reformat paths a Sister wrote.

    Called only for doc claims (``artifact_id`` IS NULL) -- the call site gates on that. An
    artifact claim's source_file is the card-chosen path from report.json, and ``doc:...#p42``
    is a legal filename: renaming it would present a card's own text as a corpus-verified page.
    """
    match = _DOC_SOURCE_RE.fullmatch(str(source_file or ""))
    return f"语料文档 {match.group(1)} 第 {match.group(2)} 页" if match else source_file


def _one_line(value):
    """Fold a value onto one line, so it can never open a row of the listing below.

    The listing's rows are typography and nothing else -- ``[N] text``, then indented ``source:``
    and ``quote:`` lines -- and both the finding text and the quote are written by whatever page
    was fetched. A quote that contains those three lines used to render as an extra ``[N]`` row
    carrying an attacker-chosen URL, indistinguishable from a real one inside the same untrusted
    fence (the fence defangs the sentinel; it does not separate rows). ``splitlines`` is what
    decides where a row begins, so it is also what has to be emptied out here: it splits on every
    line break Python knows, U+2028 and U+0085 included.
    """
    return " ".join(str(value or "").splitlines()).strip()


def _sources(con, run):
    """The run's ledger as ``(sources, listing)``: what ``audit_report`` checks, and what the model reads.

    One entry per claim, in the ledger's own order -- stable across the two calls a rewrite costs.
    ``audit_report`` renumbers by first appearance once the report comes back clean, so the numbers
    the reader sees climb even though the model cited them in whatever order it needed.
    """
    sources, lines, seen, urls = [], [], set(), {}
    for finding in ledger.findings(con, run["id"], limit=_LEDGER_LIMIT):
        for claim in ledger.claims(con, finding["id"]):
            key = (claim["source_file"], claim["quote"])
            if key in seen:
                continue        # two findings resting on one sentence are one citable source
            seen.add(key)
            if claim["artifact_id"] not in urls:
                urls[claim["artifact_id"]] = _provenance_url(con, claim["artifact_id"])
            url = urls[claim["artifact_id"]]
            # Only a doc claim earned the corpus rename: see _human_source on why an artifact
            # claim's source_file must go through verbatim, however doc-shaped it looks.
            where = (_human_source(claim["source_file"]) if claim["artifact_id"] is None
                     else claim["source_file"])
            sources.append(citation.Source(evidence=claim["quote"], url=url,
                                           label=f"{finding['id']} {where}"))
            lines.append(f"[{len(sources)}] {_one_line(finding['text'])}\n"
                         f"    source: {_one_line(where)}"
                         + (f" — {url}" if url else "") + "\n"
                         f"    quote: {_one_line(claim['quote'])}")
    return tuple(sources), "\n".join(lines)


def _source_block(listing):
    if not listing:
        return _NO_LEDGER
    return ("\n# Numbered sources: the run's sha-locked evidence. These numbers are the [N] you cite.\n"
            + prompt_guard.untrusted("evidence-ledger", listing))


def _problem_list(problems):
    return "\n".join(f"[{p.kind}] line {p.line}: {p.message}\n    ⟨{p.excerpt}⟩" for p in problems)


def _rewrite_block(audit):
    """The one second chance: the draft back in front of her, with the machine-checked defects."""
    return ("\n# Your draft did not pass the citation check -- rewrite it once\n"
            "Every defect below was found by comparing your draft against the numbered sources "
            "above. Fix each one and change nothing else, then output the whole report again. "
            "Where a claim has no source that carries it, drop the claim or state it as your own "
            "unsourced reading -- do not attach a number that does not hold it.\n"
            + prompt_guard.untrusted("citation-check-findings", _problem_list(audit.problems))
            + prompt_guard.untrusted("your-previous-draft", audit.body))


def _reference_list(sources, numbers):
    """What the reader resolves [N] against, in whichever numbering the delivered body ended up in.

    Only the rows the body actually cites: a report that passed comes back renumbered onto exactly
    its own sources, but a report that still has defects keeps the ledger's numbering (see
    ``citation.audit_report``), and printing the whole ledger under it would bury the handful of
    entries it cited in every finding the run ever made.
    """
    rows = "\n".join(f"- [{n}] {_one_line(s.label)}" + (f" — {s.url}" if s.url else "")
                     for n, s in enumerate(sources, start=1) if n in numbers)
    return f"\n\n## 引用来源\n\n{rows}\n"


def _unresolved(problems, failure):
    """The defects that survived the rewrite. Delivered with the report -- never in place of it."""
    note = f"\n(打回重写的那次调用未能完成:{failure};交付的是初稿。)\n" if failure else ""
    return ("\n\n## 未落地清单\n\n以下问题在交付稿里仍未对上台账证据。报告照常交付,但这些位置尚未落地,"
            f"读者应自行核对:\n{note}\n"
            + "\n".join(f"- **{p.kind}**(第 {p.line} 行):{p.message}\n  > {p.excerpt}" for p in problems)
            + "\n")


def _gate(con, run, cfg, worker, draft, sources, listing):
    """Audit the draft, spend at most one rewrite on it, and return what gets delivered."""
    audit = citation.audit_report(draft, sources)
    failure = ""
    if audit.problems:
        try:
            second = _write(con, run, cfg, worker, FINAL_CONTRACT, tools=FINAL_TOOLS,
                            extra=_source_block(listing) + _rewrite_block(audit))
        except Exception as error:  # noqa: BLE001 - the rewrite is a second chance; losing it costs the report its repairs, not its delivery
            failure = f"{type(error).__name__}: {error}"
        else:
            audit = citation.audit_report(second, sources)
    body = audit.body
    # audit.sources is what body's numbers point at -- the ledger's list while defects remain, the
    # renumbered one once it passed -- so the reference list is always read off the same pair.
    numbers = citation.cited(body, audit.sources)
    if numbers:
        body += _reference_list(audit.sources, numbers)
    if audit.problems:
        body += _unresolved(audit.problems, failure)
    return body


def finalize(con, run, cfg, worker):
    """survey.md (every node shown, nothing judged) then final.md (the adjudication, citation-gated)."""
    survey = _write(con, run, cfg, worker, SURVEY_CONTRACT, tools=SURVEY_TOOLS)
    _sid, survey_path = runs.write_text(con, run["id"], "survey", 'Survey by node', "survey.md", survey + "\n")
    sources, listing = _sources(con, run)
    final = _write(con, run, cfg, worker, FINAL_CONTRACT, tools=FINAL_TOOLS, extra=_source_block(listing))
    if sources:
        final = _gate(con, run, cfg, worker, final, sources, listing)
    aid, path = runs.write_text(con, run["id"], "final", 'Final report', "final.md", final + "\n")
    return {"artifact": aid, "path": path, "content": final, "survey_path": survey_path}

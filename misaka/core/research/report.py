"""Durable report drafts and Last Order adjudication of a separate red-team review.

Python orders the stages and checks artifact identity; it never judges or rewrites
research prose, quotations, numbers, or reference markers.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from misaka.core.platform import prompt_guard
from misaka.core.platform import tasks as task_store
from misaka.core.research import commands, ledger, planner, runs
from misaka.core.session_manager import find_most_recent_session

DRAFT_CONTRACT = """# Adjudication draft — not delivered; awaiting independent red-team review
Draft the answer to the original research question. The planning-stage prohibition on answering
is lifted. This turn is a working draft, not the delivered answer. Read every node's conclusion and complete critique;
consult full sources in context, not just ledger excerpts. The ledger records declarations, not machine-certified truths.

Do not vote or let a majority erase a minority view. Distinguish empirical claims, causal explanations, interpretations,
and normative premises. Examine differences in definitions, scope, period and method before resolving a disagreement.
Give a direct answer, the strongest competing accounts, evidentiary limits, unresolved questions, and what would change
the answer. Depth limits describe execution, not whether an objection is correct.

Use traceable citations in a format suited to the material: author/title, URLs, document/page or saved source paths.
Include the references the reader needs. You may retrieve material needed to verify or compare the submitted evidence;
preserve it in the workspace and identify its source and path so the red team can read it. Do not turn this draft into
an undisclosed new research assignment; identify substantial unfilled evidence needs as limits. Do not force interpretation or normative
reasoning into a verbatim quotation. Do not hide corrections or unsupported leaps behind reference markers.
A separate red team will review this saved draft before you adjudicate its objections.
""" + planner.SOURCES_FOOTER + planner.MARKDOWN_OUTPUT

FINAL_CONTRACT = """# Final adjudication — working turn, not delivered
Adjudicate the independent red team's review of the saved report draft. The planning-stage prohibition
on answering is lifted. Read the full draft, review, and the sources relevant to each objection. Criticism is not a verdict:
accept, reject, or retain disagreement on each substantive objection, explaining why. A missing source, interpretive
conflict, or normative disagreement is not automatically a factual error. Do not rank views by vote or authority.

Produce the complete final answer with traceable references, limits and competing explanations. Include a concise
review-disposition section explaining which objections changed the answer, which you rejected with reasons, and which
remain unsettled. You may consult sources to adjudicate the review, but do not conceal fresh evidence or present a new
claim as having been reviewed in the earlier draft. This is a terminal review, not another research-tree expansion.
This working turn will be archived; only the subsequent final-report delivery is authoritative.
""" + planner.SOURCES_FOOTER + planner.MARKDOWN_OUTPUT

SURVEY_CONTRACT = """# Finished-run survey — one section per node, in tree order
Show the question, methods, conclusion, key evidence, full red-team objections, and what each child found. Include competing
interpretations and uncertainty. Explain which issues opened children or were parked; statuses describe execution, not truth.
Do not vote, rank nodes or adjudicate the original question here.
""" + planner.MARKDOWN_OUTPUT

# Stage contracts change the deliverable, not the basic research/Skill capabilities.
SURVEY_TOOLS = FINAL_TOOLS = planner.RESEARCH_TOOLS


def _nodes(con, run):
    out = []
    for node in runs.nodes(con, run["id"]):
        synthesis = runs.artifacts(con, run["id"], kind="synthesis",
                                   **({"branch_id": node["id"]} if node["parent_id"] else {"root_only": True}))
        critique = runs.artifacts(con, run["id"], kind="critique",
                                  **({"branch_id": node["id"]} if node["parent_id"] else {"root_only": True}))
        plans = runs.artifacts(con, run["id"], kind="plan",
                               **({"branch_id": node["id"]} if node["parent_id"] else {"root_only": True}))
        manifest = os.path.join(run["workspace"], runs.node_dir(run, node["id"] if node["parent_id"] else None),
                                "SOURCES.md")
        out.append({"node": node["id"], "parent": node["parent_id"], "depth": node["depth"], "status": node["status"],
                    "question": node["trigger_text"],
                    "rounds": runs.plan_round(con, run["id"], node["id"]),
                    "plans": [a["path"] for a in plans],
                    # the node's folder in one page: its products, the sources they cite, hard-linked beside them
                    "sources_manifest": manifest if os.path.isfile(manifest) else None,
                    "conclusion_path": [a["path"] for a in synthesis],
                    "critique": [{"path": a["path"], "content": runs.artifact_text(a)} for a in critique
                                 if os.path.splitext(a["path"])[1].lower() == ".md"],
                    "critique_attachments": [a["path"] for a in critique
                                             if os.path.splitext(a["path"])[1].lower() != ".md"],
                    "issues": [{"issue": i["id"], "status": i["status"], "question": i["question"],
                                "reason": i["reason"], "child": i["child_branch_id"]}
                               for i in runs.issues(con, run["id"], node_id=node["id"])],
                    # the conclusion itself, so every node is in front of her rather than behind a path
                    "conclusion": "\n\n".join(runs.read_artifact(con, a["id"]) or "" for a in synthesis)})
    return out


def _boundary(con, run):
    return [{"issue": i["id"], "node": i["branch_id"], "status": i["status"], "question": i["question"],
             "rationale": i["rationale"]}
            for i in runs.issues(con, run["id"]) if i["status"] in {"assigned", "parked", "open"}]


def _write(con, run, cfg, worker, contract, *, tools, extra="", check_active=None):
    """One Last Order call in the root session over the whole tree; returns the Markdown it wrote.

    Each stage names its material/Skill baseline; stage contracts, not arbitrary
    capability starvation, distinguish the survey from final adjudication.
    """
    root = run["workspace"]
    # Both blocks below are model-written text that started life on a fetched page (a node's
    # synthesis, a red-team card's issue questions), so they get the same fence as the material
    # catalog -- otherwise half this prompt is guarded and half is not.
    prompt = (contract + f"""
# Original question
{run['question']}

# Nodes: conclusions, critiques, and what each issue's fork found (root first, then by depth)
Read the tree through each node's folder: its `sources_manifest` (nodes/<node>/SOURCES.md) lists that node's
products and every source they rest on, hard-linked under sources/ beside them; a node with `rounds` > 1 planned
more than once, and `plans` holds each round. A node whose status is `parked` and that has no conclusion was skipped
by the user before any research (its issue's `reason` says so): unresearched, not resolved. Read the sources through
the manifest, not only the conclusions.
{prompt_guard.untrusted("research-nodes", json.dumps(_nodes(con, run), ensure_ascii=False, indent=2))}
# Honest boundary: issues that stayed open, assigned, or parked
{prompt_guard.untrusted("research-boundary", json.dumps(_boundary(con, run), ensure_ascii=False, indent=2))}
""" + planner.navigation(run["id"]) + extra)
    session_dir = os.path.dirname(run["root_session"]) if run["root_session"] else runs.session_dir(run, "root-lo")
    if check_active:
        check_active()
    _obj, text, err = worker.run_llm_json(
        os.path.join(cfg["roles_root"], "last_order"), prompt,
        cfg["provider"], cfg["default_model"], cwd=root, tools=list(planner.session_tools(worker, tools)),
        timeout=None, soul=False, raw=True, research_context=True,
        usage_db=cfg.get("db"), usage_task_id=run["id"], usage_generation=1,
        usage_token_cap=cfg.get("token_cap"), session_dir=session_dir, session_file=run["root_session"],
        continue_session=bool(find_most_recent_session(session_dir)), thinking="high",
    )
    if check_active:
        check_active()
    if err or not text or not text.strip():
        raise RuntimeError(f"Report output is empty or failed: {err or ''}")
    return text


def materials(con, run):
    """Full declaration/source map, without selecting 'good' claims or a closed citation list."""
    rows = [{**dict(f), "claims": [dict(c) for c in ledger.claims(con, f["id"])]}
            for f in ledger.findings(con, run["id"])]
    files = [{"id": a["id"], "path": a["path"], "kind": a["kind"], "sha256": a["sha256"]}
             for a in runs.artifacts(con, run["id"])]
    submitted = planner.task_sources(con, run, [t for t in runs.tasks(con, run["id"]) if t["status"] == "done"])
    return prompt_guard.untrusted("research-materials", json.dumps(
        {"declared_findings": rows, "artifacts": files, "task_submissions": submitted}, ensure_ascii=False, indent=2))


def _checkpoint(con, run, kind):
    rows = runs.artifacts(con, run["id"], kind=kind, root_only=True)
    if not rows:
        return None
    row = rows[-1]
    runs.artifact_text(row)  # A modified checkpoint is an integrity error, not permission to redraft.
    return row


def prepare(con, run, cfg, worker, *, check_active=None):
    """Resume existing survey/draft checkpoints instead of silently changing the review target."""
    for kind, contract, tools in (("survey", SURVEY_CONTRACT, SURVEY_TOOLS),
                                   ("draft", DRAFT_CONTRACT, FINAL_TOOLS)):
        if check_active:
            check_active()
        if _checkpoint(con, run, kind) is None:
            text = _write(con, run, cfg, worker, contract, tools=tools,
                          extra="\n# Material catalog\n" + materials(con, run), check_active=check_active)
            if check_active:
                check_active()
            runs.write_text(con, run["id"], kind, kind.title(), runs.run_path(run, f"{kind}.md"), text)
    return _checkpoint(con, run, "draft")


def review_body(con, run, draft):
    return (f"""## goal
Independently red-team the saved final-report draft at `{draft['path']}` for this research question:
{run['question']}
Review target: artifact `{draft['id']}`, sha256 `{draft['sha256']}`.
Read the whole draft, the survey, node critiques and full sources (including supplements cited in the draft).
Inspect quotations and attribution in context, reasoning, causal and conceptual claims, methods, omissions,
competing interpretations and normative premises. Do not infer that a claim is false merely because a
number or wording is absent from a short excerpt. Your own objections also require reasons and may be wrong.
You may search/read supplementary sources; preserve and locate anything you use. Do not edit the draft.

## deliverable
Write `critique.md` under your card's deliverable directory. For each objection identify the draft passage,
source/context, reasoning, and a suggested correction or an explicit unresolved disagreement.
Call `misaka_card_note` with your complete `issues` list (kind, question, rationale, priority, material).
Use issues=[] explicitly if none. The root Last Order will adjudicate this review; no new research branches
are created from it and material=true is not an automatic verdict.

## material map
""" + materials(con, run))


def review_receipt(con, run, draft, task_id):
    """Require the current generation's submitted review of this exact saved draft."""
    rows = [r for r in runs.tasks(con, run["id"], kind="final_review") if r["id"] == task_id]
    if not rows or rows[0]["status"] != "done":
        raise ValueError("Final red-team review has not completed.")
    task = rows[0]
    target = json.loads(task_store.latest_payload(con, task_id, "research_review_target") or "{}")
    if target != {"artifact": draft["id"], "sha256": draft["sha256"]}:
        raise ValueError("Final review target does not match the saved draft.")
    runs.artifact_text(draft)
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
        extra = ("\n# Saved draft\n" + prompt_guard.untrusted("report-draft", runs.artifact_text(draft))
                 + "\n# Independent red-team receipt\n"
                 + prompt_guard.untrusted("final-review", json.dumps(review, ensure_ascii=False, indent=2))
                 + "\n# Material catalog\n" + materials(con, run))
        text = _write(con, run, cfg, worker, FINAL_CONTRACT, tools=FINAL_TOOLS,
                      extra=extra, check_active=check_active)
        if check_active:
            check_active()
        aid, _path = runs.write_text(con, run["id"], "final", "Final report",
                                    runs.run_path(run, "final.md"), text, metadata=metadata)
        final = runs.artifact(con, aid)
    return {"artifact": final["id"], "path": final["path"], "content": runs.artifact_text(final),
            "survey_path": survey["path"], "draft_path": draft["path"], "review_task_id": review_task_id}

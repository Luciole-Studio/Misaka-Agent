"""Wave-level research critic: review the independent syntheses and open material issues."""
import json
import os

from misaka.platform import prompt_guard, tasks as task_store
from misaka.research import ledger, runs

SOUL_TEMPLATE = """# Research Critic

You are the red team of the MISAKA research network. Your job is to hunt for reasoning failures.
Do not polish prose or format reports. Look for weak reasoning: unsupported assumptions, bias,
single-source evidence, overgeneralization, internal contradiction, and material gaps.

## Posture
- Be precise about what is uncertain. A false objection does as much damage as a false claim.
- Every criticism must point to a concrete next research step; rhetorical fault-finding is worthless.
- Raise questions rather than pronouncing verdicts; later research confirms or rejects them.
"""

RUN_PROMPT = """You are the red-team critic for a research run. Do not extend the report, and do not invent objections for the sake of balance.
Read the syntheses, the plan, and whichever source-task artifacts you need. Find the weak points that could materially change the answer.

Inspect facts and quotations, inference and causation, concepts and scope, methods and sampling, standpoint and bias,
omitted actors or processes, interactions, time horizons, consequences, and normative claims disguised as facts.
Different frameworks can yield different interpretations without either side being automatically wrong.

Return JSON only:
{"review_markdown":"The complete red-team review",
 "issues":[{"kind":"fact|logic|causation|concept|scope|method|bias|normative|unresolved",
   "question":"A specific question that can be researched on its own", "rationale":"Why it could change the conclusion",
   "priority":0, "material":true|false, "branch_id":"id of the branch the issue belongs to, or null"}],
 "disagreements":[{"claim_a":"", "claim_b":"", "type":"fact|causation|interpretation|method|normative|scope",
   "material":true|false, "resolvable":true|false,
   "question":"If resolvable, a self-contained question for a new Last Order branch",
   "what_would_resolve":"The evidence or clarification that would settle it, or why it cannot be settled"}],
 "evidence_assessments":[{"finding_id":"an evidence-ledger id, or empty for a synthesis-level claim",
   "claim":"The claim being assessed", "source_independence":"Whether the sources are genuinely independent, and why",
   "proximity":"Primary or secondary; how far from the object or event, and why", "method_fit":"How well the method fits the claim",
   "counterevidence":"The strongest contrary or missing evidence", "scope":"What the evidence actually supports",
   "judgement":"Reasoned decision to accept, downgrade, or suspend the claim"}],
 "stop_recommendation":{"stop":true|false,"reason":"The expected marginal value of another research wave"}}

Priority is dispatch order, not an evidence score. Only material issues open branches. Judge reliability by giving reasons,
not by adding up a score. Assess only the evidence that matters to the answer. Every material, resolvable disagreement must
come with a research question. Irreducible interpretive or normative disagreements stay in the final report; do not vote
them away. Output JSON only.
"""


def ensure_profile(roles_root):
    """Create the built-in critic profile when absent and return its directory."""
    profile = os.path.join(os.path.expanduser(roles_root), "critic")
    soul = os.path.join(profile, "SOUL.md")
    if not os.path.exists(soul):
        os.makedirs(profile, exist_ok=True)
        with open(soul, "w", encoding="utf-8") as f:
            f.write(SOUL_TEMPLATE)
    return profile


def critique_run(con, run, cfg, worker, *, wave, synthesis_tasks):
    """Review synthesized reports and return researchable gaps and disagreements."""
    root = runs.run_dir(run)
    targets = []
    for task in synthesis_tasks:
        try:
            with open(os.path.join(task_store.task_state_dir(task["id"]), "report.json"),
                      encoding="utf-8") as f:
                report = json.load(f)
        except (OSError, ValueError):
            report = {}
        for rel in report.get("artifacts") or []:
            path = os.path.realpath(os.path.join(task["workspace"] or "", str(rel)))
            if os.path.isfile(path):
                targets.append({"task_id": task["id"], "path": path})
    if not targets:
        raise RuntimeError("The red team has no synthesis report to review.")
    plan_paths = [a["path"] for a in runs.artifacts(con, run["id"])
                  if a["kind"] in {"plan", "method_review", "context"}]
    evidence_ledger = []
    for finding in ledger.findings(con, run["id"]):
        evidence_ledger.append({**dict(finding),
                                "claims": [dict(row) for row in ledger.claims(con, finding["id"])]})
    prompt = (RUN_PROMPT + f"""
# Original question
{run['question']}
"""
              + '\n# Synthesis reports (read these first)\n' + json.dumps(targets, ensure_ascii=False, indent=2)
              + '\n# Planning and branch context (read as needed)\n'
              + json.dumps(plan_paths, ensure_ascii=False, indent=2)
              + '\n# Research branches (set branch_id when an issue clearly belongs to one)\n'
              + json.dumps([dict(row) for row in runs.branches(con, run["id"])],
                           ensure_ascii=False, indent=2)
              + '\n# Current evidence ledger (give reasons for every reliability judgement)\n'
              + prompt_guard.untrusted(
                    "evidence-ledger", json.dumps(evidence_ledger, ensure_ascii=False, indent=2)))
    session_dir = runs.session_dir(run, "redteam", f"wave-{wave}")
    obj, raw, err = worker.run_llm_json(
        ensure_profile(cfg["roles_root"]), prompt, cfg["provider"], cfg["default_model"],
        cwd=root, tools=["read"], timeout=runs.call_timeout(
            cfg, max(600, int(cfg.get("judge_timeout", 600)))),
        bare=True, usage_db=cfg.get("db"), usage_task_id=run["id"], usage_generation=1,
        usage_token_cap=cfg.get("token_cap"), session_dir=session_dir, thinking="high",
    )
    if err:
        raise RuntimeError(f"Red-team review failed: {err}")
    if not isinstance(obj, dict) or not isinstance(obj.get("issues"), list):
        raise ValueError("The red team returned an invalid review.")
    issues = []
    for item in obj["issues"]:
        if not isinstance(item, dict) or not item.get("question") or not item.get("material"):
            continue
        try:
            priority = int(item.get("priority") or 0)
        except (TypeError, ValueError):
            priority = 0
        branch_id = str(item.get("branch_id") or "").strip() or None
        if branch_id and not con.execute(
            "SELECT 1 FROM research_branches WHERE id=? AND run_id=?", (branch_id, run["id"])
        ).fetchone():
            branch_id = None
        issues.append({"kind": str(item.get("kind") or "unresolved"),
                       "question": str(item["question"]).strip(),
                       "rationale": str(item.get("rationale") or "").strip(),
                       "priority": priority, "branch_id": branch_id})
    disagreements = []
    for item in obj.get("disagreements") or []:
        if not isinstance(item, dict):
            continue
        clean = {key: item.get(key) for key in (
            "claim_a", "claim_b", "type", "material", "resolvable", "question",
            "what_would_resolve")}
        disagreements.append(clean)
        if clean["material"] and clean["resolvable"] and str(clean["question"] or "").strip():
            issues.append({
                "kind": "disagreement", "question": str(clean["question"]).strip(),
                "rationale": str(
                    clean["what_would_resolve"]
                    or "Additional evidence is needed to resolve the disagreement."
                ).strip(),
                "priority": 0, "branch_id": None,
            })
    assessment_ids = []
    for assessment in obj.get("evidence_assessments") or []:
        if not isinstance(assessment, dict) or not str(assessment.get("claim") or "").strip() \
                or not str(assessment.get("judgement") or "").strip():
            continue
        aid = runs.add_assessment(con, run["id"], assessment, wave=wave)
        if aid:
            assessment_ids.append(aid)
    review = str(obj.get("review_markdown") or "").strip()
    if not review:
        review = '# Red-team review\n\n' + "\n".join(f"- {x['question']}" for x in issues)
    json_aid, json_path = runs.write_text(
        con, run["id"], "critique", f"Wave {wave} red-team review (JSON)",
        f"critiques/{wave}.json", json.dumps(obj, ensure_ascii=False, indent=2),
        metadata={"wave": wave},
    )
    md_aid, md_path = runs.write_text(
        con, run["id"], "critique", f"Wave {wave} red-team review",
        f"critiques/{wave}.md", review, metadata={"wave": wave, "json_artifact": json_aid},
    )
    return {"issues": issues, "disagreements": disagreements,
            "assessment_ids": assessment_ids,
            "stop_recommendation": obj.get("stop_recommendation") or {},
            "json_artifact": json_aid, "artifact": md_aid,
            "paths": [json_path, md_path], "raw": raw}

"""The end of a run: a survey that introduces every node's result without judging (survey.md), then the
adjudication that answers from all of them, keeping disagreement visible (final.md)."""
from __future__ import annotations

import json
import os

from misaka.core.session_manager import find_most_recent_session
from misaka.research import runs


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


def _write(con, run, cfg, worker, contract):
    """One Last Order call in the root session over the whole tree; returns the Markdown it wrote."""
    root = runs.run_dir(run)
    prompt = (contract + f"""
# Original question
{run['question']}

# Nodes: conclusions, critiques, and what each issue's fork found (root first, then by depth)
{json.dumps(_nodes(con, run), ensure_ascii=False, indent=2)}

# Honest boundary: issues that stayed open, inconclusive, or parked
{json.dumps(_boundary(con, run), ensure_ascii=False, indent=2)}
""")
    session_dir = runs.session_dir(run, "root-lo")
    _obj, text, err = worker.run_llm_json(
        os.path.join(cfg["roles_root"], "last_order"), prompt,
        cfg["provider"], cfg["default_model"], cwd=root, tools=["read"],
        timeout=runs.call_timeout(cfg, max(900, int(cfg.get("judge_timeout", 600)))), soul=False, raw=True,
        usage_db=cfg.get("db"), usage_task_id=run["id"], usage_generation=1,
        usage_token_cap=cfg.get("token_cap"), session_dir=session_dir,
        continue_session=bool(find_most_recent_session(session_dir)), thinking="high",
    )
    if err or not text or len(text.strip()) < 80:
        raise RuntimeError("Final report output is too short.")
    return text.strip()


def finalize(con, run, cfg, worker):
    """survey.md (every node shown, nothing judged) then final.md (the adjudication)."""
    survey = _write(con, run, cfg, worker, SURVEY_CONTRACT)
    _sid, survey_path = runs.write_text(con, run["id"], "survey", 'Survey by node', "survey.md", survey + "\n")
    final = _write(con, run, cfg, worker, FINAL_CONTRACT)
    aid, path = runs.write_text(con, run["id"], "final", 'Final report', "final.md", final + "\n")
    return {"artifact": aid, "path": path, "content": final, "survey_path": survey_path}

"""The final adjudication: Last Order answers from every node's conclusion, keeping disagreement visible."""
from __future__ import annotations

import json
import os

from misaka.core.session_manager import find_most_recent_session
from misaka.research import runs


FINAL_CONTRACT = """You are Last Order at the final adjudication stage of a research run. The planning-stage rule against answering is now lifted.
Read the root conclusion, every node conclusion that re-researched an undermined point, every red-team critique, and whichever
source-task artifacts you need, then answer the original question.

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


def _nodes(con, run):
    out = []
    for node in runs.nodes(con, run["id"]):
        synthesis = runs.artifacts(con, run["id"], kind="synthesis",
                                   **({"branch_id": node["id"]} if node["parent_id"] else {"root_only": True}))
        critique = runs.artifacts(con, run["id"], kind="critique",
                                  **({"branch_id": node["id"]} if node["parent_id"] else {"root_only": True}))
        out.append({"node": node["id"], "depth": node["depth"], "status": node["status"],
                    "question": node["trigger_text"],
                    "conclusion": [a["path"] for a in synthesis],
                    "critique": [a["path"] for a in critique]})
    return out


def _boundary(con, run):
    return [{"issue": i["id"], "node": i["branch_id"], "status": i["status"], "question": i["question"],
             "rationale": i["rationale"]}
            for i in runs.issues(con, run["id"]) if i["status"] in {"inconclusive", "parked", "open"}]


def adjudicate(con, run, cfg, worker):
    root = runs.run_dir(run)
    prompt = (FINAL_CONTRACT + f"""
# Original question
{run['question']}

# Nodes: conclusions and critiques (root first, then the nodes that re-researched undermined points)
{json.dumps(_nodes(con, run), ensure_ascii=False, indent=2)}

# Honest boundary: issues that stayed open, inconclusive, or parked
{json.dumps(_boundary(con, run), ensure_ascii=False, indent=2)}
""")
    session_dir = runs.session_dir(run, "root-lo")
    _obj, text, err = worker.run_llm_json(
        os.path.join(cfg["roles_root"], "last_order"), prompt,
        cfg["provider"], cfg["default_model"], cwd=root, tools=["read"],
        timeout=runs.call_timeout(cfg, max(900, int(cfg.get("judge_timeout", 600)))),
        bare=True, soul=False, raw=True,
        usage_db=cfg.get("db"), usage_task_id=run["id"], usage_generation=1,
        usage_token_cap=cfg.get("token_cap"), session_dir=session_dir,
        continue_session=bool(find_most_recent_session(session_dir)), thinking="high",
    )
    if err or not text or len(text.strip()) < 80:
        raise RuntimeError("Final report output is too short.")
    return text.strip()


def finalize(con, run, cfg, worker):
    final = adjudicate(con, run, cfg, worker)
    aid, path = runs.write_text(con, run["id"], "final", 'Final report', "final.md", final + "\n")
    return {"artifact": aid, "path": path, "content": final}

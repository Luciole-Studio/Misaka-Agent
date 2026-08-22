"""Research synthesis cards and the final adjudication that preserves disagreement."""
from __future__ import annotations

import json
import os

from misaka.core.session_manager import find_most_recent_session
from misaka.platform import prompt_guard, tasks as task_store
from misaka.research import runs


SYNTHESIS_PREFIX = 'Consolidated:'


def gather(con, ids=None):
    """Compatibility helper: return done tasks that are not synthesis tasks."""
    rows = [task_store.get(con, i) for i in ids] if ids else task_store.by_status(con, "done")
    return [r for r in rows if r and r["status"] == "done"
            and not r["title"].startswith(SYNTHESIS_PREFIX)]


def _task_sources(rows):
    parts = []
    for row in rows:
        report = {}
        try:
            with open(os.path.join(task_store.task_state_dir(row["id"]), "report.json"),
                      encoding="utf-8") as handle:
                report = json.load(handle)
        except (OSError, ValueError):
            pass
        artifacts = []
        for rel in report.get("artifacts") or []:
            path = os.path.realpath(os.path.join(row["workspace"] or "", str(rel)))
            if os.path.isfile(path):
                artifacts.append(path)
        parts.append({"task_id": row["id"], "title": row["title"],
                      "summary": report.get("summary") or "", "artifacts": artifacts,
                      "findings": report.get("findings") or [],
                      "uncertain": report.get("uncertain") or []})
    return parts


def card_body(rows, title, *, lens='Overall evidence and competing interpretations', run_id=None, wave=0,
              preflight_path=None):
    """Build the contract for one independent synthesis card from auditable task artifacts."""
    sources = _task_sources(rows)
    payload = prompt_guard.untrusted(
        "research-material-map", json.dumps(sources, ensure_ascii=False, indent=2))
    preflight = f"Read `{preflight_path}` first.\n" if preflight_path else ""
    return f"""## goal
Independently synthesize the accepted research output for "{title}" into `synthesis.md`.
Analytical lens: **{lens}**.
{preflight}Research run: {run_id or 'unassigned'}
Wave: {wave}

## material map
{payload}

## boundaries
- Introduce no facts beyond the supplied artifacts. Summaries are navigation aids, not evidence; read the source artifacts.
- Do not treat repeated accounts of the same thing as independent evidence.
- Do not vote, and do not hide competing interpretations or insufficient evidence.

## acceptance criteria
- `synthesis.md` exists.
- It separates shared findings, competing findings, key evidence, counterevidence, methodological limits,
  value premises, and unresolved questions.
- Every empirical judgement cites `[task_id/path]`.
- It states what new evidence could change the judgement.
"""


def create(con, rows, title, *, assignee="synthesizer", lens='Overall evidence and competing interpretations',
           workspace=None, run_id=None, wave=0, preflight_path=None, timeout_seconds=1200):
    """Create one synthesis task; the caller links it to a research run."""
    return task_store.create_task(
        con, f"{SYNTHESIS_PREFIX}{title}",
        body=card_body(rows, title, lens=lens, run_id=run_id, wave=wave,
                       preflight_path=preflight_path),
        assignee=assignee, workspace=workspace, timeout_seconds=timeout_seconds,
    )


FINAL_CONTRACT = """You are Last Order at the final adjudication stage of a research run. The planning-stage rule against answering is now lifted.
Read every independent synthesis, every red-team review, the disagreement matrix, and whichever source-task artifacts you need, then answer the original question.

Do not vote, and do not let a majority erase a minority view. Work out whether apparent conflicts come from different definitions,
scopes, periods, methods, evidence, or value premises. Resolve only what the material supports. Keep competing
conclusions side by side when the evidence cannot decide between them. Separate empirical facts, causal interpretations,
broader interpretations, and normative choices.

The reader must be able to identify:
- a direct answer to the original question;
- supported common findings and their evidentiary limits;
- the strongest competing conclusions and their premises;
- factual, causal, methodological, interpretive, and normative disagreements;
- claims overturned or downgraded by red-team review;
- areas not researched, material that was unavailable, and questions that remain undecidable;
- the evidence that would change the conclusion.

Write free-form Markdown only. Do not output JSON or describe these instructions.
"""

FINAL_AUDIT = """You are the final-report auditor. Read the draft and the research output it cites.
Check whether the draft represents the evidence faithfully, turns unresolved disagreement into false consensus,
passes off interpretation or value judgement as fact, cites material that does not exist, or omits a red-team issue that could
change the answer.

Return JSON only:
{"approved": true|false, "audit_markdown":"The complete audit",
 "material_errors":["A substantive error that must be corrected"],
 "unresolved_ok":["A disagreement that should stay open rather than be forced to a resolution"]}
Do not list minor editorial preferences as material errors.
"""


def _paths(con, run_id, kinds):
    return [a["path"] for a in runs.artifacts(con, run_id) if a["kind"] in kinds]


def adjudicate(con, run, cfg, worker, *, synthesis_tasks, disagreements=()):
    root = runs.run_dir(run)
    synth_paths = []
    for row in synthesis_tasks:
        for item in _task_sources([row]):
            synth_paths.extend(item["artifacts"])
    supporting = _paths(con, run["id"], {"plan", "method_review", "critique", "context"})
    prompt = (FINAL_CONTRACT + f"""
# Original question
{run['question']}
"""
              + '\n# Independent syntheses\n' + json.dumps(synth_paths, ensure_ascii=False, indent=2)
              + '\n# Plan, branch context, and red-team reviews\n'
              + json.dumps(supporting, ensure_ascii=False, indent=2)
              + '\n# Disagreement matrix\n' + json.dumps(list(disagreements), ensure_ascii=False, indent=2))
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
    aid, path = runs.write_text(con, run["id"], "final_draft", 'Draft final report',
                                "final.draft.md", text.strip() + "\n")
    return aid, path, text.strip()


def audit_final(con, run, cfg, worker, draft_path):
    root = runs.run_dir(run)
    prompt = (FINAL_AUDIT + f"""
# Original question
{run['question']}
# Draft of final.md
{draft_path}
"""
              + '# Red-team reviews and syntheses to check against\n'
              + json.dumps(_paths(con, run["id"], {"critique", "synthesis"}),
                           ensure_ascii=False, indent=2))
    session_dir = runs.session_dir(run, "final-audit")
    obj, raw, err = worker.run_llm_json(
        os.path.join(cfg["roles_root"], "redteam"), prompt,
        cfg["provider"], cfg["default_model"], cwd=root, tools=["read"],
        timeout=runs.call_timeout(cfg, max(600, int(cfg.get("judge_timeout", 600)))),
        bare=True, soul=False,
        usage_db=cfg.get("db"), usage_task_id=run["id"], usage_generation=1,
        usage_token_cap=cfg.get("token_cap"), session_dir=session_dir, thinking="high",
    )
    if err or not isinstance(obj, dict) or not isinstance(obj.get("approved"), bool):
        raise RuntimeError("Final audit returned an invalid response.")
    aid, path = runs.write_text(
        con, run["id"], "final_audit", 'Final report audit', "final-audit.json",
        json.dumps(obj, ensure_ascii=False, indent=2),
    )
    return obj, aid, path, raw


def revise_final(con, run, cfg, worker, draft_path, audit):
    root = runs.run_dir(run)
    prompt = ('Revise the report to address the final audit. Output the complete Markdown only; do not explain what you changed. Keep unresolved disagreements open.\n'
              + f"""# Draft
{draft_path}
# Audit
"""
              + json.dumps(audit, ensure_ascii=False, indent=2))
    session_dir = runs.session_dir(run, "root-lo")
    _obj, text, err = worker.run_llm_json(
        os.path.join(cfg["roles_root"], "last_order"), prompt,
        cfg["provider"], cfg["default_model"], cwd=root, tools=["read"], raw=True,
        timeout=runs.call_timeout(cfg, max(900, int(cfg.get("judge_timeout", 600)))),
        bare=True, soul=False,
        usage_db=cfg.get("db"), usage_task_id=run["id"], usage_generation=1,
        usage_token_cap=cfg.get("token_cap"), session_dir=session_dir,
        continue_session=True, thinking="high",
    )
    if err or not text or len(text.strip()) < 80:
        raise RuntimeError("Revised final report output is too short.")
    return text.strip()


def finalize(con, run, cfg, worker, *, synthesis_tasks, disagreements=()):
    _draft_id, draft_path, draft = adjudicate(
        con, run, cfg, worker, synthesis_tasks=synthesis_tasks, disagreements=disagreements)
    audit, _audit_id, _audit_path, _raw = audit_final(con, run, cfg, worker, draft_path)
    final = revise_final(con, run, cfg, worker, draft_path, audit) if not audit["approved"] else draft
    aid, path = runs.write_text(con, run["id"], "final", 'Final report',
                                "final.md", final.rstrip() + "\n")
    return {"artifact": aid, "path": path, "content": final, "audit": audit}

"""Last Order's research calls and the card contracts, with a deliberately small machine-checked envelope.

The prose is open-ended. Python validates only the fields needed to route
work; it never judges whether a method, source, interpretation, or conclusion is sound.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from misaka.config import CFG
from misaka.core.session_manager import find_most_recent_session
from misaka.platform import prompt_guard
from misaka.platform import tasks as task_store
from misaka.research import ledger, runs
from misaka.skills import layers as skill_layers

PROJECT_INTAKE_CONTRACT = """You are Last Order in Research mode. Draft the project brief (PROJECT.md) for the user's research question.
Do not answer the original question, and do not break it into research tasks yet.
Return exactly one JSON object:
{"project_markdown": "the complete PROJECT.md"}

Requirements:
- project_markdown must cover the original question, the research goal, the assumptions to test, and the boundaries.
- Phrase assumptions as things to investigate, never as conclusions.
- Output JSON only.
"""


ROOT_CONTRACT = """You are Last Order, the planner for Research mode.
Do not answer the user's question at this stage. Your only deliverable is a research design.
Do not assume that textbooks, mass media, the mainstream view, or the contrarian view is correct.

Start by working out what the user is actually asking, what else the question could mean, and which of its
premises are untested. Then map the relevant objects, processes, interactions, prior knowledge, time horizons,
consequences, feedback effects, and disciplines. Pick the methods, theories, analytical frameworks, and source
strategies that fit the problem, and state each method's blind spots and the competing approaches. The method
catalog is a menu, not a requirement: combine, reject, or add methods as the problem demands. Before dividing the
work, check the design for dimensions it forgot: read the `coverage-maps` skill from the method catalog (its references
are maps of fields, facets, kinds of question, and traditions) and call `coverage_scan` with two or three phrasings of
the question to see where the literature actually discusses it. Only after that divide the work into tasks and choose
Sisters by their profiles and skills. `plan_markdown` must end with a section "Coverage maps used": which maps and scans
you consulted, which you did not and why, and which dimensions they surfaced.

Return exactly one JSON object of this shape. `plan_markdown` and `extensions` are free-form:
{
  "status": "ready|clarify",
  "clarifying_questions": ["A question that needs a human decision before research can continue"],
  "plan_markdown": "The complete, open-ended research plan in Markdown; never the answer",
  "methods": [{"name":"Method", "why":"Why it fits", "blind_spots":"What it may miss", "skill":"optional skill name"}],
  "tasks": [{
    "local_id":"short-safe-id", "title":"Task title", "question":"The exact research question",
    "rationale":"Why it matters", "method":"How to investigate it", "source_strategy":"Where to look and what to look for",
    "falsifiers":"Evidence that would overturn the working premise", "deliverable":"Path of the Markdown artifact",
    "dependencies":["other-local-id"], "capabilities":["required capability"],
    "assignee":"a Sister id from the roster", "assignee_reason":"Why this Sister fits", "priority":0
  }],
  "red_team": {"assignee":"a Sister id from the roster", "reason":"Why this Sister's profile fits adversarial review"},
  "extensions": {}
}

Rules:
- Never force a question into PICO, a causal-variable model, or any other single-discipline template.
- Use status=clarify only when research genuinely cannot continue without a human choice. Uncertainty that can be researched belongs in a probe or a task.
- Tasks must be substantive research assignments written for this question, not mechanical templates.
- Choose Sisters by their capability profiles; do not default to the first roster entry.
- The red team is the Sister whose profile makes her the best critic of this plan's conclusion; only you decide who that is.
- Output JSON only.
"""


PREFLIGHT_CONTRACT = """You are a Sister preparing to execute a research assignment.
Plan the investigation before starting it; do no research and draw no conclusions yet. Decide where and how to find
material, how you will tell good sources from bad, which tools and methods fit, which premises might be wrong, and what
would make you change course or stop.

Return JSON only:
{"preflight_markdown":"A free-form plan in Markdown",
 "sources":["A source type, collection, archive, or dataset to try first"],
 "queries":["An initial search or inspection step"],
 "tools":["A system tool or skill you may use"],
 "failure_modes":["A likely failure or source of bias"],
 "falsifiers":["Material that could overturn the task's working premise"],
 "stop_condition":"When the task is done, or when it can no longer proceed honestly"}
"""


SYNTHESIS_CONTRACT = """You are Last Order writing this node's conclusion: one synthesis of the accepted research output, in one pass.
Read the source artifacts (summaries are navigation aids, not evidence) and check every claim against its quotation.

Write free-form Markdown that separates: shared findings, competing findings, key evidence, counterevidence,
methodological limits, value premises, and unresolved questions. Every empirical judgement cites `[task_id/path]`.
Do not vote, do not hide competing interpretations or insufficient evidence, and introduce no facts beyond the supplied artifacts.
State what new evidence could change the judgement. Output Markdown only; no JSON.
"""


PROBE_CONTRACT = """You are Last Order's fork on ONE issue the red team raised against this node's conclusion. You keep the
node's whole planning context; this issue is the only thing you investigate. Do not replan the project.

Each round, do one of two things:
- open research cards that test the issue (one or several; choose Sisters by their profiles), or
- when what the cards returned settles it, give the verdict.

Return exactly one JSON object:
{"tasks": [{
   "local_id":"short-safe-id", "title":"Card title", "question":"The exact question the card answers",
   "rationale":"How the answer bears on the issue", "method":"How to investigate it", "source_strategy":"Where to look",
   "falsifiers":"What would show the conclusion is wrong", "deliverable":"Path of the Markdown artifact",
   "dependencies":[], "capabilities":[], "assignee":"a Sister id from the roster", "assignee_reason":"Why this Sister fits",
   "priority":0}],
 "verdict": null | {"verdict":"supports|inconclusive|undermines", "reason":"What in the card artifacts decides it"}}
- supports: the conclusion stands; the material is additional support.
- inconclusive: the issue cannot be settled (material unavailable, sources conflict, question not answerable).
- undermines: the conclusion is shaken; the issue deserves a research node of its own.
A round without tasks must carry a verdict. Do not vote and do not soften. Output JSON only.
"""


RED_TEAM_CONTRACT = """## goal
Red-team the conclusion at `{synthesis_path}` for "{question}". Hunt for reasoning failures; do not extend the report and do not
polish prose. Deliver `critique.md` (your review) and `critique.json` (the issues, machine-readable).

## material
- Conclusion under review: `{synthesis_path}`
- Plan: `{plan_path}`
- Source-task artifacts: read whichever the conclusion cites.
{evidence}
## what to inspect
Facts and quotations, inference and causation, concepts and scope, methods and sampling, standpoint and bias, omitted actors or
processes, interactions, time horizons, consequences, and normative claims disguised as facts. For omissions, use the
`coverage-maps` skill (maps of fields, facets, kinds of question, traditions) and `coverage_scan` (where the literature discusses
this question): a dimension the conclusion never touches is an issue. Different frameworks can yield different interpretations
without either side being automatically wrong. A false objection does as much damage as a false claim.

## acceptance criteria
- `critique.md` exists and every criticism names a concrete next research step.
- `critique.json` exists and is exactly: `{{"issues": [{{"kind": "fact|logic|causation|concept|scope|method|bias|normative|unresolved",
  "question": "A specific question that can be researched on its own", "rationale": "Why it could change the conclusion",
  "priority": 0, "material": true}}]}}`. Only `material: true` issues are probed; keep the list honest, not long.
- Register both files in `report.json`.
"""


def _frontmatter_description(path):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    if not text.startswith("---"):
        return ""
    for line in text.split("---", 2)[1].splitlines():
        if line.strip().startswith("description:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    return ""


def sister_catalog(root=None):
    root = os.path.expanduser(root or CFG["profiles_root"])
    if not os.path.isdir(root):
        return []
    out = []
    for sid in sorted(os.listdir(root)):
        profile = os.path.join(root, sid)
        if not os.path.isdir(profile):
            continue
        desc_path = os.path.join(profile, "DESCRIBE.md")
        description = _frontmatter_description(desc_path)
        try:
            body = Path(desc_path).read_text(encoding="utf-8")[:3000]
        except OSError:
            body = ""
        skills = []
        skills_dir = os.path.join(profile, "skills")
        if os.path.isdir(skills_dir):
            for name in sorted(x for x in os.listdir(skills_dir) if not x.startswith(".")):
                skill_path = os.path.join(skills_dir, name, "SKILL.md")
                skills.append({"name": name, "description": _frontmatter_description(skill_path),
                               "path": skill_path})
        out.append({"id": sid, "description": description, "profile": body, "skills": skills})
    return out


def method_catalog(profile_dir, cwd):
    """The research methods Last Order can plan with: her skill index in this folder."""
    from misaka.skills import index as skill_index
    return [{"name": e["name"], "description": e["description"], "path": e["path"]}
            for e in skill_index.build(skill_layers.skill_roots(profile_dir, cwd))]


def _catalog_text(items):
    return json.dumps(items, ensure_ascii=False, indent=2)


def _call(worker, cfg, prompt, *, cwd, session_dir, continue_session=False,
          profile="last_order", tools=("read",), raw=False, task_id=None,
          timeout=None, thinking="high", model=None):
    kwargs = dict(
        cwd=cwd, tools=list(tools),
        timeout=runs.call_timeout(
            cfg, timeout or max(600, int(cfg.get("judge_timeout", 600)))), soul=False,
        raw=raw, usage_db=cfg.get("db"), usage_task_id=task_id,
        usage_generation=1, usage_token_cap=cfg.get("token_cap"),
        session_dir=session_dir, continue_session=continue_session,
        thinking=thinking,
    )
    if model:
        kwargs["model"] = model
    return worker.run_llm_json(
        os.path.join(cfg["roles_root"], profile), prompt,
        cfg["provider"], cfg["default_model"], **kwargs,
    )


def intake_session_dir(cfg, workspace):
    """Where the PROJECT.md intake conversation is kept: under the runs root, never inside the
    project folder (a fresh project is committed wholesale by ``misaka init``)."""
    import hashlib
    root = (cfg or {}).get("tasks_root") or os.path.expanduser("~/.misaka/tasks")
    digest = hashlib.sha256(str(Path(workspace).expanduser().resolve()).encode("utf-8")).hexdigest()[:12]
    return os.path.join(root, "intake", digest)


def ensure_project_brief(cfg, worker, question, workspace):
    """Return ``<workspace>/PROJECT.md``, letting Last Order draft it when the folder has none."""
    workspace = str(Path(workspace).expanduser().resolve())
    path = os.path.join(workspace, "PROJECT.md")
    if os.path.isfile(path):
        return path
    obj, _raw, err = _call(
        worker, cfg, PROJECT_INTAKE_CONTRACT + f"""
# The user's research question
{question}
""",
        cwd=workspace, session_dir=intake_session_dir(cfg, workspace), tools=(),
        timeout=120, thinking="low",
    )
    if err:
        raise RuntimeError(f"Last Order could not draft PROJECT.md: {err}")
    text = str((obj or {}).get("project_markdown") or "").strip()
    if len(text) < 40:
        raise ValueError("Last Order did not generate a complete PROJECT.md.")
    Path(path).write_text(text.rstrip() + "\n", encoding="utf-8")
    return path


def _validate_task(raw, roster, index):
    if not isinstance(raw, dict):
        raise ValueError(f"Task {index} is not an object.")
    required = ("local_id", "title", "question", "rationale", "deliverable", "assignee")
    task = {k: raw.get(k) for k in raw}
    for key in required:
        if not isinstance(task.get(key), str) or not task[key].strip():
            raise ValueError(f"Task {index} is missing {key!r}.")
        task[key] = task[key].strip()
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


def validate_plan(obj, roster):
    if not isinstance(obj, dict):
        raise ValueError("Last Order planning output is not an object.")
    status = obj.get("status")
    if status not in {"ready", "clarify"}:
        raise ValueError("Last Order returned an invalid planning status.")
    plan_markdown = obj.get("plan_markdown")
    if not isinstance(plan_markdown, str) or len(plan_markdown.strip()) < 40:
        raise ValueError("Last Order returned an incomplete plan_markdown value.")
    roster_ids = {r["id"] if isinstance(r, dict) else str(r) for r in roster}
    tasks = _validate_tasks(obj.get("tasks"), roster_ids)
    if status == "ready" and not tasks:
        raise ValueError("A ready research plan must contain at least one task.")
    questions = obj.get("clarifying_questions") or []
    if not isinstance(questions, list) or any(not isinstance(q, str) or not q.strip() for q in questions):
        raise ValueError("clarifying_questions must be a list of non-empty strings.")
    if status == "clarify" and not questions:
        raise ValueError("A clarify plan must ask at least one question.")
    red_team = obj.get("red_team") if isinstance(obj.get("red_team"), dict) else {}
    if status == "ready" and red_team.get("assignee") not in roster_ids:
        raise ValueError("Last Order must name one roster Sister as the red team.")
    return {
        **obj, "status": status, "plan_markdown": plan_markdown.strip(), "tasks": tasks,
        "red_team": {"assignee": red_team.get("assignee"), "reason": str(red_team.get("reason") or "")},
        "clarifying_questions": [q.strip() for q in questions],
        "methods": obj.get("methods") if isinstance(obj.get("methods"), list) else [],
        "extensions": obj.get("extensions") if isinstance(obj.get("extensions"), dict) else {},
    }


def _roster(cfg):
    roster = sister_catalog(cfg.get("profiles_root"))
    if not roster:
        raise RuntimeError("The Sister roster is empty; research tasks cannot be assigned.")
    return roster


def _lo_session(run, node, *parts):
    return runs.session_dir(run, "root-lo" if node["parent_id"] is None else f"node-{node['id']}", *parts)


def plan(con, run, cfg, worker, node, *, context_path=None):
    """Open or continue the node's Last Order session and return its research plan."""
    root = runs.run_dir(run)
    session_dir = _lo_session(run, node)
    roster = _roster(cfg)
    methods = method_catalog(os.path.join(cfg["roles_root"], "last_order"), run["workspace"])   # the project folder's skills/, not the run dir's
    prompt = ROOT_CONTRACT
    if node["parent_id"] is None:
        prompt += f"""
# Original question
{run['question']}

# Project / PageIndex workspace index (read before planning)
{root}/workspace-index.md
"""
    else:
        prompt += f"""
This is a targeted research node. Address the issue that undermined the parent conclusion without replanning the whole project.

# Issue that opened this node
{node['trigger_text']}

# Context packet (ancestor sessions and artifact map; read it first)
{context_path}
"""
    prompt += f"""
# Sister capability profiles
{_catalog_text(roster)}

# Method and tool skills available
{_catalog_text(methods)}
"""
    obj, raw, err = _call(
        worker, cfg, prompt, cwd=root, session_dir=session_dir, tools=("read", "coverage_scan"),
        continue_session=bool(find_most_recent_session(session_dir)), task_id=run["id"],
    )
    if err:
        raise RuntimeError(f"Last Order planning failed for node {node['id']}: {err}")
    return validate_plan(obj, roster), raw, find_most_recent_session(session_dir)


def task_sources(rows):
    """What each done card delivered: its report and the artifacts it registered, as absolute paths."""
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


def evidence_block(con, run, node):
    """The node's evidence ledger (findings with their verbatim claims), wrapped as untrusted data."""
    findings = []
    for finding in ledger.findings(con, run["id"], branch_id=node["id"]):
        findings.append({**dict(finding),
                         "claims": [dict(row) for row in ledger.claims(con, finding["id"])]})
    return prompt_guard.untrusted("evidence-ledger", json.dumps(findings, ensure_ascii=False, indent=2))


def synthesize(con, run, cfg, worker, node, task_rows):
    """Last Order writes the node's conclusion from its accepted cards. Returns Markdown."""
    prompt = (SYNTHESIS_CONTRACT + f"""
# Question
{node['trigger_text']}

# Material map (read the artifacts, not just this map)
""" + prompt_guard.untrusted("research-material-map",
                            json.dumps(task_sources(task_rows), ensure_ascii=False, indent=2))
              + "\n# Evidence ledger\n" + evidence_block(con, run, node))
    session_dir = _lo_session(run, node)
    _obj, text, err = _call(
        worker, cfg, prompt, cwd=runs.run_dir(run), session_dir=session_dir, raw=True,
        continue_session=bool(find_most_recent_session(session_dir)), task_id=run["id"],
        timeout=max(900, int(cfg.get("judge_timeout", 600))),
    )
    if err or not text or len(text.strip()) < 80:
        raise RuntimeError(f"Synthesis for node {node['id']} is too short or failed: {err or ''}")
    return text.strip() + "\n"


def red_team_body(node, *, synthesis_path, plan_path, evidence=""):
    return RED_TEAM_CONTRACT.format(
        question=node["trigger_text"], synthesis_path=synthesis_path, plan_path=plan_path,
        evidence=f"- Evidence ledger:\n{evidence}\n" if evidence else "")


def fork_session(source_dir, target_dir):
    """A pi fork of the newest session in ``source_dir`` into ``target_dir``: the whole line so far,
    with ``parentSession`` recorded. None when there is nothing to fork (the fork then starts fresh)."""
    from misaka.core.session_manager import SessionManager
    source = find_most_recent_session(source_dir)
    if not source:
        return None
    os.makedirs(target_dir, exist_ok=True)
    manager = SessionManager.open(source, target_dir)
    leaf = manager.getLeafId()
    if not leaf:
        return None
    branched = manager.createBranchedSession(leaf)
    if branched and not os.path.isfile(branched):
        manager.rewrite_file()  # the write is deferred until an assistant turn; the fork resumes from disk
    return branched


def probe_step(con, run, cfg, worker, node, issue, cards, *, synthesis_path, round_no, rounds, final=False):
    """One round of the fork on ``issue``: more cards, or the verdict. Returns (tasks, verdict).
    With ``final`` no more cards may be opened: the answer must be a verdict."""
    roster = _roster(cfg)
    closing = ("\n# Final judgement: every round has been used, no more cards can be opened. "
               "Give the verdict now, from the artifacts returned so far.\n") if final else ""
    prompt = (PROBE_CONTRACT + closing + f"""
# Issue
{_catalog_text({"id": issue["id"], "kind": issue["kind"], "question": issue["question"], "rationale": issue["rationale"]})}

# Conclusion under test
{synthesis_path}

# Round {round_no} of {rounds}. Cards returned so far (read their artifacts; none yet on round 1)
""" + prompt_guard.untrusted("probe-results", json.dumps(task_sources(cards), ensure_ascii=False, indent=2))
              + f"""

# Sister capability profiles
{_catalog_text(roster)}
""")
    session_dir = runs.probe_session_dir(run, issue["id"])
    obj, _raw, err = _call(
        worker, cfg, prompt, cwd=runs.run_dir(run), session_dir=session_dir,
        continue_session=bool(find_most_recent_session(session_dir)), task_id=run["id"],
    )
    if err:
        raise RuntimeError(f"The fork on issue {issue['id']} failed: {err}")
    obj = obj if isinstance(obj, dict) else {}
    tasks = _validate_tasks(obj.get("tasks"), {r["id"] for r in roster})
    verdict = obj.get("verdict")
    if verdict is not None:
        if not isinstance(verdict, dict) or verdict.get("verdict") not in {"supports", "inconclusive", "undermines"}:
            raise ValueError(f"The fork on issue {issue['id']} returned an invalid verdict.")
        verdict = {"verdict": verdict["verdict"], "reason": str(verdict.get("reason") or "")}
    if final and verdict is None:
        raise ValueError(f"The fork on issue {issue['id']} did not give a verdict in its final round.")
    if not tasks and verdict is None:
        raise ValueError(f"The fork on issue {issue['id']} neither opened cards nor gave a verdict.")
    return tasks, verdict


def preflight(run, cfg, worker, task, *, node):
    root = runs.run_dir(run)
    sid = task["assignee"]
    profile = os.path.join(cfg["profiles_root"], sid)
    if not os.path.isdir(profile):
        raise ValueError(f"Sister profile not found: {sid}")
    session_dir = runs.session_dir(run, "preflight", node["id"], task["local_id"])
    prompt = (PREFLIGHT_CONTRACT + '\n# Original question\n' + run["question"]
              + '\n# Your research assignment\n' + _catalog_text(task)
              + f"""
# Run workspace
{root}
""")
    obj, raw, err = worker.run_llm_json(
        profile, prompt, cfg["provider"], cfg["default_model"], cwd=root,
        tools=[], timeout=runs.call_timeout(
            cfg, max(300, int(cfg.get("judge_timeout", 600)))),
        usage_db=cfg.get("db"), usage_task_id=run["id"], usage_generation=1,
        usage_token_cap=cfg.get("token_cap"), session_dir=session_dir,
        thinking="medium",
    )
    if err:
        raise RuntimeError(f"Sister {sid} preflight failed: {err}")
    if not isinstance(obj, dict) or len(str(obj.get("preflight_markdown") or "").strip()) < 20:
        raise ValueError(f"Sister {sid} returned an invalid preflight plan.")
    return obj, raw, find_most_recent_session(session_dir)


def task_body(task, preflight_path, evidence=""):
    ledger = f"\n## evidence so far\nWhat earlier cards established (read the sources; do not repeat them):\n{evidence}\n" if evidence else ""
    return f"""## research question
{task['question']}

## rationale
{task['rationale']}

## method and source strategy
Method: {task.get('method') or 'See the preflight plan.'}
Source strategy: {task.get('source_strategy') or 'See the preflight plan.'}
Potential falsifiers: {task.get('falsifiers') or 'Identify evidence that could overturn the working premise.'}

## preflight plan
Read `{preflight_path}` first and work from that plan. Change course when a key premise fails, and record why.
{ledger}
## deliverable
{task['deliverable']}

## boundaries
Do not treat authority, mainstream opinion, contrarian opinion, or the task's own premise as evidence.
If material is unavailable, state the limit that creates instead of claiming proof.

## acceptance criteria
- Register at least one Markdown artifact in `report.json`.
- Back every empirical claim with a traceable source and an exact quotation or precise location.
- Separate facts, inferences, interpretations, and normative judgements.
- Record counterevidence, competing explanations, and unresolved questions.
- Include a `findings` array in `report.json`. Each item uses:
  `{{"text":"self-contained claim","claim_type":"fact|inference|interpretation|normative",`
  `"source_file":"registered artifact path","quote":"exact text present in that artifact"}}`
- You write the findings yourself. The system only checks that the path is a registered artifact and the quote appears in it verbatim; it does not judge credibility.
"""

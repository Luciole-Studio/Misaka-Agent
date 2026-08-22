"""LLM-authored research plans with a deliberately small machine-checked envelope.

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
from misaka.skills import layers as skill_layers
from misaka.research import runs


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
catalog is a menu, not a requirement: combine, reject, or add methods as the problem demands. Only after that
divide the work into tasks and choose Sisters by their profiles and skills.

Return exactly one JSON object of this shape. `plan_markdown` and `extensions` are free-form:
{
  "status": "ready|clarify|probe",
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
  "synthesis_team": [{"assignee":"a Sister id from the roster", "lens":"The independent perspective this synthesis takes"}],
  "extensions": {}
}

Rules:
- Never force a question into PICO, a causal-variable model, or any other single-discipline template.
- Use status=clarify only when research genuinely cannot continue without a human choice. Uncertainty that can be researched belongs in a probe or a task.
- Tasks must be substantive research assignments written for this question, not mechanical templates.
- Choose Sisters by their capability profiles; do not default to the first roster entry.
- Provide at least two independent synthesis perspectives. Disagreements are never settled by vote.
- Output JSON only.
"""

METHOD_REVIEW_CONTRACT = """You are an independent methodology reviewer. Review the research plan only; do not answer the original question.
Check whether the plan closes the question too early; conflates facts, causes, interpretations, consequences,
or normative judgements; pairs methods with objects they do not fit; or leaves out sources, actors, processes,
interactions, time horizons, prior knowledge, impacts, or counterevidence that matter. Flag any authority, mainstream view,
contrarian view, or single theory that the plan treats as beyond question. Confirm that every task can produce auditable material.

Return JSON only:
{"approved": true|false,
 "review_markdown":"The complete review",
 "material_omissions":["Only omissions that could change the result"],
 "required_revisions":["A change the plan must make"],
 "method_probes":["A methodological uncertainty worth investigating"]}
Prefer a structure that fits the problem over a fixed checklist. Do not pad the review with low-value items to look thorough.
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
    out, seen = [], set()
    for directory in skill_layers.skills_stack(profile_dir, cwd=cwd):
        path = os.path.join(directory, "SKILL.md")
        if not os.path.isfile(path):
            continue
        name = os.path.basename(os.path.realpath(directory))
        if name in seen:
            continue
        seen.add(name)
        out.append({"name": name, "description": _frontmatter_description(path), "path": path})
    return out


def _catalog_text(items):
    return json.dumps(items, ensure_ascii=False, indent=2)


def _call(worker, cfg, prompt, *, cwd, session_dir, continue_session=False,
          profile="last_order", tools=("read",), raw=False, task_id=None,
          timeout=None, thinking="high", model=None):
    kwargs = dict(
        cwd=cwd, tools=list(tools),
        timeout=runs.call_timeout(
            cfg, timeout or max(600, int(cfg.get("judge_timeout", 600)))),
        bare=True, soul=False,
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
        cwd=workspace, session_dir=None, tools=(),
        timeout=120, thinking="low", model=cfg["default_model"],
    )
    if err:
        raise RuntimeError(f"Last Order failed to draft the project brief: {err}")
    if not isinstance(obj, dict):
        raise ValueError("Last Order's project brief output is not an object.")
    markdown = str(obj.get("project_markdown") or "").strip()
    if len(markdown) < 40:
        raise ValueError("Last Order did not generate a complete PROJECT.md.")
    Path(path).write_text(markdown + "\n", encoding="utf-8")
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


def validate_plan(obj, roster):
    if not isinstance(obj, dict):
        raise ValueError("Last Order planning output is not an object.")
    status = obj.get("status")
    if status not in {"ready", "clarify", "probe"}:
        raise ValueError("Last Order returned an invalid planning status.")
    plan_markdown = obj.get("plan_markdown")
    if not isinstance(plan_markdown, str) or len(plan_markdown.strip()) < 40:
        raise ValueError("Last Order returned an incomplete plan_markdown value.")
    roster_ids = {r["id"] if isinstance(r, dict) else str(r) for r in roster}
    tasks = [_validate_task(raw, roster_ids, i) for i, raw in enumerate(obj.get("tasks") or [], 1)]
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
    if status == "ready" and not tasks:
        raise ValueError("A ready research plan must contain at least one task.")
    team = []
    team_ids = set()
    for member in obj.get("synthesis_team") or []:
        if not isinstance(member, dict) or member.get("assignee") not in roster_ids:
            continue
        if member["assignee"] in team_ids:
            continue
        team_ids.add(member["assignee"])
        team.append({"assignee": member["assignee"], "lens": str(member.get("lens") or 'Independent synthesis')})
    if status == "ready" and len(team) < 2:
        raise ValueError(
            "Last Order must select at least two Sisters for independent synthesis perspectives."
        )
    return {
        **obj, "status": status, "plan_markdown": plan_markdown.strip(), "tasks": tasks,
        "synthesis_team": team,
        "clarifying_questions": [str(x) for x in obj.get("clarifying_questions") or []],
        "methods": obj.get("methods") if isinstance(obj.get("methods"), list) else [],
        "extensions": obj.get("extensions") if isinstance(obj.get("extensions"), dict) else {},
    }


def plan_root(con, run, cfg, worker, *, revision=None):
    root = runs.run_dir(run)
    session_dir = runs.session_dir(run, "root-lo")
    roster = sister_catalog(cfg.get("profiles_root"))
    if not roster:
        raise RuntimeError("The Sister roster is empty; research tasks cannot be assigned.")
    methods = method_catalog(os.path.join(cfg["roles_root"], "last_order"), root)
    prompt = (
        ROOT_CONTRACT
        + f"""
# Original question
{run['question']}
"""
        + f"""
# Project / PageIndex workspace index (read before planning)
{root}/workspace-index.md
"""
        + f"""
# Sister capability profiles
{_catalog_text(roster)}
"""
        + f"""
# Method and tool skills available
{_catalog_text(methods)}
"""
    )
    if revision:
        prompt += ('\n# Previous version of the plan\n' + _catalog_text(revision["plan"])
                   + '\n# Methodology review findings\n' + _catalog_text(revision["review"])
                   + "\nRevise the plan to address the review; do not defend the previous version.\n")
    obj, raw, err = _call(
        worker, cfg, prompt, cwd=root, session_dir=session_dir,
        continue_session=bool(revision) or bool(find_most_recent_session(session_dir)),
        task_id=run["id"],
    )
    if err:
        raise RuntimeError(f"Last Order planning failed: {err}")
    plan = validate_plan(obj, roster)
    session_file = find_most_recent_session(session_dir)
    return plan, raw, session_file


def plan_branch(con, run, cfg, worker, branch, context_path, *, revision=None):
    """Open or continue a dedicated Last Order session for one material issue confirmed by the critic."""
    root = runs.run_dir(run)
    session_dir = runs.session_dir(run, "branches", branch["id"])
    roster = sister_catalog(cfg.get("profiles_root"))
    methods = method_catalog(os.path.join(cfg["roles_root"], "last_order"), root)
    prompt = (
        ROOT_CONTRACT
        + "\nThis is a targeted extension branch. Address the issue that triggered it without replanning the whole project."
        + f"""
# Issue that triggered this branch
{branch['trigger_text']}
"""
        + f"""
# Context packet (ancestor sessions and artifact map; read it first)
{context_path}
"""
        + f"""
# Sister capability profiles
{_catalog_text(roster)}
"""
        + f"""
# Method and tool skills available
{_catalog_text(methods)}
"""
    )
    if revision:
        prompt += ('\n# Previous branch plan\n' + _catalog_text(revision["plan"])
                   + '\n# Methodology review findings\n' + _catalog_text(revision["review"])
                   + '\nRevise the branch plan to address the review; do not defend the previous version.\n')
    obj, raw, err = _call(
        worker, cfg, prompt, cwd=root, session_dir=session_dir,
        continue_session=bool(revision) or bool(find_most_recent_session(session_dir)),
        task_id=run["id"],
    )
    if err:
        raise RuntimeError(f"Last Order planning failed for branch {branch['id']}: {err}")
    plan = validate_plan(obj, roster)
    session_file = find_most_recent_session(session_dir)
    return plan, raw, session_file


def review_methods(run, cfg, worker, plan):
    root = runs.run_dir(run)
    session_dir = runs.session_dir(run, "method-review")
    prompt = (METHOD_REVIEW_CONTRACT + f"""
# Original question
{run['question']}
"""
              + '\n# Plan under review\n' + _catalog_text(plan))
    obj, raw, err = _call(
        worker, cfg, prompt, cwd=root, session_dir=session_dir,
        profile="redteam", tools=(), task_id=run["id"],
    )
    if err:
        raise RuntimeError(f"Method review failed: {err}")
    if not isinstance(obj, dict) or not isinstance(obj.get("approved"), bool):
        raise ValueError("The methodology reviewer returned an invalid response.")
    obj["review_markdown"] = str(obj.get("review_markdown") or "").strip()
    for key in ("material_omissions", "required_revisions", "method_probes"):
        obj[key] = [str(x) for x in obj.get(key) or []]
    return obj, raw


def preflight(run, cfg, worker, task, *, branch_id=None):
    root = runs.run_dir(run)
    sid = task["assignee"]
    profile = os.path.join(cfg["profiles_root"], sid)
    if not os.path.isdir(profile):
        raise ValueError(f"Sister profile not found: {sid}")
    label = branch_id or "root"
    session_dir = runs.session_dir(run, "preflight", label, task["local_id"])
    prompt = (PREFLIGHT_CONTRACT + '\n# Original question\n' + run["question"]
              + '\n# Your research assignment\n' + _catalog_text(task)
              + f"""
# Run workspace
{root}
""")
    obj, raw, err = worker.run_llm_json(
        profile, prompt, cfg["provider"], cfg["default_model"], cwd=root,
        tools=[], timeout=runs.call_timeout(
            cfg, max(300, int(cfg.get("judge_timeout", 600)))), bare=True,
        usage_db=cfg.get("db"), usage_task_id=run["id"], usage_generation=1,
        usage_token_cap=cfg.get("token_cap"), session_dir=session_dir,
        thinking="medium",
    )
    if err:
        raise RuntimeError(f"Sister {sid} preflight failed: {err}")
    if not isinstance(obj, dict) or len(str(obj.get("preflight_markdown") or "").strip()) < 20:
        raise ValueError(f"Sister {sid} returned an invalid preflight plan.")
    return obj, raw, find_most_recent_session(session_dir)


def task_body(task, preflight_path):
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

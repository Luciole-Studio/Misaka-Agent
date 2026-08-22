"""Persistent, depth-bounded Research Workflow driven by plans that Last Order writes.

The state machine enforces phase order and depth. The models own the semantics:
question framing, methods, Sister selection, task count, issue priority, source
appraisal, and whether a disagreement can be resolved.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from misaka.platform import budget, tasks as task_store
from misaka.research import context as context_packet
from misaka.research import critic, ledger, planner, report, runs
from misaka import workspace as workspace_index

POLL_SECONDS = 2.0
ACTIVE_TASKS = ("running", "review", "verifying", "finalizing")
TERMINAL_TASKS = ("done", "failed", "stopped")

RESEARCH_DISCIPLINE = """[Research Workflow active]
Last Order is now in Research mode. During planning, do not answer the original question: frame it, design the inquiry,
choose methods, and assign Sisters. Every task starts with a preflight plan. Every research wave gets independent
syntheses and a red-team review. Each material weak point opens a new Last Order branch session for targeted replanning.

Code enforces phase order, depth, persistence, and artifact integrity. The models keep the judgement calls: framing, methods,
Sister selection, source quality, task count, and disagreements. The rule against answering lifts only at final adjudication,
which must keep competing conclusions side by side wherever the evidence cannot decide between them. The usual global budgets and
per-call failure limits still apply.
"""


class NullRunner:
    async def launch_ready(self, **_kwargs):
        raise RuntimeError("No Sister task runtime is available for this research session.")


async def _progress(callback, stage, message, run=None, **details):
    if not callback:
        return
    payload = {"stage": stage, "message": message, **details}
    if run is not None:
        payload["run_id"] = run["id"]
    result = callback(payload)
    if hasattr(result, "__await__"):
        await result



def _json_artifact(con, run_id, kind):
    rows = runs.artifacts(con, run_id, kind=kind)
    if not rows:
        return None
    try:
        return json.loads(Path(rows[-1]["path"]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None



def _save_plan(con, run, plan, review=None, *, branch_id=None):
    prefix = f"branches/{branch_id}/" if branch_id else ""
    suffix = f"-{branch_id}" if branch_id else ""
    json_aid, json_path = runs.write_text(
        con, run["id"], "branch_plan_json" if branch_id else "plan_json",
        f"Research plan{suffix}", prefix + "plan.json",
        json.dumps(plan, ensure_ascii=False, indent=2), branch_id=branch_id,
    )
    md_aid, md_path = runs.write_text(
        con, run["id"], "branch_plan" if branch_id else "plan",
        f"Research plan{suffix}", prefix + "plan.md", plan["plan_markdown"].rstrip() + "\n",
        branch_id=branch_id, metadata={"json_artifact": json_aid},
    )
    if review is not None:
        review_json, _ = runs.write_text(
            con, run["id"], "method_review_json", f"Method review{suffix}",
            prefix + "method-review.json", json.dumps(review, ensure_ascii=False, indent=2),
            branch_id=branch_id,
        )
        review_md = review.get("review_markdown") or '# Method review\n\nNo changes requested.'
        runs.write_text(
            con, run["id"], "method_review", f"Method review{suffix}",
            prefix + "method-review.md", review_md.rstrip() + "\n", branch_id=branch_id,
            metadata={"json_artifact": review_json},
        )
    return {"json": json_aid, "json_path": json_path, "markdown": md_aid, "path": md_path}


def _refresh_workspace_index(con, run):
    tree = workspace_index.outline(
        con, workspace=run["workspace"], run_id=run["id"], research_store=runs)
    runs.write_text(
        con, run["id"], "workspace_index_json", 'Project / PageIndex workspace index',
        "workspace-index.json", json.dumps(tree, ensure_ascii=False, indent=2),
    )
    return runs.write_text(
        con, run["id"], "workspace_index", 'Project / PageIndex workspace index',
        "workspace-index.md", '# Project / PageIndex workspace index\n\n```text\n'
        + workspace_index.render(tree) + "\n```\n",
    )


async def _plan_and_review(con, run, cfg, worker, *, branch=None, context_path=None,
                           progress=None):
    label = f"branch {branch['id']}" if branch else "the root question"
    await _progress(progress, "planning", f"Last Order is planning {label}.", run)
    if branch:
        call = lambda revision=None: planner.plan_branch(
            con, run, cfg, worker, branch, context_path, revision=revision)
    else:
        call = lambda revision=None: planner.plan_root(con, run, cfg, worker, revision=revision)
    plan, raw, session_file = await asyncio.to_thread(call)
    _save_plan(con, run, plan, branch_id=branch["id"] if branch else None)
    if plan["status"] == "clarify":
        return plan, None, session_file
    await _progress(
        progress, "method_review",
        f"Planning produced {len(plan['tasks'])} research tasks for {label}; "
        "an independent reviewer is checking the methodology.",
        run,
        tasks=[{"title": task["title"], "assignee": task["assignee"]}
               for task in plan["tasks"]],
    )
    review, _review_raw = await asyncio.to_thread(planner.review_methods, run, cfg, worker, plan)
    if not review["approved"] and review["required_revisions"]:
        plan, raw, session_file = await asyncio.to_thread(
            call, {"plan": plan, "review": review})
        # One re-review catches revisions that were ignored, without turning into an endless veto loop.
        review, _review_raw = await asyncio.to_thread(planner.review_methods, run, cfg, worker, plan)
    _save_plan(con, run, plan, review, branch_id=branch["id"] if branch else None)
    await _progress(progress, "plan_ready", f"Planning and methodology review for {label} are complete.", run)
    return plan, review, session_file



async def _submit_tasks(con, run, cfg, worker, plan, *, branch_id=None, wave=0,
                        progress=None):
    candidates = plan["tasks"]
    created, created_specs, local_to_task = [], [], {}
    prefix = f"{branch_id}-" if branch_id else "root-"
    for spec in candidates:
        if runs.stop_requested(con, run["id"]):
            break
        await _progress(
            progress, "preflight",
            f"Sister {spec['assignee']} is planning its research approach for {spec['title']!r}.",
            run,
        )
        preflight, _raw, preflight_session = await asyncio.to_thread(
            planner.preflight, run, cfg, worker, spec, branch_id=branch_id)
        rel = f"tasks/{prefix}{spec['local_id']}-preflight.md"
        aid, preflight_path = runs.write_text(
            con, run["id"], "preflight", f"{spec['title']} · preflight", rel,
            preflight["preflight_markdown"].rstrip() + "\n", branch_id=branch_id,
            metadata={"assignee": spec["assignee"], "session_file": preflight_session,
                      "sources": preflight.get("sources") or [], "tools": preflight.get("tools") or []},
        )
        tid = task_store.create_task(
            con, spec["title"], body=planner.task_body(spec, preflight_path),
            assignee=spec["assignee"], priority=spec.get("priority", 0),
            workspace=run["workspace"], timeout_seconds=runs.call_timeout(cfg, 1800),
        )
        dependencies = list(spec.get("dependencies") or [])
        runs.link_task(con, run["id"], tid, kind="research", branch_id=branch_id,
                       wave=wave, preflight_artifact=aid, local_id=spec["local_id"],
                       dependencies=dependencies)
        local_to_task[spec["local_id"]] = tid
        created.append(tid)
        created_specs.append(spec)
    # depends_json keeps Last Order's plan as written; task_links is the executable projection of it.
    for spec in created_specs:
        child_id = local_to_task[spec["local_id"]]
        for dependency in spec.get("dependencies") or []:
            parent_id = local_to_task.get(dependency)
            if parent_id is None:
                raise RuntimeError(f"Research dependency was not created: {dependency}")
            task_store.link_tasks(con, parent_id, child_id)
    return created



def _release_dependencies(con, run_id):
    changed = 0
    for row in runs.tasks(con, run_id):
        if row["status"] != "todo":
            continue
        state, parents = task_store.dependency_state(con, row["id"])
        if state == "failed":
            task_store.mark_stopped(con, row["id"])
            task_store.add_event(con, row["id"], "dependency_failed", {"parents": parents})
            changed += 1
        elif task_store.promote_task(con, row["id"]):
            changed += 1
    return changed


async def _stop_active(runner, task_ids, context):
    stop = getattr(runner, "stop", None)
    if not callable(stop):
        return
    await asyncio.gather(
        *(stop(task_id, confirmed=True, context=context) for task_id in task_ids),
        return_exceptions=True,
    )


async def _drive_tasks(con, cfg, runner, run_id, *, context=None,
                       tool_call_id="research", poll_seconds=POLL_SECONDS,
                       progress=None):
    """Drive only this run's tasks; tasks waiting on dependencies stay in ``todo``."""
    last_snapshot = None
    while True:
        run = runs.get(con, run_id)
        linked = runs.tasks(con, run_id)
        snapshot = tuple((row["id"], row["status"]) for row in linked)
        if snapshot != last_snapshot:
            counts = {}
            for _task_id, status in snapshot:
                counts[status] = counts.get(status, 0) + 1
            text = ','.join(f"{status} {count}" for status, count in sorted(counts.items()))
            await _progress(progress, "tasks", f"Task status: {text or 'no tasks'}.", run,
                            counts=counts)
            last_snapshot = snapshot
        active = [row["id"] for row in linked if row["status"] in ACTIVE_TASKS]
        if runs.stop_requested(con, run_id):
            await _stop_active(runner, active, context)
            for row in linked:
                if row["status"] in {"ready", "todo"}:
                    task_store.mark_stopped(con, row["id"])
            return "stopped"
        if budget.status(con, cfg.get("token_cap"))["mode"] != "normal":
            await _stop_active(runner, active, context)
            for row in linked:
                if row["status"] in {"ready", "todo"}:
                    task_store.mark_stopped(con, row["id"])
            return "budget"
        _release_dependencies(con, run_id)
        linked = runs.tasks(con, run_id)
        free = max(0, max(1, int(cfg.get("research_parallel", 4)))
                   - sum(1 for row in linked if row["status"] in ACTIVE_TASKS))
        ready = [row["id"] for row in linked if row["status"] == "ready"][:free]
        active = [row["id"] for row in linked if row["status"] in ACTIVE_TASKS]
        if ready or active:
            await runner.launch_ready(context=context, tool_call_id=tool_call_id,
                                      task_ids=[*ready, *active])
            await asyncio.sleep(poll_seconds)
            continue
        waiting = [row for row in linked if row["status"] == "todo"]
        if waiting:
            _release_dependencies(con, run_id)
            if any(row["status"] == "todo" for row in runs.tasks(con, run_id)):
                raise RuntimeError("Research task dependencies cannot advance; the graph may contain a cycle.")
            continue
        return "done"



def _task_link(con, task_id):
    return con.execute("SELECT * FROM research_run_tasks WHERE task_id=?", (task_id,)).fetchone()



def _copy_task_artifacts(con, run, task):
    link = _task_link(con, task["id"])
    if not link or not task["workspace"]:
        return []
    try:
        data = json.loads(
            Path(task_store.task_state_dir(task["id"]), "report.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return []
    out = []
    for rel in data.get("artifacts") or []:
        source = Path(task["workspace"], str(rel)).resolve()
        try:
            source.relative_to(Path(task["workspace"]).resolve())
        except ValueError:
            continue
        if not source.is_file():
            continue
        try:
            text = source.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # binary originals are handled by corpus/PageIndex ingestion
        kind = "synthesis" if link["kind"] == "synthesis" else "task_output"
        if kind == "synthesis":
            dest = f"syntheses/{link['wave']}-{task['id']}-{source.name}"
        else:
            dest = f"tasks/{task['id']}/{source.name}"
        aid, path = runs.write_text(
            con, run["id"], kind, f"[{task['id']}] {source.name}", dest, text,
            branch_id=link["branch_id"], task_id=task["id"],
            metadata={"source_workspace": str(source), "source_file": str(rel),
                      "wave": link["wave"]},
        )
        out.append({"id": aid, "path": path, "source_file": str(rel)})
    return out



def settle_done_tasks(con, *, run_id):
    """Copy accepted artifacts and ingest the Sisters' findings, once per task generation."""
    run = runs.get(con, run_id)
    stats = {"tasks": 0, "findings": 0, "claims": 0, "dropped": []}
    for task in runs.tasks(con, run_id):
        if task["status"] != "done":
            continue
        generation = int(task["generation"])
        if task_store.latest_payload(con, task["id"], "research_v2_settled", generation=generation):
            continue
        _copy_task_artifacts(con, run, task)
        if task["research_kind"] == "synthesis":
            task_store.add_event(con, task["id"], "research_v2_settled",
                                 {"kind": "synthesis"}, generation=generation)
            stats["tasks"] += 1
            continue
        try:
            payload = json.loads(
                Path(task_store.task_state_dir(task["id"]), "report.json").read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, ValueError):
            payload = {}
        result = ledger.ingest_report(con, run, task, payload)
        task_store.add_event(
            con, task["id"], "research_v2_settled",
            result, generation=generation,
        )
        stats["tasks"] += 1
        stats["findings"] += result["findings"]
        stats["claims"] += result["claims"]
        stats["dropped"].extend(f"{task['id']}:{item}" for item in result["dropped"])
    return stats


async def _create_syntheses(con, run, cfg, worker, plan, *, wave, progress=None):
    existing = [row for row in runs.tasks(con, run["id"], kind="synthesis") if row["wave"] == wave]
    if existing:
        return [row["id"] for row in existing]
    team, seen = [], set()
    if wave:
        for branch in runs.branches(con, run["id"]):
            if int(branch["depth"]) != int(wave):
                continue
            rows = runs.artifacts(con, run["id"], branch_id=branch["id"], kind="branch_plan_json")
            if not rows:
                continue
            try:
                branch_plan = json.loads(Path(rows[-1]["path"]).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for member in branch_plan.get("synthesis_team") or []:
                key = member.get("assignee")
                if key not in seen:
                    seen.add(key)
                    team.append(member)
    for member in plan.get("synthesis_team") or []:
        key = member.get("assignee")
        if key not in seen:
            seen.add(key)
            team.append(member)
    if len(team) < 2:
        raise RuntimeError("Independent synthesis requires at least two Sister perspectives.")
    source_rows = [row for row in runs.tasks(con, run["id"], kind="research") if row["status"] == "done"]
    created = []
    for index, member in enumerate(team, 1):
        await _progress(
            progress, "synthesis_preflight",
            f"Sister {member['assignee']} is preparing the wave {wave} synthesis through the lens: {member['lens']}.",
            run,
        )
        spec = {
            "local_id": f"synthesis-{wave}-{index}",
            "title": f"Synthesis · wave {wave} · {member['lens']}",
            "question": run["question"], "rationale": "Independent synthesis of all research output accepted so far",
            "method": member["lens"],
            "source_strategy": "Read the accepted task artifacts and check every claim against its quotation.",
            "falsifiers": "Keep the evidence that conflicts with this synthesis lens.",
            "deliverable": "synthesis.md", "dependencies": [],
            "capabilities": ["synthesis", "evidence review"],
            "assignee": member["assignee"],
            "assignee_reason": f"Last Order chose this synthesis lens: {member['lens']}",
            "priority": 0,
        }
        preflight, _raw, session_file = await asyncio.to_thread(
            planner.preflight, run, cfg, worker, spec)
        aid, preflight_path = runs.write_text(
            con, run["id"], "preflight", f"{spec['title']} · preflight",
            f"tasks/{spec['local_id']}-preflight.md", preflight["preflight_markdown"].rstrip() + "\n",
            metadata={"assignee": spec["assignee"], "session_file": session_file},
        )
        tid = report.create(
            con, source_rows, f"{runs.project_name(run)} · wave {wave} synthesis",
            assignee=member["assignee"], lens=member["lens"],
            workspace=run["workspace"], run_id=run["id"], wave=wave,
            preflight_path=preflight_path, timeout_seconds=runs.call_timeout(cfg, 1200),
        )
        runs.link_task(con, run["id"], tid, kind="synthesis", wave=wave,
                       preflight_artifact=aid, local_id=spec["local_id"])
        created.append(tid)
    return created



def _can_expand(run):
    return int(run["wave"]) < runs.limits(run)["max_depth"]


async def _expand_issues(con, run, cfg, worker, *, next_wave, progress=None):
    limits = runs.limits(run)
    if next_wave > limits["max_depth"]:
        for issue in runs.open_issues(con, run["id"]):
            runs.set_issue(con, issue["id"], "parked")
        return [], []
    created, questions = [], []
    for issue in runs.open_issues(con, run["id"]):
        branch = (con.execute(
            "SELECT * FROM research_branches WHERE id=? AND run_id=?",
            (issue["child_branch_id"], run["id"]),
        ).fetchone() if issue["child_branch_id"] else None)
        if branch:
            context_row = runs.artifact(con, branch["context_artifact"])
            if not context_row:
                raise RuntimeError(
                    f"Branch {branch['id']} is awaiting clarification but has no context artifact."
                )
            context_path = context_row["path"]
        else:
            parent = (con.execute(
                "SELECT * FROM research_branches WHERE id=?", (issue["branch_id"],)
            ).fetchone() if issue["branch_id"] else None)
            branch = runs.create_branch(
                con, run["id"], trigger=issue["question"], parent_id=issue["branch_id"],
                depth=next_wave,
            )
            _refresh_workspace_index(con, run)
            _aid, context_path, _packet = context_packet.create(
                con, run, issue=issue, branch=branch, parent_branch=parent)
        plan, review, session_file = await _plan_and_review(
            con, run, cfg, worker, branch=branch, context_path=context_path,
            progress=progress)
        runs.set_branch(con, branch["id"], status="planned", session_file=session_file)
        if plan["status"] == "clarify":
            runs.set_branch(con, branch["id"], status="waiting_input")
            runs.set_issue(con, issue["id"], "open", branch["id"])
            asked = plan["clarifying_questions"] or [
                "What decision or missing information is needed before this branch can proceed?"
            ]
            questions.extend(f"[{branch['id']}] {question}" for question in asked)
            continue
        task_ids = await _submit_tasks(
            con, run, cfg, worker, plan, branch_id=branch["id"], wave=next_wave,
            progress=progress)
        if not task_ids:
            runs.set_branch(con, branch["id"], status="empty")
            runs.set_issue(con, issue["id"], "parked", branch["id"])
            continue
        runs.set_branch(con, branch["id"], status="executing")
        runs.set_issue(con, issue["id"], "selected", branch["id"])
        created.extend(task_ids)
    return created, questions



def _finish_branches(con, run_id, wave):
    for branch in runs.branches(con, run_id):
        if branch["depth"] != wave or branch["status"] != "executing":
            continue
        linked = runs.tasks(con, run_id, branch_id=branch["id"])
        if linked and all(row["status"] in TERMINAL_TASKS for row in linked):
            status = "done" if all(row["status"] == "done" for row in linked) else "failed"
            runs.set_branch(con, branch["id"], status=status)
            con.execute(
                "UPDATE research_issues SET status=? WHERE child_branch_id=?",
                ("resolved" if status == "done" else "parked", branch["id"]),
            )


def _persisted_disagreements(con, run_id):
    out = []
    for artifact in runs.artifacts(con, run_id, kind="critique"):
        if not artifact["path"].endswith(".json"):
            continue
        try:
            data = json.loads(Path(artifact["path"]).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.extend(item for item in data.get("disagreements") or [] if isinstance(item, dict))
    return out


def _partial_report(con, run, reason):
    artifacts = runs.artifacts(con, run["id"])
    lines = ['# Incomplete research run', "", f"- Run: `{run['id']}`",
             f"- Project: `{runs.project_name(run)}` (`{run['workspace']}`)",
             f"- Reason: {reason}", "", '## Original question',
             run["question"], "", '## Saved artifacts']
    lines.extend(f"- [{row['kind']}] {row['title']} — `{row['path']}`" for row in artifacts)
    lines += ["", "This document records where the run stopped. It is not a final report, and the research is incomplete.", ""]
    return runs.write_text(con, run["id"], "partial", 'Incomplete research run',
                           "partial.md", "\n".join(lines))


def _partial_result(con, run, reason):
    aid, path = _partial_report(con, run, reason)
    runs.set_state(con, run["id"], phase=run["phase"], status="stopped",
                   final_artifact=aid)
    return {"reason": "stopped", "final": {"artifact": aid, "path": path,
            "content": Path(path).read_text(encoding="utf-8")},
            "run": runs.summary(con, run["id"])}


def _copy_completed_without_models(con, run):
    for task in runs.tasks(con, run["id"]):
        if task["status"] == "done":
            _copy_task_artifacts(con, run, task)


async def run(con, cfg, runner, worker, *, run_id, context=None,
              tool_call_id="research", poll_seconds=POLL_SECONDS, progress=None):
    """Advance one persisted research run until it finishes, stops, or needs user input."""
    runs.init(con)
    run = runs.get(con, run_id)
    if not run:
        raise ValueError(f"Research run not found: {run_id}")
    if not os.path.isdir(run["workspace"]):
        raise RuntimeError(f"The research run's project folder no longer exists: {run['workspace']}")
    runs.ensure_layout(run)
    cfg = dict(cfg)
    disagreements = _persisted_disagreements(con, run_id)
    latest_syntheses = []
    try:
        if runs.stop_requested(con, run_id):
            return _partial_result(con, run, "The user requested a stop.")
        if budget.status(con, cfg.get("token_cap"))["mode"] != "normal":
            return _partial_result(con, run, "The shared token budget limit was reached.")
        _refresh_workspace_index(con, run)
        # Root planning is a hard gate; its raw output never reaches the user's answer channel.
        if run["phase"] in {"created", "planning"}:
            runs.set_state(con, run_id, phase="planning", status="active")
            run = runs.get(con, run_id)
            plan, review, session_file = await _plan_and_review(
                con, run, cfg, worker, progress=progress)
            runs.set_state(con, run_id, root_session=session_file)
            if plan["status"] == "clarify":
                runs.set_state(con, run_id, phase="waiting_input", status="waiting_input")
                return {"reason": "waiting_input", "questions": plan["clarifying_questions"],
                        "run": runs.summary(con, run_id)}
            await _submit_tasks(con, run, cfg, worker, plan, wave=0, progress=progress)
            runs.set_state(con, run_id, phase="executing", wave=0)
        else:
            plan = _json_artifact(con, run_id, "plan_json")
            if not plan:
                raise RuntimeError("The persisted research run has no plan.json and cannot resume.")

        while True:
            run = runs.get(con, run_id)
            wave = int(run["wave"])
            phase = run["phase"]
            if phase == "executing":
                outcome = await _drive_tasks(
                    con, cfg, runner, run_id, context=context,
                    tool_call_id=tool_call_id, poll_seconds=poll_seconds,
                    progress=progress)
                if outcome in {"stopped", "budget"}:
                    _copy_completed_without_models(con, run)
                    runs.set_state(con, run_id, phase="finalizing",
                                   status="stopping" if outcome == "stopped" else "active")
                else:
                    settle_done_tasks(con, run_id=run_id)
                    _finish_branches(con, run_id, wave)
                    runs.set_state(con, run_id, phase="synthesizing")
                continue

            if phase == "synthesizing":
                await _progress(
                    progress, "synthesizing",
                    f"Wave {wave}: independent synthesis from multiple perspectives.", run)
                await _create_syntheses(
                    con, run, cfg, worker, plan, wave=wave, progress=progress)
                outcome = await _drive_tasks(
                    con, cfg, runner, run_id, context=context,
                    tool_call_id=tool_call_id, poll_seconds=poll_seconds,
                    progress=progress)
                if outcome in {"stopped", "budget"}:
                    _copy_completed_without_models(con, run)
                    runs.set_state(con, run_id, phase="finalizing",
                                   status="stopping" if outcome == "stopped" else "active")
                    continue
                settle_done_tasks(con, run_id=run_id)
                latest_syntheses = [row for row in runs.tasks(con, run_id, kind="synthesis")
                                     if row["wave"] == wave and row["status"] == "done"]
                if len(latest_syntheses) < 2:
                    raise RuntimeError("Fewer than two synthesis reports completed successfully.")
                runs.set_state(con, run_id, phase="critiquing")
                continue

            if phase == "critiquing":
                await _progress(
                    progress, "critiquing",
                    f"Wave {wave}: syntheses complete; the red team is checking for factual and logical gaps.", run)
                latest_syntheses = [row for row in runs.tasks(con, run_id, kind="synthesis")
                                     if row["wave"] == wave and row["status"] == "done"]
                review = await asyncio.to_thread(
                    critic.critique_run, con, run, cfg, worker,
                    wave=wave, synthesis_tasks=latest_syntheses)
                disagreements.extend(review["disagreements"])
                for issue in review["issues"]:
                    runs.add_issue(con, run_id, wave=wave, **issue)
                open_now = runs.open_issues(con, run_id)
                hard_stop = (runs.stop_requested(con, run_id)
                             or budget.status(con, cfg.get("token_cap"))["mode"] != "normal")
                if not open_now or hard_stop or not _can_expand(run):
                    for issue in open_now:
                        runs.set_issue(con, issue["id"], "parked")
                    runs.set_state(con, run_id, phase="finalizing")
                else:
                    runs.set_state(con, run_id, phase="branch_planning")
                continue

            if phase == "branch_planning":
                next_wave = wave + 1
                await _progress(
                    progress, "branch_planning",
                    f"The red team left {len(runs.open_issues(con, run_id))} open issues; "
                    f"Last Order is planning expansion wave {next_wave}.",
                    run,
                )
                created, questions = await _expand_issues(
                    con, run, cfg, worker, next_wave=next_wave, progress=progress)
                if questions:
                    runs.set_state(con, run_id, phase="branch_waiting_input",
                                   status="waiting_input")
                    return {"reason": "waiting_input", "questions": questions,
                            "run": runs.summary(con, run_id)}
                if not created:
                    runs.set_state(con, run_id, phase="finalizing")
                else:
                    runs.set_state(con, run_id, phase="executing", wave=next_wave)
                continue

            if phase == "finalizing":
                await _progress(
                    progress, "finalizing",
                    "Research branches are closed; Last Order is adjudicating and auditing the final report.", run)
                stop_reason = None
                if runs.stop_requested(con, run_id):
                    stop_reason = "The user requested a stop."
                elif budget.status(con, cfg.get("token_cap"))["mode"] != "normal":
                    stop_reason = "The shared token budget limit was reached."
                if stop_reason:
                    return _partial_result(con, run, stop_reason)
                if not latest_syntheses:
                    all_synth = [row for row in runs.tasks(con, run_id, kind="synthesis")
                                 if row["status"] == "done"]
                    last_wave = max((int(row["wave"]) for row in all_synth), default=-1)
                    latest_syntheses = [row for row in all_synth if int(row["wave"]) == last_wave]
                if len(latest_syntheses) < 2:
                    raise RuntimeError("Final adjudication requires at least two independent synthesis reports.")
                disagreements = _persisted_disagreements(con, run_id)
                result = await asyncio.to_thread(
                    report.finalize, con, run, cfg, worker,
                    synthesis_tasks=latest_syntheses, disagreements=disagreements)
                final_status = "stopped" if runs.stop_requested(con, run_id) else "done"
                runs.set_state(con, run_id, phase="done", status=final_status,
                               final_artifact=result["artifact"])
                _refresh_workspace_index(con, runs.get(con, run_id))
                return {"reason": final_status, "final": result,
                        "run": runs.summary(con, run_id)}

            if phase == "waiting_input":
                return {"reason": "waiting_input", "run": runs.summary(con, run_id)}
            if phase == "branch_waiting_input":
                return {"reason": "waiting_input", "run": runs.summary(con, run_id)}
            if phase == "done":
                final = runs.artifact(con, run["final_artifact"]) if run["final_artifact"] else None
                return {"reason": run["status"], "final": {"path": final["path"] if final else None},
                        "run": runs.summary(con, run_id)}
            raise RuntimeError(f"Unknown research phase: {phase}")
    except Exception as error:
        current = runs.get(con, run_id)
        if (runs.stop_requested(con, run_id)
                or budget.status(con, cfg.get("token_cap"))["mode"] != "normal"):
            return _partial_result(
                con, current, "The run was stopped or reached the shared token budget during a model call."
            )
        runs.set_state(con, run_id, status="failed", error=f"{type(error).__name__}: {error}")
        raise

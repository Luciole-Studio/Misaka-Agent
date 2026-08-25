"""Persistent, depth-bounded Research Workflow: breadth-first over a tree of conclusions.

A node is one conclusion. Its routine (``_expand``) is the same for the root and for every
node opened under it: Last Order plans, Sisters research, Last Order synthesizes, the
red-team Sister she named finds issues, one probe card tests each issue, Last Order triages,
and only an issue that undermines the conclusion opens a child node. The frontier is FIFO by
(depth, created_at); the depth limit bounds how many times a conclusion may be overturned in
a chain. Code enforces the order, the depth, persistence, and artifact integrity; the models
keep every judgement call.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from misaka.platform import budget, cards as card_files, repo, tasks as task_store
from misaka.research import context as context_packet
from misaka.research import ledger, planner, report, runs
from misaka import workspace as workspace_index

POLL_SECONDS = 2.0
ACTIVE_TASKS = ("running", "review", "verifying", "finalizing")
TERMINAL_TASKS = ("done", "failed", "stopped")

RESEARCH_DISCIPLINE = """[Research Workflow active]
Last Order is now in Research mode. Each node of the research tree runs the same routine: Last Order plans and assigns
Sisters (every task starts with a preflight plan), Last Order writes the node's conclusion in one pass, the red-team Sister she
named finds issues in it, one probe card tests each issue, and Last Order judges what every probe showed. Only an issue that
undermines the conclusion opens a child node; the tree is expanded breadth-first up to the chosen depth.

Code enforces phase order, depth, persistence, and artifact integrity. The models keep the judgement calls: framing, methods,
Sister selection, source quality, task count, and what shakes a conclusion. The rule against answering lifts only at final
adjudication, which must keep competing conclusions side by side wherever the evidence cannot decide between them.
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


def _bid(node):
    """Artifact scope: root artifacts carry no branch id (they sit at the top of the run directory)."""
    return node["id"] if node["parent_id"] else None


def _scope(node):
    return {"branch_id": node["id"]} if node["parent_id"] else {"root_only": True}


def _write(con, run, node, kind, title, name, content, **metadata):
    return runs.write_text(con, run["id"], kind, title, runs.node_prefix(node) + name, content,
                           branch_id=_bid(node), metadata=metadata or None)


def _artifact_path(con, run, node, kind):
    rows = runs.artifacts(con, run["id"], kind=kind, **_scope(node))
    return rows[-1]["path"] if rows else None


def _json_artifact(con, run, node, kind):
    path = _artifact_path(con, run, node, kind)
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")) if path else None
    except (OSError, ValueError):
        return None


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


def _label(node):
    return "the root question" if node["parent_id"] is None else f"node {node['id']}"


async def _submit_tasks(con, run, cfg, worker, node, specs, *, kind, progress=None):
    """Preflight every spec, then open its card on the node's line. Returns local_id -> task id."""
    root = runs.node_root(run, node)
    local_to_task = {}
    for spec in specs:
        if runs.stop_requested(con, run["id"]):
            break
        await _progress(progress, "preflight",
                        f"Sister {spec['assignee']} is planning its approach for {spec['title']!r}.", run)
        preflight, _raw, session = await asyncio.to_thread(
            planner.preflight, run, cfg, worker, spec, node=node)
        aid, path = _write(con, run, node, "preflight", f"{spec['title']} · preflight",
                           f"tasks/{spec['local_id']}-preflight.md",
                           preflight["preflight_markdown"].rstrip() + "\n",
                           assignee=spec["assignee"], session_file=session)
        tid = card_files.create(con, root, spec["title"], planner.task_body(spec, path),
                                spec["assignee"], priority=spec.get("priority", 0),
                                timeout_seconds=runs.call_timeout(cfg, 1800))
        runs.link_task(con, run["id"], tid, kind=kind, node=node, preflight_artifact=aid,
                       local_id=spec["local_id"], dependencies=spec.get("dependencies") or [])
        local_to_task[spec["local_id"]] = tid
    # depends_json keeps Last Order's plan as written; the cards' frontmatter `needs` is the executable projection of it.
    for spec in specs:
        if spec["local_id"] not in local_to_task:
            continue
        for dependency in spec.get("dependencies") or []:
            if dependency not in local_to_task:
                raise RuntimeError(f"Research dependency was not created: {dependency}")
            task_store.link_tasks(con, local_to_task[dependency], local_to_task[spec["local_id"]])
    return local_to_task


def _release_dependencies(con, run_id):
    for row in runs.tasks(con, run_id):
        if row["status"] != "todo":
            continue
        state, parents = task_store.dependency_state(con, row["id"])
        if state == "failed":
            task_store.mark_stopped(con, row["id"])
            task_store.add_event(con, row["id"], "dependency_failed", {"parents": parents})
        else:
            task_store.promote_task(con, row["id"])


async def _stop_active(runner, task_ids, context):
    stop = getattr(runner, "stop", None)
    if not callable(stop):
        return
    await asyncio.gather(
        *(stop(task_id, confirmed=True, context=context) for task_id in task_ids),
        return_exceptions=True,
    )


async def _drive_tasks(con, cfg, runner, run_id, *, context=None,
                       tool_call_id="research", poll_seconds=POLL_SECONDS, progress=None):
    """Drive this run's open cards until none is left; tasks waiting on dependencies stay in ``todo``."""
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
            await _progress(progress, "tasks", f"Task status: {text or 'no tasks'}.", run, counts=counts)
            last_snapshot = snapshot
        active = [row["id"] for row in linked if row["status"] in ACTIVE_TASKS]
        halt = ("stopped" if runs.stop_requested(con, run_id) else
                "budget" if budget.status(con, cfg.get("token_cap"))["mode"] != "normal" else None)
        if halt:
            await _stop_active(runner, active, context)
            for row in linked:
                if row["status"] in {"ready", "todo"}:
                    task_store.mark_stopped(con, row["id"])
            return halt
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
        if any(row["status"] == "todo" for row in linked):
            _release_dependencies(con, run_id)
            if any(row["status"] == "todo" for row in runs.tasks(con, run_id)):
                raise RuntimeError("Research task dependencies cannot advance; the graph may contain a cycle.")
            continue
        return "done"


def _copy_task_artifacts(con, run, task):
    """Copy a done card's registered text artifacts into the run directory (the project's line)."""
    link = con.execute("SELECT * FROM research_run_tasks WHERE task_id=?", (task["id"],)).fetchone()
    if not link or not task["workspace"]:
        return
    try:
        data = json.loads(Path(task_store.task_state_dir(task["id"]), "report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    node = runs.node(con, link["branch_id"])
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
        if link["kind"] == "red_team":
            kind, dest = "critique", runs.node_prefix(node) + f"critique/{source.name}"
        else:
            kind, dest = "task_output", f"tasks/{task['id']}/{source.name}"
        runs.write_text(con, run["id"], kind, f"[{task['id']}] {source.name}", dest, text,
                        branch_id=_bid(node), task_id=task["id"],
                        metadata={"source_workspace": str(source), "source_file": str(rel)})


def settle_done_tasks(con, *, run_id):
    """Copy accepted artifacts and ingest the Sisters' findings, once per task generation."""
    run = runs.get(con, run_id)
    for task in runs.tasks(con, run_id):
        if task["status"] != "done":
            continue
        generation = int(task["generation"])
        if task_store.latest_payload(con, task["id"], "research_v2_settled", generation=generation):
            continue
        _copy_task_artifacts(con, run, task)
        result = {"kind": task["research_kind"]}
        if task["research_kind"] != "red_team":
            try:
                payload = json.loads(Path(task_store.task_state_dir(task["id"]), "report.json")
                                     .read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = {}
            result = ledger.ingest_report(con, run, task, payload)
        task_store.add_event(con, task["id"], "research_v2_settled", result, generation=generation)


def _done(con, run, node, kind):
    return [row for row in runs.tasks(con, run["id"], kind=kind, node_id=node["id"]) if row["status"] == "done"]


def _ingest_critique(con, run, node, task):
    rows = [a for a in runs.artifacts(con, run["id"], kind="critique", task_id=task["id"])
            if a["path"].endswith(".json")]
    if not rows:
        raise RuntimeError(f"The red team card {task['id']} delivered no critique.json.")
    data = json.loads(Path(rows[-1]["path"]).read_text(encoding="utf-8"))
    for item in (data.get("issues") if isinstance(data, dict) else None) or []:
        if not isinstance(item, dict) or not item.get("material"):
            continue
        try:
            priority = int(item.get("priority") or 0)
        except (TypeError, ValueError):
            priority = 0
        runs.add_issue(con, run["id"], node=node, kind=item.get("kind"),
                       question=item.get("question"), rationale=item.get("rationale"), priority=priority)


def _children_settled(con, run, node):
    return all(c["status"] in runs.NODE_TERMINAL for c in runs.nodes(con, run["id"], parent_id=node["id"]))


def _close(con, run, node, status):
    """Leave the node. Post-order: a node closes -- and merges its line into its parent's -- only
    after every child has closed; until then it waits as ``closing``. Failure keeps the branch unmerged."""
    if status == "closed" and not _children_settled(con, run, node):
        runs.set_node(con, node["id"], status="closing")
        return "closing"
    if node["worktree"]:
        parent = runs.node(con, node["parent_id"])
        outcome = repo.branch_finish(
            run["workspace"], runs.node_branch(node["id"]), node["worktree"],
            into=(parent["worktree"] if parent and parent["worktree"] else run["workspace"]),
            merge=status == "closed", message=f"research {run['id']}/{node['id']}: {status}")
        if outcome == "conflict":
            runs.set_state(con, run["id"],
                           error=f"node {node['id']}: merge conflict; resolve branch {runs.node_branch(node['id'])} by hand")
    if status != "closed":
        con.execute("UPDATE research_issues SET status='parked' WHERE child_branch_id=?", (node["id"],))
    runs.set_node(con, node["id"], status=status)
    return status


def _settle_closing(con, run):
    """Close every waiting node whose children are all terminal, deepest first (the tree's post-order)."""
    while True:
        waiting = [n for n in runs.nodes(con, run["id"])
                   if n["status"] == "closing" and _children_settled(con, run, n)]
        if not waiting:
            return
        _close(con, run, waiting[-1], "closed")


async def _expand(con, cfg, runner, worker, run, node, *, context, tool_call_id, poll_seconds, progress):
    """Advance one node through its routine. Returns the node's terminal status, a halt
    ("stopped" / "budget"), or a waiting_input result dict."""
    drive = lambda: _drive_tasks(con, cfg, runner, run["id"], context=context,   # noqa: E731
                                 tool_call_id=tool_call_id, poll_seconds=poll_seconds, progress=progress)
    nid = node["id"]
    while True:
        node = runs.node(con, nid)
        status = node["status"]
        if status == "queued":
            if node["parent_id"]:
                parent = runs.node(con, node["parent_id"])
                worktree = repo.branch_start(
                    run["workspace"], runs.node_branch(nid), runs.node_worktree(run, nid),
                    base=runs.node_branch(parent["id"]) if parent["parent_id"] else None)
                if worktree:
                    runs.set_node(con, nid, worktree=worktree)
            runs.set_node(con, nid, status="planning")

        elif status in ("planning", "waiting_input"):
            context_path = None
            if node["parent_id"]:
                issue = con.execute("SELECT * FROM research_issues WHERE child_branch_id=?", (nid,)).fetchone()
                _aid, context_path, _packet = context_packet.create(
                    con, run, issue=issue, node=node, parent=runs.node(con, node["parent_id"]))
            await _progress(progress, "planning", f"Last Order is planning {_label(node)}.", run)
            plan, _raw, session_file = await asyncio.to_thread(
                planner.plan, con, run, cfg, worker, node, context_path=context_path)
            _write(con, run, node, "plan_json", "Research plan (JSON)", "plan.json",
                   json.dumps(plan, ensure_ascii=False, indent=2))
            _write(con, run, node, "plan", "Research plan", "plan.md", plan["plan_markdown"].rstrip() + "\n")
            runs.set_node(con, nid, session_file=session_file)
            if node["parent_id"] is None:
                runs.set_state(con, run["id"], root_session=session_file)
            if plan["status"] == "clarify":
                runs.set_node(con, nid, status="waiting_input")
                runs.set_state(con, run["id"], phase="waiting_input", status="waiting_input")
                return {"reason": "waiting_input", "questions": plan["clarifying_questions"],
                        "run": runs.summary(con, run["id"])}
            await _progress(progress, "plan_ready",
                            f"Planning produced {len(plan['tasks'])} research tasks for {_label(node)}.", run,
                            tasks=[{"title": t["title"], "assignee": t["assignee"]} for t in plan["tasks"]])
            await _submit_tasks(con, run, cfg, worker, node, plan["tasks"], kind="research", progress=progress)
            runs.set_node(con, nid, status="executing")

        elif status == "executing":
            outcome = await drive()
            if outcome != "done":
                return outcome
            settle_done_tasks(con, run_id=run["id"])
            if not _done(con, run, node, "research"):
                return _close(con, run, node, "failed")
            runs.set_node(con, nid, status="synthesizing")

        elif status == "synthesizing":
            await _progress(progress, "synthesizing", f"Last Order is writing the conclusion for {_label(node)}.", run)
            text = await asyncio.to_thread(planner.synthesize, con, run, cfg, worker, node,
                                           _done(con, run, node, "research"))
            _write(con, run, node, "synthesis", f"Conclusion · {_label(node)}", "synthesis.md", text)
            runs.set_node(con, nid, status="critiquing")

        elif status == "critiquing":
            if not runs.tasks(con, run["id"], kind="red_team", node_id=nid):
                plan = _json_artifact(con, run, node, "plan_json")
                await _progress(progress, "red_team",
                                f"Sister {plan['red_team']['assignee']} is red-teaming the conclusion for {_label(node)}.", run)
                body = planner.red_team_body(node, synthesis_path=_artifact_path(con, run, node, "synthesis"),
                                             plan_path=_artifact_path(con, run, node, "plan"))
                tid = card_files.create(con, runs.node_root(run, node), f"Red team · {_label(node)}", body,
                                        plan["red_team"]["assignee"], timeout_seconds=runs.call_timeout(cfg, 1800))
                runs.link_task(con, run["id"], tid, kind="red_team", node=node, local_id="red-team")
            outcome = await drive()
            if outcome != "done":
                return outcome
            settle_done_tasks(con, run_id=run["id"])
            red = _done(con, run, node, "red_team")
            if not red:
                return _close(con, run, node, "failed")
            _ingest_critique(con, run, node, red[-1])
            runs.set_node(con, nid, status="probing")

        elif status == "probing":
            open_issues = runs.issues(con, run["id"], node_id=nid, status="open")
            if open_issues:
                await _progress(progress, "probing",
                                f"The red team left {len(open_issues)} issues on {_label(node)}; Last Order is planning probes.", run)
                specs = await asyncio.to_thread(planner.plan_probes, con, run, cfg, worker, node, open_issues,
                                                synthesis_path=_artifact_path(con, run, node, "synthesis"))
                _write(con, run, node, "probes_json", "Probe plan", "probes.json",
                       json.dumps(specs, ensure_ascii=False, indent=2))
                local_to_task = await _submit_tasks(con, run, cfg, worker, node, specs, kind="probe", progress=progress)
                for spec in specs:
                    if spec["local_id"] in local_to_task:
                        runs.set_issue(con, spec["issue_id"], "probing", probe_task_id=local_to_task[spec["local_id"]])
            if runs.issues(con, run["id"], node_id=nid, status="probing"):
                outcome = await drive()
                if outcome != "done":
                    return outcome
                settle_done_tasks(con, run_id=run["id"])
            runs.set_node(con, nid, status="triaging")

        elif status == "triaging":
            issues = runs.issues(con, run["id"], node_id=nid, status="probing")
            if issues:
                verdicts = await asyncio.to_thread(
                    planner.triage, con, run, cfg, worker, node, issues, _done(con, run, node, "probe"),
                    synthesis_path=_artifact_path(con, run, node, "synthesis"))
                _write(con, run, node, "triage_json", "Probe triage", "triage.json",
                       json.dumps(verdicts, ensure_ascii=False, indent=2))
                for issue in issues:
                    verdict = verdicts[issue["id"]]["verdict"]
                    if verdict == "undermines" and node["depth"] < runs.limits(run)["max_depth"]:
                        child = runs.create_node(con, run["id"], trigger=issue["question"],
                                                 parent_id=nid, depth=node["depth"] + 1)
                        runs.set_issue(con, issue["id"], "undermines", child_branch_id=child["id"])
                    else:
                        runs.set_issue(con, issue["id"], "parked" if verdict == "undermines" else verdict)
            return _close(con, run, node, "closed")

        else:
            raise RuntimeError(f"Node {nid} is in an unknown state: {status}")


def _partial_result(con, run, reason):
    artifacts = runs.artifacts(con, run["id"])
    lines = ['# Incomplete research run', "", f"- Run: `{run['id']}`",
             f"- Project: `{runs.project_name(run)}` (`{run['workspace']}`)",
             f"- Reason: {reason}", "", '## Original question',
             run["question"], "", '## Saved artifacts']
    lines.extend(f"- [{row['kind']}] {row['title']} — `{row['path']}`" for row in artifacts)
    lines += ["", "This document records where the run stopped. It is not a final report, and the research is incomplete.", ""]
    aid, path = runs.write_text(con, run["id"], "partial", 'Incomplete research run', "partial.md", "\n".join(lines))
    runs.set_state(con, run["id"], status="stopped", final_artifact=aid)
    return {"reason": "stopped", "final": {"artifact": aid, "path": path,
            "content": Path(path).read_text(encoding="utf-8")},
            "run": runs.summary(con, run["id"])}


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
    halts = {"stopped": "The user requested a stop.", "budget": "The shared token budget limit was reached."}
    try:
        if runs.stop_requested(con, run_id):
            return _partial_result(con, run, halts["stopped"])
        if budget.status(con, cfg.get("token_cap"))["mode"] != "normal":
            return _partial_result(con, run, halts["budget"])
        _refresh_workspace_index(con, run)
        if run["phase"] == "created":
            runs.set_state(con, run_id, phase="active")
        # ponytail: nodes expand one at a time (cards within a node run in parallel);
        # expand siblings concurrently if wall-clock ever matters.
        while True:
            _settle_closing(con, run)
            node = runs.next_node(con, run_id)
            if node is None:
                break
            runs.set_state(con, run_id, wave=node["depth"])
            result = await _expand(con, cfg, runner, worker, run, node, context=context,
                                   tool_call_id=tool_call_id, poll_seconds=poll_seconds, progress=progress)
            if isinstance(result, dict):
                return result
            if result in halts:
                settle_done_tasks(con, run_id=run_id)
                return _partial_result(con, run, halts[result])
        runs.set_state(con, run_id, phase="finalizing")
        await _progress(progress, "finalizing", "Every node is closed; Last Order is adjudicating the final report.", run)
        result = await asyncio.to_thread(report.finalize, con, run, cfg, worker)
        runs.set_state(con, run_id, phase="done", status="done", final_artifact=result["artifact"])
        _refresh_workspace_index(con, runs.get(con, run_id))
        return {"reason": "done", "final": result, "run": runs.summary(con, run_id)}
    except Exception as error:
        runs.set_state(con, run_id, status="failed", error=f"{type(error).__name__}: {error}"[:500])
        raise


if __name__ == "__main__":                          # self-check: a two-level tree with a fake worker, no LLM
    import re
    import sys
    import tempfile

    from misaka.config import CFG

    class FakeWorker:
        def __init__(self, levels=1):
            self.triages, self.levels = 0, levels

        def run_llm_json(self, profile_dir, prompt, *_args, raw=False, **_kwargs):
            if prompt.startswith(planner.PREFLIGHT_CONTRACT):
                return {"preflight_markdown": "Look at the one source we have and quote it faithfully."}, "", None
            if prompt.startswith(planner.SYNTHESIS_CONTRACT) or prompt.startswith(report.FINAL_CONTRACT):
                return None, "# Conclusion\n\n" + "The evidence supports the claim [t/out.md]. " * 6, None
            if prompt.startswith(planner.PROBE_PLAN_CONTRACT):
                ids = re.findall(r'"issue_id": "(i_[0-9a-f]+)"', prompt)
                return {"tasks": [{"issue_id": i, "local_id": f"probe-{n}", "title": f"Probe {n}",
                                   "question": "Does it hold?", "rationale": "Tests the issue",
                                   "deliverable": "out.md", "assignee": "s1"} for n, i in enumerate(ids, 1)]}, "", None
            if prompt.startswith(planner.TRIAGE_CONTRACT):
                self.triages += 1
                ids = re.findall(r'"issue_id": "(i_[0-9a-f]+)"', prompt)
                verdict = "undermines" if self.triages <= self.levels else "supports"
                return {"verdicts": [{"issue_id": i, "verdict": verdict, "reason": "scripted"} for i in ids]}, "", None
            if prompt.startswith(planner.ROOT_CONTRACT):
                return {"status": "ready", "plan_markdown": "A plan that is long enough to pass the envelope check.",
                        "tasks": [{"local_id": "t1", "title": "Read the source", "question": "What does it say?",
                                   "rationale": "Only source", "deliverable": "out.md", "assignee": "s1"}],
                        "red_team": {"assignee": "s1", "reason": "only Sister"}}, "", None
            return None, "", f"unexpected prompt: {prompt[:40]}"

    class FakeRunner:
        def __init__(self, con, levels=1):
            self.con, self.red_teams, self.levels = con, 0, levels

        async def launch_ready(self, *, task_ids, **_kwargs):
            for tid in task_ids:
                row = task_store.get(self.con, tid)
                if row["status"] != "ready":
                    continue
                link = self.con.execute("SELECT kind FROM research_run_tasks WHERE task_id=?", (tid,)).fetchone()
                state = Path(task_store.task_state_dir(tid))
                state.mkdir(parents=True, exist_ok=True)
                if link["kind"] == "red_team":
                    self.red_teams += 1
                    issues = [{"kind": "scope", "question": f"Is the scope right? ({tid})", "rationale": "x",
                               "priority": 1, "material": True}] if self.red_teams <= self.levels else []
                    Path(row["workspace"], "critique.md").write_text("# Review\n", encoding="utf-8")
                    Path(row["workspace"], "critique.json").write_text(json.dumps({"issues": issues}), encoding="utf-8")
                    report_ = {"artifacts": ["critique.md", "critique.json"]}
                else:
                    Path(row["workspace"], f"{tid}.md").write_text("Finding one\n", encoding="utf-8")
                    report_ = {"artifacts": [f"{tid}.md"],
                               "findings": [{"text": "Finding one is a self-contained claim", "claim_type": "fact",
                                             "source_file": f"{tid}.md", "quote": "Finding one"}]}
                (state / "report.json").write_text(json.dumps({
                    "schema_version": 1, "status": "done", "summary": "scripted", "uncertain": [], "notes": "", **report_}))
                repo.commit_card(row["workspace"], tid, report_, f"card {tid}: submit")
                self.con.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))

    tmp = tempfile.mkdtemp(prefix="misaka-research-")
    os.environ["MISAKA_RUNS_HOME"] = os.path.join(tmp, "runs")
    CFG["tasks_root"] = os.path.join(tmp, "task-state")
    for d in ("profiles/sisters/s1", "profiles/last_order"):
        os.makedirs(os.path.join(tmp, d))
    Path(tmp, "profiles/sisters/s1/DESCRIBE.md").write_text("---\ndescription: reads things\n---\n")
    ws = os.path.join(tmp, "project")
    os.makedirs(ws)
    print("\n".join(card_files.init_project(ws)))
    cfg = {"db": os.path.join(tmp, "board.db"), "profiles_root": os.path.join(tmp, "profiles/sisters"),
           "roles_root": os.path.join(tmp, "profiles"), "provider": "x", "default_model": "y",
           "judge_timeout": 5, "token_cap": 0}
    con = task_store.connect(cfg["db"])
    r = runs.create(con, workspace=ws, question="Does the source support the claim?", limits={"max_depth": 2})
    out = asyncio.run(run(con, cfg, FakeRunner(con, 2), FakeWorker(2), run_id=r["id"], poll_seconds=0))
    assert out["reason"] == "done", out
    root, child, grandchild = tree = runs.nodes(con, r["id"])
    assert [n["depth"] for n in tree] == [0, 1, 2] and all(n["status"] == "closed" for n in tree), [dict(n) for n in tree]
    assert child["parent_id"] == root["id"] and grandchild["parent_id"] == child["id"]
    kinds = sorted(t["research_kind"] for t in runs.tasks(con, r["id"]))
    assert kinds == ["probe", "probe", "red_team", "red_team", "red_team", "research", "research", "research"], kinds
    assert sorted(i["status"] for i in runs.issues(con, r["id"])) == ["undermines", "undermines"]
    assert Path(ws, "research", r["id"], "final.md").is_file()
    assert not runs.get(con, r["id"])["last_error"], runs.get(con, r["id"])["last_error"]
    # post-order: the grandchild merged into the child's branch, the child (carrying it) into master -- one merge point on master
    assert repo._git(ws, "rev-list", "--merges", "--count", "--first-parent", "HEAD").stdout.strip() == "1"
    merged_into_child = repo._git(ws, "branch", "--merged", f"research/{child['id']}").stdout
    assert f"research/{grandchild['id']}" in merged_into_child, merged_into_child
    log = repo._git(ws, "log", "--oneline").stdout
    missing = [(t["id"], t["research_kind"], t["workspace"]) for t in runs.tasks(con, r["id"]) if f"card {t['id']}: submit" not in log]
    assert not missing, (missing, repo._git(ws, "log", "--all", "--graph", "--oneline").stdout)
    assert not any(os.path.exists(n["worktree"] or "/nonexistent") for n in tree)
    print(repo._git(ws, "log", "--graph", "--oneline").stdout, file=sys.stderr)
    # depth 0: the undermining issue is parked, no child node
    r0 = runs.create(con, workspace=ws, question="Second run", limits={"max_depth": 0})
    worker0 = FakeWorker()
    asyncio.run(run(con, cfg, FakeRunner(con), worker0, run_id=r0["id"], poll_seconds=0))
    assert len(runs.nodes(con, r0["id"])) == 1
    assert [i["status"] for i in runs.issues(con, r0["id"])] == ["parked"]
    print("workflow self-check OK", file=sys.stderr)

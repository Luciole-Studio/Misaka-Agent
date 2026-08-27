"""Persistent, depth-bounded Research Workflow: breadth-first over a tree of conclusions.

A node is one conclusion. Its routine (``_expand``) is the same for the root and for every
node opened under it: Last Order plans, Sisters research, Last Order synthesizes, the
red-team Sister she named finds issues, one probe card tests each issue, Last Order triages,
and only an issue that undermines the conclusion opens a child node. Each issue is investigated by
a fork of the node's Last Order session (its own process, its own Sisters) that returns the verdict;
the fork that found the conclusion undermined becomes the child node's Last Order. The frontier is FIFO by
(depth, created_at); the depth limit bounds how many times a conclusion may be overturned in
a chain. Code enforces the order, the depth, persistence, and artifact integrity; the models
keep every judgement call.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import sys
from pathlib import Path

from misaka import workspace as workspace_index
from misaka.platform import budget, repo
from misaka.platform import cards as card_files
from misaka.platform import tasks as task_store
from misaka.research import context as context_packet
from misaka.research import ledger, planner, report, runs

POLL_SECONDS = 2.0
MAX_PROBE_ROUNDS = 3          # ponytail: a fork opens cards at most this many times before it must judge
ACTIVE_TASKS = ("running", "review")
RESEARCH_DISCIPLINE = """[Research Workflow active]
Last Order is now in Research mode. Each node of the research tree runs the same routine: Last Order plans and assigns
Sisters (every task starts with a preflight plan), Last Order writes the node's conclusion in one pass, the red-team Sister she
named finds issues in it, and a fork of Last Order's session investigates each issue with Sisters of its own and returns a
verdict. Only an issue that undermines the conclusion opens a child node (the fork becomes its Last Order); the tree is
expanded breadth-first up to the chosen depth.

Code enforces phase order, depth, persistence, and artifact integrity. The models keep the judgement calls: framing, methods,
Sister selection, source quality, task count, and what shakes a conclusion. The rule against answering lifts only at final
adjudication, which must keep competing conclusions side by side wherever the evidence cannot decide between them.
"""


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
                           branch_id=_bid(node), metadata=metadata or None,
                           source_workspace=runs.node_root(run, node))


def _artifact_path(con, run, node, kind):
    rows = runs.artifacts(con, run["id"], kind=kind, **_scope(node))
    return runs.artifact_path(rows[-1]) if rows else None


def _json_artifact(con, run, node, kind):
    rows = runs.artifacts(con, run["id"], kind=kind, **_scope(node))
    return json.loads(runs.artifact_text(rows[-1])) if rows else None


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


def _node_argv(run, node):
    return [sys.executable, "-m", "misaka", "research", "--node", run["id"], node["id"]]


def _probe_argv(run, issue):
    return [sys.executable, "-m", "misaka", "research", "--probe", run["id"], issue["id"]]


def _label(node):
    return "the root question" if node["parent_id"] is None else f"node {node['id']}"


def _scope_local_ids(specs, prefix):
    """Copies of ``specs`` with ``local_id`` and ``dependencies`` prefixed by ``prefix``. Local
    ids are unique inside one plan only; probes of different issues (and rounds) on the same node
    reuse ``a``/``b``, so they get ``<issue>.r<round>.`` in front before anything is keyed on them."""
    if not prefix:
        return list(specs)
    out = []
    for spec in specs:
        scoped = dict(spec)
        scoped["local_id"] = prefix + str(spec["local_id"])
        scoped["dependencies"] = [prefix + str(dep) for dep in (spec.get("dependencies") or [])]
        out.append(scoped)
    return out


async def _preflight_specs(run, cfg, worker, node, specs, *, progress):
    """Plan every spec's approach, one session at a time. Returns one ``planner.preflight`` result
    per spec, in the specs' own order.

    One at a time is not a preference; it is the only thing this process can do, and saying so is
    the point of this function. A preflight is a whole in-process model session, and
    ``platform.session.run_session`` opens every one of those inside ``_env_window``, a process-wide
    lock held for the *entire* session rather than for the ``os.environ`` mutation it is named
    after. The window cannot simply be narrowed: a session's identity travels through the
    environment and is read out of it all the way through the turn -- ``budget.record_external_call``
    reads ``MISAKA_USAGE_DB``/``_TASK_ID``/``_GENERATION`` at every accounted call,
    ``messages.register`` and ``roster`` read the card and the role, ``extensions.mcp`` reads the
    profile, a spawned sub-agent re-reads all three -- and ``_run_session`` restores the keys it
    saved, which two overlapping sessions cannot both do. So overlapping the calls bought no
    wall-clock at all: they queued on that lock exactly as they had queued on each other.

    It was not free, either. ``worker.run_llm_json`` reserves budget capacity *before* it reaches
    the session, so N overlapping preflights held N reservations while N-1 of them sat waiting for
    the lock. Under a token cap that is how a run that used to finish starts failing: the
    reservations cross the Beast threshold, one takes the whole remainder, the next is refused, and
    ``planner.preflight`` raises "shared token budget exhausted" where the serial pass had simply
    planned the next card. Serial, exactly one reservation exists at a time and it is settled
    before the next is asked for.

    Real concurrency here needs a preflight to run in its own process -- the same answer headless
    card dispatch reached (``research.node.HeadlessRunner``) -- not a thread in this one.
    """
    planned = []
    for spec in specs:
        await _progress(progress, "preflight",
                        f"Sister {spec['assignee']} is planning its approach for {spec['title']!r}.", run)
        # The failure of one spec ends the submit, so the specs behind it never open a session:
        # the exception leaves this loop before the next preflight is reached.
        planned.append(await asyncio.to_thread(planner.preflight, run, cfg, worker, spec, node=node))
    return planned


async def _submit_tasks(con, run, cfg, worker, node, specs, *, kind, issue_id=None, evidence="", progress=None,
                        local_prefix=""):
    """Preflight every spec, then open its card on the node's line. Returns local_id -> task id
    (the scoped id when ``local_prefix`` is set).

    Three passes, because only the middle one may leave the loop thread: which specs still need a
    card is read off the board here, their plans are written in worker threads, and the cards are
    then created in the plan's own order -- so creation, link and output directory stay one
    transaction, and the card a dependency points at still exists before the card that needs it.
    """
    root = runs.node_root(run, node)
    local_to_task = {}
    specs = _scope_local_ids(specs, local_prefix)
    pending = []
    for spec in specs:
        if runs.stop_requested(con, run["id"]):
            break
        existing = con.execute(
            "SELECT task_id FROM research_run_tasks WHERE run_id=? AND branch_id=? AND local_id=?",
            (run["id"], node["id"], spec["local_id"]),
        ).fetchone()
        if existing and task_store.get(con, existing["task_id"]) is not None:
            local_to_task[spec["local_id"]] = existing["task_id"]   # a resume after a crash: the card already exists
            continue
        if existing:                                   # the card was deleted: drop the stale link and rebuild it
            con.execute("DELETE FROM research_run_tasks WHERE task_id=?", (existing["task_id"],))
        pending.append(spec)
    planned = await _preflight_specs(run, cfg, worker, node, pending, progress=progress) if pending else []
    for spec, (preflight, _raw, session) in zip(pending, planned, strict=True):   # one plan per spec, in order
        if runs.stop_requested(con, run["id"]):
            break                                      # a stop that arrived while the plans were being written
        aid, path = _write(con, run, node, "preflight", f"{spec['title']} · preflight",
                           f"tasks/{spec['local_id']}-preflight.md",
                           preflight["preflight_markdown"].rstrip() + "\n",
                           assignee=spec["assignee"], session_file=session)
        with task_store.write_txn(con):                # card, link and output dir land together or not at all
            tid = card_files.create(
                con, root, spec["title"], planner.task_body(spec, path, evidence=evidence), spec["assignee"],
                priority=spec.get("priority", 0), timeout_seconds=runs.call_timeout(cfg, 1800),
                after_row=lambda tid, aid=aid, spec=spec: runs.link_task(   # linked before the file exists
                    con, run["id"], tid, kind=kind, node=node, preflight_artifact=aid,
                    local_id=spec["local_id"], issue_id=issue_id, dependencies=spec.get("dependencies") or []))
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
    """Promote every waiting card whose dependencies are settled. Blocking, and called from a
    worker thread for it: ``dependency_state`` reaches ``task_store.parent_ids``, which reads and
    parses the card *file* of every todo card -- the frontmatter ``needs`` is the executable
    contract, so there is no table to consult -- and this runs twice per two-second tick."""
    for row in runs.tasks(con, run_id):
        if row["status"] != "todo":
            continue
        state, parents = task_store.dependency_state(con, row["id"])
        if state == "failed":
            task_store.mark_stopped(con, row["id"])
            task_store.add_event(con, row["id"], "dependency_failed", {"parents": parents})
        else:
            task_store.promote_task(con, row["id"])


def _stop_pending(con, linked):
    """Hold back every card of a halted scope that had not started yet.

    Off the loop thread as one hop rather than one per card: ``mark_stopped`` mirrors the card file
    and commits it, and ``repo.commit`` retries up to four git subprocesses with a growing sleep
    between them while ``index.lock`` is contended. A stop with fifty linked cards was tens of
    seconds of frozen loop. A card the reconciler has just accepted is in ``review`` or ``done``
    and is deliberately not touched here.
    """
    for row in linked:
        if row["status"] in {"ready", "todo"}:
            task_store.mark_stopped(con, row["id"])


async def _stop_active(runner, task_ids, context):
    stop = getattr(runner, "stop", None)
    if not callable(stop):
        return
    await asyncio.gather(
        *(stop(task_id, confirmed=True, context=context) for task_id in task_ids),
        return_exceptions=True,
    )


async def _drive_tasks(con, cfg, runner, run_id, *, scope, context=None,
                       tool_call_id="research", poll_seconds=POLL_SECONDS, progress=None):
    """Drive the cards in ``scope`` (task ids) until none is open; tasks waiting on dependencies stay in ``todo``."""
    last_snapshot = None
    while True:
        run = runs.get(con, run_id)
        linked = [row for row in runs.tasks(con, run_id) if row["id"] in scope]
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
                "budget" if budget.status(con, cfg.get("token_cap"))["mode"] == "stop" else None)
        if halt:
            await _stop_active(runner, active, context)
            if active:
                # Those cards were just killed mid-turn, so every one of their rows still reads
                # ``running`` with its claim on it and half an hour left on the lease -- and
                # ``db.claim``'s admission counter counts a claimed running row with no expiry
                # filter at all, host-wide. Left standing they refuse every claim on the machine,
                # in every project, until a human clears them. ``dispatch.reconcile`` is the one
                # path that settles a card whose worker is gone -- each under that card's exact
                # ownership fence -- and it validates reports and can reach git, so it goes off
                # the loop thread.
                from misaka.network import dispatch
                await asyncio.to_thread(dispatch.reconcile, con, cfg)
                linked = [row for row in runs.tasks(con, run_id) if row["id"] in scope]
            await asyncio.to_thread(_stop_pending, con, linked)
            return halt
        await asyncio.to_thread(_release_dependencies, con, run_id)
        linked = [row for row in runs.tasks(con, run_id) if row["id"] in scope]
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
            await asyncio.to_thread(_release_dependencies, con, run_id)
            if any(row["status"] == "todo" and row["id"] in scope for row in runs.tasks(con, run_id)):
                raise RuntimeError("Research task dependencies cannot advance; the graph may contain a cycle.")
            continue
        complete_scope = bool(scope) and {row["id"] for row in linked} == set(scope)
        return "done" if complete_scope and all(row["status"] == "done" for row in linked) else "failed"


def _register_task_artifacts(con, run, task):
    """Register a done card's text artifacts where they are: on the card's line, at their project-relative
    path (the same path once the node merges). Nothing is copied."""
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
            inside = source.relative_to(Path(task["workspace"]).resolve())
        except ValueError:
            continue
        if not source.is_file():
            continue
        try:
            raw = source.read_bytes()
            raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # binary originals are handled by corpus/PageIndex ingestion
        kind = "critique" if link["kind"] == "red_team" else "task_output"
        runs.register_file(con, run["id"], kind, f"[{task['id']}] {source.name}",
                           os.path.join(run["workspace"], str(inside)), sha256=hashlib.sha256(raw).hexdigest(),
                           branch_id=_bid(node), task_id=task["id"],
                           metadata={"source_workspace": str(source), "source_file": str(rel)})


def settle_done_tasks(con, *, run_id):
    """Register accepted artifacts and ingest the Sisters' findings, once per task generation."""
    run = runs.get(con, run_id)
    for task in runs.tasks(con, run_id):
        if task["status"] != "done":
            continue
        generation = int(task["generation"])
        if task_store.latest_payload(con, task["id"], "research_v2_settled", generation=generation):
            continue
        _register_task_artifacts(con, run, task)
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
    """Register the red team's material issues. Returns None, or why the critique could not be
    read: a card that came back done without a usable critique.json fails its node, because an
    exception here escapes to the driver and takes the whole run down with the node."""
    rows = [a for a in runs.artifacts(con, run["id"], kind="critique", task_id=task["id"])
            if a["path"].endswith(".json")]
    if not rows:
        return f"The red team card {task['id']} delivered no critique.json."
    try:
        data = json.loads(runs.artifact_text(rows[-1]))
    except (OSError, ValueError) as error:
        return f"The red team card {task['id']} delivered an unreadable critique.json: {error}"
    for item in (data.get("issues") if isinstance(data, dict) else None) or []:
        if not isinstance(item, dict) or not item.get("material"):
            continue
        try:
            priority = int(item.get("priority") or 0)
        except (TypeError, ValueError):
            priority = 0
        runs.add_issue(con, run["id"], node=node, kind=item.get("kind"),
                       question=item.get("question"), rationale=item.get("rationale"), priority=priority)
    return None


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
        into = parent["worktree"] if parent and parent["worktree"] else run["workspace"]
        # A failed node keeps its worktree: its cards still point there and a human may want to look.
        outcome = repo.branch_finish(
            run["workspace"], runs.node_branch(node["id"]), node["worktree"], into=into,
            merge=status == "closed", remove=status == "closed",
            message=f"research {run['id']}/{node['id']}: {status}")
        expected = "merged" if status == "closed" else "closed"
        if outcome != expected:
            runs.set_state(con, run["id"],
                           error=f"node {node['id']}: its line could not be safely finished or removed; "
                                 f"resolve branch {runs.node_branch(node['id'])} by hand")
            runs.set_node(con, node["id"], status="conflict")     # a state, not a string: resume cannot clear it
            return "conflict"
        if outcome == "merged":
            runs.relocate_node_tasks(con, node, node["worktree"], into)
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


async def _expand(con, cfg, runner, worker, run, node, *, spawner, context, tool_call_id, poll_seconds, progress):
    """Advance one node through its routine. Returns the node's terminal status, a halt
    ("stopped" / "budget"), or a waiting_input result dict."""
    nid = node["id"]

    def drive(kind):
        scope = {row["id"] for row in runs.tasks(con, run["id"], kind=kind, node_id=nid)}
        return _drive_tasks(con, cfg, runner, run["id"], scope=scope, context=context,
                            tool_call_id=tool_call_id, poll_seconds=poll_seconds, progress=progress)
    while True:
        node = runs.node(con, nid)
        status = node["status"]
        if status == "queued":
            if node["parent_id"]:
                parent = runs.node(con, node["parent_id"])
                worktree = repo.branch_start(
                    run["workspace"], runs.node_branch(nid), runs.node_worktree(run, nid),
                    base=runs.node_branch(parent["id"]) if parent["parent_id"] else None)
                if not worktree:                        # isolation is the node's precondition, not a nicety
                    runs.set_state(con, run["id"], error=f"node {nid}: its worktree could not be created")
                    return _close(con, run, node, "failed")
                runs.set_node(con, nid, worktree=worktree)
            runs.set_node(con, nid, status="planning")

        elif status in ("planning", "waiting_input"):
            plan = _json_artifact(con, run, node, "plan_json")
            if plan is not None and plan.get("status") == "clarify":
                plan = None                       # resume the same Last Order session with the user's answer
            context_path = None
            if plan is None and node["parent_id"]:
                issue = con.execute("SELECT * FROM research_issues WHERE child_branch_id=?", (nid,)).fetchone()
                _aid, context_path, _packet = context_packet.create(
                    con, run, issue=issue, node=node, parent=runs.node(con, node["parent_id"]))
            if plan is None:
                await _progress(progress, "planning", f"Last Order is planning {_label(node)}.", run)
                plan, _raw, session_file = await asyncio.to_thread(
                    planner.plan, con, run, cfg, worker, node, context_path=context_path)
                _write(con, run, node, "plan", "Research plan", "plan.md", plan["plan_markdown"].rstrip() + "\n")
                runs.set_node(con, nid, session_file=session_file)
                if node["parent_id"] is None:
                    runs.set_state(con, run["id"], root_session=session_file)
                # plan.json is the checkpoint: its Markdown and session lineage
                # have both landed before this final marker.
                _write(con, run, node, "plan_json", "Research plan (JSON)", "plan.json",
                       json.dumps(plan, ensure_ascii=False, indent=2))
            elif not _artifact_path(con, run, node, "plan"):
                _write(con, run, node, "plan", "Research plan", "plan.md", plan["plan_markdown"].rstrip() + "\n")
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
            outcome = await drive("research")
            if outcome == "failed":
                return _close(con, run, node, "failed")
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
                                             plan_path=_artifact_path(con, run, node, "plan"),
                                             evidence=planner.evidence_block(con, run, node))
                tid = card_files.create(con, runs.node_root(run, node), f"Red team · {_label(node)}", body,
                                        plan["red_team"]["assignee"], timeout_seconds=runs.call_timeout(cfg, 1800))
                runs.link_task(con, run["id"], tid, kind="red_team", node=node, local_id="red-team")
            outcome = await drive("red_team")
            # Terminal, as in the executing phase: a node left non-terminal by a process that has
            # already exited is what the driver turns into a dead run.
            if outcome == "failed":
                return _close(con, run, node, "failed")
            if outcome != "done":
                return outcome
            settle_done_tasks(con, run_id=run["id"])
            red = _done(con, run, node, "red_team")
            if not red:
                return _close(con, run, node, "failed")
            unusable = _ingest_critique(con, run, node, red[-1])
            if unusable:
                task_store.add_event(con, red[-1]["id"], "research_critique_unusable", {"reason": unusable})
                runs.set_state(con, run["id"], error=f"node {nid}: {unusable}")
                return _close(con, run, node, "failed")
            runs.set_node(con, nid, status="probing")

        elif status == "probing":
            pending = [i for i in runs.issues(con, run["id"], node_id=nid) if i["status"] in ("open", "probing")]
            if pending:
                await _progress(progress, "probing",
                                f"The red team left {len(pending)} issues on {_label(node)}; a Last Order fork investigates each.",
                                run, issues=[i["id"] for i in pending])
                handles = {}
                try:
                    for issue in pending:
                        probe_dir = runs.probe_session_dir(run, issue["id"])
                        if not os.path.isdir(probe_dir):             # resume: a fork already forked keeps its session
                            planner.fork_session(planner._lo_session(run, node), probe_dir)
                        runs.set_issue(con, issue["id"], "probing")
                        await asyncio.to_thread(_reap_orphan_runner, con, "research_issues", issue)
                        handle = spawner.spawn(_probe_argv(run, issue), cwd=run["workspace"],
                                               title=f"LO·{nid}·{issue['id']}", place="split")
                        handles[issue["id"]] = handle
                        _record_runner(con, "research_issues", issue["id"], handle)
                except BaseException:
                    await _stop_all_off_loop(spawner, handles)
                    raise
                outcome = await _wait_probes(con, cfg, spawner, run, handles, poll_seconds=poll_seconds)
                if outcome != "done":
                    return outcome
            runs.set_node(con, nid, status="triaging")

        elif status == "triaging":
            for issue in runs.issues(con, run["id"], node_id=nid):
                if issue["status"] != "undermines" or issue["child_branch_id"]:
                    continue
                if node["depth"] < runs.limits(run)["max_depth"]:
                    child = next((n for n in runs.nodes(con, run["id"], parent_id=nid)     # created before a crash
                                  if n["trigger_text"] == issue["question"]), None) \
                        or runs.create_node(con, run["id"], trigger=issue["question"], parent_id=nid,
                                            depth=node["depth"] + 1)
                    probe_dir = runs.probe_session_dir(run, issue["id"])
                    if os.path.isdir(probe_dir):                 # the fork that found it becomes the child's Last Order
                        shutil.copytree(probe_dir, planner._lo_session(run, child), dirs_exist_ok=True)
                    runs.set_issue(con, issue["id"], "undermines", child_branch_id=child["id"])
                else:
                    runs.set_issue(con, issue["id"], "parked")
            return _close(con, run, node, "closed")

        else:
            raise RuntimeError(f"Node {nid} is in an unknown state: {status}")


def _unfinished_reason(con, run_id):
    """Why the run must not be adjudicated as done: nodes that failed, or nodes whose branch is
    still in conflict (their work is not on the line the report is written from)."""
    parts = []
    for status, label in (("failed", "failed"), ("conflict", "in merge conflict")):
        ids = [n["id"] for n in runs.nodes(con, run_id) if n["status"] == status]
        if ids:
            parts.append(f"{len(ids)} node(s) {label}: {', '.join(ids)}")
    return "; ".join(parts) or None


def _settle_conflicts(con, run):
    """A human merged a conflicted branch by hand: the node closes and its cards move to the parent line."""
    for node in runs.nodes(con, run["id"]):
        if node["status"] != "conflict" or not node["worktree"]:
            continue
        parent = runs.node(con, node["parent_id"])
        into = parent["worktree"] if parent and parent["worktree"] else run["workspace"]
        if repo.branch_merged(run["workspace"], runs.node_branch(node["id"]), into):
            outcome = repo.branch_finish(
                run["workspace"], runs.node_branch(node["id"]), node["worktree"], into=into,
                merge=False, remove=True)
            if outcome != "closed":
                runs.set_state(con, run["id"],
                               error=f"node {node['id']}: its merged worktree could not be safely removed; "
                                     f"resolve branch {runs.node_branch(node['id'])} by hand")
                continue
            runs.relocate_node_tasks(con, node, node["worktree"], into)
            runs.set_node(con, node["id"], status="closed")


async def _keep_lease(con, run_id, lock, lost):
    """Renew the driver lease in the background for as long as the run is being driven; a failed
    renewal (another driver took over) raises the flag the main loop checks."""
    while True:
        await asyncio.sleep(runs.DRIVER_TTL_SECONDS / 3)
        if not runs.heartbeat_driver(con, run_id, lock):
            lost.set()
            return


_INCOMPLETE_BANNER = (
    "> **This research is incomplete.** The run stopped before its final adjudication, so nothing below has\n"
    "> been weighed against anything else. Each conclusion is quoted exactly as the node that reached it wrote\n"
    "> it: none has been reconciled with the others, none has been through the citation gate, and the issues\n"
    "> listed below were never settled. This is the material the run had produced when it stopped, not its\n"
    "> answer.")


def _cell(value):
    """One table cell. ``report._one_line`` is what decides where a row begins, so text the run
    fetched cannot open a row of its own; escaping the pipes stops it opening a *column*."""
    return report._one_line(value).replace("|", r"\|")


_HEADING = re.compile(r"^(#{1,6})(\s|$)")
_FENCE = re.compile(r"^\s{0,3}(```|~~~)")


def _headings(text):
    """``(line index, level)`` of every ATX heading outside a fenced block -- a ``#`` in code is prose."""
    fence, out = None, []
    for index, line in enumerate(text.splitlines()):
        opener = _FENCE.match(line)
        if fence is not None:
            if opener and line.strip().startswith(fence):
                fence = None
        elif opener:
            fence = opener.group(1)
        elif found := _HEADING.match(line):
            out.append((index, len(found.group(1))))
    return out


def _nested(text, under):
    """A conclusion with its own headings pushed below the heading of the section quoting it.

    A node writes free-form Markdown and usually opens at ``##``, which outranks the ``###`` naming
    the node it came from: left alone, the second node's heading reads in any outline as part of the
    first node's conclusion, and the tree renders backwards. Only the level moves. The words are
    untouched, as is the synthesis artifact on disk -- that copy, not this one, is what gets cited.
    """
    found = _headings(text)
    if not found:
        return text
    lines = text.splitlines()
    deeper = max(0, under + 1 - min(level for _index, level in found))
    for index, level in found:
        lines[index] = "#" * min(6, level + deeper) + lines[index][level:]
    return "\n".join(lines)


def _conclusions(con, run):
    """Every node's conclusion, inlined whole, in tree order.

    A halted run cannot call the model again, and its nodes have already written the only thing it
    has to hand back. Listing those files as paths hands the reader a directory listing instead of
    a document. ``runs.nodes`` orders by depth then creation -- the root, then each node that
    re-researched a point in it -- which is the order the survey and the adjudication read them in.
    """
    out = []
    for node in runs.nodes(con, run["id"]):
        rows = runs.artifacts(con, run["id"], kind="synthesis", **_scope(node))
        if not rows:
            continue
        texts = []
        for row in rows:
            try:
                texts.append(_nested(runs.artifact_text(row).strip(), 3))
            except (OSError, ValueError) as error:
                # A conclusion moved or edited under the run costs this document that one section.
                # It must never cost the document: this is a halted run's whole deliverable.
                texts.append(f"*(This conclusion could not be read back: {error})*")
        out.append(f"### [{node['id']}] depth {node['depth']} — {report._one_line(node['trigger_text'])}\n\n"
                   + "\n\n".join(texts))
    return out


def _open_issues(con, run):
    """The issues nobody settled, as a table.

    ``report._boundary`` is the query, not a copy of it: the boundary a halted run declares and the
    one the final report is held to have to be the same set, or two documents of one run disagree
    about what it left unanswered.
    """
    rows = report._boundary(con, run)
    if not rows:
        return ["No issue was left open, inconclusive, or parked."]
    return ["The issues this run left open, inconclusive, or parked by the depth limit:", "",
            "| Issue | Node | Status | Question | Why it was raised |",
            "| --- | --- | --- | --- | --- |",
            *(f"| {_cell(row['issue'])} | {_cell(row['node'])} | {_cell(row['status'])} "
              f"| {_cell(row['question'])} | {_cell(row['rationale'])} |" for row in rows)]


def _partial_result(con, run, reason, *, status="stopped"):
    """What a run that cannot finish hands back -- a deliverable, never a list of paths.

    Assembled with no model call at all, because the halt this most often answers is the token
    budget: at that moment there is nothing left to spend, and everything the document needs is
    already on disk. Each node's conclusion as that node wrote it, the ledger's count of what the
    run actually verified, and the issues it never settled.
    """
    findings = len(ledger.findings(con, run["id"], limit=report._LEDGER_LIMIT))
    lines = ['# Incomplete research run', "", f"- Run: `{run['id']}`",
             f"- Project: `{runs.project_name(run)}` (`{run['workspace']}`)",
             f"- Reason: {reason}", "", _INCOMPLETE_BANNER, "", '## Original question',
             run["question"], "", '## Conclusions reached before the run stopped']
    for section in _conclusions(con, run) or ["No node had written its conclusion yet."]:
        lines += ["", section]
    lines += ["", '## Evidence on record', "",
              (f"The ledger holds {findings} verified finding{'' if findings == 1 else 's'}: each rests on a "
               "quotation checked verbatim against the source the card registered for it."),
              "", '## Unresolved issues', ""]
    lines += _open_issues(con, run)
    lines += ["", '## Saved artifacts', ""]
    lines.extend(f"- [{row['kind']}] {row['title']} — `{row['path']}`"
                 for row in runs.artifacts(con, run["id"]))
    lines += [""]
    aid, path = runs.write_text(con, run["id"], "partial", 'Incomplete research run', "partial.md", "\n".join(lines))
    runs.set_state(con, run["id"], status=status, final_artifact=aid)
    return {"reason": status, "final": {"artifact": aid, "path": path,
            "content": Path(path).read_text(encoding="utf-8")},
            "run": runs.summary(con, run["id"])}


def _stop_all(spawner, handles):
    """Stop every started process of a batch; the one place a batch unwinds."""
    stop = getattr(spawner, "stop", None)
    for handle in handles.values():
        if stop is not None:
            try:
                stop(handle)
            except Exception:  # noqa: BLE001, S110 - best effort while unwinding
                pass


async def _stop_all_off_loop(spawner, handles):
    """``_stop_all`` from a coroutine, on a worker thread.

    Both spawners take real time per handle: ``ProcessSpawner.stop`` walks and suspends the whole
    process tree, then waits out two five-second grace periods, and ``PaneSpawner.stop`` talks to
    the panel daemon. A four-node level is tens of seconds, and the loop this unwinds on is often
    not the research driver's own -- ``last_order.research`` starts ``workflow.run`` as a task on
    Last Order's loop, so this cleanup ran in the middle of her streaming and her inbox.

    Shielded, because the commonest way to arrive here is that very driver being cancelled when
    her session shuts down: an unshielded await would be cancelled before the executor picked the
    job up, and the processes would simply be left running. The thread cannot be cancelled once it
    starts, so a cancelled caller stops waiting while the kill still finishes.
    """
    if not handles:
        return
    try:
        await asyncio.shield(asyncio.to_thread(_stop_all, spawner, handles))
    except asyncio.CancelledError:      # the kill is running in its thread; the caller re-raises
        pass


async def _wait_probes(con, cfg, spawner, run, handles, *, poll_seconds):
    """Wait until every fork has written its verdict (or the run halted). If one fork's process
    dies, the others are stopped before the error propagates."""
    try:
        return await _wait_probes_inner(con, cfg, spawner, run, handles, poll_seconds=poll_seconds)
    except BaseException:
        await _stop_all_off_loop(spawner, handles)
        raise


async def _wait_probes_inner(con, cfg, spawner, run, handles, *, poll_seconds):
    while handles:
        await asyncio.sleep(poll_seconds)
        halted = (runs.stop_requested(con, run["id"])
                  or budget.status(con, cfg.get("token_cap"))["mode"] == "stop")
        for iid, handle in list(handles.items()):
            if runs.issue(con, iid)["status"] != "probing":
                handles.pop(iid)
            elif not spawner.alive(handle):
                if halted:
                    handles.pop(iid)
                else:
                    raise RuntimeError(f"The fork on issue {iid} ended without a verdict; resume the run to retry it.")
    if runs.stop_requested(con, run["id"]):
        return "stopped"
    if budget.status(con, cfg.get("token_cap"))["mode"] == "stop":
        return "budget"
    return "done"


async def expand_node(con, cfg, runner, worker, *, run_id, node_id, spawner, progress=None):
    """One node's routine, as run by its own process (misaka.research.node)."""
    runs.init(con)
    run, node = runs.get(con, run_id), runs.node(con, node_id)
    if not run or not node:
        raise ValueError(f"Research node not found: {run_id}/{node_id}")
    return await _expand(con, dict(cfg), runner, worker, run, node, spawner=spawner, context=None,
                         tool_call_id=f"research:{run_id}:{node_id}", poll_seconds=POLL_SECONDS,
                         progress=progress)


def _probe_gave_up(con, issue_id, reason):
    """A fork whose cards failed still leaves a verdict. Its node waits on the issue's status, so
    a verdict-less fork whose process is gone kills that node -- and with it the run."""
    verdict = {"verdict": "inconclusive", "reason": reason}
    runs.set_issue(con, issue_id, verdict["verdict"], reason=verdict["reason"])
    return verdict["verdict"]


def _probe_rounds_done(con, run_id, issue_id):
    """How many rounds this fork already opened cards for, read off the scoped local ids."""
    rounds = 0
    for t in runs.tasks(con, run_id, issue_id=issue_id):
        found = re.match(rf"{re.escape(issue_id)}\.r(\d+)\.", t["local_id"] or "")
        if found:
            rounds = max(rounds, int(found.group(1)))
    return rounds


async def probe(con, cfg, runner, worker, *, run_id, issue_id, progress=None, poll_seconds=POLL_SECONDS):
    """Last Order's fork on one issue, as run by its own process: open cards, read what they
    returned, judge. Ends with the issue's verdict written; halts propagate like a node's."""
    runs.init(con)
    run, issue = runs.get(con, run_id), runs.issue(con, issue_id)
    if not run or not issue:
        raise ValueError(f"Research issue not found: {run_id}/{issue_id}")
    node = runs.node(con, issue["branch_id"])
    synthesis_path = _artifact_path(con, run, node, "synthesis")
    # A resumed fork continues where it stopped: open cards of the last round are driven first,
    # and the round counter comes from the cards on record, so the three-round cap holds.
    unfinished = {t["id"] for t in runs.tasks(con, run_id, issue_id=issue_id) if t["status"] != "done"}
    if unfinished:
        outcome = await _drive_tasks(con, dict(cfg), runner, run_id, scope=unfinished,
                                     tool_call_id=f"research:{run_id}:{issue_id}", poll_seconds=poll_seconds,
                                     progress=progress)
        if outcome == "failed":
            return _probe_gave_up(con, issue_id, "The fork's open cards failed; no verdict could be reached.")
        if outcome != "done":
            return outcome
        settle_done_tasks(con, run_id=run_id)
    for round_no in range(_probe_rounds_done(con, run_id, issue_id) + 1, MAX_PROBE_ROUNDS + 1):
        cards = [t for t in runs.tasks(con, run_id, issue_id=issue_id) if t["status"] == "done"]
        await _progress(progress, "probe", f"Fork on issue {issue_id}: round {round_no}, {len(cards)} card(s) in.", run)
        tasks, verdict = await asyncio.to_thread(
            planner.probe_step, con, run, dict(cfg), worker, node, issue, cards,
            synthesis_path=synthesis_path, round_no=round_no, rounds=MAX_PROBE_ROUNDS)
        if verdict:
            runs.set_issue(con, issue_id, verdict["verdict"], reason=verdict["reason"])
            return verdict["verdict"]
        opened = await _submit_tasks(con, run, dict(cfg), worker, node, tasks, kind="probe", issue_id=issue_id,
                                     evidence=planner.evidence_block(con, run, node), progress=progress,
                                     local_prefix=f"{issue_id}.r{round_no}.")
        outcome = await _drive_tasks(con, dict(cfg), runner, run_id, scope=set(opened.values()),
                                     tool_call_id=f"research:{run_id}:{issue_id}", poll_seconds=poll_seconds,
                                     progress=progress)
        if outcome == "failed":
            return _probe_gave_up(con, issue_id,
                                  f"Round {round_no}'s cards failed; no verdict could be reached.")
        if outcome != "done":
            return outcome
        settle_done_tasks(con, run_id=run_id)
    # The last round's cards came back too: Last Order judges them before the fork gives up.
    cards = [t for t in runs.tasks(con, run_id, issue_id=issue_id) if t["status"] == "done"]
    verdict, reason = None, f"No verdict after {MAX_PROBE_ROUNDS} rounds of cards."
    try:
        _more, verdict = await asyncio.to_thread(
            planner.probe_step, con, run, dict(cfg), worker, node, issue, cards,
            synthesis_path=synthesis_path, round_no=MAX_PROBE_ROUNDS + 1, rounds=MAX_PROBE_ROUNDS, final=True)
    except (RuntimeError, ValueError) as error:
        reason = f"{reason[:-1]} ({error})."
    if verdict is None:
        verdict = {"verdict": "inconclusive", "reason": reason}
    runs.set_issue(con, issue_id, verdict["verdict"], reason=verdict["reason"])
    return verdict["verdict"]


def _record_runner(con, table, row_id, handle):
    """Persist a spawned child's identity so a successor driver can see and reap it."""
    pid = getattr(handle, "pid", None)
    if pid is None:
        return                                   # a pane handle: the panel daemon owns that process
    from misaka.platform import processes
    con.execute(f'UPDATE "{table}" SET runner_pid=?, runner_identity=? WHERE id=?',
                (int(pid), processes.identity(int(pid)), row_id))


def _reap_orphan_runner(con, table, row):
    """A previous driver's child may still be running this node or probe. Never start a second
    one beside it: terminate the recorded tree first (its own claim fences any late writes).

    Blocking, and called from a worker thread for it: ``processes.terminate`` suspends the whole
    tree, signals it, and waits out its grace periods before the fallback kill."""
    keys = row.keys() if hasattr(row, "keys") else row
    pid = row["runner_pid"] if "runner_pid" in keys else None
    identity = row["runner_identity"] if "runner_identity" in keys else None
    if pid and identity:
        from misaka.platform import processes
        if processes.identity_is_alive(int(pid), identity):
            processes.terminate(int(pid))
    if pid or identity:
        con.execute(f'UPDATE "{table}" SET runner_pid=NULL, runner_identity=NULL WHERE id=?', (row["id"],))


async def _expand_level(con, cfg, spawner, run, level, *, poll_seconds, progress, driver_lock=None):
    """Spawn every node of the level and wait until each has left the frontier (terminal,
    closing, or waiting for input). Returns "done", a halt, or a waiting_input result. If one
    node's process dies, the level's other processes are stopped before the error propagates."""
    handles = {}
    try:
        for node in level:                              # registered one by one: a failed spawn stops the started ones
            await asyncio.to_thread(_reap_orphan_runner, con, "research_branches", node)
            handle = spawner.spawn(_node_argv(run, node), cwd=run["workspace"],
                                   title=f"LO·{node['id']}", place="split")
            handles[node["id"]] = handle
            _record_runner(con, "research_branches", node["id"], handle)
        return await _wait_level(con, cfg, spawner, run, handles, poll_seconds=poll_seconds, progress=progress,
                                 driver_lock=driver_lock)
    except BaseException:
        await _stop_all_off_loop(spawner, handles)
        raise


async def _wait_level(con, cfg, spawner, run, handles, *, poll_seconds, progress, driver_lock=None):
    last = {}
    while handles:
        await asyncio.sleep(poll_seconds)
        if driver_lock and not runs.heartbeat_driver(con, run["id"], driver_lock):
            raise RuntimeError(f"Research run {run['id']}: the driver lease was taken over by another process.")
        halted = (runs.stop_requested(con, run["id"])
                  or budget.status(con, cfg.get("token_cap"))["mode"] == "stop")
        for nid, handle in list(handles.items()):
            node = runs.node(con, nid)
            if node["status"] != last.get(nid):
                last[nid] = node["status"]
                await _progress(progress, "node", f"{_label(node)}: {node['status']}.", run, node=nid)
            if node["status"] in ("closing", "waiting_input", "conflict", *runs.NODE_TERMINAL):
                handles.pop(nid)
            elif not spawner.alive(handle):
                if halted:
                    handles.pop(nid)
                else:
                    raise RuntimeError(f"Node {nid}'s process ended while {node['status']}; resume the run to retry it.")
    waiting = [n for n in runs.nodes(con, run["id"]) if n["status"] == "waiting_input"]
    if waiting:
        questions = [f"[{n['id']}] {q}" for n in waiting
                     for q in ((_json_artifact(con, run, n, "plan_json") or {}).get("clarifying_questions") or [])]
        runs.set_state(con, run["id"], phase="waiting_input", status="waiting_input")
        return {"reason": "waiting_input", "questions": questions, "run": runs.summary(con, run["id"])}
    if runs.stop_requested(con, run["id"]):
        return "stopped"
    if budget.status(con, cfg.get("token_cap"))["mode"] == "stop":
        return "budget"
    return "done"


async def run(con, cfg, spawner, worker, *, run_id, poll_seconds=POLL_SECONDS, progress=None):
    """Advance one persisted research run until it finishes, stops, or needs user input.
    Nodes are processes (misaka.research.node); ``spawner`` starts one and says whether it lives."""
    runs.init(con)
    run = runs.get(con, run_id)
    if not run:
        raise ValueError(f"Research run not found: {run_id}")
    if not os.path.isdir(run["workspace"]):
        raise RuntimeError(f"The research run's project folder no longer exists: {run['workspace']}")
    driver_lock = f"driver:{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(3)}"
    if not runs.acquire_driver(con, run_id, driver_lock):
        raise RuntimeError(f"Research run {run_id} is already being driven by another process "
                           f"(lease {run['driver_lock']}); wait for it or stop that process.")
    runs.ensure_layout(run)
    cfg = dict(cfg)
    halts = {"stopped": "The user requested a stop.", "budget": "The shared token budget limit was reached."}
    lost = asyncio.Event()
    keeper = asyncio.create_task(_keep_lease(con, run_id, driver_lock, lost))
    try:
        _settle_conflicts(con, run)
        if runs.stop_requested(con, run_id):
            return _partial_result(con, run, halts["stopped"])
        if budget.status(con, cfg.get("token_cap"))["mode"] == "stop":
            return _partial_result(con, run, halts["budget"])
        _refresh_workspace_index(con, run)
        if run["phase"] == "created":
            runs.set_state(con, run_id, phase="active", driver_lock=driver_lock)
        while True:                                   # breadth-first: one level at a time, its nodes in parallel
            if lost.is_set():
                raise RuntimeError(f"Research run {run_id}: the driver lease was taken over by another process.")
            _settle_closing(con, run)
            level = runs.next_level(con, run_id)
            if not level:
                break
            runs.set_state(con, run_id, wave=level[0]["depth"], driver_lock=driver_lock)
            await _progress(progress, "level", f"Depth {level[0]['depth']}: {len(level)} node(s) expanding.", run,
                            nodes=[n["id"] for n in level])
            result = await _expand_level(con, cfg, spawner, run, level, poll_seconds=poll_seconds, progress=progress,
                                         driver_lock=driver_lock)
            if isinstance(result, dict):
                return result
            if result in halts:
                settle_done_tasks(con, run_id=run_id)
                return _partial_result(con, run, halts[result])
        if lost.is_set():
            raise RuntimeError(f"Research run {run_id}: the driver lease was taken over by another process.")
        unfinished = _unfinished_reason(con, run_id)
        if unfinished:
            settle_done_tasks(con, run_id=run_id)
            await _progress(progress, "unfinished", f"The run cannot be adjudicated: {unfinished}", run)
            return _partial_result(con, run, unfinished, status="failed")
        runs.set_state(con, run_id, phase="finalizing", driver_lock=driver_lock)
        await _progress(progress, "finalizing", "Every node is closed; Last Order is adjudicating the final report.", run)
        result = await asyncio.to_thread(report.finalize, con, run, cfg, worker)
        runs.set_state(con, run_id, phase="done", status="done", final_artifact=result["artifact"],
                       driver_lock=driver_lock)
        _refresh_workspace_index(con, runs.get(con, run_id))
        return {"reason": "done", "final": result, "run": runs.summary(con, run_id)}
    except Exception as error:
        runs.set_state(con, run_id, status="failed", error=f"{type(error).__name__}: {error}"[:500],
                       driver_lock=driver_lock)               # a stale driver's word no longer lands
        raise
    finally:
        keeper.cancel()
        runs.release_driver(con, run_id, driver_lock)

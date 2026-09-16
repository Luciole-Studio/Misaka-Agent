"""Persistent Research Workflow: breadth-first over a depth-bounded tree of conclusions.

Every node follows the same routine: Last Order plans and assigns Sisters, synthesizes their
research, receives the chosen Sister's red-team review, and directly dispatches each material
issue to a fork of her session at depth + 1. That fork is the child node's Last Order from the
start, not a preliminary investigator. No verdict round or second fork precedes its research.
Code enforces phase order, depth, persistence, and artifact integrity; models make the judgements.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import socket
import sys
from datetime import UTC, datetime
from graphlib import CycleError, TopologicalSorter
from pathlib import Path

from misaka import workspace as workspace_index
from misaka.core.platform import budget
from misaka.core.platform import cards as card_files
from misaka.core.platform import tasks as task_store
from misaka.core.research import bundle, commands, ledger, planner, report, runs
from misaka.core.research import context as context_packet
from misaka.utils.async_lifecycle import settle, settle_thread_call

POLL_SECONDS = 2.0
_LOG = logging.getLogger(__name__)
ACTIVE_TASKS = ("running", "review")
# Statuses on which a *missing* plan edge is history rather than a fault: the card is finished (or
# gone) and has nothing left to wait for. `running` is excluded on purpose -- see `_submit_tasks`.
_EDGES_ARE_HISTORY = runs.SETTLED_TASK_STATUSES - {"running"}
RESEARCH_DISCIPLINE = """[Research Workflow active]
Last Order is now in Research mode. Each node of the research tree runs the same routine: Last Order plans and assigns
Sisters (each Sister plans and executes within its own task session); once their cards are back she either writes the
node's conclusion or sends Sisters out for another round first (up to the run's follow-up limit), and the red-team
Sister she named returns her review to that Last Order. Follow the current plan-approval policy: when enabled for a
resident session, each root, fork or follow-up plan waits for its own approval; otherwise the driver proceeds after
an accepted ready plan. Last Order explicitly dispatches a fork of her session for each material issue;
each fork IS a child node at depth + 1 and performs the same full routine with its own Sisters and red team.
There is no preliminary investigation or second fork. The tree expands breadth-first; max_depth is the
largest allowed depth (root = 0). At that depth red-team review still runs, but its issues remain parked
for final adjudication rather than spawning more research.

Code enforces phase order, depth, persistence, and artifact integrity. The models keep the judgement calls: framing, methods,
Sister selection, source quality, task count, and what shakes a conclusion. Planning produces a design, not an answer.
Node synthesis produces a working conclusion for independent review. Final adjudication produces the report for delivery,
keeping competing conclusions side by side wherever the evidence cannot decide between them.
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
    """Artifact scope: root artifacts carry no branch id (their folder is named by the run)."""
    return node["id"] if node["parent_id"] else None


def _scope(node):
    return {"branch_id": node["id"]} if node["parent_id"] else {"root_only": True}


def _write(con, run, node, kind, title, name, content, **metadata):
    with runs.owned_txn(con, run, node):
        return runs.write_text(con, run["id"], kind, title, runs.generated_path(run, name, branch_id=_bid(node)), content,
                               branch_id=_bid(node), metadata=metadata or None)


def _bundle(con, run, *, task=None, node=None):
    """Gather sources beside a settled card, a closed node, or the run's delivered document.
    A bundle is derived from what the ledger and the project already hold, so its failure is
    recorded (an event on the card, a log line otherwise) and the workflow carries on: no card,
    node or run state depends on it."""
    try:
        if task is not None:
            bundle.card_bundle(con, run, task)
        elif node is not None:
            bundle.node_bundle(con, run, node)
        else:
            bundle.final_bundle(con, run)
    except Exception as error:  # noqa: BLE001 - derived output must never undo a settled state
        if task is not None:
            task_store.add_event(con, task["id"], "bundle_error", {"error": str(error)[:200]},
                                 generation=int(task["generation"]))
        else:
            _LOG.warning("research %s: sources bundle for %s failed: %s",
                         run["id"], node["id"] if node is not None else "final/", error)


def _artifact_path(con, run, node, kind):
    rows = runs.artifacts(con, run["id"], kind=kind, **_scope(node))
    return runs.artifact_path(rows[-1]) if rows else None


def _artifact_paths(con, run, node, kind):
    """Every artifact of a kind on the node, oldest first, for a prompt: a node that planned
    more than once has more than one plan."""
    rows = runs.artifacts(con, run["id"], kind=kind, **_scope(node))
    return ", ".join(f"`{runs.artifact_path(row)}`" for row in rows) if rows else "`(none)`"


def _write_plan(con, run, node, plan, round):
    """The round's plan as Markdown and JSON: ``plan.md`` for the first round, ``plan-<n>.md`` after."""
    suffix = "" if round <= 1 else f"-{round}"
    title = "Research plan" if round <= 1 else f"Research plan (round {round})"
    _write(con, run, node, "plan", title, f"plan{suffix}.md", plan["plan_markdown"].rstrip() + "\n")
    _write(con, run, node, "plan_json", title + " (JSON)", f"plan{suffix}.json",
           json.dumps(plan, ensure_ascii=False, indent=2))


def _forget_plan(con, run, node, round):
    """Drop a withdrawn follow-up round's plan files and their registrations: the round never
    ran, and a plan nobody executed must not sit beside the ones that did (the red team and the
    final report read every plan of a node). Only rounds after the first can be withdrawn."""
    if round <= 1:
        return
    with runs.owned_txn(con, run, node):
        names = {f"plan-{round}.md", f"plan-{round}.json"}
        for row in runs.artifacts(con, run["id"], **_scope(node)):
            if row["kind"] in ("plan", "plan_json") and os.path.basename(row["path"]) in names:
                con.execute("DELETE FROM research_artifacts WHERE id=?", (row["id"],))
                with contextlib.suppress(OSError):
                    os.unlink(row["path"])


def _followup_recorded(con, run, node, round):
    return runs.plan_round(con, run["id"], node["id"]) > round


def _followup_tool(con, cfg, run, node, round):
    """The next round's plan tool for the synthesis turn: recorded like any phase command, and
    read by the driver as the node's decision to research more before concluding."""
    current = runs.node(con, node["id"])
    return commands.tool(
        con, run, node, key=runs.plan_key(round + 1), name="misaka_research_assign",
        description=f"Last Order: assign a follow-up round of research cards (round {round + 1}) on this node",
        model=commands.Plan,
        validate=lambda value: planner.validate_plan(value, planner._roster({**cfg, "workspace": run["workspace"]})),
        session_dir=planner._lo_session(run, node),
        session_file=run["root_session"] if node["parent_id"] is None else current["session_file"],
        supersede=True)


def _refresh_workspace_index(con, run):
    """Export a dated navigation snapshot at a run boundary; agents use the live view."""
    tree = workspace_index.outline(
        con, workspace=run["workspace"], run_id=run["id"], research_store=runs)
    tree["snapshot_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    runs.write_text(
        con, run["id"], "workspace_index_json", 'Project / PageIndex workspace index',
        runs.run_path(run, "workspace-index.json"), json.dumps(tree, ensure_ascii=False, indent=2),
    )
    return runs.write_text(
        con, run["id"], "workspace_index", 'Project / PageIndex workspace index',
        runs.run_path(run, "workspace-index.md"), '# Project / PageIndex workspace index\n\n'
        f"Snapshot at {tree['snapshot_at']}; not live state.\n"
        f'For current state, call misaka_research_view(view="workspace", run_id="{run["id"]}").\n\n```text\n'
        + workspace_index.render(tree) + "\n```\n",
    )


def _try_refresh_workspace_index(con, run):
    try:
        return _refresh_workspace_index(con, run)
    except InterruptedError:
        raise
    except OSError:
        _LOG.warning("Research %s: derived workspace index could not be written", run["id"], exc_info=True)


def _node_argv(run, node, key):
    return [sys.executable, "-m", "misaka", "research", "--node", run["id"], node["id"], "--runner-key", key]


def _label(node):
    return "the root question" if node["parent_id"] is None else f"node {node['id']}"


def _round_specs(specs, round):
    """A later round's cards carry the round in their local ids (``r2/source``): the ids are the
    plan's own namespace, and a round-two card named like a round-one card is a new card."""
    if round <= 1:
        return list(specs)
    prefix = f"r{round}/"
    return [{**spec, "local_id": prefix + spec["local_id"],
             "dependencies": [prefix + dep for dep in (spec.get("dependencies") or [])]} for spec in specs]


async def _submit_tasks(con, run, node, specs, *, kind, progress=None, round=1, previous=()):
    """Create the LO's cards directly in dependency order; each Sister plans in its own session.

    Card, research link and output directory land together, with every dependency present in
    the first published file. A resumed dispatch reuses cards already created. ``round`` is the
    node's planning round; ``previous`` the earlier rounds' cards the new ones build on.
    """
    root = run["workspace"]
    specs = _round_specs(specs, round)
    local_to_task = {}
    by_id = {spec["local_id"]: spec for spec in specs}
    graph = {lid: spec.get("dependencies") or [] for lid, spec in by_id.items()}
    missing = {dep for deps in graph.values() for dep in deps} - by_id.keys()
    if missing:
        raise RuntimeError(f"Research dependency was not created: {sorted(missing)}")
    try:
        ordered = [by_id[lid] for lid in TopologicalSorter(graph).static_order()]
    except CycleError as error:
        raise RuntimeError("Research task dependencies contain a cycle.") from error
    for spec in ordered:
        if runs.stop_requested(con, run["id"]):
            break
        with runs.owned_txn(con, run, node):
            existing = con.execute(
                "SELECT task_id FROM research_run_tasks WHERE run_id=? AND branch_id=? AND local_id=?",
                (run["id"], node["id"], spec["local_id"]),
            ).fetchone()
            if existing and task_store.get(con, existing["task_id"]) is not None:
                local_to_task[spec["local_id"]] = existing["task_id"]   # a resume after a crash: the card already exists
                continue
            if existing:                                   # the card was deleted: drop the stale link and rebuild it
                con.execute("DELETE FROM research_run_tasks WHERE task_id=?", (existing["task_id"],))
            tid = card_files.create(
                con, root, spec["title"],
                planner.task_body(spec, run_id=run["id"], node=node, siblings=specs, previous=previous),
                spec["assignee"], priority=spec.get("priority", 0),
                needs=[local_to_task[dep] for dep in spec.get("dependencies") or []],
                after_row=lambda tid, spec=spec: runs.link_task(   # linked before the file exists
                    con, run["id"], tid, kind=kind, node=node, round=round,
                    local_id=spec["local_id"], dependencies=spec.get("dependencies") or []))
        local_to_task[spec["local_id"]] = tid
        await _progress(progress, "assigned", f"Created {kind} card {tid}: {spec['title']} → Sister {spec['assignee']}.",
                        run, task_id=tid, local_id=spec["local_id"], dependencies=spec.get("dependencies") or [])
    if runs.stop_requested(con, run["id"]):
        return local_to_task
    # depends_json keeps Last Order's plan as written; the cards' frontmatter `needs` is the executable projection of it.
    for spec in specs:
        if spec["local_id"] not in local_to_task:
            continue
        dependencies = spec.get("dependencies") or []
        # The plan's own completeness is checked for every spec, whatever the card is doing: a
        # dependency Last Order named and nobody created is a broken plan, and a card that had
        # already settled must not be the reason we stop looking.
        for dependency in dependencies:
            if dependency not in local_to_task:
                raise RuntimeError(f"Research dependency was not created: {dependency}")
        child = task_store.get(con, local_to_task[spec["local_id"]])
        # A resume replays this pass over the cards of the first attempt: a node that failed after
        # its research was done comes back through `planning` with every card already finished, and
        # an edge that never reached one of those is history now -- nothing is left to wait for. So
        # skip it, the same guard and the same reason as `runs._backfill_dependencies`. `running`
        # is deliberately not in that set: a card someone claimed between its creation here and
        # this pass still has its whole job ahead of it, so a *missing* edge on it is a real
        # ordering fault and `link_tasks` should say so out loud. (An edge it already carries
        # short-circuits inside `link_tasks` before any status check, so a plain replay is quiet.)
        if child is None or child["status"] in _EDGES_ARE_HISTORY:
            continue
        with runs.owned_txn(con, run, node):
            task_store.link_dependencies(
                con, [local_to_task[dep] for dep in dependencies], child["id"])
    return local_to_task


def _release_dependencies(con, run_id, *, owner=None):
    """Promote every waiting card whose dependencies are settled. Blocking, and called from a
    worker thread for it: ``dependency_state`` reaches ``task_store.parent_ids``, which reads and
    parses the card *file* of every todo card -- the frontmatter ``needs`` is the executable
    contract, so there is no table to consult -- and this runs twice per two-second tick."""
    for row in runs.tasks(con, run_id):
        if row["status"] != "todo":
            continue
        state, parents = task_store.dependency_state(con, row["id"])
        with task_store.write_txn(con):
            if owner is not None:
                runs.check_owner(con, *owner)
            current = task_store.get(con, row["id"])
            if current is None or current["generation"] != row["generation"] or current["status"] != "todo":
                continue
            if state == "failed":
                task_store.mark_stopped(con, row["id"])
                task_store.add_event(con, row["id"], "dependency_failed", {"parents": parents})
            else:
                task_store.promote_task(con, row["id"])


def _stop_pending(con, linked, *, owner=None):
    """Hold back every card of a halted scope that had not started yet.

    Off the loop thread as one hop rather than one per card: ``mark_stopped`` mirrors the card file
    and commits it, and ``repo.commit`` retries up to four git subprocesses with a growing sleep
    between them while ``index.lock`` is contended. A stop with fifty linked cards was tens of
    seconds of frozen loop. A card the reconciler has just accepted is in ``review`` or ``done``
    and is deliberately not touched here.
    """
    for row in linked:
        if row["status"] in {"ready", "todo"}:
            with task_store.write_txn(con):
                if owner is not None:
                    runs.check_owner(con, *owner, allow_stop=True)
                current = task_store.get(con, row["id"])
                if (current is not None and current["generation"] == row["generation"]
                        and current["status"] in {"ready", "todo"}):
                    task_store.mark_stopped(con, row["id"], generation=row["generation"])


async def _stop_active(runner, task_ids, context, *, captured=None, owner=None):
    stop = getattr(runner, "stop", None)
    if not callable(stop):
        return
    outcomes = await asyncio.gather(
        *(stop(task_id, confirmed=True, context=context,
               **({"research_owner": {"run_id": owner[0]["id"], "driver_lock": owner[0]["driver_lock"],
                                      "node_id": owner[1]["id"] if owner[1] is not None else None,
                                      "runner_key": owner[1]["runner_key"] if owner[1] is not None else None}}
                  if owner is not None else {}),
               **({"expected_generation": captured[task_id]["generation"],
                   "expected_claim_lock": captured[task_id]["claim_lock"]}
                  if captured is not None and task_id in captured else {})) for task_id in task_ids),
        return_exceptions=True,
    )
    errors = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    if errors:
        raise ExceptionGroup("Research card stop failed", errors)


async def _halt_scope(con, cfg, runner, captured, context, *, owner=None):
    from misaka.core.network import dispatch
    active = {tid for tid, row in captured.items() if row["status"] in ACTIVE_TASKS}
    try:
        await _stop_active(runner, active, context, captured=captured, owner=owner)
    finally:
        await asyncio.to_thread(dispatch.reconcile, con, cfg)
        await asyncio.to_thread(_stop_pending, con, list(captured.values()), owner=owner)


def _help_waiting(con, row):
    """True only for a card parked by its own current SendMessage help request."""
    if row["status"] not in {"blocked", "triage"} or row["block_kind"] != "needs_input":
        return False
    try:
        payload = json.loads(
            task_store.latest_payload(
                con,
                row["id"],
                row["status"],
                generation=row["generation"],
            )
            or "{}"
        )
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("message_id") is not None


async def _wait_unpaused(con, run_id, session):
    from misaka.core.session_control import for_session

    control = for_session(session) if session is not None else None
    while control is not None and control.paused and not runs.stop_requested(con, run_id):
        control.check_active()
        await asyncio.sleep(.1)


async def _drive_tasks(con, cfg, runner, run_id, *, scope, context=None,
                       tool_call_id="research", poll_seconds=POLL_SECONDS, progress=None, check_active=None, session=None, owner=None):
    """Drive the cards in ``scope`` (task ids) until none is open; tasks waiting on dependencies stay in ``todo``."""
    captured = {row["id"]: row for row in runs.tasks(con, run_id) if row["id"] in scope}
    try:
        return await _drive_tasks_inner(con, cfg, runner, run_id, scope=scope, context=context,
                                       tool_call_id=tool_call_id, poll_seconds=poll_seconds,
                                       progress=progress, check_active=check_active, session=session,
                                       captured=captured, owner=owner)
    except BaseException:
        # The same unwind serves root/fork and normal error/cancellation. Drain it
        # through repeated cancellation, before the caller releases its driver/node.
        try:
            if check_active:
                check_active()
            await settle(asyncio.create_task(_halt_scope(con, cfg, runner, captured, context, owner=owner)))
        except Exception:  # noqa: BLE001 - retain the original failure after cleanup
            _LOG.exception("Research card cleanup did not complete (or owner was superseded)")
        raise


async def _drive_tasks_inner(con, cfg, runner, run_id, *, scope, context=None,
                       tool_call_id="research", poll_seconds=POLL_SECONDS, progress=None, check_active=None, session=None, captured=None, owner=None):
    """Drive the cards in ``scope`` (task ids) until none is open; tasks waiting on dependencies stay in ``todo``."""
    last_snapshot = None
    while True:
        await _wait_unpaused(con, run_id, session)
        if check_active:
            check_active()
        run = runs.get(con, run_id)
        linked = [row for row in runs.tasks(con, run_id) if row["id"] in scope]
        for row in linked:
            previous = captured.get(row["id"])
            if previous is not None and previous["generation"] != row["generation"]:
                raise ValueError(f"Research card {row['id']} changed generation")
            captured[row["id"]] = row
        snapshot = tuple((row["id"], row["status"]) for row in linked)
        if snapshot != last_snapshot:
            counts = {}
            for _task_id, status in snapshot:
                counts[status] = counts.get(status, 0) + 1
            text = ','.join(f"{status} {count}" for status, count in sorted(counts.items()))
            await _progress(progress, "tasks", f"Task status: {text or 'no tasks'}.", run,
                            counts=counts, task_ids=sorted(scope))
            last_snapshot = snapshot
        active = [row["id"] for row in linked if row["status"] in ACTIVE_TASKS]
        pending = getattr(runner, "pending", None)
        flying = set(await pending(scope)) if pending is not None else set()
        halt = ("stopped" if runs.stop_requested(con, run_id) else
                "budget" if budget.exhausted(con, cfg.get("token_cap")) else None)
        if halt:
            await _halt_scope(con, cfg, runner, captured, context, owner=owner)
            return halt
        if check_active:
            check_active()
        await asyncio.to_thread(_release_dependencies, con, run_id, owner=owner)
        linked = [row for row in runs.tasks(con, run_id) if row["id"] in scope]
        waiting = [row["id"] for row in linked if _help_waiting(con, row)]
        # Per-node Sister slots are independent of the run's LO-node parallelism.
        free = max(0, max(1, int(cfg.get("research_parallel", 4)))
                   - len(flying | {row["id"] for row in linked if row["status"] in ACTIVE_TASKS}
                         | set(waiting)))
        ready = [row["id"] for row in linked if row["status"] == "ready"][:free]
        active = [row["id"] for row in linked if row["status"] in ACTIVE_TASKS]
        if ready or active or waiting or flying:
            if check_active:
                check_active()
            _result, cancelled = await settle(asyncio.create_task(runner.launch_ready(
                context=context, tool_call_id=tool_call_id, task_ids=[*ready, *active, *waiting])))
            for tid in [*ready, *active, *waiting]:
                row = task_store.get(con, tid)
                if row is not None and row["generation"] == captured[tid]["generation"]:
                    captured[tid] = row
            if cancelled is not None:
                raise cancelled
            await asyncio.sleep(poll_seconds)
            continue
        if any(row["status"] == "todo" for row in linked):
            await asyncio.to_thread(_release_dependencies, con, run_id, owner=owner)
            if any(row["status"] == "todo" and row["id"] in scope for row in runs.tasks(con, run_id)):
                raise RuntimeError("Research task dependencies cannot advance; the graph may contain a cycle.")
            continue
        complete_scope = bool(scope) and {row["id"] for row in linked} == set(scope)
        return "done" if complete_scope and all(row["status"] == "done" for row in linked) else "failed"


def _submitted_payload(con, task):
    try:
        payload = json.loads(task_store.latest_payload(
            con, task["id"], "submitted", generation=task["generation"]
        ) or "{}")
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


ARTIFACT_ROOTS = ("nodes", "final")


def _register_task_artifacts(con, run, task, submitted):
    """Register submitted project files at their original location. Nothing is copied or moved.

    Only files under the by-node layout (``nodes/``, ``final/``) are a card's outputs. A Sister
    who lists a page from the shared ``downloads/`` cache, or a file at the project root, has
    named a source, not a product: those are gathered by the bundles beside her outputs and
    are neither registered nor committed with the node. Each such path is recorded on the card."""
    link = con.execute("SELECT * FROM research_run_tasks WHERE task_id=?", (task["id"],)).fetchone()
    if not link or not task["workspace"]:
        return
    node = runs.node(con, link["branch_id"])
    for rel in submitted.get("artifacts") or []:
        source = Path(task["workspace"], str(rel)).resolve()
        try:
            inside = source.relative_to(Path(run["workspace"]).resolve())
        except ValueError as error:
            if (submitted.get("artifact_digests") or {}).get(str(rel)) is not None:
                raise ValueError(f"Accepted artifact moved outside the workspace: {rel}") from error
            continue
        if inside.parts[:1] not in {(root,) for root in ARTIFACT_ROOTS}:
            task_store.add_event(con, task["id"], "artifact_outside_layout", {"path": str(rel)},
                                 generation=int(dict(task).get("generation") or 1))
            continue
        try:
            raw = source.read_bytes()
        except OSError as error:
            if "artifact_digests" in submitted:
                raise ValueError(f"Accepted artifact is missing: {rel}") from error
            continue
        digest = hashlib.sha256(raw).hexdigest()
        if "artifact_digests" in submitted and submitted["artifact_digests"].get(str(rel)) != digest:
            raise ValueError(f"Accepted artifact changed before Research registration: {rel}")
        try:
            raw.decode("utf-8")
            binary = False
        except UnicodeDecodeError:
            # Keep binary deliverables by identity. Document tools read their content;
            # registration neither decodes them as prose nor certifies any quotation.
            binary = True
        kind = {"red_team": "critique", "final_review": "final_critique"}.get(link["kind"], "task_output")
        runs.register_file(con, run["id"], kind, f"[{task['id']}] {source.name}",
                           str(source), sha256=digest,
                           branch_id=_bid(node), task_id=task["id"],
                           metadata={"source_file": str(rel),
                                     "submission_digest_verified": "artifact_digests" in submitted,
                                     **({"binary": True} if binary else {})})


def _clear_task_outputs(con, run_id, task_id):
    """Drop the derived result set that an older generation of this task supplied."""
    con.execute(
        "DELETE FROM research_claims WHERE finding_id IN ("
        "SELECT id FROM research_findings WHERE run_id=? AND task_id=?)",
        (run_id, task_id),
    )
    con.execute(
        "DELETE FROM research_findings WHERE run_id=? AND task_id=?",
        (run_id, task_id),
    )
    con.execute(
        "DELETE FROM research_artifacts WHERE run_id=? AND task_id=? "
        "AND kind IN ('task_output','critique','final_critique')",
        (run_id, task_id),
    )


def settle_done_tasks(con, *, run_id):
    """Register accepted artifacts and ingest the Sisters' findings, once per task generation."""
    run = runs.get(con, run_id)
    for task in runs.tasks(con, run_id):
        if task["status"] != "done":
            continue
        generation = int(task["generation"])
        if task_store.latest_payload(con, task["id"], "research_v2_settled", generation=generation):
            continue
        settled = False
        with task_store.write_txn(con):
            # The unlocked scan is only a fast path. Another LO may have settled or
            # resumed this card before we acquired the writer lock.
            current = task_store.get(con, task["id"])
            if current is None or current["generation"] != generation or current["status"] != "done":
                continue
            if task_store.latest_payload(con, task["id"], "research_v2_settled", generation=generation):
                continue
            submitted = _submitted_payload(con, current)
            _clear_task_outputs(con, run["id"], task["id"])
            _register_task_artifacts(con, run, current, submitted)
            result = {"kind": task["research_kind"]}
            if task["research_kind"] not in runs.REVIEW_KINDS:
                result = ledger.ingest_report(con, run, task, submitted)
            task_store.add_event(
                con, task["id"], "research_v2_settled", result, generation=generation
            )
            settled = True
        if settled:                                   # once per generation, after the ledger has its findings
            _bundle(con, run, task=task)


def _done(con, run, node, kind):
    return [row for row in runs.tasks(con, run["id"], kind=kind, node_id=node["id"]) if row["status"] == "done"]


def _receive_critique(con, run, node, task):
    """Receive the red team's frozen tool submission. Opening forks is a separate LO command."""
    submitted = _submitted_payload(con, task)
    if "issues" not in submitted:
        return f"Red-team card {task['id']} has not recorded issues through misaka_card_note."
    with runs.owned_txn(con, run, node):
        for item in submitted["issues"]:
            if item["material"]:
                runs.add_issue(con, run["id"], node=node, kind=item["kind"], question=item["question"],
                               rationale=item["rationale"], priority=item["priority"])
    return None


async def _return_review(con, run, node, red, issues, reason, *, session=None):
    """Record a no-dispatch review in its owning LO conversation, without waking a model."""
    from misaka.core.session_manager import SessionManager

    path = node["session_file"]
    if not path or not os.path.isfile(path):
        raise RuntimeError(f"Node {node['id']} has no persisted Last Order session for its review.")
    delivery = f"research-review:{run['id']}:{node['id']}:{red['id']}:{red['generation']}"
    content = f"# Red-team review returned\n\n{reason}\n" + await asyncio.to_thread(
        planner.review_context, con, run, red, issues)
    details = {"run_id": run["id"], "node_id": node["id"], "task_id": red["id"], "delivery_id": delivery}

    def check_owner():
        if runs.stop_requested(con, run["id"]):
            raise InterruptedError("Research stopped before the review was recorded.")
        owner = runs.node(con, node["id"])
        if (owner is None or owner["runner_key"] != node["runner_key"]
                or runs.get(con, run["id"])["driver_lock"] != run["driver_lock"]):
            raise RuntimeError("Research review belongs to a superseded node or driver.")

    if session is not None:
        while not session.isIdle:
            check_owner()
            await asyncio.sleep(0.2)
        if session.sessionManager.sessionFile != path:
            raise RuntimeError("Research window changed conversation before the review was recorded.")
    check_owner()
    if session is not None:
        # Idle delivery persists and updates the live context/UI in the same event-loop turn.
        await session.sendCustomMessage(
            {"customType": "research-review", "content": content, "display": True, "details": details},
            {"triggerTurn": False, "_deliveryId": delivery, "_onPersist": lambda: None})
    else:
        def record():
            manager = SessionManager.open(path)
            if not any(entry.get("type") == "custom_message" and
                       isinstance(entry.get("details"), dict) and entry["details"].get("delivery_id") == delivery
                       for entry in manager.getEntries()):
                manager.appendCustomMessageEntry("research-review", content, True, details)
        await asyncio.to_thread(record)


def _dispatch_children(con, run, node):
    """Project the accepted LO assignments into formal children, once even across a crash.

    The child/issue link is atomic. Session files are written outside the writer transaction;
    a fork persisted just before a crash is reused in this child's dedicated directory.
    """
    from misaka.core.session_manager import find_most_recent_session

    command = runs.action(con, run["id"], node["id"], "investigate")
    if command is None:
        raise RuntimeError(f"Node {node['id']} has no Last Order investigation dispatch command.")
    children = []
    for assignment in command["payload"]["assignments"]:
        if runs.stop_requested(con, run["id"]):
            return children
        with runs.owned_txn(con, run, node):
            child = runs.assign_child(con, run["id"], node, assignment["issue_id"])
        if not child["session_file"]:
            directory = planner._lo_session(run, child)
            session = (find_most_recent_session(directory)
                       or planner.fork_session(command["session_file"], directory))
            if not session:
                raise RuntimeError(f"Node {child['id']}: parent Last Order session has no forkable history.")
            with runs.owned_txn(con, run, node):
                child = runs.set_node(con, child["id"], session_file=session)
        children.append(child)
    return children


def _children_settled(con, run, node):
    return all(c["status"] in runs.NODE_TERMINAL for c in runs.nodes(con, run["id"], parent_id=node["id"]))


def _close(con, run, node, status):
    """Close after child completion. Artifacts are already in the project; nothing moves."""
    runs.check_owner(con, run, node)
    if status == "failed" and not runs.node(con, node["id"])["last_error"]:
        details = []
        for card in runs.tasks(con, run["id"], node_id=node["id"]):
            if card["status"] not in {"failed", "stopped"}:
                continue
            payload = task_store.latest_payload(con, card["id"], "failed", generation=card["generation"])
            details.append(f"{card['id']} ({card['status']}): {payload or 'no failure detail recorded'}")
        reason = f"{node['status']}: " + ("; ".join(details) or "node did not complete this phase")
        with runs.owned_txn(con, run, node):
            con.execute("UPDATE research_branches SET last_error=? WHERE id=?", (reason, node["id"]))
            runs.set_state(con, run["id"], error=f"node {node['id']}: {reason}")
    if status == "closed" and not _children_settled(con, run, node):
        with runs.owned_txn(con, run, node):
            runs.set_node(con, node["id"], status="closing")
        return "closing"
    _bundle(con, run, node=node)                      # the node's folder is complete: gather what it rests on
    runs.set_node(con, node["id"], status=status, owner=(run, node))
    return status


def _settle_closing(con, run):
    """Close every waiting node whose children are all terminal, deepest first (the tree's post-order)."""
    while True:
        waiting = [n for n in runs.nodes(con, run["id"])
                   if n["status"] == "closing" and _children_settled(con, run, n)]
        if not waiting:
            return
        _close(con, run, waiting[-1], "closed")


async def _expand(con, cfg, runner, worker, run, node, *, context, tool_call_id, poll_seconds, progress, session=None):
    """Advance one node through its routine. Returns the node's terminal status, a halt
    ("stopped" / "budget"), or a waiting_input result dict."""
    try:
        return await _expand_owned(con, cfg, runner, worker, run, node, context=context,
                                   tool_call_id=tool_call_id, poll_seconds=poll_seconds,
                                   progress=progress, session=session)
    except BaseException:
        async def cleanup():
            # Covers cancellation between card creation and entering the drive loop too.
            with runs.owned_txn(con, run, node, allow_stop=True):
                captured = {row["id"]: row for row in runs.tasks(con, run["id"], node_id=node["id"])}
            if captured:
                await _halt_scope(con, cfg, runner, captured, context, owner=(run, node))
        try:
            await settle(asyncio.create_task(cleanup()))
        except Exception:  # noqa: BLE001 - preserve the phase's original failure
            _LOG.exception("Research node cleanup did not complete (or owner was superseded)")
        raise


async def _expand_owned(con, cfg, runner, worker, run, node, *, context, tool_call_id, poll_seconds, progress, session=None):
    """Advance one node through its routine. Returns the node's terminal status, a halt
    ("stopped" / "budget"), or a waiting_input result dict."""
    nid = node["id"]
    owner_run, owner_node = run, node

    def check(*, allow_stop=False):
        runs.check_owner(con, owner_run, owner_node, allow_stop=allow_stop)

    def set_node(**fields):
        return runs.set_node(con, nid, owner=(owner_run, owner_node), **fields)

    def drive(kind):
        scope = {row["id"] for row in runs.tasks(con, run["id"], kind=kind, node_id=nid)}
        return _drive_tasks(con, cfg, runner, run["id"], scope=scope, context=context,
                            tool_call_id=tool_call_id, poll_seconds=poll_seconds, progress=progress, session=session,
                            check_active=lambda: check(allow_stop=True), owner=(owner_run, owner_node))
    while True:
        await _wait_unpaused(con, run["id"], session)
        with runs.owned_txn(con, owner_run, owner_node, allow_stop=True):
            node = runs.node(con, nid)
        if runs.stop_requested(con, run["id"]):
            scope = {row["id"] for row in runs.tasks(con, run["id"], node_id=nid)}
            return await _drive_tasks(con, cfg, runner, run["id"], scope=scope, context=context,
                                      poll_seconds=poll_seconds, owner=(owner_run, owner_node))
        status = node["status"]
        if status == "queued":
            set_node(status="planning")

        elif status in ("planning", "waiting_input", "awaiting_approval"):
            round, command = runs.current_plan(con, run["id"], nid)
            round = round or 1
            plan = command["payload"] if command else None
            context_path = None
            if plan is None and node["parent_id"]:
                issue = con.execute("SELECT * FROM research_issues WHERE child_branch_id=?", (nid,)).fetchone()
                _aid, context_path, _packet = context_packet.create(
                    con, run, issue=issue, node=node, parent=runs.node(con, node["parent_id"]))
            if plan is None:
                await _progress(progress, "planning", f"Last Order is planning {_label(node)}.", run)
                plan, _raw, session_file = await asyncio.to_thread(
                    planner.plan, run, cfg, worker, node, con=con, context_path=context_path)
            else:
                session_file = command["session_file"]
            # The tool command is the checkpoint. Rebuild its projections even if the
            # process died just after acceptance, or these still show a prior clarification.
            _write_plan(con, run, node, plan, round)
            if node["parent_id"] is None:
                session_file = run["root_session"] or session_file
            set_node(session_file=session_file)
            if node["parent_id"] is None:
                with runs.owned_txn(con, owner_run, owner_node):
                    runs.set_state(con, run["id"], root_session=session_file)
            if plan["status"] == "clarify":
                set_node(status="waiting_input")
                return {"reason": "waiting_input", "questions": plan["clarifying_questions"],
                        "run": runs.summary(con, run["id"])}
            if planner.plan_waits_for_user(cfg, worker) and not runs.plan_started(con, run["id"], nid):
                halted = await _await_approval(con, cfg, runner, worker, run, node, round=round,
                                               poll_seconds=poll_seconds, progress=progress)
                if halted == "skipped":
                    return await _skip_node(con, run, node, progress)
                if halted:
                    return halted
                continue                                  # the plan as it now stands has the user's go-ahead
            if plan.get("reframed_question"):
                with runs.owned_txn(con, owner_run, owner_node):
                    runs.reframe(con, run["id"], node, plan["reframed_question"])
                    run, node = runs.get(con, run["id"]), runs.node(con, nid)
            if node["parent_id"] is None:
                with runs.owned_txn(con, owner_run, owner_node):
                    planner.publish_project_brief(run["workspace"], plan, round=round)
            await _progress(progress, "plan_ready",
                            f"Planning produced {len(plan['tasks'])} research tasks for {_label(node)}"
                            + (f" (round {round})." if round > 1 else "."), run,
                            tasks=[{"title": t["title"], "assignee": t["assignee"], "local_id": t["local_id"],
                                    "dependencies": t.get("dependencies") or []} for t in plan["tasks"]],
                            red_team=plan["red_team"], plan=plan, round=round)
            previous = [row for row in runs.tasks(con, run["id"], kind="research", node_id=nid)
                        if row["status"] == "done" and int(row["wave"] or 1) < round]
            await _submit_tasks(con, run, node, plan["tasks"], kind="research", progress=progress,
                                round=round, previous=previous)
            set_node(status="executing")

        elif status == "executing":
            outcome = await drive("research")
            if outcome == "failed":
                return _close(con, run, node, "failed")
            if outcome != "done":
                return outcome
            check()
            settle_done_tasks(con, run_id=run["id"])
            if not _done(con, run, node, "research"):
                return _close(con, run, node, "failed")
            set_node(status="synthesizing")

        elif status == "synthesizing":
            done = _done(con, run, node, "research")
            # The round whose cards she is reading is the latest one with cards back, not the
            # latest plan: a follow-up plan recorded just before a crash has no cards yet.
            round = max((int(row["wave"] or 1) for row in done), default=1)
            left = runs.limits(run)["max_followups"] - (round - 1)    # follow-ups still allowed on this node
            if _artifact_path(con, run, node, "synthesis"):
                set_node(status="critiquing")
                continue
            if _followup_recorded(con, run, node, round):   # a crash after she asked for more cards
                set_node(status="planning")
                continue
            await _progress(progress, "synthesizing",
                            f"Last Order is reading the cards of {_label(node)}"
                            + (f" (round {round}; {max(left, 0)} more round(s) possible)" if round > 1 or left > 0 else "")
                            + ".", run)
            followup = _followup_tool(con, cfg, run, node, round) if left > 0 else None
            text = await asyncio.to_thread(planner.synthesize, con, run, cfg, worker, node, done,
                                           followup=followup, round=round, left=max(left, 0))
            if _followup_recorded(con, run, node, round):
                new_round, command = runs.current_plan(con, run["id"], nid)
                _write_plan(con, run, node, command["payload"], new_round)
                await _progress(progress, "followup_planned",
                                f"Last Order asked for round {new_round} on {_label(node)}: "
                                f"{len(command['payload']['tasks'])} more card(s).", run, node=nid, round=new_round)
                set_node(status="planning")
                continue
            _write(con, run, node, "synthesis", f"Conclusion · {_label(node)}", "synthesis.md", text)
            set_node(status="critiquing")

        elif status == "critiquing":
            if not runs.tasks(con, run["id"], kind="red_team", node_id=nid):
                plan = runs.current_plan(con, run["id"], nid)[1]["payload"]
                await _progress(progress, "red_team",
                                f"Preparing Sister {plan['red_team']['assignee']}'s red-team assignment for {_label(node)}.", run)
                # Her reasoning is a fourth material, distinct from the three the red team already
                # holds (conclusion and plan by path, evidence inline): only the thinking and
                # working prose of her own turns, written once as an artifact and given by path.
                deliberation = planner.deliberation_text(node["session_file"])
                if deliberation:
                    _write(con, run, node, "deliberation", f"Deliberation · {_label(node)}", "deliberation.md",
                           deliberation)
                body = planner.red_team_body(node, synthesis_path=_artifact_path(con, run, node, "synthesis"),
                                             plan_path=_artifact_paths(con, run, node, "plan"),
                                             deliberation_path=_artifact_path(con, run, node, "deliberation"),
                                             evidence=planner.evidence_block(con, run, node),
                                             own_cards=[row for row in _done(con, run, node, "research")
                                                        if row["assignee"] == plan["red_team"]["assignee"]])
                spec = {
                    # '@' is outside LO task IDs, so research named 'red-team' cannot collide.
                    "local_id": "@red-team", "title": f"Red team · {_label(node)}",
                    "question": node["trigger_text"], "rationale": plan["red_team"]["reason"],
                    "assignee": plan["red_team"]["assignee"], "deliverable": "critique.md",
                    "instructions": body,
                }
                await _submit_tasks(con, run, node, [spec], kind="red_team", progress=progress)
            outcome = await drive("red_team")
            # Terminal, as in the executing phase: a node left non-terminal by a process that has
            # already exited is what the driver turns into a dead run.
            if outcome == "failed":
                return _close(con, run, node, "failed")
            if outcome != "done":
                return outcome
            check()
            settle_done_tasks(con, run_id=run["id"])
            red = _done(con, run, node, "red_team")
            if not red:
                return _close(con, run, node, "failed")
            unusable = _receive_critique(con, run, node, red[-1])
            if unusable:
                task_store.add_event(con, red[-1]["id"], "research_review_missing", {"reason": unusable})
                with runs.owned_txn(con, owner_run, owner_node):
                    con.execute("UPDATE research_branches SET last_error=? WHERE id=?", (unusable, nid))
                    runs.set_state(con, run["id"], error=f"node {nid}: {unusable}")
                return _close(con, run, node, "failed")
            set_node(status="reviewing")

        elif status == "reviewing":
            red = _done(con, run, node, "red_team")
            if not red:
                raise RuntimeError(f"Node {nid} has no completed red-team review to dispatch.")
            issues = list(runs.issues(con, run["id"], node_id=nid))
            at_limit = node["depth"] >= runs.limits(run)["max_depth"]
            if at_limit or not issues:
                reason = ("Depth limit reached; no child research was performed. The review remains available for final adjudication."
                          if at_limit else "No material issues were reported; no child research is needed.")
                try:
                    await _return_review(con, run, node, red[-1], issues, reason, session=session)
                except InterruptedError:
                    return "stopped"
                if runs.stop_requested(con, run["id"]):
                    return "stopped"
                if at_limit:
                    with runs.owned_txn(con, owner_run, owner_node):
                        con.execute("UPDATE research_issues SET status='parked',reason=? WHERE run_id=? AND branch_id=?",
                                    (reason, run["id"], nid))
                outcome = await asyncio.to_thread(_close, con, run, node, "closed")
                await _progress(progress, "review_returned", reason, run)
                return outcome
            await _progress(progress, "review_returned", "The red-team review is back with the node Last Order for dispatch.", run)
            await asyncio.to_thread(planner.investigate, con, run, cfg, worker, node, red[-1])
            children = await asyncio.to_thread(_dispatch_children, con, run, node)
            if runs.stop_requested(con, run["id"]):
                return "stopped"
            if children:
                await _progress(progress, "children_assigned",
                                f"Last Order assigned {len(children)} fork LO node(s) at depth {node['depth'] + 1}.",
                                run, depth=node["depth"] + 1, nodes=[c["id"] for c in children])
            return _close(con, run, node, "closed")

        else:
            raise RuntimeError(f"Node {nid} is in an unknown state: {status}")


async def _await_approval(con, cfg, runner, worker, run, node, *, poll_seconds, progress, round=1):
    """Hold the node at its accepted plan until its Last Order records the user's go-ahead.

    The node's conversation stays open the whole time: the root's is the user's own window, a
    fork's is its own window in the panel (or, headless, a chat attached to its live session).
    Two tools are put in that conversation for the wait -- revise the plan, start it -- and taken out after;
    nothing here reads the user's words. A revised plan is republished as it lands. Returns
    "stopped" if the run was stopped meanwhile, "skipped" if the user chose to close the node unresearched
    (a fork's first plan), else None once the go-ahead is recorded."""
    nid = node["id"]
    with task_store.write_txn(con):
        runs._owned(con, run, node)
        runs.set_node(con, nid, status="awaiting_approval")
        current = runs.node(con, nid)
    session_file = current["session_file"] or run["root_session"]   # the conversation that recorded the plan
    roster = planner._roster({**cfg, "workspace": run["workspace"]})
    tools = commands.review_tools(con, run, current, validate=lambda value: planner.validate_plan(value, roster),
                                  session_file=session_file, round=round)
    plan = runs.action(con, run["id"], nid, runs.plan_key(round))
    if node["parent_id"] is None:
        where = "in this window"
    elif getattr(runner, "home", None):
        where = "in its own tab"
    else:
        where = f"with `misaka chat --attach --session {session_file}`"
    session = getattr(worker, "session", None)
    control = _session_control(session)
    try:
        await _progress(progress, "plan_review",
                        f"{_label(node)}: the plan{f' (round {round})' if round > 1 else ''} is written and waits for "
                        f"your go-ahead. Talk it over with its Last Order {where}; she records the start once you agree.",
                        run, node=nid, plan_path=_artifact_path(con, run, node, "plan"), session_file=session_file,
                        round=round)
        if runs.stop_requested(con, run["id"]):
            return "stopped"
        runs._owned(con, run, node)
        if session is not None:
            session.registerCustomTools(tools)           # the window's own turns see them
        if control is not None:
            control.review_tools = tools                 # an attached chat's turns see them too
        while True:
            if runs.stop_requested(con, run["id"]):
                return "stopped"
            runs._owned(con, run, node)
            latest = runs.action(con, run["id"], nid, runs.plan_key(round))
            if latest is None:                          # withdrawn: the round never ran, so its plan goes too
                _forget_plan(con, run, node, round)
                return None
            if runs.skipped(con, run["id"], nid):       # the user chose not to research this node at all
                return "skipped"
            if latest["tool_call_id"] != plan["tool_call_id"]:
                plan = latest
                _write_plan(con, run, node, plan["payload"], round)
                await _progress(progress, "plan_revised", f"{_label(node)}: Last Order revised the plan; it still waits for your go-ahead.",
                                run, node=nid, round=round)
            if runs.plan_started(con, run["id"], nid):
                return None
            await asyncio.sleep(poll_seconds)
    finally:
        if session is not None:
            session.unregisterCustomTools(tools)
        if control is not None and control.review_tools is tools:
            control.review_tools = ()
        con.execute("UPDATE research_branches SET status='planning' WHERE id=? "
                    "AND runner_key IS ? AND status='awaiting_approval' AND EXISTS "
                    "(SELECT 1 FROM research_runs WHERE id=? AND driver_lock IS ?)",
                    (nid, node["runner_key"], run["id"], run["driver_lock"]))


async def _skip_node(con, run, node, progress):
    """Close a node the user chose not to research: its plan stays on record as the proposal it
    was, its issue is parked with the reason, and no card or conclusion ever exists. The final
    adjudication sees the issue as unresolved, which is the honest empty ticket."""
    nid = node["id"]
    decision = runs.skipped(con, run["id"], nid)
    reason = "Skipped by the user before any research: " + ((decision or {}).get("payload") or {}).get("reason", "")
    with runs.owned_txn(con, run, node):
        con.execute("UPDATE research_issues SET status='parked', reason=? WHERE run_id=? AND child_branch_id=?",
                    (reason, run["id"], nid))
    outcome = await asyncio.to_thread(_close, con, run, node, "parked")
    await _progress(progress, "node_skipped",
                    f"{_label(node)}: skipped by the user; no research was done, and its issue stays parked "
                    "for final adjudication.", run, node=nid, reason=reason)
    return outcome


def _session_control(session):
    if session is None:
        return None
    from misaka.core.session_control import for_session
    try:
        return for_session(session)
    except Exception:  # noqa: BLE001 - a session without parts (a test double) has no control
        return None


def _unfinished_reason(con, run_id):
    """Incomplete nodes or a missing root must never pass final adjudication."""
    nodes = runs.nodes(con, run_id)
    if not nodes:
        return "the run has no research node: its creation was interrupted, so there is nothing to adjudicate"
    failed = [n for n in nodes if n["status"] == "failed"]
    if not failed:
        return None
    details = [f"{n['id']} ({n['last_error']})" if n["last_error"] else n["id"] for n in failed]
    return f"{len(failed)} node(s) failed: {', '.join(details)}"


async def _keep_lease(con, run_id, lock, lost):
    """Renew the driver lease in the background for as long as the run is being driven; a failed
    renewal (another driver took over) raises the flag the main loop checks."""
    while True:
        await asyncio.sleep(runs.DRIVER_TTL_SECONDS / 3)
        try:
            renewed = runs.heartbeat_driver(con, run_id, lock)
        except Exception:  # No renewal means no authority, including a database failure.
            lost.set()
            raise
        if not renewed:
            lost.set()
            return


_INCOMPLETE_BANNER = (
    "> **This research is incomplete.** The run stopped before its final adjudication, so nothing below has\n"
    "> been weighed against anything else. Each conclusion is quoted exactly as the node that reached it wrote\n"
    "> it: final-report red-team review and adjudication have not completed, and the issues\n"
    "> listed below were never settled. This is the material the run had produced when it stopped, not its\n"
    "> answer.")


def _cell(value):
    """Keep quoted table data from opening new rows or columns."""
    return " ".join(str(value or "").splitlines()).strip().replace("|", r"\|")


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
        out.append(f"### [{node['id']}] depth {node['depth']} — {_cell(node['trigger_text'])}\n\n"
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
        return ["No issue was left open, assigned, or parked."]
    return ["The issues this run left open, assigned, or parked by the depth limit:", "",
            "| Issue | Node | Status | Question | Why it was raised |",
            "| --- | --- | --- | --- | --- |",
            *(f"| {_cell(row['issue'])} | {_cell(row['node'])} | {_cell(row['status'])} "
              f"| {_cell(row['question'])} | {_cell(row['rationale'])} |" for row in rows)]


def _partial_result(con, run, reason, *, status="stopped", driver_lock=None):
    """What a run that cannot finish hands back -- a deliverable, never a list of paths.

    Assembled with no model call at all, because the halt this most often answers is the token
    budget: at that moment there is nothing left to spend, and everything the document needs is
    already on disk. Each node's conclusion as that node wrote it, the ledger's count of what the
    run recorded, and the issues it never settled.
    """
    findings = len(ledger.findings(con, run["id"]))
    lines = ['# Incomplete research run', "", f"- Run: `{run['id']}`",
             f"- Project: `{runs.project_name(run)}` (`{run['workspace']}`)",
             f"- Reason: {reason}", "", _INCOMPLETE_BANNER, "", '## Original question',
             run["question"], "", '## Conclusions reached before the run stopped']
    for section in _conclusions(con, run) or ["No node had written its conclusion yet."]:
        lines += ["", section]
    lines += ["", '## Evidence on record', "",
              (f"The ledger holds {findings} declared finding{'' if findings == 1 else 's'}. "
               "These are researchers' records, not machine-verified conclusions."),
              "", '## Unresolved issues', ""]
    lines += _open_issues(con, run)
    lines += ["", '## Saved artifacts', ""]
    lines.extend(f"- [{row['kind']}] {row['title']} — `{row['path']}`"
                 for row in runs.artifacts(con, run["id"]))
    lines += [""]
    expected = {**dict(run), "driver_lock": driver_lock} if driver_lock is not None else run
    with runs.owned_txn(con, expected, allow_stop=True):
        aid, path = runs.write_text(con, run["id"], "partial", 'Incomplete research run', runs.run_path(run, "partial.md"), "\n".join(lines))
        runs.set_state(con, run["id"], status=status, final_artifact=aid, driver_lock=driver_lock)
    _try_refresh_workspace_index(con, runs.get(con, run["id"]))
    _bundle(con, run)
    runs._commit(con, run, f"research {run['id']}: {status}")
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

    ``ProcessSpawner.stop`` walks and suspends the whole process tree, then waits out two
    five-second grace periods. A four-node level is tens of seconds, and the loop this unwinds on is often
    not the research driver's own -- ``last_order.research`` starts ``workflow.run`` as a task on
    Last Order's loop, so this cleanup ran in the middle of her streaming and her inbox.

    Drained through cancellation: the owner waits until the process cleanup has finished before
    propagating cancellation, including repeated cancellation while shutdown is in progress.
    """
    if not handles:
        return
    _result, cancelled = await settle_thread_call(_stop_all, spawner, handles)
    if cancelled is not None:
        raise cancelled


async def expand_node(con, cfg, runner, worker, *, run_id, node_id, progress=None, session=None):
    """One node's routine, as run by its own process (misaka.core.research.node)."""
    runs.init(con)
    run, node = runs.get(con, run_id), runs.node(con, node_id)
    if not run or not node:
        raise ValueError(f"Research node not found: {run_id}/{node_id}")
    runs.require_layout(run)
    return await _expand(con, dict(cfg), runner, worker, run, node, context=None,
                         tool_call_id=f"research:{run_id}:{node_id}", poll_seconds=POLL_SECONDS,
                         progress=progress, session=session)


def _record_runner(con, table, row_id, handle, *, key=None):
    """The parent also records identity; the child's atomic claim covers a lost spawn reply."""
    pid = getattr(handle, "pid", None)
    if pid is None:
        return
    from misaka.core.platform import processes
    identity = processes.identity(int(pid))
    if identity is not None:
        con.execute(f'UPDATE "{table}" SET runner_pid=?, runner_identity=? WHERE id=? AND runner_key IS ? '
                    'AND (runner_pid IS NULL OR (runner_pid=? AND runner_identity=?))',
                    (int(pid), identity, row_id, key, int(pid), identity))


def _clear_runner(con, table, row_id, *, key=None):
    con.execute(f'UPDATE "{table}" SET runner_pid=NULL, runner_identity=NULL, runner_key=NULL '
                'WHERE id=? AND runner_key IS ?', (row_id, key))


def _reap_orphan_runner(con, table, row):
    """Fence late spawns atomically, then prove the prior process dead before replacing it.

    RETURNING observes a child that claimed between the caller's read and this fence. The
    key is invalidated even without a PID: a delayed pane.create must never start old work.
    """
    old = con.execute(f'UPDATE "{table}" SET runner_key=NULL WHERE id=? AND runner_key IS ? '
                      'RETURNING runner_pid, runner_identity', (row["id"], row["runner_key"])).fetchone()
    if old is None:
        raise RuntimeError(f"Research execution {row['id']} changed owners before cleanup")
    pid, identity = old
    if pid and identity:
        from misaka.core.platform import processes
        if processes.identity_is_alive(int(pid), identity):
            processes.terminate(int(pid))
        if processes.identity_is_alive(int(pid), identity):
            raise RuntimeError(f"Research process {pid} for {row['id']} did not stop")
    _clear_runner(con, table, row["id"])


def _runner_error(label, row):
    reason = row["last_error"] or ("no exception was recorded (the process may have been killed externally, "
                                   "or its window closed)")
    return RuntimeError(f"{label} ended while {row['status']}: {reason}; resume the run to retry it.")


async def _expand_level(con, cfg, spawner, run, level, *, poll_seconds, progress, driver_lock=None):
    """Run the level's nodes through a window as wide as the run's parallelism: a node starts as
    soon as a slot is free, and a slot frees the moment a node leaves the frontier (terminal,
    closing, or waiting for input) -- so one node held at its plan, or on a slow Sister, does not
    hold the nodes queued behind it. Returns "done", a halt, or a waiting_input result; on a halt
    or a node waiting for input nothing more is started, and the untouched nodes stay queued for
    the resume. If one node's process dies, the others are stopped before the error propagates."""
    handles = {}

    async def start(node):                          # registered one by one: a failed spawn stops the started ones
        await asyncio.to_thread(_reap_orphan_runner, con, "research_branches", node)
        with runs.owned_txn(con, run):
            key = runs.prepare_runner(con, "research_branches", node["id"])
        handle, cancelled = await settle_thread_call(
            spawner.spawn, _node_argv(run, node, key), cwd=run["workspace"])
        handles[node["id"]] = handle
        _record_runner(con, "research_branches", node["id"], handle, key=key)
        if cancelled is not None:
            raise cancelled
        return key
    try:
        return await _wait_level(con, cfg, spawner, run, handles, poll_seconds=poll_seconds, progress=progress,
                                 driver_lock=driver_lock, pending=list(level), width=runs.limits(run)["parallel"],
                                 start=start)
    except BaseException:
        await _stop_all_off_loop(spawner, handles)
        raise


async def _wait_level(con, cfg, spawner, run, handles, *, poll_seconds, progress, driver_lock=None,
                      pending=(), width=None, start=None):
    """Watch the running nodes until none is left, starting the next of ``pending`` (through
    ``start``, which returns the node's runner key) whenever fewer than ``width`` run -- unless
    the run halted or a node stopped to wait for the user's input, after which nothing more starts."""
    last = {}
    keys = {nid: runs.node(con, nid)["runner_key"] for nid in handles}
    pending, hold = list(pending), False

    async def top_up():
        nonlocal hold
        while pending and not hold and (width is None or len(handles) < width):
            if (runs.stop_requested(con, run["id"]) or budget.exhausted(con, cfg.get("token_cap"))
                    or any(n["status"] == "waiting_input" for n in runs.nodes(con, run["id"]))):
                hold = True
                break
            if driver_lock and runs.get(con, run["id"])["driver_lock"] != driver_lock:
                raise RuntimeError(f"Research run {run['id']}: the driver lease was taken over by another process.")
            node = pending.pop(0)
            keys[node["id"]] = await start(node)

    await top_up()
    while handles:
        await asyncio.sleep(poll_seconds)
        if driver_lock and not runs.heartbeat_driver(con, run["id"], driver_lock):
            raise RuntimeError(f"Research run {run['id']}: the driver lease was taken over by another process.")
        halted = (runs.stop_requested(con, run["id"])
                  or budget.exhausted(con, cfg.get("token_cap")))
        if halted:
            hold = True
            await _stop_all_off_loop(spawner, handles)
            await _settle_stopped_tasks(con, cfg, run["id"])
        for nid, handle in list(handles.items()):
            node = runs.node(con, nid)
            if node["runner_key"] is not None and node["runner_key"] != keys[nid]:
                raise RuntimeError(f"Research node {nid} changed owners.")
            hold = hold or node["status"] == "waiting_input"
            if node["status"] != last.get(nid):
                last[nid] = node["status"]
                await _progress(progress, "node", f"{_label(node)}: {node['status']}.", run, node=nid)
            # A node is settled when its process has exited (headless) or when it released the
            # runner it held while staying alive (an interactive node keeps its window open after
            # its routine). Release consumes the key; an unclaimed non-null key is not done.
            if not spawner.alive(handle) or (node["runner_key"] is None and node["runner_pid"] is None):
                node = runs.node(con, nid)
                if halted or (not node["last_error"] and node["status"] in (
                        "closing", "waiting_input", *runs.NODE_TERMINAL)):
                    handles.pop(nid)
                    _clear_runner(con, "research_branches", nid, key=keys[nid])
                    hold = hold or node["status"] == "waiting_input"   # the run is about to pause for the user
                else:
                    raise _runner_error(f"Node {nid}'s process", node)
        await top_up()
    if runs.stop_requested(con, run["id"]):
        return "stopped"
    if budget.exhausted(con, cfg.get("token_cap")):
        return "budget"
    waiting = [n for n in runs.nodes(con, run["id"]) if n["status"] == "waiting_input"]
    if waiting:
        questions = [f"[{n['id']}] {q}" for n in waiting
                     for q in ((runs.current_plan(con, run["id"], n["id"])[1] or {}).get("payload", {}).get("clarifying_questions") or [])]
        runs.set_state(con, run["id"], phase="waiting_input", status="waiting_input", driver_lock=driver_lock)
        return {"reason": "waiting_input", "questions": questions, "run": runs.summary(con, run["id"])}
    return "done"


async def _settle_stopped_tasks(con, cfg, run_id):
    from misaka.core.network import dispatch
    await asyncio.to_thread(dispatch.reconcile, con, cfg)
    await asyncio.to_thread(_stop_pending, con, runs.tasks(con, run_id))


async def run(con, cfg, spawner, worker, *, run_id, poll_seconds=POLL_SECONDS, progress=None,
              resume=False, clarification="", origin_session=None, session=None):
    """Advance one persisted research run until it finishes, stops, or needs user input.
    With ``session``, that existing window is the root LO. Child node LOs are processes; ``spawner`` starts one and says whether it lives."""
    runs.init(con)
    run = runs.get(con, run_id)
    if not run:
        raise ValueError(f"Research run not found: {run_id}")
    runs.require_layout(run)
    if (any(n["status"] in {"probing", "triaging"} for n in runs.nodes(con, run_id))
            or con.execute("SELECT 1 FROM research_actions WHERE run_id=? "
                           "AND (action_key LIKE 'probe:%' OR action_key='triage') LIMIT 1", (run_id,)).fetchone()):
        raise ValueError("This run used the removed probe workflow; start a new run. "
                         "Its artifacts and sessions are preserved.")
    if not os.path.isdir(run["workspace"]):
        raise RuntimeError(f"The research run's project folder no longer exists: {run['workspace']}")
    driver_lock = f"driver:{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(3)}"
    if not runs.acquire_driver(con, run_id, driver_lock):
        raise RuntimeError(f"Research run {run_id} is already being driven by another process "
                           f"(lease {run['driver_lock']}); wait for it or stop that process.")
    cfg = dict(cfg)
    halts = {"stopped": "The user requested a stop.", "budget": "The shared token budget limit was reached."}
    window = root_runner = None
    lost = asyncio.Event()
    keeper = asyncio.create_task(_keep_lease(con, run_id, driver_lock, lost))
    def check_active(*, allow_stop=False):
        current = runs.get(con, run_id)
        if lost.is_set() or current["driver_lock"] != driver_lock:
            cause = keeper.exception() if keeper.done() and not keeper.cancelled() else None
            raise RuntimeError(f"Research run {run_id}: driver lease lost" +
                               (f": {cause}" if cause else "."))
        if not allow_stop and current["stop_requested"]:
            raise InterruptedError("The user requested a stop.")

    def partial(reason, *, status="stopped"):
        check_active(allow_stop=True)
        return _partial_result(con, run, reason, status=status, driver_lock=driver_lock)

    try:
        # Stop shallower writers before refreshing the child inventory: one may have
        # created a child after our first read. No task generation changes until all stop.
        reaped = set()
        while pending := [n for n in runs.nodes(con, run_id) if n["id"] not in reaped]:
            for row in pending:
                check_active(allow_stop=True)
                await asyncio.to_thread(_reap_orphan_runner, con, "research_branches", row)
                reaped.add(row["id"])
        if origin_session:
            from misaka.core.platform import notifications
            with task_store.write_txn(con):
                current = runs.get(con, run_id)
                if current["origin_session"] != origin_session:
                    con.execute("UPDATE research_runs SET origin_session=? WHERE id=? AND driver_lock=?",
                                (origin_session, run_id, driver_lock))
                    subscription = notifications.subscribe(con, run_id, "research-chat")
                    con.execute("UPDATE notification_subscriptions SET lease_token=NULL,leased_event_id=NULL,"
                                "lease_expires=NULL WHERE id=?", (subscription,))
        run = runs.get(con, run_id)
        check_active(allow_stop=True)
        await asyncio.to_thread(runs.recover_publications, con, run)
        if resume:
            run = runs.resume(con, run_id, driver_lock=driver_lock, clarification=clarification)
        elif run["status"] == "done":
            raise ValueError(f"Research run {run_id} is done; start a new run.")
        run = runs.get(con, run_id)
        runs.ensure_layout(run)
        if runs.stop_requested(con, run_id):
            await _settle_stopped_tasks(con, cfg, run_id)
            return partial(halts["stopped"])
        if budget.exhausted(con, cfg.get("token_cap")):
            return partial(halts["budget"])
        if session is not None:
            from misaka.core.research.node import HeadlessRunner, PaneRunner
            from misaka.core.research.window import WindowLO
            window = WindowLO(session, check_active)
            runs.set_state(con, run_id, root_session=window.session_file, driver_lock=driver_lock)
            root = next(n for n in runs.nodes(con, run_id) if n["parent_id"] is None)
            # A window is not a killable node subprocess. The driver epoch fences its
            # commands without storing this foreground process as a child runner.
            with runs.owned_txn(con, run):
                runs.prepare_runner(con, "research_branches", root["id"])
                runs.set_node(con, root["id"], session_file=window.session_file)
            pane = os.environ.get("MISAKA_NET_PANE")
            root_runner = PaneRunner(con, cfg, run_id, pane) if pane else HeadlessRunner(con, cfg)
        run = runs.get(con, run_id)
        if run["phase"] == "created":
            runs.set_state(con, run_id, phase="active", driver_lock=driver_lock)
        while True:                                   # breadth-first: one level at a time, its nodes in parallel
            check_active()
            _settle_closing(con, run)
            level = runs.next_level(con, run_id)
            if not level:
                break
            runs.set_state(con, run_id, wave=level[0]["depth"], driver_lock=driver_lock)
            await _progress(progress, "level", f"Depth {level[0]['depth']}: {len(level)} node(s) expanding.", run,
                            nodes=[n["id"] for n in level])
            if window is not None and level[0]["parent_id"] is None:
                result = await _expand(
                    con, cfg, root_runner, window, run, level[0], context=None,
                    tool_call_id=f"research:{run_id}:{level[0]['id']}", poll_seconds=poll_seconds, progress=progress,
                    session=session)
            else:
                result = await _expand_level(con, cfg, spawner, run, level, poll_seconds=poll_seconds, progress=progress,
                                             driver_lock=driver_lock)
            if isinstance(result, dict):
                if result.get("reason") == "waiting_input":
                    runs.set_state(con, run_id, phase="waiting_input", status="waiting_input", driver_lock=driver_lock)
                    result["run"] = runs.summary(con, run_id)
                return result
            if result in halts:
                settle_done_tasks(con, run_id=run_id)
                return partial(halts[result])
        check_active()
        unfinished = _unfinished_reason(con, run_id)
        if unfinished:
            settle_done_tasks(con, run_id=run_id)
            await _progress(progress, "unfinished", f"The run cannot be adjudicated: {unfinished}", run)
            return partial(unfinished, status="failed")
        runs.set_state(con, run_id, phase="finalizing", driver_lock=driver_lock)
        await _progress(progress, "finalizing", "Every node is closed; Last Order is drafting the report for independent red-team review.", run)
        draft = await asyncio.to_thread(report.prepare, con, runs.get(con, run_id), cfg, window or worker,
                                        check_active=check_active)
        check_active()
        root = next(n for n in runs.nodes(con, run_id) if n["parent_id"] is None)
        plan = runs.current_plan(con, run_id, root["id"])[1]["payload"]
        spec = {"local_id": "@final-review", "title": "Red team · final report",
                "assignee": plan["red_team"]["assignee"], "instructions": report.review_body(con, run, draft)}
        linked = await _submit_tasks(con, run, root, [spec], kind="final_review", progress=progress)
        check_active()
        review_id = linked["@final-review"]
        target = {"artifact": draft["id"], "sha256": draft["sha256"]}
        previous = task_store.latest_payload(con, review_id, "research_review_target")
        if previous is None:
            task_store.add_event(con, review_id, "research_review_target", target)
        elif json.loads(previous) != target:
            raise ValueError("Final red-team card belongs to a different draft.")
        if root_runner is None:
            from misaka.core.research.node import HeadlessRunner
            root_runner = HeadlessRunner(con, cfg)
        await _progress(progress, "final_review", "The independent red team is reviewing the saved report draft.", run,
                        task_id=review_id, draft_path=draft["path"])
        outcome = await _drive_tasks(con, cfg, root_runner, run_id, scope={review_id},
                                     tool_call_id=f"research:{run_id}:final-review", poll_seconds=poll_seconds,
                                     progress=progress, check_active=lambda: check_active(allow_stop=True), session=session,
                                     owner=(run, None))
        check_active(allow_stop=True)
        settle_done_tasks(con, run_id=run_id)
        if outcome in halts:
            return partial(halts[outcome])
        if outcome != "done":
            return partial("Final-report red-team review failed.", status="failed")
        try:
            report.review_receipt(con, run, draft, review_id)
        except (OSError, TypeError, ValueError) as error:
            task_store.add_event(con, review_id, "research_review_missing", {"reason": str(error)})
            return partial(str(error), status="failed")
        check_active()
        await _progress(progress, "adjudicating", "Last Order is weighing the final red team's objections; objections are not automatic verdicts.", run)
        result = await asyncio.to_thread(report.finalize, con, runs.get(con, run_id), cfg, window or worker,
                                         review_task_id=review_id, check_active=check_active)
        check_active()
        runs.set_state(con, run_id, phase="done", status="done", final_artifact=result["artifact"],
                       driver_lock=driver_lock)
        _try_refresh_workspace_index(con, runs.get(con, run_id))
        _bundle(con, run)
        runs._commit(con, run, f"research {run_id}: complete artifacts")
        return {"reason": "done", "final": result, "run": runs.summary(con, run_id)}
    except asyncio.CancelledError:
        current = runs.get(con, run_id)
        if current["driver_lock"] == driver_lock:
            runs.request_stop(con, run_id)
            await _settle_stopped_tasks(con, cfg, run_id)
            partial(halts["stopped"])
        raise
    except InterruptedError:
        return partial(halts["stopped"])
    except Exception as error:
        current = runs.get(con, run_id)
        if current["status"] == "done":
            raise
        if current["driver_lock"] == driver_lock:
            runs.set_state(con, run_id, status="failed", error=f"{type(error).__name__}: {error}"[:500],
                           driver_lock=driver_lock)
        raise
    finally:
        try:
            if window is not None:
                await window.close()
            if root_runner is not None and hasattr(root_runner, "close"):
                await asyncio.to_thread(root_runner.close)
        finally:
            keeper.cancel()
            await asyncio.gather(keeper, return_exceptions=True)
            runs.release_driver(con, run_id, driver_lock)

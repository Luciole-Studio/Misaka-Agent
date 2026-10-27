"""Persistent Research Workflow: breadth-first over a depth-bounded graph of possibilities.

Every node follows the same routine: Last Order plans and assigns Sisters, synthesizes their
research, and hears the red team. She answers every material issue inside the node -- revising the
conclusion (reviewed again, up to the run's revision limit), rebutting, conceding a cost, pointing to
the node that owns it, parking it, or taking it to a decision -- so a correction never becomes a fork.
The same Sister then proposes, in a new session, the alternatives the conclusion did not take, and
Last Order decides the forks. Between levels the root Last Order reconciles: pending options open as
nodes (options asking the same question share one node, with several parents), finished nodes that
arrive at the same place may be joined, and relations are recorded. Code enforces phase order, depth,
the node budget, persistence and artifact integrity; models make the judgements.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import secrets
import socket
import sqlite3
import sys
import time
from datetime import UTC, datetime
from graphlib import CycleError, TopologicalSorter
from pathlib import Path

from misaka import workspace as workspace_index
from misaka.core.platform import budget, prompt_guard
from misaka.core.platform import cards as card_files
from misaka.core.platform import tasks as task_store
from misaka.core.research import bundle, commands, ledger, planner, report, runs, views
from misaka.core.research import context as context_packet
from misaka.utils.async_lifecycle import run_in_thread, settle, settle_thread_call
from misaka.utils.markdown import atx_headings
from misaka.utils.paths import posix_relpath
from misaka.utils.values import read_field

POLL_SECONDS = 2.0
WARMUP_CAP_SECONDS = 300
_LOG = logging.getLogger(__name__)
ACTIVE_TASKS = ("running", "review")
# Statuses on which a *missing* plan edge is history rather than a fault: the card is finished (or
# gone) and has nothing left to wait for. `running` is excluded on purpose -- see `_submit_tasks`.
_EDGES_ARE_HISTORY = runs.SETTLED_TASK_STATUSES - {"running"}
RESEARCH_DISCIPLINE = """[Research Workflow active]
Last Order is now in Research mode. The research is a graph of nodes, and every node runs the same routine: Last
Order plans and assigns Sisters (each Sister plans and executes within its own task session); once their cards are
back she writes the node's conclusion or first sends Sisters out for another round (up to the run's follow-up limit).
The red-team Sister she named reviews the conclusion, and Last Order answers every material issue inside the node:
revise (the revised conclusion is reviewed again, up to the run's revision limit), rebut, concede a cost, covered by
another node, park for the final adjudication, or branch when the issue reveals a genuine alternative. Corrections
never open nodes. The same Sister then proposes, in a new session, the alternatives the conclusion did not take, and
Last Order decides which forks the node opens: each decision has two or more options with their premises, this
node's own line possibly among them. Branches are possibilities -- hypotheses, methods, readings, perspectives,
decompositions, critiques of the question itself -- none invented to fill a count, none held back that is real.
Between levels the root Last Order reconciles: options open as nodes (options of different nodes asking the same
question share one node), finished nodes whose paths arrive at the same place may be joined into one successor,
and relations between nodes are recorded. Follow the current plan-approval policy: when enabled, every plan and
every reconciliation waits for its own approval; otherwise the driver proceeds after an accepted ready plan. A
plan that reframes the question always waits for the user. While a node's research cards run, its Last Order can
change them at the user's word with `misaka_research_cards`: add cards, or give a card that has not started to
another Sister, or cancel it; the cards already working go on. The graph expands breadth-first; max_depth is the
largest allowed depth (root = 0), and max_nodes caps the graph. At max_depth a node is still reviewed and revised,
but cannot fork.

Code enforces phase order, depth, the node budget, persistence, and artifact integrity. The models keep the judgement
calls: framing, methods, Sister selection, source quality, task count, what shakes a conclusion, and what is a genuine
alternative. Planning produces a design, not an answer. Node synthesis produces a working conclusion for independent
review. Final adjudication produces the report for delivery, keeping competing conclusions side by side wherever the
evidence cannot decide between them.
While research or required review remains unfinished, including while waiting for clarification, do not deliver an
answer to the research question to the user; internal working conclusions and drafts are not delivered answers.
"""

RESEARCH_PAUSED = """[Research Workflow paused]
Research run {run_ids} of this conversation is paused ({statuses}); its driver is not running. Only the user can
continue it, with `/research resume <run id>` in this window. Until then: do not continue, restart or rework its
research cards yourself, do not change the board or its files by hand, and do not stand in for the driver.
You may report status, explain the plan or pause, and discuss other topics. Do not deliver an answer to the unfinished
research question or resume its investigation. Wait for the user.
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


# A progress note that announces work the next phase hands its Last Order with that phase's own
# tool: a review back with issues to dispose of or gaps to fill, cards that failed for good. The
# window of that Last Order keeps it out of what the model sees, and still shows it. A turn the
# red-team Sister's mail had started read it, called the phase's tool before the phase gave it,
# and got "not found" (2026-10-27, R60); the phase's prompt carries the same review and the tool.
ANNOUNCES_PHASE = "announces_phase"


def announces_phase_of(message, nodes):
    """Whether ``message`` is such a note about one of ``nodes``."""
    details = read_field(message, "details")
    return (read_field(message, "role") == "custom" and read_field(message, "customType") == "research-progress"
            and isinstance(details, dict) and details.get(ANNOUNCES_PHASE) is True and details.get("node") in nodes)


# How often a driver looks for Sisters added to or removed from the roster while the run goes on.
ROSTER_CHECK_SECONDS = 30
_rosters = {}     # run id -> (when last looked, {Sister id: description})


async def _roster_check(cfg, run, progress):
    """Tell the run's Last Order when Sisters join or leave the roster while she works (GitHub issue
    #9): every plan is made with the roster as it stands, but between plans nothing said it had
    changed. The first look is the baseline; a change is a notice in her feed, read with her next
    request."""
    from misaka.core.network import roster
    now = time.monotonic()
    seen = _rosters.get(run["id"])
    if seen and now - seen[0] < ROSTER_CHECK_SECONDS:
        return
    try:
        current = {entry["id"]: entry["description"]
                   for entry in await asyncio.to_thread(roster.routing_catalog, cfg.get("profiles_root"))}
    except Exception:  # noqa: BLE001 - a notice: an unreadable roster is not the run's business to fail on
        _LOG.debug("research: the roster could not be read", exc_info=True)
        return
    _rosters[run["id"]] = (now, current)
    if seen is None:
        return
    added, removed = sorted(set(current) - set(seen[1])), sorted(set(seen[1]) - set(current))
    if not added and not removed:
        return
    said = []
    if added:
        said.append("added " + "; ".join(f"{sid} ({' '.join(current[sid].split())[:100] or 'no description'})"
                                         for sid in added))
    if removed:
        said.append("removed " + ", ".join(removed))
    await _progress(progress, "roster_changed",
                    f"The Sister roster changed while the run works: {'; '.join(said)}. Plans and follow-ups "
                    "from now on assign from the roster as it is now.", run, added=added, removed=removed)


def _write(con, run, node, kind, title, name, content, **metadata):
    with runs.owned_txn(con, run, node):
        return runs.write_text(con, run["id"], kind, title, runs.generated_path(name, node_id=node["id"]), content,
                               branch_id=node["id"], metadata=metadata or None)


async def _views(con, run, *, final=False):
    """Rewrite the graph's views in the project. Off the loop: it waits for the run's view lock."""
    await asyncio.to_thread(views.write_views, con, run, final=final)


def _bundle(con, run, *, task=None, node=None, index=None):
    """Gather sources beside a settled card, a closed node, or the run's delivered document.
    A bundle is derived from what the ledger and the project already hold, so its failure is
    recorded (an event on the card, a log line otherwise) and the workflow carries on: no card,
    node or run state depends on it."""
    try:
        if task is not None:
            bundle.card_bundle(con, run, task, index=index)
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
    rows = runs.artifacts(con, run["id"], kind=kind, branch_id=node["id"])
    return runs.artifact_path(rows[-1]) if rows else None


def _artifact_paths(con, run, node, kind):
    """Every artifact of a kind on the node, oldest first, for a prompt: a node that planned
    more than once has more than one plan."""
    rows = runs.artifacts(con, run["id"], kind=kind, branch_id=node["id"])
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
        for row in runs.artifacts(con, run["id"], branch_id=node["id"]):
            if row["kind"] in ("plan", "plan_json") and os.path.basename(row["path"]) in names:
                con.execute("DELETE FROM research_artifacts WHERE id=?", (row["id"],))
                with contextlib.suppress(OSError):
                    os.unlink(row["path"])


def _followup_recorded(con, run, node, round):
    return runs.plan_round(con, run["id"], node["id"]) > round


def _withdraw_emptied_round(con, run, node, round):
    """Withdraw a follow-up round whose every card Last Order cancelled, as
    ``misaka_research_withdraw`` would: kept, it read as a follow-up she had asked for, and her
    conclusion was dropped as one (0.18.10 sweep). Its card changes go with it, or a round planned
    again under the same number would inherit them."""
    with runs.owned_txn(con, run, node):
        for key in (runs.start_key(round), runs.plan_key(round)):
            runs.delete_action(con, run["id"], node["id"], key)
        runs.withdraw_actions(con, run["id"], node["id"], runs.plan_change_key(round) + ":")
    _forget_plan(con, run, node, round)


def _followup_tool(con, cfg, run, node, round):
    """The next round's plan tool for the synthesis turn: recorded like any phase command, and
    read by the driver as the node's decision to research more before concluding. A follow-up
    round assigns cards only; forks belong to the first plan and to the node's decision."""
    current = runs.node(con, node["id"])
    return commands.tool(
        con, run, node, key=runs.plan_key(round + 1), name="misaka_research_assign",
        description=f"Last Order: assign a follow-up round of research cards (round {round + 1}) on this node",
        model=commands.Plan,
        validate=lambda value: planner.validate_plan(value, planner._roster({**cfg, "workspace": run["workspace"]}),
                                                     red_team_required=False),
        session_dir=planner._lo_session(run, node),
        session_file=planner.lo_session_file(run, current),
        supersede=True,
        # A round too large for one reply goes in several calls, as the first plan's does: without
        # the merge an append call replaced the cards of the call before it (0.18.10 sweep).
        merge=commands.merge_plan,
        # Told at the call, not only in the phase prompt: a Last Order who recorded a follow-up
        # went on to write a full conclusion the driver then discards (2026-09-27, six minutes).
        reply=lambda _payload: (f"Accepted: round {round + 1} is recorded. Its cards go out when this turn ends, "
                                "and you conclude after they return. Do not write the conclusion now; end this "
                                "turn with a short note."))


def _erratum_tool(con, run, node):
    """The synthesis turn's record of a correction to an earlier node's conclusion."""
    current = runs.node(con, node["id"])
    return commands.erratum_tool(con, run, current, session_file=planner.lo_session_file(run, current))


def _dissolve_tool(con, run, node, version):
    """The synthesis turn's record of a conclusion that dissolves a question, kept for this version."""
    current = runs.node(con, node["id"])

    def validate(value):
        # The root's own question is the research question.
        return {**value, "question": "research_question"} if runs.is_root(current) else value

    return commands.tool(
        con, run, node, key=runs.dissolve_key(version), name="misaka_research_dissolve",
        description="Last Order: record that this conclusion dissolves a question rather than answering it",
        model=commands.Dissolve, validate=validate, session_dir=planner._lo_session(run, node),
        session_file=planner.lo_session_file(run, current), supersede=True)


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
    return "the root question" if runs.is_root(node) else f"node {node['id']}"


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
    card's round within its kind (a research card's planning round, a red-team card's review
    round); ``previous`` the earlier rounds' research cards the new ones build on.
    """
    root = run["workspace"]
    if kind == "research":
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
                "SELECT task_id FROM research_cards WHERE run_id=? AND branch_id=? AND local_id=?",
                (run["id"], node["id"], spec["local_id"]),
            ).fetchone()
            if existing and task_store.get(con, existing["task_id"]) is not None:
                local_to_task[spec["local_id"]] = existing["task_id"]   # a resume after a crash: the card already exists
                continue
            if existing:                                   # the card was deleted: drop the stale link and rebuild it
                con.execute("DELETE FROM research_cards WHERE task_id=?", (existing["task_id"],))
            tid = card_files.create(
                con, root, spec["title"],
                planner.task_body(spec, run_id=run["id"], node=node, siblings=specs, previous=previous,
                                  lo_session=planner.lo_session_file(run, node)),
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
        # skip it (`runs.SETTLED_TASK_STATUSES` is that guard). `running`
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

    Off the loop thread as one hop rather than one per card: ``mark_stopped`` rewrites each card's
    file under its lock, and a stop can hold fifty linked cards. A card the reconciler has just
    accepted is in ``review`` or ``done`` and is deliberately not touched here.
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


async def _stop_consultations(con, task_ids):
    """A halted scope's finished cards stop answering too. Their consultations run in the panel
    daemon's panes whichever runner the scope used; without a daemon there is none left to stop,
    and ``dispatch.reconcile`` settles what a dead one left behind."""
    from misaka.ui.panel import client as net
    for task_id in task_ids:
        run = task_store.live_consultation(con, task_id)
        if run is None:
            continue
        try:
            await asyncio.to_thread(net.request, "card.stop", {
                "task_id": task_id, "consultation": run["id"], "reason": "the research run was stopped"})
        except (RuntimeError, OSError):
            pass


async def _halt_scope(con, cfg, runner, captured, context, *, owner=None):
    from misaka.core.network import dispatch
    active = {tid for tid, row in captured.items() if row["status"] in ACTIVE_TASKS}
    try:
        await _stop_active(runner, active, context, captured=captured, owner=owner)
    finally:
        await _stop_consultations(con, captured)
        await asyncio.to_thread(dispatch.reconcile, con, cfg)
        await asyncio.to_thread(_stop_pending, con, list(captured.values()), owner=owner)


async def _wait_unpaused(con, run_id, session):
    from misaka.core.session_control import for_session

    control = for_session(session) if session is not None else None
    while control is not None and control.paused and not runs.stop_requested(con, run_id):
        control.check_active()
        await asyncio.sleep(.1)


def _changed_tasks(tasks, changes):
    """A round's tasks as Last Order changed them while its cards ran, change by change."""
    by_id = {task["local_id"]: dict(task) for task in tasks}
    for change in changes:
        for item in change["cancel"]:
            by_id.pop(item["local_id"], None)
        for item in change["reassign"]:
            if item["local_id"] in by_id:
                by_id[item["local_id"]].update(assignee=item["assignee"], assignee_reason=item["assignee_reason"])
        for task in change["add"]:
            by_id[task["local_id"]] = task
    return list(by_id.values())


def _changed_plan(plan, changes):
    """The plan as Last Order changed it while its cards ran: its tasks change by change, and the
    changes recorded under its own text, which the red team and the report read."""
    if not changes:
        return plan
    record = "\n".join(f"- {change['reason']}: " + "; ".join(
        [f"added {task['local_id']} → Sister {task['assignee']}" for task in change["add"]]
        + [f"{item['local_id']} → Sister {item['assignee']}" for item in change["reassign"]]
        + [f"cancelled {item['local_id']} ({item['reason']})" for item in change["cancel"]]) for change in changes)
    return {**plan, "tasks": _changed_tasks(plan["tasks"], changes),
            "plan_markdown": plan["plan_markdown"].rstrip() + "\n\n## Changed while the cards ran\n" + record}


def _round_cards(con, run, node, round):
    """A round's research cards by their local id in the plan (a later round's carry ``r<n>/``)."""
    prefix = "" if round <= 1 else f"r{round}/"
    return {row["local_id"][len(prefix):]: row for row in runs.tasks(con, run["id"], kind="research", node_id=node["id"])
            if int(row["round"] or 1) == round and (row["local_id"] or "").startswith(prefix)}


def _check_plan_change(con, run, node, round, roster, change):
    """A change to a round's running cards, checked as a plan's tasks are (the same roster, ids,
    dependencies and cycles) and written down by local id."""
    plan = runs.action(con, run["id"], node["id"], runs.plan_key(round))["payload"]
    tasks = _changed_tasks(plan["tasks"], runs.plan_changes(con, run["id"], node["id"], round))
    cards = _round_cards(con, run, node, round)
    wanted = {task["local_id"] for task in tasks}
    local_of = {row["id"]: local for local, row in cards.items()}

    def unstarted(ref, done):
        local = local_of.get(ref, ref)
        row = cards.get(local)
        if row is None or local not in wanted:
            raise ValueError(f"{ref} is not one of this round's cards: {', '.join(sorted(wanted)) or 'none'}.")
        if row["status"] not in ("ready", "todo"):
            raise ValueError(f"Card {row['id']} ({local}) is {row['status']}; only a card that has not started is {done}.")
        return local

    roster_ids = {entry["id"] if isinstance(entry, dict) else str(entry) for entry in roster}
    for assignee in [item["assignee"] for item in change["reassign"]] + [task["assignee"] for task in change["add"]]:
        if assignee not in roster_ids:
            raise ValueError(f"{assignee} is not in the Sister roster: {', '.join(sorted(roster_ids))}.")
    commands._refuse_repeated_ids(change["add"])
    for task in change["add"]:
        if task["local_id"] in wanted or task["local_id"] in cards:
            raise ValueError(f"{task['local_id']} is already a card of this round; give the new one another local_id.")
    normalized = {"reason": change["reason"], "add": change["add"],
                  "reassign": [{"local_id": unstarted(item["card"], "reassigned"), "assignee": item["assignee"],
                                "assignee_reason": item["assignee_reason"]} for item in change["reassign"]],
                  "cancel": [{"local_id": unstarted(item["card"], "cancelled"), "reason": item["reason"]}
                             for item in change["cancel"]]}
    # The whole round as it would stand, with the Sisters its cards already have taken as they are.
    checked = planner._validate_tasks(_changed_tasks(tasks, [normalized]), roster_ids | {t["assignee"] for t in tasks})
    added = {task["local_id"] for task in change["add"]}
    normalized["add"] = [task for task in checked if task["local_id"] in added]
    return normalized


async def _apply_plan_changes(con, run, node, round, applied, progress, owner, pending=None):
    """Make the board match the changes Last Order recorded since the last look: cancelled cards
    and reassigned ones come off (with the cards waiting on them, whose ``needs`` name the old
    ids), and ``_submit_tasks`` creates what the round now lacks. A card the runner is already
    starting (``pending``: its process or pane is up, its claim not yet taken) counts as started.
    True when something was applied."""
    changes = runs.plan_changes(con, run["id"], node["id"], round)
    if len(changes) <= applied[0]:
        return False
    plan = runs.action(con, run["id"], node["id"], runs.plan_key(round))["payload"]
    tasks = _changed_tasks(plan["tasks"], changes)
    wanted = {task["local_id"]: task for task in tasks}
    cards = _round_cards(con, run, node, round)
    waiting = {local: row for local, row in cards.items() if row["status"] in ("ready", "todo")}
    if pending is not None and waiting:
        starting = set(await pending([row["id"] for row in waiting.values()]))
        waiting = {local: row for local, row in waiting.items() if row["id"] not in starting}
    off = {local for local, row in waiting.items()
           if local not in wanted or wanted[local]["assignee"] != row["assignee"]}
    while True:
        more = {local for local in waiting if local not in off and local in wanted
                and set(wanted[local].get("dependencies") or []) & off}
        if not more:
            break
        off |= more
    for local in sorted(off):
        runs.check_owner(con, *owner)
        ok, message = card_files.remove(con, run["workspace"], waiting[local]["id"])
        if not ok:
            raise RuntimeError(message)
    previous = [row for row in runs.tasks(con, run["id"], kind="research", node_id=node["id"])
                if row["status"] == "done" and int(row["round"] or 1) < round]
    await _submit_tasks(con, run, node, tasks, kind="research", progress=progress, round=round, previous=previous)
    # A card started between Last Order's call and this look goes on as it was.
    late = sorted({item["local_id"] for change in changes[applied[0]:] for item in change["cancel"] + change["reassign"]
                   if item["local_id"] in cards and item["local_id"] not in waiting
                   and (item["local_id"] not in wanted or cards[item["local_id"]]["assignee"] != wanted[item["local_id"]]["assignee"])})
    _write_plan(con, run, node, _changed_plan(plan, changes), round)
    newest = changes[applied[0]:]
    applied[0] = len(changes)
    await _progress(progress, "plan_changed",
                    f"{_label(node)}: Last Order changed its cards -- " + " | ".join(change["reason"] for change in newest)
                    + (f". Already started, so they go on as they were: {', '.join(late)}." if late else "."),
                    run, node=node["id"])
    return True


async def _tell_failed(con, run, linked, told, progress):
    """A card that failed for good is news the moment it lands (GitHub issue #9): what becomes of it
    -- retried, concluded without, or the node failed -- is decided once the phase's other cards are
    back, and until then nothing said it had failed. Told once per attempt."""
    open_ = [row for row in linked if row["status"] not in ("done", "failed", "stopped")]
    for row in linked:
        if row["status"] != "failed" or (row["id"], row["generation"]) in told:
            continue
        told.add((row["id"], row["generation"]))
        try:
            why = json.loads(task_store.latest_payload(con, row["id"], "failed", generation=row["generation"]) or "{}")
        except ValueError:
            why = {}
        why = (why.get("reason") if isinstance(why, dict) else None) or row["last_failure_error"] or "no failure detail recorded"
        await _progress(progress, "card_failed",
                        f"Card {row['id']} ({row['title']} → Sister {row['assignee']}) failed: {' '.join(str(why).split())[:300]}. "
                        + (f"What becomes of it is decided when the other {len(open_)} card(s) of this phase are back."
                           if open_ else "What becomes of it is decided now."), run, task_ids=[row["id"]])


async def _drive_tasks(con, cfg, runner, run_id, *, scope, context=None,
                       tool_call_id="research", poll_seconds=POLL_SECONDS, progress=None, check_active=None, session=None, owner=None,
                       refresh=None):
    """Drive the cards in ``scope`` (task ids) until none is open; tasks waiting on dependencies stay in ``todo``."""
    captured = {row["id"]: row for row in runs.tasks(con, run_id) if row["id"] in scope}
    try:
        return await _drive_tasks_inner(con, cfg, runner, run_id, scope=scope, context=context,
                                       tool_call_id=tool_call_id, poll_seconds=poll_seconds,
                                       progress=progress, check_active=check_active, session=session,
                                       captured=captured, owner=owner, refresh=refresh)
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
                       tool_call_id="research", poll_seconds=POLL_SECONDS, progress=None, check_active=None, session=None, captured=None, owner=None,
                       refresh=None):
    """Drive the cards in ``scope`` (task ids) until none is open; tasks waiting on dependencies stay in ``todo``.
    ``refresh()``, when given, is asked at every look: a new scope when the cards changed, else None."""
    last_snapshot = None
    # Attempts already failed when the phase began were told of then.
    told_failed = {(tid, row["generation"]) for tid, row in captured.items() if row["status"] == "failed"}
    while True:
        await _wait_unpaused(con, run_id, session)
        if check_active:
            check_active()
        run = runs.get(con, run_id)
        await _roster_check(cfg, run, progress)
        changed = await refresh() if refresh is not None else None
        if changed is not None:
            scope.clear()
            scope.update(changed)
            for gone in set(captured) - scope:
                captured.pop(gone)
        linked = [row for row in runs.tasks(con, run_id) if row["id"] in scope]
        await _tell_failed(con, run, linked, told_failed, progress)
        for row in linked:
            previous = captured.get(row["id"])
            if previous is not None and previous["generation"] != row["generation"]:
                # A new attempt on the same card: Last Order or the user stopped and continued
                # it, or sent a finished card back for rework. That is what the Sister tools
                # offer, not a takeover. Raising here (2026-09-18, B13) failed the run and
                # stopped every other card in the scope. Follow the card to its new attempt.
                await _progress(progress, "tasks",
                                f"Card {row['id']} moved to attempt {row['generation']} "
                                f"(was {previous['generation']}); the run follows it.",
                                run, task_ids=[row["id"]])
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
                "budget" if budget.exhausted(con, task_id=run_id) else None)
        if halt:
            await _halt_scope(con, cfg, runner, captured, context, owner=owner)
            return halt
        if check_active:
            check_active()
        await asyncio.to_thread(_release_dependencies, con, run_id, owner=owner)
        linked = [row for row in runs.tasks(con, run_id) if row["id"] in scope]
        waiting = [row["id"] for row in linked if runs.help_waiting(con, row)]
        # Per-node Sister slots are independent of the run's LO-node parallelism.
        free = max(0, runs.limits(run)["sister_parallel"]
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
                if any(runs.parked(con, row) for row in linked):
                    return "failed"     # a parked card holds its dependants: Last Order decides (lost)
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


class ArtifactDrift(ValueError):
    """A card's accepted files no longer match its submission. The bytes she declared are not
    the bytes on disk, so nothing is registered under that declaration: the card is reopened
    and the Sister declares again (her transcript continues, so that is one short turn)."""

    def __init__(self, task_id, paths):
        super().__init__(f"Card {task_id}: accepted artifacts changed after submission: {', '.join(paths)}")
        self.task_id, self.paths = task_id, list(paths)


DERIVED_BUNDLE_NAMES = frozenset({"SOURCES.md", "sources"})
# What a card's accepted files are registered as, by the card's research kind.
ARTIFACT_KINDS = {"red_team": "critique", "final_review": "final_critique", "divergence": "divergence"}


def _register_task_artifacts(con, run, task, submitted):
    """Register submitted project files at their original location. Nothing is copied or moved.

    Only files under the by-node layout (``nodes/``, ``final/``) are a card's outputs. A Sister
    who lists a page from the shared ``downloads/`` cache, or a file at the project root, has
    named a source, not a product: those are gathered by the bundles beside her outputs and
    are neither registered nor committed with the node. Each such path is recorded on the card.

    Only files in the card's own folder are frozen by their submitted digest. Its derived
    bundle (``SOURCES.md``, ``sources/``) is rebuilt at every settle, and a node-level file such
    as a coordination ledger is written by every card that shares it; freezing either failed
    whole runs (2026-09-24: 18 changed paths across 6 cards, one of them settle's own bundle).
    A changed or missing file of the card's own raises ``ArtifactDrift`` after the whole list
    is checked, and nothing of that card is registered under the stale declaration."""
    link = con.execute("SELECT * FROM research_cards WHERE task_id=?", (task["id"],)).fetchone()
    if not link or not task["workspace"]:
        return
    node = runs.node(con, link["branch_id"])
    digests = submitted.get("artifact_digests")
    if not isinstance(digests, dict):        # every submission records them (dispatch._submitted)
        raise ValueError(f"Card {task['id']}: its submission carries no artifact digests.")  # noqa: TRY004 - a bad submission, not a bad type
    generation = int(dict(task).get("generation") or 1)
    workspace = Path(run["workspace"]).resolve()
    # A card without an output folder (a headless or hand-made one) has no "own" boundary:
    # every layout file it declares is frozen, as before 2026-09-24.
    output_dir = dict(task).get("output_dir")
    own = Path(output_dir).resolve() if output_dir else None
    accepted, drifted = [], []
    for rel in submitted.get("artifacts") or []:
        source = Path(task["workspace"], str(rel)).resolve()
        try:
            inside = source.relative_to(workspace)
        except ValueError as error:
            raise ValueError(f"Accepted artifact moved outside the workspace: {rel}") from error
        if inside.parts[:1] not in {(root,) for root in ARTIFACT_ROOTS}:
            task_store.add_event(con, task["id"], "artifact_outside_layout", {"path": str(rel)}, generation=generation)
            continue
        if own is not None and not source.is_relative_to(own):
            task_store.add_event(con, task["id"], "artifact_outside_card", {"path": str(rel)}, generation=generation)
            continue
        if DERIVED_BUNDLE_NAMES & set((source.relative_to(own) if own is not None else inside).parts):
            task_store.add_event(con, task["id"], "artifact_derived_skipped", {"path": str(rel)}, generation=generation)
            continue
        try:
            raw = source.read_bytes()
        except OSError:
            drifted.append(str(rel))
            continue
        digest = hashlib.sha256(raw).hexdigest()
        if digests.get(str(rel)) != digest:
            drifted.append(str(rel))
            continue
        try:
            raw.decode("utf-8")
            binary = False
        except UnicodeDecodeError:
            # Keep binary deliverables by identity. Document tools read their content;
            # registration neither decodes them as prose nor certifies any quotation.
            binary = True
        accepted.append((rel, source, digest, binary))
    if drifted:
        raise ArtifactDrift(task["id"], drifted)
    kind = ARTIFACT_KINDS.get(link["kind"], "task_output")
    for rel, source, digest, binary in accepted:
        runs.register_file(con, run["id"], kind, f"[{task['id']}] {source.name}",
                           str(source), sha256=digest,
                           branch_id=node["id"], task_id=task["id"],
                           metadata={"source_file": str(rel), **({"binary": True} if binary else {})})


def _reopen_for_resubmission(con, task, generation):
    """A drifted card goes back to the Sister as a new attempt, like a stopped one on resume."""
    target = "todo" if task_store.parent_ids(con, task["id"]) else "ready"
    if task_store.reopen_task(con, task["id"], target_status=target,
                              expected_generation=generation, invalidate_descendants=False):
        task_store.add_event(con, task["id"], "research_resumed",
                             {"from_generation": generation, "reason": "artifact_drift"}, generation=generation + 1)
        return True
    return False


def _cards_reopened(con, run, node):
    """Research cards of the node that a settle sent back to their Sisters."""
    return [row["id"] for row in runs.tasks(con, run["id"], kind="research", node_id=node["id"])
            if row["status"] in {"todo", "ready", "running"}]


def _clear_task_outputs(con, run_id, task_id):
    """Drop the derived result set that an older generation of this task supplied. Critiques stay:
    one red-team card reviews every version, each round in its own file, and a round's file that
    is declared again is updated in place by runs.register_file (2026-09-27: round 1's
    critique.md vanished when round 2 settled)."""
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
        "AND kind IN ('task_output','final_critique','divergence')",
        (run_id, task_id),
    )


def _submitted_event_id(con, task_id, generation):
    row = con.execute("SELECT id FROM events WHERE task_id=? AND kind='submitted' AND generation=? "
                      "ORDER BY id DESC LIMIT 1", (task_id, generation)).fetchone()
    return int(row["id"]) if row else None


def _consulted_since_settle(con, task_id, generation):
    """Whether a consultation of this card ended after its latest settle (or before any)."""
    ended = con.execute("SELECT max(id) FROM events WHERE task_id=? AND kind='consultation_ended' "
                        "AND generation=?", (task_id, generation)).fetchone()[0]
    settled = con.execute("SELECT max(id) FROM events WHERE task_id=? AND kind='research_v2_settled' "
                          "AND generation=?", (task_id, generation)).fetchone()[0]
    return ended is not None and (settled is None or ended > settled)


def _settled_current(con, task_id, generation):
    """Whether this generation's latest settle covered its latest submission. A finished card
    declares again when its session keeps working after completion (todo._redeclare_if_done),
    and that newer declaration is registered like the first. A consultation that ended since
    the settle -- stopped half way through an edit, say -- may have changed her files without
    declaring them: the settle runs again, and its digest check finds that as drift."""
    settled = con.execute("SELECT id,payload FROM events WHERE task_id=? AND kind='research_v2_settled' "
                          "AND generation=? ORDER BY id DESC LIMIT 1", (task_id, generation)).fetchone()
    if settled is None:
        return False
    try:
        seen = json.loads(settled["payload"]).get("submitted_event")
    except (AttributeError, TypeError, ValueError):
        seen = None
    return seen == _submitted_event_id(con, task_id, generation) and not _consulted_since_settle(
        con, task_id, generation)


def settle_done_tasks(con, *, run_id, reopen_drift=True):
    """Register accepted artifacts and ingest the Sisters' findings, once per task generation.

    A card whose accepted files changed after submission is recorded (``research_artifact_drift``)
    and, with ``reopen_drift``, sent back to its Sister as a new attempt; a halt path passes False
    and leaves the reopening to ``runs.resume``, which reads the same event.

    Blocking: it hashes every declared file and bundles from the whole corpus, so the driver
    runs it with ``run_in_thread``. On the event loop it froze the window driving the run for
    five minutes and more (2026-10-25/26, R45)."""
    run = runs.get(con, run_id)
    index = None                                      # read once for every card this pass bundles
    for task in runs.tasks(con, run_id):
        if task["status"] != "done":
            continue
        generation = int(task["generation"])
        if _settled_current(con, task["id"], generation):
            continue
        settled = False
        with task_store.write_txn(con):
            # The unlocked scan is only a fast path. Another LO may have settled or
            # resumed this card before we acquired the writer lock.
            current = task_store.get(con, task["id"])
            if current is None or current["generation"] != generation or current["status"] != "done":
                continue
            if _settled_current(con, task["id"], generation):
                continue
            submitted = _submitted_payload(con, current)
            _clear_task_outputs(con, run["id"], task["id"])
            try:
                _register_task_artifacts(con, run, current, submitted)
            except ArtifactDrift as drift:
                task_store.add_event(con, task["id"], "research_artifact_drift", {"paths": drift.paths},
                                     generation=generation)
                _LOG.warning("Research %s: %s", run_id, drift)
                if reopen_drift:
                    _reopen_for_resubmission(con, current, generation)
                continue
            result = {"kind": task["research_kind"]}
            if task["research_kind"] not in runs.REVIEW_KINDS:
                result = ledger.ingest_report(con, run, task, submitted)
            result = {**result, "submitted_event": _submitted_event_id(con, task["id"], generation)}
            task_store.add_event(
                con, task["id"], "research_v2_settled", result, generation=generation
            )
            settled = True
        if settled:                                   # once per generation, after the ledger has its findings
            if index is None:
                index = _project_index(run)
            _bundle(con, run, task=task, index=index)


def _project_index(run):
    """The project index a settle's card bundles share. Each card read its own until 2026-10-27,
    and on a 33,308-document project every read took 27 s (R45). A settle adds nothing to the
    corpus, so one read serves the pass. A read that fails gives None: each card then reads its
    own, and a failure is recorded on the card as before."""
    try:
        return bundle.project_index(run["workspace"])
    except Exception:  # noqa: BLE001 - the card's own read records it
        return None


def _done(con, run, node, kind):
    return [row for row in runs.tasks(con, run["id"], kind=kind, node_id=node["id"]) if row["status"] == "done"]


def _receive_issues(con, run, node, task, *, round, items):
    """Record a review card's frozen submission as issues of this node for review round ``round``.
    Issues already answered keep as they are; otherwise the latest submission replaces them."""
    with runs.owned_txn(con, run, node):
        # Per round: one red-team card reviews every version, and an earlier round's answered
        # issues must neither be replaced nor keep this round's from being recorded.
        recorded = [row for row in runs.issues(con, run["id"], round=round) if row["task_id"] == task["id"]]
        if any(row["disposition"] for row in recorded):
            return
        runs.drop_issues(con, run["id"], [row["id"] for row in recorded])
        for item in items:
            runs.add_issue(con, run["id"], node=node, task_id=task["id"], round=round, **item)


def _receive_critique(con, run, node, task, round):
    """Receive the red team's frozen tool submission: its material issues, for Last Order to dispose of."""
    submitted = _submitted_payload(con, task)
    if "issues" not in submitted:
        return f"Red-team card {task['id']} has not recorded issues through misaka_card_note."
    _receive_issues(con, run, node, task, round=round, items=[
        {"origin": "critique", "kind": item["kind"], "question": item["question"], "rationale": item["rationale"],
         "priority": item["priority"]} for item in submitted["issues"] if item["material"]])
    return None


def _receive_divergence(con, run, node, task, version):
    """Receive the divergence review of version ``version``: its gaps are this node's to fill before
    it forks, and its proposals of possibilities not taken wait for the node's decision. Issues of
    a divergence review are kept per reviewed version, like the red team's."""
    submitted = _submitted_payload(con, task)
    if "alternatives" not in submitted:
        return f"Divergence card {task['id']} has not recorded alternatives through misaka_card_note."
    items = []
    for item in submitted["alternatives"]:
        rationale = f"Premise: {item['premise']}\nThe reviewer's case: {item['rationale']}"
        if item.get("covered_by"):
            rationale += f"\nThe reviewer sees {item['covered_by']} already pursuing it."
        items.append({"kind": item["kind"], "question": item["proposal"], "rationale": rationale,
                      "origin": "gap" if item["gap"] else "divergence"})
    _receive_issues(con, run, node, task, round=version, items=items)
    return None


def _gaps(con, run, node, version):
    """The gaps the divergence review found in version ``version`` of the conclusion."""
    return list(runs.issues(con, run["id"], node_id=node["id"], round=version, origin="gap"))


def _divergence_requests(con, card):
    """Every review the divergence card was asked for, oldest first: ``version`` reviewed, its
    ``round`` (the file it writes), and ``from_generation``, the attempt it went on from."""
    return [json.loads(row["payload"] or "{}") for row in con.execute(
        "SELECT payload FROM events WHERE task_id=? AND kind='divergence_review' ORDER BY id", (card["id"],))]


def _divergence_request(con, card, version):
    return next((r for r in reversed(_divergence_requests(con, card)) if int(r.get("version") or 0) == version), None)


def _divergence_reviewed(con, card, version):
    """Whether the card's current attempt is its review of version ``version``."""
    request = _divergence_request(con, card, version)
    return request is not None and int(card["generation"]) > int(request["from_generation"])


def _divergence_brief(con, run, node, version, *, round):
    """The divergence review of version ``version``: the card's contract in its first round, the
    message it is continued with after -- with what Last Order did with every gap so far."""
    again = None
    if round > 1:
        card = _divergence_card(con, run, node)
        earlier = [r for r in _divergence_requests(con, card) if int(r["round"]) < round]
        gaps = [row for r in earlier for row in _gaps(con, run, node, int(r["version"]))]
        again = {"round": round,
                 "previous": runs.artifact_path(_synthesis(con, run, node, int(earlier[-1]["version"]))),
                 "reviews": _divergence_paths(con, run, node, card),
                 "dispositions": prompt_guard.untrusted("dispositions", planner.disposition_table(gaps))}
    return planner.divergence_body(
        node, synthesis_path=runs.artifact_path(_synthesis(con, run, node, version)),
        plan_path=_artifact_paths(con, run, node, "plan"), graph_path=views.graph_path(run),
        paths_path=views.paths_path(run), deliverable=runs.versioned("divergence.md", round), rereview=again)


def _divergence_paths(con, run, node, card):
    """The divergence reviews on file, every round's ``divergence*.md``."""
    return [runs.artifact_path(row) for row in runs.artifacts(con, run["id"], kind="divergence", task_id=card["id"])
            if os.path.splitext(row["path"])[1].lower() == ".md"]


def _earlier_divergence(con, run, node, version):
    """What the divergence review of version ``version`` - 1 left, for the gap revision that makes
    version ``version``: the previous conclusion, the review, and what Last Order did with each gap."""
    previous = version - 1
    card = _divergence_card(con, run, node)
    request = _divergence_request(con, card, previous)
    name = runs.versioned("divergence.md", int(request["round"])) if request else None
    return {
        "previous": runs.artifact_path(_synthesis(con, run, node, previous)),
        "last_critiques": [path for path in _divergence_paths(con, run, node, card) if os.path.basename(path) == name],
        "last_dispositions": planner.disposition_table(_gaps(con, run, node, previous)),
    }


def _divergence_card(con, run, node):
    rows = runs.tasks(con, run["id"], kind="divergence", node_id=node["id"])
    return rows[0] if rows else None


async def _return_review(con, run, node, red, issues, reason, *, round, session=None):
    """Record a review that needs no answer in its owning LO conversation, without waking a model:
    round ``round``'s critique, the one this version got."""
    from misaka.core.session_manager import SessionManager

    path = planner.lo_session_file(run, node)
    if not path or not os.path.isfile(path):
        raise RuntimeError(f"Node {node['id']} has no persisted Last Order session for its review.")
    delivery = f"research-review:{run['id']}:{node['id']}:{red['id']}:{red['generation']}"
    content = f"# Red-team review returned\n\n{reason}\n" + await asyncio.to_thread(
        planner.review_context, con, run, red, issues, round=round)
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


def _close(con, run, node, status):
    """End the node's own work. Its pending options are the level reconciliation's, and its children
    run on their own: neither holds the node open. Artifacts are already in the project; nothing moves.
    Blocking like ``settle_done_tasks`` (a bundle and an index), and run off the event loop the same way."""
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
            con.execute("UPDATE research_execution SET last_error=? WHERE id=?", (reason, node["id"]))
            runs.set_state(con, run["id"], error=f"node {node['id']}: {reason}")
    _bundle(con, run, node=node)                      # the node's folder is complete: gather what it rests on
    _index(con, run, node)
    runs.set_node(con, node["id"], status=status, owner=(run, node))
    return status


def _index(con, run, node):
    """Index a closed node's texts in the workspace corpus, so the reconciliation, later nodes and
    the final report read them by document id. Derived like a bundle: a failure is logged and the
    node closes all the same."""
    try:
        report.index_run(con, run, node)
    except Exception as error:  # noqa: BLE001 - derived output must never undo a settled state
        _LOG.warning("research %s: indexing node %s failed: %s", run["id"], node["id"], error)


def _append_corrections(con, run, node, version, dispositions):
    """Append the ``correct`` dispositions of the final review to the final conclusion, as its
    Corrections section: the wrong figure stays legible, the corrected text stands beside it."""
    corrections = [item for item in dispositions if item["disposition"] == "correct"]
    if not corrections:
        return
    # The file as it is, not as registered: a Last Order told to state the corrected text has
    # also applied it to the file herself (2026-09-29, a patch script and an edit before the
    # command), and the integrity check on the registered text failed the node. The rewrite
    # registers what stands.
    row = _synthesis(con, run, node, version)
    text = Path(row["path"]).read_text(encoding="utf-8").rstrip("\n") + "\n\n## Corrections\n\nRecorded after the " \
        "last review; each supersedes the text it names.\n\n" + "\n".join(f"- {item['reason']}" for item in corrections) + "\n"
    _write(con, run, node, "synthesis", f"Conclusion · {_label(node)} · version {version}",
           runs.versioned("synthesis.md", version), text, version=version)


def _revisions(con, run, node, *, before=None, loop="dispose"):
    """How many times the node has chosen to revise its conclusion in one review loop -- the red
    team's (``dispose``) or the divergence review's (``gaps``): its rounds whose dispositions include
    a revise -- counting only rounds before version ``before`` when given."""
    count = 0
    for row in runs.actions(con, run["id"], node["id"], f"{loop}:"):
        if before is not None and int(row["action_key"].split(":", 1)[1]) >= before:
            continue
        count += any(item["disposition"] == "revise" for item in row["payload"]["dispositions"])
    return count


def _synthesis(con, run, node, version):
    """The node's conclusion of the given version, when written."""
    rows = views.syntheses(con, run, node)
    return rows[version - 1] if len(rows) >= version else None


def _red_team_assignee(con, run, node):
    """The node's reviewer: whoever took its first review, else the first plan's choice. The same
    Sister reviews every version and proposes the alternatives."""
    first = runs.tasks(con, run["id"], kind="red_team", node_id=node["id"])
    if first:
        return first[0]["assignee"]
    return runs.red_team(con, run["id"], node["id"])["assignee"]


def _deliberation(con, run, node, version):
    """This review round's deliberation and the last conversation entry it covers. A re-review
    reads on from where the previous round's deliberation stopped -- the red team holds that one
    already, in her own conversation or by path -- and none of it repeats a conclusion or plan she
    is given by path (2026-09-27: version 2's file ran to 168 KB, most of it both of those)."""
    session_file = planner.lo_session_file(run, node)
    start, earlier = node["fork_entry"], None
    for row in runs.artifacts(con, run["id"], kind="deliberation", branch_id=node["id"]):
        metadata = json.loads(row["metadata_json"] or "{}")
        if int(metadata.get("version") or 1) < version and metadata.get("through"):
            start, earlier = metadata["through"], runs.artifact_path(row)
    given = []
    for row in [*views.syntheses(con, run, node), *runs.artifacts(con, run["id"], kind="plan", branch_id=node["id"])]:
        with contextlib.suppress(OSError):
            given.append(Path(runs.artifact_path(row)).read_text(encoding="utf-8"))
    through = planner.session_leaf(session_file)
    return planner.deliberation_text(session_file, start, given=given, earlier=earlier), through


def _critique_paths(con, run, node, rounds):
    """The red team's critiques of the given versions: one card reviews them all, each version in
    its own file (``critique.md``, ``critique-2.md`` ...)."""
    cards = {task["id"] for task in runs.tasks(con, run["id"], kind="red_team", node_id=node["id"])}
    names = {runs.versioned("critique.md", r) for r in rounds}
    return list(dict.fromkeys(
        runs.artifact_path(row) for row in runs.artifacts(con, run["id"], kind="critique", branch_id=node["id"])
        if row["task_id"] in cards and os.path.basename(row["path"]) in names))


def _earlier_review(con, run, node, version):
    """What the review of version ``version`` - 1 left: the previous conclusion, its critiques and
    what Last Order did with each issue. Material for the revision and for the next review."""
    previous = version - 1
    earlier = range(1, version)
    return {
        "previous": runs.artifact_path(_synthesis(con, run, node, previous)),
        "critiques": _critique_paths(con, run, node, earlier),
        "last_critiques": _critique_paths(con, run, node, [previous]),
        "dispositions": planner.disposition_table(
            [row for row in runs.issues(con, run["id"], node_id=node["id"], origin="critique") if row["round"] < version]),
        "last_dispositions": planner.disposition_table(
            runs.issues(con, run["id"], node_id=node["id"], round=previous, origin="critique")),
    }


def _red_team_card(con, run, node):
    """The node's red-team card. One card reviews every version: a re-review continues it, in her
    own conversation, as a new generation (``_continue_review``), the way ``misaka_sister_message``
    continues a settled card."""
    rows = runs.tasks(con, run["id"], kind="red_team", node_id=node["id"])
    return rows[0] if rows else None


def _review_request(con, red, version):
    """How the card took up version ``version``: ``from_generation`` is the attempt it went on from
    (0: the card's own first attempt). None before the version was asked of it."""
    if version <= 1:
        return {"round": 1, "from_generation": 0}
    for row in con.execute("SELECT payload FROM events WHERE task_id=? AND kind='research_review' ORDER BY id DESC",
                           (red["id"],)):
        payload = json.loads(row["payload"] or "{}")
        if int(payload.get("round") or 0) == version:
            return payload
    return None


def _reviewed(con, red, version):
    """Whether the card's current attempt is its review of version ``version``."""
    request = _review_request(con, red, version)
    return request is not None and int(red["generation"]) > int(request["from_generation"])


def _red_team_brief(con, run, node, version, assignee):
    """What the red team is given for version ``version``: the card's contract for its first review,
    the message it is continued with after. Her reasoning is a fourth material, distinct from the
    three she already holds (conclusion and plan by path, evidence inline): only the thinking and
    working prose of the node's own turns, written once per version."""
    written = [row for row in runs.artifacts(con, run["id"], kind="deliberation", branch_id=node["id"])
               if int(json.loads(row["metadata_json"] or "{}").get("version") or 1) == version]
    if not written:
        deliberation, through = _deliberation(con, run, node, version)
        if deliberation:
            _write(con, run, node, "deliberation", f"Deliberation · {_label(node)}",
                   runs.versioned("deliberation.md", version), deliberation, version=version, through=through)
            written = [None]
    rereview = None
    if version > 1:
        earlier = _earlier_review(con, run, node, version)
        rereview = {"round": version, "previous": earlier["previous"], "critiques": earlier["critiques"],
                    "dispositions": prompt_guard.untrusted("dispositions", earlier["dispositions"])}
    return planner.red_team_body(
        node, synthesis_path=runs.artifact_path(_synthesis(con, run, node, version)),
        plan_path=_artifact_paths(con, run, node, "plan"), graph_path=views.graph_path(run),
        deliberation_path=_artifact_path(con, run, node, "deliberation") if written else None,
        evidence=planner.evidence_block(con, run, node),
        own_cards=[row for row in _done(con, run, node, "research") if row["assignee"] == assignee],
        rereview=rereview, deliverable=runs.versioned("critique.md", version))


async def _after_review(con, run, node, set_node):
    """Where a node goes once its red-team loop is over: to the divergence review when it can still
    fork -- the gaps it left are filled first, its possibilities not taken become its decision --
    else it has finished its own work."""
    if node["depth"] < runs.current_limits(con, run)["max_depth"]:
        set_node(status="diverging")
        return None
    return await run_in_thread(_close, con, run, node, "closed")


async def _expand(con, cfg, runner, worker, run, node, *, context, tool_call_id, poll_seconds, progress, session=None):
    """Advance one node through its routine. Returns the node's terminal status, a halt
    ("stopped" / "budget"), or a waiting_input result dict."""
    try:
        result = await _expand_owned(con, cfg, runner, worker, run, node, context=context,
                                     tool_call_id=tool_call_id, poll_seconds=poll_seconds,
                                     progress=progress, session=session)
        await _views(con, run)                        # the node's last transition, shown before its process ends
        return result
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
    ("stopped" / "budget"), or a waiting_input result dict. Every phase rebuilds what it needs
    from what is already recorded, so a resumed node picks up its own work where it was."""
    nid = node["id"]
    owner_run, owner_node = run, node

    def check(*, allow_stop=False):
        runs.check_owner(con, owner_run, owner_node, allow_stop=allow_stop)

    def set_node(**fields):
        return runs.set_node(con, nid, owner=(owner_run, owner_node), **fields)

    async def drive(kind):
        """Drive the node's cards of ``kind``. Before cards that failed for good fail the node, its
        Last Order decides: send them back, go on without them, or let the node fail -- the node
        used to fail at once and wait for the root to retry it (2026-09-29: provider refusals).
        While research cards run, she can change them (``misaka_research_cards``)."""
        if kind != "research":
            return await drive_cards(kind)
        round = runs.current_plan(con, run["id"], nid)[0] or 1
        current = runs.node(con, nid)
        tools = [commands.plan_change_tool(
            con, run, current, round=round, session_file=current["session_file"] or run["root_session"],
            validate=lambda change: _check_plan_change(
                con, run, current, round, planner._roster({**cfg, "workspace": run["workspace"]}), change))]
        control = _session_control(session)
        tooled = session if hasattr(session, "registerCustomTools") else None   # a test double has no tools
        if tooled is not None:
            tooled.registerCustomTools(tools)            # the window's own turns see it
        if control is not None:
            control.review_tools = tools                 # an attached chat's turns, and `research --tell`'s
        applied = [0]

        async def refresh():
            if not await _apply_plan_changes(con, run, current, round, applied, progress, (owner_run, owner_node),
                                             pending=getattr(runner, "pending", None)):
                return None
            return {row["id"] for row in runs.tasks(con, run["id"], kind=kind, node_id=nid)} - runs.given_up_cards(con, run["id"], nid)

        try:
            return await drive_cards(kind, refresh=refresh)
        finally:
            if tooled is not None:
                tooled.unregisterCustomTools(tools)
            if control is not None and control.review_tools is tools:
                control.review_tools = ()

    async def drive_cards(kind, refresh=None):
        while True:
            cards = runs.tasks(con, run["id"], kind=kind, node_id=nid)
            scope = {row["id"] for row in cards} - runs.given_up_cards(con, run["id"], nid)
            outcome = await _drive_tasks(con, cfg, runner, run["id"], scope=scope, context=context,
                                         tool_call_id=tool_call_id, poll_seconds=poll_seconds, progress=progress,
                                         session=session, check_active=lambda: check(allow_stop=True),
                                         owner=(owner_run, owner_node), refresh=refresh)
            lost = [row for row in runs.tasks(con, run["id"], kind=kind, node_id=nid)
                    if row["id"] in scope and (row["status"] in ("failed", "stopped") or runs.parked(con, row))]
            if outcome != "failed" or not lost:
                return outcome
            await _progress(progress, "cards_failed", f"{_label(node)}: {len(lost)} card(s) failed; its Last Order "
                            "decides whether to retry them, go on without them, or fail.", run, node=nid,
                            **{ANNOUNCES_PHASE: True})
            try:
                decision = await asyncio.to_thread(planner.cards_failed, con, run, cfg, worker, node,
                                                   kind=kind, lost=lost)
            except Exception as error:  # noqa: BLE001 - the decision is a chance to go on, not a new way to fail
                await _progress(progress, "cards_failed", f"{_label(node)}: no decision ({error}); the node fails.",
                                run, node=nid)
                return "failed"
            await _progress(progress, "cards_failed", f"{_label(node)}: Last Order chose to {decision['decision']} -- "
                            f"{decision['reason']}", run, node=nid)
            if decision["decision"] == "fail":
                return "failed"
            if decision["decision"] == "retry":
                with runs.owned_txn(con, owner_run, owner_node):
                    runs.reopen_cards(con, run["id"], nid)

    async def fail(card, reason):
        task_store.add_event(con, card["id"], "research_review_missing", {"reason": reason})
        with runs.owned_txn(con, owner_run, owner_node):
            con.execute("UPDATE research_execution SET last_error=? WHERE id=?", (reason, nid))
            runs.set_state(con, run["id"], error=f"node {nid}: {reason}")
        return await run_in_thread(_close, con, run, node, "failed")

    while True:
        await _wait_unpaused(con, run["id"], session)
        with runs.owned_txn(con, owner_run, owner_node, allow_stop=True):
            node = runs.node(con, nid)
        if runs.stop_requested(con, run["id"]):
            scope = {row["id"] for row in runs.tasks(con, run["id"], node_id=nid)}
            return await _drive_tasks(con, cfg, runner, run["id"], scope=scope, context=context,
                                      poll_seconds=poll_seconds, owner=(owner_run, owner_node))
        await _views(con, run)
        status = node["status"]
        if status == "queued":
            set_node(status="planning")

        elif status in ("planning", "waiting_input", "awaiting_approval"):
            round, command = runs.current_plan(con, run["id"], nid)
            round = round or 1
            plan = command["payload"] if command else None
            context_path = None
            if plan is None and not runs.is_root(node):
                # Off the loop: it locates the run's quotations in their documents (0.18.10 sweep).
                _aid, context_path, _packet = await asyncio.to_thread(context_packet.create, con, run, node=node)
            if plan is None:
                await _progress(progress, "planning", f"Last Order is planning {_label(node)}.", run)
                plan, _raw, session_file = await asyncio.to_thread(
                    planner.plan, run, cfg, worker, node, con=con, context_path=context_path)
            else:
                session_file = command["session_file"]
                # Planning again (a resume, a withdrawn follow-up): the plan as she changed it while
                # its cards ran. As first accepted, a card she cancelled came back (0.18.10 sweep).
                plan = _changed_plan(plan, runs.plan_changes(con, run["id"], nid, round))
            # The tool command is the checkpoint. Rebuild its projections even if the
            # process died just after acceptance, or these still show a prior clarification.
            _write_plan(con, run, node, plan, round)
            if runs.is_root(node):
                session_file = run["root_session"] or session_file
            set_node(session_file=session_file)
            if runs.is_root(node):
                with runs.owned_txn(con, owner_run, owner_node):
                    runs.set_state(con, run["id"], root_session=session_file)
            if plan["status"] == "clarify":
                set_node(status="waiting_input")
                return {"reason": "waiting_input", "questions": plan["clarifying_questions"],
                        "run": runs.summary(con, run["id"])}
            if planner.plan_needs_user(cfg, run, plan) and not runs.plan_started(con, run["id"], nid):
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
            if runs.is_root(node):
                with runs.owned_txn(con, owner_run, owner_node):
                    planner.publish_project_brief(run["workspace"], plan, round=round)
            if round == 1 and plan.get("decisions"):
                with runs.owned_txn(con, owner_run, owner_node):
                    for index, decision in enumerate(plan["decisions"]):
                        runs.add_decision(con, run["id"], node=node, round=1, origin="plan", index=index,
                                          question=decision["question"], stakes=decision["stakes"],
                                          options=decision["options"])
            await _progress(progress, "plan_ready",
                            f"Planning produced {len(plan['tasks'])} research tasks"
                            + (f" and {len(plan['decisions'])} decision(s)" if round == 1 and plan.get("decisions") else "")
                            + f" for {_label(node)}" + (f" (round {round})." if round > 1 else "."), run,
                            tasks=[{"title": t["title"], "assignee": t["assignee"], "local_id": t["local_id"],
                                    "dependencies": t.get("dependencies") or []} for t in plan["tasks"]],
                            red_team=plan.get("red_team"), plan=plan, round=round)
            if not plan["tasks"] and (command is None or not command["payload"]["tasks"]):
                # A pure branch point: nothing of its own to research; its options open at the
                # level's reconciliation.
                await _progress(progress, "branch_point",
                                f"{_label(node)} is a branch point: its options open when the level is reconciled.", run)
                return await run_in_thread(_close, con, run, node, "closed")
            if not plan["tasks"]:
                # Every card of the round cancelled while they ran: closed as a branch point, the
                # node had no conclusion. A follow-up round is withdrawn and the node concludes on
                # what it has; a first round has nothing to conclude on.
                if not _done(con, run, node, "research"):
                    return await run_in_thread(_close, con, run, node, "failed")
                _withdraw_emptied_round(con, run, node, round)
                set_node(status="synthesizing")
                continue
            previous = [row for row in runs.tasks(con, run["id"], kind="research", node_id=nid)
                        if row["status"] == "done" and int(row["round"] or 1) < round]
            await _submit_tasks(con, run, node, plan["tasks"], kind="research", progress=progress,
                                round=round, previous=previous)
            set_node(status="executing")

        elif status == "executing":
            outcome = await drive("research")
            if outcome == "failed":
                return await run_in_thread(_close, con, run, node, "failed")
            if outcome != "done":
                return outcome
            check()
            await run_in_thread(settle_done_tasks, con, run_id=run["id"])
            reopened = _cards_reopened(con, run, node)
            if reopened:
                await _progress(progress, "resubmitting",
                                f"{len(reopened)} card(s) changed after submission and went back to their Sisters "
                                "to declare again.", run, task_ids=reopened)
                continue
            if not _done(con, run, node, "research"):
                return await run_in_thread(_close, con, run, node, "failed")
            set_node(status="synthesizing")

        elif status == "synthesizing":
            # The conclusion is written from every card, so the node waits for each to settle
            # (2026-10-01: version 2 was written while card B answered, and B was missing from
            # its material map). A finished card answering a message -- a Sister's question,
            # Last Order's consultation -- stays done (R40); her answer's changes are declared
            # when her turn ends, so the node waits for the consultation and settles again.
            cards_here = runs.tasks(con, run["id"], kind="research", node_id=nid)
            if any(row["status"] in ("running", "review") for row in cards_here):
                set_node(status="executing")
                continue
            if any(task_store.live_consultation(con, row["id"]) is not None for row in cards_here):
                check()
                await asyncio.sleep(poll_seconds)
                continue
            if any(row["status"] == "done" and _consulted_since_settle(con, row["id"], int(row["generation"]))
                   for row in cards_here):
                await run_in_thread(settle_done_tasks, con, run_id=run["id"])
                if _cards_reopened(con, run, node):
                    set_node(status="executing")    # a declaration that no longer matches its files
                    continue
            done = _done(con, run, node, "research")
            # The round whose cards she is reading is the latest one that has cards, not the
            # latest plan: a follow-up plan recorded just before a crash has no cards yet. A
            # round whose cards all failed, and that she chose to conclude without, has been
            # worked: counted by done cards alone, it sent the node back to planning that same
            # round, for ever (0.18.10 sweep).
            round = max((int(row["round"] or 1) for row in runs.tasks(con, run["id"], kind="research", node_id=nid)),
                        default=1)
            later = runs.plan_round(con, run["id"], nid)
            if later > round and (later_plan := runs.action(con, run["id"], nid, runs.plan_key(later))) and not _changed_tasks(
                    later_plan["payload"]["tasks"], runs.plan_changes(con, run["id"], nid, later)):
                _withdraw_emptied_round(con, run, node, later)
            left = runs.current_limits(con, run)["max_followups"] - (round - 1)    # follow-ups still allowed on this node
            version = _revisions(con, run, node) + _revisions(con, run, node, loop="gaps") + 1
            # The red team reviews the versions of its own loop; once the divergence review has
            # begun, a revision is reviewed by her divergence review again.
            reviewer = "diverging" if _divergence_card(con, run, node) is not None else "critiquing"
            if _synthesis(con, run, node, version):
                set_node(status=reviewer)
                continue
            if _followup_recorded(con, run, node, round):   # a crash after she asked for more cards
                set_node(status="planning")
                continue
            await _progress(progress, "synthesizing",
                            (f"Last Order is revising the conclusion of {_label(node)} (version {version})"
                             if version > 1 else f"Last Order is reading the cards of {_label(node)}")
                            + (f" (round {round}; {max(left, 0)} more round(s) possible)" if round > 1 or left > 0 else "")
                            + ".", run)
            followup = _followup_tool(con, cfg, run, node, round) if left > 0 else None
            with runs.owned_txn(con, owner_run, owner_node):
                # A dissolution belongs to the conclusion written with it: one recorded by an
                # interrupted turn, before this version exists, is no one's.
                runs.delete_action(con, run["id"], nid, runs.dissolve_key(version))
            revision = None
            if version > 1:
                earlier = (_earlier_divergence(con, run, node, version) if reviewer == "diverging"
                           else _earlier_review(con, run, node, version))
                revision = {"version": version, "review": version - 1, "previous": earlier["previous"],
                            "critiques": earlier["last_critiques"], "dispositions": earlier["last_dispositions"],
                            "reviewer": "divergence" if reviewer == "diverging" else "red_team"}
            text = await asyncio.to_thread(planner.synthesize, con, run, cfg, worker, node, done,
                                           followup=followup, round=round, left=max(left, 0), revision=revision,
                                           dissolve=_dissolve_tool(con, run, node, version),
                                           erratum=_erratum_tool(con, run, node))
            if _followup_recorded(con, run, node, round):
                with runs.owned_txn(con, owner_run, owner_node):
                    runs.delete_action(con, run["id"], nid, runs.dissolve_key(version))
                new_round, command = runs.current_plan(con, run["id"], nid)
                _write_plan(con, run, node, command["payload"], new_round)
                await _progress(progress, "followup_planned",
                                f"Last Order asked for round {new_round} on {_label(node)}: "
                                f"{len(command['payload']['tasks'])} more card(s).", run, node=nid, round=new_round)
                set_node(status="planning")
                continue
            _write(con, run, node, "synthesis", f"Conclusion · {_label(node)}" + (f" · version {version}" if version > 1 else ""),
                   runs.versioned("synthesis.md", version), text, version=version)
            set_node(status=reviewer)

        elif status == "critiquing":
            version = len(views.syntheses(con, run, node))
            red = _red_team_card(con, run, node)
            if red is None:
                assignee = _red_team_assignee(con, run, node)
                await _progress(progress, "red_team",
                                f"Preparing Sister {assignee}'s red-team review of {_label(node)}"
                                + (f", version {version}" if version > 1 else "") + ".", run)
                plan = runs.action(con, run["id"], nid, runs.plan_key(1))["payload"]
                spec = {
                    # '@' is outside LO task IDs, so research named 'red-team' cannot collide.
                    "local_id": "@red-team", "title": f"Red team · {_label(node)}",
                    "question": node["question"], "rationale": (plan.get("red_team") or {}).get("reason") or "",
                    "assignee": assignee, "deliverable": runs.versioned("critique.md", version),
                    "instructions": _red_team_brief(con, run, node, version, assignee),
                }
                await _submit_tasks(con, run, node, [spec], kind="red_team", progress=progress, round=version)
                red = _red_team_card(con, run, node)
                if version > 1:                      # its first attempt is this version's review
                    task_store.add_event(con, red["id"], "research_review", {"round": version, "from_generation": 0},
                                         generation=int(red["generation"]))
            elif not _reviewed(con, red, version):
                request = _review_request(con, red, version)
                if request is None:
                    await _progress(progress, "red_team",
                                    f"Sister {red['assignee']} goes on with her review of {_label(node)}: "
                                    f"version {version}.", run)
                    task_store.add_event(con, red["id"], "research_review",
                                         {"round": version, "from_generation": int(red["generation"])},
                                         generation=int(red["generation"]))
                    request = _review_request(con, red, version)
                try:
                    await runner.continue_card(red["id"], _red_team_brief(con, run, node, version, red["assignee"]),
                                               expected_generation=int(request["from_generation"]))
                except (ValueError, RuntimeError, OSError) as error:
                    await _progress(progress, "red_team", f"Sister {red['assignee']}'s card could not be continued "
                                    f"yet ({error}); trying again.", run)
                    await asyncio.sleep(poll_seconds)
                    continue
            outcome = await drive("red_team")
            # Terminal, as in the executing phase: a node left non-terminal by a process that has
            # already exited is what the driver turns into a dead run.
            if outcome == "failed":
                return await run_in_thread(_close, con, run, node, "failed")
            if outcome != "done":
                return outcome
            check()
            await run_in_thread(settle_done_tasks, con, run_id=run["id"])
            red = _red_team_card(con, run, node)
            if red is None:
                return await run_in_thread(_close, con, run, node, "failed")
            if not _reviewed(con, red, version):
                await asyncio.sleep(poll_seconds)      # the continuation has not taken the card yet
                continue
            if version > 1:
                # The card's contract names the first review's file; a later version's file is named
                # in the message that continued the card, so it is checked here, not by her gate.
                critique = Path(red["output_dir"], runs.versioned("critique.md", version))
                if not critique.is_file() or critique.stat().st_size == 0:
                    return await fail(red, f"Red-team card {red['id']} did not deliver {critique.name} for version {version}.")
            unusable = _receive_critique(con, run, node, red, version)
            if unusable:
                return await fail(red, unusable)
            if runs.issues(con, run["id"], node_id=nid, round=version, origin="critique"):
                set_node(status="disposing")
                continue
            reason = "The red team reported no material issues" + (f" on version {version}." if version > 1 else ".")
            try:
                await _return_review(con, run, node, red, [], reason, round=version, session=session)
            except InterruptedError:
                return "stopped"
            await _progress(progress, "review_returned", f"{_label(node)}: {reason}", run)
            closed = await _after_review(con, run, node, set_node)
            if closed is not None:
                return closed

        elif status == "disposing":
            version = len(views.syntheses(con, run, node))
            red = _red_team_card(con, run, node)
            if red is None or red["status"] != "done" or not _reviewed(con, red, version):
                raise RuntimeError(f"Node {nid} has no completed red-team review to answer.")
            issues = list(runs.issues(con, run["id"], node_id=nid, round=version, origin="critique"))
            revisions_left = runs.current_limits(con, run)["max_revisions"] - _revisions(con, run, node, before=version)
            await _progress(progress, "review_returned",
                            f"The red-team review of {_label(node)} is back with its Last Order: "
                            f"{len(issues)} material issue(s) to answer.", run, node=nid, **{ANNOUNCES_PHASE: True})
            dispositions = await asyncio.to_thread(planner.dispose, con, run, cfg, worker, node, red, issues,
                                                   round=version, revisions_left=revisions_left)
            with runs.owned_txn(con, owner_run, owner_node):
                for item in dispositions:
                    runs.dispose(con, item["issue_id"], item["disposition"], covered_by=item["covered_by"] or None,
                                 reason=item["reason"])
            _append_corrections(con, run, node, version, dispositions)
            counts = {}
            for item in dispositions:
                counts[item["disposition"]] = counts.get(item["disposition"], 0) + 1
            await _progress(progress, "disposed", f"{_label(node)}: "
                            + ", ".join(f"{name} {count}" for name, count in sorted(counts.items())) + ".", run,
                            dispositions=counts)
            if counts.get("revise"):
                set_node(status="synthesizing")
                continue
            closed = await _after_review(con, run, node, set_node)
            if closed is not None:
                return closed

        elif status == "diverging":
            # After the red-team loop, in a session of its own: the possibilities the conclusion did
            # not take, and the gaps it left. The gaps are this node's to fill, and every version a
            # gap revision makes is reviewed again, until a review finds no gap or the node's gap
            # revisions are spent; only then do the possibilities become its decision (2026-10-01).
            version = len(views.syntheses(con, run, node))
            card = _divergence_card(con, run, node)
            if card is None:
                assignee = _red_team_assignee(con, run, node)
                await _progress(progress, "divergence", f"Preparing Sister {assignee}'s divergence review of "
                                f"{_label(node)}" + (f", version {version}" if version > 1 else "") + ".", run)
                spec = {"local_id": "@divergence", "title": f"Divergence review · {_label(node)}",
                        "question": node["question"], "rationale": "Alternatives the conclusion did not take.",
                        "assignee": assignee, "deliverable": "divergence.md",
                        "instructions": _divergence_brief(con, run, node, version, round=1)}
                await _submit_tasks(con, run, node, [spec], kind="divergence", progress=progress)
                card = _divergence_card(con, run, node)
                task_store.add_event(con, card["id"], "divergence_review",
                                     {"version": version, "round": 1, "from_generation": 0},
                                     generation=int(card["generation"]))
            elif not _divergence_reviewed(con, card, version):
                request = _divergence_request(con, card, version)
                if request is None:
                    await _progress(progress, "divergence",
                                    f"Sister {card['assignee']} goes on with her divergence review of "
                                    f"{_label(node)}: version {version}.", run)
                    task_store.add_event(con, card["id"], "divergence_review",
                                         {"version": version, "round": len(_divergence_requests(con, card)) + 1,
                                          "from_generation": int(card["generation"])},
                                         generation=int(card["generation"]))
                    request = _divergence_request(con, card, version)
                try:
                    await runner.continue_card(card["id"], _divergence_brief(con, run, node, version,
                                                                             round=request["round"]),
                                               expected_generation=int(request["from_generation"]))
                except (ValueError, RuntimeError, OSError) as error:
                    await _progress(progress, "divergence", f"Sister {card['assignee']}'s card could not be "
                                    f"continued yet ({error}); trying again.", run)
                    await asyncio.sleep(poll_seconds)
                    continue
            outcome = await drive("divergence")
            if outcome == "failed":
                return await run_in_thread(_close, con, run, node, "failed")
            if outcome != "done":
                return outcome
            check()
            await run_in_thread(settle_done_tasks, con, run_id=run["id"])
            card = _divergence_card(con, run, node)
            if not _divergence_reviewed(con, card, version):
                await asyncio.sleep(poll_seconds)      # the continuation has not taken the card yet
                continue
            request = _divergence_request(con, card, version)
            if request["round"] > 1:
                # As with the red team: a later round's file is named in the message that continued her.
                review = Path(card["output_dir"], runs.versioned("divergence.md", request["round"]))
                if not review.is_file() or review.stat().st_size == 0:
                    return await fail(card, f"Divergence card {card['id']} did not deliver {review.name} for version {version}.")
            unusable = _receive_divergence(con, run, node, card, version)
            if unusable:
                return await fail(card, unusable)
            if _gaps(con, run, node, version):
                set_node(status="filling_gaps")
                continue
            set_node(status="deciding")

        elif status == "filling_gaps":
            version = len(views.syntheses(con, run, node))
            card = _divergence_card(con, run, node)
            if card is None or card["status"] != "done" or not _divergence_reviewed(con, card, version):
                raise RuntimeError(f"Node {nid} has no completed divergence review to answer.")
            gaps = _gaps(con, run, node, version)
            revisions_left = runs.current_limits(con, run)["max_revisions"] - _revisions(con, run, node, before=version, loop="gaps")
            await _progress(progress, "review_returned",
                            f"The divergence review of {_label(node)} is back with its Last Order: "
                            f"{len(gaps)} gap(s) to fill or answer.", run, node=nid, **{ANNOUNCES_PHASE: True})
            dispositions = await asyncio.to_thread(planner.fill_gaps, con, run, cfg, worker, node, card, gaps,
                                                   version=version, round=_divergence_request(con, card, version)["round"],
                                                   revisions_left=revisions_left)
            with runs.owned_txn(con, owner_run, owner_node):
                for item in dispositions:
                    runs.dispose(con, item["issue_id"], item["disposition"], covered_by=item["covered_by"] or None,
                                 reason=item["reason"])
            counts = {}
            for item in dispositions:
                counts[item["disposition"]] = counts.get(item["disposition"], 0) + 1
            await _progress(progress, "disposed", f"{_label(node)}: "
                            + ", ".join(f"{name} {count}" for name, count in sorted(counts.items())) + ".", run,
                            dispositions=counts)
            set_node(status="synthesizing" if counts.get("revise") else "deciding")

        elif status == "deciding":
            review = _divergence_card(con, run, node)
            proposals = runs.issues(con, run["id"], node_id=nid, origin="divergence")
            branches = [row for row in runs.issues(con, run["id"], node_id=nid)
                        if row["origin"] in ("critique", "gap") and row["disposition"] == "branch"]
            if review is None:
                return await run_in_thread(_close, con, run, node, "failed")
            divergence = [{"path": runs.artifact_path(row), "content": runs.artifact_text(row)}
                          for row in runs.artifacts(con, run["id"], kind="divergence", task_id=review["id"])
                          if os.path.splitext(row["path"])[1].lower() == ".md"]
            await _progress(progress, "deciding", f"Last Order is deciding which forks {_label(node)} opens: "
                            f"{len(proposals)} proposal(s), {len(branches)} issue(s) taken to the decision.", run)
            payload = await asyncio.to_thread(planner.decide, con, run, cfg, worker, node, divergence=divergence,
                                              proposals=proposals, branches=branches)
            _apply_decisions(con, owner_run, owner_node, node, payload)
            await _progress(progress, "decided", f"{_label(node)}: {len(payload['decisions'])} decision(s), "
                            f"{len(payload['declined'])} proposal(s) declined.", run)
            return await run_in_thread(_close, con, run, node, "closed")

        else:
            raise RuntimeError(f"Node {nid} is in an unknown state: {status}")


def _apply_decisions(con, owner_run, owner_node, node, payload):
    """Record the node's decisions and what became of each proposal and branch issue. Replays land
    on the same ids."""
    with runs.owned_txn(con, owner_run, owner_node):
        for index, decision in enumerate(payload["decisions"]):
            did = runs.add_decision(con, owner_run["id"], node=node, round=1, origin="decide", index=index,
                                    question=decision["question"], stakes=decision["stakes"],
                                    options=decision["options"])
            for option, row in zip(decision["options"], runs.options(con, owner_run["id"], decision_id=did), strict=True):
                for source in option["sources"]:
                    current = runs.issue(con, source)
                    runs.dispose(con, source, "branch", option=row["id"],
                                 reason=current["reason"] if current["origin"] != "divergence" else None)
        for item in payload["declined"]:
            current = runs.issue(con, item["issue_id"])
            # A red-team issue taken to the decision keeps why it was taken there beside why it was not opened.
            reason = (f"{current['reason']} -- not opened: {item['reason']}" if current["origin"] != "divergence"
                      else item["reason"])
            if item["covered_by"] and runs.option(con, item["covered_by"]):   # an option waiting to be opened takes it up
                runs.dispose(con, item["issue_id"], "covered", option=item["covered_by"], reason=reason)
            else:
                runs.dispose(con, item["issue_id"], item["disposition"], covered_by=item["covered_by"] or None,
                             reason=reason)


def _halt_at_cap(con, run):
    """A wait for the user (a plan's go-ahead, a consent) that the run's cap ended meanwhile -- the
    talk with Last Order spent it -- stops as a cap halt, resumable, instead of waiting for a stop
    by hand (0.18.9 sweep). The run's own handler turns the error into the halt."""
    if budget.exhausted(con, task_id=run["id"]):
        raise RuntimeError(budget.EXHAUSTED_MESSAGE)


async def _await_approval(con, cfg, runner, worker, run, node, *, poll_seconds, progress, round=1):
    """Hold the node at its accepted plan until its Last Order records the user's go-ahead.

    The node's conversation stays open the whole time: the root's is the user's own window, a
    fork's is its own window in the panel (or, headless, a chat attached to its live session).
    Two tools are put in that conversation for the wait -- revise the plan, start it -- and taken out after;
    nothing here reads the user's words. A revised plan is republished as it lands. Returns
    "stopped" if the run was stopped meanwhile, "skipped" if the user chose to close the node unresearched
    (a non-root node's first plan), else None once the go-ahead is recorded."""
    nid = node["id"]
    with task_store.write_txn(con):
        runs._owned(con, run, node)
        runs.set_node(con, nid, status="awaiting_approval")
        current = runs.node(con, nid)
    session_file = current["session_file"] or run["root_session"]   # the conversation that recorded the plan
    roster = planner._roster({**cfg, "workspace": run["workspace"]})
    forks = planner.decisions_allowed(con, run, node, round)
    tools = commands.review_tools(
        con, run, current, session_file=session_file, round=round, forks=forks,
        validate=lambda value: planner.validate_plan(value, roster, decisions_allowed=forks,
                                                     root=runs.is_root(node) and round <= 1,
                                                     red_team_required=round <= 1))
    plan = runs.action(con, run["id"], nid, runs.plan_key(round))
    if runs.is_root(node) and not getattr(worker, "headless", False):
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
            _halt_at_cap(con, run)
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
        with task_store.write_txn(con):
            if runs.holds_driver(con, run["id"], run["driver_lock"]):
                con.execute("UPDATE research_execution SET status='planning' WHERE id=? "
                            "AND runner_key IS ? AND status='awaiting_approval'", (nid, node["runner_key"]))


async def _skip_node(con, run, node, progress):
    """Close a node the user chose not to research: its plan stays on record as the proposal it
    was, and no card or conclusion ever exists. The final adjudication sees a path not
    researched, with the reason, which is the honest empty ticket."""
    decision = runs.skipped(con, run["id"], node["id"])
    reason = "Skipped by the user before any research: " + ((decision or {}).get("payload") or {}).get("reason", "")
    outcome = await asyncio.to_thread(_close, con, run, node, "parked")
    await _progress(progress, "node_skipped",
                    f"{_label(node)}: skipped by the user; no research was done, and the final adjudication "
                    "sees it as a path not researched.", run, node=node["id"], reason=reason)
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
    """What stands between the run and its final adjudication: a missing root, a failed or
    unfinished node, an option without an outcome, or a reconciliation recorded and never applied."""
    nodes = runs.nodes(con, run_id)
    if not nodes:
        return "the run has no research node: its creation was interrupted, so there is nothing to adjudicate"
    failed = [n for n in nodes if n["status"] == "failed"]
    if failed:
        details = [f"{n['id']} ({n['last_error']})" if n["last_error"] else n["id"] for n in failed]
        return f"{len(failed)} node(s) failed: {', '.join(details)}"
    open_nodes = [n["id"] for n in nodes if n["status"] not in runs.NODE_TERMINAL]
    if open_nodes:
        return f"{len(open_nodes)} node(s) have not finished: {', '.join(open_nodes)}"
    pending = [row["id"] for row in runs.pending_options(con, run_id)]
    if pending:
        return f"{len(pending)} option(s) never received an outcome: {', '.join(pending)}"
    unapplied = [str(item["round"]) for item in runs.reconcile_rounds(con, run_id) if item["applied_at"] is None]
    if unapplied:
        return f"reconciliation {', '.join(unapplied)} was recorded but never applied"
    return None


async def _keep_lease(con, run_id, lock, lost):
    """Renew the driver lease in the background for as long as the run is being driven; a failed
    renewal (another driver took over) raises the flag the main loop checks."""
    renewed_at, wait = time.monotonic(), runs.DRIVER_TTL_SECONDS / 3
    while True:
        await asyncio.sleep(wait)
        try:
            renewed = runs.heartbeat_driver(con, run_id, lock)
        except sqlite3.OperationalError:
            # A board another process holds past the busy timeout is not a lost lease while the
            # lease has time left: tried again shortly (it failed the run as "driver lease lost"
            # with most of the lease to go, 0.18.10 sweep).
            if time.monotonic() - renewed_at < runs.DRIVER_TTL_SECONDS - 30:
                wait = 5
                continue
            lost.set()
            raise
        except Exception:  # No renewal means no authority, including a database failure.
            lost.set()
            raise
        if not renewed:
            lost.set()
            return
        renewed_at, wait = time.monotonic(), runs.DRIVER_TTL_SECONDS / 3


_INCOMPLETE_BANNER = (
    "> **This research is incomplete.** The run stopped before its final adjudication, so nothing below has\n"
    "> been weighed against anything else. Each conclusion is quoted exactly as the node that reached it wrote\n"
    "> it: final-report red-team review and adjudication have not completed, and the issues\n"
    "> listed below were never settled. This is the material the run had produced when it stopped, not its\n"
    "> answer.")


def _cell(value):
    """Keep quoted table data from opening new rows or columns."""
    return " ".join(str(value or "").splitlines()).strip().replace("|", r"\|")


def _nested(text, under):
    """A conclusion with its own headings pushed below the heading of the section quoting it.

    A node writes free-form Markdown and usually opens at ``##``, which outranks the ``###`` naming
    the node it came from: left alone, the second node's heading reads in any outline as part of the
    first node's conclusion, and the graph renders backwards. Only the level moves. The words are
    untouched, as is the synthesis artifact on disk -- that copy, not this one, is what gets cited.
    """
    lines = text.splitlines()
    found = atx_headings(lines)
    if not found:
        return text
    deeper = max(0, under + 1 - min(level for _index, level, _title in found))
    for index, level, _title in found:
        lines[index] = "#" * min(6, level + deeper) + lines[index].lstrip(" ")[level:]
    return "\n".join(lines)


def _conclusions(con, run):
    """Every node's latest conclusion, inlined whole, in graph order.

    A halted run cannot call the model again, and its nodes have already written the only thing it
    has to hand back. Listing those files as paths hands the reader a directory listing instead of
    a document. ``runs.nodes`` orders by depth then creation -- the root, then each level of the
    graph -- which is the order the survey and the adjudication read them in. Only the latest
    version of a revised conclusion is quoted: the earlier ones are what the red team corrected.
    """
    out = []
    for node in runs.nodes(con, run["id"]):
        rows = views.syntheses(con, run, node)
        if not rows:
            continue
        try:
            text = _nested(runs.artifact_text(rows[-1]).strip(), 3)
        except (OSError, ValueError) as error:
            # A conclusion moved or edited under the run costs this document that one section.
            # It must never cost the document: this is a halted run's whole deliverable.
            text = f"*(This conclusion could not be read back: {error})*"
        parents = ", ".join(p["id"] for p in runs.parents(con, node["id"])) or "none"
        version = f", version {len(rows)}" if len(rows) > 1 else ""
        out.append(f"### [{node['id']}] {views.kind(con, node)}, depth {node['depth']}{version} — "
                   f"{_cell(node['question'])}\n\nParents: {parents}\n\n{text}")
    return out


def _open_issues(con, run):
    """The issues nobody settled, as a table.

    ``report._boundary`` is the query, not a copy of it: the boundary a halted run declares and the
    one the final report is held to have to be the same set, or two documents of one run disagree
    about what it left unanswered.
    """
    rows = report._boundary(con, run)
    if not rows:
        return ["No issue was left undisposed or parked."]
    return ["The issues this run left undisposed or parked:", "",
            "| Issue | Node | Disposition | Question | Why it was raised |",
            "| --- | --- | --- | --- | --- |",
            *(f"| {_cell(row['issue'])} | {_cell(row['node'])} | {_cell(row['disposition'])} "
              f"| {_cell(row['question'])} | {_cell(row['rationale'])} |" for row in rows)]


def _partial_result(con, run, reason, *, status="stopped", driver_lock=None):
    """What a run that cannot finish hands back -- a deliverable, never a list of paths.

    Assembled with no model call at all, because the halt this most often answers is the token
    budget: at that moment there is nothing left to spend, and everything the document needs is
    already on disk. Each node's conclusion as that node wrote it, the ledger's count of what the
    run recorded, and the issues it never settled. Blocking (the workspace index and the run's
    bundle read the whole project), so a halt runs it off the event loop.
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
        # A failure says why where the run is looked up, not only in the partial report (B126).
        runs.set_state(con, run["id"], status=status, final_artifact=aid, driver_lock=driver_lock,
                       error=reason[:500] if status == "failed" else None)
    _try_refresh_workspace_index(con, runs.get(con, run["id"]))
    _bundle(con, run)
    views.write_views(con, run, final=True)
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
    if identity is None:
        return
    row = con.execute(f'SELECT runner_pid, runner_identity FROM "{table}" WHERE id=? AND runner_key IS ?',
                      (row_id, key)).fetchone()
    if row is None:
        return
    if row["runner_pid"] is not None and not (
            int(row["runner_pid"]) == int(pid) and processes.same_identity(row["runner_identity"], identity)):
        return
    if row["runner_pid"] is not None:
        return                                   # the child's own reading stands (claim_runner)
    con.execute(f'UPDATE "{table}" SET runner_pid=?, runner_identity=? WHERE id=? AND runner_key IS ? '
                'AND runner_pid IS NULL', (int(pid), identity, row_id, key))


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


def _runner_failure(row, tail=None):
    """Why a node's process ended before its routine did, for the node's record."""
    reason = row["last_error"] or ("no exception was recorded (the process may have been killed externally, "
                                   "or its window closed)")
    if tail:
        reason += f"; its last output: {tail}"
    return f"its process ended while {row['status']}: {reason}"


def _pane_tail(spawner, handle):
    """What the node's window showed last, when the spawner can read it (a pane; not a process)."""
    read = getattr(spawner, "tail", None)
    if not callable(read):
        return None
    try:
        return read(handle)
    except Exception:  # noqa: BLE001 - the tail decorates an error already being raised
        return None


async def _expand_level(con, cfg, spawner, run, level, *, poll_seconds, progress, driver_lock=None):
    """Run the level's nodes through a window as wide as the run's parallelism: a node starts as
    soon as a slot is free, and a slot frees the moment a node leaves the frontier (terminal or
    waiting for input) -- so one node held at its plan, or on a slow Sister, does not hold the
    nodes queued behind it. Returns "done", a halt, or a waiting_input result; on a halt or a node
    waiting for input nothing more is started, and the untouched nodes stay queued for the resume.
    A node whose process dies is marked failed and the level goes on; the run pauses after it for
    the resume that retries the node (2026-09-28: one node's missed command took three others' windows
    and eleven running cards down with it)."""
    handles = {}

    async def start(node):                          # registered one by one: a failed spawn stops the started ones
        await asyncio.to_thread(_reap_orphan_runner, con, "research_execution", node)
        with runs.owned_txn(con, run):
            key = runs.prepare_runner(con, "research_execution", node["id"])
        handle, cancelled = await settle_thread_call(
            spawner.spawn, _node_argv(run, node, key), cwd=run["workspace"])
        handles[node["id"]] = handle
        _record_runner(con, "research_execution", node["id"], handle, key=key)
        if cancelled is not None:
            raise cancelled
        return key
    try:
        return await _wait_level(con, cfg, spawner, run, handles, poll_seconds=poll_seconds, progress=progress,
                                 driver_lock=driver_lock, pending=list(level), width=runs.current_limits(con, run)["parallel"],
                                 start=start)
    except BaseException:
        await _stop_all_off_loop(spawner, handles)
        raise


def _replied_since(path, offset):
    """Whether the conversation file at ``path`` gained, after byte ``offset``, a model reply the
    provider accepted -- an assistant message that is neither an error nor aborted -- and the
    offset to read from next. Only whole lines count: the writer may be mid-append."""
    try:
        with open(path, "rb") as handle:
            if os.fstat(handle.fileno()).st_size < offset:
                offset = 0                          # rewritten in place: read it again from the start
            handle.seek(offset)
            chunk = handle.read()
    except OSError:
        return False, offset
    end = chunk.rfind(b"\n") + 1
    for line in chunk[:end].splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = entry.get("message") if isinstance(entry, dict) else None
        if (entry.get("type") == "message" and isinstance(message, dict) and message.get("role") == "assistant"
                and message.get("stopReason") not in ("error", "aborted")):
            return True, offset + end
    return False, offset + end


async def _wait_level(con, cfg, spawner, run, handles, *, poll_seconds, progress, driver_lock=None,
                      pending=(), width=None, start=None):
    """Watch the running nodes until none is left, starting the next of ``pending`` (through
    ``start``, which returns the node's runner key) whenever fewer than ``width`` run -- unless
    the run halted or a node stopped to wait for the user's input, after which nothing more starts.

    Nodes start one at a time: the next waits until the last one started has had a model reply
    accepted. A node's first request carries the whole conversation it inherited, and a level's
    forks share it; sent together they hit the provider as one burst (2026-09-27: eight at once were
    refused with 502 "Upstream access forbidden", twice). One by one, each finds the shared part
    already in the provider's prompt cache, and the load arrives spread out. Only a node that has
    not planned is waited for: one that has shares no prefix with the level's other forks and may
    send nothing for a long time -- resumed while waiting on its cards (2026-09-28: two 15-minute
    holds on a free slot), retried after a card failed (2026-09-29: nine retried nodes held the
    next start five minutes each)."""
    last = {}
    keys = {nid: runs.node(con, nid)["runner_key"] for nid in handles}
    pending, hold = list(pending), False
    warming = {}                                    # node id -> [conversation file, read offset, deadline] until its first reply

    def still_warming():
        for nid in list(warming):
            replied, warming[nid][1] = _replied_since(*warming[nid][:2])
            if replied or nid not in handles or time.monotonic() > warming[nid][2]:
                warming.pop(nid)
        return bool(warming)

    async def top_up():
        nonlocal hold
        while pending and not hold and (width is None or len(handles) < width) and not still_warming():
            if (runs.stop_requested(con, run["id"]) or budget.exhausted(con, task_id=run["id"])
                    or any(n["status"] == "waiting_input" for n in runs.nodes(con, run["id"]))):
                hold = True
                break
            if driver_lock and runs.get(con, run["id"])["driver_lock"] != driver_lock:
                raise RuntimeError(f"Research run {run['id']}: the driver lease was taken over by another process.")
            node = pending.pop(0)
            conversation = runs.node(con, node["id"])["session_file"]
            offset = os.path.getsize(conversation) if conversation and os.path.isfile(conversation) else None
            keys[node["id"]] = await start(node)
            if offset is not None and runs.action(con, run["id"], node["id"], runs.plan_key(1)) is None:
                # ponytail: a fixed cap; a plan's first reply has taken five minutes, never more.
                warming[node["id"]] = [conversation, offset, time.monotonic() + WARMUP_CAP_SECONDS]
                if pending:
                    await _progress(progress, "node", f"{_label(node)} started; the next of {len(pending)} waiting "
                                    "starts once its first request is answered.", run, node=node["id"])

    await top_up()
    while handles:
        await asyncio.sleep(poll_seconds)
        if driver_lock and not runs.heartbeat_driver(con, run["id"], driver_lock):
            raise RuntimeError(f"Research run {run['id']}: the driver lease was taken over by another process.")
        await _roster_check(cfg, run, progress)
        halted = (runs.stop_requested(con, run["id"])
                  or budget.exhausted(con, task_id=run["id"]))
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
                if not halted and node["status"] not in ("waiting_input", *runs.NODE_TERMINAL):
                    reason = _runner_failure(node, tail=_pane_tail(spawner, handle))
                    with runs.owned_txn(con, run):
                        con.execute("UPDATE research_execution SET last_error=?,status='failed',updated_at=? WHERE id=?",
                                    (reason, int(time.time()), nid))
                    await _progress(progress, "node_failed", f"{_label(node)} failed: {reason}. The level goes on. "
                                    f"If the cause was passing, misaka_research_retry(node=\"{nid}\") runs it again now; "
                                    "otherwise the resume after this level retries it.", run, node=nid)
                handles.pop(nid)
                _clear_runner(con, "research_execution", nid, key=keys[nid])
                hold = hold or node["status"] == "waiting_input"   # the run is about to pause for the user
        # A node Last Order retried (runs.retry_node) is back on the frontier with no process: queue it again.
        queued = {n["id"] for n in pending} | handles.keys()
        pending += [n for n in runs.next_level(con, run["id"]) if n["status"] == "planning"
                    and n["id"] not in queued and n["runner_key"] is None and n["runner_pid"] is None]
        await top_up()
    if runs.stop_requested(con, run["id"]):
        return "stopped"
    if budget.exhausted(con, task_id=run["id"]):
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


def _prepare_sessions(con, run):
    """Give every node that has not run yet its conversation: a fork of the Last Order conversation
    of its ``views.fork_source`` -- its parent, or for a join the lowest node its parents share --
    with ``fork_entry`` marking where its own turns begin. A fork written just before a crash is
    found in the node's own folder and adopted, since the node never ran in it."""
    from misaka.core.session_manager import findMostRecentSession

    for node in runs.nodes(con, run["id"]):
        if node["status"] != "queued" or node["session_file"] or runs.is_root(node):
            continue
        source = views.fork_source(con, node)
        directory = planner._lo_session(run, node)
        session = findMostRecentSession(directory)
        entry = planner.session_leaf(session) if session else None
        if not session:
            session, entry = planner.fork_session(planner.lo_session_file(run, source), directory)
        if not session:
            raise RuntimeError(f"Node {node['id']}: {source['id']}'s Last Order session has no forkable history.")
        with runs.owned_txn(con, run):
            runs.set_fork_entry(con, node["id"], entry)
            runs.set_node(con, node["id"], session_file=session)


def _reconcile_material(con, run):
    """What the root Last Order reconciles: every pending option with the decision it belongs to (and
    the paths it says it brings back), every node that finished its own work, every coverage claim
    across lines waiting for its review, and the limits."""
    chosen = runs.current_limits(con, run)
    workspace = run["workspace"]
    rel = lambda path: posix_relpath(path, workspace) if path else None
    covered = set()
    for item in runs.reconcile_rounds(con, run["id"]):
        if item["applied_at"] is not None:
            covered.update(item["receipt"]["considered"])
    finished = []
    for node in runs.nodes(con, run["id"]):
        if node["status"] != "closed":
            continue
        conclusions = views.syntheses(con, run, node)
        conclusion = runs.artifact_path(conclusions[-1]) if conclusions else None
        finished.append({"id": node["id"], "kind": views.kind(con, node), "depth": node["depth"],
                         "question": node["question"], "new_since_last_reconciliation": node["id"] not in covered,
                         "parents": [p["id"] for p in runs.parents(con, node["id"])],
                         "conclusion": rel(conclusion), "conclusion_doc": conclusion and report.doc_id(run, conclusion),
                         "node_view": views.node_view_path(node["id"])})
    snapshot = views.snapshot(con, run)
    recorded = {entry["id"]: entry for group in snapshot["paths"] for entry in group["entries"]}

    def brought_back(record):
        entry = recorded.get(record, {})
        return {"path": record, "text": entry.get("text"), "went": entry.get("outcome")}

    pending = [{"option": row["id"], "label": row["label"], "premise": row["premise"],
                "decision": row["decision_question"], "stakes": row["decision_stakes"],
                "decided_at": row["decided_at"], "decided_in": row["decision_origin"],
                **({"answers": [brought_back(record) for record in row["answers"]]} if row["answers"] else {})}
               for row in views.reconcilable_options(con, run)]
    return {"graph": views.graph_path(run), "paths": views.paths_path(run),
            "limits": {"max_depth": chosen["max_depth"], "max_nodes": chosen["max_nodes"],
                       "nodes_now": len(runs.nodes(con, run["id"]))},
            "pending_options": pending, "coverage_claims": views.coverage_claims(con, run),
            "finished_nodes": finished, "relations_so_far": runs.relations(con, run["id"])}


async def _reconcile(con, cfg, run, window, *, poll_seconds, progress, check_active):
    """The level barrier: when the graph calls for it (``views.reconcile_state``), the root Last Order
    reconciles in the run's own conversation and the result is applied in one transaction. A
    recorded reconciliation is never asked for twice; one that no longer fits the graph is dropped
    and asked for again. Returns None, or a halt ("stopped") while waiting for the user's go-ahead."""
    rounds = runs.reconcile_rounds(con, run["id"])
    if rounds and rounds[-1]["applied_at"] is None:
        round = rounds[-1]["round"]
    else:
        needed, _considered = views.reconcile_state(con, run)
        if not needed:
            return None
        round = rounds[-1]["round"] + 1 if rounds else 1
    await _views(con, run)

    def validate(payload):
        return views.resolve_reconcile(con, runs.get(con, run["id"]), payload)

    await _progress(progress, "reconciling", f"The root Last Order is reconciling the graph (round {round}).", run)
    for attempt in (1, 2):
        check_active()
        await asyncio.to_thread(planner.reconcile, con, run, cfg, window, round=round,
                                material=_reconcile_material(con, run), validate=validate)
        if planner.plan_waits_for_user(cfg, run) and not runs.reconcile_approved(con, run["id"], round):
            halted = await _await_reconcile_approval(con, run, window, round=round, validate=validate,
                                                     poll_seconds=poll_seconds, progress=progress)
            if halted:
                return halted
        try:
            resolved = validate(runs.reconcile(con, run["id"], round)["payload"])
            break
        except ValueError:
            if attempt == 2:
                raise
            with runs.owned_txn(con, run):
                runs.withdraw_reconcile(con, run["id"], round)
    _needed, considered = views.reconcile_state(con, run)
    receipt = runs.apply_reconcile(con, run, round, resolved, considered=considered)
    opened = len(receipt["nodes"]) + len(receipt["joins"])
    reopened = sum(1 for item in resolved.get("coverage", ()) if item["verdict"] == "reopen")
    reviewed = (f", {len(resolved['coverage'])} coverage claim(s) reviewed ({reopened} reopened)"
                if resolved.get("coverage") else "")
    await _progress(progress, "reconciled",
                    f"Reconciliation {round}: {opened} node(s) opened"
                    + (f" ({len(receipt['joins'])} join(s))" if receipt["joins"] else "")
                    + f", {len(resolved['not_pursued'])} option(s) not pursued{reviewed}, "
                    f"{len(resolved['relations'])} relation(s) recorded.", run,
                    nodes=[*receipt["nodes"], *receipt["joins"]])
    await _views(con, run)
    return None


async def _await_reconcile_approval(con, run, window, *, round, validate, poll_seconds, progress):
    """Hold the reconciliation until the root Last Order records the user's go-ahead, in the run's
    own conversation (the user's window, or the headless root attached with ``misaka chat``). As
    with a plan: revise or start, nothing read from prose. Returns "stopped" or None."""
    tools = commands.reconcile_review_tools(con, run, round=round, validate=validate,
                                            session_file=run["root_session"])
    session = getattr(window, "session", None)
    control = _session_control(session)
    where = ("in this window" if not getattr(window, "headless", False)
             else f"with `misaka chat --attach --session {run['root_session']}`")
    try:
        await _progress(progress, "reconcile_review",
                        f"Reconciliation {round} is recorded and waits for your go-ahead. Talk it over with the root "
                        f"Last Order {where}; she records the start once you agree.", run, round=round)
        if session is not None:
            session.registerCustomTools(tools)
        if control is not None:
            control.review_tools = tools
        while True:
            if runs.stop_requested(con, run["id"]):
                return "stopped"
            runs.check_owner(con, run, allow_stop=True)
            if runs.reconcile_approved(con, run["id"], round):
                return None
            await asyncio.sleep(poll_seconds)
    finally:
        if session is not None:
            session.unregisterCustomTools(tools)
        if control is not None and control.review_tools is tools:
            control.review_tools = ()


async def _await_consent(con, cfg, run, window, *, poll_seconds, progress, check_active):
    """When a node's conclusion dissolves the research question itself, the user decides whether the
    final answer may say so -- in any run, whatever its approval policy, as with a reframed question.
    The root Last Order puts it to them in the run's conversation, then the report waits until she
    records their decision. Returns None, or "stopped"."""
    dissolving = views.dissolving_nodes(con, run)
    root = runs.root(con, run["id"])
    if not dissolving or runs.action(con, run["id"], root["id"], runs.CONSENT_KEY):
        return None
    material = [{"node": node["id"], "question": node["question"],
                 "conclusion": runs.artifact_path(views.syntheses(con, run, node)[-1]),
                 "grounds": views.dissolution(con, run, node)["grounds"]} for node in dissolving]
    where = ("in this window" if not getattr(window, "headless", False)
             else f"with `misaka chat --attach --session {run['root_session']}`")
    await _progress(progress, "dissolution", "The research concludes that the question itself dissolves "
                    f"({', '.join(n['id'] for n in dissolving)}); the final report waits for your decision. "
                    f"Talk it over with the root Last Order {where}; she records it once you have decided.", run,
                    nodes=[n["id"] for n in dissolving])
    check_active()
    await asyncio.to_thread(planner.present_dissolution, con, run, cfg, window, material=material)
    tool = commands.consent_tool(con, run, session_file=run["root_session"])
    session = getattr(window, "session", None)
    control = _session_control(session)
    try:
        if session is not None:
            session.registerCustomTools([tool])
        if control is not None:
            control.review_tools = [tool]
        while True:
            if runs.stop_requested(con, run["id"]):
                return "stopped"
            _halt_at_cap(con, run)
            runs.check_owner(con, run, allow_stop=True)
            if runs.action(con, run["id"], root["id"], runs.CONSENT_KEY):
                return None
            await asyncio.sleep(poll_seconds)
    finally:
        if session is not None:
            session.unregisterCustomTools([tool])
        if control is not None and control.review_tools and control.review_tools[0] is tool:
            control.review_tools = ()


async def run(con, cfg, spawner, *, run_id, poll_seconds=POLL_SECONDS, progress=None,
              resume=False, clarification="", origin_session=None, session=None):
    """Advance one persisted research run until it finishes, stops, or needs user input.
    Reuse the supplied root session or own one headless root through final adjudication.
    Only fork node LOs are processes managed by ``spawner``."""
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
    cfg = dict(cfg)
    halts = {"stopped": "The user requested a stop.", "budget": "The run reached research.token_cap; raise it and resume the run to go on."}
    window = root_runner = root_context = None
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

    async def partial(reason, *, status="stopped"):
        check_active(allow_stop=True)
        # Every halt lands here, the cancel and interrupt paths included: what the Sisters
        # finished is registered, ingested and bundled before the partial report is written,
        # so the report and a later resume see it (2026-09-23: a driver crash left 39 done
        # cards unregistered for five hours). A settle failure must not cost the report.
        try:
            await run_in_thread(settle_done_tasks, con, run_id=run_id, reopen_drift=False)
        except Exception:  # noqa: BLE001 - the partial report is what a halt owes the user
            _LOG.exception("Research %s: settling finished cards before the partial report failed", run_id)
        return await run_in_thread(_partial_result, con, run, reason, status=status, driver_lock=driver_lock)

    try:
        # Stop shallower writers before refreshing the child inventory: one may have
        # created a child after our first read. No task generation changes until all stop.
        reaped = set()
        while pending := [n for n in runs.nodes(con, run_id) if n["id"] not in reaped]:
            for row in pending:
                check_active(allow_stop=True)
                await asyncio.to_thread(_reap_orphan_runner, con, "research_execution", row)
                reaped.add(row["id"])
        if origin_session:
            from misaka.core.platform import notifications
            with task_store.write_txn(con):
                current = runs.get(con, run_id)
                if current["origin_session"] != origin_session:
                    if runs.holds_driver(con, run_id, driver_lock):
                        con.execute("UPDATE research_runs SET origin_session=? WHERE id=?", (origin_session, run_id))
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
            return await partial(halts["stopped"])
        if budget.exhausted(con, task_id=run_id):
            return await partial(halts["budget"])
        from misaka.core.research.node import HeadlessRunner, PaneRunner
        from misaka.core.research.window import WindowLO, node_description, node_session
        root = runs.root(con, run_id)
        # The driver owns the root in-process; never register it as a killable child.
        with runs.owned_txn(con, run):
            runs.prepare_runner(con, "research_execution", root["id"])
        pane = os.environ.get("MISAKA_NET_PANE") if session is not None else None
        if session is None:
            context = node_session(con, cfg, runs.get(con, run_id), runs.node(con, root["id"]))
            window = await context.__aenter__()
            root_context = context
            session = window.session
        else:
            window = WindowLO(session, check_active, describe=lambda: node_description(con, root["id"]))
        check_active()
        # The root's own conversation begins where the run met this window: what the window held
        # before is not the node's deliberation. A window adopted with --here begins anew.
        bound = runs.node(con, root["id"])["session_file"]
        rebound = not bound or os.path.realpath(bound) != os.path.realpath(window.session_file)
        leaf = await asyncio.to_thread(planner.session_leaf, window.session_file) if rebound else None
        with runs.owned_txn(con, run):
            runs.set_state(con, run_id, root_session=window.session_file, driver_lock=driver_lock)
            if rebound:
                runs.set_fork_entry(con, root["id"], leaf)
            runs.set_node(con, root["id"], session_file=window.session_file)
        root_runner = PaneRunner(con, cfg, run_id, pane) if pane else HeadlessRunner(con, cfg)
        run = runs.get(con, run_id)
        if run["phase"] == "created":
            runs.set_state(con, run_id, phase="active", driver_lock=driver_lock)
        while True:                                   # breadth-first: one level at a time, its nodes in parallel
            check_active()
            result = await _reconcile(con, cfg, runs.get(con, run_id), window, poll_seconds=poll_seconds,
                                      progress=progress, check_active=check_active)
            if result is not None:
                return (await partial(halts[result])) if result in halts else result
            await asyncio.to_thread(_prepare_sessions, con, run)
            await _views(con, run)
            level = runs.next_level(con, run_id)
            if not level:
                break
            runs.set_state(con, run_id, wave=level[0]["depth"], driver_lock=driver_lock)
            await _progress(progress, "level", f"Depth {level[0]['depth']}: {len(level)} node(s) expanding.", run,
                            nodes=[n["id"] for n in level])
            if runs.is_root(level[0]):
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
                return await partial(halts[result])
        check_active()
        unfinished = _unfinished_reason(con, run_id)
        if unfinished:
            await _progress(progress, "unfinished", f"The run cannot be adjudicated: {unfinished}", run)
            return await partial(unfinished, status="failed")
        halted = await _await_consent(con, cfg, runs.get(con, run_id), window, poll_seconds=poll_seconds,
                                      progress=progress, check_active=check_active)
        if halted:
            return await partial(halts[halted])
        runs.set_state(con, run_id, phase="finalizing", driver_lock=driver_lock)
        await _progress(progress, "finalizing", "Every node is closed; Last Order is drafting the report for independent red-team review.", run)
        draft = await asyncio.to_thread(report.prepare, con, runs.get(con, run_id), cfg, window,
                                        check_active=check_active)
        check_active()
        root = runs.root(con, run_id)
        spec = {"local_id": "@final-review", "title": "Red team · final report",
                "assignee": _red_team_assignee(con, run, root), "instructions": report.review_body(con, run, draft)}
        linked = await _submit_tasks(con, run, root, [spec], kind="final_review", progress=progress)
        check_active()
        review_id = linked["@final-review"]
        target = {"artifact": draft["id"], "sha256": draft["sha256"]}
        previous = task_store.latest_payload(con, review_id, "research_review_target")
        if previous is None:
            task_store.add_event(con, review_id, "research_review_target", target)
        elif json.loads(previous) != target:
            raise ValueError("Final red-team card belongs to a different draft.")
        await _progress(progress, "final_review", "The independent red team is reviewing the saved report draft.", run,
                        task_id=review_id, draft_path=draft["path"])
        outcome = await _drive_tasks(con, cfg, root_runner, run_id, scope={review_id},
                                     tool_call_id=f"research:{run_id}:final-review", poll_seconds=poll_seconds,
                                     progress=progress, check_active=lambda: check_active(allow_stop=True), session=session,
                                     owner=(run, None))
        check_active(allow_stop=True)
        await run_in_thread(settle_done_tasks, con, run_id=run_id)
        if outcome in halts:
            return await partial(halts[outcome])
        if outcome != "done":
            return await partial("Final-report red-team review failed.", status="failed")
        try:
            report.review_receipt(con, run, draft, review_id)
        except (OSError, TypeError, ValueError) as error:
            task_store.add_event(con, review_id, "research_review_missing", {"reason": str(error)})
            return await partial(str(error), status="failed")
        check_active()
        await _progress(progress, "adjudicating", "Last Order is weighing the final red team's objections; objections are not automatic verdicts.", run)
        result = await asyncio.to_thread(report.finalize, con, runs.get(con, run_id), cfg, window,
                                         review_task_id=review_id, check_active=check_active)
        check_active()
        runs.set_state(con, run_id, phase="done", status="done", final_artifact=result["artifact"],
                       driver_lock=driver_lock)
        await run_in_thread(_try_refresh_workspace_index, con, runs.get(con, run_id))
        await run_in_thread(_bundle, con, run)
        await _views(con, run, final=True)
        return {"reason": "done", "final": result, "run": runs.summary(con, run_id)}
    except asyncio.CancelledError:
        current = runs.get(con, run_id)
        if current["driver_lock"] == driver_lock:
            runs.request_stop(con, run_id)
            await _settle_stopped_tasks(con, cfg, run_id)
            await partial(halts["stopped"])
        raise
    except InterruptedError:
        return await partial(halts["stopped"])
    except Exception as error:
        current = runs.get(con, run_id)
        if current["status"] == "done":
            raise
        if budget.exhausted(con, task_id=run_id):
            # A phase stopped because the run reached research.token_cap: a halt, resumable.
            await _settle_stopped_tasks(con, cfg, run_id)
            return await partial(halts["budget"])
        if current["driver_lock"] == driver_lock:
            runs.set_state(con, run_id, status="failed", error=f"{type(error).__name__}: {error}"[:500],
                           driver_lock=driver_lock)
        raise
    finally:
        try:
            if root_context is not None:
                await root_context.__aexit__(*sys.exc_info())
            elif window is not None:
                await window.close()
        finally:
            try:
                if root_runner is not None and hasattr(root_runner, "close"):
                    await asyncio.to_thread(root_runner.close)
            finally:
                keeper.cancel()
                await asyncio.gather(keeper, return_exceptions=True)
                runs.release_driver(con, run_id, driver_lock)

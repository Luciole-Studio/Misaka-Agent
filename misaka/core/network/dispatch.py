"""Run cards headlessly and settle their board-owned submissions."""
import hashlib
import json
import os
import secrets
import socket
from dataclasses import dataclass
from pathlib import Path

from misaka.core.documents import workspace as ws_index
from misaka.core.network import worker
from misaka.core.platform import admission, budget
from misaka.core.platform import tasks as db

_skipped_logged = set()


def _profile_dir(cfg, assignee):
    for root in (cfg["profiles_root"], cfg["roles_root"]):
        d = os.path.join(root, assignee)
        if os.path.isdir(d):
            return d
    return None


def _event_summary(d, nbytes):
    """Summarize an oversized harness event: keep its type, tool name, and first line of text."""
    keep = {"type": d.get("type") or "event", "truncated_bytes": nbytes}
    for k in ("toolName", "name"):
        if isinstance(d.get(k), str):
            keep[k] = d[k]
    for k in ("text", "delta", "thinking", "error", "errorMessage"):
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            keep[k] = v.strip().splitlines()[0][:300]
            break
    return json.dumps(keep, ensure_ascii=False)


def _compact_event(line, cap=4000):
    """Cap a harness event line at ``cap`` bytes, anything over it reduced to a summary. An
    ``agent_end`` keeps no usage: every model request is recorded in the ledger by the meter as it
    ends (``misaka.core.platform.metering``), and a total here would count the turn twice -- old
    rows that carry one are still counted (``budget.spent``), new ones never do.

    A ``message_update`` is not kept (None): it is one streamed delta of the message its
    ``message_end`` records whole, and kept, a card wrote a board transaction per token -- 46,000
    rows, 22 MB for one run (0.18.10 sweep)."""
    if '"message_update"' in line:
        try:
            if json.loads(line).get("type") == "message_update":
                return None
        except ValueError:
            pass
    if '"agent_end"' in line:
        try:
            if json.loads(line).get("type") == "agent_end":
                return json.dumps({"type": "agent_end"})
        except ValueError:
            pass
    if len(line) <= cap:
        return line
    try:
        d = json.loads(line)
    except ValueError:
        return json.dumps({"type": "raw", "truncated_bytes": len(line),
                           "preview": line[:200]}, ensure_ascii=False)
    return _event_summary(d, len(line))


def _worker_identity():
    """A verifiable identity for the process that is about to claim a card.

    A bare PID was enough while the claimer was one long-lived dispatcher. A card is now
    claimed by the short-lived child that runs it, and a killed child's PID comes back around:
    ``reconcile`` below would find a live unrelated process behind the recorded number, skip the
    card forever, and the drive loop would offer it again on every poll.

    When the claiming process leads its own process group -- the shape a workflow's node process
    starts a card child in -- publish the group form the Sister path uses, so the reconciler can also
    fence the model session the child left behind before the card is given to a new owner. The
    prefix is a promise about the process group, so it is only made when it is true: claimed from
    inside someone else's group, it would aim ``terminate_orphaned_group`` at that group.
    """
    from misaka.core.platform import processes as process_tree
    from misaka.core.subagent.child import PROCESS_GROUP_IDENTITY
    me = process_tree.identity(os.getpid())
    if not me:
        return None
    try:
        leads_group = os.name == "posix" and os.getpgrp() == os.getpid()
    except OSError:
        leads_group = False
    return PROCESS_GROUP_IDENTITY + me if leads_group else me


def _last_order_lock(lock):
    return str(lock or "").split(":", 1)[0] in {"lo", "lo-retry", "lo-resume"}


def _idle_seconds(t, now):
    """How long the card has shown no progress, once past the wedge threshold; else None."""
    last = t["heartbeat_at"]
    if last is None:
        return None
    idle = now - int(last)
    return idle if idle >= db.HEARTBEAT_STALE_SECONDS else None


def reconcile(con, cfg, *, task_ids=None, workspace=None):
    """Settle every running card whose worker is gone, or wedged.

    A worker is gone when its lease has lapsed and neither its claimer nor its recorded
    process identity is alive; its claim is released under the exact ownership fence, so
    a paused-but-live Last Order keeps the Sister it still owns. A worker is wedged when
    its process is alive but the card has shown no progress for ``HEARTBEAT_STALE_SECONDS``.
    Only a card whose recorded process leads its own process group is stopped for that (a
    headless worker, a Sister root): a panel card records no group and is never in this branch,
    and a card under a Last Order's lock (``lo*``) is left to her -- her process hosts other work
    and stops its own wedged Sisters itself. ``task_ids`` and ``workspace`` narrow the pass.
    """
    import time as _time

    from misaka.core.network.sister_runtime import _claimer_alive, _owner_alive
    from misaka.core.platform import processes as process_tree
    from misaka.core.subagent.child import PROCESS_GROUP_IDENTITY

    wanted = set(task_ids) if task_ids is not None else None
    now = int(_time.time())
    # A consultation whose process went with its daemon: settled, and its sender told.
    from misaka.core.network import messages
    for run in db.abandon_consultations(con, now=now):
        messages.tell_asker(con, run, run["summary"])
    for t in db.by_status(con, "running", workspace=workspace):
        if wanted is not None and t["id"] not in wanted:
            continue
        expires = t["claim_expires"]
        if expires is not None and int(expires) >= now and _claimer_alive(t["claim_lock"]):
            continue
        stored = str(t["worker_identity"] or "")
        reason = db.ABANDONED_REASON
        if stored.startswith(PROCESS_GROUP_IDENTITY):
            leader = stored[len(PROCESS_GROUP_IDENTITY):]
            idle = None if _last_order_lock(t["claim_lock"]) else _idle_seconds(t, now)
            alive = process_tree.identity_is_alive(t["worker_pid"], leader)
            if idle is None and alive:
                continue
            # A dead leader's group is fenced before its lease goes; a wedged live group is
            # stopped the same way (SIGTERM, then SIGKILL) and reclaimed only once it is gone.
            if not process_tree.terminate_orphaned_group(int(t["worker_pid"]), leader):
                continue
            if idle is not None and alive:
                reason = f"the worker showed no progress for {idle} s and was stopped"
        elif _owner_alive(t):
            # A recorded identity is checked against the PID that holds it now, so a reused
            # PID does not read as the original worker.
            continue
        from misaka.utils import loop_watchdog
        stall = loop_watchdog.report(f"card-{t['id']}", t["worker_pid"])
        if stall:
            reason = f"{reason}; {stall}"
        try:
            finish_abandoned(con, t, reason=reason)
        except Exception as error:  # noqa: BLE001 - one card must not strand the rest
            # A card whose workspace has gone (project deleted or moved) cannot have its file
            # rewritten, so settling it raises. Without this guard that exception leaves the
            # loop and every card after it keeps its expired lease forever -- which is how two
            # cards on this author's board sat `running` for 111 hours behind one card whose
            # directory no longer existed. The lease is the thing that must not outlive its
            # worker, so release it in the database even when the file cannot be updated, and
            # record why. `board()` already models this shape as `missing_file`.
            db.add_event(con, t["id"], "reconcile_failed",
                         {"error": f"{type(error).__name__}: {error}"[:500]},
                         generation=t["generation"])



def run_task(con, t, cfg, *, say=None):
    """Claim a ready card and run it; with ``say``, continue the settled card ``t`` instead -- a new
    generation in her own conversation, the message being the turn (the headless counterpart of
    ``pane.continue_card``, which ``misaka_sister_message`` uses in a panel)."""
    from misaka.core.network.ally import presets
    host_cap, assignee_cap = admission.limits()
    identity = _worker_identity()
    ally = t["assignee"] in presets.names()
    profile_dir = None if ally else _profile_dir(cfg, t["assignee"])
    if not ally and not profile_dir:
        if (t["id"], t["generation"]) not in _skipped_logged:    # a reopened card gets a fresh look
            _skipped_logged.add((t["id"], t["generation"]))
            lock = f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"
            generation = int(t["generation"])
            if db.claim(con, t["id"], lock, ttl_seconds=60,
                        generation=generation, pid=os.getpid(), worker_identity=identity,
                        host_cap=host_cap, assignee_cap=assignee_cap):
                reason = f"Assignee profile not found: {t['assignee']}"
                db.add_event(con, t["id"], "failed", {"reason": reason},
                             generation=generation, claim_lock=lock)
                db.mark_failed(con, t["id"], generation=generation, claim_lock=lock,
                               failure_kind="configuration", reason=reason)
        return False

    lock = f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"
    generation = int(t["generation"])
    if say is not None:
        if not db.claim_resume(con, t["id"], lock, os.getpid(), worker_identity=identity,
                               expected_generation=generation, host_cap=host_cap, assignee_cap=assignee_cap):
            return False
        t = db.get(con, t["id"])
        generation = int(t["generation"])
    elif not db.claim(
        con,
        t["id"],
        lock,
        ttl_seconds=1800,
        generation=generation,
        pid=os.getpid(),
        worker_identity=identity,
        host_cap=host_cap,
        assignee_cap=assignee_cap,
    ):
        return False
    workspace = db.workspace_for(t)
    if not os.path.isdir(workspace):          # the card's folder is the project; a gone one is never recreated in silence
        db.block_task(con, t["id"], "needs_input", f"the project folder no longer exists: {workspace}",
                      generation=generation)
        return False
    if not t["workspace"] and not db.set_workspace(
        con, t["id"], workspace, generation=generation, claim_lock=lock
    ):
        return False
    run_dir = workspace
    db.add_event(
        con,
        t["id"],
        "continued" if say is not None else "claimed",
        {"lock": lock, "workspace": workspace},
        generation=generation,
    )

    task = dict(t)
    if say is not None:
        task["_say"] = say
    from misaka.core.platform import cards as card_files
    task["_attachments"] = card_files.attachment_list(run_dir, t["id"], workspace=workspace)
    task["_handoffs"] = worker.card_handoffs(con, t)
    task.update(worker.card_extras(con, t, cfg))
    bud = budget.status(con, cfg.get("token_cap"), task_id=t["id"])
    if bud["mode"] == "stop":
        if db.back_to_ready(
            con, t["id"], generation=generation, claim_lock=lock
        ):
            db.add_event(
                con, t["id"], "budget_stop", bud, generation=generation
            )
        return False
    if bud["mode"] == "beast":
        db.add_event(con, t["id"], "beast_mode", bud)
        task["beast"] = True
    usage_db = cfg.get("db")
    if not usage_db:
        try:
            usage_db = con.execute("PRAGMA database_list").fetchone()[2] or None
        except (AttributeError, IndexError, TypeError):
            usage_db = None
    try:
        verdict = _run_ally(con, task, generation, lock, usage_db, say) if ally else worker.run_card(
            task, run_dir, profile_dir, cfg["provider"], cfg["default_model"],
            on_event=lambda line: (event := _compact_event(line)) is not None and db.add_event(
                con,
                t["id"],
                "harn_event",
                event,
                generation=generation,
                claim_lock=lock,
            ),
            usage_db=usage_db,
            usage_generation=generation,
            usage_claim_lock=lock,
            usage_token_cap=cfg.get("token_cap"),
            con=con,
        )
    except BaseException as error:
        # Cancellation may be retried; an actual startup/runtime fault must not spawn
        # the same broken card every scheduler tick. Preserve its actionable cause.
        with db.write_txn(con):
            settle = db.mark_failed if isinstance(error, Exception) else db.back_to_ready
            reason = f"{type(error).__name__}: {error}"[:2000]
            details = {"reason": reason, "failure_kind": "crash"} if isinstance(error, Exception) else {}
            if settle(con, t["id"], generation=generation, claim_lock=lock, **details):
                db.add_event(con, t["id"], "dispatch_error",
                             {"reason": reason},
                             generation=generation)
        raise
    if verdict.get("budget_stop"):
        if db.back_to_ready(
            con, t["id"], generation=generation, claim_lock=lock
        ):
            db.add_event(
                con,
                t["id"],
                "budget_stop",
                budget.status(con, cfg.get("token_cap"), task_id=t["id"]),
                generation=generation,
            )
    elif verdict.get("settled"):
        pass
    elif verdict.get("exit_code") == 0:
        db.mark_unsettled(con, t["id"], generation=generation, claim_lock=lock)
    else:
        failure = {
            "reason": str(verdict["reason"]),
            "exit_code": verdict["exit_code"],
            "stderr_tail": verdict.get("stderr_tail", "")[-500:],
        }
        blocked = str(verdict["reason"]).startswith("blocked:")
        if blocked:
            db.block_task(
                con, t["id"], "needs_input",
                str(verdict["reason"])[len("blocked:"):].strip(),
                generation=generation, claim_lock=lock,
            )
        elif db.add_event(
            con, t["id"], "failed", failure,
            generation=generation, claim_lock=lock,
        ):
            db.mark_failed(con, t["id"], generation=generation, claim_lock=lock)
    return True


def _run_ally(con, task, generation, lock, usage_db, say):
    """An ally's attempt, in this process: its runner settles the card, or leaves it running."""
    from misaka.core.network.ally import card as ally_card
    from misaka.core.platform.session import run_coro

    run_coro(ally_card.run_attempt(task, generation, lock, db_path=usage_db, say=say))
    row = db.get(con, task["id"])
    if row is not None and row["status"] == "running" and row["claim_lock"] == lock:
        return {"exit_code": 0}                  # idle without completing: unsettled
    return {"settled": True}


# A review card's typed record, frozen with its submission: a red team's issues, a divergence
# review's alternatives. Whatever ``worker.build_submission`` attaches must pass through here --
# the alternatives once did not, and every divergence review failed its node (2026-09-27).
REVIEW_FIELDS = ("issues", "alternatives")


def _submitted(submission, *, artifact_digests=None):
    return {"summary": submission["summary"], "artifacts": submission.get("artifacts", []),
            "notes": submission.get("notes", ""), "uncertain": submission.get("uncertain", []),
            "findings": submission.get("findings", []),
            "artifact_digests": artifact_digests or {},
            **{field: submission[field] for field in REVIEW_FIELDS if field in submission}}


def finish_abandoned(con, t, *, reason=db.ABANDONED_REASON):
    """Release a dead worker's unfinished claim.

    A completed lifecycle hook has already moved the row out of ``running`` and written its
    ``submitted`` event atomically.  A dead worker still owning a running row therefore has no
    result to recover or inspect.
    """
    fence = {"generation": t["generation"], "claim_lock": t["claim_lock"], "worker_pid": t["worker_pid"],
                 "worker_identity": t["worker_identity"], "claim_expires": t["claim_expires"]}
    outcome = db.reclaim_abandoned(con, t["id"], reason=reason, **fence)
    if outcome != "ready":
        return outcome            # None: the fence moved on; "failed": out of attempts, gave_up recorded
    db.add_event(
        con,
        t["id"],
        "reclaimed",
        {"reason": reason},
        generation=t["generation"],
    )
    return "reclaimed"


def index_after_review(con, task_id, generation):
    """A reviewer's approval finishes the card: its last submitted artifacts join the corpus now."""
    row = con.execute(
        "SELECT payload FROM events WHERE task_id=? AND kind='submitted' AND generation=? "
        "ORDER BY id DESC LIMIT 1",
        (task_id, generation),
    ).fetchone()
    try:
        artifacts = json.loads(row["payload"] or "{}").get("artifacts") or [] if row else []
    except (TypeError, ValueError):
        artifacts = []
    index_artifacts(con, task_id, artifacts, generation)


@dataclass(frozen=True)
class _PreparedSubmission:
    owner: tuple
    payload: dict


def prepare_submission(t, submission):
    """Freeze attachment bytes before acceptance; async hosts call this off their loop."""
    # Hash outside the short ownership transaction. Caller-provided digests are never trusted.
    root = Path(t["workspace"]).resolve()
    digests = {}
    for name in submission.get("artifacts") or []:
        path = (root / str(name)).resolve()
        try:
            path.relative_to(root)
            with path.open("rb") as source:
                digests[str(name)] = hashlib.file_digest(source, "sha256").hexdigest()
        except (OSError, ValueError):
            # Record absence too: a file appearing later is not an accepted attachment.
            digests[str(name)] = None
    return _PreparedSubmission((t["id"], t["generation"], t["claim_lock"], t["workspace"]),
                               _submitted(submission, artifact_digests=digests))


def accept_state(con, t, submission, *, generation, claim_lock):
    """Land the ownership CAS and its immutable payload without yielding to another turn."""
    prepared = submission if isinstance(submission, _PreparedSubmission) else prepare_submission(t, submission)
    if prepared.owner != (t["id"], generation, claim_lock, t["workspace"]):
        raise ValueError("Prepared submission belongs to a different card attempt")
    with db.write_txn(con):                                # done and its submitted payload land together
        if not db.submit_task(con, t["id"], generation=generation, claim_lock=claim_lock):
            return False
        db.add_event(con, t["id"], "submitted", prepared.payload, generation=generation)
    return True


def accept_side_effects(con, t, submission, *, generation):
    """Index a submission whose state transition has already landed: its artifacts join the
    project's document corpus. Nothing is committed; the project's git history is the user's."""
    index_artifacts(con, t["id"], submission.get("artifacts", []), generation)


def accept(con, t, submission, *, generation, claim_lock):
    """Accept atomically, then run the slower indexing tail."""
    if not accept_state(
        con, t, submission, generation=generation, claim_lock=claim_lock
    ):
        return False
    accept_side_effects(con, t, submission, generation=generation)
    return True


def redeclare(con, row):
    """Declare a finished card's outputs again, as a newer ``submitted`` row of its generation.

    Completion does not end a card's session: a note from Last Order that crossed the
    completion, or a message she answers in a consultation, starts more turns in it, and the
    deliverables they change stayed declared under the old digests. The run's settle then saw
    bytes that did not match the declaration and, until 2026-09-24, failed the whole run (18
    changed paths on 6 cards). The declaration follows the work instead: the same submission
    the completion built, rebuilt from the output folder and the findings recorded in the
    generation, and indexed again. No model turn is spent on it. Blocking; a Sister's session
    and an ally's runner both call it. True when a newer declaration was recorded."""
    generation = int(row["generation"])
    try:
        last = json.loads(db.latest_payload(con, row["id"], "submitted", generation=generation) or "{}")
    except (TypeError, ValueError):
        last = {}
    if not isinstance(last, dict):
        last = {}
    try:
        submission = worker.build_submission(con, row, str(last.get("summary") or ""))
        prepared = prepare_submission(row, submission)
    except Exception as error:  # noqa: BLE001 - a declaration that cannot be rebuilt is noted, not fatal
        db.add_event(con, row["id"], "redeclare_failed", {"reason": str(error)[:200]}, generation=generation)
        return False
    payload = prepared.payload
    # Findings recorded after the completion change the declaration as much as a file does.
    if all(payload.get(key) == last.get(key) for key in ("artifact_digests", "artifacts", "findings", "uncertain")):
        return False
    with db.write_txn(con):
        db.add_event(con, row["id"], "submitted", payload, generation=generation)
        db.add_event(con, row["id"], "redeclared", {"artifacts": len(payload.get("artifacts") or [])},
                     generation=generation)
    accept_side_effects(con, row, submission, generation=generation)
    return True


def index_artifacts(con, task_id, artifacts, generation):
    """A done card's artifacts join the corpus (PageIndex); an index failure never blocks the card."""
    row = db.get(con, task_id)
    if row is None or row["status"] != "done" or int(row["generation"]) != int(generation):
        return                                # a newer generation owns the card: nothing of ours to index
    try:
        got = ws_index.ingest_artifacts(con, row, artifacts=artifacts)
    except Exception as error:  # noqa: BLE001 - a derived index must not undo an acceptance
        db.add_event(con, task_id, "index_error", {"error": str(error)[:200]}, generation=generation)
        return
    if got:
        db.add_event(con, task_id, "indexed", {"docs": [item[0] for item in got]}, generation=generation)
    # ingest_artifacts refuses a deliverable by *returning without it* (no extractor for the
    # suffix, a scanned PDF with no OCR, an unreadable EPUB, an escaping or missing path) --
    # only an infrastructure fault reaches the except above. Recording just the successes left a
    # card that delivered three files and indexed none with no trace on the board, in the card
    # file, or in the ledger, while every later reader assumed the corpus had them. The failure
    # still must not undo the acceptance: it is an event, not a status.
    landed = {item[1] for item in got}
    missed = [str(name) for name in (artifacts or []) if str(name) not in landed]
    if missed:
        db.add_event(con, task_id, "index_skipped", {"artifacts": missed[:20]},
                     generation=generation)

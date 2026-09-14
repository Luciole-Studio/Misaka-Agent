"""Run cards headlessly and settle their board-owned submissions."""
import json
import os
import secrets
import socket

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
    """Cap a harness event line at ``cap`` bytes: reduce ``agent_end`` to a token total, anything else to a summary."""
    if len(line) <= cap:
        return line
    try:
        d = json.loads(line)
    except ValueError:
        return json.dumps({"type": "raw", "truncated_bytes": len(line),
                           "preview": line[:200]}, ensure_ascii=False)
    if d.get("type") == "agent_end":
        total = 0
        for message in d.get("messages") or []:
            usage = message.get("usage") if isinstance(message, dict) else None
            if not isinstance(usage, dict):
                continue
            if isinstance(usage.get("totalTokens"), int):
                total += usage["totalTokens"]
            else:
                total += sum(
                    int(usage.get(key) or 0)
                    for key in (
                        "input",
                        "output",
                        "cacheRead",
                        "cacheWrite",
                        "input_tokens",
                        "output_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                    )
                )
        # Always valid JSON, bounded regardless of message count or size;
        # slicing the serialized ledger used to yield invalid JSON.
        return json.dumps(
            {
                "type": "agent_end",
                "messages": [{"role": "assistant", "usage": {"totalTokens": total}}],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return _event_summary(d, len(line))


def _worker_identity():
    """A verifiable identity for the process that is about to claim a card.

    A bare PID was enough while the claimer was one long-lived dispatcher. A card is now
    claimed by the short-lived child that runs it, and a killed child's PID comes back around:
    ``reconcile`` below would find a live unrelated process behind the recorded number, skip the
    card forever, and the drive loop would offer it again on every poll.

    When the claiming process leads its own process group -- the shape ``research.node`` starts a
    card child in -- publish the group form the Sister path uses, so the reconciler can also
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
    Only a headless card process is stopped for that: it runs in its own process group under
    its own lease, whereas a Last Order's or the panel's lease stands for a process that
    hosts other work and stops its own wedged Sisters itself. ``task_ids`` and ``workspace``
    narrow the pass.
    """
    import time as _time

    from misaka.core.network.sister_runtime import _claimer_alive, _owner_alive
    from misaka.core.platform import processes as process_tree
    from misaka.core.subagent.child import PROCESS_GROUP_IDENTITY

    wanted = set(task_ids) if task_ids is not None else None
    now = int(_time.time())
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



def run_task(con, t, cfg):
    host_cap, assignee_cap = admission.limits()
    identity = _worker_identity()
    profile_dir = _profile_dir(cfg, t["assignee"])
    if not profile_dir:
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
    if not db.claim(
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
        "claimed",
        {"lock": lock, "workspace": workspace},
        generation=generation,
    )

    task = dict(t)
    from misaka.core.platform import cards as card_files
    task["_attachments"] = card_files.attachment_list(run_dir, t["id"], workspace=workspace)
    task["_handoffs"] = worker.card_handoffs(con, t)
    task.update(worker.card_extras(con, t, cfg, include_colleagues=False))
    bud = budget.status(con, cfg.get("token_cap"))
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
        verdict = worker.run_card(
            task, run_dir, profile_dir, cfg["provider"], cfg["default_model"],
            on_event=lambda line: db.add_event(
                con,
                t["id"],
                "harn_event",
                _compact_event(line),
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
            details = {"reason": reason} if isinstance(error, Exception) else {}
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
                budget.status(con, cfg.get("token_cap")),
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


def _submitted(submission):
    return {"summary": submission["summary"], "artifacts": submission.get("artifacts", []),
            "notes": submission.get("notes", ""), "uncertain": submission.get("uncertain", []),
            "findings": submission.get("findings", []),
            **({"issues": submission["issues"]} if "issues" in submission else {})}


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


def accept_state(con, t, submission, *, generation, claim_lock):
    """Land the ownership CAS and its immutable payload without yielding to another turn."""
    with db.write_txn(con):                                # done and its submitted payload land together
        if not db.submit_task(
            con, t["id"], generation=generation, claim_lock=claim_lock, commit=False
        ):
            return False
        db.add_event(con, t["id"], "submitted", _submitted(submission), generation=generation)
    return True


def _research_linked(con, task_id):
    """True when the card belongs to a research run: its Git history is written per node when
    the node closes, not per card, so acceptance skips the per-card commit."""
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_run_tasks'").fetchone():
            return False
        return con.execute("SELECT 1 FROM research_run_tasks WHERE task_id=?", (task_id,)).fetchone() is not None
    except Exception:  # noqa: BLE001 - a board without the research schema is an ordinary board
        return False


def accept_side_effects(con, t, submission, *, generation, workspace):
    """Commit and index a submission whose state transition has already landed."""
    from filelock import FileLock

    from misaka.core.platform import cards, repo

    committed = True
    # Generation changes publish through this same card lock. Read the file fence,
    # not SQLite, while holding it: transitions acquire DB -> card, never card -> DB.
    # Git can wait on its own repository lock without occupying the board's writer.
    with FileLock(os.path.join(db.task_state_dir(t["id"]), "card.lock")):
        try:
            fields = cards.read(cards.card_path(workspace, t["id"]))["fields"]
            current = (int(fields.get("generation", 1)) == int(generation)
                       and fields.get("status") in {"done", "review"})
        except (OSError, ValueError, TypeError):
            current = False
        if current and repo.enabled(workspace) and not _research_linked(con, t["id"]):
            committed = repo.commit_card(
                workspace, t["id"], submission, f"card {t['id']}: submit"
            )
    if not committed:
        db.add_event(con, t["id"], "git_commit_pending", {"reason": "submit"}, generation=generation)
    index_artifacts(con, t["id"], submission.get("artifacts", []), generation)


def accept(con, t, submission, *, generation, claim_lock, workspace):
    """Accept atomically, then run the slower Git and indexing tail."""
    if not accept_state(
        con, t, submission, generation=generation, claim_lock=claim_lock
    ):
        return False
    accept_side_effects(
        con, t, submission, generation=generation, workspace=workspace,
    )
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

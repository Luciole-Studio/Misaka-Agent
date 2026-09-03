"""Run cards headlessly and settle their submissions. Submission is acceptance: a valid
report.json, committed on the card's line, makes the card done (or hands it to the reviewer
it names); nothing re-checks it afterwards."""
import json
import os
import secrets
import socket

from misaka.core.network import worker
from misaka.core.platform import admission, budget
from misaka.core.platform import tasks as db
from misaka.documents import workspace as ws_index

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


def reconcile(con, cfg):
    import time as _time

    from misaka.core.network.sister_runtime import _claimer_alive, _owner_alive
    from misaka.core.platform import processes as process_tree
    from misaka.core.subagent.child import PROCESS_GROUP_IDENTITY

    now = int(_time.time())
    for t in db.by_status(con, "running"):
        expires = t["claim_expires"]
        if expires is not None and int(expires) >= now and _claimer_alive(t["claim_lock"]):
            continue
        stored = str(t["worker_identity"] or "")
        if stored.startswith(PROCESS_GROUP_IDENTITY):
            # Match sister_runtime: reclaim a grouped orphan only after its process group is gone.
            leader = stored[len(PROCESS_GROUP_IDENTITY):]
            if process_tree.identity_is_alive(t["worker_pid"], leader):
                continue
            if not process_tree.terminate_orphaned_group(int(t["worker_pid"]), leader):
                continue
        elif _owner_alive(t):
            # Also the Sister path's rule: a recorded identity is checked against the PID that
            # holds it now, so a reused PID does not read as the original worker.
            continue
        try:
            finish_abandoned(con, t)
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
                db.add_event(con, t["id"], "failed",
                             {"reason": f"Assignee profile not found: {t['assignee']}"},
                             generation=generation, claim_lock=lock)
                db.mark_failed(con, t["id"], generation=generation, claim_lock=lock)
        return False

    lock = f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"
    generation = int(t["generation"])
    if not db.claim(
        con,
        t["id"],
        lock,
        ttl_seconds=max(1800, int(t["timeout_seconds"]) + 60),
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
    except BaseException:  # Ensure a terminating dispatcher never leaves the task marked running.
        if db.back_to_ready(con, t["id"], generation=generation, claim_lock=lock):
            db.add_event(con, t["id"], "dispatch_error",
                         {"reason": "run_card crashed"}, generation=generation)
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
    elif verdict["ok"]:
        accept(con, t, verdict["report"], generation=generation, claim_lock=lock, workspace=run_dir)
    else:
        failure = {
            "reason": str(verdict["reason"]),
            "exit_code": verdict["exit_code"],
            "timed_out": verdict["timed_out"],
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


def _submitted(report):
    return {"summary": report["summary"], "artifacts": report.get("artifacts", []),
            "notes": report.get("notes", ""), "uncertain": report.get("uncertain", [])}


def _owned(con, task_id, *, generation, claim_lock):
    """True while the card is still running under this exact claim (the fence ``submit_task``
    applies), so no git side effect happens on behalf of an owner the board has already replaced."""
    row = db.get(con, task_id)
    if row is None or (generation is not None and int(row["generation"]) != int(generation)):
        return False
    if claim_lock is None:
        return True
    return (row["status"] == "running" and row["claim_lock"] == claim_lock
            and row["claim_expires"] is not None and int(row["claim_expires"]) >= int(_now()))


def _now():
    import time
    return int(time.time())


def finish_abandoned(con, t):
    """The shared tail of both reconcilers (``reconcile`` here, the Sister runtime's
    ``_reconcile_abandoned``): validate the dead worker's report and, under its exact ownership
    fence, block it, accept it (commit + corpus) or send it back. Returns ``"blocked"``,
    ``"submitted"``, ``"reclaimed"`` or ``None`` when someone else got there first."""
    from misaka.core.platform import repo
    ok, result = worker.check_report(db.workspace_for(t), con=con, task_id=t["id"], generation=t["generation"])
    fence = {"generation": t["generation"], "claim_lock": t["claim_lock"], "worker_pid": t["worker_pid"],
                 "worker_identity": t["worker_identity"], "claim_expires": t["claim_expires"]}
    if not ok and str(result).startswith("blocked:"):
        return "blocked" if db.block_abandoned(
            con, t["id"], "needs_input", str(result)[len("blocked:"):].strip(), **fence) else None
    if ok:
        workspace = db.workspace_for(t)
        with db.write_txn(con):               # acceptance and its submitted payload land together
            if not db.reclaim_abandoned(con, t["id"], submitted=True, **fence):
                return None
            db.add_event(con, t["id"], "submitted", {**_submitted(result), "reconciled": True},
                         generation=t["generation"])
        if repo.enabled(workspace) and not repo.commit_card(
                workspace, t["id"], result, f"card {t['id']}: submit (reconciled)"):
            db.add_event(con, t["id"], "git_commit_pending", {"reason": "submit-reconciled"},
                         generation=t["generation"])
        index_artifacts(con, t["id"], result.get("artifacts", []), t["generation"])
        return "submitted"
    if not db.reclaim_abandoned(con, t["id"], submitted=False, **fence):
        return None
    db.add_event(con, t["id"], "reclaimed", {"reason": str(result)[:500]}, generation=t["generation"])
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


def accept(con, t, report, *, generation, claim_lock, workspace):
    """Submission is acceptance: the ownership CAS in ``submit_task`` is the final word, and done
    lands with its submitted payload in one transaction. Git records the acceptance afterwards --
    only the CAS winner commits, so a losing owner can no longer leave a stale commit; a failed
    commit leaves a ``git_commit_pending`` event (the cards model: git is history, not a veto)."""
    from misaka.core.platform import repo
    if not _owned(con, t["id"], generation=generation, claim_lock=claim_lock):
        return False
    with db.write_txn(con):                                # done and its submitted payload land together
        if not db.submit_task(con, t["id"], generation=generation, claim_lock=claim_lock):
            return False
        db.add_event(con, t["id"], "submitted", _submitted(report), generation=generation)
    if repo.enabled(workspace) and not repo.commit_card(workspace, t["id"], report, f"card {t['id']}: submit"):
        db.add_event(con, t["id"], "git_commit_pending", {"reason": "submit"}, generation=generation)
    index_artifacts(con, t["id"], report.get("artifacts", []), generation)
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


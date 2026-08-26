"""Run cards headlessly and settle their submissions. Submission is acceptance: a valid
report.json, committed on the card's line, makes the card done (or hands it to the reviewer
it names); nothing re-checks it afterwards."""
import json
import os
import secrets
import socket

from misaka.platform import tasks as db
from misaka.platform import admission, budget
from misaka.documents import workspace as ws_index
from misaka.network import worker

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


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def reconcile(con, cfg):
    import time as _time

    from misaka.network.sister_runtime import _claimer_alive
    from misaka.extensions.sisters.subagent.child import PROCESS_GROUP_IDENTITY
    from misaka.platform import processes as process_tree

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
        elif _pid_alive(t["worker_pid"]):
            continue
        finish_abandoned(con, t)



def run_task(con, t, cfg):
    host_cap, assignee_cap = admission.limits()
    profile_dir = _profile_dir(cfg, t["assignee"])
    if not profile_dir:
        if t["id"] not in _skipped_logged:
            _skipped_logged.add(t["id"])
            lock = f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"
            generation = int(t["generation"])
            if db.claim(con, t["id"], lock, ttl_seconds=60,
                        generation=generation, pid=os.getpid(),
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
        host_cap=host_cap,
        assignee_cap=assignee_cap,
    ):
        return False
    workspace = db.workspace_for(t)
    os.makedirs(workspace, exist_ok=True)
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
    from misaka.platform import cards as card_files
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


def _fence_intact(con, t):
    """True while the abandoned card still carries the exact ownership ``reclaim_abandoned`` matches."""
    row = db.get(con, t["id"])
    return row is not None and row["status"] == "running" and all(
        row[key] == t[key] for key in ("generation", "claim_lock", "worker_pid", "worker_identity", "claim_expires"))


def _now():
    import time
    return int(time.time())


def finish_abandoned(con, t):
    """The shared tail of both reconcilers (``reconcile`` here, the Sister runtime's
    ``_reconcile_abandoned``): validate the dead worker's report and, under its exact ownership
    fence, block it, accept it (commit + corpus) or send it back. Returns ``"blocked"``,
    ``"submitted"``, ``"reclaimed"`` or ``None`` when someone else got there first."""
    from misaka.platform import repo
    ok, result = worker.check_report(db.workspace_for(t), con=con, task_id=t["id"])
    fence = dict(generation=t["generation"], claim_lock=t["claim_lock"], worker_pid=t["worker_pid"],
                 worker_identity=t["worker_identity"], claim_expires=t["claim_expires"])
    if not ok and str(result).startswith("blocked:"):
        return "blocked" if db.block_abandoned(
            con, t["id"], "needs_input", str(result)[len("blocked:"):].strip(), **fence) else None
    if ok:
        if not _fence_intact(con, t):
            return None
        repo.commit_card(db.workspace_for(t), t["id"], result, f"card {t['id']}: submit (reconciled)")
    if not db.reclaim_abandoned(con, t["id"], submitted=bool(ok), **fence):
        return None
    if ok:
        db.add_event(con, t["id"], "submitted", {**_submitted(result), "reconciled": True},
                     generation=t["generation"])
        index_artifacts(con, t["id"], result.get("artifacts", []), t["generation"])
        return "submitted"
    db.add_event(con, t["id"], "reclaimed", {"reason": str(result)[:500]}, generation=t["generation"])
    return "reclaimed"


def index_after_review(con, task_id, generation):
    """A reviewer's approval finishes the card: its last submitted artifacts join the corpus now."""
    row = con.execute(
        "SELECT payload FROM events WHERE task_id=? AND kind='submitted' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    try:
        artifacts = json.loads(row["payload"] or "{}").get("artifacts") or [] if row else []
    except (TypeError, ValueError):
        artifacts = []
    index_artifacts(con, task_id, artifacts, generation)


def accept(con, t, report, *, generation, claim_lock, workspace):
    """Submission is acceptance: commit the report's artifacts on the card's line; the card is
    done (or waits for the reviewer it names) and its artifacts join the corpus. The commit
    happens only for the card's current owner; the CAS in ``submit_task`` is the final word."""
    from misaka.platform import repo
    if not _owned(con, t["id"], generation=generation, claim_lock=claim_lock):
        return False
    repo.commit_card(workspace, t["id"], report, f"card {t['id']}: submit")
    if not db.submit_task(con, t["id"], generation=generation, claim_lock=claim_lock):
        return False
    db.add_event(con, t["id"], "submitted", _submitted(report), generation=generation)
    index_artifacts(con, t["id"], report.get("artifacts", []), generation)
    return True


def index_artifacts(con, task_id, artifacts, generation):
    """A done card's artifacts join the corpus (PageIndex); an index failure never blocks the card."""
    row = db.get(con, task_id)
    if row is None or row["status"] != "done":
        return
    try:
        got = ws_index.ingest_artifacts(con, row, artifacts=artifacts)
    except Exception as error:  # noqa: BLE001 - a derived index must not undo an acceptance
        db.add_event(con, task_id, "index_error", {"error": str(error)[:200]}, generation=generation)
        return
    if got:
        db.add_event(con, task_id, "indexed", {"docs": [item[0] for item in got]}, generation=generation)


def dispatch_once(con, cfg, task_ids=None):
    reconcile(con, cfg)
    n = 0
    wanted = set(task_ids) if task_ids is not None else None
    for t in db.fair_ready(con, lane="workers"):
        if wanted is not None and t["id"] not in wanted:
            continue
        n += run_task(con, db.get(con, t["id"]), cfg) or 0
    return n


if __name__ == "__main__":      # self-check: submission is acceptance (done / review / reclaimed after a crash)
    import tempfile
    import time as _time
    from pathlib import Path

    from misaka.config import CFG
    from misaka.platform import repo

    tmp = tempfile.mkdtemp(prefix="misaka-accept-")
    CFG["tasks_root"] = os.path.join(tmp, "task-state")
    ws_index.ingest_artifacts = lambda con, task, artifacts=None: [("doc", a) for a in artifacts or []]  # no corpus here
    ws = os.path.join(tmp, "p")
    os.makedirs(os.path.join(ws, "cards"))
    repo._git(ws, "init", "-q")
    Path(ws, "PROJECT.md").write_text("x\n")
    repo.commit(ws, ["PROJECT.md"], "init")
    con = db.connect(os.path.join(tmp, "board.db"))

    def card(**kw):
        tid = db.create_task(con, "t", body="## goal\nx", assignee="s1", workspace=ws, **kw)
        Path(ws, "cards", f"{tid}.md").write_text("card\n")
        assert db.claim(con, tid, "lock", ttl_seconds=60, generation=1, pid=os.getpid())
        Path(ws, f"{tid}.md").write_text("out\n")
        return tid, {"summary": "s", "artifacts": [f"{tid}.md"], "uncertain": [], "notes": ""}

    tid, rep = card()                                                   # plain card: submit == done
    assert accept(con, db.get(con, tid), rep, generation=1, claim_lock="lock", workspace=ws)
    row = db.get(con, tid)
    assert row["status"] == "done" and row["completed_at"] and row["claim_lock"] is None, dict(row)
    assert f"card {tid}: submit" in repo._git(ws, "log", "--oneline").stdout
    kinds = [r["kind"] for r in con.execute("SELECT kind FROM events WHERE task_id=? ORDER BY id", (tid,))]
    assert kinds[-2:] == ["submitted", "indexed"], kinds
    assert not accept(con, db.get(con, tid), rep, generation=1, claim_lock="lock", workspace=ws)   # not running any more

    tid, rep = card(reviewer="r1")                                     # a named reviewer gates acceptance
    assert accept(con, db.get(con, tid), rep, generation=1, claim_lock="lock", workspace=ws)
    assert db.get(con, tid)["status"] == "review"
    assert db.claim_review(con, tid, "r1", "rlock", generation=1)
    assert db.approve_review(con, tid, "rlock", generation=1)
    assert db.get(con, tid)["status"] == "done"

    tid, rep = card()                                                   # the worker died after writing a valid report
    t = db.get(con, tid)
    assert db.reclaim_abandoned(con, tid, generation=1, claim_lock="lock", worker_pid=t["worker_pid"],
                                worker_identity=t["worker_identity"], claim_expires=t["claim_expires"], submitted=True)
    index_artifacts(con, tid, rep["artifacts"], 1)
    assert db.get(con, tid)["status"] == "done"
    print("dispatch accept self-check OK")

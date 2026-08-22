"""Run cards and reconcile their artifacts through the red-team acceptance gate."""
import json
import os
import secrets
import socket
import subprocess

from misaka.platform import tasks as db
from misaka.network import validate
from misaka.platform import admission, budget, prompt_guard
from misaka.documents import workspace as ws_index
from misaka.network import worker

MAX_VERIFY_ROUNDS = 2
_skipped_logged = set()


def _profile_dir(cfg, assignee):
    for root in (cfg["profiles_root"], cfg["roles_root"]):
        d = os.path.join(root, assignee)
        if os.path.isdir(d):
            return d
    return None


def run_hooks(cfg, task, workspace):
    """Run executable acceptance hooks and return their rejection reasons."""
    hook_dir = cfg.get("hooks_dir") or ""
    if not os.path.isdir(hook_dir):
        return []
    vetoes = []
    for name in sorted(os.listdir(hook_dir)):
        path = os.path.join(hook_dir, name)
        if not os.access(path, os.X_OK) or name.startswith("."):
            continue
        try:
            env = os.environ.copy()
            env["MISAKA_TASK_DIR"] = db.task_state_dir(task["id"])
            env["MISAKA_REPORT"] = os.path.join(env["MISAKA_TASK_DIR"], "report.json")
            p = subprocess.run(
                [path, workspace], capture_output=True, text=True, timeout=60, env=env
            )
        except (OSError, subprocess.SubprocessError) as e:
            vetoes.append(f"Hook {name} failed to run: {e}")
            continue
        if p.returncode != 0:
            vetoes.append(f"[{name}] {(p.stdout + p.stderr).strip()[:200]}")
    return vetoes


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
    from misaka.extensions.subagent.child import PROCESS_GROUP_IDENTITY
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
        ok, result = worker.check_report(
            db.workspace_for(t), con=con, task_id=t["id"])
        if not ok and str(result).startswith("blocked:"):
            db.block_abandoned(
                con, t["id"], "needs_input", str(result)[len("blocked:"):].strip(),
                generation=t["generation"], claim_lock=t["claim_lock"],
                worker_pid=t["worker_pid"], worker_identity=t["worker_identity"],
                claim_expires=t["claim_expires"],
            )
            continue
        if not db.reclaim_abandoned(
            con,
            t["id"],
            generation=t["generation"],
            claim_lock=t["claim_lock"],
            worker_pid=t["worker_pid"],
            worker_identity=t["worker_identity"],
            claim_expires=t["claim_expires"],
            submitted=bool(ok),
        ):
            continue
        if ok:
            db.add_event(
                con,
                t["id"],
                "submitted",
                {
                    "summary": result["summary"],
                    "artifacts": result.get("artifacts", []),
                    "notes": result.get("notes", ""),
                    "uncertain": result.get("uncertain", []),
                    "reconciled": True,
                },
                generation=t["generation"],
            )
        else:
            db.add_event(
                con,
                t["id"],
                "reclaimed",
                {"reason": str(result)},
                generation=t["generation"],
            )


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
    db.add_event(
        con,
        t["id"],
        "claimed",
        {"lock": lock, "workspace": workspace},
        generation=generation,
    )

    task = dict(t)
    task["_attachments"] = db.stage_attachments(con, t["id"], workspace)
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
    fb = db.latest_payload(
        con, t["id"], "verify_fail", generation=generation
    )
    if fb:
        fixes = json.loads(fb).get("must_fix", [])
        task["feedback"] = (
            (task.get("feedback") or "")
            + "⚠️ The previous submission was rejected. Address these required fixes:\n"
            + "\n".join(f"- {item}" for item in fixes)
        )

    usage_db = cfg.get("db")
    if not usage_db:
        try:
            usage_db = con.execute("PRAGMA database_list").fetchone()[2] or None
        except (AttributeError, IndexError, TypeError):
            usage_db = None
    try:
        verdict = worker.run_card(
            task, workspace, profile_dir, cfg["provider"], cfg["default_model"],
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
        if db.mark_verifying(
            con, t["id"], generation=generation, claim_lock=lock
        ):
            db.add_event(
                con,
                t["id"],
                "submitted",
                {
                    "summary": verdict["report"]["summary"],
                    "artifacts": verdict["report"]["artifacts"],
                    "notes": verdict["report"].get("notes", ""),
                    "uncertain": verdict["report"].get("uncertain", []),
                },
                generation=generation,
            )
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


def _publish_finalizing(con, t, verify_token, generation, reasons, artifacts=None):
    """Complete finalization: index artifacts and record ``verify_pass``. Returns False if this judge no longer owns the verification."""
    if verify_token is None or not db.owns_verification(
        con, t["id"], verify_token, generation=generation
    ):
        return False
    if not db.owns_verification(
        con, t["id"], verify_token, generation=generation
    ) or not db.finish_finalize(
        con, t["id"], verify_token, generation=generation
    ):
        return False

    got = []
    index_error = None
    try:
        got = ws_index.ingest_artifacts(con, t, artifacts=artifacts)
    except Exception as error:  # A derived-index failure must not block task finalization.
        index_error = error

    if index_error is not None:
        db.add_event(
            con,
            t["id"],
            "index_error",
            {"error": str(index_error)[:200]},
            generation=generation,
        )
    elif got:
        db.add_event(
            con,
            t["id"],
            "indexed",
            {"docs": [item[0] for item in got]},
            generation=generation,
        )
    db.add_event(
        con,
        t["id"],
        "verify_pass",
        {"reasons": reasons},
        generation=generation,
    )
    return True


def judge_task(con, t, cfg, verify_token=None, generation=None):
    """Run the mandatory red-team gate for one verifying card."""
    generation = int(t["generation"] if generation is None else generation)
    if verify_token is not None and not db.owns_verification(
        con, t["id"], verify_token, generation=generation
    ):
        return
    ws = db.workspace_for(t)
    valid, report_or_reason = worker.check_report(ws or "", task_id=t["id"])
    if not valid:
        if db.back_to_ready(
            con, t["id"], verify_token, generation=generation
        ):
            db.add_event(
                con,
                t["id"],
                "verify_reclaimed",
                {"reason": f"verifying report invalid: {report_or_reason}"[:500]},
                generation=generation,
            )
        return
    verified_artifacts = list(report_or_reason.get("artifacts", []))
    report_limit = worker.MAX_REPORT_BYTES
    try:
        with open(os.path.join(db.task_state_dir(t["id"]), "report.json"), "rb") as f:
            report_bytes = f.read(report_limit + 1)
        if len(report_bytes) > report_limit:
            raise ValueError("report.json too large")
        report_full = report_bytes.decode("utf-8")
        report_raw = report_full[:4000]
    except (OSError, UnicodeDecodeError, ValueError):
        if db.back_to_ready(
            con, t["id"], verify_token, generation=generation
        ):
            db.add_event(
                con,
                t["id"],
                "verify_reclaimed",
                {"reason": "report.json disappeared during verification"},
                generation=generation,
            )
        return
    if t["status"] == "finalizing":
        decision = db.latest_payload(
            con, t["id"], "verify_decision_pass", generation=generation
        )
        try:
            reasons = json.loads(decision or "{}").get("reasons") or []
        except (TypeError, ValueError, AttributeError):
            reasons = []
        _publish_finalizing(con, t, verify_token, generation, reasons,
                            artifacts=verified_artifacts)
        return
    try:
        uncertain = json.loads(report_full).get("uncertain") or []
    except (ValueError, AttributeError):
        uncertain = []
    focus = ("\n# Author-reported uncertainties to verify first\n"
             + "\n".join(f"- {x}" for x in uncertain[:3]) + "\n") if uncertain else ""
    prompt = f"""# Task contract to review
Title: {t['title']}

{t['body']}

# Submitted report.json
{prompt_guard.untrusted('report.json', report_raw)}{focus}

The task workspace is your working directory. Verify each acceptance criterion against the actual deliverables with the read tool. Return only this JSON object:
{{"pass": true|false, "reasons": ["..."], "must_fix": ["..."]}}"""
    for attempt in (1, 2):
        if verify_token is not None and not db.owns_verification(
            con, t["id"], verify_token, generation=generation
        ):
            return
        obj, _raw, err = worker.run_llm_json(
            os.path.join(cfg["roles_root"], "redteam"), prompt,
            cfg["provider"], cfg["default_model"],
            cwd=ws, tools=["read"], timeout=cfg.get("judge_timeout", 600),
            usage_db=cfg["db"],
            usage_task_id=t["id"],
            usage_generation=generation,
            usage_token_cap=cfg.get("token_cap"),
            on_event=lambda line: db.add_event(
                con,
                t["id"],
                "harn_event",
                _compact_event(line),
                generation=generation,
            ),
        )
        errors = validate.validate_verdict(obj) if err is None else [err]
        if not errors:
            break
        if verify_token is None or db.owns_verification(
            con, t["id"], verify_token, generation=generation
        ):
            db.add_event(
                con,
                t["id"],
                "verify_error",
                {"attempt": attempt, "errors": errors},
                generation=generation,
            )
        else:
            return
    if errors:
        # Two malformed judge responses indicate infrastructure trouble; keep the task verifiable.
        return
    if obj["pass"]:
        vetoes = run_hooks(cfg, t, t["workspace"] or "")
        if vetoes:
            rounds = db.bump_verify(
                con, t["id"], verify_token, generation=generation
            )
            if rounds is None:
                return
            db.add_event(
                con,
                t["id"],
                "hook_veto",
                {"round": rounds, "vetoes": vetoes},
                generation=generation,
            )
            if rounds >= MAX_VERIFY_ROUNDS:
                failure = {
                    "reason": "verification hooks failed repeatedly",
                    "must_fix": vetoes,
                }
                if db.add_event(
                    con,
                    t["id"],
                    "failed",
                    failure,
                    generation=generation,
                    verify_lock=verify_token,
                ):
                    db.mark_failed(
                        con, t["id"], verify_token, generation=generation
                    )
            else:
                if db.back_to_ready(
                    con, t["id"], verify_token, generation=generation
                ):
                    db.add_event(
                        con,
                        t["id"],
                        "verify_fail",
                        {"round": rounds, "must_fix": vetoes},
                        generation=generation,
                    )
            return
        db.add_event(
            con,
            t["id"],
            "verify_decision_pass",
            {"reasons": obj["reasons"]},
            generation=generation,
        )
        if verify_token is None or not db.begin_finalize(
            con, t["id"], verify_token, generation=generation
        ):
            return
        finalizing = dict(t)
        finalizing["status"] = "finalizing"
        _publish_finalizing(
            con,
            finalizing,
            verify_token,
            generation,
            obj["reasons"],
            artifacts=verified_artifacts,
        )
    else:
        rounds = db.bump_verify(
            con, t["id"], verify_token, generation=generation
        )
        if rounds is None:
            return
        if rounds >= MAX_VERIFY_ROUNDS:
            failure = {
                "reason": "; ".join(obj["must_fix"]) or "verification failed repeatedly",
                "round": rounds,
                "must_fix": obj["must_fix"],
            }
            if db.add_event(
                con,
                t["id"],
                "failed",
                failure,
                generation=generation,
                verify_lock=verify_token,
            ) and db.mark_failed(
                con, t["id"], verify_token, generation=generation
            ):
                db.add_event(
                    con,
                    t["id"],
                    "verify_gave_up",
                    {"round": rounds, "must_fix": obj["must_fix"]},
                    generation=generation,
                )
        else:
            if db.back_to_ready(
                con, t["id"], verify_token, generation=generation
            ):
                db.add_event(
                    con,
                    t["id"],
                    "verify_fail",
                    {"round": rounds, "must_fix": obj["must_fix"]},
                    generation=generation,
                )


def dispatch_once(con, cfg, task_ids=None):
    reconcile(con, cfg)
    n = 0
    wanted = set(task_ids) if task_ids is not None else None
    for t in db.fair_ready(con, lane="workers"):
        if wanted is not None and t["id"] not in wanted:
            continue
        n += run_task(con, db.get(con, t["id"]), cfg) or 0
    for t in [*db.by_status(con, "verifying"), *db.by_status(con, "finalizing")]:
        if wanted is not None and t["id"] not in wanted:
            continue
        token = f"judge:{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"
        ttl = max(1800, int(cfg.get("judge_timeout", 600)) * 7 + 300)
        generation = int(t["generation"])
        if db.claim_verification(
            con, t["id"], token, ttl, generation=generation
        ):
            try:
                judge_task(con, db.get(con, t["id"]), cfg, token, generation=generation)
            finally:
                db.release_verification(
                    con, t["id"], token, generation=generation
                )
            n += 1
    return n
